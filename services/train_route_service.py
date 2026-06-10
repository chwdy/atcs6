"""Route definition lifecycle service.

The HTTP route handlers in `routes/train_routes.py` parse JSON, then call
into these functions. Non-HTTP callers (CLI tooling, journey tests) call
the same functions directly with structured kwargs.

The service owns:
  * `route_id` allocation (when not supplied).
  * Mode-string canonicalization (incl. legacy "one_way"/"loop" aliases).
  * `Route.last_edited_at` bookkeeping for the five definition-edit
    fields that design_new.md "Route edit timestamps" tracks.
  * Validation of `assigned_train_ids` (only existing trains are
    actually assigned -- unknown IDs are silently dropped, matching the
    pre-refactor behavior).
  * Lookup helpers that raise `NotFoundError` rather than returning None
    when the caller asked for a specific route.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from app_state import AppState
from runtime_payloads import AcquisitionMode
from services.exceptions import NotFoundError, ValidationError
from train_route import (
    Route,
    RouteMode,
    RouteStartMode,
    RouteTask,
)


_MODE_ALIASES = {
    "one_way": RouteMode.ONE_OFF,
    "loop": RouteMode.CIRCULAR,
}

EDIT_FIELDS = (
    "name",
    "tasks",
    "mode",
    "priority",
    "acquisition_mode",
)

# Per design_new.md "RouteRun overrides" — fields that may carry per-field
# push policies on a PUT /api/train-routes/<id> edit. The overrides themselves
# are written via PATCH /api/route-runs/<run_number>/overrides; this list
# names the route-level fields whose edits may distribute through the
# RouteExecutor.apply_route_field_push helper.
OVERRIDE_ALLOWLIST = (
    "priority",
    "acquisition_mode",
)


def _coerce_mode(mode_str: str) -> RouteMode:
    """Convert a wire-string into RouteMode, accepting legacy aliases."""
    try:
        return RouteMode(mode_str)
    except ValueError:
        if mode_str in _MODE_ALIASES:
            return _MODE_ALIASES[mode_str]
        raise ValidationError(f"Invalid mode {mode_str!r}")


def _coerce_start_mode(value: Any) -> RouteStartMode:
    """Convert a wire-string into RouteStartMode. Per design_new.md "Run
    start-mode seeding"."""
    if isinstance(value, RouteStartMode):
        return value
    try:
        return RouteStartMode(value)
    except ValueError:
        raise ValidationError(f"Invalid start_mode {value!r}")


