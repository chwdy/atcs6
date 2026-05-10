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
from typing import Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING

from enums import ControllerCondition, Direction, TrainState
from train_controller import TrainController, create_controller_for_train
from train_route import (
    RouteMode,
    RouteRunStatus,
    RouteTask,
    RouteTaskRun,
)
from runtime_payloads import (
    AcquisitionMode,
    ErrorRecord,
    PendingPathCancelRequest,
    ResolvedRouteTarget,
    ResourceEvent,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
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
    """

    walk_position: int
    task_kind: str  # "waypoint" | "stop" | "dwell"
    direction: str  # "forward" | "reverse"
    resolved_target: Optional[ResolvedRouteTarget] = None


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
    # Effective traversal direction for the current directed walk.
    # `1` means forward, `-1` means reverse. Named `signed_direction` to keep
    # it distinct from the `"forward"`/`"reverse"` string-form direction
    # carried by `ResolvedNextTaskProjection.direction` and `RouteTask.task_direction`.
    signed_direction: int = 1
    # Per-run task table, parallel to Route.tasks. Allocated when the run is
    # created (or on first use) and grown lazily as Route.tasks gains entries.
    task_runs: List[RouteTaskRun] = field(default_factory=list)
    # System-time Unix seconds when this run instance started. Written once.
    started_at: Optional[float] = None
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
    def current_waypoint_index(self) -> int:
        """Derived: index in ``Route.tasks`` of the next task on the walk.

        Per design_new.md lines 296 and 1101 the cursor "advances with the
        ``finished_at`` write" — there is no parallel stored field. The
        derived rule:

          * Walk ``task_runs`` in order; return the ``task_index`` of the
            first entry whose ``finished_at is None`` (the next un-finished
            walk position).
          * If every entry is finished (terminal walk), return the last
            entry's ``task_index``.
          * If ``task_runs`` is empty (pre-allocation edge case), return 0.

        For ``CIRCULAR`` / ``ROUND_TRIP`` routes the walk grows past
        ``len(Route.tasks)`` via ``_append_next_walk_task``; each appended
        ``RouteTaskRun`` carries its own underlying ``task_index``, so this
        property naturally tracks the wrap/reflection.
        """
        if not self.task_runs:
            return 0
        for run in self.task_runs:
            if run.finished_at is None:
                return run.task_index
        return self.task_runs[-1].task_index

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
          ``direction`` field; ``RouteRun.signed_direction`` is updated by
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
            if self.signed_direction >= 0:
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
        path_dispatcher: Optional["PathDispatcher"] = None,
        shared_controllers: Optional[Dict[str, TrainController]] = None,
    ) -> None:
        self.network = network
        self.path_dispatcher = path_dispatcher
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
        self.controllers = shared_controllers if shared_controllers is not None else {}
        self._owns_controllers = shared_controllers is None
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

    # ------------------------------------------------------------------ helpers
    def _get_controller(self, train: "Train") -> TrainController:
        """Get or create TrainController for a train."""
        if train.train_id not in self.controllers:
            self.controllers[train.train_id] = create_controller_for_train(
                train_id=train.train_id,
                network=self.network,
                path_dispatcher=self.path_dispatcher,
                route_executor=self,
            )
        return self.controllers[train.train_id]

    def _controller_by_id(self, train_id: str) -> Optional[TrainController]:
        """Look up controller by train_id without creating it."""
        return self.controllers.get(train_id)

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
        getter = getattr(self.path_dispatcher, "active_path_id_by_train", None)
        if callable(getter):
            try:
                return getter(train_id)
            except Exception:
                return None
        return None

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
            route_id = self.assigned_route_id_for(train_id)
            if route_id is None:
                return 5
            route = routes.get(route_id)
            if route is None:
                return 5
            return int(getattr(route, "priority", 5))

        ordered.sort(key=effective_priority, reverse=True)
        return ordered

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
        if controller.state == TrainState.OFF:
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
        if controller.state == TrainState.RUNNING_AT_STATION:
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

        # Phase 1 step `66aa`: dispatch via PathDispatcher.create_path or
        # extend_path (v1 signature). Both methods take train_id +
        # requested_destination_block_id + requested_destination_direction +
        # acquisition_mode and return a PathAcquisitionResult.
        self._authority_acquisition_step(
            train, route, exec_state, controller, projection
        )

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
            has_active_path = controller.received_path_plan is not None
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

        Per design_new.md, on completion this step writes
        `task_runs[task_index].finished_at = now`, advances
        `current_waypoint_index`, and calls `controller.finish_dwell()`.
        The next task's `started_at` is derived (read via
        `RouteExecutionState.task_started_at(i)`) — it just falls out of the
        finished_at write per the canonical rule, so there is no separate
        write step.
        """
        task_index = exec_state.current_waypoint_index
        task = route.get_task_at(task_index) if hasattr(route, "get_task_at") else None
        if task is None or task.task_kind != "dwell":
            return

        # Lazy-allocate task_runs row.
        self._sync_task_runs(route, exec_state)
        if task_index >= len(exec_state.task_runs):
            return
        row = exec_state.task_runs[task_index]
        dwell_started_at = exec_state.task_started_at(task_index)

        if not task.should_depart(now, dwell_started_at):
            return

        # Mark the dwell row finished and advance.
        row.finished_at = now
        exec_state._append_next_walk_task(route)

        # Advance the per-run cursor based on the route mode.
        self._advance_to_next_task(route, exec_state)

        controller.finish_dwell()

    def _covered_task_index_advance(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        now: float,
    ) -> None:
        """Phase 1 step 4: skip already-covered `waypoint` tasks.

        Walks forward through the route; for each next non-finished `waypoint`
        whose resolved target block is already inside the current span, write
        `finished_at` (which triggers `_append_next_walk_task` for
        CIRCULAR/ROUND_TRIP and advances the derived
        `current_waypoint_index`). Stops at a `stop` or `dwell` task, when
        no further task fits coverage, or when the cursor stops advancing.
        """
        self._sync_task_runs(route, exec_state)
        task_count = route.get_task_count()
        while exec_state.current_waypoint_index < task_count:
            idx = exec_state.current_waypoint_index
            task = route.get_task_at(idx)
            if task is None:
                break

            # Block coverage never finishes a stop or dwell.
            if task.task_kind in ("stop", "dwell"):
                break

            # waypoint coverage: resolve target block and check current span.
            target_block = self._resolve_task_target_block(task)
            if not target_block:
                break

            if not self._is_train_at_block(train, target_block):
                # waypoint can also be satisfied by being at the platform tile.
                if not self._is_train_at_platform_for_task(train, task):
                    break

            # Write finished_at on the matching task_runs row. The derived
            # `current_waypoint_index` then naturally advances on the next
            # loop iteration; `_append_next_walk_task` appends the next
            # walk-position entry for CIRCULAR/ROUND_TRIP routes.
            if idx >= len(exec_state.task_runs):
                break
            row = exec_state.task_runs[idx]
            if row.finished_at is not None:
                # Already finished — cursor would not advance, so guard
                # against an infinite loop by breaking explicitly.
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
        if controller.state == TrainState.RUNNING_AT_STATION:
            return
        # Per design_new.md, the dwell-entry gate is `is_recovery_clean`.
        if not getattr(controller, "is_recovery_clean", False):
            return

        controller.enter_station_dwell()

    def _authority_acquisition_step(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        projection: ResolvedNextTaskProjection,
    ) -> None:
        """Phase 1 step 7: create or extend authority for the resolved target.

        On a successful create/extend, write
        ``task_runs[projection.walk_position].resolved_target`` (Phase-1-only
        writer) and call ``controller.depart()``. Per design_new.md
        line 1078, departure is not a route-level policy in v1: Phase 1
        always calls ``depart()`` after a successful create/extend.
        There is no ``departure_requested`` latch.
        """
        if self.path_dispatcher is None:
            return
        if projection.resolved_target is None:
            return

        target_block = projection.resolved_target.requested_destination_block_id
        target_dir = projection.resolved_target.requested_destination_direction

        # Decide create vs extend by active authority presence.
        active_path_id = self._get_active_path_id(train.train_id)

        # Resolve acquisition_mode for the route.
        route_mode_raw = getattr(route, "acquisition_mode", None)
        if isinstance(route_mode_raw, AcquisitionMode):
            acquisition_mode = route_mode_raw
        elif isinstance(route_mode_raw, str):
            try:
                acquisition_mode = AcquisitionMode(route_mode_raw)
            except ValueError:
                acquisition_mode = AcquisitionMode.BEST_EFFORT
        else:
            acquisition_mode = AcquisitionMode.BEST_EFFORT

        success = False
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
            return

        # Successful create/extend: write
        # task_runs[walk_position].resolved_target.
        target_idx = projection.walk_position
        self._sync_task_runs(route, exec_state)
        if 0 <= target_idx < len(exec_state.task_runs):
            exec_state.task_runs[target_idx].resolved_target = ResolvedRouteTarget(
                requested_destination_block_id=target_block,
                requested_destination_direction=target_dir,
            )

        # Update direction if projection says so.
        if projection.direction == "forward":
            exec_state.signed_direction = 1
        elif projection.direction == "reverse":
            exec_state.signed_direction = -1

        # Per design_new.md line 1078: Phase 1 always calls depart() after a
        # successful create/extend.
        depart = getattr(controller, "depart", None)
        if callable(depart):
            try:
                depart()
            except Exception:
                # Phase 1 retries depart() on a later tick; logging the
                # raised reason keeps non-acceptance visible without
                # aborting the planning kernel.
                logger.warning(
                    "depart() raised for train %s; will retry on a later tick",
                    getattr(train, "train_id", "?"),
                    exc_info=True,
                )

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
        if (
            resource_event.exit_edge is not None
            and projection.resolved_target.requested_destination_direction
            != resource_event.exit_edge
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
          * walk task_runs from current_waypoint_index + 1
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

        # Walk task_runs from current_waypoint_index + 1 looking for a stop.
        self._sync_task_runs(route, exec_state)
        task_count = route.get_task_count()
        match_index: Optional[int] = None
        for i in range(exec_state.current_waypoint_index + 1, task_count):
            task = route.get_task_at(i)
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
            row = exec_state.task_runs[i]
            target = row.resolved_target
            if target is None:
                # Fall back to live-resolution match.
                target_block = self._resolve_task_target_block(task)
                if not target_block:
                    continue
                if authority_end_block_id and target_block != authority_end_block_id:
                    continue
                match_index = i
                break
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
            match_index = i
            break

        if match_index is None:
            return False

        # Record finished_at on the matched stop row plus any intermediate
        # task_runs rows so the derived `current_waypoint_index` advances past
        # the matched stop. (Per design_new.md the cursor is derived; we move
        # it by writing finished_at, never by direct assignment.)
        cursor = exec_state.current_waypoint_index
        for i in range(cursor, match_index + 1):
            if not (0 <= i < len(exec_state.task_runs)):
                continue
            row = exec_state.task_runs[i]
            if row.finished_at is None:
                row.finished_at = event_time
                exec_state._append_next_walk_task(route)
        # Phase 3 must NOT call enter_station_dwell — that fires from the next
        # eligible Phase 1 dwell-entry step.
        return True

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
            and controller.state == TrainState.RUNNING_AT_STATION
        ):
            finish_dwell = getattr(controller, "finish_dwell", None)
            if callable(finish_dwell):
                try:
                    finish_dwell()
                except Exception:
                    logger.warning(
                        "finish_dwell() raised for train %s during route-run "
                        "removal",
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

    def _resolve_task_target_block(self, task: RouteTask) -> Optional[str]:
        """Forward to `RouteTargetResolver.resolve_task_target_block(task)`."""
        return self._get_route_target_resolver().resolve_task_target_block(task)

    def _resolve_task_direction(
        self,
        train: "Train",
        block_id: str,
        task_direction: str,
        signed_direction: int,
    ) -> str:
        """Forward to `RouteTargetResolver.resolve_task_direction(...)`."""
        from route_target_resolver import RouteTargetResolver

        return RouteTargetResolver.resolve_task_direction(
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
        """Maintain ``signed_direction`` for ROUND_TRIP route reflections.

        The cursor itself (``current_waypoint_index``) is now derived from
        ``task_runs[*].finished_at`` (see ``RouteExecutionState`` property
        docstring). Each ``finished_at`` write triggers
        ``_append_next_walk_task(route)`` which appends the next
        ``RouteTaskRun`` for ``CIRCULAR`` / ``ROUND_TRIP`` routes — that
        append is what advances the derived cursor.

        This helper retains a single responsibility: flip ``signed_direction``
        at ROUND_TRIP boundaries so the next ``_append_next_walk_task`` call
        appends the reflected walk-position. Returns ``True`` if the walk
        can continue (more tasks ahead), ``False`` for a terminal ONE_OFF
        walk.
        """
        task_count = route.get_task_count()
        if task_count == 0:
            return False

        if route.mode == RouteMode.ONE_OFF:
            return exec_state.current_waypoint_index < task_count

        if route.mode == RouteMode.CIRCULAR:
            return True

        # ROUND_TRIP — flip signed_direction at boundaries. The derived
        # cursor jumps to the reflected position once `_append_next_walk_task`
        # appends the next entry with the new direction.
        cursor = exec_state.current_waypoint_index
        if cursor >= task_count - 1 and exec_state.signed_direction > 0:
            exec_state.signed_direction = -1
        elif cursor <= 0 and exec_state.signed_direction < 0:
            exec_state.signed_direction = 1

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
        from train_at_target_predicates import TrainAtTargetPredicates

        return TrainAtTargetPredicates.get_target_platform_tile(train, platform)

    def _get_target_station_tile(
        self, train: "Train", station: object
    ) -> Optional[Tuple[int, int]]:
        """Forward to `TrainAtTargetPredicates.get_target_station_tile(...)`."""
        from train_at_target_predicates import TrainAtTargetPredicates

        return TrainAtTargetPredicates.get_target_station_tile(train, station)

    def _is_train_at_station(self, train: "Train", station_id: str) -> bool:
        """Forward to `TrainAtTargetPredicates.is_train_at_station(...)`."""
        return self._get_at_target_predicates().is_train_at_station(
            train, station_id
        )

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

        v1 Path stores `path_tiles` as `List[PathTileEntry]` (not a list of
        `(x, y)` tuples). Project to `(x, y)` for `convert_tiles_to_train_path`
        which expects a coordinate sequence.
        """
        from path_utils import convert_tiles_to_train_path

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
