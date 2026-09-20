"""Route definition lifecycle service.

The HTTP route handlers in `routes/train_routes.py` parse JSON, then call
into these functions. Non-HTTP callers (CLI tooling, journey tests) call
the same functions directly with structured kwargs.

The service owns:
  * `route_id` allocation (when not supplied).
  * Mode-string canonicalization (incl. legacy "one_way"/"loop" aliases).
  * `Route.last_edited_at` bookkeeping for the definition-edit
    fields (name/tasks/mode/priority/manual_override)
    that design_new.md "Route edit timestamps" tracks.
  * Validation of `assigned_train_ids` (only existing trains are
    actually assigned -- unknown IDs are silently dropped, matching the
    pre-refactor behavior).
  * Lookup helpers that raise `NotFoundError` rather than returning None
    when the caller asked for a specific route.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from app_state import AppState
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
    "manual_override",
)

# Per design_new.md "RouteRun overrides" — fields that may carry per-field
# push policies on a PUT /api/train-routes/<id> edit. The overrides themselves
# are written via PATCH /api/route-runs/<run_number>/overrides; this list
# names the route-level fields whose edits may distribute through the
# RouteExecutor.apply_route_field_push helper.
OVERRIDE_ALLOWLIST = (
    "priority",
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
    # Per design_new.md "Route-definition validation": task_kind is required at task creation;
    # missing, null, or unrecognized task_kind is a validation error — the
    # creation path never infers a default kind. (RouteTask.from_dict stays
    # lenient for deserialization of already-validated persisted routes.)
    raw_kind = item.get("task_kind")
    if raw_kind not in ("waypoint", "stop", "dwell", "run"):
        raise ValidationError(
            "task_kind is required and must be one of 'waypoint', 'stop', "
            f"'dwell', or 'run' (got {raw_kind!r})"
        )
    if raw_kind == "run":
        # Per design_new.md "Task time semantics": a run is a timed roll — it requires a
        # finite task_time > 0 (the roll duration in simulated seconds) and must
        # omit target_time (a departure timestamp, meaningless for a run).
        task_time = item.get("task_time")
        if (
            not isinstance(task_time, (int, float))
            or isinstance(task_time, bool)
            or not math.isfinite(float(task_time))
            or float(task_time) <= 0
        ):
            raise ValidationError(
                "a run task requires a finite task_time > 0 (got "
                f"{task_time!r})"
            )
        if item.get("target_time") is not None:
            raise ValidationError("a run task must omit target_time")
    return RouteTask.from_dict(dict(item))


def _registry(state: AppState) -> dict:
    return state.sim_controller.route_executor.route_registry


def _unassign_train_from_other_routes(
    state: AppState, keep_route_id: str, train_id: str
) -> None:
    """Enforce the single-assignment rule (design_new.md, "Single-assignment rule"): a train is
    assigned to at most one route at a time. Before assigning `train_id` to
    `keep_route_id`, unassign it from every other route that currently lists it,
    so a route create/update never leaves a train in two routes' assignment
    sets at once.
    """
    for other in _registry(state).values():
        if other.route_id != keep_route_id and train_id in other.assigned_train_ids:
            other.unassign_train(train_id)


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


def _reject_leading_dwell(tasks) -> None:
    """A route's first task must not be a dwell (design_new.md, "Route-definition validation").

    A dwell holds after a preceding task at that location; the initial walk has
    no predecessor, so there is nothing to hold after. The API rejects a
    leading dwell at creation/update time.
    """
    if tasks and tasks[0].task_kind == "dwell":
        raise ValidationError(
            "Route.tasks[0] must not be a dwell task (design_new.md, 'Route-definition validation'): a "
            "dwell holds after a preceding task, but the first task has no "
            "predecessor."
        )


def _validate_route_definition(tasks, mode, priority) -> None:
    """Create/update-time structural validation (design_new.md
    "Route-definition validation").

    Takes the proposed `tasks` / `mode` / `priority` (not a live `Route`) so a
    caller can validate an edit before committing it. Covers the rules the doc
    puts at creation/update time: the priority range, the CIRCULAR / ROUND_TRIP
    distinct-location guard, and the dwell-follow rule. It deliberately does NOT
    check that a non-dwell task exists or that a non-circular route ends in a
    stop/run — those are completeness checks the doc gates at activation
    ("before a RouteRun may be created"), so an incomplete route may still be
    saved as a draft. Per-task shape (`task_kind`, run `task_time` /
    `target_time`) is validated by `_coerce_route_task`, and the leading-dwell
    rule by `_reject_leading_dwell`.
    """
    if not 0 <= int(priority) <= 9:
        raise ValidationError(
            f"Route priority must be an integer in 0..9 (got {priority!r})."
        )

    tasks = list(tasks)
    non_dwell = [t for t in tasks if t.task_kind != "dwell"]

    # CIRCULAR / ROUND_TRIP need two distinct non-dwell locations to travel /
    # reflect between (design_new.md, the CIRCULAR distinct-location rule and
    # the ROUND_TRIP degenerate-route guard); a loop whose moves all name one
    # location has nothing to travel between. Only checked once the route has
    # non-dwell tasks — an empty draft defers to the activation-time
    # non-dwell-exists check rather than being rejected as "one location".
    if (
        mode in (RouteMode.CIRCULAR, RouteMode.ROUND_TRIP)
        and non_dwell
        and len({t.task_location for t in non_dwell}) < 2
    ):
        raise ValidationError(
            f"A {mode.value} route must contain at least two non-dwell tasks "
            "whose task_location values differ."
        )

    # Every maximal run of dwell tasks must directly follow a stop or run at the
    # same task_location (design_new.md, the dwell-follow rule): a waypoint never
    # establishes a stopped, authority-free state to hold after.
    for i, task in enumerate(tasks):
        if task.task_kind != "dwell":
            continue
        j = i - 1
        while j >= 0 and tasks[j].task_kind == "dwell":
            j -= 1
        if j < 0 or tasks[j].task_kind not in ("stop", "run"):
            raise ValidationError(
                "A dwell task must directly follow a stop or run task; a "
                "waypoint never establishes a stopped state to hold after."
            )
        if tasks[j].task_location != task.task_location:
            raise ValidationError(
                "A dwell task must name the same task_location as the stop or "
                "run task it follows."
            )


def _validate_route_activatable(tasks, mode, priority) -> None:
    """Activation-time validation (design_new.md): the create/update definition
    rules plus the completeness checks the doc gates at activation — a non-dwell
    task must exist, and a non-circular route must end in a stop or run
    (optionally followed by dwell tasks) so the run has a defined place to come
    to rest."""
    _validate_route_definition(tasks, mode, priority)

    non_dwell = [t for t in tasks if t.task_kind != "dwell"]
    if not non_dwell:
        raise ValidationError(
            "A route must contain at least one non-dwell task (waypoint, stop, "
            "or run) before activation."
        )
    if mode != RouteMode.CIRCULAR and non_dwell[-1].task_kind not in (
        "stop",
        "run",
    ):
        raise ValidationError(
            "A non-circular route must end in a stop or run task "
            "(optionally followed by dwell tasks) before activation."
        )


def create_route(
    state: AppState,
    *,
    route_id: Optional[str] = None,
    name: str = "",
    mode: str = "one_off",
    tasks: Optional[Iterable[Any]] = None,
    assigned_train_ids: Optional[Iterable[str]] = None,
    priority: Optional[int] = None,
    manual_override: Optional[bool] = None,
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
        edit_fields_present: If supplied, controls whether
            last_edited_at is bumped. When None, the helper leaves the
            timestamp alone -- the route handler computes this from
            whether any of the edit fields appeared in the JSON.
    """
    resolved_id = route_id or f"ROUTE-{uuid.uuid4().hex[:6].upper()}"
    resolved_mode = _coerce_mode(mode)

    route = Route(route_id=resolved_id, name=name, mode=resolved_mode)

    if priority is not None:
        try:
            route.priority = int(priority)
        except (TypeError, ValueError):
            raise ValidationError("priority must be an integer")

    if manual_override is not None:
        route.manual_override = bool(manual_override)

    if start_mode is not None:
        route.start_mode = _coerce_start_mode(start_mode)

    if start_task_index is not None:
        route.start_task_index = _coerce_start_task_index(start_task_index)

    coerced_tasks = [_coerce_route_task(task_data) for task_data in (tasks or ())]
    _reject_leading_dwell(coerced_tasks)
    for task in coerced_tasks:
        route.add_task(task)

    _validate_route_definition(route.tasks, route.mode, route.priority)

    if edit_fields_present:
        route.last_edited_at = time.time()

    executor = state.sim_controller.route_executor
    executor.register_route(route)

    # Assignment goes through the canonical assignment writer, never a direct
    # `Route.assigned_train_ids` mutation (design_new.md "Route assignment
    # writers"). A train idle or with no live run is assigned immediately; a
    # train with a live RUNNING run on another route is held in
    # `_pending_route_assignments` and committed by Action after that run tears
    # down, so the single-assignment invariant is never briefly violated. The
    # route is registered first so the writer can resolve it.
    for train_id in assigned_train_ids or ():
        if train_id in state.network.trains:
            executor.request_route_assignment(train_id, route.route_id)

    return route


