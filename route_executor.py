"""
Route Executor

Per design_new.md "Route And RouteExecutor" and "Planning Kernel" sections.

RouteExecutor owns:
  * Phase 1 planning (`tick_train_phase1`) for one train: route-deviation
    cleanup, dwell-complete timer check, covered-task index advance, dwell-entry,
    target resolution, and authority acquisition via PathDispatcher.
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

from enums import Direction, TrainState
from train_controller import TrainController, create_controller_for_train
from enums import PlatformSide
from train_route import RouteMode, TravelDirectionPolicy
from runtime_payloads import PathCancellationKind, PendingPathCancelRequest

if TYPE_CHECKING:
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from train import Train
    from train_route import Route, Waypoint

logger = logging.getLogger(__name__)


# =============================================================================
# Resolved-target schemas (kept local; runtime_payloads.py owns cross-component
# payloads, but route-side resolution products live with RouteExecutor).
# =============================================================================


@dataclass
class ResolvedRouteTarget:
    """Resolved dispatcher request contract for one task attempt.

    Per design_new.md, carries exactly the route-owned target contract:
    `requested_destination_block_id` plus block-relative
    `requested_destination_direction` ("forward" / "reverse"). Dispatcher
    derives terminal frame from this block plus its own resource_sequence; the
    route never passes a centimeter target.
    """

    requested_destination_block_id: str
    requested_destination_direction: str  # "forward" | "reverse"


@dataclass
class ResolvedNextTaskProjection:
    """Projection returned by `_find_next_task_target(train_id)`.

    Read-only result. Phase 1's authority-acquisition step is the only writer of
    `task_runs[i].resolved_target`; the helper itself never writes that field.
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

    The current implementation keeps the legacy class name `RouteExecutionState`
    while the design doc names the concept `RouteRun`. They are the same thing.

    Fields beyond the v1 minimum (current_waypoint_index, target_waypoint_index,
    direction, task_runs, etc.) are retained for callers/tests that already
    depend on them; new v1 latches and bookkeeping flags are appended without
    removing the legacy fields.
    """

    train_id: str
    route_id: str
    current_waypoint_index: int = 0
    direction: int = 1
    route_start_time: Optional[float] = None
    arrived_at_platform_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_platform_id: Optional[str] = None
    resolved_platform_id: Optional[str] = None
    # v1 latch consumed by Phase-1 route-deviation cleanup.
    path_cancelled: bool = False
    # Per-run diagnostic status; written by Phase 1 / handle_path_cancellation.
    # Allowed values: "RUNNING" / "NO_PATH" / "DEVIATED" / "FINISHED" /
    # "DEACTIVATED". The v1 design specifies a `current_route_status` object
    # with `state` and `last_cancellation_kind`; we expose those two fields
    # directly on the run for now without a nested object.
    route_state: str = "RUNNING"
    last_cancellation_kind: Optional[PathCancellationKind] = None
    # Active-path correlation for Phase 3 callbacks.
    active_path_id: Optional[str] = None


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
        # Note: no `simulation_time` accumulator is kept (per design_new.md
        # "Concrete RouteExecutor tick calls" — all stored timestamps are
        # system-time Unix seconds via time.time()).

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
        trains use the current shared `Route.priority`; trains without a current
        RouteRun use effective priority 5. The merged set is sorted strictly by
        effective priority descending (`9` highest, `0` lowest), with stable
        existing iteration order as the only tie-break.
        """
        routes = getattr(self.network, "routes", {}) or {}
        ordered = list(candidate_train_ids)

        def effective_priority(train_id: str) -> int:
            train = self.network.trains.get(train_id) if hasattr(
                self.network, "trains"
            ) else None
            route_id = None
            if train is not None:
                route_id = getattr(train, "assigned_route_id", None)
            if route_id is None:
                # Fall back to RouteRun.route_id when present.
                exec_state = self.execution_states.get(train_id)
                if exec_state is not None:
                    route_id = exec_state.route_id
            if route_id is None:
                return 5
            route = routes.get(route_id)
            if route is None:
                return 5
            return int(getattr(route, "priority", 5))

        # Stable sort by descending priority. Python's sort is stable, so ties
        # preserve the input iteration order.
        ordered.sort(key=effective_priority, reverse=True)
        return ordered

    # ------------------------------------------------------------------ update
    def update(self, delta_seconds: float) -> None:
        """Refresh route-managed scheduling inputs for this tick.

        Per design_new.md, this method does NOT iterate trains. The
        `SimulationController` outer loop owns per-train transactions and calls
        `RouteExecutor.tick_train_phase1(train_id, delta_seconds)` once for each
        runnable train as the entry point of Phase 1.
        """
        # No accumulator and no per-train iteration. The hook exists so future
        # scheduling state (e.g. shared-route reconciliation snapshot) can be
        # refreshed once per tick before the per-train Phase 1 calls run.
        return

    # ------------------------------------------------------------------ run mgmt
    def _ensure_run_for(
        self, train: "Train", route: "Route"
    ) -> RouteExecutionState:
        """Get or create the RouteExecutionState for (train, route)."""
        exec_state = self.execution_states.get(train.train_id)
        if exec_state is None or exec_state.route_id != route.route_id:
            exec_state = RouteExecutionState(
                train_id=train.train_id,
                route_id=route.route_id,
                route_start_time=self._now(),
            )
            self.execution_states[train.train_id] = exec_state
        return exec_state

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

        Per design_new.md "Planning Kernel" steps (1)–(7). Each step has its own
        gating predicate; steps 1–5 always run, steps 6–7 are gated by the
        Phase-1 eligibility precondition.
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
            # The controller must already exist before authority assignment;
            # construct one for the safety-net case.
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

        # Terminal runs do nothing in Phase 1 (state preserved as historical
        # record).
        if exec_state.route_state in ("FINISHED", "DEACTIVATED"):
            return

        now = self._now()

        # ---------- Step 1: route-deviation cleanup ---------------------------
        if exec_state.path_cancelled and self._deviation_cleanup_eligible(
            controller
        ):
            exec_state.path_cancelled = False
            if exec_state.route_state == "DEVIATED":
                exec_state.route_state = "RUNNING"

        # ---------- Step 2: dwell-complete timer check -----------------------
        if controller.state == TrainState.RUNNING_AT_STATION:
            self._dwell_complete_check(train, route, exec_state, controller, now)

        # ---------- Step 3: lookup next non-finished task --------------------
        projection = self._find_next_task_target(train_id)
        if projection is None:
            # Terminal walk for ONE_OFF; only fires after the last stop+dwell
            # has completed. Latch pending_route_finished on the controller
            # mailbox and return — Phase 3 handles teardown.
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

        self._authority_acquisition_step(
            train, route, exec_state, controller, projection
        )

    # ----- Phase 1 sub-steps ------------------------------------------------
    def _deviation_cleanup_eligible(self, controller: TrainController) -> bool:
        """Predicate for the route-deviation cleanup latch consumer."""
        if controller.state not in (TrainState.IDLE, TrainState.STANDBY):
            return False
        # received_path_plan must be None (no live authority).
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
        """Predicate for steps 6–7 of the Planning Kernel."""
        # If live authority exists, the extend branch is allowed.
        try:
            plan = controller.received_path_plan
        except Exception:
            plan = None
        if plan is not None:
            return True
        # No live authority → IDLE/STANDBY + clean handoff state is required.
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

        On completion: write `task_runs[task_index].finished_at = now`, advance
        `current_waypoint_index`, call `controller.finish_dwell()`. If a next
        task on the directed walk exists, write
        `task_runs[task_index+1].started_at = task_runs[task_index].finished_at`
        only if that field is currently None.
        """
        # In v1 the timer comes from RouteTask; the legacy code uses Waypoint
        # with `dwell_time_sec` and `should_depart(...)`. Reuse that here.
        waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
        if waypoint is None:
            return
        # Compute time at platform from arrival_at_platform_time when present.
        time_at_platform = (
            now - exec_state.arrived_at_platform_time
            if exec_state.arrived_at_platform_time is not None
            else 0.0
        )
        route_start = (
            exec_state.route_start_time
            if exec_state.route_start_time is not None
            else (route.started_at if route.started_at is not None else now)
        )
        if not waypoint.should_depart(time_at_platform, route_start, now):
            return

        # Advance route per legacy mode-aware advance helper (writes back to
        # exec_state.current_waypoint_index and exec_state.direction).
        has_more = self._advance_to_next_waypoint(route, exec_state)
        exec_state.arrived_at_platform_time = None
        exec_state.waiting_for_path = False
        # finish_dwell is the canonical dwell-exit transition on the controller.
        finish_dwell = getattr(controller, "finish_dwell", None)
        if callable(finish_dwell):
            try:
                finish_dwell()
            except Exception:
                # finish_dwell may not yet be implemented on TrainController in
                # the in-progress migration; fall back to legacy
                # depart_from_station for the same effect.
                fallback = getattr(controller, "depart_from_station", None)
                if callable(fallback):
                    try:
                        fallback(has_more_waypoints=has_more)
                    except Exception:
                        pass
        else:
            # Legacy controller surface: emulate via depart_from_station.
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
        whose resolved target block is already inside the current span, advance
        the cursor and record `finished_at`. Stops at a `stop` or `dwell` task
        or when no further task fits coverage.
        """
        # Legacy code base does not expose RouteTask kinds; emulate the
        # behavior on the legacy waypoint-based Route by treating every
        # waypoint as a potential coverage target only when the train head is
        # already at the resolved platform tile. This keeps the helper safe
        # while the broader Route schema migration is incomplete.
        waypoint_count = route.get_waypoint_count()
        while exec_state.current_waypoint_index < waypoint_count:
            waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
            if waypoint is None:
                break
            target_platform_id = exec_state.resolved_platform_id or waypoint.platform_id
            if not target_platform_id:
                break
            if not self._is_train_at_platform(train, target_platform_id):
                break
            # Treat the legacy waypoint with non-zero dwell as a stop+dwell pair
            # (do not skip), matching design_new.md "stops are served only by
            # dispatcher-committed completion".
            if (
                getattr(waypoint, "dwell_time_sec", 0.0) is not None
                and waypoint.dwell_time_sec > 0.0
            ):
                break
            # Otherwise advance.
            self._advance_to_next_waypoint(route, exec_state)

    def _dwell_entry_step(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        task_index: int,
    ) -> None:
        """Phase 1 step 5: enter dwell when the next task is `dwell`.

        Preconditions per design_new.md: predecessor task finished, controller
        is IDLE/STANDBY, `received_path_plan is None`. The dwell row's
        started_at is already populated by the predecessor's finished_at writer
        per the canonical first-write rule; this step does not write started_at.
        """
        # Already in dwell → nothing to do.
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
        # Record arrival timestamp so dwell-complete check has a baseline.
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
            # Resolution-only — no concrete target this tick.
            return

        target_block = projection.resolved_target.requested_destination_block_id
        target_dir = projection.resolved_target.requested_destination_direction

        # Decide create vs extend by live authority presence.
        active_path_id = self._get_active_path_id(train.train_id)

        success = False
        try:
            if active_path_id is not None:
                # Extend the existing live path. Newer signature requires
                # (path_id, requested_destination_block_id,
                #  requested_destination_direction, acquisition_mode); the
                # legacy in-tree `extend_path(path_id, new_end_block_id)` is
                # used as a fallback.
                acquisition_mode = getattr(route, "acquisition_mode", None)
                ext_kwargs = {
                    "path_id": active_path_id,
                    "requested_destination_block_id": target_block,
                    "requested_destination_direction": target_dir,
                }
                if acquisition_mode is not None:
                    ext_kwargs["acquisition_mode"] = acquisition_mode
                try:
                    result = self.path_dispatcher.extend_path(**ext_kwargs)
                except TypeError:
                    result = self.path_dispatcher.extend_path(
                        active_path_id, target_block
                    )
                # ExtensionResult / ExtendPathResult uses .success
                success = bool(getattr(result, "success", False))
                if success:
                    exec_state.active_path_id = active_path_id
            else:
                # Create new authority for this train.
                acquisition_mode = getattr(route, "acquisition_mode", None)
                cre_kwargs = {
                    "train_id": train.train_id,
                    "requested_destination_block_id": target_block,
                    "requested_destination_direction": target_dir,
                }
                if acquisition_mode is not None:
                    cre_kwargs["acquisition_mode"] = acquisition_mode
                try:
                    result = self.path_dispatcher.create_path(**cre_kwargs)
                except TypeError:
                    # Legacy signature.
                    current_block_id = self._get_current_block_id(train)
                    if not current_block_id:
                        return
                    result = self.path_dispatcher.create_path(
                        start_block_id=current_block_id,
                        end_block_id=target_block,
                        train_id=train.train_id,
                    )
                success = bool(getattr(result, "success", False))
                if success:
                    new_path = getattr(result, "path", None)
                    if new_path is not None:
                        exec_state.active_path_id = getattr(
                            new_path, "path_id", None
                        )
                    else:
                        exec_state.active_path_id = self._get_active_path_id(
                            train.train_id
                        )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "authority acquisition raised for %s: %s", train.train_id, exc
            )
            success = False

        if not success:
            # No authority created/grown. Leave route_state untouched here; the
            # caller may set NO_PATH if same-tick candidates have been
            # exhausted. The legacy code base does not loop the helper for
            # station candidates yet; we leave that hook for a future pass.
            return

        # Successful create/extend: ensure status is RUNNING.
        exec_state.route_state = "RUNNING"
        exec_state.waiting_for_path = False
        if hasattr(route, "clear_error"):
            try:
                route.clear_error()
            except Exception:
                pass

        # Effective departure mode: route default overridden by per-task
        # `departure_mode` (when present). Legacy schema does not carry this
        # per-task; fall back to Route.default_departure_mode (default "auto").
        effective_mode = getattr(route, "default_departure_mode", "auto") or "auto"

        if effective_mode == "auto":
            # Direct call; no latch in v1.
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
        transition to FINISHED, calls `route.unassign_train(...)`, etc. Phase 1
        does NOT write `state = FINISHED` itself.
        """
        # Only latch when there is a route to finish and the run is not already
        # terminal.
        if exec_state.route_state in ("FINISHED", "DEACTIVATED"):
            return
        try:
            controller.pending_route_finished = True
        except Exception:
            pass

    # ------------------------------------------------------------------ Phase 3
    # Callbacks invoked synchronously by PathDispatcher / SimulationController.
    def handle_train_advanced(
        self,
        train_id: str,
        path_id: str,
        resource_type: str,
        resource_id: str,
        resource_traversal_direction: Optional[str] = None,
    ) -> None:
        """Phase-3 head-advance callback. Consumes at most one task per call.

        Per design_new.md "Dispatcher progress/completion flow" and
        "Physical progress / deviation procedure":
          * Switches are no-ops on the route side.
          * Only `task_kind == "waypoint"` tasks may be consumed by head entry;
            `stop` tasks are served only by dispatcher-committed completion via
            `handle_path_completion`.
          * The reported block must match the next non-finished task's
            resolved-target block AND direction.
        """
        if resource_type != "block":
            return
        train, route, exec_state = self._resolve_run_route(train_id)
        if train is None or route is None or exec_state is None:
            return
        # Live-resolve the next non-finished task (read-only).
        projection = self._find_next_task_target(train_id)
        if projection is None:
            return
        if projection.task_kind != "waypoint":
            # Stop tasks served by handle_path_completion only; non-waypoint
            # next task → no head-advance consumption on the route side.
            return
        if projection.resolved_target is None:
            return
        if projection.resolved_target.requested_destination_block_id != resource_id:
            # Block mismatch — covered-task index advance in next Phase 1
            # reconciles intermediate passes.
            return
        if (
            resource_traversal_direction is not None
            and projection.resolved_target.requested_destination_direction
            != resource_traversal_direction
        ):
            return

        # Consume that one task: advance the cursor.
        # Keep legacy mode-aware advance for now (writes
        # exec_state.current_waypoint_index/direction).
        self._advance_to_next_waypoint(route, exec_state)
        exec_state.arrived_at_platform_time = None

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        cancellation_kind: Optional[PathCancellationKind] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Phase-3 cancellation callback.

        All cancellation kinds share the same body (no per-kind branch). The
        callback is bookkeeping only: it must not choose a fresh target or
        create fresh authority.
        """
        exec_state = self.execution_states.get(train_id)
        if exec_state is None:
            return
        exec_state.path_cancelled = True
        exec_state.route_state = "DEVIATED"
        if cancellation_kind is not None:
            exec_state.last_cancellation_kind = cancellation_kind
        exec_state.waiting_for_path = False
        # Clear any active-path correlation.
        if exec_state.active_path_id == path_id:
            exec_state.active_path_id = None

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        authority_end_block_id: Optional[str] = None,
        requested_destination_direction: Optional[str] = None,
        event_time: Optional[float] = None,
        # Legacy compatibility kwargs (older SimulationController polling code
        # still passes _end_tile/_event_time).
        _end_tile: Optional[Tuple[int, int]] = None,
        _event_time: Optional[object] = None,
    ) -> bool:
        """Phase-3 completion callback.

        Per design_new.md "Active-path completion procedure" step 10:
          * walk task_runs from current_waypoint_index + 1
          * pick first non-finished `task_kind == "stop"` whose stored
            resolved_target matches authority_end_block_id +
            requested_destination_direction
          * record finished_at, advance current_waypoint_index, write next
            task's started_at if empty
          * clear route-owned active-path correlation
        """
        # Resolve event_time (from legacy `_event_time` when needed).
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

        # Mark route-side active-path correlation cleared.
        if exec_state.active_path_id == path_id:
            exec_state.active_path_id = None

        # Legacy-compatible bookkeeping: clear waiting_for_path.
        exec_state.waiting_for_path = False

        # Walk tasks from current_waypoint_index + 1 looking for a stop whose
        # resolved_target matches the dispatcher's reported authority end.
        # Until the broader RouteTask migration is complete, the legacy Route
        # only carries Waypoint entries; treat each waypoint as a stop and
        # match by platform-block resolution.
        waypoint_count = route.get_waypoint_count()
        match_index: Optional[int] = None
        for i in range(exec_state.current_waypoint_index + 1, waypoint_count):
            waypoint = route.get_waypoint_at(i)
            if waypoint is None:
                continue
            wp_block = self._get_platform_block_id(waypoint.platform_id)
            if not wp_block:
                continue
            if authority_end_block_id and wp_block != authority_end_block_id:
                continue
            match_index = i
            break

        if match_index is None:
            # No matching stop task — completion is a noop on the route side.
            # Leave current_waypoint_index untouched so the next eligible Phase
            # 1 reconciles via covered-task advance.
            return False

        # Apply _enter_task_arrival semantics: advance cursor to match_index.
        exec_state.current_waypoint_index = max(
            exec_state.current_waypoint_index, match_index
        )
        if hasattr(route, "current_waypoint_index"):
            route.current_waypoint_index = exec_state.current_waypoint_index
        exec_state.arrived_at_platform_time = event_time
        # Phase 3 must NOT call enter_station_dwell — that fires from the next
        # eligible Phase 1 dwell-entry step.
        return True

    # ------------------------------------------------------------------ removal
    def remove_train_route_run(self, train_id: str, reason: str) -> None:
        """Tear down a RouteRun for `pending_route_abort` / `pending_power_off`.

        Per design_new.md "RouteRun" section: if the controller is
        RUNNING_AT_STATION, exit dwell via TrainController.finish_dwell() before
        removing the run. The caller owns the dispatcher cancel step (or the
        no-active-path skip).
        """
        controller = self._controller_by_id(train_id)
        if controller is not None and controller.state == TrainState.RUNNING_AT_STATION:
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

        # Unassign train from the route.
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
            # Clear assigned_route_id when it matches.
            try:
                if getattr(train, "assigned_route_id", None) == route_id:
                    train.assigned_route_id = None
            except Exception:
                pass

        # Drop the run.
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
            writer of that field is Phase 1's authority-acquisition step.

        v1 Note: the legacy Route still uses Waypoint entries (no
        `task_kind` discriminator yet). Treat each waypoint as a `stop` task
        whose resolved target is the platform's primary block; the design_new
        contract requires the helper to return waypoint/stop/dwell, but until
        the Route schema migration adds `RouteTask` we surface every waypoint
        as `stop`.
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
        waypoint_count = route.get_waypoint_count()
        if waypoint_count == 0:
            return None

        # Find the next non-finished task index according to the route mode and
        # current cursor. The legacy advance helper handles ROUND_TRIP /
        # CIRCULAR mechanics; mirror that logic here without mutating
        # exec_state.
        next_index = self._compute_next_task_index(
            route, exec_state, waypoint_count
        )
        if next_index is None:
            return None

        waypoint = route.get_waypoint_at(next_index)
        if waypoint is None:
            return None

        # Resolve a concrete platform-end block for this waypoint.
        target_block = self._get_platform_block_id(waypoint.platform_id)
        if target_block is None:
            # Cannot resolve a concrete target; surface projection without a
            # resolved_target so caller can decide what to do.
            return ResolvedNextTaskProjection(
                task_index=next_index,
                task_kind="stop",
                direction="forward"
                if exec_state.direction >= 0
                else "reverse",
                resolved_target=None,
            )

        # Block-relative direction: legacy schema has no direction information,
        # so derive a deterministic value from the train's current effective
        # direction or default to "forward".
        derived_dir = self._derive_block_direction(
            train, target_block, exec_state.direction
        )

        resolved = ResolvedRouteTarget(
            requested_destination_block_id=target_block,
            requested_destination_direction=derived_dir,
        )

        # Honor the caller-provided `exclude` set (list of ResolvedRouteTarget
        # instances). For station/multi-platform tasks the caller iterates
        # candidates; the legacy schema only has a single platform per
        # waypoint, so a present exclude with this resolved value means we
        # have nothing else to try this tick.
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
            task_kind="stop",
            direction="forward" if exec_state.direction >= 0 else "reverse",
            resolved_target=resolved,
        )

    def _compute_next_task_index(
        self,
        route: "Route",
        exec_state: RouteExecutionState,
        waypoint_count: int,
    ) -> Optional[int]:
        """Compute the next task index without mutating exec_state."""
        if waypoint_count == 0:
            return None

        idx = exec_state.current_waypoint_index
        # If the cursor is still pointing at a waypoint that has not been
        # consumed (the "armed next task"), return that index. The legacy code
        # base advances `current_waypoint_index` at consumption time; if the
        # cursor still reads a valid index that has not advanced past
        # waypoint_count, that index is the armed target.
        if 0 <= idx < waypoint_count:
            return idx

        # ONE_OFF: we are past the last waypoint → terminal.
        if route.mode == RouteMode.ONE_OFF:
            if idx >= waypoint_count:
                return None
            return idx

        # CIRCULAR: wrap.
        if route.mode == RouteMode.CIRCULAR:
            return idx % waypoint_count

        # ROUND_TRIP: reflect.
        if route.mode == RouteMode.ROUND_TRIP:
            if idx >= waypoint_count:
                return waypoint_count - 2 if waypoint_count > 1 else 0
            if idx < 0:
                return 1 if waypoint_count > 1 else 0
            return idx
        return None

    def _derive_block_direction(
        self,
        train: "Train",
        block_id: str,
        signed_direction: int,
    ) -> str:
        """Best-effort block-relative direction.

        v1 design requires `forward` / `reverse` derived from
        `Block.ordered_tiles`; until that resolver is wired through here, fall
        back to `forward` and rely on dispatcher to recompute the terminal
        frame internally.
        """
        # Default to "forward"; dispatcher consumes this only for diagnostic /
        # request-contract purposes and recomputes the terminal frame from
        # block geometry.
        return "forward"

    # ------------------------------------------------------------------ legacy
    # The helpers below are retained for callers/tests that still drive the
    # legacy waypoint-based execution surface (platform lookup, station-group
    # selection, etc). They are read-mostly and side-effect-free except where
    # explicitly noted.

    def _get_active_route_train_pairs(self) -> List[Tuple["Train", "Route"]]:
        """Collect active (train, route) pairs with compatibility fallback."""
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

        # Backward-compatible fallback: single train assignment on Train.
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

    def _advance_to_next_waypoint(
        self, route: "Route", exec_state: RouteExecutionState
    ) -> bool:
        """Advance per-train waypoint index based on route mode.

        Mutates `exec_state.current_waypoint_index` and `exec_state.direction`.
        """
        waypoint_count = route.get_waypoint_count()
        if waypoint_count == 0:
            return False

        if route.mode == RouteMode.ONE_OFF:
            exec_state.current_waypoint_index += 1
            exec_state.resolved_platform_id = None
            route.current_waypoint_index = exec_state.current_waypoint_index
            return exec_state.current_waypoint_index < waypoint_count

        if route.mode == RouteMode.CIRCULAR:
            exec_state.current_waypoint_index += 1
            if exec_state.current_waypoint_index >= waypoint_count:
                exec_state.current_waypoint_index = 0
            exec_state.resolved_platform_id = None
            route.current_waypoint_index = exec_state.current_waypoint_index
            return True

        # ROUND_TRIP
        exec_state.current_waypoint_index += exec_state.direction
        if exec_state.current_waypoint_index >= waypoint_count:
            exec_state.direction = -1
            exec_state.current_waypoint_index = waypoint_count - 2
            if exec_state.current_waypoint_index < 0:
                exec_state.current_waypoint_index = 0
        elif exec_state.current_waypoint_index < 0:
            exec_state.direction = 1
            exec_state.current_waypoint_index = 1
            if exec_state.current_waypoint_index >= waypoint_count:
                exec_state.current_waypoint_index = 0

        exec_state.resolved_platform_id = None
        route.current_waypoint_index = exec_state.current_waypoint_index
        route.direction = exec_state.direction
        return True

    def _resolve_target_platform(
        self,
        train: "Train",
        waypoint: "Waypoint",
        exec_state: RouteExecutionState,
    ) -> Optional[str]:
        """Resolve flexible platform target for current waypoint."""
        if not waypoint.flexible_platform:
            return waypoint.platform_id

        if exec_state.resolved_platform_id and self._is_platform_available(
            exec_state.resolved_platform_id, exec_state.train_id
        ):
            return exec_state.resolved_platform_id

        target_platform = self._get_platform(waypoint.platform_id)
        if not target_platform:
            return waypoint.platform_id

        station_group = self._find_station_group_for_platform(waypoint.platform_id)
        if not station_group:
            return waypoint.platform_id

        compatible = self._find_compatible_platforms(
            station_group,
            waypoint,
            train,
            exec_state,
            exec_state.train_id,
            target_platform,
        )
        if not compatible:
            return waypoint.platform_id
        if waypoint.platform_id in compatible:
            return waypoint.platform_id
        return compatible[0]

    def _find_station_group_for_platform(self, platform_id: str) -> Optional[object]:
        """Find logical station group containing platform_id, if present."""
        for station in getattr(self.network, "station_groups", {}).values():
            if hasattr(station, "platform_ids") and platform_id in station.platform_ids:
                return station
        return None

    def _find_compatible_platforms(
        self,
        station_group: object,
        waypoint: "Waypoint",
        train: "Train",
        exec_state: RouteExecutionState,
        train_id: str,
        target_platform: object,
    ) -> List[str]:
        """Find compatible platforms in a station group for flexible routing."""
        compatible: List[str] = []
        preserve_direction: Optional[Direction] = None
        if waypoint.travel_direction_policy == TravelDirectionPolicy.PRESERVE:
            preserve_direction = self._get_preserved_travel_direction(
                train,
                exec_state,
                target_platform,
            )

        for platform_id in getattr(station_group, "platform_ids", []):
            platform = self._get_platform(platform_id)
            if not platform:
                continue
            if not self._is_platform_available(platform_id, train_id):
                continue
            if not self._is_side_compatible(
                platform,
                target_platform,
                waypoint.required_platform_side,
            ):
                continue
            if waypoint.travel_direction_policy == TravelDirectionPolicy.PRESERVE:
                if not self._is_platform_direction_compatible(
                    train,
                    exec_state,
                    platform,
                    preserve_direction,
                ):
                    continue
            compatible.append(platform_id)
        return compatible

    def _get_platform_center(self, platform: object) -> Optional[Tuple[float, float]]:
        """Get center tile/position for platform-like object."""
        get_center_position = getattr(platform, "get_center_position", None)
        if callable(get_center_position):
            center = get_center_position()
            if (
                isinstance(center, tuple)
                and len(center) == 2
                and isinstance(center[0], (int, float))
                and isinstance(center[1], (int, float))
            ):
                return (float(center[0]), float(center[1]))

        positions = list(getattr(platform, "positions", []))
        if not positions:
            return None
        avg_x = sum(x for x, _ in positions) / len(positions)
        avg_y = sum(y for _, y in positions) / len(positions)
        return (avg_x, avg_y)

    def _direction_from_points(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
    ) -> Optional[Direction]:
        """Convert a 2D vector into dominant cardinal direction."""
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if dx == 0 and dy == 0:
            return None
        if abs(dx) > abs(dy):
            return Direction.EAST if dx > 0 else Direction.WEST
        return Direction.NORTH if dy > 0 else Direction.SOUTH

    def _direction_between_platforms(
        self, from_platform: object, to_platform: object
    ) -> Optional[Direction]:
        """Get dominant travel direction from one platform center to another."""
        from_center = self._get_platform_center(from_platform)
        to_center = self._get_platform_center(to_platform)
        if not from_center or not to_center:
            return None
        return self._direction_from_points(from_center, to_center)

    def _get_preserved_travel_direction(
        self,
        train: "Train",
        exec_state: RouteExecutionState,
        target_platform: object,
    ) -> Optional[Direction]:
        """Get preferred direction for PRESERVE policy."""
        if exec_state.last_waypoint_platform_id:
            previous_platform = self._get_platform(exec_state.last_waypoint_platform_id)
            if previous_platform:
                direction = self._direction_between_platforms(
                    previous_platform,
                    target_platform,
                )
                if direction is not None:
                    return direction

        return train.get_effective_direction()

    def _is_platform_direction_compatible(
        self,
        train: "Train",
        exec_state: RouteExecutionState,
        candidate_platform: object,
        preserve_direction: Optional[Direction],
    ) -> bool:
        """Check candidate against preserve-direction requirement."""
        if preserve_direction is None:
            return True

        candidate_direction: Optional[Direction] = None
        if exec_state.last_waypoint_platform_id:
            previous_platform = self._get_platform(exec_state.last_waypoint_platform_id)
            if previous_platform:
                candidate_direction = self._direction_between_platforms(
                    previous_platform,
                    candidate_platform,
                )
        elif train.position:
            candidate_tile = self._get_target_platform_tile(train, candidate_platform)
            if candidate_tile:
                candidate_direction = self._direction_from_points(
                    (
                        float(train.position.head_tile[0]),
                        float(train.position.head_tile[1]),
                    ),
                    (float(candidate_tile[0]), float(candidate_tile[1])),
                )

        if candidate_direction is None:
            return False
        return candidate_direction == preserve_direction

    def _is_platform_available(self, platform_id: str, train_id: str) -> bool:
        """Check if at least one block in the platform is available."""
        platform = self._get_platform(platform_id)
        if not platform:
            return False

        block_ids = set()
        get_all_assigned_blocks = getattr(platform, "get_all_assigned_blocks", None)
        if callable(get_all_assigned_blocks):
            assigned_blocks = get_all_assigned_blocks()
            if isinstance(assigned_blocks, (list, set, tuple)):
                block_ids.update(assigned_blocks)
        for x, y in getattr(platform, "positions", []):
            track = self.network.get_track(x, y)
            if track and track.block_section_id:
                block_ids.add(track.block_section_id)

        if not block_ids:
            return True

        for block_id in block_ids:
            block = self.network.blocks.get(block_id)
            if not block:
                continue
            if block.is_free() or (
                block.is_reserved() and block.occupied_by == train_id
            ):
                return True
        return False

    def _is_side_compatible(
        self,
        candidate: object,
        target: object,
        required_side: PlatformSide,
    ) -> bool:
        """Check platform side compatibility for flexible selection."""
        candidate_side = getattr(candidate, "platform_side", None)
        target_side = getattr(target, "platform_side", None)
        if candidate_side is None or target_side is None:
            return True

        candidate_side_value = (
            candidate_side.value
            if hasattr(candidate_side, "value")
            else str(candidate_side)
        )
        target_side_value = (
            target_side.value if hasattr(target_side, "value") else str(target_side)
        )

        def to_side_set(side_value: str) -> set:
            if side_value == "both":
                return {"left", "right"}
            if side_value == "left":
                return {"left"}
            if side_value == "right":
                return {"right"}
            return {"left", "right"}

        candidate_sides = to_side_set(candidate_side_value)
        target_sides = to_side_set(target_side_value)

        if required_side == PlatformSide.EITHER:
            return candidate_sides.issubset(target_sides)
        if required_side == PlatformSide.BOTH:
            return candidate_sides == {"left", "right"} and candidate_sides.issubset(
                target_sides
            )
        if required_side == PlatformSide.LEFT:
            return "left" in candidate_sides and candidate_sides.issubset(target_sides)
        if required_side == PlatformSide.RIGHT:
            return "right" in candidate_sides and candidate_sides.issubset(target_sides)
        return True

    def _is_train_at_platform(self, train: "Train", platform_id: str) -> bool:
        """Check if train head is at target platform tile."""
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
        """Convert dispatcher path tiles into train path tuples."""
        from path_utils import convert_tiles_to_train_path

        tiles = getattr(path, "path_tiles", [])
        if len(tiles) == 0:
            return []
        if len(tiles) == 1:
            raise ValueError("Cannot determine train direction for single-tile path")
        return convert_tiles_to_train_path(tiles)

    def set_train_waypoint_index(self, train_id: str, waypoint_index: int) -> bool:
        """Override current waypoint index for an assigned train."""
        train = self.network.trains.get(train_id)
        if not train or not train.assigned_route_id:
            return False

        route = getattr(self.network, "routes", {}).get(train.assigned_route_id)
        if not route:
            return False
        if waypoint_index < 0 or waypoint_index >= route.get_waypoint_count():
            return False

        exec_state = self.execution_states.get(train_id)
        if not exec_state or exec_state.route_id != route.route_id:
            exec_state = RouteExecutionState(
                train_id=train_id,
                route_id=route.route_id,
                route_start_time=self._now(),
            )
            self.execution_states[train_id] = exec_state

        exec_state.current_waypoint_index = waypoint_index
        exec_state.arrived_at_platform_time = None
        exec_state.resolved_platform_id = None
        exec_state.waiting_for_path = False
        return True
