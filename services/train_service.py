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
an already-assigned route without ``force`` raises ``ConflictError``.
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


def operator_command(operation: str):
    """Run an operator train command between ticks (design_new.md
    "Operator-command processing slot").

    The wrapper:
      * refuses an unidentified train, which never accepts an operator command
        (design_new.md "Unidentified trains", Never driven);
      * holds ``state.simulation_lock`` — the runtime loop holds the same lock
        for every whole tick, so the command never runs inside one;
      * refuses a ghosted train (``presence_unknown``), which accepts no
        command until detection returns;
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
                if getattr(controller, "presence_unknown", False):
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


def _apply_train_config(
    state: AppState,
    train: Train,
    speed_profile_id: Optional[str],
    min_radius_cm: Optional[float],
    max_slope: Optional[float],
) -> None:
    """Apply optional persistent train config in place.

    Covers the fields the create form sends beyond identity/length/speed:
    speed profile, minimum curve radius, and maximum gradient. Each value is
    None to leave the model default untouched. An unknown ``speed_profile_id``
    is ignored — the train then falls back to its default profile.
    """
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
    if speed_profile_id and state.speed_profile_registry.get(speed_profile_id):
        train.speed_profile_id = speed_profile_id


def create_train(
    state: AppState,
    *,
    train_id: str,
    length_cm: float = 100.0,
    max_speed_kmh: float = 100.0,
    car_number: str = "",
    position: Optional[dict] = None,
    direction: str = "EAST",
    speed_profile_id: Optional[str] = None,
    min_radius_cm: Optional[float] = None,
    max_slope: Optional[float] = None,
    roster_entry_id: Optional[str] = None,
) -> Train:
    """Create a train, placing it when a position is given.

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
                roster_entry_id=roster_entry_id,
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

        _apply_train_config(state, train, speed_profile_id, min_radius_cm, max_slope)
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
    _apply_train_config(state, train, speed_profile_id, min_radius_cm, max_slope)
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

    errors: List[str] = []

    if max_speed is not None:
        try:
            train.max_speed = float(max_speed)
        except (ValueError, TypeError):
            errors.append("max_speed")

    if car_number is not None:
        train.car_number = str(car_number)

    if length_cm is not None:
        try:
            new_length = float(length_cm)
            if new_length > 0:
                if train.consist:
                    train.consist[0].length = new_length
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


