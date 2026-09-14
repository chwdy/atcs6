"""Train lifecycle and operation service.

The HTTP layer in ``routes/trains.py`` parses JSON and shapes the
sibling-payload response (`train` / `controller` / `dispatcher_progress`);
this layer owns the domain mutations behind every train endpoint:

  * CRUD -- create (placement-aware or editor), update, delete.
  * Placement -- reposition, speed-profile assignment.
  * Route assignment / retarget -- the abort-latch + assign/unassign
    transaction per design_new.md "Operator retargeting / route swap".
  * Operator commands -- power, cab, handle, mode, ATO go/stop. These
    validate their input here and delegate to ``TrainController``; they
    return the controller's ``CommandResult`` so the caller can surface
    ``success`` / ``message`` (HTTP 400 on failure). ``depart`` returns
    the controller's ``(success, reason)`` tuple unchanged.

Lookup raises ``NotFoundError``; bad input raises ``ValidationError``;
an already-assigned route without ``force`` raises ``ConflictError``.
``reposition_train`` and ``assign_speed_profile`` take
``state.simulation_lock`` (the routes they replace did).
"""

from __future__ import annotations

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


def refuses_unidentified_trains(refusal=None):
    """Refuse every operator command aimed at an unidentified train.

    design_new.md "Unidentified trains", Never driven: an unidentified train is never assigned a
    route, never given a `Path`, never given a cab, and never accepts an operator
    command — driving a train needs a cab, a speed profile and a route, and a
    unidentified train was never set up with any of them. The decorated command returns its own
    failure shape, so the HTTP layer maps it to 400 like any other refused
    command; `refusal` builds that shape from the message (default: a failed
    `CommandResult`).
    """
    build = refusal or (lambda message: CommandResult(success=False, message=message))

    def decorate(func):
        @functools.wraps(func)
        def wrapper(state: AppState, train_id: str, *args, **kwargs):
            if getattr(require_train(state, train_id), "is_unidentified", False):
                return build(_unidentified_refusal_message(train_id))
            return func(state, train_id, *args, **kwargs)

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


