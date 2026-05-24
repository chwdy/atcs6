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
from typing import Any, Iterable, List, Optional

from app_state import AppState
from runtime_payloads import AcquisitionMode
from services.exceptions import NotFoundError, ValidationError
from train_route import Route, RouteMode, RouteTask


_MODE_ALIASES = {
    "one_way": RouteMode.ONE_OFF,
    "loop": RouteMode.CIRCULAR,
}

EDIT_FIELDS = ("name", "tasks", "mode", "priority", "acquisition_mode")


def _coerce_mode(mode_str: str) -> RouteMode:
    """Convert a wire-string into RouteMode, accepting legacy aliases."""
    try:
        return RouteMode(mode_str)
    except ValueError:
        if mode_str in _MODE_ALIASES:
            return _MODE_ALIASES[mode_str]
        raise ValidationError(f"Invalid mode {mode_str!r}")


def _coerce_route_task(item: Any) -> RouteTask:
    """Build a RouteTask from a JSON request item."""
    if not isinstance(item, dict):
        raise ValidationError("task entry must be an object")
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
    assigned_train_ids: Optional[Iterable[str]] = None,
) -> Route:
    """Apply a partial update to an existing route.

    Each kwarg is None to leave that field untouched. assigned_train_ids
    is the full replacement set; trains not in the list will be
    unassigned and trains in the list (and known to the network) will be
    assigned. Unknown trains are silently dropped.

    Raises:
        NotFoundError when no route exists with that id.
        ValidationError on bad input shapes.
    """
    route = _require_route(state, route_id)

    definition_edit_touched = False

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

    if acquisition_mode is not None:
        try:
            route.acquisition_mode = AcquisitionMode(acquisition_mode)
        except ValueError:
            raise ValidationError(
                f"Invalid acquisition_mode {acquisition_mode!r}"
            )
        definition_edit_touched = True

    if assigned_train_ids is not None:
        requested_train_ids = set(assigned_train_ids)
        current_train_ids = set(route.assigned_train_ids)

        for train_id in current_train_ids - requested_train_ids:
            route.unassign_train(train_id)

        for train_id in requested_train_ids - current_train_ids:
            if train_id in state.network.trains:
                route.assign_train(train_id)

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
    for train_id in previously_assigned:
        executor.execution_states.pop(train_id, None)

    executor.unregister_route(route_id)
    return True


def activate_route(
    state: AppState, route_id: str, simulation_time: Optional[float] = None
) -> Route:
    """Activate the route at simulation_time (defaults to controller now).

    Raises:
        NotFoundError when the route does not exist.
    """
    route = _require_route(state, route_id)
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
