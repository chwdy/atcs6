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

from typing import Any, List, Optional, Tuple

from app_state import AppState
from enums import CommandResult, Direction, HandlePosition
from services.exceptions import ConflictError, NotFoundError, ValidationError
from train import Train, TrainCar


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


def list_trains(state: AppState) -> List[Train]:
    """Return every train on the network."""
    return list(state.network.trains.values())


# ------------------------------------------------------------------ CRUD ----


def create_train(
    state: AppState,
    *,
    train_id: str,
    length_cm: float = 100.0,
    max_speed_kmh: float = 100.0,
    car_number: str = "",
    position: Optional[dict] = None,
    direction: str = "EAST",
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
        if track is None or not track.block_section_id:
            raise ValidationError(f"No block at position ({pos_x}, {pos_y})")

        try:
            train = state.sim_controller.add_train(
                train_id=train_id,
                starting_block_id=track.block_section_id,
                initial_direction=direction,
                max_speed_kmh=max_speed_kmh,
                length_cm=length_cm,
                name=car_number,
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
    state: AppState, train_id: str, *, route_id: Optional[str], force: bool = False
) -> Train:
    """Assign, retarget, or clear a train's route.

    Per design_new.md "Operator retargeting / route swap" this writes the
    Phase-3 route-abort latch and updates ``Route`` assignment, but never
    mutates ``RouteExecutor.execution_states`` or cancels the path inline --
    those flow through Phase-3 cleanup, and Phase 1 of the next eligible
    tick allocates a fresh RouteRun against the newly assigned route.

    ``route_id`` None clears the current assignment. A non-None route_id
    assigns/retargets.

    Raises:
        NotFoundError when the train or the target route does not exist.
        ValidationError when the target route has no tasks.
        ConflictError when the train is already on a route and ``force`` is
            False.
    """
    train = require_train(state, train_id)

    route_executor = (
        state.sim_controller.route_executor if state.sim_controller else None
    )
    route_registry = route_executor.route_registry if route_executor else {}
    current_route_id = (
        route_executor.assigned_route_id_for(train_id) if route_executor else None
    )

    controller = _controller(state, train) if state.sim_controller else None

    def _has_active_path() -> bool:
        return (
            controller is not None
            and controller.path_projection.received_path_plan is not None
        )

    # Branch 1: unassign (route_id is None).
    if route_id is None:
        if current_route_id:
            if controller is not None:
                controller.route_hooks.phase_latches.set_route_abort(
                    reason="unassign",
                    has_active_path=_has_active_path(),
                )
            current_route = route_registry.get(current_route_id)
            if current_route:
                current_route.unassign_train(train_id)
        return train

    # Branch 2: assign / retarget (route_id provided).
    route = route_registry.get(route_id)
    if not route:
        raise NotFoundError("Route not found")

    # Reject a 0-task route up front: Phase 1 would otherwise allocate a
    # RouteRun, find no target for the empty walk, and immediately finish
    # the run (unassigning the train and, for ONE_OFF, deactivating the
    # route) -- a "success" that silently goes idle a few ticks later.
    if not route.tasks:
        raise ValidationError(
            f"Route {route_id!r} has no tasks. Add at least one task "
            f"before assigning a train."
        )

    # Same-route assign is an idempotent no-op.
    if current_route_id == route_id:
        return train

    if current_route_id and not force:
        raise ConflictError(
            f"Train already assigned to route {current_route_id}. "
            f"Use force=true to reassign."
        )

    if current_route_id:
        # Retargeting: write the abort latch first so Phase 3 commits the
        # cancellation through the canonical workflow.
        if controller is not None:
            controller.route_hooks.phase_latches.set_route_abort(
                reason="retarget",
                has_active_path=_has_active_path(),
            )
        previous_route = route_registry.get(current_route_id)
        if previous_route:
            previous_route.unassign_train(train_id)

    route.assign_train(train_id)
    return train


# ----------------------------------------------------- operator commands ----
#
# These validate their input and delegate to the TrainController, returning
# the controller's CommandResult (or, for depart, its (success, reason)
# tuple) unchanged. The caller maps a non-success result to HTTP 400.


def power_on(state: AppState, train_id: str) -> CommandResult:
    """Power on a train (OFF -> READY)."""
    return _controller(state, require_train(state, train_id)).commands.power_on_cmd()


def power_off(state: AppState, train_id: str) -> CommandResult:
    """Power off a train (any state -> OFF; must be stopped)."""
    return _controller(state, require_train(state, train_id)).commands.power_off_cmd()


def set_cab(state: AppState, train_id: str, *, cab: Any) -> CommandResult:
    """Set the active cab ('A', 'B', or off/None).

    Raises ValidationError on an unrecognized cab value.
    """
    if cab not in ("A", "B", None, "off", "Off", "OFF"):
        raise ValidationError(
            f"Invalid cab value: {cab}. Must be 'A', 'B', or None"
        )
    if cab in ("off", "Off", "OFF"):
        cab = None
    return _controller(state, require_train(state, train_id)).commands.set_cab(cab)


def depart(state: AppState, train_id: str) -> Tuple[bool, Optional[str]]:
    """Depart-acceptance (READY -> RUNNING_MOVING).

    Returns the controller's ``(success, reason)`` tuple unchanged.

    Raises NotFoundError when the train does not exist.
    """
    return _controller(state, require_train(state, train_id)).depart()


def ato_go(state: AppState, train_id: str) -> CommandResult:
    """Start ATO movement toward the destination."""
    return _controller(state, require_train(state, train_id)).ato.go()


def ato_stop(state: AppState, train_id: str) -> CommandResult:
    """Service stop via ATO (used by both /stop and /ato-stop)."""
    return _controller(state, require_train(state, train_id)).ato.stop()


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


def set_mode(state: AppState, train_id: str, *, mode: Any) -> CommandResult:
    """Set the control mode ('ato' or 'manual')."""
    if mode not in ("ato", "manual"):
        raise ValidationError(f"Invalid mode: {mode}. Must be 'ato' or 'manual'")
    return _controller(state, require_train(state, train_id)).commands.set_mode(mode)


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


def set_manual_power_percent(
    state: AppState, train_id: str, *, percent: Any
) -> CommandResult:
    """Set the manual power-percent command (0..100)."""
    return _controller(
        state, require_train(state, train_id)
    ).commands.set_manual_power_percent(percent)