def diagnose_stopped_train(state: AppState, train_id: str) -> dict:
    """Walk every gate between a standing train and a moving one.

    A train that will not move is the hardest thing in this app to diagnose,
    because each gate is individually reasonable and none of them says anything.
    The route is assigned but Plan never plans it because the train is `OFF`;
    the path exists but ATO was never told to go; everything is green but the
    block has no track binding so the rail is dead. From the layout all of those
    look identical: a train that sits there.

    The gates are walked in the order they actually bite, and the first failing
    one is the answer — fixing a later gate while an earlier one is shut changes
    nothing. Every gate carries what to do about it.

    Returns `{train_id, moving, blocking_gate, gates: [...]}` where each gate is
    `{name, ok, detail, fix}`.

    Raises:
        NotFoundError when no train has that id.
    """
    train = require_train(state, train_id)
    sim = state.sim_controller
    gates: List[dict] = []

    def gate(name: str, ok: bool, detail: str, fix: str = "") -> None:
        gates.append({"name": name, "ok": bool(ok), "detail": detail, "fix": fix})

    if sim is None:
        gate("runtime", False, "no simulation runtime is running",
             "Switch to Operation mode.")
        return {"train_id": train_id, "moving": False,
                "blocking_gate": "runtime", "gates": gates}

    controller = sim.get_controller(train_id)
    if controller is None:
        gate("controller", False, "this train has no controller",
             "Re-place the train.")
        return {"train_id": train_id, "moving": False,
                "blocking_gate": "controller", "gates": gates}

    speed = float(controller.physics_sync.current_speed_kmh)

    # 1. The runtime has to be running at all.
    # Both fatal latches sit on the same gate, but they are cleared different
    # ways, so the advice has to name the right one.
    if sim.hardware_failure:
        gate("runtime_halted", False,
             "a hardware failure has every train halted",
             "Repair the hardware, then activate the environment again.")
    else:
        gate("runtime_halted", not sim.failed_runtime,
             "a fatal track conflict has every train halted" if sim.failed_runtime
             else "runtime is live",
             "Sort the conflict out on the layout, then Clear Conflict.")

    gate("playing", bool(state.simulation_running),
         "the clock is paused" if not state.simulation_running else "clock is running",
         "Press Play.")

    # 2. An unidentified train is not driveable, and an OFF train never plans a route:
    #    `RouteExecutor.tick_train_plan(...)` returns immediately for `OFF`.
    gate("not_unidentified", not getattr(train, "is_unidentified", False),
         "this is an unidentified train — the app has never been told what it is"
         if getattr(train, "is_unidentified", False) else "a real train",
         "Set it up as a real train from its panel.")

    powered = controller.powered
    gate("powered", powered,
         "the train is powered off" if not powered else f"status {controller.train_status.name}",
         "Press Power On. While a train is OFF its route is never planned at all.")

    # 3. Authority. No path means no ATC limit, which means no voltage. There is
    #    no cab gate — the head end is derived from the live authority
    #    (design_new.md "Reservation-side head derivation").
    path_id = sim.path_dispatcher.active_path_id_by_train(train_id)
    route_id = sim.route_executor.assigned_route_id_for(train_id)
    if path_id:
        gate("authority", True, f"holding path {path_id}")
    elif route_id:
        gate("authority", False,
             f"route {route_id} is assigned but no path has been acquired",
             "Usually the track ahead is not free — check for an occupied or "
             "reserved block on the way, including an unidentified train.")
    else:
        gate("authority", False, "no route assigned and no path",
             "Assign a route, or create a path to a destination.")

    # 4. Told to go.
    mode = controller.commands.control_mode
    if mode == "ato":
        gate("told_to_go", bool(controller.ato.engaged),
             "ATO is not engaged" if not controller.ato.engaged else "ATO engaged",
             "Engage ATO (set mode to ATO).")
    else:
        at_neutral = controller.ops.handle_position is HandlePosition.N
        gate("told_to_go", not at_neutral,
             "the handle is at N" if at_neutral
             else f"handle {controller.ops.handle_position.name}",
             "Notch the handle up to a power position.")

    # 5. ATC. Zero limit is the usual last stop before "nothing happens".
    limit, reason, _code = controller.atc.speed_limit_with_code()
    gate("atc_allows_movement", limit > 0,
         f"ATC limit is {limit:.1f} km/h ({reason})" if limit <= 0
         else f"ATC limit {limit:.1f} km/h",
         "The train is not authorised to move yet; the authority gate above is "
         "usually the reason.")

    # 6. The rail itself. Everything above can be green on a dead block.
    unreachable = sorted(
        getattr(sim.power_controller, "_unreachable_blocks", set())
        & set(_blocks_of(controller))
    )
    gate("track_output_reachable", not unreachable,
         f"the output driver cannot drive {', '.join(unreachable)}"
         if unreachable else "track output is reachable",
         "Bind those blocks to a track chip in Hardware Setup — the software is "
         "commanding voltage that never reaches the rail.")

    blocking = next((g["name"] for g in gates if not g["ok"]), None)
    return {
        "train_id": train_id,
        "moving": speed > 0,
        "speed_kmh": round(speed, 2),
        "blocking_gate": blocking,
        "gates": gates,
    }


def _blocks_of(controller) -> List[str]:
    span = controller.physics_sync.current_span
    return list(getattr(span, "occupied_blocks", None) or [])


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


