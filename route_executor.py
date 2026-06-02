"""
Route Executor

Per design_new.md "Route And RouteExecutor" and "Planning Kernel" sections.

RouteExecutor owns:
  * Phase 1 planning (`tick_train_phase1`) for one train: route-deviation
    cleanup, dwell-complete timer check, covered-task index advance,
    dwell-entry, target resolution, and authority acquisition via
    PathDispatcher.
  * Phase 3 callbacks invoked synchronously by PathDispatcher:
    `handle_train_advanced`, `handle_path_cancellation`, `handle_path_completion`.
  * Runnable-train ordering for the per-tick simulation loop.
  * Removal of route runs for the abort/power-off paths.

RouteExecutor never calls `PathDispatcher.cancel_path(...)` directly. Phase 1
that wants to cancel an active path latches `phase_latches.path_cancel_request`
on the controller mailbox and waits for the Phase 3 cancel→callback→cleanup
flow.
"""

from dataclasses import dataclass, field
import logging
import time
from typing import Any, ClassVar, Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING

from enums import ControllerCondition, Direction, TrainState
from train_controller import TrainController, create_controller_for_train
from train_route import (
    RouteMode,
    RouteRunStatus,
    RouteTask,
    RouteTaskRun,
    WaypointUnreachableAction,
)
from runtime_payloads import (
    AcquisitionMode,
    ErrorRecord,
    FatalConflictKind,
    FatalPhysicalEntryConflict,
    PathAcquisitionFailureKind,
    PathDispatcherForRouteExecutor,
    PendingPathCancelRequest,
    ResolvedRouteTarget,
    ResourceEvent,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from train_route import Route

logger = logging.getLogger(__name__)


# Pending admin/system cancellation latches that gate Phase-1 authority
# acquisition per design_new.md (Planning Kernel section). Both create and
# extend branches require this set to be empty in active_conditions.
_PENDING_CANCEL_CONDITIONS = frozenset(
    {
        ControllerCondition.PATH_CANCEL_PENDING,
        ControllerCondition.ROUTE_ABORT_PENDING,
        ControllerCondition.ROUTE_DEACTIVATE_PENDING,
    }
)


# =============================================================================
# Resolved-target schemas
# =============================================================================
# `ResolvedRouteTarget` is now imported from runtime_payloads (S5 migration).


@dataclass
class ResolvedNextTaskProjection:
    """Projection returned by `_find_next_task_target(train_id)`.

    Read-only result. Phase 1's authority-acquisition step is the only writer
    of `task_runs[walk_position].resolved_target`; this helper itself never
    writes that field.

    Per design_new.md line 1089, `walk_position` is the walk-position index
    in `RouteRun.task_runs`. The underlying `RouteTask` is
    `Route.tasks[task_runs[walk_position].task_index]`.

    `failure_reason` carries the resolver's diagnostic when
    `resolved_target is None` for a non-dwell task because every candidate
    block / platform was filtered out (disabled, manual without override,
    every station platform unavailable). Phase 1 writes this string into
    `task_runs[walk_position].last_error` via `_record_run_error(...)` so
    the operator sees why the task is stalled instead of an empty
    diagnostic. `None` for dwell projections and for the same-tick exclude
    sentinel (all candidates already attempted) — both of those non-None-
    target paths do not need a fresh resolver-level error record.
    """

    walk_position: int
    task_kind: str  # "waypoint" | "stop" | "dwell"
    direction: str  # "forward" | "reverse"
    resolved_target: Optional[ResolvedRouteTarget] = None
    failure_reason: Optional[str] = None


# =============================================================================
# RouteExecutionState (a.k.a. RouteRun in design_new.md)
# =============================================================================


@dataclass
class RouteExecutionState:
    """Per-train route run state (the design doc calls this `RouteRun`).

    Per design_new.md "Route-run state" table.
    """

    train_id: str
    route_id: str
    # System-time Unix seconds when this run instance started. Per
    # design_new.md "Route-run state": "Written exactly once at `RouteRun`
    # creation as `current_time` and never mutated afterward." Required at
    # construction; no default.
    started_at: float
    # Effective traversal direction for the current directed walk.
    # Per design_new.md "Route-run state" — stored as the literal string
    # `"forward"` or `"reverse"`. ROUND_TRIP arithmetic computes a local
    # signed integer from this literal at use sites; the stored field is
    # always the string form.
    direction: str = "forward"
    # Per-run task table, parallel to Route.tasks. Allocated when the run is
    # created (or on first use) and grown lazily as Route.tasks gains entries.
    task_runs: List[RouteTaskRun] = field(default_factory=list)
    # Composite run-instance number assigned by RouteExecutor (diagnostic only).
    run_number: int = 0
    # v1 latch consumed by Phase-1 route-deviation cleanup. Also drives the
    # derived `display_state` projection (DEVIATED while True).
    path_cancelled: bool = False
    # Per-run lifecycle state. Stored as the canonical `RouteRunStatus` enum
    # (RUNNING / FINISHED / DEACTIVATED). Deviation is NOT a stored state;
    # consumers that need the diagnostic read `display_state` instead.
    current_route_status: RouteRunStatus = RouteRunStatus.RUNNING
    # Per-run error tracking. Per design_new.md "Canonical ownership split"
    # the active path is read live from `path_dispatcher.active_path_id_by_train`
    # / `received_path_plan.path_id`; route runs do not store a correlation
    # field of their own. `last_error` is the canonical ErrorRecord shape so
    # message and timestamp are always either both present or both absent.
    last_error: Optional["ErrorRecord"] = None

    # Sparse per-run overrides of allowlisted route-level operational fields.
    # Per design_new.md "RouteRun overrides" canonical home: keys are field
    # names from the v1 override allowlist
    # (`"priority"`, `"acquisition_mode"`, `"action_on_waypoint_unreachable"`);
    # a key being absent means "use the shared `Route` value for that field."
    # Reads go through the effective_* helpers below, which resolve the shared
    # Route live each access so later operator edits to either the run-level
    # override or the shared Route take effect on the next read.
    overrides: Dict[str, Any] = field(default_factory=dict)
    # Diagnostic-only audit timestamp; bumped by RouteExecutor.set_run_override /
    # clear_run_override per design_new.md. Not consumed for control flow.
    overrides_updated_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Effective-value read helpers (per design_new.md "RouteRun overrides")
    # ------------------------------------------------------------------

    OVERRIDE_ALLOWLIST: ClassVar[Tuple[str, ...]] = (
        "priority",
        "acquisition_mode",
        "action_on_waypoint_unreachable",
    )

    def _resolve_route(self, route_executor: Optional[object] = None) -> Optional["Route"]:
        """Resolve the shared Route for effective-value fallback.

        Callers that already hold a `Route` reference pass it via the
        `route` argument on `effective_*`. When neither is available the
        helpers fall back to the stored value (or the shared-route default
        when nothing is stored) so they remain safe to call outside a
        RouteExecutor context.
        """
        if route_executor is None:
            return None
        getter = getattr(route_executor, "get_route", None)
        if not callable(getter):
            return None
        try:
            return getter(self.route_id)
        except Exception:
            return None

    def effective_priority(
        self,
        route: Optional["Route"] = None,
        route_executor: Optional[object] = None,
    ) -> int:
        """Per-run priority: override if present, else shared Route.priority,
        else default 5. See design_new.md "RouteRun overrides"."""
        if "priority" in self.overrides:
            return int(self.overrides["priority"])
        resolved = route if route is not None else self._resolve_route(route_executor)
        if resolved is not None:
            return int(getattr(resolved, "priority", 5))
        return 5

    def effective_acquisition_mode(
        self,
        route: Optional["Route"] = None,
        route_executor: Optional[object] = None,
    ) -> AcquisitionMode:
        """Per-run acquisition_mode: override if present, else shared Route's,
        else BEST_EFFORT. See design_new.md "RouteRun overrides"."""
        if "acquisition_mode" in self.overrides:
            val = self.overrides["acquisition_mode"]
            if isinstance(val, AcquisitionMode):
                return val
            try:
                return AcquisitionMode(val)
            except Exception:
                return AcquisitionMode.BEST_EFFORT
        resolved = route if route is not None else self._resolve_route(route_executor)
        if resolved is not None:
            return getattr(resolved, "acquisition_mode", AcquisitionMode.BEST_EFFORT)
        return AcquisitionMode.BEST_EFFORT

    def effective_action_on_waypoint_unreachable(
        self,
        route: Optional["Route"] = None,
        route_executor: Optional[object] = None,
    ) -> WaypointUnreachableAction:
        """Per-run action_on_waypoint_unreachable: override if present, else
        shared Route's, else WAIT. See design_new.md "Action on waypoint
        unreachable" + "RouteRun overrides"."""
        if "action_on_waypoint_unreachable" in self.overrides:
            val = self.overrides["action_on_waypoint_unreachable"]
            if isinstance(val, WaypointUnreachableAction):
                return val
            try:
                return WaypointUnreachableAction(val)
            except Exception:
                return WaypointUnreachableAction.WAIT
        resolved = route if route is not None else self._resolve_route(route_executor)
        if resolved is not None:
            return getattr(
                resolved,
                "action_on_waypoint_unreachable",
                WaypointUnreachableAction.WAIT,
            )
        return WaypointUnreachableAction.WAIT

    def task_started_at(self, i: int) -> Optional[float]:
        """Derived per design_new.md ``RouteTaskRun.started_at`` rule.

        Returns ``RouteRun.started_at`` for index 0 and
        ``task_runs[i-1].finished_at`` for i > 0. Returns ``None`` when the
        predecessor has not yet recorded a ``finished_at`` (or when ``i`` is
        out of range). The doc lists ``started_at`` as a derived read; this
        helper is the only canonical reader.
        """
        if i < 0 or i >= len(self.task_runs):
            return None
        if i == 0:
            return self.started_at
        return self.task_runs[i - 1].finished_at

    @property
    def display_state(self) -> str:
        """UI-only diagnostic projection.

        Per design_new.md ``current_route_status`` schema: returns
        ``"DEVIATED"`` whenever ``path_cancelled`` is true; otherwise the
        stored ``current_route_status.value``. Not a stored field; no
        writers. Runtime branching never keys on this value.
        """
        if self.path_cancelled:
            return "DEVIATED"
        return self.current_route_status.value

    @property
    def current_waypoint_index(self) -> Optional[int]:
        """Derived: physical-progress cursor — the largest walk-position
        index `i` in `task_runs` where `task_runs[i].finished_at is not
        None`, or `None` when no task has finished yet.

        Per design_new.md "Route-run state" — `Optional[int]` (read-only
        derived). Computed each access from `task_runs`; not a stored
        field; no writers — the writers that record progress all set
        `task_runs[i].finished_at`, and the cursor reflects that update on
        the next read. Returns the underlying `task_index` of the last
        finished entry (which differs from its position in `task_runs` for
        `CIRCULAR` / `ROUND_TRIP` walks past `len(Route.tasks)`).

        Consumers that want "next task to plan" should use
        `next_planning_index` instead.
        """
        last_finished_index: Optional[int] = None
        for run in self.task_runs:
            if run.finished_at is not None:
                last_finished_index = run.task_index
        return last_finished_index

    @property
    def next_planning_index(self) -> int:
        """Derived: index in `Route.tasks` of the next task to plan or
        execute on the walk.

        For an empty walk and a walk where no task has finished yet, this
        is `0`. Otherwise it is the `task_index` of the first entry in
        `task_runs` whose `finished_at is None`. If every entry is
        finished (terminal walk), it is the last entry's `task_index`.
        """
        if not self.task_runs:
            return 0
        for run in self.task_runs:
            if run.finished_at is None:
                return run.task_index
        return self.task_runs[-1].task_index

    @property
    def next_planning_walk_position(self) -> int:
        """Derived: list position in `task_runs` of the next non-finished
        entry — the walk-position counterpart of `next_planning_index`.

        Per design_new.md line 1104 + line 1267, `walk_position` (the list
        index in `task_runs`) and `task_index` (the index into
        `Route.tasks`) decouple after `CIRCULAR` / `ROUND_TRIP` wrap-around
        when `len(task_runs) > len(Route.tasks)`. Callers that need to
        index `task_runs` directly must use this property, not
        `next_planning_index`.

        Returns `0` for an empty walk. Returns `len(task_runs) - 1` for a
        terminal walk (every entry finished).
        """
        if not self.task_runs:
            return 0
        for i, run in enumerate(self.task_runs):
            if run.finished_at is None:
                return i
        return len(self.task_runs) - 1

    def _append_next_walk_task(self, route: "Route") -> None:
        """Grow `task_runs` for the next walk-position when applicable.

        Per design_new.md (Canonical Walk-Append Rule, ~line 1226): callers
        invoke this helper immediately after writing
        ``task_runs[i].finished_at``. The helper is a no-op for ``ONE_OFF``
        routes and inside the initial pre-allocation; it appends a fresh
        ``RouteTaskRun`` only at wrap/reflection points for ``CIRCULAR`` /
        ``ROUND_TRIP``.

        - ``ONE_OFF``: never appends.
        - ``CIRCULAR``: append ``task_index = (last.task_index + 1) % N``.
        - ``ROUND_TRIP``: forward past the last task reflects to
          ``task_count - 2`` (second-to-last); reverse past the first task
          reflects to index ``1`` (second). The appended entry carries no
          ``direction`` field; ``RouteRun.direction`` is updated by
          ``_find_next_task_target(...)``.

        ``route`` may be ``None`` if the caller cannot resolve the shared
        route definition; the helper then no-ops safely.
        """
        if route is None or route.mode == RouteMode.ONE_OFF:
            return
        if not self.task_runs:
            return
        last = self.task_runs[-1]
        if last.finished_at is None:
            return  # the latest walk-position is still in flight
        task_count = len(route.tasks)
        if task_count == 0:
            return
        last_index = last.task_index

        if route.mode == RouteMode.CIRCULAR:
            next_index = (last_index + 1) % task_count
        elif route.mode == RouteMode.ROUND_TRIP:
            # Local sign integer derived from the canonical string field.
            sign = 1 if self.direction == "forward" else -1
            if sign >= 0:
                if last_index >= task_count - 1:
                    next_index = max(0, task_count - 2)
                else:
                    next_index = last_index + 1
            else:
                if last_index <= 0:
                    next_index = min(1, task_count - 1)
                else:
                    next_index = last_index - 1
        else:
            return

        self.task_runs.append(RouteTaskRun(task_index=next_index))


class RouteExecutor:
    """Manages route execution for all trains."""

    def __init__(
        self,
        network: "RailwayNetwork",
        path_dispatcher: Optional[PathDispatcherForRouteExecutor] = None,
        shared_controllers: Optional[Dict[str, TrainController]] = None,
    ) -> None:
        self.network = network
        self.path_dispatcher: Optional[PathDispatcherForRouteExecutor] = path_dispatcher
        # Canonical Route registry (per design_new.md "Route And RouteExecutor").
        # Keyed by route_id. Mutated only via register_route / unregister_route.
        self.route_registry: Dict[str, "Route"] = {}
        # `execution_states` holds the currently-RUNNING run for each train
        # (keyed by train_id; one slot per train). Per design_new.md, a run is
        # not deleted on teardown — its state flips to FINISHED / DEACTIVATED
        # and the row is moved to `_historical_runs` so a subsequent fresh run
        # for the same train can occupy the slot. The active-run derived read
        # is `execution_state_for(train_id)`; UI/log consumers read history
        # via `history_for_train(train_id)`.
        self.execution_states: Dict[str, RouteExecutionState] = {}
        self._historical_runs: List[RouteExecutionState] = []
        # Per design_new.md (fix #28): RouteExecutor no longer maintains its
        # own controller registry. All lookups go through
        # self.simulation_controller.get_or_create_controller(train_id). The
        # shared_controllers arg is accepted for backward compatibility with
        # tests that construct RouteExecutor in isolation; it is stored but
        # not used for lookups — those always go through simulation_controller
        # when one is present.
        self._shared_controllers = shared_controllers  # kept for test compat
        # Per-route run-instance counter (1-based). diagnostic only.
        self._run_count_for_route: Dict[str, int] = {}

        # Helper modules — lazy-init on first access. The actual logic lives
        # in `route_target_resolver.RouteTargetResolver` and
        # `train_at_target_predicates.TrainAtTargetPredicates`; this class
        # keeps thin forwarders so the doc-canonical names continue to work.
        self._target_resolver = None
        self._at_target_predicates = None

    def _get_route_target_resolver(self):
        if self._target_resolver is None:
            from route_target_resolver import RouteTargetResolver

            self._target_resolver = RouteTargetResolver(self)
        return self._target_resolver

    def _get_at_target_predicates(self):
        if self._at_target_predicates is None:
            from train_at_target_predicates import TrainAtTargetPredicates

            self._at_target_predicates = TrainAtTargetPredicates(self)
        return self._at_target_predicates

    # ----------------------------------------------------- route registry API
    def register_route(self, route: "Route") -> None:
        """Register a Route in the canonical registry."""
        self.route_registry[route.route_id] = route

    def unregister_route(self, route_id: str) -> Optional["Route"]:
        """Remove a Route from the registry; return the removed Route or None."""
        return self.route_registry.pop(route_id, None)

    # --------------------------------------------------- canonical accessors
    def execution_state_for(
        self, train_id: str
    ) -> Optional[RouteExecutionState]:
        """Return the train's currently-RUNNING `RouteRun`, or ``None``.

        Per design_new.md: a derived read keyed on
        ``current_route_status.state == RUNNING``. Phases 1, 2, 3, and route
        callbacks all use this accessor (rather than reading the underlying
        dict slot directly) so a stale DEACTIVATED/FINISHED row in the
        registry is never observed as the active run.
        """
        state = self.execution_states.get(train_id)
        if state is not None and state.current_route_status == RouteRunStatus.RUNNING:
            return state
        return None

    def assigned_route_id_for(self, train_id: str) -> Optional[str]:
        """Return the `route_id` of the train's currently-assigned route, or
        ``None``.

        Per design_new.md the canonical store for "which route is a train on?"
        is `Route.assigned_train_ids` on the registry-side, which
        `Route.assign_train(train_id)` writes. Callers that previously read
        `Train.assigned_route_id` go through this accessor; `Train` no longer
        carries the field.

        Resolution order:
          1. Prefer the live RouteRun in `execution_states[train_id]` (covers
             PLANNING and RUNNING; terminal runs are archived out of the slot
             by `_archive_run` so they correctly read as ``None``).
          2. Otherwise scan `route_registry` for the first route whose
             `assigned_train_ids` contains the train. This covers the
             between-tick window where `Route.assign_train(train_id)` has
             been called but Phase 1 has not yet allocated an exec_state.
        """
        state = self.execution_states.get(train_id)
        if state is not None:
            return state.route_id
        for route_id, route in self.route_registry.items():
            if train_id in getattr(route, "assigned_train_ids", []):
                return route_id
        return None

    def history_for_train(self, train_id: str) -> List[RouteExecutionState]:
        """Return every `RouteRun` for `train_id`, sorted by `run_number`.

        Per design_new.md (~line 1162): includes RUNNING, FINISHED, and
        DEACTIVATED rows across all routes the train has been assigned to.
        """
        out: List[RouteExecutionState] = [
            r for r in self._historical_runs if r.train_id == train_id
        ]
        live = self.execution_states.get(train_id)
        if live is not None and live.train_id == train_id:
            out.append(live)
        return sorted(out, key=lambda r: r.run_number)

    def history_for_route(self, route_id: str) -> List[RouteExecutionState]:
        """Return every `RouteRun` for `route_id`, sorted by start.

        The route-keyed counterpart of `history_for_train`: includes RUNNING,
        FINISHED, and DEACTIVATED rows for the route across every train that
        has run it (archived rows plus any live run still in a slot). A
        read-only aggregation — touches no run state.
        """
        out: List[RouteExecutionState] = [
            r for r in self._historical_runs if r.route_id == route_id
        ]
        for live in self.execution_states.values():
            if live.route_id == route_id:
                out.append(live)
        return sorted(out, key=lambda r: (r.started_at, r.run_number))

    # ------------------------------------------------------------------ helpers
    def _get_controller(self, train: "Train") -> TrainController:
        """Get or create TrainController for a train.

        Per design_new.md (fix #28): delegates to
        ``simulation_controller.get_or_create_controller(train_id)`` when a
        back-reference is available, so the controller registry is always
        owned by SimulationController. Falls back to creating a standalone
        controller (for test contexts without a SimulationController).
        """
        sc = getattr(self, "simulation_controller", None)
        if sc is not None:
            try:
                return sc.get_or_create_controller(train.train_id)
            except KeyError:
                pass
        # Fallback for test contexts with no SimulationController.
        fallback = self._shared_controllers
        if fallback is None:
            fallback = {}
            self._shared_controllers = fallback
        if train.train_id not in fallback:
            fallback[train.train_id] = create_controller_for_train(
                train_id=train.train_id,
                network=self.network,
                path_dispatcher=self.path_dispatcher,
                route_executor=self,
            )
        return fallback[train.train_id]

    def _controller_by_id(self, train_id: str) -> Optional[TrainController]:
        """Look up controller by train_id without creating it.

        Per design_new.md (fix #28): delegates to SimulationController's
        registry when present; falls back to the shared_controllers dict for
        test contexts.
        """
        sc = getattr(self, "simulation_controller", None)
        if sc is not None:
            return getattr(sc, "controllers", {}).get(train_id)
        fallback = self._shared_controllers or {}
        return fallback.get(train_id)

    def _now(self) -> float:
        """System-time Unix seconds; canonical timestamp source for v1."""
        return time.time()

    def _compose_run_number(self, route_id: str, run_count: int) -> int:
        """Compose the diagnostic ``RouteRun.run_number``.

        Per design_new.md: ``route_registry_index * 1_000_000 + run_count``.
        ``route_registry_index`` is the route's insertion order in
        ``self.route_registry`` (stable in CPython 3.7+ dict ordering).
        Falls back to 0 when the route is not yet registered.
        """
        try:
            route_registry_index = list(self.route_registry.keys()).index(route_id)
        except ValueError:
            route_registry_index = 0
        return route_registry_index * 1_000_000 + run_count

    def _get_active_path_id(self, train_id: str) -> Optional[str]:
        """Return the dispatcher's currently active path id for train_id."""
        if self.path_dispatcher is None:
            return None
        return self.path_dispatcher.active_path_id_by_train(train_id)

    # ------------------------------------------------------------------ ordering
    def order_runnable_trains(
        self, candidate_train_ids: Iterable[str]
    ) -> List[str]:
        """Order candidate trains by route priority (descending), stable ties.

        Per design_new.md "Concrete RouteExecutor tick calls": route-managed
        trains use the current shared `Route.priority`; trains without a
        current RouteRun use effective priority 5. The merged set is sorted
        strictly by effective priority descending (`9` highest, `0` lowest),
        with stable existing iteration order as the only tie-break.
        """
        routes = self.route_registry
        ordered = list(candidate_train_ids)

        def effective_priority(train_id: str) -> int:
            # Per design_new.md "RouteRun overrides": the per-run override on
            # `priority` wins over the shared `Route.priority` when set.
            run = self.execution_state_for(train_id)
            route_id = self.assigned_route_id_for(train_id)
            route = routes.get(route_id) if route_id else None
            if run is not None:
                return run.effective_priority(route=route, route_executor=self)
            if route is not None:
                return int(getattr(route, "priority", 5))
            return 5

        ordered.sort(key=effective_priority, reverse=True)
        return ordered

    # ------------------------------------------------------------------
    # Route + RouteRun override helpers (per design_new.md
    # "RouteRun overrides" canonical home).
    # ------------------------------------------------------------------

    def get_route(self, route_id: str) -> Optional["Route"]:
        """Resolve a shared `Route` from the registry. Used by
        `RouteExecutionState.effective_*` helpers so they can fall back to
        the live shared value when no per-run override is present.
        """
        return self.route_registry.get(route_id)

    def set_run_override(
        self, train_id: str, field_name: str, value: Any
    ) -> Tuple[bool, str]:
        """Set one allowlisted override on the train's currently RUNNING run.

        Per design_new.md "RouteRun overrides" canonical home: only the v1
        allowlist (`priority`, `acquisition_mode`, `action_on_waypoint_unreachable`)
        is accepted. Validation is per-field. Historical (FINISHED / DEACTIVATED)
        runs reject override edits because their snapshot is part of the
        historical record.

        Returns (success, message). No RUNNING run is a no-op success so
        callers can treat the helper as idempotent for non-RUNNING trains.
        """
        if field_name not in RouteExecutionState.OVERRIDE_ALLOWLIST:
            return (False, f"override field {field_name!r} is not in v1 allowlist")
        run = self.execution_state_for(train_id)
        if run is None:
            return (True, "no RUNNING route_run for train")
        # Per-field type validation.
        if field_name == "priority":
            try:
                int_val = int(value)
            except (TypeError, ValueError):
                return (False, "priority must be an integer")
            if int_val < 0 or int_val > 9:
                return (False, "priority must be in 0..9")
            run.overrides[field_name] = int_val
        elif field_name == "acquisition_mode":
            if isinstance(value, AcquisitionMode):
                run.overrides[field_name] = value
            else:
                try:
                    run.overrides[field_name] = AcquisitionMode(value)
                except (TypeError, ValueError):
                    return (False, f"acquisition_mode must be one of {[m.value for m in AcquisitionMode]}")
        elif field_name == "action_on_waypoint_unreachable":
            if isinstance(value, WaypointUnreachableAction):
                run.overrides[field_name] = value
            else:
                try:
                    run.overrides[field_name] = WaypointUnreachableAction(value)
                except (TypeError, ValueError):
                    return (
                        False,
                        f"action_on_waypoint_unreachable must be one of "
                        f"{[a.value for a in WaypointUnreachableAction]}",
                    )
        run.overrides_updated_at = self._now()
        return (True, f"override {field_name} set on run {run.run_number}")

    def clear_run_override(
        self, train_id: str, field_name: str
    ) -> Tuple[bool, str]:
        """Remove one allowlisted override from the train's currently RUNNING
        run, restoring the shared-route value for that field.

        Per design_new.md "RouteRun overrides": clearing a key that is not
        present is a no-op success. No RUNNING run is a no-op success too.
        """
        if field_name not in RouteExecutionState.OVERRIDE_ALLOWLIST:
            return (False, f"override field {field_name!r} is not in v1 allowlist")
        run = self.execution_state_for(train_id)
        if run is None:
            return (True, "no RUNNING route_run for train")
        if field_name in run.overrides:
            del run.overrides[field_name]
            run.overrides_updated_at = self._now()
        return (True, f"override {field_name} cleared on run {run.run_number}")

    def apply_route_field_push(
        self, route_id: str, field_name: str, push: str
    ) -> Tuple[int, int]:
        """Distribute a route-level field edit to every currently RUNNING
        RouteRun for `route_id` per the push policy.

        Per design_new.md "RouteRun overrides" canonical home:
          - push == "force" — clear `overrides[field_name]` on every RUNNING
            run so the new shared-route value takes effect everywhere.
          - push == "soft" — leave existing overrides intact; runs without
            an override for this field automatically pick up the new shared
            value through the effective-value helpers.

        Returns ``(touched, skipped)``: number of runs whose override was
        cleared, and number left intact (no-op or soft-skipped).
        """
        if field_name not in RouteExecutionState.OVERRIDE_ALLOWLIST:
            return (0, 0)
        if push not in ("force", "soft"):
            push = "soft"
        touched = 0
        skipped = 0
        for run in self.execution_states.values():
            if run.route_id != route_id:
                continue
            if run.current_route_status != RouteRunStatus.RUNNING:
                continue
            if push == "soft":
                skipped += 1
                continue
            # force
            if field_name in run.overrides:
                del run.overrides[field_name]
                run.overrides_updated_at = self._now()
                touched += 1
            else:
                skipped += 1
        return (touched, skipped)

    # ------------------------------------------------------------------ update
    def update(self, delta_seconds: float) -> None:
        """Refresh route-managed scheduling inputs for this tick.

        Per design_new.md, this method does NOT iterate trains. The
        `SimulationController` outer loop owns per-train transactions and
        calls `RouteExecutor.tick_train_phase1(train_id, delta_seconds)` once
        for each runnable train as the entry point of Phase 1.
        """
        return

    # ------------------------------------------------------------------ run mgmt
    def _ensure_run_for(
        self, train: "Train", route: "Route"
    ) -> RouteExecutionState:
        """Get or create the RouteExecutionState for (train, route)."""
        exec_state = self.execution_states.get(train.train_id)
        if exec_state is None or exec_state.route_id != route.route_id:
            now = self._now()
            run_count = self._run_count_for_route.get(route.route_id, 0) + 1
            self._run_count_for_route[route.route_id] = run_count
            exec_state = RouteExecutionState(
                train_id=train.train_id,
                route_id=route.route_id,
                started_at=now,
                run_number=self._compose_run_number(route.route_id, run_count),
            )
            self.execution_states[train.train_id] = exec_state
        # Ensure task_runs is sized to the current Route.tasks list.
        self._sync_task_runs(route, exec_state)
        return exec_state

    def _sync_task_runs(
        self, route: "Route", exec_state: RouteExecutionState
    ) -> None:
        """Grow exec_state.task_runs to match Route.tasks.

        Lazy allocation: each new task index gets a fresh RouteTaskRun row.
        Existing rows are not rewritten so timing/finished state is preserved.
        """
        tasks = getattr(route, "tasks", []) or []
        while len(exec_state.task_runs) < len(tasks):
            i = len(exec_state.task_runs)
            exec_state.task_runs.append(RouteTaskRun(task_index=i))

    def _resolve_run_route(
        self, train_id: str
    ) -> Tuple[Optional["Train"], Optional["Route"], Optional[RouteExecutionState]]:
        """Resolve the (train, route, exec_state) triple for train_id."""
        train = (
            self.network.trains.get(train_id)
            if hasattr(self.network, "trains")
            else None
        )
        if train is None:
            return None, None, None
        route_id = self.assigned_route_id_for(train_id)
        route = None
        if route_id:
            route = self.route_registry.get(route_id)
        exec_state = self.execution_states.get(train_id)
        return train, route, exec_state

    # ------------------------------------------------------------------ Phase 1
    def tick_train_phase1(self, train_id: str, delta_seconds: float) -> None:
        """Canonical Phase 1 entry point for one train.

        Per design_new.md "Planning Kernel" steps (1)-(7). Each step has its
        own gating predicate; steps 1-5 always run, steps 6-7 are gated by
        the Phase-1 eligibility precondition.
        """
        train = (
            self.network.trains.get(train_id)
            if hasattr(self.network, "trains")
            else None
        )
        if train is None:
            return

        controller = self._controller_by_id(train_id)
        if controller is None:
            controller = self._get_controller(train)

        # If controller is OFF, Phase 1 is skipped entirely.
        if controller.ops.state == TrainState.OFF:
            return

        route_id = self.assigned_route_id_for(train_id)
        route = None
        if route_id:
            route = self.route_registry.get(route_id)

        # No route assignment → no Phase 1 work for this train.
        if route is None or not getattr(route, "is_active", False):
            return

        if train_id not in route.assigned_train_ids:
            route.assign_train(train_id)
        exec_state = self._ensure_run_for(train, route)

        # Terminal runs do nothing in Phase 1.
        if exec_state.current_route_status in (
            RouteRunStatus.FINISHED,
            RouteRunStatus.DEACTIVATED,
        ):
            return

        now = self._now()

        # Phase 1 kernel — step IDs trace back to design_new.md "Planning
        # Kernel" section. The `9c4f` (RouteRun reallocation) step is
        # performed earlier inside `_ensure_run_for(...)`; this method
        # picks up at `5fc7`.
        # ---------- 5fc7: route-deviation cleanup ----------------------------
        if exec_state.path_cancelled and self._deviation_cleanup_eligible(
            controller
        ):
            exec_state.path_cancelled = False

        # ---------- 70c3: dwell-complete timer check -------------------------
        # Per design_new.md line 2133: EB freezes the dwell timer (fix #8).
        if controller.ops.state == TrainState.RUNNING_AT_STATION and not controller.eb.active:
            self._dwell_complete_check(train, route, exec_state, controller, now)

        # ---------- c481: helper lookup via _find_next_task_target -----------
        projection = self._find_next_task_target(train_id)
        if projection is None:
            # Terminal walk for ONE_OFF; finalize the run directly.
            self.finish_route_run(train_id)
            return

        # If next task is a dwell, skip directly to 3a37 (dwell-entry).
        if projection.task_kind != "dwell":
            # ---------- a5c6: covered-task index advance ---------------------
            self._covered_task_index_advance(
                train, route, exec_state, controller, now
            )
            # Re-resolve after a possible cursor change.
            projection = self._find_next_task_target(train_id)
            if projection is None:
                self.finish_route_run(train_id)
                return

        # ---------- 3a37: dwell-entry transition -----------------------------
        if projection.task_kind == "dwell":
            self._dwell_entry_step(
                train, route, exec_state, controller, projection.walk_position
            )
            # Phase 1 returns for this tick once dwell-entry decides — no path
            # creation while dwell is pending or active.
            return

        # ---------- 592a: re-resolve next target (post cursor changes) ------
        projection = self._find_next_task_target(train_id)
        if projection is None or projection.task_kind == "dwell":
            return

        # ---------- 66aa: authority acquisition (create_path / extend_path) -
        if not self._eligible_for_authority_acquisition(controller, exec_state):
            return

        # Per design_new.md (fix #14, part a): wrap authority acquisition in a
        # retry loop that accumulates previously-failed resolved_target values
        # into an `exclude` set and re-calls `_find_next_task_target(train_id,
        # exclude=exclude)` so StationTarget tasks can try alternate platforms
        # within the same tick. Cap iterations at the number of platforms in
        # the resolved station group (or a conservative maximum of 8) to
        # prevent an infinite loop when every candidate is blocked.
        exclude: List[ResolvedRouteTarget] = []
        _max_iterations = 8
        # Track the walk-position of the first attempted target so we can
        # apply the action_on_waypoint_unreachable policy below if the whole
        # retry loop exhausts without acquiring authority.
        failed_walk_position: Optional[int] = projection.walk_position
        any_success = False
        for _attempt in range(_max_iterations):
            if projection is None or projection.task_kind == "dwell":
                break
            # Per design_new.md "RouteTaskRun.last_error canonical writer":
            # a sentinel projection (resolved_target=None) means
            # `route_target_resolver` filtered out every candidate (manual /
            # disabled flag, no platforms, etc.). Write last_error from the
            # resolver's failure_reason BEFORE breaking the loop, so a SKIP
            # action_on_waypoint_unreachable doesn't advance the cursor past
            # the task with an empty last_error.
            if projection.resolved_target is None:
                if projection.failure_reason and 0 <= projection.walk_position < len(exec_state.task_runs):
                    now = self._now()
                    err = ErrorRecord(text=projection.failure_reason, time=now)
                    exec_state.task_runs[projection.walk_position].last_error = err
                    exec_state.last_error = err
                break  # sentinel projection — no more candidates this tick
            # Phase 1 step `66aa`: dispatch via PathDispatcher.create_path or
            # extend_path (v1 signature). Both methods take train_id +
            # requested_destination_block_id + requested_destination_direction +
            # acquisition_mode and return a PathAcquisitionResult.
            success = self._authority_acquisition_step(
                train, route, exec_state, controller, projection
            )
            if success:
                any_success = True
                break
            # Acquisition failed for this resolved_target. Add to exclude
            # and try the next candidate platform (fix #14a).
            exclude.append(projection.resolved_target)
            projection = self._find_next_task_target(train_id, exclude=exclude)
            if projection is None:
                # Terminal walk — no later task on the directed walk.
                break

        # Per design_new.md "Action on waypoint unreachable": when every
        # same-tick candidate failed without creating or growing live
        # authority, consult the effective policy for this run. WAIT
        # (default) preserves the existing v1 behaviour — leave the task
        # armed, retry on the next eligible tick. SKIP advances the cursor
        # past the failed task so a later Phase 1 tries the following one.
        if (
            not any_success
            and failed_walk_position is not None
            and 0 <= failed_walk_position < len(exec_state.task_runs)
            and exec_state.task_runs[failed_walk_position].finished_at is None
        ):
            try:
                has_active_path = (
                    controller.path_projection.received_path_plan is not None
                )
            except Exception:
                has_active_path = False
            if not has_active_path:
                action = exec_state.effective_action_on_waypoint_unreachable(
                    route=route, route_executor=self
                )
                if action == WaypointUnreachableAction.SKIP:
                    now = self._now()
                    # Per design_new.md "RouteTaskRun.last_error canonical
                    # writer": SKIP must not advance past a task with an
                    # empty last_error. Resolver-side and acquisition-side
                    # failures above should have already written last_error;
                    # write a fallback record here only when neither did.
                    failed_row = exec_state.task_runs[failed_walk_position]
                    if failed_row.last_error is None:
                        fallback_err = ErrorRecord(
                            text="task target unreachable: no candidates",
                            time=now,
                        )
                        failed_row.last_error = fallback_err
                        exec_state.last_error = fallback_err
                    exec_state.task_runs[failed_walk_position].finished_at = now
                    # Bookkeeping per the canonical append rule.
                    try:
                        exec_state._append_next_walk_task(route)
                    except Exception:
                        pass

    # ----- Phase 1 sub-steps ------------------------------------------------
    def _deviation_cleanup_eligible(self, controller: TrainController) -> bool:
        """Predicate for the route-deviation cleanup latch consumer.

        Per design_new.md (line 277), this gate is `controller.is_recovery_clean`.
        """
        return bool(getattr(controller, "is_recovery_clean", False))

    def is_clean_for_create(self, train_id: str) -> bool:
        """Canonical Phase-1 create-branch gate per design_new.md.

        Per the Planning Kernel section: true when the controller has reached
        recovery (``is_recovery_clean``), no pending admin/system cancellation
        latch is set (``PATH_CANCEL_PENDING``, ``ROUTE_ABORT_PENDING``,
        ``ROUTE_DEACTIVATE_PENDING`` all clear), and the route-run's deviation
        latch (``path_cancelled``) is clear. Phase 1 invokes ``create_path``
        only when this predicate is true.
        """
        controller = self._controller_by_id(train_id)
        if controller is None:
            return False
        if not controller.is_recovery_clean:
            return False
        if controller.active_conditions & _PENDING_CANCEL_CONDITIONS:
            return False
        exec_state = self.execution_state_for(train_id)
        if exec_state is not None and exec_state.path_cancelled:
            return False
        return True

    def _eligible_for_authority_acquisition(
        self, controller: TrainController, exec_state: RouteExecutionState
    ) -> bool:
        """Phase-1 eligibility precondition for create_path / extend_path.

        Per design_new.md (Planning Kernel section): the dispatch is
        two-branch. Extend is allowed when an active path exists AND none of
        the three pending admin/system cancellation latches are set (so
        Phase 1 does not extend a path Phase 3 is about to cancel). Create
        delegates to ``is_clean_for_create``.
        """
        if controller.active_conditions & _PENDING_CANCEL_CONDITIONS:
            return False
        try:
            has_active_path = controller.path_projection.received_path_plan is not None
        except Exception:
            has_active_path = False
        if has_active_path:
            return True
        # Create branch — full clean-for-create predicate.
        train_id = getattr(exec_state, "train_id", None)
        if train_id is None:
            return False
        return self.is_clean_for_create(train_id)

    def _dwell_complete_check(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        now: float,
    ) -> None:
        """Phase 1 step 2: finish a dwell whose timer has elapsed.

        Per design_new.md line 2133: if EB is active, the dwell timer is
        frozen — return immediately (fix #8).

        Per design_new.md, on completion this step writes
        `task_runs[task_index].finished_at = now`, advances
        `current_waypoint_index`, and calls `controller.finish_dwell()`.
        The next task's `started_at` is derived (read via
        `RouteExecutionState.task_started_at(i)`) — it just falls out of the
        finished_at write per the canonical rule, so there is no separate
        write step.
        """
        # EB freezes dwell timer (design_new.md line 2133, fix #8).
        if controller.eb.active:
            return

        # Per design, `current_waypoint_index` is the largest finished
        # walk-position; the dwell task currently in flight is the next
        # planning index.
        task_index = exec_state.next_planning_index
        task = route.get_task_at(task_index) if hasattr(route, "get_task_at") else None
        if task is None or task.task_kind != "dwell":
            return

        # Lazy-allocate task_runs row.
        self._sync_task_runs(route, exec_state)
        # Locate the in-flight row for this `task_index`. For initial
        # walk-positions the list index matches the task_index, but after
        # CIRCULAR / ROUND_TRIP wraparound multiple list positions can
        # share the same task_index — the in-flight one is the latest
        # entry whose finished_at is still None. Walk from the trailing
        # entry backward and pick the first match.
        row = None
        row_pos = -1
        for pos in range(len(exec_state.task_runs) - 1, -1, -1):
            r = exec_state.task_runs[pos]
            if r.task_index == task_index and r.finished_at is None:
                row = r
                row_pos = pos
                break
        if row is None:
            return
        # `started_at` for this walk-position is the previous
        # task_runs entry's finished_at (or RouteRun.started_at for the
        # first entry). Use the row's list position, not its task_index,
        # so wraparound entries anchor on the correct predecessor.
        if row_pos == 0:
            dwell_started_at = exec_state.started_at
        else:
            dwell_started_at = exec_state.task_runs[row_pos - 1].finished_at

        if not task.should_depart(now, dwell_started_at):
            return

        # Mark the dwell row finished and advance.
        row.finished_at = now
        exec_state._append_next_walk_task(route)

        # Advance the per-run cursor based on the route mode.
        self._advance_to_next_task(route, exec_state)

        # Unpack (success, reason) tuple per train_controller agent signature.
        _fd_success, _fd_reason = controller.finish_dwell()

    def _covered_task_index_advance(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        now: float,
    ) -> None:
        """Phase 1 step 4: skip already-covered `waypoint` and same-block `stop` tasks.

        For each next non-finished task:
        - `waypoint`: satisfied when the resolved target block is already inside
          the current span.
        - `stop`: satisfied when the train is stopped with no active path and
          the resolved target block equals the train's current block (same-block
          case — the train is already at the destination, so no movement is
          needed). This handles routes created while the train is already at the
          destination platform.
        - `dwell`: never satisfied here; must go through the dwell-entry step.

        Writes `finished_at` when satisfied (triggers `_append_next_walk_task`
        for CIRCULAR/ROUND_TRIP and advances the derived `current_waypoint_index`).
        Stops advancing when the next task is not satisfiable or is a `dwell`.
        """
        self._sync_task_runs(route, exec_state)
        task_count = route.get_task_count()
        # Per design, the loop iterates the next un-finished planning index;
        # `current_waypoint_index` is the largest finished walk-position
        # (or None) and is not the right cursor here.
        while exec_state.next_planning_index < task_count:
            idx = exec_state.next_planning_index
            task = route.get_task_at(idx)
            if task is None:
                break

            if task.task_kind == "dwell":
                break

            target_block = self._resolve_task_target_block(task, train=train)
            if not target_block:
                break

            if task.task_kind == "stop":
                # A stop is immediately satisfied only when the train is already
                # stopped at the destination block with no active path. The
                # pathfinder returns 0 candidates for same-block paths, so
                # create_path would never succeed in this case.
                if not controller.is_stopped():
                    break
                if self._get_active_path_id(train.train_id) is not None:
                    break
                if not self._is_train_at_block(train, target_block):
                    break
            else:
                # waypoint coverage: check current span or platform tile.
                if not self._is_train_at_block(train, target_block):
                    if not self._is_train_at_platform_for_task(train, task):
                        break

            # Write finished_at on the matching task_runs row. The derived
            # `current_waypoint_index` then naturally advances on the next
            # loop iteration; `_append_next_walk_task` appends the next
            # walk-position entry for CIRCULAR/ROUND_TRIP routes.
            #
            # For CIRCULAR / ROUND_TRIP routes, multiple `task_runs` list
            # positions can share the same `task_index` value once
            # `_append_next_walk_task` has wrapped around. Walk from the
            # trailing entry backward and pick the first row matching
            # this `idx` (task_index) with finished_at still None.
            row = None
            for pos in range(len(exec_state.task_runs) - 1, -1, -1):
                r = exec_state.task_runs[pos]
                if r.task_index == idx and r.finished_at is None:
                    row = r
                    break
            if row is None:
                break
            row.finished_at = now
            exec_state._append_next_walk_task(route)
            self._advance_to_next_task(route, exec_state)

    def _dwell_entry_step(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        task_index: int,
    ) -> None:
        """Phase 1 step 5: enter dwell when the next task is `dwell`.

        Preconditions: predecessor task finished, controller is in READY,
        `received_path_plan is None`. The dwell row's `started_at` is already
        populated by the predecessor's `finished_at` writer per the canonical
        first-write rule; this step does not write `started_at`.
        """
        if controller.ops.state == TrainState.RUNNING_AT_STATION:
            return
        # Per design_new.md, the dwell-entry gate is `is_recovery_clean`.
        if not getattr(controller, "is_recovery_clean", False):
            return

        # Unpack (success, reason) tuple per train_controller agent signature.
        _esd_success, _esd_reason = controller.enter_station_dwell()

    def _authority_acquisition_step(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        projection: ResolvedNextTaskProjection,
    ) -> bool:
        """Phase 1 step 7: create or extend authority for the resolved target.

        Returns True when authority was successfully acquired (create/extend
        succeeded and depart() was called), False otherwise. The return value
        is used by the fix #14 retry loop in tick_train_phase1.

        On a successful create/extend, write
        ``task_runs[projection.walk_position].resolved_target`` (Phase-1-only
        writer) and call ``controller.depart()``. Per design_new.md
        line 1078, departure is not a route-level policy in v1: Phase 1
        always calls ``depart()`` after a successful create/extend.
        There is no ``departure_requested`` latch.
        """
        if self.path_dispatcher is None:
            return False
        if projection.resolved_target is None:
            return False

        target_block = projection.resolved_target.requested_destination_block_id
        target_dir = projection.resolved_target.requested_destination_direction

        # Decide create vs extend by active authority presence.
        active_path_id = self._get_active_path_id(train.train_id)

        # Resolve acquisition_mode: per-run override (if present) wins over
        # shared Route.acquisition_mode per design_new.md "RouteRun overrides"
        # canonical home.
        acquisition_mode = exec_state.effective_acquisition_mode(
            route=route, route_executor=self
        )

        success = False
        result = None
        try:
            if active_path_id is not None:
                result = self.path_dispatcher.extend_path(
                    path_id=active_path_id,
                    requested_destination_block_id=target_block,
                    requested_destination_direction=target_dir,
                    acquisition_mode=acquisition_mode,
                )
                success = bool(getattr(result, "success", False))
            else:
                result = self.path_dispatcher.create_path(
                    train_id=train.train_id,
                    requested_destination_block_id=target_block,
                    requested_destination_direction=target_dir,
                    acquisition_mode=acquisition_mode,
                )
                success = bool(getattr(result, "success", False))
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "authority acquisition raised for %s: %s", train.train_id, exc
            )
            success = False

        if not success:
            # Auto-recover from extend_path's INVALID_REVERSE: the new
            # destination needs the train to turn around at the current
            # authority-end, which extend_path cannot grant. Latch a
            # cancellation so Phase 3 tears the stale path down once the train
            # stops; the next eligible Phase 1 will see no active path and
            # acquire via create_path from the train's current span instead.
            # Per design_new.md latch-table rules this is the same pattern
            # Phase 1 uses for any "live path must be replaced" condition:
            # write the latch, stop planning this tick, let Phase 3 commit.
            fkind = getattr(result, "failure_kind", None)
            if (
                active_path_id is not None
                and fkind == PathAcquisitionFailureKind.INVALID_REVERSE
            ):
                cancel_reason = (
                    getattr(result, "reason", None)
                    or "extend would reverse at authority end"
                )
                controller.route_hooks.phase_latches.set_path_cancel_request(
                    initiator="auto_recreate_for_reverse",
                    reason=(
                        f"{cancel_reason}; auto-cancelling so the next "
                        "acquisition uses create_path from the train's "
                        "current span"
                    ),
                )
                return False

            # Per design_new.md (fix #17): write last_error on failed
            # create_path/extend_path so operators can inspect the reason.
            now = self._now()
            reason_text = (
                getattr(result, "reason", None) or "create_path/extend_path failed"
            )
            err = ErrorRecord(text=reason_text, time=now)
            row_idx = projection.walk_position
            self._sync_task_runs(route, exec_state)
            if 0 <= row_idx < len(exec_state.task_runs):
                exec_state.task_runs[row_idx].last_error = err
            exec_state.last_error = err
            return False

        # Authority acquisition succeeded for this task. Clear any prior error
        # on the run and on the just-acquired task row so the live "current
        # path error" projection (train-panel badge / Quick Route no-path
        # toast, derived from `RouteRun.last_error` in `TrainController.to_dict`)
        # reflects only a CURRENT inability to route, not a stale earlier
        # failure that has since been resolved.
        if exec_state.last_error is not None:
            exec_state.last_error = None
        success_row_idx = projection.walk_position
        if 0 <= success_row_idx < len(exec_state.task_runs):
            exec_state.task_runs[success_row_idx].last_error = None

        # Fix (deviation guard): insert any start-block tiles that lie between
        # the train's current head tile and the path segment's first tile into
        # the first resource entry's path_tiles, in the correct traversal
        # order.
        #
        # The pathfinder builds tile sequences starting from the block's
        # canonical exit tile (one end of the block).  For both forward and
        # reverse traversals, the segment may omit tiles that lie between the
        # train's head and the segment's first listed tile.  Those omitted
        # tiles are absent from tile_xy_to_seq_index, so the deviation check
        # in TrainController.on_physics_span_update flags them as
        # out_of_sequence and immediately cancels the freshly-created path.
        #
        # Strategy: locate the train's head tile and the segment's first tile
        # in Block.ordered_tiles, then insert tiles in the traversal direction
        # (head → first-seg-tile) so ATC direction logic is not confused.
        #
        # Applies only to create_path (active_path_id is None); extend_path
        # continues an existing path whose tiles already cover the current
        # position.
        if active_path_id is None and result is not None:
            created_path_id = getattr(result, "path_id", None)
            if created_path_id and self.path_dispatcher is not None:
                _created_path = self.path_dispatcher.paths.get(created_path_id)
                if (
                    _created_path is not None
                    and _created_path.resource_sequence
                ):
                    _first_entry = _created_path.resource_sequence[0]
                    if (
                        _first_entry.resource_type == "block"
                        and _first_entry.path_tiles
                    ):
                        _covered_xys = {
                            (t.x, t.y) for t in _first_entry.path_tiles
                        }
                        _ctrl = self._controller_by_id(train.train_id)
                        _head_tile = (
                            _ctrl._get_head_tile() if _ctrl is not None else None
                        )
                        if (
                            _head_tile is not None
                            and _head_tile not in _covered_xys
                        ):
                            _block_obj = self.network.blocks.get(
                                _first_entry.resource_id
                            )
                            _ordered_raw = (
                                getattr(_block_obj, "ordered_tiles", []) or []
                                if _block_obj is not None
                                else []
                            )

                            def _xy(bt) -> Tuple[int, int]:
                                if hasattr(bt, "x"):
                                    return (bt.x, bt.y)
                                return (int(bt[0]), int(bt[1]))

                            _ordered_xys: List[Tuple[int, int]] = [
                                _xy(bt) for bt in _ordered_raw
                            ]
                            _first_seg_xy = (
                                _first_entry.path_tiles[0].x,
                                _first_entry.path_tiles[0].y,
                            )
                            try:
                                _h_idx = _ordered_xys.index(_head_tile)
                                _s_idx = _ordered_xys.index(_first_seg_xy)
                            except ValueError:
                                _h_idx = None
                                _s_idx = None

                            from path import PathTileEntry as _PathTileEntry
                            _insert: List[_PathTileEntry] = []
                            if _h_idx is not None and _s_idx is not None:
                                if _h_idx < _s_idx:
                                    # Forward traversal: insert tiles in
                                    # ascending index order from head up to
                                    # (not including) the first-seg tile.
                                    for _i in range(_h_idx, _s_idx):
                                        _bxy = _ordered_xys[_i]
                                        if _bxy not in _covered_xys:
                                            _insert.append(
                                                _PathTileEntry(
                                                    x=_bxy[0],
                                                    y=_bxy[1],
                                                    entry_edge="",
                                                    exit_edge="",
                                                    resource_sequence_index=(
                                                        _first_entry.sequence_index
                                                    ),
                                                )
                                            )
                                            _covered_xys.add(_bxy)
                                elif _h_idx > _s_idx:
                                    # Reverse traversal: insert tiles in
                                    # descending index order from head down to
                                    # (not including) the first-seg tile.
                                    for _i in range(_h_idx, _s_idx, -1):
                                        _bxy = _ordered_xys[_i]
                                        if _bxy not in _covered_xys:
                                            _insert.append(
                                                _PathTileEntry(
                                                    x=_bxy[0],
                                                    y=_bxy[1],
                                                    entry_edge="",
                                                    exit_edge="",
                                                    resource_sequence_index=(
                                                        _first_entry.sequence_index
                                                    ),
                                                )
                                            )
                                            _covered_xys.add(_bxy)
                            if _insert:
                                _first_entry.path_tiles[0:0] = _insert
                                logger.debug(
                                    "[RouteExecutor] inserted %d start-block "
                                    "tile(s) %s before seg-first %s in path %s"
                                    " entry %s (deviation guard)",
                                    len(_insert),
                                    [(_t.x, _t.y) for _t in _insert],
                                    _first_seg_xy,
                                    created_path_id,
                                    _first_entry.resource_id,
                                )

        # Successful create/extend: write
        # task_runs[walk_position].resolved_target.
        target_idx = projection.walk_position
        self._sync_task_runs(route, exec_state)
        if 0 <= target_idx < len(exec_state.task_runs):
            exec_state.task_runs[target_idx].resolved_target = ResolvedRouteTarget(
                requested_destination_block_id=target_block,
                requested_destination_direction=target_dir,
            )

        # Update direction if projection says so. The canonical store is
        # the literal string; no signed-int translation is needed here.
        if projection.direction in ("forward", "reverse"):
            exec_state.direction = projection.direction

        # Per design_new.md line 1078: Phase 1 always calls depart() after a
        # successful create/extend.
        depart = getattr(controller, "depart", None)
        if callable(depart):
            try:
                # Unpack (success, reason) tuple per train_controller agent.
                _dep_result = depart()
                if isinstance(_dep_result, tuple):
                    pass  # (success, reason) — no action needed on success
            except Exception as exc:
                # Phase 1 retries depart() on a later tick; surface the
                # raised reason via NotificationCenter so operators see
                # non-acceptance without aborting the planning kernel.
                train_id = getattr(train, "train_id", "?")
                sc = self.simulation_controller
                nc = (
                    getattr(sc, "notification_center", None)
                    if sc is not None
                    else None
                )
                emitted = False
                if nc is not None:
                    try:
                        from notification_center import Severity as _Severity

                        nc.emit(
                            source="RouteExecutor",
                            kind="depart_failed",
                            severity=_Severity.WARNING,
                            event_time=time.time(),
                            train_id=train_id,
                            payload={"reason": str(exc), "will_retry": True},
                        )
                        emitted = True
                    except Exception:
                        pass
                if not emitted:
                    logger.warning(
                        "depart() raised for train %s; will retry on a later "
                        "tick (NotificationCenter unavailable)",
                        train_id,
                        exc_info=True,
                    )
        return True

    def finish_route_run(self, train_id: str) -> None:
        """Phase 1 terminal-walk handler.

        Per design_new.md ``current_route_status`` write contract:
          * sets ``RouteRun.current_route_status = FINISHED``
          * calls ``route.unassign_train(train_id)``
          * archives the run as a historical FINISHED row (``execution_states``
            slot frees up so a later route assignment can populate a fresh run)
          * for ``RouteMode.ONE_OFF`` routes with no remaining assigned trains,
            also calls ``route.deactivate()``
        """
        exec_state = self.execution_state_for(train_id)
        if exec_state is None:
            return

        route_id = exec_state.route_id
        route = self.route_registry.get(route_id)

        exec_state.current_route_status = RouteRunStatus.FINISHED

        if route is not None:
            route.unassign_train(train_id)

        # Move the now-FINISHED run to the historical archive so the live slot
        # is free for a later assignment of the same train.
        self._archive_run(exec_state)

        if (
            route is not None
            and getattr(route, "mode", None) == RouteMode.ONE_OFF
            and not getattr(route, "assigned_train_ids", [])
        ):
            simulation_controller = getattr(self, "simulation_controller", None)
            route.deactivate(simulation_controller)

    def _archive_run(self, exec_state: RouteExecutionState) -> None:
        """Move a now-terminal `RouteRun` to `_historical_runs`.

        After this call, `execution_states.get(exec_state.train_id)` returns
        the next live run (or `None`) rather than this terminal one.
        """
        train_id = exec_state.train_id
        if self.execution_states.get(train_id) is exec_state:
            del self.execution_states[train_id]
        self._historical_runs.append(exec_state)

    # ------------------------------------------------------------------ Phase 3
    def handle_train_advanced(
        self,
        train_id: str,
        path_id: str,
        resource_event: ResourceEvent,
    ) -> None:
        """Phase-3 head-advance callback. Consumes at most one task per call.

        Per design_new.md:
          * Switches are no-ops on the route side.
          * Only `task_kind == "waypoint"` tasks may be consumed by head entry;
            `stop` tasks are served only by dispatcher-committed completion.
          * The reported block must match the next non-finished task's
            resolved-target block AND direction.
        """
        if resource_event.resource_type != "block":
            return
        train, route, exec_state = self._resolve_run_route(train_id)
        if train is None or route is None or exec_state is None:
            return

        projection = self._find_next_task_target(train_id)
        if projection is None:
            return
        if projection.task_kind != "waypoint":
            return
        if projection.resolved_target is None:
            return
        if (
            projection.resolved_target.requested_destination_block_id
            != resource_event.resource_id
        ):
            return
        # Match on block AND direction (design_new.md:465). Both sides are
        # block-relative "forward"/"reverse": the resolved target carries the
        # requested block-relative direction, and `traversal_direction` is the
        # block-relative direction the dispatcher computed for this entry. Skip
        # the direction check only when the dispatcher could not resolve a
        # direction (None) — falling back to block-id-only match rather than
        # consuming on a wrong-direction pass when direction IS known.
        if (
            resource_event.traversal_direction is not None
            and projection.resolved_target.requested_destination_direction
            != resource_event.traversal_direction
        ):
            return

        # Consume that one task: record finished_at and advance the cursor.
        now = self._now()
        idx = projection.walk_position
        self._sync_task_runs(route, exec_state)
        if 0 <= idx < len(exec_state.task_runs):
            row = exec_state.task_runs[idx]
            if row.finished_at is None:
                row.finished_at = now
                exec_state._append_next_walk_task(route)

        self._advance_to_next_task(route, exec_state)

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        initiator: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Phase-3 cancellation callback.

        Per design_new.md, every cancellation kind shares the same body. The
        callback is bookkeeping only: it must not choose a fresh target or
        create fresh authority. `initiator` is the writer-supplied free-form
        string (``"eb"``, ``"operator_cancelled"``, ``"power_off"``,
        ``"deviation"``, ``"revoked"``, …); routing/planning logic must not
        branch on it.
        """
        exec_state = self.execution_states.get(train_id)
        if exec_state is None:
            return
        # Per design_new.md: deviation is purely the `path_cancelled` latch
        # plus the derived `display_state` projection. The stored
        # `current_route_status.state` does not transition to DEVIATED.
        exec_state.path_cancelled = True
        # Per design_new.md (fix #17): record cancellation reason on last_error.
        now = self._now()
        exec_state.last_error = ErrorRecord(
            text=f"cancelled: {initiator}" if initiator else "cancelled",
            time=now,
        )

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        authority_end_block_id: Optional[str] = None,
        requested_destination_direction: Optional[str] = None,
        event_time: Optional[float] = None,
    ) -> bool:
        """Phase-3 completion callback.

        Per design_new.md "Active-path completion procedure":
          * walk task_runs from the next non-finished list position
          * pick first non-finished `task_kind == "stop"` whose stored
            resolved_target matches authority_end_block_id +
            requested_destination_direction
          * record finished_at, advance current_waypoint_index, write next
            task's started_at if empty
          * clear route-owned active-path correlation
        """
        if event_time is None:
            event_time = self._now()

        train, route, exec_state = self._resolve_run_route(train_id)
        if train is None or route is None or exec_state is None:
            return False

        # Walk `task_runs` by list position starting from the next
        # non-finished entry. Per design_new.md line 1104 + line 1267,
        # `walk_position` (list index) and `task_index` (Route.tasks index)
        # decouple after `CIRCULAR` / `ROUND_TRIP` wrap-around, so the
        # iteration must be over the un-finished list suffix — not over
        # `range(next_planning_index, task_count)`, which would land on
        # already-finished early rows once the walk has wrapped.
        self._sync_task_runs(route, exec_state)
        start_pos = exec_state.next_planning_walk_position
        match_pos: Optional[int] = None
        for pos in range(start_pos, len(exec_state.task_runs)):
            row = exec_state.task_runs[pos]
            task = route.get_task_at(row.task_index)
            if task is None:
                continue
            if task.task_kind != "stop":
                # waypoint or dwell — not the stop-completion match target.
                # waypoint matches happen via handle_train_advanced; dwell
                # tasks are not matched here.
                if task.task_kind == "waypoint":
                    # Skip but don't break — there may be a stop after.
                    continue
                # dwell — definitely break (must be served via finish_dwell).
                break
            # Match by resolved_target.
            target = row.resolved_target
            if target is None:
                # Per design_new.md line 1479: task_runs[i].resolved_target is
                # the authoritative match key.  Re-resolving at completion time
                # is forbidden — log a diagnostic and skip this walk-position
                # (fix #5).
                logger.warning(
                    "handle_path_completion: task_runs[%d] for train %s has "
                    "resolved_target=None; skipping (authority_end=%s). "
                    "Phase 1 must write resolved_target before path completes.",
                    pos,
                    train_id,
                    authority_end_block_id,
                )
                continue
            if (
                authority_end_block_id
                and target.requested_destination_block_id != authority_end_block_id
            ):
                continue
            if (
                requested_destination_direction
                and target.requested_destination_direction
                != requested_destination_direction
            ):
                continue
            match_pos = pos
            break

        if match_pos is None:
            # Per design_new.md line 1479: no matching stop task is a contract
            # violation; raise a fatal diagnostic (fix #6).
            sc = getattr(self, "simulation_controller", None)
            if sc is not None:
                sc.enter_failed_runtime(
                    FatalPhysicalEntryConflict(
                        resource_type="route_completion_mismatch",
                        resource_id=authority_end_block_id or "unknown",
                        incoming_train_id=train_id,
                        prior_owner_train_id=None,
                        conflict_kind=FatalConflictKind.RESERVED_FOR_OTHER_TRAIN,
                        incoming_path_id=path_id,
                    )
                )
            else:
                logger.error(
                    "handle_path_completion: no matching stop task for train %s "
                    "(authority_end=%s, dir=%s); SimulationController unavailable "
                    "so fatal diagnostic could not be raised.",
                    train_id,
                    authority_end_block_id,
                    requested_destination_direction,
                )
            return False

        # Record finished_at on the matched stop row via _enter_task_arrival
        # (fix #27: extracted helper; per design_new.md line ~1479). Both the
        # cursor and the match index are list positions in `task_runs`, not
        # `Route.tasks` indices, so this loop correctly targets the current
        # walk's rows even after CIRCULAR / ROUND_TRIP wrap-around.
        for pos in range(start_pos, match_pos + 1):
            if not (0 <= pos < len(exec_state.task_runs)):
                continue
            row = exec_state.task_runs[pos]
            if row.finished_at is None:
                self._enter_task_arrival(exec_state, route, pos, event_time)
        # Phase 3 must NOT call enter_station_dwell — that fires from the next
        # eligible Phase 1 dwell-entry step.
        return True

    def _enter_task_arrival(
        self,
        exec_state: RouteExecutionState,
        route: "Route",
        walk_position: int,
        now: float,
    ) -> None:
        """Record task arrival: write `task_runs[walk_position].finished_at = now`.

        Per design_new.md line ~1479 (`_enter_task_arrival` helper): this is the
        canonical and only writer of `task_runs[i].finished_at` for the
        stop-completion path (fix #27). Also triggers `_append_next_walk_task`
        so CIRCULAR/ROUND_TRIP routes grow the walk-position list correctly.
        """
        if not (0 <= walk_position < len(exec_state.task_runs)):
            return
        row = exec_state.task_runs[walk_position]
        if row.finished_at is not None:
            return
        row.finished_at = now
        exec_state._append_next_walk_task(route)

    # ------------------------------------------------------------------ removal
    def remove_train_route_run(self, train_id: str) -> None:
        """Tear down a `RouteRun` for `pending_route_abort` /
        `pending_power_off` / `pending_route_deactivate`.

        Per design_new.md (~line 1242): looks up the train's currently-RUNNING
        `RouteRun` (no-op if none), then performs the canonical teardown:

          1. If the controller is in `RUNNING_AT_STATION`, call
             `TrainController.finish_dwell()` to exit dwell into READY first.
          2. Call `Route.unassign_train(train_id)` on the run's `route_id`.
          3. Set `current_route_status.state = DEACTIVATED` and archive the
             row as a historical run (visible to `history_for_train(...)`).
             The derived `execution_state_for(train_id)` no longer returns it.

        The originating cause is recorded only on the `path_cancellation` and
        `command_result` notifications, not on any field of the `RouteRun`.
        """
        exec_state = self.execution_state_for(train_id)
        if exec_state is None:
            # No active run to remove; some other Phase-3 consumer already
            # ran or no run was ever assigned.
            return

        controller = self._controller_by_id(train_id)
        if (
            controller is not None
            and controller.ops.state == TrainState.RUNNING_AT_STATION
        ):
            finish_dwell = getattr(controller, "finish_dwell", None)
            if callable(finish_dwell):
                try:
                    # Unpack (success, reason) tuple per train_controller agent.
                    _fd_result = finish_dwell()
                    if isinstance(_fd_result, tuple):
                        pass  # (success, reason) — no action needed on success
                except Exception as exc:
                    sc = self.simulation_controller
                    nc = (
                        getattr(sc, "notification_center", None)
                        if sc is not None
                        else None
                    )
                    emitted = False
                    if nc is not None:
                        try:
                            from notification_center import Severity as _Severity

                            nc.emit(
                                source="RouteExecutor",
                                kind="finish_dwell_failed",
                                severity=_Severity.WARNING,
                                event_time=time.time(),
                                train_id=train_id,
                                payload={
                                    "reason": str(exc),
                                    "context": "route_run_removal",
                                },
                            )
                            emitted = True
                        except Exception:
                            pass
                    if not emitted:
                        logger.warning(
                            "finish_dwell() raised for train %s during route-run "
                            "removal (NotificationCenter unavailable)",
                            train_id,
                            exc_info=True,
                        )

        route_id = exec_state.route_id
        if route_id:
            route = self.route_registry.get(route_id)
            if route is not None:
                route.unassign_train(train_id)

        # Mark DEACTIVATED and move to the historical archive.
        exec_state.current_route_status = RouteRunStatus.DEACTIVATED
        self._archive_run(exec_state)

    # ------------------------------------------------------------------ helper
    def _find_next_task_target(
        self,
        train_id: str,
        exclude: Optional[Iterable[ResolvedRouteTarget]] = None,
    ) -> Optional[ResolvedNextTaskProjection]:
        """Forward to `RouteTargetResolver.find_next_task_target(...)`."""
        return self._get_route_target_resolver().find_next_task_target(
            train_id, exclude=exclude
        )

    def find_next_move_target(self, train_id: str) -> Optional[str]:
        """Forward to `RouteTargetResolver.find_next_move_target(...)`.

        Public because `PathDispatcher` calls it (through
        `simulation_controller.route_executor`) during best-effort truncation
        to detect the reversal-trap case.
        """
        return self._get_route_target_resolver().find_next_move_target(train_id)

    def _resolve_task_target_block(
        self,
        task: RouteTask,
        train: Optional["Train"] = None,
    ) -> Optional[str]:
        """Forward to `RouteTargetResolver.resolve_task_target_block(task)`.

        When `train` is supplied, the resolver uses it to prefer a platform
        of a `StationTarget` that this train already occupies (or any
        non-occupied platform) over a platform held by another train.
        """
        return self._get_route_target_resolver().resolve_task_target_block(
            task, train=train
        )

    def _resolve_task_direction(
        self,
        train: "Train",
        block_id: str,
        task_direction: str,
        signed_direction: int,
    ) -> str:
        """Forward to `RouteTargetResolver.resolve_task_direction(...)`.

        `signed_direction` here is the local int form computed from
        `RouteRun.direction` at the call site. The resolver helper still
        takes a signed int because it folds it into per-block traversal
        sign math; the canonical store is the literal string on RouteRun.
        """
        return self._get_route_target_resolver().resolve_task_direction(
            train, block_id, task_direction, signed_direction
        )

    def _compute_next_task_index(
        self,
        route: "Route",
        exec_state: RouteExecutionState,
        task_count: int,
    ) -> Optional[int]:
        """Forward to `RouteTargetResolver.compute_next_task_index(...)`."""
        from route_target_resolver import RouteTargetResolver

        return RouteTargetResolver.compute_next_task_index(
            route, exec_state, task_count
        )
    def _get_active_route_train_pairs(self) -> List[Tuple["Train", "Route"]]:
        """Collect active (train, route) pairs."""
        pairs: List[Tuple["Train", "Route"]] = []
        seen_pairs = set()
        routes = self.route_registry

        for route in routes.values():
            if not route.is_active:
                continue
            for train_id in route.assigned_train_ids:
                train = self.network.trains.get(train_id)
                if train:
                    pair_key = (train.train_id, route.route_id)
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        pairs.append((train, route))

        return pairs

    def _advance_to_next_task(
        self, route: "Route", exec_state: RouteExecutionState
    ) -> bool:
        """Maintain ``direction`` for ROUND_TRIP route reflections.

        The cursor itself (``current_waypoint_index``) is now derived from
        ``task_runs[*].finished_at`` (see ``RouteExecutionState`` property
        docstring). Each ``finished_at`` write triggers
        ``_append_next_walk_task(route)`` which appends the next
        ``RouteTaskRun`` for ``CIRCULAR`` / ``ROUND_TRIP`` routes — that
        append is what advances the derived cursor.

        This helper retains a single responsibility: flip
        ``RouteRun.direction`` at ROUND_TRIP boundaries so the next
        ``_append_next_walk_task`` call appends the reflected walk-position.
        Returns ``True`` if the walk can continue (more tasks ahead),
        ``False`` for a terminal ONE_OFF walk.
        """
        task_count = route.get_task_count()
        if task_count == 0:
            return False

        if route.mode == RouteMode.ONE_OFF:
            # ONE_OFF terminates when the next planning index exhausts the
            # task list. `current_waypoint_index` (last finished, may be
            # None) is not the right cursor here.
            return exec_state.next_planning_index < task_count

        if route.mode == RouteMode.CIRCULAR:
            return True

        # ROUND_TRIP — flip the canonical direction string at boundaries.
        # The derived cursor jumps to the reflected position once
        # `_append_next_walk_task` appends the next entry with the new
        # direction. Use the last-finished cursor (None before the first
        # finish; treat that as -1 for the boundary comparison below).
        cursor_opt = exec_state.current_waypoint_index
        cursor = -1 if cursor_opt is None else cursor_opt
        if cursor >= task_count - 1 and exec_state.direction == "forward":
            exec_state.direction = "reverse"
        elif cursor <= 0 and exec_state.direction == "reverse":
            exec_state.direction = "forward"

        return True

    # ------------------------------------------------------------------ network helpers
    def _is_train_at_block(self, train: "Train", block_id: str) -> bool:
        """Forward to `TrainAtTargetPredicates.is_train_at_block(...)`."""
        return self._get_at_target_predicates().is_train_at_block(train, block_id)

    def _is_train_at_platform_for_task(
        self, train: "Train", task: RouteTask
    ) -> bool:
        """Forward to `TrainAtTargetPredicates.is_train_at_platform_for_task(...)`."""
        return self._get_at_target_predicates().is_train_at_platform_for_task(
            train, task
        )

    def _is_train_at_platform(self, train: "Train", platform_id: str) -> bool:
        """Forward to `TrainAtTargetPredicates.is_train_at_platform(...)`."""
        return self._get_at_target_predicates().is_train_at_platform(
            train, platform_id
        )

    def _get_target_platform_tile(
        self, train: "Train", platform: object
    ) -> Optional[Tuple[int, int]]:
        """Forward to `TrainAtTargetPredicates.get_target_platform_tile(...)`."""
        preds = self._get_at_target_predicates()
        return preds.get_target_platform_tile(preds._facing(train), platform)

    def _get_platform(self, platform_id: str) -> Optional[object]:
        """Forward to `RouteTargetResolver.get_platform(platform_id)`."""
        return self._get_route_target_resolver().get_platform(platform_id)

    def _get_platform_block_id(self, platform_id: str) -> Optional[str]:
        """Forward to `RouteTargetResolver.platform_block_id(platform_id)`."""
        return self._get_route_target_resolver().platform_block_id(platform_id)

    def _get_current_block_id(self, train: "Train") -> Optional[str]:
        """Forward to `TrainAtTargetPredicates.current_block_id(train)`."""
        return self._get_at_target_predicates().current_block_id(train)

    def _convert_path_to_train_path(
        self, path: object
    ) -> List[Tuple[int, int, Direction]]:
        """Convert dispatcher path tiles into train path tuples.

        v1 Path exposes the ordered tile list via `Path.get_path_tiles()`
        (per design_new.md "Path field schema": exposed as a function, not
        a property). Project to `(x, y)` for `convert_tiles_to_train_path`
        which expects a coordinate sequence.
        """
        from path_utils import convert_tiles_to_train_path

        getter = getattr(path, "get_path_tiles", None)
        if callable(getter):
            path_tiles = getter()
        else:
            path_tiles = getattr(path, "path_tiles", [])
        if not path_tiles:
            return []
        # Project PathTileEntry list to (x, y) tuples.
        coords: List[Tuple[int, int]] = []
        for t in path_tiles:
            if hasattr(t, "x") and hasattr(t, "y"):
                coords.append((t.x, t.y))
            elif isinstance(t, (tuple, list)) and len(t) >= 2:
                coords.append((int(t[0]), int(t[1])))
        if len(coords) == 0:
            return []
        if len(coords) == 1:
            raise ValueError("Cannot determine train direction for single-tile path")
        return convert_tiles_to_train_path(coords)
