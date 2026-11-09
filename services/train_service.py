"""Train lifecycle and operation service.

The HTTP layer in ``routes/trains.py`` parses JSON and shapes the
sibling-payload response (`train` / `controller` / `dispatcher_progress`);
this layer owns the domain mutations behind every train endpoint:

  * CRUD -- create (placement-aware or editor), update, delete.
  * Placement -- reposition, speed-profile assignment.
  * Route assignment / retarget -- the abort-latch + assign/unassign
    transaction per design_new.md "Operator retargeting / route swap".
  * Operator commands -- power, handle, mode, ATO go/stop, EB. Each runs
    between ticks through ``operator_command``: it holds
    ``state.simulation_lock``, refuses an unidentified or ghosted train, runs
    the controller command, reports its result event, and returns the
    controller's ``CommandResult`` so the caller can surface ``success`` /
    ``message`` (design_new.md "Operator-command processing slot").

Lookup raises ``NotFoundError``; bad input raises ``ValidationError``;
a refused assignment raises ``ConflictError``.
``reposition_train`` and ``assign_speed_profile`` take
``state.simulation_lock`` (the routes they replace did).
"""

from __future__ import annotations

import contextlib
import functools
from typing import Any, Dict, List, Optional, Tuple

from app_state import AppState
from enums import CommandResult, Direction, HandlePosition
from services.exceptions import ConflictError, NotFoundError, ValidationError
from train import Train, TrainCar
from train_route import RouteStartMode


# --------------------------------------------------------------- lookups ----


def _controller(state: AppState, train: Train):
    """Return (creating if needed) the TrainController for a train."""
    return state.sim_controller._get_or_create_controller(train)


def get_train(state: AppState, train_id: str) -> Optional[Train]:
    """Return the train, or None when not found."""
    return state.network.trains.get(train_id)


def require_train(state: AppState, train_id: str) -> Train:
    """Return the train or raise NotFoundError."""
    train = state.network.trains.get(train_id)
    if train is None:
        raise NotFoundError("Train not found")
    return train


def _unidentified_refusal_message(train_id: str) -> str:
    return (
        f"Train {train_id} is an unidentified train: the app cannot drive it. It has no "
        f"cab, no route, and no operator controls."
    )


def _ghost_refusal_message(train_id: str) -> str:
    return (
        f"Train {train_id} has lost detection: no detector backs its position, so it "
        f"accepts no command until detection returns."
    )


@contextlib.contextmanager
def _runtime_lock(state: AppState):
    """Hold ``state.simulation_lock`` so the caller runs between two ticks."""
    lock = getattr(state, "simulation_lock", None)
    if lock is None:
        yield
        return
    with lock:
        yield


def operator_command(operation: str, refuse_ghost: bool = True):
    """Run an operator train command between ticks (design_new.md
    "Operator-command processing slot").

    The wrapper:
      * refuses an unidentified train, which never accepts an operator command
        (design_new.md "Unidentified trains", Never driven);
      * holds ``state.simulation_lock`` — the runtime loop holds the same lock
        for every whole tick, so the command never runs inside one;
      * refuses a ghosted train (``presence_unknown``) before the command's own
        gates, unless ``refuse_ghost`` is False. The two EB commands,
        ``eb.activate()`` and ``eb.activate_cut_out()``, pass False: emergency
        braking stays available on every train at all times (design_new.md
        "A ghosted train refuses commands, except EB"), so they keep their own
        gates only;
      * runs the command, reports its command-result event once through
        ``controller.commands.report_result``, and returns its ``CommandResult``.
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(state: AppState, train_id: str, *args, **kwargs):
            train = require_train(state, train_id)
            if getattr(train, "is_unidentified", False):
                return CommandResult(
                    success=False, message=_unidentified_refusal_message(train_id)
                )
            with _runtime_lock(state):
                controller = _controller(state, train)
                if refuse_ghost and getattr(controller, "presence_unknown", False):
                    result = CommandResult(
                        success=False, message=_ghost_refusal_message(train_id)
                    )
                else:
                    result = func(state, train_id, *args, **kwargs)
                controller.commands.report_result(operation, result)
            return result

        return wrapper

    return decorate


def require_drivable_train(state: AppState, train_id: str) -> Train:
    """The train, rejecting an unidentified train (design_new.md "Unidentified trains").

    For the command paths that raise rather than return a failure shape.
    """
    train = require_train(state, train_id)
    if getattr(train, "is_unidentified", False):
        raise ValidationError(_unidentified_refusal_message(train_id))
    return train


def list_trains(state: AppState) -> List[Train]:
    """Return every train on the network."""
    return list(state.network.trains.values())


# ------------------------------------------------------------------ CRUD ----


def require_copy_source(state: AppState, profile_id: Any) -> Optional[int]:
    """The profile a train may be given, as a number, or None for the Default.

    design_new.md "SpeedProfile", Giving a train a profile: a request names the
    Default profile or a set, and refuses an instance profile or a number that
    names no profile.

    Raises:
        ValidationError naming why the profile was refused.
    """
    from speed_profile import as_profile_id

    if profile_id is None or profile_id == "":
        return None
    pid = as_profile_id(profile_id)
    if pid is None:
        raise ValidationError(f"speed_profile_id must be a number, not {profile_id!r}")
    try:
        state.speed_profile_registry.copy_source(pid)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return pid


def _apply_train_config(
    train: Train,
    min_radius_cm: Optional[float],
    max_slope: Optional[float],
) -> None:
    """Apply optional persistent train config in place: minimum curve radius
    and maximum gradient. Each value is None to leave the default untouched."""
    if min_radius_cm is not None:
        try:
            train.min_radius_cm = float(min_radius_cm)
        except (TypeError, ValueError):
            pass
    if max_slope is not None:
        try:
            train.max_slope = float(max_slope)
        except (TypeError, ValueError):
            pass


def create_train(
    state: AppState,
    *,
    train_id: str,
    length_cm: float = 100.0,
    max_speed_kmh: float = 100.0,
    car_number: str = "",
    position: Optional[dict] = None,
    direction: str = "EAST",
    speed_profile_id: Optional[Any] = None,
    min_radius_cm: Optional[float] = None,
    max_slope: Optional[float] = None,
) -> Train:
    """Create a train, placing it when a position is given.

    The train's instance profile is a copy of the Default profile or the set
    ``speed_profile_id`` names (design_new.md "SpeedProfile", Giving a train a
    profile).

    When ``position`` is a dict and a SimulationController is available the
    train is wired through ``sim_controller.add_train`` (controller +
    physics + notifications in one step) and then re-placed on the exact
    requested tile. Otherwise the train is built without placement (editor
    scenario).

    Raises:
        ValidationError when train_id is blank, the position lacks x/y, or
            no block exists at the requested tile.
    """
    if not train_id:
        raise ValidationError("Missing train ID")
    with _runtime_lock(state):
        speed_profile_id = require_copy_source(state, speed_profile_id)

    if position is not None and state.sim_controller is not None:
        try:
            pos_x = int(position["x"])
            pos_y = int(position["y"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError("position must include x and y")

        track = state.network.get_track(pos_x, pos_y)
        if track is None or not track.block_id:
            raise ValidationError(f"No block at position ({pos_x}, {pos_y})")

        try:
            train = state.sim_controller.add_train(
                train_id=train_id,
                starting_block_id=track.block_id,
                initial_direction=direction,
                max_speed_kmh=max_speed_kmh,
                length_cm=length_cm,
                name=car_number,
                speed_profile_id=speed_profile_id,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        # add_train places on some tile of the block; re-place on the exact
        # requested (x, y) for backwards compatibility. Best-effort: a
        # tile-precise failure leaves the train on a valid tile of the block.
        try:
            state.sim_controller.reposition_train(
                train, pos_x, pos_y, Direction[direction.upper()]
            )
        except (KeyError, ValueError) as exc:
            import logging

            logging.getLogger(__name__).debug(
                "Tile-precise placement failed for %s: %s", train_id, exc
            )

        _apply_train_config(train, min_radius_cm, max_slope)
        state.network.update_signals_from_blocks()
        return train

    # No-position / editor path: direct construction, no placement.
    car = TrainCar(
        car_id=f"{train_id}_loco",
        car_type="LOCOMOTIVE",
        length_cm=length_cm,
        max_speed=max_speed_kmh,
    )
    train = Train(
        train_id=train_id,
        consist=[car],
        max_speed=max_speed_kmh,
        car_number=car_number,
    )
    _apply_train_config(train, min_radius_cm, max_slope)
    train.speed_profile_id = state.speed_profile_registry.create_instance(
        source_profile_id=speed_profile_id, max_speed_kmh=max_speed_kmh
    ).profile_id
    state.network.add_train(train)
    state.network.update_signals_from_blocks()
    return train


def update_train(
    state: AppState,
    train_id: str,
    *,
    max_speed: Optional[Any] = None,
    car_number: Optional[Any] = None,
    length_cm: Optional[Any] = None,
) -> Train:
    """Apply a partial update to a train's configuration.

    Each kwarg is None to leave that field untouched. Invalid numeric
    values for the fields that were supplied are collected and reported
    together.

    Raises:
        NotFoundError when no train has that id.
        ValidationError listing every field whose value did not parse.
    """
    train = require_train(state, train_id)
    # A locomotive has one name, its `car_number` (design_new.md "Train
    # Entity", A locomotive has one name).
    new_name = None if car_number is None else str(car_number).strip()

    errors: List[str] = []

    if max_speed is not None:
        try:
            train.max_speed = float(max_speed)
        except (ValueError, TypeError):
            errors.append("max_speed")

    if new_name is not None:
        train.car_number = new_name

    if length_cm is not None:
        try:
            new_length = float(length_cm)
            if new_length > 0:
                if train.consist:
                    train.consist[0].length_cm = new_length
                else:
                    train.consist.append(
                        TrainCar(
                            car_id=f"{train_id}_loco",
                            car_type="LOCOMOTIVE",
                            length_cm=new_length,
                            max_speed=train.max_speed,
                        )
                    )
        except (ValueError, TypeError):
            errors.append("length_cm")

    if errors:
        raise ValidationError(
            f"Invalid values for fields: {', '.join(errors)}"
        )

    return train


def _parse_tile(value: Optional[dict], field: str) -> Optional[Tuple[int, int]]:
    """Parse an optional ``{"x": int, "y": int}`` tile from a request body."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object with x and y")
    try:
        return (int(value["x"]), int(value["y"]))
    except (KeyError, TypeError, ValueError):
        raise ValidationError(f"{field} must include x and y")


