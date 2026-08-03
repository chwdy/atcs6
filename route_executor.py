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

from constants import (
    DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS,
    IDLE_HOLD_COOLDOWN_TICKS,
    IDLE_HOLD_RELEASE_TICKS,
)
from enums import ControllerCondition, Direction, TrainState
from train_controller import TrainController, create_controller_for_train
from train_route import (
    RouteMode,
    RouteRunStatus,
    RouteStartMode,
    RouteTask,
    RouteTaskRun,
)
from runtime_payloads import AcquisitionMode, ErrorRecord, FatalConflictKind, FatalPhysicalEntryConflict, PathAcquisitionFailureKind, PathDispatcherForRouteExecutor, ResolvedRouteTarget, ResourceEvent, TargetResolutionFailureKind

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
    every station platform unavailable). Phase 1 writes this string directly
    as the `ErrorRecord` of one route-run error event (inlined in
    `tick_train_phase1`) so the operator sees why the task is stalled instead
    of an empty diagnostic. `None` for dwell projections and for the same-tick exclude
    sentinel (all candidates already attempted) — both of those non-None-
    target paths do not need a fresh resolver-level error record.
    """

    walk_position: int
    task_kind: str  # "waypoint" | "stop" | "dwell"
    direction: str  # "forward" | "reverse"
    resolved_target: Optional[ResolvedRouteTarget] = None
    failure_reason: Optional[str] = None
    # Structured cause when `resolved_target is None` for a non-dwell task:
    # `MISSING_TARGET` (broken — target ID absent from the network) drives the
    # broken-task skip; `TARGET_UNAVAILABLE` (present but disabled/manual, or
    # all station platforms filtered/held) drives the unreachable-task wait.
    # Per design_new.md "Unreachable and broken task handling". `None` for
    # dwell projections and the same-tick exclude sentinel.
    failure_kind: Optional["TargetResolutionFailureKind"] = None


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
    # v1 latch consumed by Phase-1 route-deviation cleanup. With
    # `path_cancel_was_deviation` it drives the derived `display_state`
    # projection (DEVIATED / CANCELLED while True).
    path_cancelled: bool = False
    # Classification of the active `path_cancelled` latch (design_new.md:1456):
    # True when the cancellation was a movement-derived deviation, False for
    # every other authority cancellation (operator cancel, revoke, EB,
    # power-off). Written by `handle_path_cancellation(...)` from its
    # `was_deviation` argument; cleared together with `path_cancelled` by
    # Phase-1 deviation cleanup. Drives the `display_state`
    # DEVIATED-vs-CANCELLED split.
    path_cancel_was_deviation: bool = False
    # Phase-1-maintained flag (NOT a latch): set when every task on a
    # CIRCULAR / ROUND_TRIP walk is broken (target ID missing from the
    # network), cleared by the next Phase 1 pass that finds a valid task.
    # Drives the derived `display_state == "STALLED"` projection. Always
    # False for ONE_OFF runs (they finish instead). Per design_new.md
    # "Unreachable and broken task handling".
    no_reachable_task: bool = False
    # Per-run lifecycle state. Stored as the canonical `RouteRunStatus` enum
    # (RUNNING / FINISHED / DEACTIVATED). Deviation is NOT a stored state;
    # consumers that need the diagnostic read `display_state` instead.
    current_route_status: RouteRunStatus = RouteRunStatus.RUNNING

    # Sparse per-run overrides of allowlisted route-level operational fields.
    # Per design_new.md "RouteRun overrides" canonical home: keys are field
    # names from the v1 override allowlist
    # (`"priority"`, `"acquisition_mode"`);
    # a key being absent means "use the shared `Route` value for that field."
    # Reads go through the effective_* helpers below, which resolve the shared
    # Route live each access so later operator edits to either the run-level
    # override or the shared Route take effect on the next read.
    overrides: Dict[str, Any] = field(default_factory=dict)
    # Diagnostic-only audit timestamp; bumped by RouteExecutor.set_run_override /
    # clear_run_override per design_new.md. Not consumed for control flow.
    overrides_updated_at: Optional[float] = None
    # Resolved start mode actually used to seed this run's starting walk-position
    # (per design_new.md "Run start-mode seeding"). Written once at allocation by
    # `RouteExecutor._seed_start_position(...)`; diagnostic only (surfaced in the
    # run-record payload). NOT a live override — start mode has no meaning after
    # the run's starting position is seeded.
    start_mode: Optional[RouteStartMode] = None
    # Walk-position this run was seeded to plan toward first — its first real
    # movement target (per design_new.md "Route-run state"). 0 for FIRST; the
    # seeded position `s` for NEAREST / SPECIFIC. Written once at allocation by
    # `RouteExecutor._seed_start_position(...)`. Read only by `is_starting`.
    start_walk_position: int = 0
    # Idle-hold release counters (NOT latches), Phase-1-maintained by direct
    # write in the authority-acquisition step. `idle_hold_ticks` counts
    # consecutive Phase-1 ticks on a stalled truncated best-effort hold;
    # `release_cooldown_ticks` is the post-release acquisition-skip countdown.
    # Per design_new.md "Idle-hold maintenance and release" (Planning Kernel
    # step 66aa).
    idle_hold_ticks: int = 0
    release_cooldown_ticks: int = 0

    @property
    def run_id(self) -> str:
        """This run's id as an event entity.

        A run is identified by its train, its route and its run number — it has
        no id column of its own — so the entity id joins the three. It is the
        `entity_id` of a `"route_run"` ref, and the prefix of a
        `"walk_position"` ref (`"<run_id>#<index>"`), so a run-level filter and
        a task-level filter both find an event about a task (design_new.md
        "Runtime notifications").
        """
        return f"{self.train_id}:{self.route_id}:{self.run_number}"

    # ------------------------------------------------------------------
    # Effective-value read helpers (per design_new.md "RouteRun overrides")
    # ------------------------------------------------------------------

    OVERRIDE_ALLOWLIST: ClassVar[Tuple[str, ...]] = (
        "priority",
        "acquisition_mode",
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

        Per design_new.md ``current_route_status`` schema (the ``display_state``
        row), in priority order: (1) when ``current_route_status`` is terminal
        (``FINISHED`` / ``DEACTIVATED``), the terminal state — even if
        ``path_cancelled`` is still set on the historical row; (2) else
        ``"DEVIATED"`` when ``path_cancelled`` and ``path_cancel_was_deviation``,
        ``"CANCELLED"`` when ``path_cancelled`` and not; (3) else ``"STALLED"``
        when ``no_reachable_task``; (4) otherwise the stored
        ``current_route_status.value``. Not a stored field; no writers. Runtime
        branching never keys on this value.
        """
        if self.current_route_status in (
            RouteRunStatus.FINISHED,
            RouteRunStatus.DEACTIVATED,
        ):
            return self.current_route_status.value
        if self.path_cancelled:
            return "DEVIATED" if self.path_cancel_was_deviation else "CANCELLED"
        if self.no_reachable_task:
            return "STALLED"
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

    @property
    def is_starting(self) -> bool:
        """Derived: True while this run is still completing its first real
        movement target (design_new.md "Route-run state" + the runnable-train
        ordering rule in "Per-train tick transaction").

        True while ``next_planning_walk_position <= start_walk_position``;
        flips to False once the train completes the task at the seeded start
        walk-position, and stays False because ``next_planning_walk_position``
        (the first unfinished walk-position) only advances over the run —
        including across CIRCULAR / ROUND_TRIP wrap. ``current_waypoint_index``
        is unsuitable here: it is ``task_index`` space and cycles on wrap.

        Read by ``RouteExecutor.order_runnable_trains`` as the primary phase
        tier (running-phase runs ordered ahead of starting-phase runs).
        """
        return self.next_planning_walk_position <= self.start_walk_position

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
        # Append-only, never-recycled registration index per route_id, used to
        # compose the diagnostic RouteRun.run_number (design_new.md line 1309).
        # A route removed and re-added keeps its original index, and the counter
        # never decreases, so run_numbers stay unique across the registry's life.
        self._route_registry_index: Dict[str, int] = {}
        self._next_route_registry_index: int = 0
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
        # Per-assignment one-shot start-mode input (per design_new.md "Run
        # start-mode seeding"). Keyed by train_id; populated by
        # `train_service.assign_route(...)` when the operator picks a start mode
        # for a specific assignment, consumed+cleared by the next fresh
        # `_ensure_run_for(...)`. Set/cleared on every assign/retarget/unassign
        # so a choice can never leak onto a different route's run.
        self._pending_start_overrides: Dict[str, Dict[str, Any]] = {}
        # Held re-assignment for a same-route structural `Route.tasks` edit (per
        # design_new.md "Shared-route edit teardown"). The structural-edit
        # handler records `train_id -> route_id` here instead of re-adding to
        # `Route.assigned_train_ids` up front, because the Phase-3 deactivate
        # consumer's `remove_train_route_run(...)` unassigns the train from the
        # run's `route_id` as part of the clear -- an up-front re-add would be
        # undone by that unassign. The deactivate consumer applies the held
        # re-assignment AFTER that clear, so the next eligible Phase 1 sees the
        # train assigned with no RUNNING run and allocates a fresh run. Consumed
        # only by the route_deactivate consumer; abort/power-off never record it.
        self._pending_reassign: Dict[str, str] = {}

        # Per-tick coordinator state (design_new.md "Contention snapshot" +
        # "Meet coordination"). Read-only tick-start views rebuilt every tick by
        # `build_tick_coordination(...)` (after order_runnable_trains, before the
        # per-train loop); retained across no ticks, so they are not latches and
        # hold no Latch-Table entry.
        #   contention_snapshot: block_id -> (train_id, effective_priority) of the
        #     single highest-priority train blocked on (waiting_for) that block.
        #   meet_assignment: train_id -> MeetCeiling capping how far it may
        #     reserve this tick when a higher-priority train needs the same track.
        self.contention_snapshot: Dict[str, Tuple[str, int]] = {}
        self.meet_assignment: Dict[str, "MeetCeiling"] = {}

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
        # Assign a stable append-only index on first registration only; a
        # re-registered route_id keeps its original index (never recycled).
        if route.route_id not in self._route_registry_index:
            self._route_registry_index[route.route_id] = self._next_route_registry_index
            self._next_route_registry_index += 1

    def unregister_route(self, route_id: str) -> Optional["Route"]:
        """Remove a Route from the registry; return the removed Route or None."""
        return self.route_registry.pop(route_id, None)

    # --------------------------------------------------- canonical accessors
    def execution_state_for(
        self, train_id: str
    ) -> Optional[RouteExecutionState]:
        """Return the train's currently-RUNNING `RouteRun`, or ``None``.

        Per design_new.md: a derived read keyed on
        ``current_route_status == RUNNING``. Phases 1, 2, 3, and route
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
          1. Prefer the live RouteRun in `execution_states[train_id]` (the live
             RUNNING run; terminal runs are archived out of the slot
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

    def route_run_summary_for(self, train_id: str) -> Optional[Dict[str, Any]]:
        """Five-field summary of the train's currently-`RUNNING` `RouteRun`,
        or ``None`` when the train has no RUNNING run.

        Per design_new.md "Data Flow To UI" (~line 2309), the composite
        network/train snapshot exposes `route_runs_by_train_id` mapping
        `train_id -> {route_id, run_number, state, display_state, last_error}`
        for the RUNNING run, where `last_error` is the derived
        `NotificationCenter.latest_error_for(...)` read rather than stored
        state (design_new.md "Error events", Reading the current reason).
        for the RUNNING run. The live run lives in `execution_states[train_id]`
        (the live RUNNING run; terminal runs are archived out of the slot); only a
        run whose stored `current_route_status` is `RUNNING` is reported here.
        """
        run = self.execution_states.get(train_id)
        if run is None or run.current_route_status != RouteRunStatus.RUNNING:
            return None
        return {
            "route_id": run.route_id,
            "run_number": run.run_number,
            "state": run.current_route_status.value,
            "display_state": run.display_state,
            "last_error": self.latest_run_error(run),
        }

    def latest_run_error(self, run: RouteExecutionState) -> Optional[Dict[str, Any]]:
        """The most recent unresolved-looking error event for this run, as the
        `{text, origin}` dict the UI renders, or `None`.

        A derived read over the one event stream — no entity stores an error
        (design_new.md "Error events"). Nothing marks an event resolved, so a
        surface that must not show a stale reason gates on live state.
        """
        sc = getattr(self, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is None:
            return None
        try:
            found = nc.latest_error_for("route_run", run.run_id)
        except Exception:
            return None
        if found is None or found.error is None:
            return None
        return found.error.to_dict()

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

        Per design_new.md line 1309: ``route_registry_index * 1_000_000 +
        run_count``. ``route_registry_index`` is the route's append-only
        registration order (assigned in ``register_route``, never recycled or
        compacted on route removal), so the diagnostic run_number stays unique
        across the registry's lifetime. Falls back to 0 when the route is not
        yet registered.
        """
        route_registry_index = self._route_registry_index.get(route_id, 0)
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
        """Order candidate trains by run phase then route priority, stable ties.

        Per design_new.md "Per-train tick transaction" (the canonical ordering
        rule): sort first by run phase — running-phase runs (`is_starting`
        False) ahead of starting-phase runs (`is_starting` True) — then by
        descending effective priority within each tier (route-managed trains use
        the per-run override or the shared `Route.priority`; trains without a
        current RouteRun are running-tier at effective priority 5). Stable
        existing iteration order is the only tie-break.
        """
        routes = self.route_registry
        ordered = list(candidate_train_ids)

        def sort_key(train_id: str) -> Tuple[int, int]:
            # Per design_new.md "RouteRun overrides": the per-run override on
            # `priority` wins over the shared `Route.priority` when set.
            run = self.execution_state_for(train_id)
            route_id = self.assigned_route_id_for(train_id)
            route = routes.get(route_id) if route_id else None
            if run is not None:
                priority = run.effective_priority(route=route, route_executor=self)
                starting = run.is_starting
            elif route is not None:
                priority = int(getattr(route, "priority", 5))
                starting = False
            else:
                priority = 5
                starting = False
            # Ascending stable sort: tier 0 (running) before tier 1 (starting);
            # `-priority` puts higher effective priority first within a tier.
            return (1 if starting else 0, -priority)

        ordered.sort(key=sort_key)
        return ordered

    # ------------------------------------------------------------------
    # Per-tick coordination (design_new.md "Contention snapshot" +
    # "Meet coordination"). Built in the tick preamble after
    # order_runnable_trains and before the first train's Phase 1.
    # ------------------------------------------------------------------

    def build_tick_coordination(self, ordered_train_ids: List[str]) -> None:
        """Rebuild the contention snapshot and meet assignment for this tick.

        design_new.md "Contention snapshot" + "Meet coordination": both are
        read-only tick-start views — recomputed from scratch every tick and
        retained across none. The snapshot is built first; meet coordination
        seeds its conflict detection from the snapshot's contending pairs.
        """
        self.contention_snapshot = self._build_contention_snapshot(ordered_train_ids)
        self.meet_assignment = self._build_meet_assignment(ordered_train_ids)

    def _build_contention_snapshot(
        self, ordered_train_ids: List[str]
    ) -> Dict[str, Tuple[str, int]]:
        """`block_id -> (train_id, effective_priority)` for every block on which
        some train is blocked from growing its authority, recording the single
        highest-`effective_priority` waiter (ties broken by the stable
        `order_runnable_trains` order). design_new.md "Contention snapshot".
        """
        snapshot: Dict[str, Tuple[str, int]] = {}
        for train_id in ordered_train_ids:
            blocked_on = self._waiting_for_block_id(train_id)
            if not blocked_on:
                continue
            eff_pri = self._effective_priority_for(train_id)
            current = snapshot.get(blocked_on)
            # Strictly-higher priority replaces; equal keeps the earlier (the
            # stable order_runnable_trains tie-break, since we iterate in that
            # order). So the entry is the highest-priority waiter on the block.
            if current is None or eff_pri > current[1]:
                snapshot[blocked_on] = (train_id, eff_pri)
        return snapshot

    def _build_meet_assignment(
        self, ordered_train_ids: List[str]
    ) -> Dict[str, "MeetCeiling"]:
        """Per-tick `train_id -> MeetCeiling` (design_new.md "Meet coordination").

        Delegated to `MeetCoordinator`, which reads the just-built
        `contention_snapshot` to seed conflict detection.
        """
        from meet_coordination import MeetCoordinator

        return MeetCoordinator(self).build(ordered_train_ids)

    def _effective_priority_for(self, train_id: str) -> int:
        """`RouteRun.effective_priority` for a train, mirroring the rule
        `order_runnable_trains` uses: per-run override, else shared
        `Route.priority`, else 5 for a train with no current RouteRun."""
        run = self.execution_state_for(train_id)
        route_id = self.assigned_route_id_for(train_id)
        route = self.route_registry.get(route_id) if route_id else None
        if run is not None:
            return int(run.effective_priority(route=route, route_executor=self))
        if route is not None:
            return int(getattr(route, "priority", 5))
        return 5

    def _waiting_for_block_id(self, train_id: str) -> Optional[str]:
        """The block this train's active path is blocked on (the
        `waiting_for_block_id` live recompute), or None."""
        if self.path_dispatcher is None:
            return None
        return self.path_dispatcher.get_waiting_for_block_id(train_id)

    def _held_future_reservations(self, train_id: str) -> set:
        """Block ids this train holds as not-yet-entered future reservations
        (reserved-ahead, not occupied) — the set the contention-driven release
        checks against the snapshot."""
        bm = getattr(self.network, "block_manager", None)
        if bm is None:
            return set()
        return set(bm.reserved_blocks_for_train(train_id))

    def _contention_release_due(self, train_id: str) -> bool:
        """True when a strictly-higher-priority run is blocked on a block this
        run holds as a not-yet-entered future reservation.

        design_new.md "Contention snapshot" (line 1544) — the contention-driven
        release branch of Planning Kernel step 66aa. Reads the tick-start
        `contention_snapshot` (highest-priority waiter per block) against this
        run's reserved-ahead corridor.
        """
        if not self.contention_snapshot:
            return False
        my_pri = self._effective_priority_for(train_id)
        for block_id in self._held_future_reservations(train_id):
            entry = self.contention_snapshot.get(block_id)
            if entry is not None and entry[0] != train_id and entry[1] > my_pri:
                return True
        return False

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
        allowlist (`priority`, `acquisition_mode`)
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
        is_fresh = exec_state is None or exec_state.route_id != route.route_id
        if is_fresh:
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
        # Seed the starting walk-position per the run's effective start mode
        # (design_new.md "Run start-mode seeding"). Only for a freshly-allocated
        # run, and only after _sync_task_runs has populated task_runs.
        if is_fresh:
            self._seed_start_position(train, route, exec_state)
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

    # ------------------------------------------------------------------ start mode
    def set_pending_start_override(
        self,
        train_id: str,
        start_mode: RouteStartMode,
        start_task_index: Optional[int],
    ) -> None:
        """Stash a one-shot start-mode override consumed by `train_id`'s next
        fresh `RouteRun` (design_new.md "Run start-mode seeding"). Canonical
        writer for the pending start-override bundle."""
        self._pending_start_overrides[train_id] = {
            "start_mode": start_mode,
            "start_task_index": start_task_index,
        }

    def clear_pending_start_override(self, train_id: str) -> None:
        """Drop any stashed start-mode override for `train_id` (no-op if none),
        so an assign/retarget/unassign never reuses an earlier choice."""
        self._pending_start_overrides.pop(train_id, None)

    def _resolve_start_policy(
        self, train_id: str, route: "Route"
    ) -> Tuple[RouteStartMode, Optional[int]]:
        """Resolve `(start_mode, start_task_index)` for a fresh run.

        Per design_new.md "Run start-mode seeding": the per-assignment one-shot
        input wins if present, else the route-level default. Consumes (pops) the
        pending bundle so a per-assignment choice applies to exactly one run.
        """
        pending = self._pending_start_overrides.pop(train_id, None)
        if pending and isinstance(pending.get("start_mode"), RouteStartMode):
            return pending["start_mode"], pending.get("start_task_index")
        return (
            getattr(route, "start_mode", RouteStartMode.FIRST),
            getattr(route, "start_task_index", None),
        )

    def _compute_start_task_index(
        self,
        train: "Train",
        route: "Route",
        mode: RouteStartMode,
        specific_index: Optional[int],
    ) -> Optional[int]:
        """Task index the run should start at, or `None` to fall back to FIRST.

        NEAREST defers to `RouteTargetResolver.nearest_start_stop_index(...)`.
        SPECIFIC accepts the given index only when it is in range and names a
        `task_kind == "stop"` task. Per design_new.md "Run start-mode seeding".
        """
        if mode == RouteStartMode.NEAREST:
            return self._get_route_target_resolver().nearest_start_stop_index(
                train, route
            )
        if mode == RouteStartMode.SPECIFIC:
            tasks = getattr(route, "tasks", []) or []
            if (
                specific_index is not None
                and 0 <= specific_index < len(tasks)
                and getattr(tasks[specific_index], "task_kind", None) == "stop"
            ):
                return specific_index
            return None
        return None

    def _seed_start_position(
        self, train: "Train", route: "Route", exec_state: RouteExecutionState
    ) -> None:
        """Seed a fresh run's starting walk-position per its effective start
        mode (design_new.md "Run start-mode seeding").

        FIRST leaves the run at `task_runs[0]`. NEAREST / SPECIFIC pre-finish
        the skipped prefix `[0, s)` by setting each row's `finished_at` to the
        run's `started_at`, so `next_planning_index` lands on the chosen stop.
        The chosen index `s` is always a forward-leg position (`s <=
        len(tasks) - 1`), so the apex is never pre-finished, no walk-append
        fires, and `direction` stays `"forward"`. Records the resolved mode on
        the run for the run-record payload; falls back to FIRST (no seeding)
        when NEAREST finds no reachable stop or SPECIFIC names an invalid index.
        """
        mode, specific_index = self._resolve_start_policy(exec_state.train_id, route)
        exec_state.start_mode = mode
        if mode == RouteStartMode.FIRST:
            return
        s = self._compute_start_task_index(train, route, mode, specific_index)
        if s is None:
            # NEAREST found no reachable stop, or SPECIFIC named an invalid
            # index — fall back to FIRST.
            exec_state.start_mode = RouteStartMode.FIRST
            return
        # s is a valid in-range index; s == 0 means the chosen stop is already
        # the first task, so no prefix needs skipping (mode stays as resolved).
        for i in range(s):
            exec_state.task_runs[i].finished_at = exec_state.started_at
        # Record the seeded first-target walk-position for the `is_starting`
        # phase tier (design_new.md "Run start-mode seeding"). For the initial
        # walk, walk-position == task_index, so this is the seed index `s`;
        # after the prefix-finish above, `next_planning_walk_position == s`.
        exec_state.start_walk_position = s

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
            # Clear the latch and its classification together (design_new.md:1457).
            exec_state.path_cancelled = False
            exec_state.path_cancel_was_deviation = False

        # ---------- 70c3: dwell-complete timer check -------------------------
        # Per design_new.md lines 1826/1828 (EB-while-dwelling rows `0a8e`/`9f74`
        # and the EB-while-dwelling suppression rule): the dwell timer itself is
        # NOT frozen by EB — it is the derived `task_started_at(...)` value,
        # which `eb.activate()` never touches, so it keeps counting toward
        # `should_depart`. What is suppressed is dwell COMPLETION: finish_dwell /
        # depart must not fire while `eb.active == True` (you must not depart a
        # train under EB). Completion is served only once `eb.release()` clears
        # the latch.
        if controller.ops.state == TrainState.RUNNING_AT_STATION and not controller.eb.active:
            self._dwell_complete_check(train, route, exec_state, controller, now)

        # ---------- c481: helper lookup via _find_next_task_target -----------
        projection = self._find_next_task_target(train_id)
        if projection is None:
            # Terminal walk for ONE_OFF; finalize the run directly.
            self.finish_route_run(train_id)
            return

        # Broken-skip a dwell whose own `task_location` has left the network,
        # then re-run the lookup (design_new.md Phase Table, step `c481`). A
        # broken dwell must never reach dwell-entry, or the train would hold on
        # the timer of a station that no longer exists. Bounded by the task
        # count so a CIRCULAR / ROUND_TRIP walk of all-broken tasks falls
        # through to the no-reachable-task stall instead of skipping forever.
        for _ in range(len(route.tasks) or 1):
            if (
                projection is None
                or projection.task_kind != "dwell"
                or projection.failure_kind
                != TargetResolutionFailureKind.MISSING_TARGET
            ):
                break
            self._skip_broken_task(exec_state, route, projection.walk_position)
            projection = self._find_next_task_target(train_id)
        if projection is None:
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

        # ---------- run: timed roll, then a smooth ATC stop ------------------
        # A `run` task keeps the train rolling for `task_time` seconds and then
        # lets it coast to a smooth ATC stop wherever it is (position-agnostic).
        # While rolling it falls through to ordinary authority acquisition; once
        # the timer elapses `_run_task_step` drives the stop and returns True so
        # no fresh authority is taken this tick.
        if projection.task_kind == "run":
            if self._run_task_step(
                train, route, exec_state, controller, projection, now, delta_seconds
            ):
                return

        # ---------- 592a: re-resolve next target (post cursor changes) ------
        projection = self._find_next_task_target(train_id)
        if projection is None or projection.task_kind == "dwell":
            return

        # ---------- 66aa: authority acquisition (create_path / extend_path) -
        if not self._eligible_for_authority_acquisition(controller, exec_state):
            return

        # Live-path target re-resolution invalidation (design_new.md:1716, 1735,
        # case `162a`): when a live path exists and a fresh resolve of the CURRENT
        # walk-position now yields a different target BLOCK than the path was
        # built toward — e.g. a StationTarget re-resolving to a different
        # platform — the already-acquired authority toward the old block is
        # potentially stale. Latch a `revoked` cancel and wait for the ordinary
        # Phase 3 cancel / replan flow instead of extending toward a different
        # block; the same RouteRun is kept (this is NOT a structural
        # Route.tasks edit). Normal waypoint-chaining advances to a fresh
        # walk-position whose stored `resolved_target` is still None, so it
        # never trips this check.
        #
        # A direction-only change for the SAME block does not trip this check.
        # A `task_direction == "either"` task resolves direction against the
        # train's current span each time (design_new.md "RouteTask data"), so
        # the resolved direction can legitimately differ from an earlier
        # resolution purely because the train moved closer under its own live
        # authority — the target block never changed, so the live path is not
        # stale. Fall through and let `extend_path(...)` receive the freshly
        # resolved direction. `extend_path(...)` still rejects a direction that
        # would require reversing at the current authority end
        # (`failure_kind = invalid_reverse`, design_new.md "Path extension"),
        # and `_authority_acquisition_step` latches this same `revoked` cancel
        # through that structural-extend-failure path, so an actually
        # incompatible direction still gets cancelled and replanned.
        active_path_id = self._get_active_path_id(train_id)
        if active_path_id is not None and 0 <= projection.walk_position < len(
            exec_state.task_runs
        ):
            stored_target = exec_state.task_runs[
                projection.walk_position
            ].resolved_target
            new_target = projection.resolved_target
            if (
                stored_target is not None
                and new_target is not None
                and stored_target.requested_destination_block_id
                != new_target.requested_destination_block_id
            ):
                controller.route_hooks.phase_latches.set_path_cancel_request(
                    initiator="revoked",
                    reason=(
                        "live-path target re-resolved for walk-position "
                        f"{projection.walk_position} "
                        f"({stored_target.requested_destination_block_id} -> "
                        f"{new_target.requested_destination_block_id}); "
                        "cancelling stale authority so the next pass replans "
                        "toward the new target"
                    ),
                )
                return

        # Idle-hold release cooldown (design_new.md "Idle-hold maintenance and
        # release"): while cooling down after a release, skip acquisition so the
        # just-freed reservations/locks stay available for higher-priority runs.
        if exec_state.release_cooldown_ticks > 0:
            exec_state.release_cooldown_ticks -= 1
            return

        # Capture the live authority end before this tick's acquisition so the
        # idle-hold maintenance below can tell whether the authority grew.
        _pre_path = (
            self.path_dispatcher.get_active_path(train_id)
            if self.path_dispatcher is not None
            else None
        )
        auth_end_before = (
            _pre_path.get_authority_end_block_id() if _pre_path is not None else None
        )

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
        # apply the broken-vs-unreachable policy below if the whole retry loop
        # exhausts without acquiring authority. Per design_new.md "Unreachable
        # and broken task handling".
        failed_walk_position: Optional[int] = projection.walk_position
        # A BROKEN task has its target ID absent from the network (resolver
        # failure_kind == MISSING_TARGET). Captured from the initial projection
        # before the retry loop reassigns `projection`.
        initial_broken = (
            projection.resolved_target is None
            and projection.failure_kind == TargetResolutionFailureKind.MISSING_TARGET
        )
        any_success = False
        for _attempt in range(_max_iterations):
            if projection is None or projection.task_kind == "dwell":
                break
            # A sentinel projection (resolved_target=None) means the resolver
            # filtered out every candidate (missing target, or disabled /
            # manual / all-platforms-unavailable). Emit the resolver's
            # failure_reason BEFORE breaking the loop so a broken-task skip
            # does not advance the cursor past the task with no diagnostic.
            if projection.resolved_target is None:
                if projection.failure_reason and 0 <= projection.walk_position < len(exec_state.task_runs):
                    self._emit_run_error(
                        exec_state,
                        projection.walk_position,
                        "planning_failed",
                        projection.failure_reason,
                    )
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

        # Idle-hold maintenance + release (design_new.md "Idle-hold maintenance
        # and release", step 66aa). A truncated best-effort hold that neither
        # grew this tick nor has the train moving accrues idle ticks; at the
        # threshold it latches the ordinary cancel and returns True (stop
        # planning this tick — Phase 3 commits the release).
        if self._maintain_idle_hold(train_id, controller, exec_state, auth_end_before):
            return

        if any_success:
            # A valid task was served this tick → the walk is not all-broken.
            exec_state.no_reachable_task = False
            return

        # No live authority was created. Apply the broken-vs-unreachable policy
        # (design_new.md "Unreachable and broken task handling"). It fires only
        # when there is no active path and the failed walk-position is still
        # unfinished.
        if (
            failed_walk_position is None
            or not (0 <= failed_walk_position < len(exec_state.task_runs))
            or exec_state.task_runs[failed_walk_position].finished_at is not None
        ):
            return
        try:
            has_active_path = (
                controller.path_projection.received_path_plan is not None
            )
        except Exception:
            has_active_path = False
        if has_active_path:
            return

        if not initial_broken:
            # Unreachable task (target present but disabled / manual / blocked):
            # WAIT — leave the task armed. The diagnostic was already emitted by the
            # resolver sentinel branch or the acquisition step. The current task
            # is itself a valid (present) task, so the walk is not all-broken.
            exec_state.no_reachable_task = False
            return

        # Broken task (target missing from the network).
        if self._walk_has_valid_task(exec_state, route, failed_walk_position):
            # A valid task remains ahead → skip the broken one toward it.
            exec_state.no_reachable_task = False
            self._skip_broken_task(exec_state, route, failed_walk_position)
            return

        if route is not None and route.mode == RouteMode.ONE_OFF:
            # No valid task left: skip the broken task; the next Phase 1 sees
            # the terminal walk and finishes the run (having served nothing).
            exec_state.no_reachable_task = False
            self._skip_broken_task(exec_state, route, failed_walk_position)
            return

        # CIRCULAR / ROUND_TRIP with no valid task anywhere on the walk: the
        # walk never goes terminal, so do not keep skipping / appending. Stall:
        # hold idle, record the diagnostic, and warn on the False->True edge.
        if not exec_state.no_reachable_task:
            self._emit_run_error(
                exec_state,
                failed_walk_position,
                "run_stalled",
                (
                    "route run stalled: no reachable task "
                    "(every task target is missing)"
                ),
            )
            exec_state.no_reachable_task = True
            self._emit_route_warning(
                exec_state.train_id,
                "run_stalled",
                "every task target on the route is missing from the network",
            )

    def _walk_has_valid_task(
        self,
        exec_state: "RouteExecutionState",
        route: "Route",
        from_walk_position: int,
    ) -> bool:
        """True when the directed walk still contains a *valid* task — one
        whose `task_location` target ID is present in the network (broken or
        not). For ONE_OFF, scans `Route.tasks` from the current task_index
        onward; for CIRCULAR / ROUND_TRIP, scans all of `Route.tasks` because
        the walk cycles. Per design_new.md "Unreachable and broken task
        handling".
        """
        tasks = getattr(route, "tasks", None) if route is not None else None
        if not tasks:
            return False
        resolver = self._get_route_target_resolver()
        if route.mode == RouteMode.ONE_OFF:
            try:
                start_idx = exec_state.task_runs[from_walk_position].task_index
            except Exception:
                start_idx = 0
            candidates = tasks[start_idx:]
        else:
            candidates = tasks
        return any(resolver.task_target_present(task) for task in candidates)

    def _skip_broken_task(
        self,
        exec_state: "RouteExecutionState",
        route: "Route",
        walk_position: int,
    ) -> None:
        """Skip a broken task (target ID missing from the network): emit the
        diagnostic, set finished_at, and append the next walk task. Per
        design_new.md "Unreachable and broken task handling".
        """
        now = self._now()
        failed_row = exec_state.task_runs[walk_position]
        self._emit_run_error(
            exec_state,
            walk_position,
            "task_skipped",
            "task skipped: target missing from network",
        )
        failed_row.finished_at = now
        try:
            exec_state._append_next_walk_task(route)
        except Exception:
            pass
        self._emit_route_warning(
            exec_state.train_id,
            "task_skipped",
            f"task {walk_position} skipped: target missing from network",
        )

    def _emit_run_error(
        self, exec_state, walk_position, origin: str, text: str,
        operation: str = "tick_train_phase1",
    ) -> None:
        """Emit a run/task error as an event (design_new.md "Error events").

        No entity stores an error: the record rides in the event's `error`
        field, and the run panel reads it back through
        `NotificationCenter.latest_error_for(...)`. The event names both a
        `"route_run"` ref and a `"walk_position"` ref, so a run-level filter
        and a task-level filter both find it.

        Repeats are suppressed by `NotificationCenter`, so Phase 1 may call
        this on every eligible tick while a task stays blocked for the same
        reason.
        """
        sc = getattr(self, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is None:
            return
        try:
            from notification_center import (
                Kind as _Kind,
                route_run_ref as _rref,
                train_ref as _tref,
                walk_position_ref as _wref,
            )
            from runtime_payloads import ErrorRecord as _ErrRec

            entities = [_tref(exec_state.train_id), _rref(exec_state.run_id)]
            if walk_position is not None:
                entities.append(_wref(exec_state.run_id, walk_position))
            nc.emit(
                source="RouteExecutor",
                operation=operation,
                kind=_Kind.WARNING,
                message=text,
                entities=entities,
                error=_ErrRec(text, origin),
                event_time=self._now(),
            )
        except Exception:
            pass

    def _emit_route_warning(
        self, train_id: str, origin: str, reason: str,
        operation: str = "tick_train_phase1",
    ) -> None:
        """Emit an operator-facing warning via NotificationCenter, falling back
        to a logger warning when the center is unavailable. Used by the
        broken-task skip and the no-reachable-task stall (design_new.md
        "Unreachable and broken task handling").

        `origin` is the `ErrorRecord.origin` label naming why the train is
        blocked — one of the v1 error origins (`"planning_failed"`,
        `"task_skipped"`, `"run_stalled"`, `"wedge"`, `"meet_hold"`). Repeats
        with the same entity, operation, origin and payload are suppressed by
        `NotificationCenter`, so Phase 1 may call this on every eligible tick.
        """
        sc = getattr(self, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is not None:
            try:
                from notification_center import Kind as _Kind, train_ref as _train_ref
                from runtime_payloads import ErrorRecord as _ErrorRecord

                nc.emit(
                    source="RouteExecutor",
                    operation=operation,
                    kind=_Kind.WARNING,
                    message=reason,
                    entities=[_train_ref(train_id)],
                    error=_ErrorRecord(reason, origin),
                    event_time=time.time(),
                )
                return
            except Exception:
                pass
        logger.warning("route warning for %s (%s): %s", train_id, origin, reason)

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

        Per design_new.md lines 1826/1828 (EB-while-dwelling rows `0a8e`/`9f74`):
        EB does NOT freeze the dwell timer — `started_at` is the derived
        `task_started_at(...)` value, which `eb.activate()` never touches, so the
        timer keeps counting. What EB suppresses is dwell COMPLETION: while
        `eb.active == True` this step returns immediately so finish_dwell / depart
        do not fire (a train must not depart under EB); completion resumes only
        once `eb.release()` clears the latch.

        Per design_new.md, on completion this step writes
        `task_runs[task_index].finished_at = now`, advances
        `current_waypoint_index`, and calls `controller.finish_dwell()`.
        The next task's `started_at` is derived (read via
        `RouteExecutionState.task_started_at(i)`) — it just falls out of the
        finished_at write per the canonical rule, so there is no separate
        write step.
        """
        # EB suppresses dwell COMPLETION, not the timer (design_new.md lines
        # 1826/1828): the timer is the derived `started_at`, untouched by EB, so
        # it keeps counting; finish_dwell / depart only fire once EB is released.
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

        # Mark the dwell row finished and advance through the canonical
        # finished_at + delay_class + walk-append writer (delay_class no-ops
        # for dwell rows per `_record_delay_class`).
        self.advance_walk_position(exec_state, route, row_pos, now)

        # Advance the per-run cursor based on the route mode.
        self._advance_to_next_task(route, exec_state)

        # Unpack (success, reason) tuple per train_controller agent signature.
        _fd_success, _fd_reason = controller.finish_dwell()

    def _run_task_step(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        projection: "TargetProjection",
        now: float,
        delta_seconds: float,
    ) -> bool:
        """Phase-1 handling for a `run` task (design_new.md Planning Kernel
        step `6a0d`, "Run timer, service stop, and normal-progress authority
        release").

        A `run` keeps the train moving for `task_time` seconds of simulated
        time, then lets it coast to a smooth stop wherever it is and finishes
        the run so the following task (typically a `dwell`) takes over. The
        stop is `TrainController.begin_service_stop()`, an operational
        deceleration, not an abrupt cut, and the run never targets a stop
        location — where it comes to rest is not part of the contract
        (design_new.md "RouteTask data").

        The timer is measured in accumulated simulated `delta_seconds` (not
        wall-clock) and stored on `RouteTaskRun.run_elapsed_seconds`, so
        "run 20 s" means 20 s of simulation and behaves the same regardless
        of tick cadence. This step is `run_elapsed_seconds`'s only writer.

        Returns:
            True  — the run window has elapsed and the train is stopping or the
                    run is finished this tick; the caller must not acquire fresh
                    authority.
            False — the run is still within its time window; the caller falls
                    through to ordinary authority acquisition to keep moving.
        """
        walk_pos = projection.walk_position
        if not (0 <= walk_pos < len(exec_state.task_runs)):
            return False
        run_row = exec_state.task_runs[walk_pos]

        # Already finished — nothing left for this step to do.
        if run_row.finished_at is not None:
            return True

        task = route.tasks[run_row.task_index]
        run_time = (
            task.task_time if (task.task_time and task.task_time > 0.0) else 0.0
        )
        train_id = train.train_id

        run_row.run_elapsed_seconds = min(
            run_row.run_elapsed_seconds + max(0.0, delta_seconds), run_time
        )

        # Still inside the run window → keep rolling toward the direction seed.
        if run_row.run_elapsed_seconds < run_time:
            return False

        # Window elapsed: bring the train to a smooth stop and release the
        # onward authority so nothing re-drives it; the cancel commits in the
        # gated Phase 3 once the train is stopped.
        controller.begin_service_stop()
        if (
            self._get_active_path_id(train_id) is not None
            and controller.route_hooks.phase_latches.path_cancel_request is None
        ):
            controller.route_hooks.phase_latches.set_path_cancel_request(
                initiator="run_complete",
                reason=f"run task (walk-position {walk_pos}) timer elapsed",
                route_progress_kind="run_complete",
            )

        if (
            not controller.is_stopped()
            or controller.route_hooks.phase_latches.path_cancel_request is not None
            or controller.path_projection.received_path_plan is not None
        ):
            return True

        # At rest with authority released: record finished_at + advance the
        # cursor so the next task (typically a dwell at this position) is
        # armed on the next tick.
        controller.consume_service_stop()
        self.advance_walk_position(exec_state, route, walk_pos, now)
        self._advance_to_next_task(route, exec_state)
        return True

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

            # A `dwell` goes through dwell-entry; a `run` ends on its own timer
            # (handled in `_run_task_step`) — neither is ever satisfied by
            # coverage here, so stop advancing when either is next.
            if task.task_kind in ("dwell", "run"):
                break

            target_block = self.resolve_task_target_block(task, train=train)
            if not target_block:
                break

            if task.task_kind == "stop":
                # A stop is immediately satisfied only when the train is already
                # stopped at the destination block with no active path. The
                # pathfinder returns 0 candidates for same-block paths, so
                # create_path would never succeed in this case.
                #
                # The covered-stop stays HEAD-anchored (head tile in the
                # destination block), not span-anchored: a long train at a
                # dead-end can have its TAIL spill into the next block — which on
                # a turnaround is the very destination it must drive back to —
                # while its head is still short of it. Span membership alone
                # would wrongly declare that stop complete and finish the route
                # without the train ever turning around. The head must actually
                # reach the destination. (The covered-stop's doc home,
                # design_new.md line 1458, describes the train already parked AT
                # the destination, where head and span agree; head-anchoring is
                # the safe reading of that case and preserves turnaround safety.)
                if not controller.is_stopped():
                    break
                if self._get_active_path_id(train.train_id) is not None:
                    break
                if not self._is_train_at_block(train, target_block):
                    break
            else:
                # waypoint coverage (design_new.md line 1458): covered when the
                # resolved target block is anywhere in the train's current span
                # (a waypoint the train physically passed mid-span is covered
                # even after its head has moved beyond it), or — for a
                # platform-anchored task — the head sits at the platform tile.
                if not self._target_block_in_current_span(controller, target_block):
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
            self.advance_walk_position(exec_state, route, pos, now)
            self._advance_to_next_task(route, exec_state)

    @staticmethod
    def _target_block_in_current_span(
        controller: TrainController, block_id: str
    ) -> bool:
        """True when ``block_id`` is anywhere in the train's current span.

        Covered-task / covered-stop coverage reads the train's full physical
        span, not just the head-tile block (design_new.md line 1458): the
        target is covered when it appears in
        ``SimulationController.get_or_create_controller(train_id)
        .get_current_span().occupied_blocks``. Returns False when no span is
        available yet (early bootstrap), mirroring the head-only predicate's
        not-at-target result.
        """
        if controller is None:
            return False
        span = controller.get_current_span()
        if span is None:
            return False
        return block_id in (getattr(span, "occupied_blocks", None) or [])

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

        # Meet-coordination ceiling (design_new.md "Meet coordination" line 1558
        # + "Acquisition-cap rule" line 1025): when this train carries a meet
        # ceiling, keep the request at the real route target and pass the ceiling
        # as the acquisition cap. The dispatcher acquires toward the real target
        # but stores no authority past the cap, so `authority_end_block_id` lands
        # on the ceiling while `requested_destination_block_id` (and the
        # `task_runs[...].resolved_target` written on success below) stay the
        # farther route target. The capped authority never completes, so the
        # train holds at the ceiling. The first tick the train carries no
        # ceiling, no cap is passed and the full route target is requested.
        acquisition_cap_block_id: Optional[str] = None
        ceiling = self.meet_assignment.get(train.train_id)
        if ceiling is not None and ceiling.block_id != target_block:
            acquisition_cap_block_id = ceiling.block_id

        # Decide create vs extend by active authority presence.
        active_path_id = self._get_active_path_id(train.train_id)

        # Resolve acquisition_mode: per-run override (if present) wins over
        # shared Route.acquisition_mode per design_new.md "RouteRun overrides"
        # canonical home.
        acquisition_mode = exec_state.effective_acquisition_mode(
            route=route, route_executor=self
        )

        # design_new.md line 1208 / line 640: forward the assigned route's
        # operator override into the acquisition chain so an operator-initiated
        # route can serve `manual` resources. Route-level only (never a RouteRun
        # override); an automatic route leaves this False.
        manual_override = bool(getattr(route, "manual_override", False))

        success = False
        result = None
        try:
            if active_path_id is not None:
                result = self.path_dispatcher.extend_path(
                    path_id=active_path_id,
                    requested_destination_block_id=target_block,
                    requested_destination_direction=target_dir,
                    acquisition_mode=acquisition_mode,
                    manual_override=manual_override,
                    acquisition_cap_block_id=acquisition_cap_block_id,
                )
                success = bool(getattr(result, "success", False))
            else:
                result = self.path_dispatcher.create_path(
                    train_id=train.train_id,
                    requested_destination_block_id=target_block,
                    requested_destination_direction=target_dir,
                    acquisition_mode=acquisition_mode,
                    manual_override=manual_override,
                    acquisition_cap_block_id=acquisition_cap_block_id,
                )
                success = bool(getattr(result, "success", False))
        except Exception as exc:  # pragma: no cover — defensive
            success = False

        if not success:
            # Auto-recover from a structural extend_path failure: the live path
            # cannot be grown toward this target at all, whatever the live
            # traffic does. INVALID_REVERSE means the new destination needs the
            # train to turn around at the current authority end; NO_VIABLE_PATH
            # means no legal forward suffix exists to it, including when the
            # only route there runs back through a block this path still holds.
            # Neither clears by waiting, so retrying against the same path would
            # spin until the train ran out its authority and stopped. Latch a
            # cancellation so Phase 3 tears the stale path down once the train
            # stops; the next eligible Phase 1 will see no active path and
            # acquire via create_path from the train's current span instead.
            # Per the **Planning Kernel** section these are the failure kinds that
            # "latch cancellation before a fresh create attempt", and per the
            # latch-table rules this is the same pattern Phase 1 uses for any
            # "live path must be replaced" condition: write the latch, stop
            # planning this tick, let Phase 3 commit. BLOCKED_BEFORE_TARGET is
            # deliberately not here — it is a temporary live conflict, and the
            # path stays valid while Phase 1 retries.
            fkind = getattr(result, "failure_kind", None)
            if active_path_id is not None and fkind in (
                PathAcquisitionFailureKind.INVALID_REVERSE,
                PathAcquisitionFailureKind.NO_VIABLE_PATH,
            ):
                cancel_reason = (
                    getattr(result, "reason", None)
                    or "extend cannot grow this path toward the target"
                )
                controller.route_hooks.phase_latches.set_path_cancel_request(
                    initiator="revoked",
                    reason=(
                        f"{cancel_reason}; auto-cancelling so the next "
                        "acquisition uses create_path from the train's "
                        "current span"
                    ),
                )
                return False

            # Emit why acquisition failed so operators can inspect the reason.
            reason_text = (
                getattr(result, "reason", None) or "create_path/extend_path failed"
            )
            row_idx = projection.walk_position
            self._sync_task_runs(route, exec_state)
            self._emit_run_error(
                exec_state, row_idx, "planning_failed", reason_text,
            )
            return False

        # Authority acquisition succeeded for this task. Nothing to clear:
        # errors are events, not entity state, and the surfaces that show a
        # reason gate on live state instead — the no-authority banner and the
        # Quick Route toast are shown only while the controller payload carries
        # no `received_path_plan`, so a reason from before the train acquired
        # authority is never displayed (design_new.md "Error events").

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

        # Successful create/extend: write
        # task_runs[walk_position].resolved_target.
        #
        # Take the pair from the dispatcher's own result rather than from this
        # tick's projection. The result names what the dispatcher actually
        # planned toward and is the authority on the live path's terminal
        # direction. The projection is a fresh re-resolve of a task that may be
        # authored `task_direction = "either"`, and it can name a direction the
        # live authority does not have — notably once the head is already
        # inside the target block, where the walk that reaches that block
        # resolves the opposite way round. This field is the match key stop
        # completion looks up, so it has to describe the authority the path
        # holds, not the latest re-resolve.
        result_block = getattr(result, "requested_destination_block_id", None)
        result_dir = getattr(result, "requested_destination_direction", None)
        target_idx = projection.walk_position
        self._sync_task_runs(route, exec_state)
        if 0 <= target_idx < len(exec_state.task_runs):
            exec_state.task_runs[target_idx].resolved_target = ResolvedRouteTarget(
                requested_destination_block_id=result_block or target_block,
                requested_destination_direction=result_dir or target_dir,
            )

        # design_new.md "Event log": a fresh create (no prior active path) is a
        # PATH_CREATED event; an extend grows an existing path and is not.
        if active_path_id is None:
            self._emit_event(
                "create_path",
                f"path created for {train.train_id} to {target_block}",
                train_id=train.train_id,
                path_id=getattr(result, "path_id", None),
                payload={"requested_destination_block_id": target_block},
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
                        from notification_center import Kind as _Kind, train_ref as _tref
                        from runtime_payloads import ErrorRecord as _ErrRec

                        nc.emit(
                            source="RouteExecutor",
                            operation="depart",
                            kind=_Kind.WARNING,
                            message=f"depart() raised for {train_id}; will retry",
                            entities=[_tref(train_id)],
                            error=_ErrRec(str(exc), "depart_failed"),
                            event_time=time.time(),
                            payload={"will_retry": True},
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

    def _maintain_idle_hold(
        self,
        train_id: str,
        controller: TrainController,
        exec_state: RouteExecutionState,
        auth_end_before: Optional[str],
    ) -> bool:
        """Maintain `idle_hold_ticks` and release a stalled best-effort hold.

        Per design_new.md "Idle-hold maintenance and release" (Planning Kernel
        step 66aa). Returns True when it latched an idle-hold release (the caller
        stops planning this tick; Phase 3 commits the cancel). A truncated
        best-effort authority (``authority_end_block_id !=
        requested_destination_block_id``) that neither grew this tick (the
        authority end is unchanged from ``auth_end_before``) nor has the train
        moving accrues `idle_hold_ticks`; at `IDLE_HOLD_RELEASE_TICKS` it latches
        ``pending_path_cancel_request(initiator="idle_hold_release")``, starts the
        cooldown, and returns True. Any growth, train movement, full authority,
        or no live path resets `idle_hold_ticks` to 0.
        """
        path = (
            self.path_dispatcher.get_active_path(train_id)
            if self.path_dispatcher is not None
            else None
        )
        if path is None or (
            path.get_authority_end_block_id() == path.requested_destination_block_id
        ):
            # Full authority or no live path → not a truncated best-effort hold.
            exec_state.idle_hold_ticks = 0
            return False
        grew = (
            auth_end_before is None
            or path.get_authority_end_block_id() != auth_end_before
        )
        moving = not controller.is_stopped()
        if grew or moving:
            exec_state.idle_hold_ticks = 0
            return False
        # Contention-driven release (design_new.md "Contention snapshot", line
        # 1544): a stalled best-effort hold whose reserved-ahead corridor blocks
        # a strictly-higher-priority run is released NOW, not after the idle-hold
        # timeout — the higher-priority run is waiting on resources this run is
        # sitting on. Same latch path as the timeout release (Phase 1 latches;
        # Phase 3 commits the cancel once stopped), only sooner.
        if self._contention_release_due(train_id):
            controller.route_hooks.phase_latches.set_path_cancel_request(
                initiator="contention_release",
                reason=(
                    f"best-effort authority at {path.get_authority_end_block_id()} "
                    "released for a higher-priority run blocked on a held "
                    "reservation"
                ),
            )
            exec_state.release_cooldown_ticks = IDLE_HOLD_COOLDOWN_TICKS
            exec_state.idle_hold_ticks = 0
            return True
        # A meet-ceiling-governed hold is deliberate, not hoarding: it must not
        # time out and release (design_new.md:1582 — "a meet ceiling capping
        # this tick's acquisition ... must not time out and release"). Reset the
        # no-progress count, but only after the contention-release safety floor
        # above (design_new.md:1560), which still applies to a meet-held run.
        if self.meet_assignment.get(train_id) is not None:
            exec_state.idle_hold_ticks = 0
            return False
        exec_state.idle_hold_ticks += 1
        if exec_state.idle_hold_ticks < IDLE_HOLD_RELEASE_TICKS:
            return False
        # Stalled past the budget: release the held truncated authority the
        # ordinary system-replan way (Phase 1 latches; Phase 3 commits the
        # cancel once stopped) and start the cooldown so the freed resources
        # stay available for higher-priority runs.
        controller.route_hooks.phase_latches.set_path_cancel_request(
            initiator="idle_hold_release",
            reason=(
                f"best-effort authority idle for {exec_state.idle_hold_ticks} "
                f"ticks at {path.get_authority_end_block_id()}; releasing so "
                "higher-priority runs can use the held resources"
            ),
        )
        exec_state.release_cooldown_ticks = IDLE_HOLD_COOLDOWN_TICKS
        exec_state.idle_hold_ticks = 0
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
        self.advance_walk_position(exec_state, route, idx, now)

        self._advance_to_next_task(route, exec_state)

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        reason: Optional[str] = None,
        was_deviation: bool = False,
        was_run_completion: bool = False,
    ) -> None:
        """Phase-3 cancellation callback.

        Per design_new.md "route path-event callback schema" (lines 1700,
        1730), the v1 callback signature is ``(train_id, path_id, reason=None,
        was_deviation=False, was_run_completion=False)``. Every cancellation
        kind shares the same body; ``was_deviation`` and ``was_run_completion``
        are the two classification bits ``PathDispatcher.cancel_path(...)``
        derives from ``deviation`` / ``route_progress_kind`` and are the
        only cancellation-kind details that cross this callback boundary. The
        callback is bookkeeping only: it must not choose a fresh target or
        create fresh authority. The free-form diagnostic `initiator` stays on
        the `path_cancellation` notification.

        A `was_run_completion` callback writes neither `path_cancelled` nor
        `path_cancel_was_deviation`: a timer-elapsed `run` task releasing its
        now-unneeded authority is a normal route-progress event, not a
        cancellation, and must not project `display_state == "CANCELLED"`.
        """
        exec_state = self.execution_states.get(train_id)
        if exec_state is None:
            return
        if was_run_completion:
            return
        # Per design_new.md: deviation is purely the `path_cancelled` latch plus
        # its `path_cancel_was_deviation` classification, surfaced through the
        # derived `display_state` projection (DEVIATED for a movement deviation,
        # CANCELLED for every other authority cancellation). The stored
        # `current_route_status` does not transition.
        exec_state.path_cancelled = True
        exec_state.path_cancel_was_deviation = was_deviation
        # The cancellation reason is emitted, not stored.
        self._emit_run_error(
            exec_state,
            exec_state.current_waypoint_index,
            "deviation" if was_deviation else "operator_cancelled",
            f"cancelled: {reason}" if reason else "cancelled",
            operation="handle_path_cancellation",
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
                # waypoint, run, or dwell — none is the stop-completion match
                # target. waypoint matches happen via handle_train_advanced;
                # run tasks are completed only by Phase 1 step 6a0d; neither
                # is ever finished by dispatcher path completion.
                if task.task_kind in ("waypoint", "run"):
                    # Skip but don't break — there may be a stop after.
                    continue
                # dwell — definitely break (must be served via finish_dwell).
                break
            # Match by resolved_target.
            target = row.resolved_target
            if target is None:
                # design_new.md: `task_runs[i].resolved_target` is the
                # authoritative match key, and re-resolving it at completion
                # time is forbidden. Diagnose and skip this walk-position.
                self._emit_route_warning(
                    train_id,
                    "planning_failed",
                    f"handle_path_completion: task_runs[{pos}] has "
                    f"resolved_target=None; skipping "
                    f"(authority_end={authority_end_block_id}). Phase 1 must "
                    f"write resolved_target before the path completes.",
                    operation="handle_path_completion",
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

        # Finish the matched stop row — plus any intervening walk-positions the
        # cursor lagged on — through the canonical `advance_walk_position(...)`
        # writer. The intervening sweep is a safety net: a waypoint the train
        # passed and then vacated is no longer in its span, so Phase 1's
        # covered-task advance (which keys on span membership) can no longer
        # finish it; finishing it here keeps the derived progress cursor from
        # stalling behind a vacated waypoint. Both the cursor and the match
        # index are list positions in `task_runs`, not `Route.tasks` indices, so
        # this loop targets the current walk's rows even after CIRCULAR /
        # ROUND_TRIP wrap-around.
        matched_row_was_open = (
            0 <= match_pos < len(exec_state.task_runs)
            and exec_state.task_runs[match_pos].finished_at is None
        )
        for pos in range(start_pos, match_pos + 1):
            if not (0 <= pos < len(exec_state.task_runs)):
                continue
            row = exec_state.task_runs[pos]
            if row.finished_at is not None:
                continue
            # The matched stop (pos == match_pos) is always finished here. An
            # intervening lagged row is force-finished ONLY when it is a
            # `waypoint` the train passed and vacated — the sole case Phase 1's
            # span-keyed covered-task advance can no longer reach. A non-waypoint
            # intervening row is outside that safety net and must not be
            # force-finished (it would fabricate an arrival/`delay_class`).
            if pos != match_pos:
                task = route.get_task_at(row.task_index)
                if task is None or task.task_kind != "waypoint":
                    continue
            self.advance_walk_position(exec_state, route, pos, event_time)
        # ROUND_TRIP / CIRCULAR walks reflect or wrap at route ends by
        # flipping `RouteRun.direction` so the next `_append_next_walk_task`
        # appends the reflected walk-position. Every other advance site
        # (dwell-complete, head-advance, covered-task advance) pairs the
        # `advance_walk_position(...)` writer with this flip; the
        # dispatcher-stop-completion path must too, or a ROUND_TRIP whose
        # forward apex is a bare `stop` with no trailing dwell oscillates at
        # the apex instead of reflecting (design_new.md:1281-1288, 1312).
        # Guarded on the matched row having been open so an idempotent
        # re-entry cannot double-flip the direction.
        if matched_row_was_open:
            self._advance_to_next_task(route, exec_state)
        # design_new.md "Event log": the train reached its stop target.
        self._emit_event(
            "handle_path_completion",
            f"{train_id} arrived at {authority_end_block_id}",
            train_id=train_id,
            path_id=path_id,
            payload={"block_id": authority_end_block_id},
        )
        # Phase 3 must NOT call enter_station_dwell — that fires from the next
        # eligible Phase 1 dwell-entry step.
        return True

    def _record_delay_class(
        self,
        exec_state: RouteExecutionState,
        route: "Route",
        row_pos: int,
        now: float,
    ) -> None:
        """Classify `task_runs[row_pos].delay_class` on real task completion.

        Per design_new.md "RouteTaskRun schema": compares the shared task's
        expected `task_time` with elapsed time since the predecessor walk
        position's finish (`RouteRun.started_at` for row 0). No-op for `dwell`
        rows and for tasks with no `task_time`. Tasks pre-finished by start-mode
        seeding or skipped as broken are never routed here, so they keep
        `delay_class = None`.
        """
        if not (0 <= row_pos < len(exec_state.task_runs)):
            return
        row = exec_state.task_runs[row_pos]
        if not (0 <= row.task_index < len(route.tasks)):
            return
        task = route.tasks[row.task_index]
        if task.task_kind == "dwell" or task.task_time is None:
            return
        started = (
            exec_state.started_at
            if row_pos == 0
            else exec_state.task_runs[row_pos - 1].finished_at
        )
        if started is None:
            return
        elapsed = now - started
        tol = DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS
        if elapsed < task.task_time - tol:
            row.delay_class = "early"
        elif elapsed > task.task_time + tol:
            row.delay_class = "late"
        else:
            row.delay_class = "on_time"

    def advance_walk_position(
        self,
        exec_state: RouteExecutionState,
        route: "Route",
        walk_position: int,
        now: float,
    ) -> None:
        """Advance one walk-position: write `task_runs[walk_position].finished_at`,
        classify `delay_class`, and append the next walk task.

        This is `RouteRun.advance_walk_position(...)` per design_new.md
        "Route-run state" — the single canonical writer of the
        finished_at + delay_class + walk-append sequence. Every completion site
        (stop arrival, head-advance, covered-task advance, dwell-complete)
        routes through it so the three steps cannot drift. Idempotent: a
        walk-position whose `finished_at` is already set is a no-op.
        `_record_delay_class` no-ops for `dwell` rows and tasks without
        `task_time`.
        """
        if not (0 <= walk_position < len(exec_state.task_runs)):
            return
        row = exec_state.task_runs[walk_position]
        if row.finished_at is not None:
            return
        row.finished_at = now
        self._record_delay_class(exec_state, route, walk_position, now)
        exec_state._append_next_walk_task(route)

    # ------------------------------------------------------------------ removal
    def remove_train_route_run(self, train_id: str) -> None:
        """Tear down a `RouteRun` for `pending_route_abort` /
        `pending_power_off` / `pending_route_deactivate`.

        Per design_new.md (~line 1242): looks up the train's currently-RUNNING
        `RouteRun` (no-op if none), then performs the canonical teardown:

          1. Force-clear any service-stop override through
             `TrainController.consume_service_stop(force=True)` so a run
             interrupted by this teardown never leaves braking armed on a
             later route.
          2. If the controller is in `RUNNING_AT_STATION`, call
             `TrainController.finish_dwell()` to exit dwell into READY first.
          3. Call `Route.unassign_train(train_id)` on the run's `route_id`.
          4. Set `current_route_status = DEACTIVATED` and archive the
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
        if controller is not None:
            # Force-clear through the service-stop teardown path so a run
            # interrupted by this teardown never leaves braking armed on a
            # later route (design_new.md "TrainController.begin_service_stop()
            # / consume_service_stop()").
            controller.consume_service_stop(force=True)
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
                            from notification_center import Kind as _Kind, train_ref as _tref
                            from runtime_payloads import ErrorRecord as _ErrRec

                            nc.emit(
                                source="RouteExecutor",
                                operation="remove_train_route_run",
                                kind=_Kind.WARNING,
                                message=(
                                    f"finish_dwell() raised for {train_id} during "
                                    "route-run removal"
                                ),
                                entities=[_tref(train_id)],
                                error=_ErrRec(str(exc), "finish_dwell_failed"),
                                event_time=time.time(),
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

    def record_pending_reassign(self, train_id: str, route_id: str) -> None:
        """Hold a same-route re-assignment until the Phase-3 deactivate clear
        is done (per design_new.md "Shared-route edit teardown").

        Recorded by the structural `Route.tasks` edit handler instead of
        re-adding to `Route.assigned_train_ids` up front, because the deactivate
        consumer's `remove_train_route_run(...)` unassigns the train during the
        clear and would undo an up-front re-add.
        """
        self._pending_reassign[train_id] = route_id

    def apply_pending_reassign(self, train_id: str) -> Optional[str]:
        """Apply (and clear) any held re-assignment for `train_id` AFTER its run
        has been torn down and unassigned. Returns the re-assigned `route_id`,
        or `None` when nothing was held.

        Called only by the Phase-3 `pending_route_deactivate` consumer, after
        `remove_train_route_run(train_id)` -- per design_new.md "Shared-route
        edit teardown". The abort and power-off consumers never record a held
        re-assignment, so they tear down without re-assigning.
        """
        route_id = self._pending_reassign.pop(train_id, None)
        if route_id is None:
            return None
        route = self.route_registry.get(route_id)
        if route is None:
            return None
        route.assign_train(train_id)
        return route_id

    def discard_pending_reassign(self, train_id: str) -> None:
        """Drop any held re-assignment for `train_id` WITHOUT applying it.

        Per design_new.md "Shared-route edit teardown" (line 1461): when an
        earlier Phase-3 consumer (abort / power-off) or a natural finish ended
        the run before the deactivate consumer ran, the operator/admin teardown
        wins — the held re-assignment is discarded so a powered-off or aborted
        train is never silently re-assigned to the edited route.
        """
        self._pending_reassign.pop(train_id, None)

    def _emit_event(
        self, operation, message, train_id=None, payload=None, path_id=None,
        kind=None,
    ) -> None:
        """Emit an operational event-log notification through NotificationCenter
        (design_new.md "Event log"). Best-effort: resolves the sink defensively,
        no-ops when none is wired, and never raises into the caller."""
        try:
            sc = getattr(self, "simulation_controller", None)
            nc = getattr(sc, "notification_center", None) if sc is not None else None
            if nc is None:
                return
            from notification_center import (
                Kind as _Kind,
                path_ref as _path_ref,
                train_ref as _train_ref,
            )

            entities = []
            if train_id:
                entities.append(_train_ref(train_id))
            if path_id:
                entities.append(_path_ref(path_id))
            nc.emit(
                source="RouteExecutor",
                operation=operation,
                kind=kind if kind is not None else _Kind.INFO,
                message=message,
                entities=entities,
                event_time=self._now(),
                payload=payload or {},
            )
        except Exception:
            pass

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

    def waypoint_and_run_target_blocks(self, train_id: str) -> set:
        """Resolved target block_ids of every `task_kind == "waypoint"` or
        `task_kind == "run"` task on this train's current RouteRun.

        Public because `PathDispatcher` calls it (through
        `simulation_controller.route_executor`) during best-effort truncation:
        per design_new.md "`create_path()` flow" item 11, a stored live
        authority's terminal block must never land on a pass-through waypoint
        target or a run's direction-seed target. Waypoints are consumed by
        head-entry on a still-live authority; run tasks are completed only by
        Phase 1 step `6a0d`. Neither is ever finished by dispatcher path
        completion.
        """
        exec_state = self.execution_states.get(train_id)
        if exec_state is None:
            return set()
        route = self.route_registry.get(exec_state.route_id)
        if route is None:
            return set()
        train = self.network.trains.get(train_id) if self.network else None
        targets: set = set()
        for task in route.tasks:
            if getattr(task, "task_kind", None) not in ("waypoint", "run"):
                continue
            block_id = self.resolve_task_target_block(task, train=train)
            if block_id:
                targets.add(block_id)
        return targets

    def resolve_task_target_block(
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