def clear_hardware_failure(state: AppState) -> dict:
    """Release a hardware-failure halt after the environment was reactivated.

    Raises:
        ValidationError when there is no runtime, no hardware halt, or the
        environment has not been reactivated since the failure latched.
    """
    with state.simulation_lock:
        if state.sim_controller is None:
            raise ValidationError("No simulation runtime is running")
        try:
            payload = state.sim_controller.clear_hardware_failure()
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    return {
        "component": payload.component if payload else None,
        "operation": payload.operation if payload else None,
        "resource_id": payload.resource_id if payload else None,
        "message": payload.message if payload else None,
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
    When provided on a genuine assign/retarget, they are stashed on
    ``RouteExecutor.set_pending_start_override(...)`` and consumed by the
    next fresh ``RouteRun`` allocation; ``None`` means "use the route default."
    The pending bundle is cleared at the top of every call so a stale choice
    can never leak onto a different route's run.

    Raises:
        NotFoundError when the train or the target route does not exist.
        ValidationError when the target route has no tasks or start_mode is
            invalid.
        ConflictError when the train is already on a route and ``force`` is
            False.
        ValidationError when the train is an unidentified train: an unidentified train is never assigned a
            route (design_new.md "Unidentified trains").
    """
    train = require_drivable_train(state, train_id)

    route_executor = (
        state.sim_controller.route_executor if state.sim_controller else None
    )

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

    # Reject a 0-task route up front: Plan would otherwise allocate a
    # RouteRun, find no target for the empty walk, and immediately finish
    # the run (unassigning the train and, for ONE_OFF, deactivating the
    # route) -- a "success" that silently goes idle a few ticks later.
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


@refuses_unidentified_trains()
def power_on(state: AppState, train_id: str) -> CommandResult:
    """Power on a train (OFF -> READY)."""
    return _controller(state, require_train(state, train_id)).commands.power_on_cmd()


@refuses_unidentified_trains()
def power_off(state: AppState, train_id: str) -> CommandResult:
    """Power off a train (any state -> OFF; must be stopped)."""
    return _controller(state, require_train(state, train_id)).commands.power_off_cmd()


@refuses_unidentified_trains()
def ato_go(state: AppState, train_id: str) -> CommandResult:
    """Start ATO driving so the train drives toward its destination.

    The canonical `/ato/go` operator command (design_new.md "ATO go"):
    accepted only when `train_status == ATO`.
    """
    return _controller(state, require_train(state, train_id)).commands.ato_go()


@refuses_unidentified_trains()
def ato_stop(state: AppState, train_id: str) -> CommandResult:
    """Pause ATO driving (train holds; used by both /stop and /ato-stop).

    The canonical `/ato/stop` operator command (design_new.md "ATO stop"):
    accepted only when `train_status == ATO`; accepted while moving.
    """
    return _controller(state, require_train(state, train_id)).commands.ato_stop()


@refuses_unidentified_trains()
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


@refuses_unidentified_trains()
def release_emergency(state: AppState, train_id: str) -> CommandResult:
    """Release the emergency brake via the canonical ``eb.release()`` command.

    The handle never releases EB (see ``OperatorCommands.set_handle`` and
    design_new.md, "EB release"); EB is released only by ``eb.release()``, which is
    latch-gated (stopped, EB cancellation committed, authority cleared). This
    is the operator surface for the "Release Emergency" / Reset button.

    ``eb.release()`` clears only ``eb.active``; `train_status` is derived, so
    once the latch clears a powered train reads `train_status == MANUAL` with no
    further state write needed (design_new.md "EB release").
    """
    controller = _controller(state, require_train(state, train_id))
    result = controller.eb.release()
    return result


@refuses_unidentified_trains()
def set_mode(state: AppState, train_id: str, *, mode: Any) -> CommandResult:
    """Set the control mode ('ato' or 'manual')."""
    if mode not in ("ato", "manual"):
        raise ValidationError(f"Invalid mode: {mode}. Must be 'ato' or 'manual'")
    return _controller(state, require_train(state, train_id)).commands.set_mode(mode)


@refuses_unidentified_trains()
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


@refuses_unidentified_trains()
def set_manual_power_percent(
    state: AppState, train_id: str, *, percent: Any
) -> CommandResult:
    """Set the manual power-percent command (0..100)."""
    return _controller(
        state, require_train(state, train_id)
    ).commands.set_manual_power_percent(percent)


def _calibration_profile(state: AppState, train_id: str):
    """Resolve (train, profile) for a calibration command, enforcing the gates.

    Gate (design_new.md "Speed calibration"): a train following an ATO route is
    a controlled pass and must not be hijacked for calibration. An unidentified train is
    rejected outright — calibration drives the train, which the app cannot do
    for an unidentified train (design_new.md "Unidentified trains").
    """
    train = require_drivable_train(state, train_id)
    sim = getattr(state, "sim_controller", None)
    if sim is None:
        raise ValidationError("Calibration requires a runtime")
    executor = getattr(sim, "route_executor", None)
    if executor is not None and executor.execution_state_for(train_id) is not None:
        raise ConflictError("Cannot calibrate a train that is running a route")
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


def _require_calibration_ready(state: AppState, train_id: str, train, sim) -> None:
    """The opening preconditions (design_new.md "Speed calibration", Where it runs).

    Every one of them is a condition the pass already carries: it drives with
    `set_manual_power_percent(...)` (a manual-only command) and turns the train
    around between legs by re-framing the span to its other head end while the
    train is stopped (design_new.md "Reversed-frame re-expression"); there is no
    operator cab.
    """
    controller = _controller(state, train)
    if controller.commands.control_mode != "manual":
        raise ConflictError(
            "Speed calibration drives the train manually — switch it to manual"
        )
    if controller.manual.manual_mode_type != "power_percent":
        raise ConflictError(
            "Speed calibration commands a power percent — switch the manual "
            "mode to power_percent"
        )
    if not controller.powered:
        raise ConflictError(
            f"Train must be powered to calibrate (currently {controller.train_status.value})"
        )
    if not controller.is_stopped():
        raise ConflictError("Train must be stopped to start a calibration pass")
    # There is no cab precondition: the pass reverses by re-framing the span to
    # its other head end between legs (design_new.md "Reversed-frame
    # re-expression"), not by an operator cab selection.
    # The train must have no assigned route to open a pass (design_new.md
    # "Speed calibration", Opening a pass): `assigned_route_id_for(train_id) is
    # None`, not merely no *running* route. A manual train holding an
    # assigned-but-not-yet-running route would otherwise open a pass and then
    # have that route contend with the pass's own leg route. Only the pass's
    # own `CalibrationRun.active_leg_route_id` may be assigned after opening, so
    # this is an open-only precondition (the during-pass record/commit commands
    # legitimately run with the leg route assigned).
    executor = getattr(sim, "route_executor", None)
    if executor is not None and executor.assigned_route_id_for(train_id) is not None:
        raise ConflictError(
            "Unassign the train's route first — a calibration pass owns the "
            "train's movement, and only its own leg route may be assigned"
        )
    if sim.speed_calibration.lighting_runs.get(train_id) is not None:
        raise ConflictError("A lighting-voltage run is already open for this train")
    if sim.speed_calibration.runs.get(train_id) is not None:
        raise ConflictError("A speed-calibration pass is already open for this train")


def calibrate_train(
    state: AppState,
    train_id: str,
    *,
    mode: str = "auto",
    measured_block_id: str = "",
    marker_spacing_cm: float = 0.0,
):
    """Open a speed-calibration pass (design_new.md "Speed calibration").

    Auto profiling needs the measured block; the pass resolves its two
    neighbours and then drives itself from the runtime tick, turning the train
    around at each end. Manual marking needs the operator's marker spacing, and
    the operator drives the shuttle and issues ``calibrate_record`` at each mark.
    Returns the `CalibrationRun`.
    """
    train, profile, sim = _calibration_profile(state, train_id)
    _require_calibration_ready(state, train_id, train, sim)

    outer: tuple = ()
    length_cm = 0.0
    heading_to = 1
    if mode != "manual":
        if not measured_block_id:
            raise ValidationError("measured_block_id is required for auto profiling")
        outer, length_cm = resolve_calibration_stretch(state, measured_block_id)
        heading_to = _leg_heading(state, train_id, outer)
    elif float(marker_spacing_cm or 0.0) <= 0.0:
        raise ValidationError("marker_spacing_cm is required for manual marking")

    return sim.speed_calibration.start(
        train,
        profile,
        mode=mode,
        measured_block_id=measured_block_id,
        outer_block_ids=outer,
        block_length_cm=length_cm,
        marker_spacing_cm=float(marker_spacing_cm or 0.0),
        heading_to=heading_to,
        safe_state_generation=int(getattr(sim, "safe_state_generation", 0) or 0),
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


def calibrate_record(state: AppState, train_id: str):
    """Manual marking: the operator marks one pass of a marker."""
    _train, _profile, sim = _calibration_profile(state, train_id)
    return sim.speed_calibration.record(train_id, now=float(sim.operating_clock()))


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


def _lighting_calibration_target(state: AppState, train_id: str):
    """Resolve (train, profile, sim) for a lighting-voltage command.

    Gates (design_new.md "Lighting-voltage calibration", What the run drives):
    the train must be placed, in manual mode, READY and stopped, following no
    ATO route, and with no speed-calibration pass open. The locomotive creeps
    forward from the span's current head framing (design_new.md "Authority-side
    head derivation"); there is no operator cab.
    """
    train, profile, sim = _calibration_profile(state, train_id)
    controller = _controller(state, train)
    if controller.commands.control_mode != "manual":
        raise ConflictError(
            "Lighting calibration is a manual-mode pass — switch the train to manual"
        )
    if not controller.powered:
        raise ConflictError(
            f"Train must be powered to calibrate lighting voltage "
            f"(status {controller.train_status.name})"
        )
    if not controller.is_stopped():
        raise ConflictError("Train must be stopped to calibrate lighting voltage")
    if sim.speed_calibration.runs.get(train_id) is not None:
        raise ConflictError("A speed-calibration pass is already open for this train")
    return train, profile, sim


def start_lighting_calibration(state: AppState, train_id: str):
    """Open a lighting-voltage run (design_new.md "Lighting-voltage calibration").

    From here the candidate voltage is on the rails: the operator steps it up
    until the locomotive creeps, steps back down one, and saves.
    """
    train, profile, sim = _lighting_calibration_target(state, train_id)
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
