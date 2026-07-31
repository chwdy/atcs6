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
from typing import Any, List, Optional, Tuple

from app_state import AppState
from enums import CommandResult, Direction, HandlePosition, TrainState
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


def _ghost_refusal_message(train_id: str) -> str:
    return (
        f"Train {train_id} is a ghost train: the app cannot drive it. It has no "
        f"cab, no route, and no operator controls."
    )


def refuses_ghost_trains(refusal=None):
    """Refuse every operator command aimed at a ghost train.

    design_new.md "Ghost trains", Never driven: a ghost is never assigned a
    route, never given a `Path`, never given a cab, and never accepts an operator
    command — driving a train needs a cab, a speed profile and a route, and a
    ghost was never set up with any of them. The decorated command returns its own
    failure shape, so the HTTP layer maps it to 400 like any other refused
    command; `refusal` builds that shape from the message (default: a failed
    `CommandResult`).
    """
    build = refusal or (lambda message: CommandResult(success=False, message=message))

    def decorate(func):
        @functools.wraps(func)
        def wrapper(state: AppState, train_id: str, *args, **kwargs):
            if getattr(require_train(state, train_id), "is_ghost", False):
                return build(_ghost_refusal_message(train_id))
            return func(state, train_id, *args, **kwargs)

        return wrapper

    return decorate


def require_drivable_train(state: AppState, train_id: str) -> Train:
    """The train, rejecting a ghost train (design_new.md "Ghost trains").

    For the command paths that raise rather than return a failure shape.
    """
    train = require_train(state, train_id)
    if getattr(train, "is_ghost", False):
        raise ValidationError(_ghost_refusal_message(train_id))
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