def adopt_unidentified_train(
    state: AppState,
    train_id: str,
    *,
    new_train_id: Optional[str] = None,
    length_cm: float = 100.0,
    max_speed_kmh: float = 100.0,
    direction: str = "EAST",
    head_tile: Optional[dict] = None,
    cab: Optional[str] = None,
    car_number: str = "",
    train_type: Optional[str] = None,
    min_radius_cm: Optional[float] = None,
    max_slope: Optional[float] = None,
    speed_profile_id: Optional[str] = None,
    roster_entry_id: Optional[str] = None,
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

        tile: Optional[Tuple[int, int]] = None
        if head_tile is not None:
            if not isinstance(head_tile, dict):
                raise ValidationError("head_tile must be an object with x and y")
            try:
                tile = (int(head_tile["x"]), int(head_tile["y"]))
            except (KeyError, TypeError, ValueError):
                raise ValidationError("head_tile must include x and y")

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
                cab=cab,
                train_type=train_type,
                name=car_number or None,
                min_radius_cm=min_radius_cm,
                max_slope=max_slope,
                speed_profile_id=speed_profile_id,
                roster_entry_id=roster_entry_id,
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
    """Remove a train from the network. Returns True on success.

    Raises:
        NotFoundError when no train has that id.
    """
    if not state.network.remove_train(train_id):
        raise NotFoundError("Train not found")
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


def assign_speed_profile(
    state: AppState, train_id: str, *, profile_id: Optional[str]
) -> Train:
    """Assign a speed profile to a train.

    Raises:
        ValidationError when profile_id is blank.
        NotFoundError when the train or the profile does not exist.
    """
    if not profile_id:
        raise ValidationError("profile_id is required")

    with state.simulation_lock:
        train = state.network.trains.get(train_id)
        if not train:
            raise NotFoundError(f"Train '{train_id}' not found")

        if not state.speed_profile_registry.get(profile_id):
            raise NotFoundError(f"Speed profile '{profile_id}' not found")

        train.speed_profile_id = profile_id

    return train


def assign_route(
    state: AppState,
    train_id: str,
    *,
    route_id: Optional[str],
    force: bool = False,
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
    assigns/retargets.

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
        ConflictError when the train is already on a route and ``force`` is
            False, or when a calibration run is open for the train.
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
            route_executor.request_route_assignment(train_id, None)
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

    if current_route_id and not force:
        raise ConflictError(
            f"Train already assigned to route {current_route_id}. "
            f"Use force=true to reassign."
        )

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
        route_executor.request_route_assignment(
            train_id, route_id, start_override=start_override
        )
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


@operator_command("eb.activate")
def emergency_brake(state: AppState, train_id: str) -> CommandResult:
    """Raise the operator emergency brake via ``eb.activate()``.

    design_new.md "EB activation": the canonical operator EB command. Its only
    gate is ``eb.active == False``; it is valid in any motion state and any
    operational state, ATO included, so it is the EB route for every train
    (``set_handle(EB)`` is accepted only on a manual train).
    """
    return _controller(state, require_train(state, train_id)).eb.activate()


@operator_command("activate_cut_out_cmd")
def activate_cut_out(state: AppState, train_id: str) -> CommandResult:
    """Raise an operator-initiated emergency cut-out via the canonical
    ``commands.activate_cut_out_cmd()`` command (design_new.md "Emergency
    cut-out").

    The operator-initiated cut-out is accepted only while the selected operating
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
    profile = (
        state.network.speed_profiles.get(train.speed_profile_id)
        if train.speed_profile_id
        else None
    )
    if profile is None:
        raise ValidationError("Train has no SpeedProfile to calibrate")
    return train, profile, sim


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
            f"Block '{measured_block_id}' has no recorded length — "
            "that length is the whole of what the measurement rests on"
        )
    manager = getattr(state.network, "block_manager", None)
    if manager is None:
        raise ValidationError("Resolving a calibration stretch requires a runtime")
    outer = manager.adjacent_blocks(measured_block_id)
    if len(outer) < 2:
        raise ValidationError(
            f"Block '{measured_block_id}' does not have a block at both ends as "
            "the points are currently set — the outer blocks are the run-up and "
            "the stopping room"
        )
    return (outer[0], outer[1]), length_cm


def calibration_readiness(
    state: AppState,
    train_id: str,
    *,
    kind: str = "speed",
    measured_block_id: str = "",
) -> List[str]:
    """Every opening condition the train does not yet meet, empty when a run
    may open (design_new.md "Speed calibration", Opening a run takes control of
    the train).

    `kind` is ``"speed"`` for a speed pass or ``"lighting"`` for a
    lighting-voltage run. For a speed pass with a `measured_block_id`, the
    train must also stand in one of that block's two outer blocks. The panel's
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
    if not train.speed_profile_id or state.network.speed_profiles.get(
        train.speed_profile_id
    ) is None:
        unmet.append("Assign a speed profile to the train")

    span = controller.get_current_span()
    held = set(getattr(span, "occupied_blocks", []) or [])
    if not held:
        unmet.append("Place the train on the layout")
    elif kind == "speed" and measured_block_id:
        try:
            outer, _length_cm = resolve_calibration_stretch(state, measured_block_id)
        except (NotFoundError, ValidationError) as exc:
            unmet.append(str(exc))
        else:
            if not held.intersection(outer):
                unmet.append(
                    f"Stand the train in {outer[0]} or {outer[1]} — a leg starts "
                    "from a stand in an outer block"
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
    if calibration.lighting_runs.get(train_id) is not None:
        unmet.append("A lighting-voltage run is already open for this train")
    run = calibration.runs.get(train_id)
    if run is not None:
        if run.is_measured:
            unmet.append(
                "A finished speed pass is waiting — save it or discard it before "
                "measuring again"
            )
        else:
            unmet.append("A speed-calibration pass is already open for this train")
    return unmet


def _require_calibration_ready(
    state: AppState, train_id: str, *, kind: str, measured_block_id: str = ""
) -> None:
    """Raise `ConflictError` naming every unmet opening condition."""
    unmet = calibration_readiness(
        state, train_id, kind=kind, measured_block_id=measured_block_id
    )
    if unmet:
        raise ConflictError("; ".join(unmet))


def _take_control_for_calibration(controller) -> None:
    """The writes opening a run makes before the run opens (design_new.md
    "Speed calibration", Opening a run takes control of the train): the
    `power_on_cmd()` power-up when the train is OFF, then the
    `set_mode("manual")` write. Each is the write its own command makes, so
    opening adds no effect beyond the run itself. The readiness check has
    already confirmed the train is stopped, so both are accepted."""
    if not controller.powered:
        controller.commands.power_on_cmd()
    controller.commands.set_mode("manual")


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
            state, train_id, kind="speed", measured_block_id=measured_block_id
        )
        train, profile, sim = _calibration_profile(state, train_id)
        outer, length_cm = resolve_calibration_stretch(state, measured_block_id)
        heading_to = _leg_heading(state, train_id, outer)
        _take_control_for_calibration(_controller(state, train))
        return sim.speed_calibration.start(
            train,
            profile,
            measured_block_id=measured_block_id,
            outer_block_ids=outer,
            block_length_cm=length_cm,
            heading_to=heading_to,
            now=float(sim.operating_clock()),
        )


def _leg_heading(state: AppState, train_id: str, outer: tuple) -> int:
    """Which outer block the opening leg runs toward.

    A leg starts from a stand in an outer block, so the train must be standing
    in one of the two, and the first leg runs toward the other one. Asking for
    authority to the block the train is already standing in is refused by the
    dispatcher, so this orientation is what makes the opening leg possible at
    all (design_new.md "Speed calibration", Where it runs).
    """
    controller = _controller(state, require_train(state, train_id))
    span = controller.get_current_span()
    held = set(getattr(span, "occupied_blocks", []) or [])
    if outer[0] in held:
        return 1
    if outer[1] in held:
        return 0
    raise ConflictError(
        f"Train must be standing in {outer[0]} or {outer[1]} to measure across "
        "this block — a leg starts from a stand in an outer block"
    )


def calibrate_status(state: AppState, train_id: str):
    """The open pass's progress payload, or None when none is open."""
    sim = getattr(state, "sim_controller", None)
    if sim is None:
        return None
    return sim.speed_calibration.runs.get(train_id)


def calibrate_cancel(state: AppState, train_id: str) -> None:
    """Drop an open pass. Its samples go with it and the profile is untouched."""
    sim = getattr(state, "sim_controller", None)
    if sim is not None:
        sim.speed_calibration.abandon(train_id, reason="cancelled by the operator")


def calibrate_commit(
    state: AppState,
    train_id: str,
    *,
    target: str = "locomotive",
    **_ignored,
):
    """Write a measured pass onto a profile (design_new.md "What commit writes").

    ``target="locomotive"`` (**Save to this locomotive**) writes the two measured
    fields onto the instance profile the calibrated train names, so that one
    locomotive runs on the new curve and no other changes. ``target="model"``
    (**Promote to model**) writes them onto the model profile the instance
    derives from and clears them from the instance in the same commit, so every
    locomotive of that model whose instance leaves them unset runs on the new
    curve, the calibrated one included. Returns the record actually written.
    """
    _train, profile, sim = _calibration_profile(state, train_id)
    run = sim.speed_calibration.runs.get(train_id)
    if run is None:
        raise NotFoundError("No calibration pass is open for this train")
    if not run.is_measured:
        raise ConflictError(
            "Calibration pass has not measured every step — a pass that does "
            "not finish writes nothing"
        )

    with state.simulation_lock:
        if target == "locomotive":
            target_profile, clear_profile = profile, None
        elif target == "model":
            if profile.derived_from:
                model_profile = state.network.speed_profiles.get(profile.derived_from)
                if model_profile is None:
                    raise ConflictError(
                        "The model profile this locomotive derives from is missing"
                    )
                target_profile, clear_profile = model_profile, profile
            else:
                # The train already names a model profile directly, so promoting
                # writes that record and there is no instance to clear.
                target_profile, clear_profile = profile, None
        else:
            raise ValidationError("target must be 'locomotive' or 'model'")
        try:
            sim.speed_calibration.commit(
                train_id, target_profile, clear_profile=clear_profile
            )
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
    return target_profile


# ------------------------------------------- lighting-voltage calibration ----


def start_lighting_calibration(state: AppState, train_id: str):
    """Open a lighting-voltage run (design_new.md "Lighting-voltage calibration").

    Opens under the same rule as a speed pass (design_new.md "Speed
    calibration", Opening a run takes control of the train): between ticks
    under the lock, every opening condition checked, then power-up when OFF and
    manual mode before the run opens. From here the candidate voltage is on the
    rails: the operator steps it up until the locomotive creeps, steps back
    down one, and saves.
    """
    with _runtime_lock(state):
        _require_calibration_ready(state, train_id, kind="lighting")
        train, profile, sim = _calibration_profile(state, train_id)
        _take_control_for_calibration(_controller(state, train))
        # Seed the candidate and ceiling from the resolved profile (the model's
        # values fill in where the instance carries none), while the run's write
        # target stays the instance profile the train names.
        resolved = state.network.speed_profiles.resolve(profile.profile_id)
        return sim.speed_calibration.start_lighting(train, resolved or profile)


def step_lighting_calibration(state: AppState, train_id: str, direction: int):
    """Move the open run's candidate one step up (`direction >= 0`) or down."""
    sim = _require_open_lighting_run(state, train_id)
    return sim.speed_calibration.step_lighting(train_id, direction)


def save_lighting_calibration(state: AppState, train_id: str) -> float:
    """Write the candidate to the profile's `lighting_voltage` and close the run."""
    sim = _require_open_lighting_run(state, train_id)
    _train, profile, _sim = _calibration_profile(state, train_id)
    return sim.speed_calibration.commit_lighting(train_id, profile)


def cancel_lighting_calibration(state: AppState, train_id: str) -> None:
    """Close the run without writing; the candidate is discarded."""
    sim = getattr(state, "sim_controller", None)
    if sim is not None:
        sim.speed_calibration.cancel_lighting(train_id)


def _require_open_lighting_run(state: AppState, train_id: str):
    """Return the runtime holding an open lighting run for this train.

    Step and save act on a run the operator already opened, so they check that
    the run exists rather than re-running the opening gates: a state change
    mid-run is reported by the missing run, not by a second gate message.
    """
    require_train(state, train_id)
    sim = getattr(state, "sim_controller", None)
    if sim is None:
        raise ValidationError("Calibration requires a runtime")
    if sim.speed_calibration.lighting_runs.get(train_id) is None:
        raise ConflictError("No lighting-voltage run is open for this train")
    return sim
