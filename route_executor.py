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
that wants to cancel a live path latches `pending_path_cancel_request` on the
controller mailbox and waits for the canonical Phase 3 cancel→callback→cleanup
flow.
"""

from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING

from enums import Direction, TrainState, PlatformSide
from train_controller import TrainController, create_controller_for_train
from train_route import (
    RouteMode,
    RouteRunStatus,
    RouteTask,
    RouteTaskRun,
)
from runtime_payloads import (
    AcquisitionMode,
    PathCancellationKind,
    PendingPathCancelRequest,
    ResolvedRouteTarget,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from train import Train
    from train_route import Route

logger = logging.getLogger(__name__)


# =============================================================================
# Resolved-target schemas
# =============================================================================
# `ResolvedRouteTarget` is now imported from runtime_payloads (S5 migration).


@dataclass
class ResolvedNextTaskProjection:
    """Projection returned by `_find_next_task_target(train_id)`.

    Read-only result. Phase 1's authority-acquisition step is the only writer
    of `task_runs[i].resolved_target`; this helper itself never writes that
    field.
    """

    task_index: int
    task_kind: str  # "waypoint" | "stop" | "dwell"
    direction: str  # "forward" | "reverse"
    resolved_target: Optional[ResolvedRouteTarget] = None


# =============================================================================
# RouteExecutionState (a.k.a. RouteRun in design_new.md)
# =============================================================================


@dataclass
class RouteExecutionState:
    """Per-train route run state.

    The current implementation keeps the legacy class name
    `RouteExecutionState`; the design doc names the concept `RouteRun`. They
    are the same thing.

    Per design_new.md "Route-run state" table.
    """

    train_id: str
    route_id: str
    # Physical-progress cursor: index in Route.tasks of the last task the train
    # has actually finished. Initialized to 0 in Wave 1 (legacy code base);
    # design_new.md specifies `-1` as the canonical sentinel — Wave 2 may flip
    # this once tests/UI consumers are updated.
    current_waypoint_index: int = 0
    # Planning cursor: index of the task currently armed for create/extend.
    target_waypoint_index: int = 0
    # Effective traversal direction for the current directed walk.
    # Stored as a signed int for legacy compatibility; design_new.md uses
    # "forward" / "reverse". `1` means forward, `-1` means reverse.
    direction: int = 1
    # Latest route_version successfully reconciled into this run.
    route_version_seen: int = 1
    # Per-run task table, parallel to Route.tasks. Allocated when the run is
    # created (or on first use) and grown lazily as Route.tasks gains entries.
    task_runs: List[RouteTaskRun] = field(default_factory=list)
    # System-time Unix seconds when this run instance started. Written once.
    started_at: Optional[float] = None
    # Composite run-instance number assigned by RouteExecutor (diagnostic only).
    run_number: int = 0
    # Legacy alias for `started_at` (some callers still reference it).
    route_start_time: Optional[float] = None
    arrived_at_platform_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_platform_id: Optional[str] = None
    resolved_platform_id: Optional[str] = None
    # v1 latch consumed by Phase-1 route-deviation cleanup.
    path_cancelled: bool = False
    # Per-run diagnostic status. design_new.md specifies a `current_route_status`
    # object with `state` and `last_cancellation_kind`; for now we store the
    # state as a string and the cancellation kind as a separate field, matching
    # legacy code shape.
    route_state: str = "RUNNING"
    current_route_status: RouteRunStatus = RouteRunStatus.RUNNING
    last_cancellation_kind: Optional[PathCancellationKind] = None
    # Active-path correlation for Phase 3 callbacks.
    active_path_id: Optional[str] = None
    # Per-run error tracking.
    last_error: Optional[str] = None
    error_time: Optional[float] = None


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
        self.execution_states: Dict[str, RouteExecutionState] = {}
        self.controllers = shared_controllers if shared_controllers is not None else {}
        self._owns_controllers = shared_controllers is None
        # Per-route run-instance counter (1-based). diagnostic only.
        self._run_count_for_route: Dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    def _get_controller(self, train: "Train") -> TrainController:
        """Get or create TrainController for a train."""
        if train.train_id not in self.controllers:
            self.controllers[train.train_id] = create_controller_for_train(
                train_id=train.train_id,
                network=self.network,
            )
        return self.controllers[train.train_id]

    def _controller_by_id(self, train_id: str) -> Optional[TrainController]:
        """Look up controller by train_id without creating it."""
        return self.controllers.get(train_id)

    def _now(self) -> float:
        """System-time Unix seconds; canonical timestamp source for v1."""
        return time.time()

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
        routes = getattr(self.network, "routes", {}) or {}
        ordered = list(candidate_train_ids)

        def effective_priority(train_id: str) -> int:
            train = (
                self.network.trains.get(train_id)
                if hasattr(self.network, "trains")
                else None
            )
            route_id = None
            if train is not None:
                route_id = getattr(train, "assigned_route_id", None)
            if route_id is None:
                exec_state = self.execution_states.get(train_id)
                if exec_state is not None:
                    route_id = exec_state.route_id
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
                route_start_time=now,
                run_number=run_count,
                route_version_seen=int(getattr(route, "route_version", 1)),
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
            row = RouteTaskRun(task_index=i)
            # Per the canonical first-write rule, task_runs[0].started_at is
            # initialized to RouteRun.started_at on creation.
            if i == 0 and exec_state.started_at is not None:
                row.started_at = exec_state.started_at
            exec_state.task_runs.append(row)

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
        route_id = getattr(train, "assigned_route_id", None)
        route = None
        if route_id:
            route = getattr(self.network, "routes", {}).get(route_id)
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

        route_id = getattr(train, "assigned_route_id", None)
        route = None
        if route_id:
            route = getattr(self.network, "routes", {}).get(route_id)

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

        # ---------- Step 1: route-deviation cleanup ---------------------------
        if exec_state.path_cancelled and self._deviation_cleanup_eligible(
            controller
        ):
            exec_state.path_cancelled = False
            if exec_state.current_route_status == RouteRunStatus.DEVIATED:
                exec_state.current_route_status = RouteRunStatus.RUNNING
            exec_state.route_state = "RUNNING"

        # ---------- Step 2: dwell-complete timer check -----------------------
        if controller.state == TrainState.RUNNING_AT_STATION:
            self._dwell_complete_check(train, route, exec_state, controller, now)

        # ---------- Step 3: lookup next non-finished task --------------------
        projection = self._find_next_task_target(train_id)
        if projection is None:
            # Terminal walk for ONE_OFF; latch pending_route_finished.
            self._maybe_latch_route_finished(controller, exec_state)
            return

        # If next task is a dwell, skip directly to step 5.
        if projection.task_kind != "dwell":
            # ---------- Step 4: covered-task index advance ----------------
            self._covered_task_index_advance(
                train, route, exec_state, controller, now
            )
            # Re-resolve after a possible cursor change.
            projection = self._find_next_task_target(train_id)
            if projection is None:
                self._maybe_latch_route_finished(controller, exec_state)
                return

        # ---------- Step 5: dwell-entry transition ---------------------------
        if projection.task_kind == "dwell":
            self._dwell_entry_step(
                train, route, exec_state, controller, projection.task_index
            )
            # Phase 1 returns for this tick once dwell-entry decides — no path
            # creation while dwell is pending or active.
            return

        # ---------- Step 6: re-resolve next target (post cursor changes) ----
        projection = self._find_next_task_target(train_id)
        if projection is None or projection.task_kind == "dwell":
            return

        # ---------- Step 7: authority acquisition ---------------------------
        if not self._eligible_for_authority_acquisition(controller, exec_state):
            return

        # Phase 1 step 7: dispatch via PathDispatcher.create_path or extend_path
        # (v1 signature). Both methods take train_id +
        # requested_destination_block_id + requested_destination_direction +
        # acquisition_mode and return a PathAcquisitionResult.
        self._authority_acquisition_step(
            train, route, exec_state, controller, projection
        )

    # ----- Phase 1 sub-steps ------------------------------------------------
    def _deviation_cleanup_eligible(self, controller: TrainController) -> bool:
        """Predicate for the route-deviation cleanup latch consumer."""
        if controller.state not in (TrainState.IDLE, TrainState.STANDBY):
            return False
        try:
            plan = controller.received_path_plan
        except Exception:
            plan = None
        if plan is not None:
            return False
        if getattr(controller, "eb_active", False):
            return False
        if getattr(controller, "pending_power_off", False):
            return False
        return True

    def _eligible_for_authority_acquisition(
        self, controller: TrainController, exec_state: RouteExecutionState
    ) -> bool:
        """Predicate for steps 6-7 of the Planning Kernel."""
        try:
            plan = controller.received_path_plan
        except Exception:
            plan = None
        if plan is not None:
            return True
        if controller.state not in (TrainState.IDLE, TrainState.STANDBY):
            return False
        if getattr(controller, "pending_path_cancel_request", None) is not None:
            return False
        if getattr(controller, "pending_route_abort", None) is not None:
            return False
        if getattr(controller, "pending_power_off", False):
            return False
        if getattr(controller, "eb_active", False):
            return False
        if exec_state.path_cancelled:
            return False
        return True

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
        `current_waypoint_index`, calls `controller.finish_dwell()`. If a
        next task on the directed walk exists, write
        `task_runs[task_index+1].started_at = task_runs[task_index].finished_at`
        only if that field is currently None.
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
        dwell_started_at = row.started_at

        if not task.should_depart(now, dwell_started_at):
            return

        # Mark the dwell row finished and advance.
        row.finished_at = now
        # Write next task's started_at via the canonical first-write rule.
        next_index = task_index + 1
        if (
            next_index < len(exec_state.task_runs)
            and exec_state.task_runs[next_index].started_at is None
        ):
            exec_state.task_runs[next_index].started_at = now

        # Advance route per legacy mode-aware advance helper.
        has_more = self._advance_to_next_task(route, exec_state)
        exec_state.arrived_at_platform_time = None
        exec_state.waiting_for_path = False

        finish_dwell = getattr(controller, "finish_dwell", None)
        if callable(finish_dwell):
            try:
                finish_dwell()
            except Exception:
                fallback = getattr(controller, "depart_from_station", None)
                if callable(fallback):
                    try:
                        fallback(has_more_waypoints=has_more)
                    except Exception:
                        pass
        else:
            fallback = getattr(controller, "depart_from_station", None)
            if callable(fallback):
                try:
                    fallback(has_more_waypoints=has_more)
                except Exception:
                    pass

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
        whose resolved target block is already inside the current span,
        advance the cursor and record `finished_at`. Stops at a `stop` or
        `dwell` task or when no further task fits coverage.
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

            # Advance.
            if idx < len(exec_state.task_runs):
                row = exec_state.task_runs[idx]
                if row.finished_at is None:
                    row.finished_at = now
                next_index = idx + 1
                if (
                    next_index < len(exec_state.task_runs)
                    and exec_state.task_runs[next_index].started_at is None
                ):
                    exec_state.task_runs[next_index].started_at = now
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

        Preconditions: predecessor task finished, controller is IDLE/STANDBY,
        `received_path_plan is None`. The dwell row's `started_at` is already
        populated by the predecessor's `finished_at` writer per the canonical
        first-write rule; this step does not write `started_at`.
        """
        if controller.state == TrainState.RUNNING_AT_STATION:
            return
        if controller.state not in (TrainState.IDLE, TrainState.STANDBY):
            return
        try:
            if controller.received_path_plan is not None:
                return
        except Exception:
            return

        enter_station_dwell = getattr(controller, "enter_station_dwell", None)
        if callable(enter_station_dwell):
            try:
                enter_station_dwell()
            except Exception:
                fallback = getattr(controller, "arrive_at_station", None)
                if callable(fallback):
                    try:
                        fallback()
                    except Exception:
                        pass
        else:
            fallback = getattr(controller, "arrive_at_station", None)
            if callable(fallback):
                try:
                    fallback()
                except Exception:
                    pass
        if exec_state.arrived_at_platform_time is None:
            exec_state.arrived_at_platform_time = self._now()

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
        `task_runs[target_waypoint_index].resolved_target` (Phase-1-only writer).
        When the effective_departure_mode is "auto", call `controller.depart()`
        directly. There is no `departure_requested` latch in v1.
        """
        if self.path_dispatcher is None:
            return
        if projection.resolved_target is None:
            return

        target_block = projection.resolved_target.requested_destination_block_id
        target_dir = projection.resolved_target.requested_destination_direction

        # Decide create vs extend by live authority presence.
        active_path_id = self._get_active_path_id(train.train_id)

        # Resolve acquisition_mode for the route.
        route_mode_raw = getattr(route, "acquisition_mode", None)
        if isinstance(route_mode_raw, AcquisitionMode):
            acquisition_mode = route_mode_raw
        elif isinstance(route_mode_raw, str):
            try:
                acquisition_mode = AcquisitionMode(route_mode_raw)
            except ValueError:
                acquisition_mode = AcquisitionMode.ALL_OR_NOTHING
        else:
            acquisition_mode = AcquisitionMode.ALL_OR_NOTHING

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
                if success:
                    exec_state.active_path_id = active_path_id
            else:
                result = self.path_dispatcher.create_path(
                    train_id=train.train_id,
                    requested_destination_block_id=target_block,
                    requested_destination_direction=target_dir,
                    acquisition_mode=acquisition_mode,
                )
                success = bool(getattr(result, "success", False))
                if success:
                    exec_state.active_path_id = (
                        getattr(result, "path_id", None)
                        or self._get_active_path_id(train.train_id)
                    )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "authority acquisition raised for %s: %s", train.train_id, exc
            )
            success = False

        if not success:
            return

        # Successful create/extend: write task_runs[target_index].resolved_target.
        target_idx = projection.task_index
        self._sync_task_runs(route, exec_state)
        if 0 <= target_idx < len(exec_state.task_runs):
            exec_state.task_runs[target_idx].resolved_target = ResolvedRouteTarget(
                requested_destination_block_id=target_block,
                requested_destination_direction=target_dir,
            )
        exec_state.target_waypoint_index = target_idx

        # Update direction if projection says so.
        if projection.direction == "forward":
            exec_state.direction = 1
        elif projection.direction == "reverse":
            exec_state.direction = -1

        # Status update: always overwrite stale DEVIATED.
        exec_state.current_route_status = RouteRunStatus.RUNNING
        exec_state.route_state = "RUNNING"
        exec_state.waiting_for_path = False
        if hasattr(route, "clear_error"):
            try:
                route.clear_error()
            except Exception:
                pass

        # Effective departure mode.
        effective_mode = getattr(route, "default_departure_mode", "auto") or "auto"

        if effective_mode == "auto":
            depart = getattr(controller, "depart", None)
            if callable(depart):
                try:
                    depart()
                except Exception:
                    pass

    def _maybe_latch_route_finished(
        self,
        controller: TrainController,
        exec_state: RouteExecutionState,
    ) -> None:
        """Latch `pending_route_finished` on the controller mailbox.

        Phase 3's `pending_route_finished` consumer performs the actual state
        transition to FINISHED, calls `route.unassign_train(...)`, etc.
        Phase 1 does NOT write `state = FINISHED` itself.
        """
        if exec_state.current_route_status in (
            RouteRunStatus.FINISHED,
            RouteRunStatus.DEACTIVATED,
        ):
            return
        try:
            controller.pending_route_finished = True
        except Exception:
            pass

    # ------------------------------------------------------------------ Phase 3
    def handle_train_advanced(
        self,
        train_id: str,
        path_id: str,
        resource_type: str,
        resource_id: str,
        resource_traversal_direction: Optional[str] = None,
    ) -> None:
        """Phase-3 head-advance callback. Consumes at most one task per call.

        Per design_new.md:
          * Switches are no-ops on the route side.
          * Only `task_kind == "waypoint"` tasks may be consumed by head entry;
            `stop` tasks are served only by dispatcher-committed completion.
          * The reported block must match the next non-finished task's
            resolved-target block AND direction.
        """
        if resource_type != "block":
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
        if projection.resolved_target.requested_destination_block_id != resource_id:
            return
        if (
            resource_traversal_direction is not None
            and projection.resolved_target.requested_destination_direction
            != resource_traversal_direction
        ):
            return

        # Consume that one task: record finished_at and advance the cursor.
        now = self._now()
        idx = projection.task_index
        self._sync_task_runs(route, exec_state)
        if 0 <= idx < len(exec_state.task_runs):
            row = exec_state.task_runs[idx]
            if row.finished_at is None:
                row.finished_at = now
            next_index = idx + 1
            if (
                next_index < len(exec_state.task_runs)
                and exec_state.task_runs[next_index].started_at is None
            ):
                exec_state.task_runs[next_index].started_at = now

        self._advance_to_next_task(route, exec_state)
        exec_state.arrived_at_platform_time = None

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        cancellation_kind: Optional[PathCancellationKind] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Phase-3 cancellation callback.

        All cancellation kinds share the same body. The callback is
        bookkeeping only: it must not choose a fresh target or create fresh
        authority.
        """
        exec_state = self.execution_states.get(train_id)
        if exec_state is None:
            return
        exec_state.path_cancelled = True
        exec_state.current_route_status = RouteRunStatus.DEVIATED
        exec_state.route_state = "DEVIATED"
        if cancellation_kind is not None:
            exec_state.last_cancellation_kind = cancellation_kind
        exec_state.waiting_for_path = False
        # Reset planning cursor to physical progress.
        exec_state.target_waypoint_index = exec_state.current_waypoint_index
        if exec_state.active_path_id == path_id:
            exec_state.active_path_id = None

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        authority_end_block_id: Optional[str] = None,
        requested_destination_direction: Optional[str] = None,
        event_time: Optional[float] = None,
        # Legacy compatibility kwargs (older SimulationController polling
        # code still passes _end_tile/_event_time).
        _end_tile: Optional[Tuple[int, int]] = None,
        _event_time: Optional[object] = None,
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
            if isinstance(_event_time, (int, float)):
                event_time = float(_event_time)
            elif isinstance(_event_time, datetime):
                event_time = _event_time.timestamp()
            else:
                event_time = self._now()

        train, route, exec_state = self._resolve_run_route(train_id)
        if train is None or route is None or exec_state is None:
            return False

        if exec_state.active_path_id == path_id:
            exec_state.active_path_id = None

        exec_state.waiting_for_path = False

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

        # _enter_task_arrival semantics: advance cursor to match_index.
        # Record finished_at on the matched stop row and write next started_at.
        if 0 <= match_index < len(exec_state.task_runs):
            row = exec_state.task_runs[match_index]
            if row.finished_at is None:
                row.finished_at = event_time
            next_index = match_index + 1
            if (
                next_index < len(exec_state.task_runs)
                and exec_state.task_runs[next_index].started_at is None
            ):
                exec_state.task_runs[next_index].started_at = event_time

        exec_state.current_waypoint_index = max(
            exec_state.current_waypoint_index, match_index
        )
        exec_state.target_waypoint_index = exec_state.current_waypoint_index
        if hasattr(route, "current_task_index"):
            route.current_task_index = exec_state.current_waypoint_index
        exec_state.arrived_at_platform_time = event_time
        # Phase 3 must NOT call enter_station_dwell — that fires from the next
        # eligible Phase 1 dwell-entry step.
        return True

    # ------------------------------------------------------------------ removal
    def remove_train_route_run(self, train_id: str, reason: str) -> None:
        """Tear down a RouteRun for `pending_route_abort` / `pending_power_off`."""
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
                    fallback = getattr(controller, "depart_from_station", None)
                    if callable(fallback):
                        try:
                            fallback(has_more_waypoints=False)
                        except Exception:
                            pass
            else:
                fallback = getattr(controller, "depart_from_station", None)
                if callable(fallback):
                    try:
                        fallback(has_more_waypoints=False)
                    except Exception:
                        pass

        train = (
            self.network.trains.get(train_id)
            if hasattr(self.network, "trains")
            else None
        )
        if train is not None:
            route_id = getattr(train, "assigned_route_id", None)
            if route_id:
                route = getattr(self.network, "routes", {}).get(route_id)
                if route is not None:
                    try:
                        route.unassign_train(train_id)
                    except Exception:
                        pass
            try:
                if getattr(train, "assigned_route_id", None) == route_id:
                    train.assigned_route_id = None
            except Exception:
                pass

        if train_id in self.execution_states:
            del self.execution_states[train_id]

    # ------------------------------------------------------------------ helper
    def _find_next_task_target(
        self,
        train_id: str,
        exclude: Optional[Iterable[ResolvedRouteTarget]] = None,
    ) -> Optional[ResolvedNextTaskProjection]:
        """Read-only projection of the next non-finished task.

        Per design_new.md "Canonical Next-Target Selection":
          * Returns `None` when no next task on the directed walk exists.
          * Helper itself stores no per-tick state.
          * Helper does NOT write `task_runs[i].resolved_target`. The only
            writer is Phase 1's authority-acquisition step.

        v1 behavior: dispatches on `task_kind` from Route.tasks[i]:
          - "waypoint" / "stop": resolve a concrete target block + direction.
          - "dwell": no dispatcher target; resolved_target is None.
        """
        train = (
            self.network.trains.get(train_id)
            if hasattr(self.network, "trains")
            else None
        )
        if train is None:
            return None
        route_id = getattr(train, "assigned_route_id", None)
        if not route_id:
            return None
        route = getattr(self.network, "routes", {}).get(route_id)
        if route is None:
            return None
        exec_state = self.execution_states.get(train_id)
        if exec_state is None:
            return None
        task_count = route.get_task_count() if hasattr(route, "get_task_count") else 0
        if task_count == 0:
            return None

        # Reconcile route_version_seen if Route.route_version has advanced.
        current_version = int(getattr(route, "route_version", 1))
        if current_version != exec_state.route_version_seen:
            # Wave 1: simple update; Wave 2 may apply reconciliation_policy
            # logic to relocate cursors.
            exec_state.route_version_seen = current_version

        next_index = self._compute_next_task_index(route, exec_state, task_count)
        if next_index is None:
            return None

        task = route.get_task_at(next_index)
        if task is None:
            return None

        direction_str = "forward" if exec_state.direction >= 0 else "reverse"

        # Dispatch on task_kind. This is the v1 fix that unblocks dwell-entry.
        if task.task_kind == "dwell":
            # Dwell tasks carry no dispatcher target.
            return ResolvedNextTaskProjection(
                task_index=next_index,
                task_kind="dwell",
                direction=direction_str,
                resolved_target=None,
            )

        # waypoint or stop: resolve a concrete block + direction.
        target_block = self._resolve_task_target_block(task)
        if target_block is None:
            return ResolvedNextTaskProjection(
                task_index=next_index,
                task_kind=task.task_kind,
                direction=direction_str,
                resolved_target=None,
            )

        resolved_dir = self._resolve_task_direction(
            train, target_block, task.task_direction, exec_state.direction
        )

        resolved = ResolvedRouteTarget(
            requested_destination_block_id=target_block,
            requested_destination_direction=resolved_dir,
        )

        # Honor caller-provided `exclude` set.
        if exclude:
            for ex in exclude:
                if (
                    ex.requested_destination_block_id
                    == resolved.requested_destination_block_id
                    and ex.requested_destination_direction
                    == resolved.requested_destination_direction
                ):
                    return None

        return ResolvedNextTaskProjection(
            task_index=next_index,
            task_kind=task.task_kind,
            direction=direction_str,
            resolved_target=resolved,
        )

    def _resolve_task_target_block(self, task: RouteTask) -> Optional[str]:
        """Resolve a RouteTask.task_location to a concrete block_id.

        v1 supports three location kinds:
          * direct block_id
          * platform_id (lookup via network.platforms)
          * station_id (lookup via network.station_groups, then pick a platform)
        The legacy schema stored task_location as a single string; we try
        block, platform, station in that order. Wave 2 may add an explicit
        kind discriminator if the storage layer can supply one.
        """
        location = getattr(task, "task_location", "")
        if not location:
            return None

        # Try block direct.
        blocks = getattr(self.network, "blocks", {}) or {}
        if location in blocks:
            return location

        # Try platform.
        block_id = self._get_platform_block_id(location)
        if block_id:
            return block_id

        # Try station.
        station_groups = getattr(self.network, "station_groups", {}) or {}
        station = station_groups.get(location)
        if station is not None:
            platform_ids = getattr(station, "platform_ids", []) or []
            for pid in platform_ids:
                bid = self._get_platform_block_id(pid)
                if bid:
                    return bid

        return None

    def _resolve_task_direction(
        self,
        train: "Train",
        block_id: str,
        task_direction: str,
        signed_direction: int,
    ) -> str:
        """Resolve a RouteTask.task_direction to "forward" / "reverse".

        v1: dispatcher never receives "either"; route resolution must always
        eliminate it before calling create_path/extend_path.
        """
        if task_direction in ("forward", "reverse"):
            return task_direction

        # "either" — derive from train direction or default to forward.
        # Wave 2 will use Block.ordered_tiles to make this exact; for Wave 1
        # we surface a deterministic fallback that the dispatcher can then
        # reconcile against block geometry.
        return "forward" if signed_direction >= 0 else "reverse"

    def _compute_next_task_index(
        self,
        route: "Route",
        exec_state: RouteExecutionState,
        task_count: int,
    ) -> Optional[int]:
        """Compute the next task index without mutating exec_state."""
        if task_count == 0:
            return None

        idx = exec_state.current_waypoint_index
        if 0 <= idx < task_count:
            return idx

        if route.mode == RouteMode.ONE_OFF:
            if idx >= task_count:
                return None
            return idx

        if route.mode == RouteMode.CIRCULAR:
            return idx % task_count

        if route.mode == RouteMode.ROUND_TRIP:
            if idx >= task_count:
                return task_count - 2 if task_count > 1 else 0
            if idx < 0:
                return 1 if task_count > 1 else 0
            return idx
        return None

    # ------------------------------------------------------------------ legacy
    def _get_active_route_train_pairs(self) -> List[Tuple["Train", "Route"]]:
        """Collect active (train, route) pairs."""
        pairs: List[Tuple["Train", "Route"]] = []
        seen_pairs = set()
        routes = getattr(self.network, "routes", {})

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

        for train in self.network.trains.values():
            if train.assigned_route_id:
                route = routes.get(train.assigned_route_id)
                if route and route.is_active:
                    if train.train_id not in route.assigned_train_ids:
                        route.assign_train(train.train_id)
                    pair_key = (train.train_id, route.route_id)
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        pairs.append((train, route))

        return pairs

    def _advance_to_next_task(
        self, route: "Route", exec_state: RouteExecutionState
    ) -> bool:
        """Advance per-train task index based on route mode.

        Mutates `exec_state.current_waypoint_index` and `exec_state.direction`.
        """
        task_count = route.get_task_count()
        if task_count == 0:
            return False

        if route.mode == RouteMode.ONE_OFF:
            exec_state.current_waypoint_index += 1
            exec_state.resolved_platform_id = None
            if hasattr(route, "current_task_index"):
                route.current_task_index = exec_state.current_waypoint_index
            return exec_state.current_waypoint_index < task_count

        if route.mode == RouteMode.CIRCULAR:
            exec_state.current_waypoint_index += 1
            if exec_state.current_waypoint_index >= task_count:
                exec_state.current_waypoint_index = 0
            exec_state.resolved_platform_id = None
            if hasattr(route, "current_task_index"):
                route.current_task_index = exec_state.current_waypoint_index
            return True

        # ROUND_TRIP
        exec_state.current_waypoint_index += exec_state.direction
        if exec_state.current_waypoint_index >= task_count:
            exec_state.direction = -1
            exec_state.current_waypoint_index = task_count - 2
            if exec_state.current_waypoint_index < 0:
                exec_state.current_waypoint_index = 0
        elif exec_state.current_waypoint_index < 0:
            exec_state.direction = 1
            exec_state.current_waypoint_index = 1
            if exec_state.current_waypoint_index >= task_count:
                exec_state.current_waypoint_index = 0

        exec_state.resolved_platform_id = None
        if hasattr(route, "current_task_index"):
            route.current_task_index = exec_state.current_waypoint_index
        if hasattr(route, "direction"):
            route.direction = exec_state.direction
        return True

    # ------------------------------------------------------------------ network helpers
    def _is_train_at_block(self, train: "Train", block_id: str) -> bool:
        """Return True when the train head sits in the named block."""
        if not train.position:
            return False
        track = self.network.get_track(*train.position.head_tile)
        if not track:
            return False
        return track.block_section_id == block_id

    def _is_train_at_platform_for_task(
        self, train: "Train", task: RouteTask
    ) -> bool:
        """Return True when the train head sits at a platform tile that
        satisfies the task's location."""
        if not train.position:
            return False
        platform = self._get_platform(task.task_location)
        if not platform:
            return False
        target_tile = self._get_target_platform_tile(train, platform)
        if not target_tile:
            return False
        return train.position.head_tile == target_tile

    def _is_train_at_platform(self, train: "Train", platform_id: str) -> bool:
        """Compatibility helper for legacy callers."""
        if not train.position:
            return False
        platform = self._get_platform(platform_id)
        if not platform:
            return False
        target_tile = self._get_target_platform_tile(train, platform)
        if not target_tile:
            return False
        return train.position.head_tile == target_tile

    def _get_target_platform_tile(
        self, train: "Train", platform: object
    ) -> Optional[Tuple[int, int]]:
        """Pick stopping tile for platform based on travel direction."""
        positions = list(getattr(platform, "positions", []))
        if not positions:
            return None
        if len(positions) == 1:
            return positions[0]

        direction = train.get_effective_direction()
        if not direction:
            return positions[0]

        def sort_key(pos: Tuple[int, int]) -> float:
            x, y = pos
            if direction.name == "NORTH":
                return -y
            if direction.name == "SOUTH":
                return y
            if direction.name == "EAST":
                return -x
            if direction.name == "WEST":
                return x
            return 0

        return sorted(positions, key=sort_key)[0]

    def _get_target_station_tile(
        self, train: "Train", station: object
    ) -> Optional[Tuple[int, int]]:
        """Compatibility helper for station-oriented tests/callers."""
        return self._get_target_platform_tile(train, station)

    def _is_train_at_station(self, train: "Train", station_id: str) -> bool:
        """Compatibility helper for station-oriented tests/callers."""
        return self._is_train_at_platform(train, station_id)

    def _get_platform(self, platform_id: str) -> Optional[object]:
        """Get platform by ID."""
        platforms = getattr(self.network, "platforms", None)
        if isinstance(platforms, dict) and platform_id in platforms:
            return platforms[platform_id]
        return None

    def _get_platform_block_id(self, platform_id: str) -> Optional[str]:
        """Get primary block for a platform."""
        platform = self._get_platform(platform_id)
        if not platform:
            return None

        get_all_assigned_blocks = getattr(platform, "get_all_assigned_blocks", None)
        if callable(get_all_assigned_blocks):
            assigned_blocks = get_all_assigned_blocks()
            block_ids = (
                list(assigned_blocks)
                if isinstance(assigned_blocks, (list, set, tuple))
                else []
            )
            if block_ids:
                return block_ids[0]

        for x, y in getattr(platform, "positions", []):
            track = self.network.get_track(x, y)
            if track and track.block_section_id:
                return track.block_section_id
        return None

    def _get_current_block_id(self, train: "Train") -> Optional[str]:
        """Get train's current block based on head tile."""
        if not train.position:
            return None
        track = self.network.get_track(*train.position.head_tile)
        if not track:
            return None
        return track.block_section_id

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

    def set_train_waypoint_index(self, train_id: str, waypoint_index: int) -> bool:
        """Override current task cursor for an assigned train."""
        train = self.network.trains.get(train_id)
        if not train or not train.assigned_route_id:
            return False

        route = getattr(self.network, "routes", {}).get(train.assigned_route_id)
        if not route:
            return False
        if waypoint_index < 0 or waypoint_index >= route.get_task_count():
            return False

        exec_state = self.execution_states.get(train_id)
        if not exec_state or exec_state.route_id != route.route_id:
            now = self._now()
            run_count = self._run_count_for_route.get(route.route_id, 0) + 1
            self._run_count_for_route[route.route_id] = run_count
            exec_state = RouteExecutionState(
                train_id=train_id,
                route_id=route.route_id,
                started_at=now,
                route_start_time=now,
                run_number=run_count,
                route_version_seen=int(getattr(route, "route_version", 1)),
            )
            self.execution_states[train_id] = exec_state

        exec_state.current_waypoint_index = waypoint_index
        exec_state.target_waypoint_index = waypoint_index
        exec_state.arrived_at_platform_time = None
        exec_state.resolved_platform_id = None
        exec_state.waiting_for_path = False
        return True