def adopt_ghost_train(
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
    """Set a ghost train up as a real, driveable train.

    The operator tells the app what the detector found — which locomotive, how
    long, which way round — and from then on it is an ordinary train
    (design_new.md "Ghost trains", Adoption). `SimulationController` owns the
    conversion; this layer parses and validates the request shape.

    Raises:
        NotFoundError when no train has that id.
        ValidationError when there is no runtime, the train is not a ghost, the
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
            train = state.sim_controller.adopt_ghost_train(
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
    The route is assigned but Phase 1 never plans it because the train is `OFF`;
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
    gate("runtime_halted", not sim.failed_runtime,
         "a fatal track conflict has every train halted" if sim.failed_runtime
         else "runtime is live",
         "Sort the conflict out on the layout, then Clear Conflict.")

    gate("playing", bool(state.simulation_running),
         "the clock is paused" if not state.simulation_running else "clock is running",
         "Press Play.")

    # 2. A ghost is not driveable, and an OFF train never plans a route:
    #    `RouteExecutor.tick_train_phase1(...)` returns immediately for `OFF`.
    gate("not_a_ghost", not getattr(train, "is_ghost", False),
         "this is a ghost train — the app has never been told what it is"
         if getattr(train, "is_ghost", False) else "a real train",
         "Set it up as a real train from its panel.")

    powered = controller.ops.state is not TrainState.OFF
    gate("powered", powered,
         "the train is powered off" if not powered else f"state {controller.ops.state.name}",
         "Press Power On. While a train is OFF its route is never planned at all.")

    cab = controller.commands.cab
    gate("cab_selected", cab is not None,
         "no cab is selected, so the train has no forward end"
         if cab is None else f"cab {cab}",
         "Pick Cab A or Cab B.")

    # 3. Authority. No path means no ATC limit, which means no voltage.
    path_id = sim.path_dispatcher.active_path_id_by_train(train_id)
    route_id = sim.route_executor.assigned_route_id_for(train_id)
    if path_id:
        gate("authority", True, f"holding path {path_id}")
    elif route_id:
        gate("authority", False,
             f"route {route_id} is assigned but no path has been acquired",
             "Usually the track ahead is not free — check for an occupied or "
             "reserved block on the way, including a ghost train.")
    else:
        gate("authority", False, "no route assigned and no path",
             "Assign a route, or create a path to a destination.")

    # 4. Told to go.
    mode = controller.commands.control_mode
    if mode == "ato":
        gate("told_to_go", bool(controller.ato.running),
             "ATO is not running" if not controller.ato.running else "ATO running",
             "Press Depart / GO.")
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
        if getattr(train, "is_ghost", False):
            # A ghost's position comes from detection only; the operator cannot
            # place it (design_new.md "Ghost trains").
            raise ValidationError(_ghost_refusal_message(train_id))

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
    Phase-3 route-abort latch and updates ``Route`` assignment, but never
    mutates ``RouteExecutor.execution_states`` or cancels the path inline --
    those flow through Phase-3 cleanup, and Phase 1 of the next eligible
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
        ValidationError when the train is a ghost: a ghost is never assigned a
            route (design_new.md "Ghost trains").
    """
    train = require_drivable_train(state, train_id)

    route_executor = (
        state.sim_controller.route_executor if state.sim_controller else None
    )
    # Clear any stale per-assignment start input before doing anything else, so
    # an unassign — or a reassign to a different route — never reuses a choice
    # made for an earlier assignment (per design_new.md "Run start-mode
    # seeding").
    if route_executor is not None:
        route_executor.clear_pending_start_override(train_id)
        # Also drop any held structural-edit re-assignment for this train: an
        # explicit assign/retarget/unassign supersedes a pending same-route
        # re-allocation, so it must not fire on a later deactivate of this train
        # (per design_new.md "Shared-route edit teardown").
        route_executor.discard_pending_reassign(train_id)
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

    # Stash the per-assignment start-mode input (if any) for the next fresh
    # RouteRun allocation. Only set on a genuine assign/retarget that reaches
    # this point; the same-route no-op above returned early. Per design_new.md
    # "Run start-mode seeding".
    if route_executor is not None and start_mode is not None:
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
        route_executor.set_pending_start_override(
            train_id, resolved_start_mode, resolved_start_index
        )

    route.assign_train(train_id)
    return train


# ----------------------------------------------------- operator commands ----
#
# These validate their input and delegate to the TrainController, returning
# the controller's CommandResult (or, for depart, its (success, reason)
# tuple) unchanged. The caller maps a non-success result to HTTP 400.


@refuses_ghost_trains()
def power_on(state: AppState, train_id: str) -> CommandResult:
    """Power on a train (OFF -> READY)."""
    return _controller(state, require_train(state, train_id)).commands.power_on_cmd()


@refuses_ghost_trains()
def power_off(state: AppState, train_id: str) -> CommandResult:
    """Power off a train (any state -> OFF; must be stopped)."""
    return _controller(state, require_train(state, train_id)).commands.power_off_cmd()


@refuses_ghost_trains()
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


@refuses_ghost_trains(lambda message: (False, message))
def depart(state: AppState, train_id: str) -> Tuple[bool, Optional[str]]:
    """Depart-acceptance (READY -> RUNNING_MOVING).

    Returns the controller's ``(success, reason)`` tuple unchanged.

    Raises NotFoundError when the train does not exist.
    """
    return _controller(state, require_train(state, train_id)).depart()


@refuses_ghost_trains()
def ato_go(state: AppState, train_id: str) -> CommandResult:
    """Start ATO movement toward the destination."""
    return _controller(state, require_train(state, train_id)).ato.go()


@refuses_ghost_trains()
def ato_stop(state: AppState, train_id: str) -> CommandResult:
    """Service stop via ATO (used by both /stop and /ato-stop)."""
    return _controller(state, require_train(state, train_id)).ato.stop()


@refuses_ghost_trains()
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


@refuses_ghost_trains()
def release_emergency(state: AppState, train_id: str) -> CommandResult:
    """Release the emergency brake via the canonical ``eb.release()`` command.

    The handle never releases EB (see ``OperatorCommands.set_handle`` and
    design_new.md:239); EB is released only by ``eb.release()``, which is
    latch-gated (stopped, EB cancellation committed, authority cleared). This
    is the operator surface for the "Release Emergency" / Reset button.
    """
    return _controller(state, require_train(state, train_id)).eb.release()


@refuses_ghost_trains()
def set_mode(state: AppState, train_id: str, *, mode: Any) -> CommandResult:
    """Set the control mode ('ato' or 'manual')."""
    if mode not in ("ato", "manual"):
        raise ValidationError(f"Invalid mode: {mode}. Must be 'ato' or 'manual'")
    return _controller(state, require_train(state, train_id)).commands.set_mode(mode)


@refuses_ghost_trains()
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


@refuses_ghost_trains()
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
    a controlled pass and must not be hijacked for calibration. A ghost train is
    rejected outright — calibration drives the train, which the app cannot do
    for a ghost (design_new.md "Ghost trains").
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


def calibrate_train(state: AppState, train_id: str, *, mode: str = "auto"):
    """Run a speed-calibration pass (design_new.md "Speed calibration").

    Auto mode measures the full commanded voltage sweep synchronously and
    commits the curves. Manual mode begins the run; the operator then issues
    ``calibrate_record`` as the head passes the marker each lap. Returns the
    `CalibrationRun`.
    """
    train, profile, sim = _calibration_profile(state, train_id)
    run = sim.speed_calibration.start(train, profile, mode=mode)
    if run.mode == "auto":
        sim.speed_calibration.run_pass(run, profile)
    return run


def calibrate_record(state: AppState, train_id: str):
    """Manual-mode lap marker: record one lap for the active calibration run."""
    train, profile, sim = _calibration_profile(state, train_id)
    return sim.speed_calibration.record(train_id, profile)