def update_route(
    state: AppState,
    route_id: str,
    *,
    name: Optional[str] = None,
    mode: Optional[str] = None,
    tasks: Optional[Iterable[Any]] = None,
    priority: Optional[Any] = None,
    manual_override: Optional[Any] = None,
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
    (`"priority"`) to `"force"` or `"soft"`. When omitted the default per
    design_new.md
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
    # teardown per design_new.md "Route Edit And Reconciliation". Timing-only task edits are
    # not structural and do not tear down running runs.
    tasks_structural_edit = False
    mode_structural_edit = False
    # Track which allowlisted fields changed so push policies can be
    # applied after all writes commit.
    changed_overridable: List[str] = []

    # Validate-then-mutate: coerce and validate the replacement task list
    # (including the leading-dwell rule) BEFORE writing any field, so a rejected
    # update does not leave name/mode partially mutated on the live registry
    # route.
    new_tasks: Optional[List[RouteTask]] = None
    if tasks is not None:
        new_tasks = []
        for task_data in tasks:
            if not isinstance(task_data, dict):
                raise ValidationError("task entry must be an object")
            if not task_data.get("task_location"):
                raise ValidationError(
                    "task_location required for each task"
                )
            new_tasks.append(_coerce_route_task(task_data))
        _reject_leading_dwell(new_tasks)

    # Validate the PROPOSED (tasks, mode, priority) before mutating the live
    # route, so a rejected edit leaves the existing route unchanged
    # (design_new.md "Route-definition validation" requires create/update to
    # reject the same structural violations activation does).
    proposed_tasks = new_tasks if tasks is not None else route.tasks
    proposed_mode = _coerce_mode(mode) if mode is not None else route.mode
    if priority is not None:
        try:
            proposed_priority = int(priority)
        except (TypeError, ValueError):
            raise ValidationError("priority must be an integer")
    else:
        proposed_priority = route.priority
    _validate_route_definition(proposed_tasks, proposed_mode, proposed_priority)

    if name is not None:
        route.name = name
        definition_edit_touched = True

    if mode is not None:
        # Route.mode is structural (design_new.md "Route Edit And Reconciliation"): it selects the
        # CIRCULAR wrap / ROUND_TRIP reflection / ONE_OFF terminal behaviour
        # that RouteRun._append_next_walk_task reads live on every append, so a
        # live mode flip could put a running walk into a shape route validation
        # would have rejected. An edit that actually changes the mode latches
        # teardown alongside a tasks edit; re-writing the same mode does not.
        new_mode = _coerce_mode(mode)
        mode_structural_edit = new_mode != route.mode
        route.mode = new_mode
        definition_edit_touched = True

    if new_tasks is not None:
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

    if manual_override is not None:
        # Route-level operator override (design_new.md "`disabled` and `manual` availability flags"). Bumps
        # last_edited_at (it is in the EDIT_FIELDS set) but is NOT in
        # OVERRIDE_ALLOWLIST, so it never pushes onto running RouteRuns: edits
        # take effect on the next create/extend with no run teardown.
        route.manual_override = bool(manual_override)
        definition_edit_touched = True

    # start_mode / start_task_index are definition edits but are NOT in the
    # last_edited_at bump-set (design_new.md "Route edit timestamps": the set is
    # exactly name/tasks/mode/priority/manual_override). They are also NOT
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

        # Route assignment/unassignment goes through the canonical assignment
        # writer, never a direct `Route.assigned_train_ids` mutation
        # (design_new.md "Route assignment writers"): a train with a live
        # RUNNING run keeps that run until Action tears it down, and the
        # replacement lives in `_pending_route_assignments` until then, so a
        # running train is never left assigned to two routes at once.
        sim_controller = getattr(state, "sim_controller", None)
        executor = (
            getattr(sim_controller, "route_executor", None)
            if sim_controller is not None
            else None
        )
        if executor is not None:
            for train_id in current_train_ids - requested_train_ids:
                executor.request_route_assignment(train_id, None)
            for train_id in requested_train_ids - current_train_ids:
                if train_id in state.network.trains:
                    executor.request_route_assignment(train_id, route.route_id)
        else:
            # Editor-only mode (no runtime surface): fall back to the direct
            # membership write, since there is no RouteRun to tear down.
            for train_id in current_train_ids - requested_train_ids:
                route.unassign_train(train_id)
            for train_id in requested_train_ids - current_train_ids:
                if train_id in state.network.trains:
                    _unassign_train_from_other_routes(
                        state, route.route_id, train_id
                    )
                    route.assign_train(train_id)

    # Per design_new.md "Shared-route edit teardown": a structural Route.tasks
    # edit tears down every assigned train's currently-RUNNING RouteRun (there is
    # no in-place reconciliation in v1). Set the per-train route_deactivate latch
    # (which fans out the inclusive path cancel when a live path exists). Action
    # commits the teardown via RouteExecutor.remove_train_route_run(train_id),
    # which UNASSIGNS the train from the run's route_id -- so the re-assignment
    # cannot be applied up front (it would be undone by that unassign). Instead,
    # hold it on RouteExecutor._pending_reassign and let the Action deactivate
    # consumer re-apply it AFTER the clear; the next eligible Plan then sees
    # the train assigned with no RUNNING run and allocates a fresh RouteRun
    # against the edited Route.tasks. Skipped in editor-only mode (no runtime
    # surface wired, so there is no RouteRun to tear down).
    if tasks_structural_edit or mode_structural_edit:
        sim_controller = getattr(state, "sim_controller", None)
        if sim_controller is not None:
            route_executor = getattr(sim_controller, "route_executor", None)
            teardown_reason = (
                f"route_tasks_edited:{route.route_id}"
                if tasks_structural_edit
                else f"route_mode_edited:{route.route_id}"
            )
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
    """Delete a route, deferring live teardown. Returns True on success.

    Per design_new.md "Route deletion waits for live teardown": `DELETE /api/train-routes/<route_id>` never removes a route used
    by a live or pending run inline. Instead it

      1. rejects further assignment/activation by setting `Route.is_active =
         False` and recording the id in `RouteExecutor.pending_route_deletions`;
      2. for every assigned train, clears any held same-route re-assignment
         (a route pending deletion is never re-applied by `_pending_reassign`).
         A train with a live controller then gets its per-train
         `route_deactivate` latch set, letting the inclusive cancel + Action
         deactivate consumer perform the ordinary authority and run teardown. A
         train the sim has never ticked has no controller (paused, just-loaded,
         or assigned while paused) and so no live run -- the Action consumer
         would never run for it, so it is unassigned inline through
         `remove_train_route_run(...)`, which nothing else would otherwise do;
      3. leaves the `Route` in the active registry while any train remains
         assigned or any RUNNING run names it, so `get_route(...)` keeps
         resolving for every live run during Plan and Action; and
      4. calls `finalize_pending_route_deletions()`, which archives the
         immutable definition to `_historical_routes` and unregisters the route
         only once nothing holds it. When the route has no assignment, live run,
         or pending teardown at DELETE time, that finalization runs here and now.

    Repeating DELETE while teardown is pending, or after the route has been
    archived, is idempotent.

    Raises:
        NotFoundError when the route has never existed (a route already archived
        by a completed deferred deletion returns True instead of raising).
    """
    registry = _registry(state)
    executor = state.sim_controller.route_executor

    if route_id not in registry:
        # Idempotent after archival: a route already removed by a completed
        # deferred deletion returns True rather than 404.
        if executor.get_historical_route(route_id) is not None:
            return True
        raise NotFoundError(f"Route {route_id!r} not found")

    route = registry[route_id]
    # Reject further assignment/activation, and record the deferred-deletion
    # intent. The registry retains the Route until nothing holds it.
    route.is_active = False
    executor.pending_route_deletions.add(route_id)

    # Fan out the per-train deactivate teardown; never unassign inline. Each
    # assigned train's later Action deactivate consumer runs
    # `remove_train_route_run(...)`, which archives + unassigns its run.
    for train_id in list(route.assigned_train_ids):
        executor.discard_pending_reassign(train_id)
        controller = state.sim_controller.get_controller(train_id)
        if controller is not None:
            controller.route_hooks.phase_latches.set_route_deactivate(
                reason=f"route_deleted:{route_id}",
                has_active_path=(
                    controller.path_projection.received_path_plan is not None
                ),
            )
        else:
            # No controller yet (train assigned before its first Plan
            # allocation, a paused or just-loaded sim): no Action pass will ever
            # consume a deactivate latch, so run the same teardown now. Its
            # no-run branch removes the assignment, so `finalize_pending_route_
            # deletions()` below can archive + unregister the route instead of
            # stranding it in the active registry forever.
            executor.remove_train_route_run(train_id)

    # Archive + unregister now when nothing holds the route (no assignment, no
    # live run); otherwise the later teardown consumers trigger the same
    # finalization once they release it.
    executor.finalize_pending_route_deletions()
    return True


def activate_route(
    state: AppState, route_id: str, simulation_time: Optional[float] = None
) -> Route:
    """Activate the route at simulation_time (defaults to controller now).

    A route with no tasks would be terminal on the first eligible Plan
    tick: ``_find_next_task_target`` returns ``None`` for an empty walk,
    which makes Plan call ``finish_route_run(train_id)`` immediately,
    which for ``RouteMode.ONE_OFF`` then calls ``deactivate_route(route_id)``
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
    # Per design_new.md "Route-definition validation": a route must carry at
    # least one non-dwell task and an in-range priority, enforced at activation.
    # A CIRCULAR route never terminates, so it may run continuously with no stop
    # (e.g. a waypoint-only loop, or a run/dwell loop) — there is no terminal
    # task to "complete". A ONE_OFF / ROUND_TRIP route must still end at a task
    # that brings the train to rest: a `stop`, or a timed `run` (which ends at a
    # smooth ATC stop), optionally followed by dwell tasks.
    _validate_route_activatable(route.tasks, route.mode, route.priority)
    # design_new.md "Route edit timestamps": activation refuses while an assigned
    # train still holds `pending_route_deactivate`. Route.activate() is a pure
    # data write with no controller reach, so this canonical activation entry
    # enforces the refusal. Resurrecting a run scheduled for teardown would let
    # a route the operator is tearing down re-allocate against that train; the
    # latch clears on the train's next eligible Action, so the operator retries.
    sim_controller = state.sim_controller
    for train_id in list(route.assigned_train_ids):
        controller = (
            sim_controller.get_controller(train_id)
            if sim_controller is not None
            else None
        )
        if controller is None:
            continue
        if controller.route_hooks.phase_latches.route_deactivate:
            raise ValidationError(
                f"Cannot activate route while train {train_id} is still "
                "deactivating a prior assignment. Retry once its teardown "
                "completes."
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
    # The per-train cancel-latch walk is the canonical job of
    # RouteExecutor.deactivate_route(route_id) (design_new.md, "Arrival detection"); Route
    # .deactivate() itself writes only is_active.
    state.sim_controller.route_executor.deactivate_route(route_id)
    return route