def _coerce_start_task_index(value: Any) -> Optional[int]:
    """Convert a wire value into an Optional[int] start_task_index."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("start_task_index must be an integer or null")


def _coerce_route_task(item: Any) -> RouteTask:
    """Build a RouteTask from a JSON request item."""
    if not isinstance(item, dict):
        raise ValidationError("task entry must be an object")
    # Per design_new.md line 1280: task_kind is required at task creation;
    # missing, null, or unrecognized task_kind is a validation error — the
    # creation path never infers a default kind. (RouteTask.from_dict stays
    # lenient for deserialization of already-validated persisted routes.)
    raw_kind = item.get("task_kind")
    if raw_kind not in ("waypoint", "stop", "dwell"):
        raise ValidationError(
            "task_kind is required and must be one of 'waypoint', 'stop', "
            f"or 'dwell' (got {raw_kind!r})"
        )
    return RouteTask.from_dict(dict(item))


def _registry(state: AppState) -> dict:
    return state.sim_controller.route_executor.route_registry


def _require_route(state: AppState, route_id: str) -> Route:
    route = _registry(state).get(route_id)
    if route is None:
        raise NotFoundError(f"Route {route_id!r} not found")
    return route


def list_routes(state: AppState) -> List[Route]:
    """Return all registered routes in registry insertion order."""
    return list(_registry(state).values())


def get_route(state: AppState, route_id: str) -> Optional[Route]:
    """Return the route, or None when not found."""
    return _registry(state).get(route_id)


def require_route(state: AppState, route_id: str) -> Route:
    """Return the route or raise NotFoundError."""
    return _require_route(state, route_id)


def create_route(
    state: AppState,
    *,
    route_id: Optional[str] = None,
    name: str = "",
    mode: str = "one_off",
    tasks: Optional[Iterable[Any]] = None,
    assigned_train_ids: Optional[Iterable[str]] = None,
    priority: Optional[int] = None,
    acquisition_mode: Optional[str] = None,
    start_mode: Optional[str] = None,
    start_task_index: Optional[Any] = None,
    edit_fields_present: Optional[bool] = None,
) -> Route:
    """Create and register a new Route.

    Args:
        route_id: Caller-supplied id; auto-generated when blank/None.
        name: Display name.
        mode: One of "one_off"|"circular"|"round_trip" (or legacy alias).
        tasks: Iterable of dict-shaped tasks (uses RouteTask.from_dict).
        assigned_train_ids: Train ids to assign. Unknown ids are silently
            dropped (preserves pre-refactor HTTP behavior).
        priority: Optional integer priority override.
        acquisition_mode: Optional acquisition-mode string.
        edit_fields_present: If supplied, controls whether
            last_edited_at is bumped. When None, the helper leaves the
            timestamp alone -- the route handler computes this from
            whether any of the five edit fields appeared in the JSON.
    """
    resolved_id = route_id or f"ROUTE-{uuid.uuid4().hex[:6].upper()}"
    resolved_mode = _coerce_mode(mode)

    route = Route(route_id=resolved_id, name=name, mode=resolved_mode)

    if priority is not None:
        try:
            route.priority = int(priority)
        except (TypeError, ValueError):
            raise ValidationError("priority must be an integer")

    if acquisition_mode is not None:
        try:
            route.acquisition_mode = AcquisitionMode(acquisition_mode)
        except ValueError:
            raise ValidationError(
                f"Invalid acquisition_mode {acquisition_mode!r}"
            )

    if start_mode is not None:
        route.start_mode = _coerce_start_mode(start_mode)

    if start_task_index is not None:
        route.start_task_index = _coerce_start_task_index(start_task_index)

    for task_data in tasks or ():
        route.add_task(_coerce_route_task(task_data))

    for train_id in assigned_train_ids or ():
        if train_id in state.network.trains:
            route.assign_train(train_id)

    if edit_fields_present:
        route.last_edited_at = time.time()

    state.sim_controller.route_executor.register_route(route)
    return route


def update_route(
    state: AppState,
    route_id: str,
    *,
    name: Optional[str] = None,
    mode: Optional[str] = None,
    tasks: Optional[Iterable[Any]] = None,
    priority: Optional[Any] = None,
    acquisition_mode: Optional[str] = None,
    start_mode: Optional[str] = None,
    start_task_index: Optional[Any] = None,
    start_task_index_present: bool = False,
    assigned_train_ids: Optional[Iterable[str]] = None,
    push_policies: Optional[Dict[str, str]] = None,
) -> Route:
    """Apply a partial update to an existing route.

    Each kwarg is None to leave that field untouched. assigned_train_ids
    is the full replacement set; trains not in the list will be
    unassigned and trains in the list (and known to the network) will be
    assigned. Unknown trains are silently dropped.

    `push_policies` is an optional dict mapping allowlisted field names
    (`"priority"`, `"acquisition_mode"`)
    to `"force"` or `"soft"`. When omitted the default per design_new.md
    "RouteRun overrides" is `"soft"`. Force pushes clear the corresponding
    `RouteRun.overrides[field]` on every currently RUNNING run for this
    route. Soft pushes leave overrides intact.

    Raises:
        NotFoundError when no route exists with that id.
        ValidationError on bad input shapes.
    """
    route = _require_route(state, route_id)
    push_policies = push_policies or {}

    definition_edit_touched = False
    # Set when the tasks edit changes the walk structure (waypoint set,
    # ordering, or task-kind / direction composition) — triggers RouteRun
    # teardown per design_new.md lines 1467-1469. Timing-only task edits are
    # not structural and do not tear down running runs.
    tasks_structural_edit = False
    # Track which allowlisted fields changed so push policies can be
    # applied after all writes commit.
    changed_overridable: List[str] = []

    if name is not None:
        route.name = name
        definition_edit_touched = True

    if mode is not None:
        route.mode = _coerce_mode(mode)
        definition_edit_touched = True

    if tasks is not None:
        new_tasks: List[RouteTask] = []
        for task_data in tasks:
            if not isinstance(task_data, dict):
                raise ValidationError("task entry must be an object")
            if not task_data.get("task_location"):
                raise ValidationError(
                    "task_location required for each task"
                )
            new_tasks.append(_coerce_route_task(task_data))

        # Structural change = any difference in kind / location / direction /
        # ordering of the walk. Timing-only edits (task_time / target_time) are
        # excluded, so they do not force a teardown.
        old_structure = [
            (t.task_kind, repr(t.task_location), t.task_direction)
            for t in route.tasks
        ]
        new_structure = [
            (t.task_kind, repr(t.task_location), t.task_direction)
            for t in new_tasks
        ]
        tasks_structural_edit = old_structure != new_structure

        route.tasks.clear()
        for task in new_tasks:
            route.add_task(task)
        definition_edit_touched = True

    if priority is not None:
        try:
            route.priority = int(priority)
        except (TypeError, ValueError):
            raise ValidationError("priority must be an integer")
        definition_edit_touched = True
        changed_overridable.append("priority")

    if acquisition_mode is not None:
        try:
            route.acquisition_mode = AcquisitionMode(acquisition_mode)
        except ValueError:
            raise ValidationError(
                f"Invalid acquisition_mode {acquisition_mode!r}"
            )
        definition_edit_touched = True
        changed_overridable.append("acquisition_mode")

    # start_mode / start_task_index are definition edits but are NOT in the
    # last_edited_at bump-set (design_new.md "Route edit timestamps": the set is
    # exactly name/tasks/mode/priority/acquisition_mode). They are also NOT
    # structural (no RouteRun teardown) and NOT live overrides (no push) — they
    # are consulted only when a fresh run is allocated, so an edit here changes
    # the default for future runs and leaves already-running runs alone. Per
    # "Run start-mode seeding".
    if start_mode is not None:
        route.start_mode = _coerce_start_mode(start_mode)

    if start_task_index_present:
        route.start_task_index = _coerce_start_task_index(start_task_index)

    # Apply per-field push policies to currently RUNNING RouteRuns for
    # this route per design_new.md "RouteRun overrides". Only look up the
    # executor when there is overridable state to push — an editor-only
    # boot or a pre-init window may legitimately have no sim_controller
    # wired, in which case a non-overridable edit (rename, task swap)
    # must still succeed.
    if changed_overridable:
        sim_controller = getattr(state, "sim_controller", None)
        if sim_controller is not None:
            executor = sim_controller.route_executor
            for field_name in changed_overridable:
                push = push_policies.get(field_name, "soft")
                if push not in ("force", "soft"):
                    push = "soft"
                executor.apply_route_field_push(route_id, field_name, push)

    if assigned_train_ids is not None:
        requested_train_ids = set(assigned_train_ids)
        current_train_ids = set(route.assigned_train_ids)

        for train_id in current_train_ids - requested_train_ids:
            route.unassign_train(train_id)

        for train_id in requested_train_ids - current_train_ids:
            if train_id in state.network.trains:
                route.assign_train(train_id)

    # Per design_new.md "Shared-route edit teardown": a structural Route.tasks
    # edit tears down every assigned train's currently-RUNNING RouteRun (there is
    # no in-place reconciliation in v1). Set the per-train route_deactivate latch
    # (which fans out the inclusive path cancel when a live path exists). Phase 3
    # commits the teardown via RouteExecutor.remove_train_route_run(train_id),
    # which UNASSIGNS the train from the run's route_id -- so the re-assignment
    # cannot be applied up front (it would be undone by that unassign). Instead,
    # hold it on RouteExecutor._pending_reassign and let the Phase-3 deactivate
    # consumer re-apply it AFTER the clear; the next eligible Phase 1 then sees
    # the train assigned with no RUNNING run and allocates a fresh RouteRun
    # against the edited Route.tasks. Skipped in editor-only mode (no runtime
    # surface wired, so there is no RouteRun to tear down).
    if tasks_structural_edit:
        sim_controller = getattr(state, "sim_controller", None)
        if sim_controller is not None:
            route_executor = getattr(sim_controller, "route_executor", None)
            teardown_reason = f"route_tasks_edited:{route.route_id}"
            for train_id in list(route.assigned_train_ids):
                controller = sim_controller.get_controller(train_id)
                if controller is None:
                    continue
                controller.route_hooks.phase_latches.set_route_deactivate(
                    reason=teardown_reason,
                    has_active_path=(
                        controller.path_projection.received_path_plan is not None
                    ),
                )
                if route_executor is not None:
                    route_executor.record_pending_reassign(
                        train_id, route.route_id
                    )

    if definition_edit_touched:
        route.last_edited_at = time.time()

    return route


def delete_route(state: AppState, route_id: str) -> bool:
    """Unassign + unregister a route. Returns True on success.

    Raises:
        NotFoundError when the route does not exist.
    """
    registry = _registry(state)
    if route_id not in registry:
        raise NotFoundError(f"Route {route_id!r} not found")

    route = registry[route_id]
    previously_assigned = list(route.assigned_train_ids)
    for train_id in previously_assigned:
        route.unassign_train(train_id)

    executor = state.sim_controller.route_executor
    # Per design_new.md canonical teardown: each assigned train's RouteRun
    # must go through finish_dwell() + DEACTIVATED + archive rather than a
    # raw dict pop (fix #4).
    for train_id in previously_assigned:
        executor.remove_train_route_run(train_id)

    executor.unregister_route(route_id)
    return True


def activate_route(
    state: AppState, route_id: str, simulation_time: Optional[float] = None
) -> Route:
    """Activate the route at simulation_time (defaults to controller now).

    A route with no tasks would be terminal on the first eligible Phase 1
    tick: ``_find_next_task_target`` returns ``None`` for an empty walk,
    which makes Phase 1 call ``finish_route_run(train_id)`` immediately,
    which for ``RouteMode.ONE_OFF`` then calls ``route.deactivate()``
    again. The user-visible result is an "activated" toast followed by an
    auto-deactivation a few ticks later, plus any assigned train silently
    unassigned. Reject activation on a 0-task route up front so this
    sequence is impossible.

    Raises:
        NotFoundError when the route does not exist.
        ValidationError when the route has no tasks.
    """
    route = _require_route(state, route_id)
    if not route.tasks:
        raise ValidationError(
            "Cannot activate a route with no tasks. Add at least one "
            "task before activating."
        )
    # Per design_new.md lines 1278-1286: a route is runnable only if it ends in
    # a stop (optionally followed by dwell tasks) — which also guarantees at
    # least one stop, so a waypoint-only route is rejected — and carries an
    # in-range priority. These are enforced at activation time.
    non_dwell_tasks = [t for t in route.tasks if t.task_kind != "dwell"]
    if not non_dwell_tasks or non_dwell_tasks[-1].task_kind != "stop":
        raise ValidationError(
            "A route must contain at least one stop and end in a stop task "
            "(optionally followed by dwell tasks) before activation."
        )
    if not 0 <= int(route.priority) <= 9:
        raise ValidationError(
            f"Route priority must be an integer in 0..9 (got {route.priority!r})."
        )
    if simulation_time is None:
        simulation_time = state.sim_controller.simulation_time
    route.activate(simulation_time)
    return route


def deactivate_route(state: AppState, route_id: str) -> Route:
    """Deactivate the route, fanning out per-train cancel latches.

    Raises:
        NotFoundError when the route does not exist.
    """
    route = _require_route(state, route_id)
    route.deactivate(state.sim_controller)
    return route