def adopt_unidentified_train(
    state: AppState,
    train_id: str,
    *,
    new_train_id: Optional[str] = None,
    length_cm: float = 100.0,
    max_speed_kmh: float = 100.0,
    direction: str = "EAST",
    head_tile: Optional[dict] = None,
    front_end_tile: Optional[dict] = None,
    cab: Optional[str] = None,
    car_number: str = "",
    train_type: Optional[str] = None,
    min_radius_cm: Optional[float] = None,
    max_slope: Optional[float] = None,
    speed_profile_id: Optional[Any] = None,
) -> Train:
    """Set an unidentified train up as a real, driveable train.

    The operator tells the app what the detector found — which locomotive, how
    long, which way round — and from then on it is an ordinary train
    (design_new.md "Unidentified trains", Adoption). `SimulationController` owns the
    conversion; this layer parses and validates the request shape.

    Raises:
        NotFoundError when no train has that id.
        ValidationError when there is no runtime, the train is not an unidentified train, the
            session reads occupancy from the model, or any field is unusable.
    """
    with state.simulation_lock:
        require_train(state, train_id)
        if state.sim_controller is None:
            raise ValidationError("No simulation runtime is running")

        if cab is not None:
            cab = str(cab).strip().upper()
            if cab not in ("A", "B"):
                raise ValidationError(f"Invalid cab: {cab!r} (expected 'A' or 'B')")

        tile = _parse_tile(head_tile, "head_tile")
        front_end = _parse_tile(front_end_tile, "front_end_tile")
        speed_profile_id = require_copy_source(state, speed_profile_id)

        try:
            length_cm = float(length_cm)
            max_speed_kmh = float(max_speed_kmh)
        except (TypeError, ValueError):
            raise ValidationError("length_cm and max_speed_kmh must be numbers")
        if length_cm <= 0:
            raise ValidationError("length_cm must be greater than 0")
        if max_speed_kmh <= 0:
            raise ValidationError("max_speed_kmh must be greater than 0")

        try:
            train = state.sim_controller.adopt_unidentified_train(
                train_id,
                (new_train_id or train_id),
                max_speed_kmh=max_speed_kmh,
                length_cm=length_cm,
                direction=direction,
                head_tile=tile,
                front_end_tile=front_end,
                cab=cab,
                train_type=train_type,
                name=car_number or None,
                min_radius_cm=min_radius_cm,
                max_slope=max_slope,
                speed_profile_id=speed_profile_id,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        state.network.update_signals_from_blocks()
        return train


def clear_failed_runtime(state: AppState) -> dict:
    """Release a fatal-conflict halt and leave every train off.

    Raises:
        ValidationError when there is no runtime, or it is not halted.
    """
    with state.simulation_lock:
        if state.sim_controller is None:
            raise ValidationError("No simulation runtime is running")
        try:
            payload = state.sim_controller.clear_failed_runtime()
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    return {
        "resource_type": payload.resource_type if payload else None,
        "resource_id": payload.resource_id if payload else None,
        "incoming_train_id": payload.incoming_train_id if payload else None,
        "prior_owner_train_id": payload.prior_owner_train_id if payload else None,
    }


def delete_train(state: AppState, train_id: str) -> bool:
    """Delete a train between ticks (design_new.md "Train Entity", Deleting a
    train). Returns True on success.

    `SimulationController.remove_train(...)` owns the refusals and the
    teardown. A pure map-editor session has no runtime, so no controller,
    physics instance, or route can name the train, and removing it from the
    network is the whole deletion.

    Raises:
        NotFoundError when no train has that id.
        ValidationError when the train is unidentified, is not OFF, or still
            holds a path or a running route run.

    Blocks the train held that still read current become an unidentified
    train, which the operator adopts to place the train again.
    """
    with _runtime_lock(state):
        train = require_train(state, train_id)
        if getattr(train, "is_unidentified", False):
            raise ValidationError(_unidentified_refusal_message(train_id))
        if state.sim_controller is None:
            state.network.remove_train(train_id)
            return True
        try:
            state.sim_controller.remove_train(train_id)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    return True


def reposition_train(
    state: AppState, train_id: str, *, position: Any, direction: Any
) -> Train:
    """Move a train to an exact tile and facing.

    Raises:
        NotFoundError when no train has that id.
        ValidationError on a missing/blank position or direction, an
            unknown direction name, or no track at the tile.
    """
    with state.simulation_lock:
        train = state.network.trains.get(train_id)
        if not train:
            raise NotFoundError(f"Train '{train_id}' not found")
        if getattr(train, "is_unidentified", False):
            # An unidentified train's position comes from detection only; the operator cannot
            # place it (design_new.md "Unidentified trains").
            raise ValidationError(_unidentified_refusal_message(train_id))

        if not isinstance(position, dict):
            raise ValidationError("position is required")
        if "x" not in position or "y" not in position:
            raise ValidationError("position must include x and y")
        if not direction:
            raise ValidationError("direction is required")
        try:
            resolved_direction = Direction[direction]
        except KeyError:
            raise ValidationError(f"Invalid direction: {direction}")

        x, y = int(position["x"]), int(position["y"])
        if state.network.tracks.get((x, y)) is None:
            raise ValidationError(f"No track at position ({x}, {y})")

        try:
            state.sim_controller.reposition_train(train, x, y, resolved_direction)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    return train


def _refuse_profile_change_during_calibration(state: AppState, train_id: str) -> None:
    """Refuse a copy into the train's instance profile while a calibration run
    is open for it or its finished pass waits (design_new.md "SpeedProfile",
    Giving a train a profile; Resetting an instance profile): the copy would
    replace the curve and voltages under the run."""
    sim = getattr(state, "sim_controller", None)
    calibration = getattr(sim, "speed_calibration", None) if sim else None
    if calibration is not None and train_id in getattr(calibration, "runs", {}):
        raise ConflictError(
            f"Train {train_id!r} has a calibration run open or a finished pass "
            "waiting; commit or discard it first"
        )


def assign_speed_profile(
    state: AppState, train_id: str, *, profile_id: Optional[Any]
) -> Train:
    """Give a train the Default profile or a set: copy it into the instance
    profile the train already owns, keeping its number (design_new.md
    "SpeedProfile", Giving a train a profile).

    Raises:
        ValidationError when profile_id is missing, not a number, names an
            instance profile, or names no profile.
        NotFoundError when the train does not exist.
        ConflictError while a calibration run is open for the train or its
            finished pass waits.
    """
    if profile_id is None or profile_id == "":
        raise ValidationError("profile_id is required")

    with state.simulation_lock:
        train = state.network.trains.get(train_id)
        if not train:
            raise NotFoundError(f"Train '{train_id}' not found")
        source_id = require_copy_source(state, profile_id)
        _refuse_profile_change_during_calibration(state, train_id)
        registry = state.speed_profile_registry
        if registry.get(train.speed_profile_id) is None:
            train.speed_profile_id = registry.create_instance(
                source_profile_id=source_id, max_speed_kmh=train.max_speed
            ).profile_id
        else:
            registry.copy_into_instance(train.speed_profile_id, source_id, train.max_speed)
    return train


def reset_speed_profile(state: AppState, train_id: str) -> Train:
    """Copy the train's parent profile back into its instance profile
    (design_new.md "SpeedProfile", Resetting an instance profile).

    Raises:
        NotFoundError when the train does not exist.
        ConflictError when the instance has no parent, its parent names no
            profile, or a calibration run is open or a finished pass waits.
    """
    with state.simulation_lock:
        train = state.network.trains.get(train_id)
        if not train:
            raise NotFoundError(f"Train '{train_id}' not found")
        registry = state.speed_profile_registry
        instance = registry.get(train.speed_profile_id)
        if instance is None:
            raise ConflictError(f"Train {train_id!r} has no instance profile")
        parent_id = instance.parent_profile_id
        parent = registry.get(parent_id) if parent_id is not None else None
        if parent is None or parent.is_instance:
            raise ConflictError(
                "This train's profile has no parent to reset to: its set was "
                "deleted, or it came from a scene"
            )
        _refuse_profile_change_during_calibration(state, train_id)
        registry.copy_into_instance(instance.profile_id, parent_id, train.max_speed)
    return train


def assign_route(
    state: AppState,
    train_id: str,
    *,
    route_id: Optional[str],
    start_mode: Optional[str] = None,
    start_task_index: Optional[Any] = None,
) -> Train:
    """Assign, retarget, or clear a train's route.

    Per design_new.md "Operator retargeting / route swap" this writes the
    Action route-abort latch and updates ``Route`` assignment, but never
    mutates ``RouteExecutor.execution_states`` or cancels the path inline --
    those flow through Action cleanup, and Plan of the next eligible
    tick allocates a fresh RouteRun against the newly assigned route.

    ``route_id`` None clears the current assignment. A non-None route_id
    assigns/retargets. A train already on another route needs no extra flag:
    the request stops its current run at the end of its reservation and holds
    the new route until that run is torn down (design_new.md "Route
    assignment writers").

    ``start_mode`` / ``start_task_index`` are an optional per-assignment
    one-shot start-mode input (per design_new.md "Run start-mode seeding").
    When provided on a genuine assign/retarget, they are coerced into a
    ``start_override`` bundle and passed to
    ``RouteExecutor.request_route_assignment(..., start_override=...)``, which
    stashes it for the next fresh ``RouteRun`` allocation; ``None`` means "use
    the route default." The pending bundle is cleared when the assignment
    commits so a stale choice can never leak onto a different route's run.

    Raises:
        NotFoundError when the train or the target route does not exist.
        ValidationError when the target route has no tasks or start_mode is
            invalid.
        ConflictError when a calibration run is open for the train, or when
            ``RouteExecutor.request_route_assignment(...)`` refuses the request.
        ValidationError when the train is an unidentified train: an unidentified train is never assigned a
            route (design_new.md "Unidentified trains").
    """
    train = require_drivable_train(state, train_id)

    route_executor = (
        state.sim_controller.route_executor if state.sim_controller else None
    )

    # A calibrating train refuses assignment, unassign included (design_new.md
    # "Route assignment writers", A calibrating train refuses assignment).
    if route_executor is not None:
        refusal = route_executor.calibration_refusal(train_id)
        if refusal is not None:
            raise ConflictError(refusal)

    # Branch 1: unassign (route_id is None). The mechanism -- defer around a
    # live run vs. unassign immediately, plus clearing stale pending start /
    # re-assignment state -- is owned by `request_route_assignment(...)` (per
    # design_new.md "Route assignment writers").
    if route_id is None:
        if route_executor is not None:
            result = route_executor.request_route_assignment(train_id, None)
            if not result.success:
                raise ConflictError(result.message)
        return train

    # Branch 2: assign / retarget (route_id provided). Validation stays here;
    # `request_route_assignment(...)` performs the assignment mechanism.
    route_registry = route_executor.route_registry if route_executor else {}
    route = route_registry.get(route_id)
    if not route:
        raise NotFoundError("Route not found")

    # A route pending deletion accepts no new assignment (design_new.md "Route
    # deletion waits for live teardown").
    if route_executor is not None and route_id in route_executor.pending_route_deletions:
        raise ValidationError(
            f"Route {route_id!r} is being deleted and accepts no new assignment."
        )

    # Reject a 0-task route up front: Plan would otherwise allocate a
    # RouteRun, find no target for the empty walk, and immediately finish
    # the run (unassigning the train and, for ONE_OFF, deleting the route) --
    # a "success" that silently goes idle a few ticks later.
    if not route.tasks:
        raise ValidationError(
            f"Route {route_id!r} has no tasks. Add at least one task "
            f"before assigning a train."
        )

    current_route_id = (
        route_executor.assigned_route_id_for(train_id) if route_executor else None
    )

    # Same-route assign is an idempotent no-op.
    if current_route_id == route_id:
        return train

    # Coerce the per-assignment start-mode input (if any) into the one-shot
    # bundle carried alongside the request; `None` means "use the route
    # default" (per design_new.md "Run start-mode seeding").
    start_override: Optional[Dict[str, Any]] = None
    if start_mode is not None:
        try:
            resolved_start_mode = RouteStartMode(start_mode)
        except ValueError:
            raise ValidationError(f"Invalid start_mode {start_mode!r}")
        try:
            resolved_start_index = (
                None if start_task_index is None else int(start_task_index)
            )
        except (TypeError, ValueError):
            raise ValidationError("start_task_index must be an integer or null")
        start_override = {
            "start_mode": resolved_start_mode,
            "start_task_index": resolved_start_index,
        }

    if route_executor is not None:
        result = route_executor.request_route_assignment(
            train_id, route_id, start_override=start_override
        )
        if not result.success:
            raise ConflictError(result.message)
    return train


# ----------------------------------------------------- operator commands ----
#
# These validate their input and delegate to the TrainController, returning
# the controller's CommandResult (or, for depart, its (success, reason)
# tuple) unchanged. The caller maps a non-success result to HTTP 400.


@operator_command("power_on_cmd")
def power_on(state: AppState, train_id: str) -> CommandResult:
    """Power on a train (OFF -> READY)."""
    return _controller(state, require_train(state, train_id)).commands.power_on_cmd()


@operator_command("power_off_cmd")
def power_off(state: AppState, train_id: str) -> CommandResult:
    """Power off a train (any state -> OFF; must be stopped)."""
    return _controller(state, require_train(state, train_id)).commands.power_off_cmd()


@operator_command("ato_go")
def ato_go(state: AppState, train_id: str) -> CommandResult:
    """Start ATO driving so the train drives toward its destination.

    The canonical `/ato/go` operator command (design_new.md "ATO go"):
    accepted only when `train_status == ATO` and the train has a route assigned.
    """
    return _controller(state, require_train(state, train_id)).commands.ato_go()


@operator_command("start_route")
def start_route(state: AppState, train_id: str) -> CommandResult:
    """Start a stopped train on its assigned route under ATO in one command.

    The Start route button (design_new.md "Start route"): powers the train up
    when OFF, selects ATO, and engages it.
    """
    return _controller(state, require_train(state, train_id)).commands.start_route()


@operator_command("ato_stop")
def ato_stop(state: AppState, train_id: str) -> CommandResult:
    """Pause ATO driving (train holds; used by both /stop and /ato-stop).

    The canonical `/ato/stop` operator command (design_new.md "ATO stop"):
    accepted only when `train_status == ATO`; accepted while moving.
    """
    return _controller(state, require_train(state, train_id)).commands.ato_stop()


@operator_command("set_handle")
def set_handle(state: AppState, train_id: str, *, position: Any) -> CommandResult:
    """Set the throttle/brake handle position.

    Raises ValidationError on a missing or unrecognized handle position.
    """
    if not position:
        raise ValidationError("Missing position")
    try:
        resolved = HandlePosition(position)
    except ValueError:
        raise ValidationError(
            f"Invalid handle position: {position}. Valid: P1-P5, N, B1-B8, EB"
        )
    return _controller(state, require_train(state, train_id)).commands.set_handle(
        resolved
    )


@operator_command("eb.activate", refuse_ghost=False)
def emergency_brake(state: AppState, train_id: str) -> CommandResult:
    """Raise the operator emergency brake via ``eb.activate()``.

    design_new.md "EB activation": the canonical operator EB command. Its only
    gate is ``eb.active == False``; it is valid in any motion state and any
    operational state, ATO included, so it is the EB route for every train
    (``set_handle(EB)`` is accepted only on a manual train).
    """
    return _controller(state, require_train(state, train_id)).eb.activate()


@operator_command("activate_cut_out_cmd", refuse_ghost=False)
def activate_cut_out(state: AppState, train_id: str) -> CommandResult:
    """Raise an operator-initiated emergency cut-out via the canonical
    ``commands.activate_cut_out_cmd()`` command (design_new.md "Emergency
    cut-out").

    The operator-initiated cut-out is accepted only while the active operating
    environment is Virtual Hardware or Real Hardware; in Simulation it is not
    offered — only the emergency brake exists — because there is no physical
    locomotive to stop faster than the modelled brake curve. The hardware-only
    gate lives on ``commands.activate_cut_out_cmd()``.
    """
    controller = _controller(state, require_train(state, train_id))
    return controller.commands.activate_cut_out_cmd()


@operator_command("eb.release")
def release_emergency(state: AppState, train_id: str) -> CommandResult:
    """Release the emergency brake via the canonical ``eb.release()`` command.

    The handle never releases EB (see ``OperatorCommands.set_handle`` and
    design_new.md, "EB release"); EB is released only by ``eb.release()``, which is
    latch-gated (stopped, EB cancellation committed, authority cleared). This
    is the operator surface for the "Release Emergency" / Reset button.

    ``eb.release()`` clears ``eb.active``; `train_status` is derived, so once
    the latch clears a powered train reads its stored `control_mode` again —
    `MANUAL`, or `ATO` waiting for `/ato/go` — with no further state write
    (design_new.md "EB release").
    """
    controller = _controller(state, require_train(state, train_id))
    result = controller.eb.release()
    return result


@operator_command("set_mode")
def set_mode(state: AppState, train_id: str, *, mode: Any) -> CommandResult:
    """Set the control mode ('ato' or 'manual')."""
    if mode not in ("ato", "manual"):
        raise ValidationError(f"Invalid mode: {mode}. Must be 'ato' or 'manual'")
    return _controller(state, require_train(state, train_id)).commands.set_mode(mode)


@operator_command("set_manual_mode_type")
def set_manual_mode_type(
    state: AppState, train_id: str, *, mode_type: Any
) -> CommandResult:
    """Set the manual submode ('simulation' = handle, or 'power_percent')."""
    if mode_type not in ("simulation", "power_percent"):
        raise ValidationError(
            f"Invalid mode_type: {mode_type}. "
            f"Must be 'simulation' or 'power_percent'"
        )
    return _controller(
        state, require_train(state, train_id)
    ).commands.set_manual_mode_type(mode_type)


@operator_command("set_manual_power_percent")
def set_manual_power_percent(
    state: AppState, train_id: str, *, percent: Any
) -> CommandResult:
    """Set the manual power-percent command (0..100)."""
    return _controller(
        state, require_train(state, train_id)
    ).commands.set_manual_power_percent(percent)


def _calibration_profile(state: AppState, train_id: str):
    """Resolve (train, profile, sim) for a calibration command.

    An unidentified train is rejected outright — calibration drives the train,
    which the app cannot do for an unidentified train (design_new.md
    "Unidentified trains"). Whether a run may open is
    `calibration_readiness(...)`'s question, not this lookup's.
    """
    train = require_drivable_train(state, train_id)
    sim = getattr(state, "sim_controller", None)
    if sim is None:
        raise ValidationError("Calibration requires a runtime")
    profile = state.network.speed_profiles.get(train.speed_profile_id)
    if profile is None or not profile.is_instance:
        raise ValidationError("Train has no instance profile to calibrate")
    return train, profile, sim


def block_name(state: AppState, block_id: str) -> str:
    """The block's name as the operator sees it, or its id when it has none."""
    block = state.network.blocks.get(block_id) if state.network else None
    return (getattr(block, "name", "") or "").strip() or block_id


def resolve_calibration_stretch(state: AppState, measured_block_id: str):
    """The measured block's two neighbours, and its length.

    The operator names one block because the other two are not a choice: they
    are whatever the points are set to. Returns ``(outer_block_ids, length_cm)``.
    """
    block = state.network.blocks.get(measured_block_id) if state.network else None
    if block is None:
        raise NotFoundError(f"Block '{measured_block_id}' not found")
    length_cm = float(getattr(block, "length_cm", 0.0) or 0.0)
    if length_cm <= 0.0:
        raise ValidationError(
            f"Block '{block_name(state, measured_block_id)}' has no recorded "
            "length — that length is the whole of what the measurement rests on"
        )
    manager = getattr(state.network, "block_manager", None)
    if manager is None:
        raise ValidationError("Resolving a calibration stretch requires a runtime")
    outer = manager.adjacent_blocks(measured_block_id)
    if len(outer) < 2:
        raise ValidationError(
            f"Block '{block_name(state, measured_block_id)}' does not have a "
            "block at both ends as the points are currently set — the outer "
            "blocks are the run-up and the stopping room"
        )
    return (outer[0], outer[1]), length_cm


def resolve_calibration_runoff(
    state: AppState,
    measured_block_id: str,
    outer: Tuple[str, str],
    train_length_cm: float,
    train_id: Optional[str] = None,
) -> Tuple[Tuple[Tuple[str, ...], Tuple[str, ...]], Dict[str, int]]:
    """Each end's run-off blocks, in order out from its outer block, as each
    end is when a leg runs toward it (design_new.md "Speed calibration", Each
    end of the stretch is an outer block and the run-off blocks past it).

    The ends hold at least `train_length_cm` plus
    `CALIBRATION_RUNOFF_MARGIN_CM`, then one block more
    (`BlockManager.runoff_blocks`), and never a block a train other than
    `train_id` stands in. Legs run one way at a time, so the two ends may share
    a run-off block. Returns the run-off blocks of each end and the switch
    positions they need that differ from the set ones. The pass finds them
    again each time a leg or the move arms.
    """
    from constants import CALIBRATION_RUNOFF_MARGIN_CM

    manager = getattr(state.network, "block_manager", None)
    if manager is None:
        return ((), ()), {}
    room_cm = max(0.0, float(train_length_cm or 0.0)) + CALIBRATION_RUNOFF_MARGIN_CM
    return manager.runoff_blocks(measured_block_id, outer, room_cm, train_id)


def calibration_block_length_cm(state: AppState, block_id: str) -> float:
    """A block's recorded length, `0` when it has none."""
    block = state.network.blocks.get(block_id) if state.network else None
    return float(getattr(block, "length_cm", 0.0) or 0.0)


def _calibration_ends(
    state: AppState, train, measured_block_id: str, outer: Tuple[str, str]
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Both ends of the stretch for this train: each outer block followed by
    its run-off blocks, sized from the train's entered length."""
    runoff, _positions = resolve_calibration_runoff(
        state,
        measured_block_id,
        outer,
        float(getattr(train, "length_cm", 0.0) or 0.0),
        train.train_id,
    )
    return (outer[0], *runoff[0]), (outer[1], *runoff[1])


def _end_lengths(state: AppState, ends: tuple) -> Tuple[Tuple[float, ...], ...]:
    """The `length_cm` of every block of each end, in the end's order."""
    return tuple(
        tuple(calibration_block_length_cm(state, block_id) for block_id in end)
        for end in ends
    )


def calibration_readiness(
    state: AppState,
    train_id: str,
    *,
    measured_block_id: str = "",
) -> List[str]:
    """Every opening condition the train does not yet meet, empty when a run
    may open (design_new.md "Speed calibration", Opening a run takes control of
    the train).

    With a `measured_block_id`, the train must also stand in one of the two
    ends of that block's stretch, or a walk must take it into one. The panel's
    readiness line shows the whole list, so the operator reads every unmet
    condition together rather than one at a time.
    """
    train = require_drivable_train(state, train_id)
    sim = getattr(state, "sim_controller", None)
    if sim is None:
        return ["Calibration requires a runtime"]
    controller = _controller(state, train)
    unmet: List[str] = []

    session = state.session_state()
    if not session.is_live:
        unmet.append(
            f"The session is {session.value.value}, not live — a calibration run "
            "opens only while the session is live"
        )
    if getattr(controller, "presence_unknown", False):
        unmet.append(_ghost_refusal_message(train_id))
    if state.network.speed_profiles.get(train.speed_profile_id) is None:
        unmet.append("The train has no instance profile")

    span = controller.get_current_span()
    held = set(getattr(span, "occupied_blocks", []) or [])
    if not held:
        unmet.append("Place the train on the layout")
    elif measured_block_id:
        try:
            outer, _length_cm = resolve_calibration_stretch(state, measured_block_id)
        except (NotFoundError, ValidationError) as exc:
            unmet.append(str(exc))
        else:
            executor = getattr(sim, "route_executor", None)
            first, second = (block_name(state, b) for b in outer)
            measured_name = block_name(state, measured_block_id)
            ends = _calibration_ends(state, train, measured_block_id, outer)
            if _span_in_end(held, measured_block_id, ends):
                if _list_holds(state, measured_block_id, train_id):
                    # The span is off the measured block but its detector has
                    # yet to read clear, and a standing train never lets it go.
                    # A leg cannot reserve a block its own train holds.
                    unmet.append(
                        f"Move the train off {measured_name}, wholly into "
                        f"the {first} or {second} end — a crossing is timed "
                        "from the moment the train enters the measured block"
                    )
            elif (
                executor is None
                or executor.calibration_move_walk(
                    train_id,
                    measured_block_id,
                    outer,
                    (ends[0][1:], ends[1][1:]),
                )
                is None
            ):
                unmet.append(
                    f"No way leads from where the train stands into {first} or "
                    f"{second} without crossing {measured_name} — drive it into "
                    "one of them"
                )

    if not controller.is_stopped():
        unmet.append("Stop the train")
    if controller.path_projection.received_path_plan is not None:
        unmet.append("The train still holds a reservation — let its path end first")
    if controller.eb.active:
        unmet.append("Release the emergency brake")
    if controller.route_hooks.phase_latches.power_off:
        unmet.append("Wait for the pending power-off to finish")
    executor = getattr(sim, "route_executor", None)
    if executor is not None and executor.assigned_route_id_for(train_id) is not None:
        unmet.append(
            "Unassign the train's route — a calibration run owns the train's "
            "movement while it is open"
        )

    calibration = sim.speed_calibration
    run = calibration.runs.get(train_id)
    if run is not None:
        if run.is_measured:
            unmet.append(
                "A finished speed pass is waiting — save it or discard it before "
                "measuring again"
            )
        elif run.ending:
            unmet.append("The last run is stopping the train — wait for it to stand")
        elif run.is_test:
            unmet.append("An ATO minimum test is already open for this train")
        else:
            unmet.append("A speed-calibration pass is already open for this train")
    return unmet


def _require_calibration_ready(
    state: AppState, train_id: str, *, measured_block_id: str = ""
) -> None:
    """Raise `ConflictError` naming every unmet opening condition."""
    unmet = calibration_readiness(
        state, train_id, measured_block_id=measured_block_id
    )
    if unmet:
        raise ConflictError("; ".join(unmet))


def _take_control_for_calibration(controller) -> dict:
    """The two operator commands opening a run calls before the run opens
    (design_new.md "Speed calibration", Opening a run takes control of the
    train): `power_on_cmd()` when the train is OFF, then `set_mode("manual")`.
    Each is the write its own command makes, so opening adds no effect beyond
    the run itself, and each emits its own command-result event. The readiness
    check has already confirmed the train is stopped, so both are accepted.

    Returns the train's mode and power before the two commands, as the keyword
    arguments the run stores to give them back (design_new.md "What ends a
    calibration run", A run the operator ends gives the train back)."""
    before = {
        "control_mode_before_opening": controller.commands.control_mode,
        "powered_before_opening": bool(controller.powered),
    }
    if not controller.powered:
        controller.commands.report_result(
            "power_on_cmd", controller.commands.power_on_cmd()
        )
    controller.commands.report_result(
        "set_mode", controller.commands.set_mode("manual")
    )
    return before


def calibrate_train(
    state: AppState,
    train_id: str,
    *,
    measured_block_id: str = "",
):
    """Open a speed-calibration pass (design_new.md "Speed calibration").

    Runs between ticks under the lock, like an operator command. When every
    opening condition holds it takes control of the train — power-up when OFF,
    then manual mode — and opens the pass, which resolves the measured block's
    two neighbours and then drives itself from the runtime tick, turning the
    train around at each end. Returns the `CalibrationRun`.
    """
    if not measured_block_id:
        raise ValidationError("measured_block_id is required")
    with _runtime_lock(state):
        _require_calibration_ready(
            state, train_id, measured_block_id=measured_block_id
        )
        train, _profile, sim = _calibration_profile(state, train_id)
        resolved = state.speed_profile_registry.get(train.speed_profile_id)
        outer, length_cm = resolve_calibration_stretch(state, measured_block_id)
        ends = _calibration_ends(state, train, measured_block_id, outer)
        heading_to = _leg_heading(state, train_id, measured_block_id, ends)
        before = _take_control_for_calibration(_controller(state, train))
        return sim.speed_calibration.start(
            train,
            resolved,
            measured_block_id=measured_block_id,
            outer_block_ids=outer,
            runoff_block_ids=(ends[0][1:], ends[1][1:]),
            end_lengths_cm=_end_lengths(state, ends),
            block_length_cm=length_cm,
            heading_to=heading_to,
            now=float(sim.operating_clock()),
            **before,
        )


def _span_in_end(held: set, measured_block_id: str, ends: tuple) -> bool:
    """Whether the train's span stands in one of the two ends of the stretch
    and off the measured block (design_new.md "Speed calibration", The pass
    first moves the train into an end of the stretch)."""
    in_end = any(held.intersection(end) for end in ends)
    return in_end and measured_block_id not in held


def _list_holds(state: AppState, block_id: str, train_id: str) -> bool:
    """Whether the train's block list still holds the block — its detector has
    yet to read it clear."""
    block = state.network.blocks.get(block_id)
    return getattr(block, "occupied_by", None) == train_id


def _leg_heading(
    state: AppState, train_id: str, measured_block_id: str, ends: tuple
) -> Optional[int]:
    """Which end the opening leg runs toward, or None when the pass must first
    move the train into one.

    The opening leg runs toward the end the train is not in. Asking for
    authority to the block the train is already standing in is refused by the
    dispatcher, so this orientation is what makes the opening leg possible at
    all (design_new.md "Speed calibration", The pass first moves the train into
    an end of the stretch).
    """
    controller = _controller(state, require_train(state, train_id))
    span = controller.get_current_span()
    held = set(getattr(span, "occupied_blocks", []) or [])
    if not _span_in_end(held, measured_block_id, ends):
        return None
    return 1 if held.intersection(ends[0]) else 0


def calibrate_status(state: AppState, train_id: str):
    """The open pass's progress payload, or None when none is open."""
    sim = getattr(state, "sim_controller", None)
    if sim is None:
        return None
    return sim.speed_calibration.runs.get(train_id)


def last_speed_calibration_result(
    state: AppState, train_id: str
) -> Optional[Dict[str, Any]]:
    """How the train's most recent speed pass ended, read from the event stream,
    or None when no pass has reported.

    Every ending — a measurement fault through `fail(...)`, `abandon(...)`, or a
    commit — sends its reason out on the command-result stream (design_new.md
    "Speed calibration", A leg that overruns the stretch fails the pass; What
    ends a calibration run). Once the run is dropped the panel reads that event
    here, so a failed pass can say why it failed; nothing is kept beside the
    stream (the same read the halt banner makes, `latest_by_operation`).
    """
    sim = getattr(state, "sim_controller", None)
    nc = getattr(sim, "notification_center", None) if sim is not None else None
    if nc is None:
        return None
    for event in nc.events_for("train", train_id):
        if event.operation != "speed_calibration":
            continue
        payload = event.payload or {}
        return {
            "success": bool(payload.get("success")),
            "reason": payload.get("reason") or event.message,
            "notification_id": event.notification_id,
        }
    return None


def calibrate_cancel(state: AppState, train_id: str) -> None:
    """End the train's run as the operator: an open run first brings the train
    to a stand inside its leg and then lets it go; a finished pass is discarded
    at once (design_new.md "What ends a calibration run", An operator ending
    stops the train before it lets it go). The profile is untouched."""
    sim = getattr(state, "sim_controller", None)
    if sim is not None:
        with _runtime_lock(state):
            # A finished pass already gave the train back when it finished;
            # discarding it gives nothing back.
            sim.speed_calibration.stop(train_id, reason="cancelled by the operator")


def calibrate_commit(
    state: AppState,
    train_id: str,
    *,
    target: str = "train",
    promote_as: Optional[str] = None,
    model_id: Optional[Any] = None,
    model_name: Optional[str] = None,
    set_name: Optional[str] = None,
    **_ignored,
):
    """Write a finished pass (design_new.md "Speed calibration", What commit
    writes).

    ``target="train"`` (**Save to this train**) writes `voltage_speed_curve`
    and `calibrated_at` onto the train's instance profile. ``target="promote"``
    (**Promote**) does the same, then promotes the instance profile to a set
    (design_new.md "SpeedProfile", Promoting an instance profile):

    * ``promote_as="new_set"`` creates a set under ``model_id``, or under a new
      model named ``model_name``, with ``set_name``, and makes it the
      instance's parent;
    * ``promote_as="current_set"`` writes over the set the instance's
      ``parent_profile_id`` names, refused when that is the Default profile or
      names no set.

    The commit clears the instance's `ato_min_voltage` (A commit clears the
    train's ATO minimum voltage), and a promoted set takes the cleared value.
    Every refusal is checked before anything is written. Returns the instance
    profile as written.
    """
    from dataclasses import replace as _replace
    from services.speed_profile_service import settle_profile_writes
    from speed_profile import DEFAULT_PROFILE_ID, as_profile_id

    if target not in ("train", "promote"):
        raise ValidationError("target must be 'train' or 'promote'")
    _train, profile, sim = _calibration_profile(state, train_id)
    run = sim.speed_calibration.runs.get(train_id)
    if run is None or run.is_test:
        raise NotFoundError("No calibration pass is open for this train")
    if not run.is_measured:
        raise ConflictError(
            "Calibration pass has not measured every step — a pass that does "
            "not finish writes nothing"
        )

    with state.simulation_lock:
        registry = state.speed_profile_registry
        # Check the promote's target before writing anything.
        promote_model = None
        new_model_name = None
        overwrite_set = None
        if target == "promote":
            if promote_as == "new_set":
                name = str(set_name or "").strip()
                if not name:
                    raise ValidationError("A new set needs a set name")
                mid = as_profile_id(model_id)
                if mid is not None:
                    promote_model = registry.get_model(mid)
                    if promote_model is None:
                        raise ValidationError(f"No model {model_id!r}")
                else:
                    new_model_name = str(model_name or "").strip()
                    if not new_model_name:
                        raise ValidationError(
                            "A new set needs a model: pick one or name a new one"
                        )
            elif promote_as == "current_set":
                parent_id = profile.parent_profile_id
                parent = registry.get(parent_id) if parent_id is not None else None
                if parent_id == DEFAULT_PROFILE_ID:
                    raise ConflictError(
                        "This train got the Default profile, which a commit cannot "
                        "change; promote it as a new set"
                    )
                if parent is None or not parent.is_set:
                    raise ConflictError(
                        "The set this train got its profile from was deleted; "
                        "promote it as a new set"
                    )
                overwrite_set = parent
            else:
                raise ValidationError("promote_as must be 'new_set' or 'current_set'")

        curve = sim.speed_calibration.measured_curve(run)
        written = _replace(profile, voltage_speed_curve=list(curve))
        _final, problems = settle_profile_writes(registry, {written.profile_id: written})
        if problems:
            raise ConflictError("; ".join(problems))
        try:
            sim.speed_calibration.commit(train_id, written)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        # `commit` stamped `calibrated_at`; settle again so the stored record
        # carries the stamp and the cleared ATO minimum voltage.
        final, _problems = settle_profile_writes(registry, {written.profile_id: written})
        instance = final[written.profile_id]
        registry.register(instance)

        if target == "promote":
            if overwrite_set is not None:
                overwrite_set.copy_values_from(instance)
            else:
                if promote_model is None:
                    promote_model = registry.add_model(new_model_name)
                new_set = registry.create_set(
                    promote_model.model_id, str(set_name).strip(), instance
                )
                instance.parent_profile_id = new_set.profile_id
    return instance


def _open_calibration_run(state: AppState, train_id: str):
    sim = getattr(state, "sim_controller", None)
    if sim is None:
        raise ValidationError("Calibration requires a runtime")
    run = sim.speed_calibration.runs.get(train_id)
    if run is None or not run.is_measuring:
        raise NotFoundError("No calibration run is open for this train")
    return sim, run


def calibrate_trial_voltage(state: AppState, train_id: str, *, track_voltage: Any):
    """Set the voltage the operator drives a speed pass's trial or an ATO
    minimum test at (design_new.md "Speed calibration", The pass starts with
    two trial stages; ATO minimum test). Returns the run."""
    try:
        voltage = float(track_voltage)
    except (TypeError, ValueError) as exc:
        raise ValidationError("track_voltage must be a number") from exc
    with _runtime_lock(state):
        sim, _run = _open_calibration_run(state, train_id)
        try:
            return sim.speed_calibration.set_trial_voltage(train_id, voltage)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc


def calibrate_keep_lighting(
    state: AppState, train_id: str, *, lighting_voltage: Optional[float]
):
    """Keep, or No lamps (`lighting_voltage=None`), in the lighting trial
    (design_new.md "Speed calibration", The pass starts with two trial stages).

    Saves the lighting voltage onto the instance profile the train names at
    once. When the profile's resolved starting voltage is not above it, the
    same write saves a starting voltage `CALIBRATION_TRIAL_STEP_V` above it,
    which It moves later replaces. A refused save keeps the pass in the
    lighting trial and raises `ConflictError` with the reason.
    """
    from dataclasses import replace as _replace

    from constants import CALIBRATION_TRIAL_STEP_V
    from services.speed_profile_service import settle_profile_writes
    from speed_calibration import PASS_LIGHTING_TRIAL

    held = None if lighting_voltage is None else float(lighting_voltage)
    with _runtime_lock(state):
        sim, run = _open_calibration_run(state, train_id)
        calibration = sim.speed_calibration
        if run.is_test or run.pass_stage != PASS_LIGHTING_TRIAL or run.ending:
            raise ConflictError("The lighting trial is not running")
        if held is not None and held < 0.0:
            raise ConflictError("The lighting voltage must not be below 0 V")
        train, profile, _sim = _calibration_profile(state, train_id)
        registry = state.speed_profile_registry
        resolved = registry.get(train.speed_profile_id)
        fields: Dict[str, Any] = {"lighting_voltage": held}
        if held is not None and float(resolved.starting_voltage or 0.0) <= held:
            fields["starting_voltage"] = round(held + float(CALIBRATION_TRIAL_STEP_V), 6)
        updated = _replace(profile, **fields)
        final, problems = settle_profile_writes(registry, {profile.profile_id: updated})
        if problems:
            reason = "; ".join(problems)
            calibration.refuse_starting_trial(train_id, reason)
            raise ConflictError(reason)
        for record in final.values():
            registry.register(record)
        return calibration.keep_lighting_voltage(train_id, held)


def calibrate_it_moves(state: AppState, train_id: str):
    """It moves: save the held lighting voltage and the trial voltage as
    `lighting_voltage` and `starting_voltage` onto the instance profile the
    train names, in one write that follows the hand-edit rules, then start the
    sweep (design_new.md "Speed calibration", The pass starts with two trial
    stages). A refused write keeps the pass in the starting trial and raises
    `ConflictError` with the reason."""
    from dataclasses import replace as _replace
    from services.speed_profile_service import settle_profile_writes

    with _runtime_lock(state):
        sim, _run = _open_calibration_run(state, train_id)
        calibration = sim.speed_calibration
        try:
            lighting, starting = calibration.starting_trial_values(train_id)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        if lighting is not None and starting <= lighting:
            reason = (
                f"The starting voltage ({starting:g} V) must be above the lighting "
                f"voltage you kept ({lighting:g} V). Raise the voltage until the "
                "train just moves, then press It moves."
            )
            calibration.refuse_starting_trial(train_id, reason)
            raise ConflictError(reason)
        _train, profile, _sim = _calibration_profile(state, train_id)
        registry = state.speed_profile_registry
        updated = _replace(profile, lighting_voltage=lighting, starting_voltage=starting)
        final, problems = settle_profile_writes(registry, {profile.profile_id: updated})
        if problems:
            reason = "; ".join(problems)
            calibration.refuse_starting_trial(train_id, reason)
            raise ConflictError(reason)
        for record in final.values():
            registry.register(record)
        return calibration.begin_sweep(train_id, starting)


def calibrate_ato_minimum_test(
    state: AppState,
    train_id: str,
    *,
    measured_block_id: str = "",
    track_voltage: Any = None,
):
    """Open an ATO minimum test (design_new.md "Speed calibration", ATO minimum
    test) under every condition a speed pass opens under, at `track_voltage`,
    or at the train's ATO minimum voltage when none is given."""
    if not measured_block_id:
        raise ValidationError("measured_block_id is required")
    with _runtime_lock(state):
        _require_calibration_ready(
            state, train_id, measured_block_id=measured_block_id
        )
        train, _profile, sim = _calibration_profile(state, train_id)
        resolved = state.speed_profile_registry.get(train.speed_profile_id)
        voltage = (
            resolved.ato_minimum_voltage
            if track_voltage is None
            else float(track_voltage)
        )
        outer, length_cm = resolve_calibration_stretch(state, measured_block_id)
        ends = _calibration_ends(state, train, measured_block_id, outer)
        heading_to = _leg_heading(state, train_id, measured_block_id, ends)
        low, high = resolved.first_point_voltage, float(resolved.max_voltage)
        if voltage < low - 1e-6 or voltage > high + 1e-6:
            raise ConflictError(
                f"the test voltage must lie from {low:g} V (the curve's first "
                f"point above the starting voltage) up to {high:g} V"
            )
        before = _take_control_for_calibration(_controller(state, train))
        return sim.speed_calibration.start_ato_minimum_test(
            train,
            resolved,
            track_voltage=voltage,
            measured_block_id=measured_block_id,
            outer_block_ids=outer,
            runoff_block_ids=(ends[0][1:], ends[1][1:]),
            end_lengths_cm=_end_lengths(state, ends),
            block_length_cm=length_cm,
            heading_to=heading_to,
            **before,
        )


def calibrate_ato_minimum_commit(state: AppState, train_id: str):
    """Commit: write the ATO minimum test's current voltage as the train's
    `ato_min_voltage` on its instance profile, then end the test (design_new.md
    "Speed calibration", ATO minimum test, Commit writes `ato_min_voltage` and
    nothing else). Refuses when the train's resolved starting voltage or curve
    changed after the test opened. Returns the written record."""
    from dataclasses import replace as _replace
    from services.speed_profile_service import settle_profile_writes
    from speed_calibration import profile_key

    with _runtime_lock(state):
        sim, run = _open_calibration_run(state, train_id)
        if not run.is_test:
            raise NotFoundError("No ATO minimum test is open for this train")
        train, profile, _sim = _calibration_profile(state, train_id)
        registry = state.speed_profile_registry
        resolved = registry.get(train.speed_profile_id)
        if profile_key(resolved) != run.test_profile_key:
            raise ConflictError(
                "The locomotive's starting voltage or curve changed after the test "
                "opened — test again"
            )
        updated = _replace(profile, ato_min_voltage=float(run.trial_voltage))
        final, problems = settle_profile_writes(
            registry, {profile.profile_id: updated}, keep_ato_min=(profile.profile_id,)
        )
        if problems:
            raise ConflictError("; ".join(problems))
        sim.speed_calibration.finish_ato_minimum_test(train_id)
        for record in final.values():
            registry.register(record)
        return final[profile.profile_id]


