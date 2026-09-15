"""
Route Executor

Per design_new.md "Route And RouteExecutor" and "Planning Kernel" sections.

RouteExecutor owns:
  * Plan (`tick_train_plan`) for one train: route-deviation
    cleanup, dwell-complete timer check, covered-task index advance,
    dwell-entry, target resolution, and authority acquisition via
    PathDispatcher.
  * Dispatcher callbacks invoked synchronously by PathDispatcher:
    `handle_train_advanced`, `handle_path_cancellation`, `handle_path_completion`.
  * Runnable-train ordering for the per-tick simulation loop.
  * Removal of route runs for the abort/power-off paths.

RouteExecutor never calls `PathDispatcher.cancel_path(...)` directly. Plan
that wants to cancel an active path latches `phase_latches.path_cancel_request`
on the controller mailbox and waits for the Action cancel→callback→cleanup
flow.
"""

from dataclasses import dataclass, field
import logging
import time
from typing import Any, ClassVar, Dict, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

from constants import (
    DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS,
)
from enums import AtcLimitReasonCode, ControllerCondition, Direction
from train_controller import TrainController, create_controller_for_train
from train_route import (
    RouteMode,
    RouteRunStatus,
    RouteStartMode,
    RouteTask,
    RouteTaskRun,
)
from notification_center import Kind
from runtime_payloads import ErrorRecord, PathAcquisitionFailureKind, PathDispatcherForRouteExecutor, ResolvedRouteTarget, ResourceEvent, TargetResolutionFailureKind

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from train_route import Route

logger = logging.getLogger(__name__)


# Pending admin/system cancellation latches that gate Plan authority
# acquisition per design_new.md (Planning Kernel section). Both create and
# extend branches require this set to be empty in active_conditions.
_PENDING_CANCEL_CONDITIONS = frozenset(
    {
        ControllerCondition.PATH_CANCEL_PENDING,
        ControllerCondition.ROUTE_ABORT_PENDING,
        ControllerCondition.ROUTE_DEACTIVATE_PENDING,
    }
)

# The four teardown latches that outrank normal Plan progress (design_new.md
# "Teardown outranks normal progress"): while any is set, steps
# `70c3`/`6a0d`/`a5c6`/`3a37` make no write. This is the cancel set plus
# `POWER_OFF_PENDING`; the acquisition-eligibility gate (steps `66aa`/`592a`)
# uses only `_PENDING_CANCEL_CONDITIONS`.
_TEARDOWN_CONDITIONS = _PENDING_CANCEL_CONDITIONS | frozenset(
    {ControllerCondition.POWER_OFF_PENDING}
)

# design_new.md "A pending teardown holds the request": when any of these
# is already active on a re-request, `request_route_assignment` writes no second
# teardown latch — the teardown consumer already in flight applies the latest
# held `_pending_route_assignments` entry when it removes the run.
_HELD_REQUEST_TEARDOWN_CONDITIONS = frozenset(
    {
        ControllerCondition.ROUTE_ABORT_PENDING,
        ControllerCondition.ROUTE_DEACTIVATE_PENDING,
        ControllerCondition.POWER_OFF_PENDING,
    }
)


# =============================================================================
# Contention snapshot (design_new.md "Contention snapshot")
# =============================================================================


@dataclass(frozen=True)
class ContentionEdge:
    """One "train A is blocked by train B on this block" fact for a tick.

    `waiting_train_id` cannot grow its authority past `waiting_block_id` because
    `holding_train_id` is in the way of the first forward resource. There is one
    edge per holder of each blocked waiter. Priority is not carried on the edge:
    wedge and meet detection read `RouteRun.effective_priority(route_registry)`
    fresh from the registry when they need it (design_new.md "Contention
    snapshot").
    """

    waiting_block_id: str
    waiting_train_id: str
    holding_train_id: str


@dataclass
class ContentionSnapshot:
    """Per-tick view of who is blocked behind whom (design_new.md "Contention
    snapshot").

    `edges` is the complete list, one per holder of each blocked waiter, in
    runnable-order (waiter) then holder-order; wedge and meet detection read it.
    The documented snapshot shape is `edges` only. Rebuilt every tick, retained
    across none — not a latch.
    """

    edges: List[ContentionEdge] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.edges)


# =============================================================================
# Wedge report (design_new.md "Wedge detection")
# =============================================================================


@dataclass(frozen=True)
class Wedge:
    """One deadlock — a set of trains that are each stopped with nowhere to go,
    every member waiting for a resource another member holds, so none can move
    (design_new.md "Wedge detection").

    `train_ids` are the members, sorted, so the same wedge has the same identity
    every tick. `block_ids` are the contested blocks the cycle runs through,
    sorted — the `waiting_block_id`s of the wait-for edges that stay inside the
    wedge. A one-train wedge has a single train whose own body holds the resource
    its authority needs next (its wait-for edge points back at itself).
    """

    train_ids: Tuple[str, ...]
    block_ids: Tuple[str, ...]


@dataclass
class WedgeReport:
    """Per-tick view of the wedges present (design_new.md "Wedge detection").

    Read-only coordinator state, like the contention snapshot: rebuilt every
    tick from that snapshot's `edges` (restricted to trains that are stopped
    with nowhere to go), retained across no ticks. It changes no runtime state
    and carries no gating predicate, so it is not a latch and has no writer
    helper. `wedges` is empty when nothing is deadlocked.
    """

    wedges: List[Wedge] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.wedges)


# =============================================================================
# Resolved-target schemas
# =============================================================================
# `ResolvedRouteTarget` is now imported from runtime_payloads (S5 migration).


@dataclass
class ArmedTaskProjection:
    """Projection returned by `RouteExecutor.find_armed_task(train_id)`.

    The public derived read for the task whose target the live authority was
    most recently acquired toward (design_new.md "ArmedTaskProjection schema").
    Read-only: it never resolves, appends, or writes.

    It is one of three named cursors, and callers use the one matching their
    question rather than deriving an armed row through raw index arithmetic:
    `_find_next_task_target(...)` resolves the next planning candidate, this
    helper identifies the acquired authority target, and
    `RouteRun.current_waypoint_index` remains only the derived physical-progress
    cursor.
    """

    walk_position: int
    task_index: int
    task_kind: str  # "waypoint" | "stop" | "dwell" | "run"
    stored_resolved_target: ResolvedRouteTarget


@dataclass
class CompletionTaskProjection:
    """Projection returned by `RouteExecutor.find_next_completion_task(train_id)`.

    The public read used only by `PathDispatcher.try_complete_active_path()` to
    identify the next physical task eligible for stop completion (design_new.md
    "CompletionTaskProjection schema"). It returns the lowest non-finished
    walk-position without appending or resolving a new target, so
    `PathDispatcher` reads only `task_kind` and `stored_resolved_target` and
    never indexes route storage itself.

    One of three named cursors (design_new.md "Separate cursor model"):
    `_find_next_task_target(...)` resolves the next planning candidate, this
    helper identifies the next physical task eligible for stop completion, and
    `find_armed_task(...)` identifies the acquired authority target. Callers use
    the projection matching their question rather than deriving a row through
    raw index arithmetic.

    `stored_resolved_target` is `Optional` here (unlike `ArmedTaskProjection`,
    which requires a non-`None` target): the lowest non-finished row may be a
    `dwell` (which carries no dispatcher target) or a not-yet-armed row, so its
    stored `resolved_target` can be `None`. Per design_new.md it is exactly
    `task_runs[walk_position].resolved_target`.
    """

    walk_position: int
    task_index: int
    task_kind: str  # "waypoint" | "stop" | "dwell" | "run"
    stored_resolved_target: Optional[ResolvedRouteTarget]


@dataclass
class ResolvedNextTaskProjection:
    """Projection returned by `_find_next_task_target(train_id)`.

    Read-only result. Plan's authority-acquisition step is the only writer
    of `task_runs[walk_position].resolved_target`; this helper itself never
    writes that field.

    Per design_new.md "Canonical Next-Target Selection", `walk_position` is the walk-position index
    in `RouteRun.task_runs`. The underlying `RouteTask` is
    `Route.tasks[task_runs[walk_position].task_index]`.

    `failure_reason` carries the resolver's diagnostic when
    `resolved_target is None` for a non-dwell task because every candidate
    block / platform was filtered out (disabled, manual without override,
    every station platform unavailable). Plan writes this string directly
    as the `ErrorRecord` of one route-run error event (inlined in
    `tick_train_plan`) so the operator sees why the task is stalled instead
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
    # The `AppState.operating_time` reading at that same moment, so a duration
    # measured from the run's start under the `"operating"` timing basis never
    # subtracts a wall instant from an operating-clock one (design_new.md
    # "RouteTaskRun schema", predecessor timestamp lookup). Never persisted.
    started_at_operating: float = 0.0
    # Effective traversal direction for the current directed walk.
    # Per design_new.md "Route-run state" — stored as the literal string
    # `"forward"` or `"reverse"`. ROUND_TRIP arithmetic computes a local
    # signed integer from this literal at use sites; the stored field is
    # always the string form.
    direction: str = "forward"
    # Per-run task table, parallel to Route.tasks. Allocated when the run is
    # created (or on first use) and grown lazily as Route.tasks gains entries.
    task_runs: List[RouteTaskRun] = field(default_factory=list)
    # Opaque global run-instance number assigned by RouteExecutor (diagnostic only).
    run_number: int = 0
    # Plan-maintained flag (NOT a latch): set when every task on a
    # CIRCULAR / ROUND_TRIP walk is broken (target ID missing from the
    # network), cleared by the next Plan pass that finds a valid task.
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
    # names from the v1 override allowlist (`"priority"`);
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
        """The run-only part of the UI display projection.

        Per design_new.md ``Route-run state`` (the ``display_state`` row), in
        priority order: (1) when ``current_route_status`` is terminal
        (``FINISHED`` / ``DEACTIVATED``), the terminal state; (2) else
        ``"STALLED"`` when ``no_reachable_task``; (3) otherwise
        ``current_route_status.value`` (``"RUNNING"``).

        The ``"CANCELLED"`` case is resolved in the read model (see
        ``RouteExecutor._display_state_for(...)``), which has the dispatcher and
        the event stream in hand: it needs the train's most recently terminated
        ``Path`` record's status plus the run's live-path state, which this
        run-only property cannot see. STALLED outranks CANCELLED, so the read
        model only considers CANCELLED when this property returns ``"RUNNING"``.
        Not a stored field; no writers. Runtime branching never keys on this
        value.
        """
        if self.current_route_status in (
            RouteRunStatus.FINISHED,
            RouteRunStatus.DEACTIVATED,
        ):
            return self.current_route_status.value
        if self.no_reachable_task:
            return "STALLED"
        return self.current_route_status.value

    @property
    def current_waypoint_index(self) -> Optional[int]:
        """Derived: physical-progress cursor — the largest walk-position
        index `i` in `task_runs` where `task_runs[i].finished_at is not
        None`, or `None` when no task has finished yet.

        Per design_new.md "Route-run state" — `Optional[int]` (read-only
        derived). This is the WALK-POSITION (the list index into `task_runs`),
        not the underlying `task_index`; the two decouple after the first
        `CIRCULAR` / `ROUND_TRIP` wrap. Computed each access from `task_runs`;
        not a stored field; no writers — the writers that record progress all
        set `task_runs[i].finished_at`, and the cursor reflects that update on
        the next read.

        Consumers that want "next task to plan" should use
        `next_planning_walk_position` / `next_planning_index` instead.
        """
        last_finished_pos: Optional[int] = None
        for i, run in enumerate(self.task_runs):
            if run.finished_at is not None:
                last_finished_pos = i
        return last_finished_pos

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

        Per design_new.md "Canonical Next-Target Selection", `walk_position` (the list
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
        # A terminal walk (every row finished) is never "starting" — the run has
        # completed its first movement target and then some. The doc models this
        # as ``next_planning_walk_position is None``; here that property returns
        # ``len(task_runs) - 1`` for a terminal walk, so guard on the terminal
        # condition directly (design_new.md "Route-run state", is_starting: "and
        # False once the train completes the task at start_walk_position,
        # including when next_planning_walk_position is None").
        if self.task_runs and all(
            r.finished_at is not None for r in self.task_runs
        ):
            return False
        return self.next_planning_walk_position <= self.start_walk_position

    def _append_next_walk_task(
        self, route: "Route", from_walk_position: int
    ) -> None:
        """Grow `task_runs` by the next directed-walk row.

        Per design_new.md "`RouteRun._append_next_walk_task(from_walk_position)`".
        Callers pass the walk-position the walk continues from: the just-finished
        row (`advance_walk_position(...)`, Plan broken-task skip) or the current
        tail (waypoint lookahead). The helper reads that row's stored
        ``walk_direction`` and appends exactly one new row:

        - ``ONE_OFF``: never appends.
        - ``CIRCULAR``: append ``task_index = (from_row.task_index + 1) % N`` with
          the same ``walk_direction`` (a circular walk never reflects).
        - ``ROUND_TRIP``: reflect at task-GROUP boundaries. A task group is one
          non-``dwell`` task followed by its contiguous ``dwell`` rows
          (``Route.task_groups()``). While another row remains in the current
          group, append that next row with the same direction. At a group
          boundary, append the first row of the adjacent group in the current
          direction. At an end group, reflect to the first row of the neighbour
          group with the flipped direction. Rows keep their authored order inside
          a group on both visits, and the end group is not repeated.

        Never reads or writes ``RouteRun.direction`` (design_new.md "Route-run state"):
        the visit direction lives per-row in ``walk_direction``. Appends only one
        row beyond the current tail, so a waypoint-lookahead row already appended
        past ``from_walk_position`` is never duplicated. ``route`` may be ``None``
        (unresolved shared route); the helper then no-ops safely.
        """
        if route is None or route.mode == RouteMode.ONE_OFF:
            return
        if not (0 <= from_walk_position < len(self.task_runs)):
            return
        # Grow by exactly one row beyond the current tail. When the walk was
        # already grown past `from_walk_position` (a waypoint-lookahead row
        # exists), that next row is already present, so this is a no-op.
        if from_walk_position != len(self.task_runs) - 1:
            return
        task_count = len(route.tasks)
        if task_count == 0:
            return
        from_row = self.task_runs[from_walk_position]
        from_index = from_row.task_index
        direction = from_row.walk_direction

        if route.mode == RouteMode.CIRCULAR:
            self.task_runs.append(
                RouteTaskRun(
                    task_index=(from_index + 1) % task_count,
                    walk_direction=direction,
                )
            )
            return

        if route.mode != RouteMode.ROUND_TRIP:
            return

        # ROUND_TRIP: reflect task groups at the route ends.
        groups = route.task_groups()
        g_idx: Optional[int] = None
        pos_in_group = 0
        for gi, group in enumerate(groups):
            if from_index in group:
                g_idx = gi
                pos_in_group = group.index(from_index)
                break
        if g_idx is None:
            return
        group = groups[g_idx]

        if pos_in_group < len(group) - 1:
            # Another row remains in this group: advance within it, same
            # direction (dwell rows keep authored order on both visits).
            next_index = group[pos_in_group + 1]
            next_direction = direction
        elif direction == "forward":
            if g_idx < len(groups) - 1:
                # Group boundary going forward: first row of the next group.
                next_index = groups[g_idx + 1][0]
                next_direction = "forward"
            else:
                # Forward end group: reflect to the preceding group, reverse.
                neighbour = groups[g_idx - 1] if len(groups) >= 2 else group
                next_index = neighbour[0]
                next_direction = "reverse"
        else:  # reverse
            if g_idx > 0:
                # Group boundary going reverse: first row of the previous group.
                next_index = groups[g_idx - 1][0]
                next_direction = "reverse"
            else:
                # Reverse end group: reflect to the second group, forward.
                neighbour = groups[1] if len(groups) >= 2 else group
                next_index = neighbour[0]
                next_direction = "forward"

        self.task_runs.append(
            RouteTaskRun(task_index=next_index, walk_direction=next_direction)
        )


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
        # Opaque, globally-monotonic run-instance counter (design_new.md
        # "Route-run state"). Starts at 1, supplies the current value, and
        # increments after every allocation regardless of route. The resulting
        # `RouteRun.run_number` is a unique history/API identity that carries no
        # route index and must not be parsed for `route_id`; the immutable
        # `RouteRun.route_id` field carries that relationship directly. Numbers
        # are never recycled or recomputed during the process lifetime.
        self._next_run_number: int = 1
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
        # `Route.assigned_train_ids` up front, because the Action deactivate
        # consumer's `remove_train_route_run(...)` unassigns the train from the
        # run's `route_id` as part of the clear -- an up-front re-add would be
        # undone by that unassign. The deactivate consumer applies the held
        # re-assignment AFTER that clear, so the next eligible Plan sees the
        # train assigned with no RUNNING run and allocates a fresh run. Consumed
        # only by the route_deactivate consumer; abort/power-off never record it.
        self._pending_reassign: Dict[str, str] = {}
        # Held route-assignment request for a train that still has a live
        # RUNNING run (per design_new.md "Route assignment writers", lines
        # 1530-1545). When an operator retargets, swaps, or unassigns a train
        # whose run is still live, `request_route_assignment(...)` does NOT
        # mutate either route's `assigned_train_ids` inline: it writes the abort
        # + inclusive cancel latches and stashes the request here as
        # `train_id -> {"requested_route_id": Optional[str], "start_override":
        # Optional[dict]}`. The Action teardown then commits it inside
        # `remove_train_route_run(...)` -- after the old run is archived and
        # unassigned -- so no stored assignment ever points at the replacement
        # route while the old run is still RUNNING. `requested_route_id is None`
        # is a held unassign (the train ends up on no route). This is request
        # state, NOT an assignment: `assigned_route_id_for(...)` never reports
        # it. Distinct from `_pending_reassign`, the narrower same-route
        # structural-edit re-add applied by the deactivate consumer.
        self._pending_route_assignments: Dict[str, Dict[str, Any]] = {}
        # Routes awaiting deferred deletion (per design_new.md "Route deletion
        # waits for live teardown"). `DELETE /api/train-routes`
        # never removes a route used by a live or pending run inline: it sets
        # `Route.is_active = False`, records the id here, and fans out the
        # per-train deactivate teardown. The registry retains the `Route` while
        # any train remains assigned or any RUNNING run names it, so
        # `get_route(...)` keeps resolving for every live run during Plan and
        # Action. `finalize_pending_route_deletions()` -- called after each
        # Action teardown and each natural finish -- archives + unregisters a
        # pending route once nothing holds it.
        self.pending_route_deletions: Set[str] = set()
        # Immutable final route definitions kept after a deferred deletion
        # archives them out of the active registry (per design_new.md line
        # 2102), so historical `RouteRun.task_runs[*].task_index` and run-panel
        # serialization can still resolve the task definitions they reference.
        # `get_route(...)` falls back to this map when the active registry has
        # no live copy. Keyed by route_id.
        self._historical_routes: Dict[str, "Route"] = {}

        # Per-tick coordinator state (design_new.md "Contention snapshot" +
        # "Meet coordination"). Read-only tick-start views rebuilt every tick by
        # `build_tick_coordination(...)` (after order_runnable_trains, before the
        # per-train loop); retained across no ticks, so they are not latches and
        # hold no Latch-Table entry.
        #   contention_snapshot: the tick's ContentionSnapshot — `edges` (one per
        #     holder of each blocked waiter, for wedge/meet detection). Built from
        #     the full PathDispatcher.get_waiting_conflict(...).
        #   meet_assignment: train_id -> MeetCeiling capping how far it may
        #     reserve this tick and pinning which candidate it may acquire when a
        #     higher-priority train needs the same track.
        self.contention_snapshot: ContentionSnapshot = ContentionSnapshot()
        #   wedge_report: the tick's WedgeReport — the set of deadlocks present,
        #     each a set of trains that are each stopped with nowhere to go and
        #     waiting on a resource another member holds. Built between the
        #     contention snapshot and the meet assignment (design_new.md "Wedge
        #     detection"). Read-only; not a latch.
        self.wedge_report: WedgeReport = WedgeReport()
        self.meet_assignment: Dict[str, "MeetCeiling"] = {}
        # Trains for which meet coordination found no holding point on the last
        # pass, so it emitted the live `meet_hold` diagnostic and left the key
        # active. `MeetCoordinator` (rebuilt each tick) reconciles this set each
        # pass and calls `NotificationCenter.resolve_condition(...)` for every
        # train that has dropped out of it (design_new.md "Error events",
        # Resolution and recurrence).
        self.meet_hold_train_ids: Set[str] = set()
        # Condition keys for the wedges reported on the last pass, so the next
        # pass can call `NotificationCenter.resolve_condition(...)` for every
        # wedge that has cleared (design_new.md "Error events", Resolution and
        # recurrence). One key per wedge, `"wedge:<sorted train ids>"`.
        self.wedge_condition_keys: Set[str] = set()

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
        ``current_route_status == RUNNING``. Observe, Plan, Action, and route
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
             been called but Plan has not yet allocated an exec_state.
        """
        state = self.execution_states.get(train_id)
        if state is not None:
            return state.route_id
        for route_id, route in self.route_registry.items():
            if train_id in getattr(route, "assigned_train_ids", []):
                return route_id
        return None

    def request_route_assignment(
        self,
        train_id: str,
        requested_route_id: Optional[str],
        start_override: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Canonical assignment-request entry (design_new.md "Route assignment
        writers"). Every HTTP/UI/admin assignment writer routes
        through here instead of mutating `Route.assigned_train_ids` directly.

        `requested_route_id is None` means unassign. `start_override`, when
        supplied, is the per-assignment one-shot start-mode bundle
        (`{"start_mode": RouteStartMode, "start_task_index": Optional[int]}`)
        that seeds the next fresh run; `None` means "use the route default".

        Three cases, keyed on whether a live RUNNING run exists:

          * No RUNNING run -> commit immediately: clear older pending route/start
            state, unassign the train from any old route, assign the requested
            route when non-None, and stash the start override for the next fresh
            run allocation.
          * A RUNNING run exists and the request names that same route -> an
            idempotent no-op; a supplied start override is dropped because the
            run already consumed its start mode.
          * A RUNNING run exists and the request names a different route (or
            None) -> defer: write the abort + inclusive-cancel latches, clear
            older pending state, and stash the request on
            `_pending_route_assignments[train_id]` WITHOUT touching either
            route's `assigned_train_ids`. The Action teardown commits it inside
            `remove_train_route_run(...)`.

        This method performs the mechanism only; the caller validates the route
        (existence, non-empty tasks, conflict/force) before invoking it.
        """
        # External assignment waits for calibration teardown (design_new.md
        # "Speed calibration", External assignment waits for calibration
        # teardown). A request to assign a non-calibration route while a speed
        # pass is open first abandons the pass — which tears its active leg route
        # down through the ordinary route-deactivate latch — then holds the
        # requested route so the Action deactivation consumer applies it after the
        # leg route is gone. Because Plan has already run for this train this tick,
        # the requested route cannot allocate or move until the next Plan pass.
        if self._abandon_calibration_for_external_assignment(
            train_id, requested_route_id, start_override
        ):
            return

        run = self.execution_state_for(train_id)

        # A RUNNING run on the very route being (re-)requested: idempotent
        # no-op. The run already consumed its start mode, so a supplied start
        # override is dropped rather than applied.
        if run is not None and requested_route_id == run.route_id:
            return

        # An explicit request supersedes any held same-route structural
        # re-allocation: a stale `_pending_reassign` entry must not fire on a
        # later deactivate of this train (design_new.md, the explicit
        # "Retargeting/swap uses the general held-assignment mechanism" rule).
        self.discard_pending_reassign(train_id)

        if run is None:
            # Commit immediately -- no live run to tear down first.
            self._pending_route_assignments.pop(train_id, None)
            self.clear_pending_start_override(train_id)
            old_route_id = self.assigned_route_id_for(train_id)
            if old_route_id:
                old_route = self.route_registry.get(old_route_id)
                if old_route is not None:
                    old_route.unassign_train(train_id)
            if requested_route_id is not None:
                new_route = self.route_registry.get(requested_route_id)
                if new_route is not None:
                    new_route.assign_train(train_id)
                if start_override is not None:
                    self._pending_start_overrides[train_id] = dict(start_override)
            # A new route assignment resets the ATO engage bit (design_new.md
            # "ATO status"): the fresh run starts not engaged (`ato_status ==
            # READY`) and the operator issues `/ato/go` to start it. This is an
            # operator-initiated write between ticks, cleared here where the
            # assignment commits immediately.
            controller = self._controller_by_id(train_id)
            if controller is not None:
                controller.commands.engaged = False
            return

        # A live run exists and the request names a different route (or None):
        # defer. Write the abort + inclusive cancel so Action tears the old run
        # down through the canonical workflow, then hold the request; do NOT
        # mutate either route's `assigned_train_ids` yet.
        controller = self._controller_by_id(train_id)
        # A pending teardown holds the request (design_new.md, "A pending teardown holds the request"): when a
        # teardown latch is already in flight for this train, write no second
        # teardown latch — the consumer already scheduled applies whatever
        # `_pending_route_assignments` holds when it removes the run. Only the
        # held request below is overwritten.
        teardown_already_pending = controller is not None and bool(
            controller.active_conditions & _HELD_REQUEST_TEARDOWN_CONDITIONS
        )
        if controller is not None and not teardown_already_pending:
            has_active_path = (
                controller.path_projection.received_path_plan is not None
            )
            controller.route_hooks.phase_latches.set_route_abort(
                reason=("unassign" if requested_route_id is None else "retarget"),
                has_active_path=has_active_path,
            )
        # Clear any older held request/start state, THEN store the fresh one.
        self._pending_route_assignments.pop(train_id, None)
        self.clear_pending_start_override(train_id)
        self._pending_route_assignments[train_id] = {
            "requested_route_id": requested_route_id,
            "start_override": None if start_override is None else dict(start_override),
        }

    def _commit_pending_route_assignment(self, train_id: str) -> None:
        """Consume any held `_pending_route_assignments[train_id]` and apply its
        requested replacement (design_new.md "Route Edit And Reconciliation").

        Called by `remove_train_route_run(...)` as its last state mutation,
        once the old run has been archived and unassigned. When the held
        `requested_route_id` is non-None the train is assigned to that route and
        the held start override moves to `_pending_start_overrides` for the next
        fresh run; when it is None the train stays unassigned. A no-op when no
        request is held.
        """
        pending = self._pending_route_assignments.pop(train_id, None)
        if not pending:
            return
        requested_route_id = pending.get("requested_route_id")
        if requested_route_id is None:
            # Held unassign: the train stays on no route; drop the start
            # override too (there is no run for it to seed).
            return
        route = self.route_registry.get(requested_route_id)
        if route is not None:
            route.assign_train(train_id)
        start_override = pending.get("start_override")
        if start_override is not None:
            self._pending_start_overrides[train_id] = dict(start_override)

    def _abandon_calibration_for_external_assignment(
        self,
        train_id: str,
        requested_route_id: Optional[str],
        start_override: Optional[Dict[str, Any]],
    ) -> bool:
        """Handle a non-calibration route requested while a speed-calibration
        pass is open (design_new.md "Speed calibration", External assignment waits
        for calibration teardown).

        Abandons the pass — which tears its active leg route down through the
        route-deactivate latch — and, when a leg route was armed, holds the
        requested route so the Action deactivation consumer applies it after the
        leg route is gone. Returns True when it took over the request (the caller
        returns), False to fall through to the ordinary assignment flow.
        """
        if requested_route_id is None:
            return False
        sc = getattr(self, "simulation_controller", None)
        calibration = getattr(sc, "speed_calibration", None) if sc is not None else None
        if calibration is None:
            return False
        cal_run = calibration.runs.get(train_id)
        if cal_run is None:
            return False
        # The pass's own leg route arriving here would be pathological; leave it
        # to the ordinary flow rather than abandoning the pass over it.
        if requested_route_id == cal_run.active_leg_route_id:
            return False

        had_leg_route = cal_run.active_leg_route_id is not None
        # Abandon the pass first. When a leg is armed, abandon() sets the
        # route-deactivate latch for the active leg route.
        calibration.abandon(train_id, reason="external route assignment requested")
        if not had_leg_route:
            # No leg route to tear down, so no RUNNING run to wait on: let the
            # ordinary flow assign the requested route immediately.
            return False

        # Hold the requested route; the Action deactivation consumer
        # (remove_train_route_run) applies it once the leg route is torn down.
        self.discard_pending_reassign(train_id)
        self._pending_route_assignments.pop(train_id, None)
        self.clear_pending_start_override(train_id)
        self._pending_route_assignments[train_id] = {
            "requested_route_id": requested_route_id,
            "start_override": None if start_override is None else dict(start_override),
        }
        return True

    def route_run_summary_for(self, train_id: str) -> Optional[Dict[str, Any]]:
        """Five-field summary of the train's currently-`RUNNING` `RouteRun`,
        or ``None`` when the train has no RUNNING run.

        Per design_new.md "Data Flow To UI", the composite
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
            "display_state": self._display_state_for(train_id, run),
            "last_error": self.latest_run_error(run),
        }

    def _display_state_for(self, train_id: str, run: RouteExecutionState) -> str:
        """Assemble the read-model `display_state` for a run (design_new.md
        "Route-run state", `display_state`).

        Starts from the run-only projection (terminal / STALLED / RUNNING) and
        adds the `"CANCELLED"` case, which STALLED outranks: when the run is
        non-terminal and not stalled (the run-only projection is `"RUNNING"`),
        holds no live path (`PathDispatcher.active_path_id_by_train(train_id) is
        None`), and the train's most recently terminated `Path` record ended
        `CANCELLED` (not `COMPLETED`), the projection is `"CANCELLED"` — the UI
        reads the reason from that path's `cancel_path` event. A fresh
        `create_path(...)` gives the run a live path again, so the projection
        returns to `"RUNNING"` on the next read with no stored field to clear.
        """
        from enums import PathStatus

        base = run.display_state
        if base != "RUNNING":
            return base
        dispatcher = self.path_dispatcher
        if dispatcher is None:
            return base
        if dispatcher.active_path_id_by_train(train_id) is not None:
            return base
        terminated = dispatcher.most_recently_terminated_path(train_id)
        if terminated is not None and terminated.status == PathStatus.CANCELLED:
            return "CANCELLED"
        return base

    def _run_is_at_started_dwell(self, run: RouteExecutionState) -> bool:
        """True when the run's current walk task is a `dwell` row whose
        `started_at` is set and whose `finished_at` is not — the route-owned
        "at station" read (design_new.md "Data Flow To UI", UI data-flow
        anchor). The next non-finished walk position is that current task.
        """
        if not run.task_runs:
            return False
        pos = run.next_planning_walk_position
        if not (0 <= pos < len(run.task_runs)):
            return False
        row = run.task_runs[pos]
        if row.started_at is None or row.finished_at is not None:
            return False
        route = self.route_registry.get(run.route_id) if run.route_id else None
        if route is None:
            return False
        task = route.get_task_at(row.task_index) if hasattr(route, "get_task_at") else None
        return task is not None and getattr(task, "task_kind", None) == "dwell"

    def ato_status_for(self, train_id: str) -> str:
        """Assemble the read-model `ato_status` projection for a train
        (design_new.md "ATO status").

        Computed from the controller's derived `train_status`,
        `commands.engaged`, and stopped/moving predicate plus the train's
        route-run state (whether a RUNNING run exists and whether its current
        walk task is a started, unfinished `dwell` row). Display-only; no
        control logic reads it. Returns the `AtoStatus` value string.
        """
        from enums import AtoStatus, TrainStatus

        controller = self._controller_by_id(train_id)
        if controller is None or controller.ops.train_status != TrainStatus.ATO:
            return AtoStatus.OFF.value

        run = self.execution_states.get(train_id)
        has_running_run = (
            run is not None and run.current_route_status == RouteRunStatus.RUNNING
        )
        # READY: not engaged, or no RUNNING run to pursue (freshly selected,
        # paused by /ato/stop, or its route finished / was torn down).
        if not controller.commands.engaged or not has_running_run:
            return AtoStatus.READY.value
        # Engaged with a RUNNING run.
        if self._run_is_at_started_dwell(run):
            return AtoStatus.DWELLING.value
        if not controller.is_stopped():
            return AtoStatus.RUNNING.value
        # Engaged, stopped, and not at a station (target not yet reached).
        return AtoStatus.HELD.value

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

        Per design_new.md "RouteExecutor.history_for_train": includes RUNNING,
        FINISHED, and DEACTIVATED rows across all routes the train has been
        assigned to. `run_number` is the global monotonic creation counter, so
        ascending `run_number` is chronological creation order, not
        route-index-major.
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

    def _now_operating(self) -> float:
        """The current `AppState.operating_time` reading (design_new.md
        "Clocks").

        Monotonic seconds of *operation*: it advances only while the session is
        running, so an operator's stop neither consumes a dwell nor makes a
        train late. The runtime reaches the accumulator through
        `SimulationController.operating_clock`, the seam `AppState` wires. A
        route executor built without a runtime falls back to wall time, which
        is the same reading for a session nothing ever stops.
        """
        sim = getattr(self, "simulation_controller", None)
        clock = getattr(sim, "operating_clock", None)
        return float(clock()) if callable(clock) else time.time()

    def _now_basis(self, route: "Route") -> float:
        """The reading of whichever clock this route's `timing_basis` names."""
        if getattr(route, "timing_basis", "operating") == "realtime":
            return self._now()
        return self._now_operating()

    def dwell_remaining_seconds(
        self,
        exec_state: RouteExecutionState,
        route: "Route",
        walk_position: int,
    ) -> float:
        """Seconds remaining before a `dwell` walk-position is depart-ready.

        The one place the UI-facing countdown is computed, so the wall/basis
        clock split lives here rather than at each payload site. A row with a
        `target_time` counts down in wall seconds; a row holding for
        `task_time` counts down against the route's `timing_basis` (design_new.md
        "Task time semantics").
        """
        if not (0 <= walk_position < len(exec_state.task_runs)):
            return 0.0
        row = exec_state.task_runs[walk_position]
        if not (0 <= row.task_index < len(route.tasks)):
            return 0.0
        task = route.tasks[row.task_index]
        return task.get_remaining_dwell_time(
            self._now(),
            self._now_basis(route),
            self._predecessor_finish_basis(exec_state, route, walk_position),
        )

    def find_armed_task(self, train_id: str) -> Optional["ArmedTaskProjection"]:
        """The task whose target the live authority was most recently acquired
        toward, or `None`.

        Per design_new.md "ArmedTaskProjection schema": it requires a `RUNNING`
        run and an active dispatcher path, then selects the greatest
        non-finished walk-position whose `resolved_target` is non-`None`.
        Append-only walk order plus step `66aa`'s single-writer rule make that
        the most recently acquired row — including a waypoint-lookahead row
        sitting ahead of the physically served task.

        It never resolves, appends, or writes, and returns `None` when there is
        no live authority or no acquired row.
        """
        exec_state = self.execution_state_for(train_id)
        if exec_state is None:
            return None
        if self._get_active_path_id(train_id) is None:
            return None
        route = self.route_registry.get(exec_state.route_id)
        for pos in range(len(exec_state.task_runs) - 1, -1, -1):
            row = exec_state.task_runs[pos]
            if row.finished_at is not None or row.resolved_target is None:
                continue
            task = route.get_task_at(row.task_index) if route is not None else None
            return ArmedTaskProjection(
                walk_position=pos,
                task_index=row.task_index,
                task_kind=getattr(task, "task_kind", "") if task is not None else "",
                stored_resolved_target=row.resolved_target,
            )
        return None

    def find_next_completion_task(
        self, train_id: str
    ) -> Optional["CompletionTaskProjection"]:
        """The next physical task eligible for stop completion, or `None`.

        Per design_new.md "CompletionTaskProjection schema": the public read used
        only by `PathDispatcher.try_complete_active_path()`. It returns the
        lowest non-finished walk-position of the train's `RUNNING` run, carrying
        that row's `task_kind` and stored `resolved_target`, without appending or
        resolving a new target. `PathDispatcher` reads only `task_kind` and
        `stored_resolved_target` from it and never indexes route storage itself.

        It returns `None` when the train has no `RUNNING` run (`execution_state_
        for(...)` is `None`) or when every walk-position is already finished (a
        terminal walk has no unfinished row). Unlike `find_armed_task(...)` it
        does NOT require an active dispatcher path — the stop-arrival caller has
        already confirmed the live path exists before it asks which row that
        path is serving.
        """
        exec_state = self.execution_state_for(train_id)
        if exec_state is None:
            return None
        route = self.route_registry.get(exec_state.route_id)
        for pos, row in enumerate(exec_state.task_runs):
            if row.finished_at is not None:
                continue
            task = route.get_task_at(row.task_index) if route is not None else None
            task_kind = getattr(task, "task_kind", "") if task is not None else ""
            # A `run` whose timer has elapsed is a stop at the current authority
            # end (design_new.md "run task"): present it as a `stop` so the
            # stop-arrival check completes it there. Plan step `6a0d` has frozen
            # this row's `resolved_target` on that authority-end block.
            if task_kind == "run":
                run_time = (
                    task.task_time
                    if (task is not None and task.task_time and task.task_time > 0.0)
                    else 0.0
                )
                if row.run_elapsed_seconds >= run_time:
                    task_kind = "stop"
            return CompletionTaskProjection(
                walk_position=pos,
                task_index=row.task_index,
                task_kind=task_kind,
                stored_resolved_target=row.resolved_target,
            )
        return None

    def _in_flight_row_pos_for_task(
        self, exec_state: RouteExecutionState, task_index: int
    ) -> int:
        """The walk-position of the unfinished row representing `task_index`.

        For the initial walk-positions the list index matches `task_index`, but
        after `CIRCULAR` / `ROUND_TRIP` wraparound several list positions share
        one `task_index` — the in-flight one is the latest entry whose
        `finished_at` is still `None`, so walk backward and take the first
        match. Returns -1 when there is none.
        """
        for pos in range(len(exec_state.task_runs) - 1, -1, -1):
            r = exec_state.task_runs[pos]
            if r.task_index == task_index and r.finished_at is None:
                return pos
        return -1

    def _predecessor_finish_basis(
        self, exec_state: RouteExecutionState, route: "Route", row_pos: int
    ) -> Optional[float]:
        """The directed-walk predecessor's finish, read on the route's basis.

        Walk-position `0` anchors on the run's own start. Reading both ends on
        one clock is what keeps a duration from subtracting a wall instant from
        an operating-clock one (design_new.md "RouteTaskRun schema",
        predecessor timestamp lookup).
        """
        realtime = getattr(route, "timing_basis", "operating") == "realtime"
        if row_pos == 0:
            return (
                exec_state.started_at if realtime else exec_state.started_at_operating
            )
        prev = exec_state.task_runs[row_pos - 1]
        return prev.finished_at if realtime else prev.finished_at_operating

    def _allocate_run_number(self) -> int:
        """Return the next globally-monotonic ``RouteRun.run_number``.

        Per design_new.md "Route-run state": ``_next_run_number`` starts at 1,
        supplies the current value, and increments after every allocation
        regardless of route. The result is an opaque history/API identity — it
        carries no route index and must not be parsed for ``route_id`` (the
        immutable ``RouteRun.route_id`` field carries that relationship
        directly). Numbers are never recycled or recomputed during the process
        lifetime.
        """
        n = self._next_run_number
        self._next_run_number += 1
        return n

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
            else:
                # No current RouteRun (unrouted, or assigned-but-not-yet-
                # allocated in the assign-to-allocation window): running tier at
                # effective priority 5, whatever the assigned route's priority
                # (design_new.md "Per-train tick transaction", No-`RouteRun`
                # default: ordered like a running-tier run at effective priority 5).
                priority = 5
                starting = False
            # Ascending stable sort: tier 0 (running) before tier 1 (starting);
            # `-priority` puts higher effective priority first within a tier.
            return (1 if starting else 0, -priority)

        ordered.sort(key=sort_key)
        return ordered

    # ------------------------------------------------------------------
    # Per-tick coordination (design_new.md "Contention snapshot" +
    # "Meet coordination"). Built after order_runnable_trains and
    # before the first train's Plan.
    # ------------------------------------------------------------------

    def build_tick_coordination(self, ordered_train_ids: List[str]) -> None:
        """Rebuild the contention snapshot and meet assignment for this tick.

        design_new.md "Contention snapshot" + "Wedge detection" + "Meet
        coordination": all three are read-only tick-start views — recomputed
        from scratch every tick and retained across none. The order is the
        doc's: the contention snapshot is built first; the wedge report reads
        that snapshot's edges; meet coordination then seeds its conflict
        detection from the same snapshot's contending pairs.
        """
        self.contention_snapshot = self._build_contention_snapshot(ordered_train_ids)
        self.wedge_report = self._build_wedge_report(ordered_train_ids)
        self.meet_assignment = self._build_meet_assignment(ordered_train_ids)

    def _build_contention_snapshot(
        self, ordered_train_ids: List[str]
    ) -> ContentionSnapshot:
        """Build the tick's `ContentionSnapshot` (design_new.md "Contention
        snapshot").

        For each train with an armed task, run the full
        `PathDispatcher.get_waiting_conflict(...)` (with no meet constraint —
        the snapshot measures the unconstrained live conflict meet coordination
        must resolve) and emit one edge per holder. Edges come out in
        runnable-order (the waiter loop) then holder-order (the conflict's holder
        list). The snapshot carries `edges` only; wedge and meet detection read
        priority fresh from the route registry when they need it.
        """
        edges: List[ContentionEdge] = []
        for train_id in ordered_train_ids:
            conflict = self._waiting_conflict_for(train_id)
            if conflict is None or not conflict.holding_train_ids:
                continue
            for holder_id in conflict.holding_train_ids:
                edges.append(
                    ContentionEdge(
                        waiting_block_id=conflict.waiting_block_id,
                        waiting_train_id=train_id,
                        holding_train_id=holder_id,
                    )
                )
        return ContentionSnapshot(edges=edges)

    def _build_meet_assignment(
        self, ordered_train_ids: List[str]
    ) -> Dict[str, "MeetCeiling"]:
        """Per-tick `train_id -> MeetCeiling` (design_new.md "Meet coordination").

        Delegated to `MeetCoordinator`, which reads the just-built
        `contention_snapshot` to seed conflict detection.
        """
        from meet_coordination import MeetCoordinator

        return MeetCoordinator(self).build(ordered_train_ids)

    # ------------------------------------------------------------------
    # Wedge detection (design_new.md "Wedge detection"). Built between the
    # contention snapshot and the meet assignment, once per tick, reading
    # tick-start committed state only.
    # ------------------------------------------------------------------

    def _build_wedge_report(self, ordered_train_ids: List[str]) -> WedgeReport:
        """Find the deadlocks in this tick's wait-for graph and report them
        (design_new.md "Wedge detection").

        The graph is `contention_snapshot.edges`: a directed edge from a waiting
        train to a train holding the resource it needs next. A wedge is a set of
        trains where every member waits on another member and is waited on — a
        cycle in that graph, including the one-train case where a train's edge
        points back at itself. Only trains that are stopped with nowhere to go
        take part, so the graph is restricted to edges whose waiter and holder
        are both such trains: a train still rolling, or one with room to advance
        under its own authority, is never a member.

        Each wedge is emitted once through `NotificationCenter` and its key is
        resolved when the wedge clears; the report itself changes no runtime
        state (it gates no planning step, caps no acquisition, cancels no path).
        """
        participants = {
            train_id
            for train_id in ordered_train_ids
            if self._is_wedge_participant(train_id)
        }
        wedges = self._find_wedges(self.contention_snapshot.edges, participants)
        report = WedgeReport(wedges=wedges)
        self._emit_wedge_events(wedges)
        return report

    def _is_wedge_participant(self, train_id: str) -> bool:
        """True when `train_id` is stopped with nowhere to go — the wedge
        membership predicate (design_new.md "Wedge detection").

        Three conditions, all from tick-start controller state:
        - it holds live authority (`received_path_plan is not None`); a train
          with no authority holds nothing it acquired, so it cannot be wedged,
          and why its task could not be served is reported elsewhere;
        - `TrainController.is_stopped()` is true — a train still rolling is never
          a member, because the resource it waits for may clear before it gets
          there;
        - it is standing at its live authority boundary with no clearance left
          to advance: the lookahead's dominant restriction is the zero-clearance
          hold against that authority, which reads as reason code
          `RESERVATION_STOP` on the leading speed curve. This is what excludes a
          stopped train that still has room to roll forward under its own
          authority.
        """
        controller = self._controller_by_id(train_id)
        if controller is None:
            return False
        projection = getattr(controller, "path_projection", None)
        if getattr(projection, "received_path_plan", None) is None:
            return False
        if not controller.is_stopped():
            return False
        try:
            payload = controller.get_lookahead()
        except Exception:
            return False
        curves = ((payload or {}).get("lookahead", {}) or {}).get("speed_curves") or []
        if not curves:
            return False
        return (
            curves[0].get("atc_limit_reason_code")
            == AtcLimitReasonCode.RESERVATION_STOP.value
        )

    @staticmethod
    def _find_wedges(
        edges: List[ContentionEdge], participants: Set[str]
    ) -> List[Wedge]:
        """Return the wedges in the wait-for graph (design_new.md "Wedge
        detection").

        Nodes are trains; a directed edge waiter -> holder means the waiter
        cannot grow its authority because the holder is in the way. Only edges
        whose waiter and holder are both in `participants` are kept, because
        every member of a wedge must itself be stopped with nowhere to go. A
        wedge is a strongly connected component that contains a cycle: two or
        more trains that block each other in a ring, or one train whose own
        edge points back at itself. Each wedge names its member trains (sorted)
        and the contested blocks the cycle runs through — the `waiting_block_id`
        of every kept edge that stays inside the component (sorted).
        """
        # Keep only edges internal to the participant set; record the blocks
        # each ordered (waiter, holder) pair is contested on, and which trains
        # hold the resource they themselves need next (the one-train wedge).
        succ: Dict[str, Set[str]] = {}
        pair_blocks: Dict[Tuple[str, str], Set[str]] = {}
        self_loop: Set[str] = set()
        for edge in edges:
            waiter = edge.waiting_train_id
            holder = edge.holding_train_id
            if waiter not in participants or holder not in participants:
                continue
            pair_blocks.setdefault((waiter, holder), set()).add(edge.waiting_block_id)
            if waiter == holder:
                self_loop.add(waiter)
                # Keep the node in the graph so it becomes its own component;
                # its self-loop is not a cross-edge, so it stays out of `succ`'s
                # neighbour sets and is recognized as a one-train wedge below.
                succ.setdefault(waiter, set())
                continue
            succ.setdefault(waiter, set()).add(holder)
            succ.setdefault(holder, set())

        components = RouteExecutor._strongly_connected_components(succ)

        wedges: List[Wedge] = []
        for component in components:
            is_wedge = len(component) > 1 or (
                len(component) == 1 and next(iter(component)) in self_loop
            )
            if not is_wedge:
                continue
            blocks: Set[str] = set()
            for (waiter, holder), block_ids in pair_blocks.items():
                if waiter in component and holder in component:
                    blocks |= block_ids
            wedges.append(
                Wedge(
                    train_ids=tuple(sorted(component)),
                    block_ids=tuple(sorted(blocks)),
                )
            )
        # Deterministic order: by the member-train tuple.
        wedges.sort(key=lambda w: w.train_ids)
        return wedges

    @staticmethod
    def _strongly_connected_components(
        succ: Dict[str, Set[str]]
    ) -> List[Set[str]]:
        """Tarjan's strongly-connected-components, iterative so a long blocking
        chain cannot overflow the recursion stack. `succ` maps each node to its
        out-neighbours; every node that appears (as key or neighbour) is
        included in exactly one returned component."""
        index_of: Dict[str, int] = {}
        low_of: Dict[str, int] = {}
        on_stack: Set[str] = set()
        stack: List[str] = []
        components: List[Set[str]] = []
        counter = 0

        nodes = set(succ.keys())
        for neighbours in succ.values():
            nodes |= neighbours

        for root in nodes:
            if root in index_of:
                continue
            # work stack of (node, iterator over its successors)
            work: List[Tuple[str, Any]] = [(root, iter(sorted(succ.get(root, set()))))]
            index_of[root] = low_of[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                node, it = work[-1]
                advanced = False
                for nxt in it:
                    if nxt not in index_of:
                        index_of[nxt] = low_of[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, iter(sorted(succ.get(nxt, set())))))
                        advanced = True
                        break
                    if nxt in on_stack:
                        low_of[node] = min(low_of[node], index_of[nxt])
                if advanced:
                    continue
                # node fully explored — if it is a component root, pop one SCC
                if low_of[node] == index_of[node]:
                    component: Set[str] = set()
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.add(w)
                        if w == node:
                            break
                    components.append(component)
                work.pop()
                if work:
                    parent = work[-1][0]
                    low_of[parent] = min(low_of[parent], low_of[node])
        return components

    def _emit_wedge_events(self, wedges: List[Wedge]) -> None:
        """Emit one operator event per wedge and resolve the ones that cleared
        (design_new.md "Wedge detection" + "Error events").

        Each wedge carries a stable `condition_key` built from its sorted member
        trains, so `NotificationCenter` drops the repeat while the same wedge
        persists and appends a fresh event only when its payload changes (its
        trains or contested blocks). A wedge present last pass but not this one
        has its key resolved, so `latest_error_for(...)` stops naming it. The
        event names the trains in the cycle and the blocks the cycle runs
        through; it carries `operation = "build_wedge_report"`, `kind =
        "warning"`, and `ErrorRecord.origin = "wedge"`.
        """
        active_now: Set[str] = set()
        for wedge in wedges:
            active_now.add(self._wedge_condition_key(wedge))
        sc = getattr(self, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is not None:
            for wedge in wedges:
                self._emit_one_wedge(nc, wedge)
            for key in self.wedge_condition_keys - active_now:
                try:
                    nc.resolve_condition(key)
                except Exception:
                    pass
        self.wedge_condition_keys = active_now

    @staticmethod
    def _wedge_condition_key(wedge: Wedge) -> str:
        """The stable key naming this wedge's live condition — its sorted member
        trains, so the same set of deadlocked trains keeps one key across ticks
        and the emit and the resolve match."""
        return "wedge:" + ",".join(wedge.train_ids)

    def _emit_one_wedge(self, nc, wedge: Wedge) -> None:
        """Emit the keyed warning for one wedge (design_new.md "Wedge
        detection")."""
        try:
            from notification_center import block_ref as _bref, train_ref as _tref

            trains = ", ".join(wedge.train_ids)
            blocks = ", ".join(wedge.block_ids)
            if len(wedge.train_ids) == 1:
                message = (
                    f"wedge: train {trains} is stopped holding the resource its "
                    f"own authority needs next"
                )
                if blocks:
                    message += f" (contested block: {blocks})"
            else:
                message = (
                    f"wedge: trains {trains} are each stopped waiting on a "
                    f"resource another holds, so none can move"
                )
                if blocks:
                    message += f" (contested blocks: {blocks})"
            entities = [_tref(t) for t in wedge.train_ids]
            entities += [_bref(b) for b in wedge.block_ids]
            nc.emit(
                source="RouteExecutor",
                operation="build_wedge_report",
                kind=Kind.WARNING,
                message=message,
                entities=entities,
                error=ErrorRecord(message, "wedge"),
                payload={
                    "train_ids": list(wedge.train_ids),
                    "block_ids": list(wedge.block_ids),
                },
                event_time=self._now(),
                condition_key=self._wedge_condition_key(wedge),
            )
        except Exception:
            pass

    def effective_priority_for(self, train_id: str) -> int:
        """Public resolver for a train's run priority, used by `PathDispatcher`
        Preemption (design_new.md "Preemption", Priority is read through an
        injected reader). Higher is stronger — `order_runnable_trains` sorts by
        descending effective priority, and Preemption takes track only from a
        strictly-lower-priority holder. Same rule as the private reader below.
        """
        return self._effective_priority_for(train_id)

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

    def _waiting_conflict_for(self, train_id: str):
        """The `WaitingConflict` for this train's blocked active path, or None.

        design_new.md "Contention snapshot": obtain `stored_resolved_target` from
        `find_armed_task(train_id)`, resolve the run's manual override, and call
        the full `PathDispatcher.get_waiting_conflict(...)` with no meet constraint
        (`reservation_cap_block_id=None`, `required_candidate_resource_keys=None`).
        """
        if self.path_dispatcher is None:
            return None
        armed = self.find_armed_task(train_id)
        if armed is None:
            return None
        manual_override = self._manual_override_for(train_id)
        return self.path_dispatcher.get_waiting_conflict(
            train_id,
            armed.stored_resolved_target,
            manual_override,
            reservation_cap_block_id=None,
            required_candidate_resource_keys=None,
        )

    def waiting_for_block_id_for(self, train_id: str) -> Optional[str]:
        """Route-supplied `waiting_for_block_id` for the composite read-model.

        design_new.md "Data Flow To UI" / "Contention snapshot": the UI payload
        composes this field here, not inside `get_active_path_progress(...)`. It
        obtains `stored_resolved_target` from `find_armed_task(train_id)`,
        resolves the run's manual override, then forwards the complete request to
        the canonical
        `PathDispatcher.get_waiting_for_block_id(...)` projection with no meet
        constraint (`reservation_cap_block_id=None`,
        `required_candidate_resource_keys=None`), matching the tick-start
        contention view. Returns `None` when there is no dispatcher, no armed
        task, or nothing blocking the forward suffix.
        """
        if self.path_dispatcher is None:
            return None
        armed = self.find_armed_task(train_id)
        if armed is None:
            return None
        manual_override = self._manual_override_for(train_id)
        return self.path_dispatcher.get_waiting_for_block_id(
            train_id,
            armed.stored_resolved_target,
            manual_override,
            reservation_cap_block_id=None,
            required_candidate_resource_keys=None,
        )

    def _manual_override_for(self, train_id: str) -> bool:
        """The `manual_override` flag for a train, from its assigned shared
        `Route` (design_new.md "`disabled` and `manual` availability flags"), matching what Plan
        step `66aa` passes to the dispatcher."""
        route_id = self.assigned_route_id_for(train_id)
        route = self.route_registry.get(route_id) if route_id else None
        return bool(getattr(route, "manual_override", False)) if route else False

    # ------------------------------------------------------------------
    # Route + RouteRun override helpers (per design_new.md
    # "RouteRun overrides" canonical home).
    # ------------------------------------------------------------------

    def get_route(self, route_id: str) -> Optional["Route"]:
        """Resolve a shared `Route` from the registry. Used by
        `RouteExecutionState.effective_*` helpers so they can fall back to
        the live shared value when no per-run override is present.

        Falls back to the archived `_historical_routes` copy when the active
        registry has no live route (design_new.md "Route Edit And Reconciliation"): a route removed by
        a deferred deletion must still resolve for historical-run serialization.
        A live route is only ever archived once nothing holds it, so no live run
        ever reaches the fallback -- Plan/Action always find their route in the
        active registry.
        """
        route = self.route_registry.get(route_id)
        if route is not None:
            return route
        return self._historical_routes.get(route_id)

    def set_run_override(
        self, train_id: str, field_name: str, value: Any
    ) -> Tuple[bool, str]:
        """Set one allowlisted override on the train's currently RUNNING run.

        Per design_new.md "RouteRun overrides" canonical home: only the v1
        allowlist (`priority`) is accepted. Validation is per-field.
        Historical (FINISHED / DEACTIVATED)
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
        calls `RouteExecutor.tick_train_plan(train_id, delta_seconds)` once
        for each runnable train as the entry point of Plan.
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
            exec_state = RouteExecutionState(
                train_id=train.train_id,
                route_id=route.route_id,
                started_at=now,
                started_at_operating=self._now_operating(),
                run_number=self._allocate_run_number(),
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
            # Both stamps, on the two clocks, so a successor's duration never
            # finds a `None` predecessor under the `"operating"` basis
            # (design_new.md "RouteTaskRun schema").
            exec_state.task_runs[i].finished_at = exec_state.started_at
            exec_state.task_runs[i].finished_at_operating = (
                exec_state.started_at_operating
            )
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

    # -------------------------------------------- speed-calibration leg (c9e1)
    def _prepare_calibration_leg(self, train_id: str, controller) -> None:
        """Plan step `c9e1`: advance the train's open speed-calibration pass and
        carry out its leg-route intents (design_new.md "Speed calibration", Each
        leg runs as its own one-off route).

        The pass decides; `RouteExecutor` executes. This is the only place speed
        calibration touches route registries: it reconciles the stored leg route
        against the live run, lets the pass advance, then builds/assigns a one-off
        route to arm a leg, tears the active leg route down to abandon one, and
        re-frames the span head between legs. A no-op for any train with no open
        auto pass.
        """
        sc = getattr(self, "simulation_controller", None)
        calibration = getattr(sc, "speed_calibration", None) if sc is not None else None
        if calibration is None:
            return
        run = calibration.runs.get(train_id)
        if run is None or not run.is_measuring:
            return

        # Reconcile the stored leg route against the live run. When it is no
        # longer this train's live RUNNING run — the leg finished through ordinary
        # stop completion, or a deactivate tore it down — clear the stored id and
        # sync `leg_armed`, so the pass may arm the next leg. A single-use leg
        # route whose run is gone (archived) is dropped from the registry too, so
        # leg routes do not accumulate across a pass.
        if run.active_leg_route_id is not None:
            exec_state = self.execution_state_for(train_id)
            if exec_state is None or exec_state.route_id != run.active_leg_route_id:
                finished_leg_route_id = run.active_leg_route_id
                run.active_leg_route_id = None
                if exec_state is None and str(finished_leg_route_id).startswith(
                    "CAL_LEG_"
                ):
                    self.unregister_route(finished_leg_route_id)
        run.leg_armed = run.active_leg_route_id is not None

        command = calibration.plan_tick(train_id)
        if command is None:
            return

        # A reversal re-frames the span to its other physical head end before the
        # leg's path is acquired (design_new.md "Speed calibration" and
        # "Reversed-frame re-expression"): the pass only reverses between legs,
        # with the train stopped, so re-expressing the two end locators is safe.
        # There is no operator cab; the leg's own authority would reframe the
        # span too, but calibration flips it up front so the next leg starts from
        # the intended end.
        if command.reverse_cab:
            target_head = (
                "end_a" if controller._framed_head_end == "end_b" else "end_b"
            )
            controller._apply_head_frame(target_head)

        if command.arm_leg_to:
            self._arm_calibration_leg(train_id, controller, run, command.arm_leg_to)

        if command.deactivate_route:
            # Abandon this leg by tearing its route down (design_new.md "Speed
            # calibration", Pass-abandonment cases). The inclusive latch cancels a
            # still-live path in the same step; an already-completed leg (its path
            # gone) needs no cancel.
            controller.route_hooks.phase_latches.set_route_deactivate(
                reason=f"calibration leg abandoned for {train_id}",
                has_active_path=controller.path_projection.received_path_plan
                is not None,
            )

    def _arm_calibration_leg(
        self, train_id: str, controller, run, far_block_id: str
    ) -> None:
        """Build, activate, and assign the one-off route for a new leg, and store
        its id on the run (design_new.md "Speed calibration", Each leg runs as its
        own one-off route).

        Arms only when no authority lingers: no stored leg route, no live path,
        and no RUNNING run. Otherwise the previous leg's teardown has not
        finished, so it waits and Plan retries next tick — the pass keeps
        `leg_armed` in step with `active_leg_route_id`, so an un-honored arm is
        re-offered.
        """
        if run.active_leg_route_id is not None:
            return
        if self._get_active_path_id(train_id) is not None:
            return
        if self.execution_state_for(train_id) is not None:
            return

        from train_route import Route, BlockTarget

        seq = getattr(self, "_calibration_leg_seq", 0) + 1
        self._calibration_leg_seq = seq
        route_id = f"CAL_LEG_{train_id}_{seq}"
        route = Route(
            route_id=route_id,
            name=f"speed-calibration leg {seq} for {train_id}",
            mode=RouteMode.ONE_OFF,
            manual_override=False,
            tasks=[
                RouteTask(
                    task_kind="stop",
                    task_location=BlockTarget(block_id=far_block_id),
                )
            ],
        )
        self.register_route(route)
        route.activate(self._now())
        route.assign_train(train_id)
        run.active_leg_route_id = route_id

    # ------------------------------------------------------------------ Plan
    def _run_plan_settlement(
        self, train_id: str, controller: "TrainController"
    ) -> None:
        """Plan settlement — the first half of a train's Plan pass, before the
        planning half resolves and acquires the next target (design_new.md Plan
        row steps `640a` / `9d8b`, and the **Stop-arrival procedure**).

        Step `640a` — head-advance consumption. Replay the route head-advance
        callback over the head-entries Observe recorded on
        `physics_sync.entered_blocks_this_tick`, in the order recorded
        (`sequence_index` order), each finishing at most one `waypoint` task via
        `RouteExecutor.handle_train_advanced(...)`; then clear the record.
        Observe itself finishes no route task.

        Step `9d8b` — stop arrival. After the head-advance consumption, when an
        active dispatcher path still exists and no path-cancel is pending (a
        pending cancel — deviation or explicit — outranks arrival), and the train
        is stopped at its authority end, call
        `PathDispatcher.try_complete_active_path(train_id)` through the injected
        dispatcher port. The train has already moved for this tick in Observe, so
        this is where the route asks whether it is now standing at the stop it was
        sent to.
        """
        entered = controller.physics_sync.entered_blocks_this_tick
        if entered:
            for path_id, event in entered:
                self.handle_train_advanced(train_id, path_id, event)
            controller.physics_sync.entered_blocks_this_tick = []

        if self.path_dispatcher is None:
            return
        if controller.route_hooks.phase_latches.path_cancel_request is not None:
            return
        if self.path_dispatcher.active_path_id_by_train(train_id) is None:
            return
        if controller.is_stopped_at_reservation_end():
            self.path_dispatcher.try_complete_active_path(train_id)

    def tick_train_plan(self, train_id: str, delta_seconds: float) -> None:
        """Canonical Plan entry point for one train.

        Per design_new.md "Planning Kernel" steps (1)-(7). Each step has its
        own gating predicate; steps 1-5 always run, steps 6-7 are gated by
        the Plan eligibility precondition. The settlement half (steps `640a` /
        `9d8b`) runs first, via `_run_plan_settlement(...)`.
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

        # If controller is OFF, Plan is skipped entirely.
        if not controller.powered:
            return

        # This tick's elapsed time, read by the braking-distance cap for its
        # one-tick cushion (design_new.md "Braking-distance lock").
        self._current_delta_seconds = float(delta_seconds)

        # ---------- c9e1: prepare the active speed-calibration leg -----------
        # Runs before the assigned-route lookup because a new leg has no assigned
        # route yet — this step is what creates and assigns it. A no-op for a
        # train with no open speed-calibration pass (design_new.md "Speed
        # calibration", Each leg runs as its own one-off route).
        self._prepare_calibration_leg(train_id, controller)

        route_id = self.assigned_route_id_for(train_id)
        route = None
        if route_id:
            route = self.route_registry.get(route_id)

        # No route assignment → no Plan work for this train.
        if route is None or not getattr(route, "is_active", False):
            return

        if train_id not in route.assigned_train_ids:
            route.assign_train(train_id)
        exec_state = self._ensure_run_for(train, route)

        # Terminal runs do nothing in Plan.
        if exec_state.current_route_status in (
            RouteRunStatus.FINISHED,
            RouteRunStatus.DEACTIVATED,
        ):
            return

        # ---------- Plan settlement (steps 640a, 9d8b) -----------------------
        # Consume the head-entered blocks Observe recorded and run the
        # stop-arrival check BEFORE the planning half — and before the teardown
        # early-return below: head-advance (640a) applies even under a pending
        # cancel latch (design_new.md "Planning Kernel"), and stop-arrival (9d8b) guards
        # itself on `path_cancel_request`. Finishing a stop and re-planning
        # therefore happen in one Plan pass (design_new.md "Stop-arrival
        # procedure").
        self._run_plan_settlement(train_id, controller)

        now = self._now()

        # Plan kernel — step IDs trace back to design_new.md "Planning
        # Kernel" section. The `9c4f` (RouteRun reallocation) step is
        # performed earlier inside `_ensure_run_for(...)`. There is no
        # deviation-cleanup step: a cancellation writes no RouteRun field; the
        # read-model `display_state` derives "CANCELLED" from the cancel_path
        # event and the terminated Path record, and returns to "RUNNING" once a
        # fresh create at step `66aa` gives the run a live path again.

        # ---------- Teardown outranks normal progress ------------------------
        # design_new.md "Teardown outranks normal progress": while any
        # teardown latch (path_cancel_request / route_abort / power_off /
        # route_deactivate) is set, steps `70c3`, `6a0d`, `a5c6`, and the dwell
        # step make no timing, progress, dwell, departure, or authority write,
        # and `c481` reads only far enough to see that no normal work may commit.
        if controller.active_conditions & _TEARDOWN_CONDITIONS:
            return

        # ---------- c481: helper lookup via _find_next_task_target -----------
        projection = self._find_next_task_target(train_id)
        if projection is None:
            # Terminal walk: finish the run, or defer to the revoke-cancel flow
            # when a live path is still held (design_new.md step `592a`).
            self._finish_or_defer_terminal_walk(train_id, controller)
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
            # A dwell whose timer has already started is served by the dwell
            # step to completion, not broken-skipped, even if its target later
            # leaves the network (design_new.md "Unreachable and broken task
            # handling", dwell walk-positions: the guard both `c481` and `592a`
            # test is the dwell row's own `started_at`).
            wp = projection.walk_position
            if (
                0 <= wp < len(exec_state.task_runs)
                and exec_state.task_runs[wp].started_at is not None
            ):
                break
            self._skip_broken_task(exec_state, route, wp)
            projection = self._find_next_task_target(train_id)
        if projection is None:
            self._finish_or_defer_terminal_walk(train_id, controller)
            return

        # If next task is a dwell, skip directly to the dwell step.
        if projection.task_kind != "dwell":
            # ---------- a5c6: covered-task index advance ---------------------
            self._covered_task_index_advance(
                train, route, exec_state, controller, now
            )
            # Re-resolve after a possible cursor change.
            projection = self._find_next_task_target(train_id)
            if projection is None:
                self._finish_or_defer_terminal_walk(train_id, controller)
                return

        # ---------- dwell: withhold onward authority until departure time ----
        # A dwell is not a controller state (design_new.md "ato.engaged"): the
        # train sits in READY while Plan holds onward authority, until the dwell
        # timer elapses. When it does, the walk advances past the dwell and the
        # pass continues so onward authority is acquired this same tick.
        if projection.task_kind == "dwell":
            if not self._dwell_step(train, route, exec_state, controller, now):
                # Still dwelling: no path creation while the timer runs.
                return
            projection = self._find_next_task_target(train_id)
            if projection is None:
                self._finish_or_defer_terminal_walk(train_id, controller)
                return
            if projection.task_kind == "dwell":
                # A back-to-back dwell holds this tick; the next pass handles it.
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

        # Step 592a commits the resolved direction from this projection
        # (design_new.md "Route-run state", direction row: "Its one writer is
        # Plan, at steps 592a / 66aa"). Step 66aa writes it again from the
        # accepted projection on a successful create/extend; this 592a write is
        # what keeps `direction` current when acquisition does not run this pass.
        if projection.direction:
            exec_state.direction = projection.direction

        # Capture the broken-vs-unreachable inputs from the freshly resolved
        # projection BEFORE the acquisition gate, so the stopped-run lock shrink
        # and the broken-skip / no-reachable-task stall handling below run even
        # on a pass where acquisition is gated off — a paused ATO train (not
        # engaged) or an EB'd train. design_new.md: the step-`592a`
        # no-valid-task branch and broken-skip run "ungated by
        # is_clean_for_create", and the "Stopped-run lock shrink" is "Not gated
        # by acquisition eligibility".
        failed_walk_position: Optional[int] = projection.walk_position
        # A BROKEN task has its target ID absent from the network (resolver
        # failure_kind == MISSING_TARGET), captured from the initial projection
        # before the retry loop inside the acquisition helper reassigns it.
        initial_broken = (
            projection.resolved_target is None
            and projection.failure_kind == TargetResolutionFailureKind.MISSING_TARGET
        )

        # ---------- 66aa: authority acquisition (create_path / extend_path) -
        # Gated internally by `_eligible_for_authority_acquisition`; returns
        # whether a create/extend succeeded this pass. When the gate is off the
        # train acquires nothing, but the stopped-run lock shrink and the
        # broken/stall handling below still run.
        any_success = self._acquire_authority_66aa(
            train, route, exec_state, controller, projection
        )

        # Stopped-run lock shrink (design_new.md "Stopped-run lock shrink", step
        # 66aa): a run that reached a stand holding more locked track than its
        # braking-distance floor releases the surplus lock flags, keeping the
        # reservation. The freed blocks stay reserved-but-not-locked, so a
        # strictly higher-priority run may preempt them while this train keeps its
        # claim. It releases lock only while stopped, so it never unlocks track
        # ahead of a rolling train. Not a cancel: the path stays ACTIVE and
        # `control_mode` is untouched.
        #
        # It runs here AFTER the acquisition helper and UNGATED by eligibility
        # ("Stopped-run lock shrink", Not gated by acquisition eligibility): on
        # an eligible pass the order is acquire -> lock -> shrink, and on a pass
        # where acquisition was skipped (a paused ATO train that keeps its
        # reservation, or an EB'd train) the shrink still frees the
        # reserved-but-unlocked tail for a higher-priority preemptor. Its own
        # is_stopped() / live-path / teardown-latch guards decide whether it does
        # anything.
        self._run_stopped_lock_shrink(train_id, train, controller)

        if any_success:
            # A valid task was served this tick → the walk is not all-broken.
            self._clear_no_reachable_task(exec_state)
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

        if not initial_broken:
            # Unreachable task (target present but disabled / manual / blocked):
            # WAIT — leave the task armed. Never consulted while live authority
            # exists, and with no path it still waits because the target may
            # become usable. The current task is itself valid (present), so the
            # walk is not all-broken. Diagnostic already emitted.
            self._clear_no_reachable_task(exec_state)
            return

        # Broken task (target missing from the network). Per design_new.md
        # "Unreachable and broken task handling", the broken-skip applies to
        # every task_kind and runs inline whether or not live authority exists —
        # a waypoint the head already consumed leaves its authority live, so a
        # later broken task must still be skipped or the spent authority never
        # comes down.
        if self._walk_has_valid_task(exec_state, route, failed_walk_position):
            # A valid task remains ahead → skip the broken one toward it. The
            # skip only advances the route cursor, so it is safe under a live
            # path; the next Plan extends toward the valid task.
            self._clear_no_reachable_task(exec_state)
            self._skip_broken_task(exec_state, route, failed_walk_position)
            return

        # No valid task anywhere on the walk. A live path (typically to a
        # head-consumed waypoint whose authority was never released) must come
        # down first: latch the ordinary cancel and defer the terminal/stall
        # write to the later pass that finds no live path (design_new.md
        # "Unreachable and broken task handling", no-valid-task rule). A cancel
        # already latched is left as-is.
        if has_active_path:
            if controller.route_hooks.phase_latches.path_cancel_request is None:
                controller.route_hooks.phase_latches.set_path_cancel_request(
                    ErrorRecord(
                        "route run has no reachable task (every task target is "
                        "missing from the network); cancelling stale authority",
                        "revoked",
                    ),
                    Kind.INFO,
                )
            return

        if route is not None and route.mode == RouteMode.ONE_OFF:
            # No valid task left and no live path: skip the broken task; the
            # next Plan sees the terminal walk and finishes the run (having
            # served nothing).
            self._clear_no_reachable_task(exec_state)
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
                condition_key=self._run_condition_key(
                    exec_state.run_id, "run_stalled"
                ),
            )
            exec_state.no_reachable_task = True
            # One event per stall (design_new.md "Unreachable and broken task
            # handling": "one error event ... origin run_stalled"): the
            # condition-keyed `_emit_run_error` above is that event, naming the
            # train and the run; no separate operator warning is emitted.

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
        failed_row.finished_at_operating = self._now_operating()
        try:
            exec_state._append_next_walk_task(route, walk_position)
        except Exception:
            pass
        # One event per skip (design_new.md "Unreachable and broken task
        # handling": "one diagnostic event ... origin task_skipped"): the
        # `_emit_run_error` above is that event, naming the train, run, and
        # walk-position; no separate operator warning is emitted.

    @staticmethod
    def _run_condition_key(run_id: str, origin: str) -> str:
        """The stable `condition_key` naming one live run-level error condition
        (design_new.md "Error events", Suppression mechanism).

        Scoped by the run and the error origin, so while a run's current task
        stays blocked for the same reason the repeat is dropped, and a changed
        reason (a different task or payload under the same origin) is appended
        as the new current reason. The owning Plan step clears it through
        `_resolve_run_condition(...)` the first tick the condition goes false.
        """
        return f"route_run:{run_id}:{origin}"

    def _resolve_run_condition(self, exec_state, origin: str) -> None:
        """Clear a run-level error condition the first tick its predicate goes
        false (design_new.md "Error events", Resolution and recurrence).
        Idempotent, so it is safe to call on every clean tick."""
        sc = getattr(self, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is None:
            return
        try:
            nc.resolve_condition(self._run_condition_key(exec_state.run_id, origin))
        except Exception:
            pass

    def _clear_no_reachable_task(self, exec_state) -> None:
        """The run has a reachable task again: drop the stall flag and resolve
        the stalled-run condition on this first transition to false, so the run
        panel stops showing "stalled" and a later stall emits a fresh event
        (design_new.md "Error events", Resolution and recurrence)."""
        exec_state.no_reachable_task = False
        self._resolve_run_condition(exec_state, "run_stalled")

    def _emit_run_error(
        self, exec_state, walk_position, origin: str, text: str,
        operation: str = "tick_train_plan",
        condition_key: Optional[str] = None,
        kind=None,
        latch_event: bool = False,
    ) -> None:
        """Emit a run/task error as an event (design_new.md "Error events").

        No entity stores an error: the record rides in the event's `error`
        field, and the run panel reads it back through
        `NotificationCenter.latest_error_for(...)`. The event names both a
        `"route_run"` ref and a `"walk_position"` ref, so a run-level filter
        and a task-level filter both find it.

        `condition_key`, when the caller is a live per-tick condition (blocked
        planning, a stalled run), makes `NotificationCenter` drop the repeat
        while the key stays active, so Plan may call this on every eligible
        tick without filling the stream; the caller resolves that key when its
        predicate goes false. A discrete one-shot diagnostic (a cancellation
        reason, a broken-task skip) passes no key and is always appended.
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
                kind=kind if kind is not None else _Kind.WARNING,
                message=text,
                entities=entities,
                error=_ErrRec(text, origin),
                event_time=self._now(),
                condition_key=condition_key,
                latch_event=latch_event,
            )
        except Exception:
            pass

    def _emit_route_warning(
        self, train_id: str, origin: str, reason: str,
        operation: str = "tick_train_plan",
    ) -> None:
        """Emit an operator-facing warning via NotificationCenter, falling back
        to a logger warning when the center is unavailable. Used by the
        broken-task skip and the no-reachable-task stall (design_new.md
        "Unreachable and broken task handling").

        `origin` is the `ErrorRecord.origin` label naming why the train is
        blocked — one of the v1 error origins (`"planning_failed"`,
        `"task_skipped"`, `"run_stalled"`, `"wedge"`, `"meet_hold"`). Repeats
        with the same entity, operation, origin and payload are suppressed by
        `NotificationCenter`, so Plan may call this on every eligible tick.
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

    # ----- Plan sub-steps ---------------------------------------------------
    def is_clean_for_create(self, train_id: str) -> bool:
        """The one Plan create-branch gate (design_new.md "is_clean_for_create").

        True when the driving source is ready to be granted authority: the
        controller is not OFF, EB is not active, no power-off is pending, no
        path cancel is pending, and the selected driving source is ready — ATO
        engaged, or a manual train (with no further condition; design_new.md, "Planning Kernel"
        "In `manual` mode there is no fifth condition ... There is no cab
        condition — the head end is derived from the authority"). It does NOT
        require the train to be stopped: there is no recovery to reach, so a
        train that just lost its authority can be granted a fresh path while
        still rolling. (Pending route abort / deactivate are the teardown rule's
        concern, blocked upstream in ``_eligible_for_authority_acquisition``.)
        """
        controller = self._controller_by_id(train_id)
        if controller is None:
            return False
        if not controller.powered:
            return False
        if controller.eb.active:
            return False
        if controller.route_hooks.phase_latches.power_off:
            return False
        if controller.route_hooks.phase_latches.path_cancel_request is not None:
            return False
        # The selected driving source must be ready to run. In ATO that is the
        # engage gate: a not-engaged ATO train neither creates nor extends
        # reservation (design_new.md "ATO status", ATO engage gate). A manual
        # train has no further gate.
        if controller.commands.control_mode == "ato":
            return controller.commands.engaged
        return True

    def _eligible_for_authority_acquisition(
        self, controller: TrainController, exec_state: RouteExecutionState
    ) -> bool:
        """Plan eligibility precondition for create_path / extend_path.

        Per design_new.md (Planning Kernel section): the dispatch is
        two-branch. Extend is allowed when an active path exists AND none of
        the three pending admin/system cancellation latches are set (so
        Plan does not extend a path Action is about to cancel) AND the ATO
        engage gate holds — a not-engaged ATO train extends no reservation
        (design_new.md ~1981). Create delegates to ``is_clean_for_create``.
        """
        if controller.active_conditions & _PENDING_CANCEL_CONDITIONS:
            return False
        try:
            has_active_path = controller.path_projection.received_path_plan is not None
        except Exception:
            has_active_path = False
        if has_active_path:
            # Extend branch — a not-engaged ATO train keeps its reservation but
            # extends nothing (ATO engage gate); manual has no such condition.
            if (
                controller.commands.control_mode == "ato"
                and not controller.commands.engaged
            ):
                return False
            return True
        # Create branch — full clean-for-create predicate.
        train_id = getattr(exec_state, "train_id", None)
        if train_id is None:
            return False
        return self.is_clean_for_create(train_id)

    def _dwell_step(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        now: float,
    ) -> bool:
        """Plan dwell step: hold onward authority while the train dwells, until
        the dwell's departure time. Returns True when the dwell is satisfied and
        the walk has advanced past it (the pass should acquire onward authority
        now); False while the train is still dwelling (Plan returns for this
        tick).

        A dwell is not a controller state (design_new.md "ato.engaged"): the
        train sits in READY, stopped at its arrival authority end, and Plan
        simply does not acquire onward authority until the dwell timer elapses.

        Samples the dwell row's paired `started_at` / `started_at_operating` on
        the first pass so the hold is measured from when the train reached the
        dwell, not from its predecessor's `finished_at`. EB does not stop the
        dwell clock and does not suppress departure: `started_at` is untouched
        by EB and the walk advances when the timer expires. What waits under EB
        is the later reservation grant, gated by `is_clean_for_create` (which
        returns False under EB). A paused (not-engaged) ATO train behaves the
        same way — its grant waits on the same engage gate — so EB and paused
        ATO are not special-cased here (design_new.md "Planning Kernel", step
        70c3).
        """
        # `current_waypoint_index` is the largest finished walk-position; the
        # dwell task currently in flight is the next planning index.
        task_index = exec_state.next_planning_index
        task = route.get_task_at(task_index) if hasattr(route, "get_task_at") else None
        if task is None or task.task_kind != "dwell":
            return False

        # Lazy-allocate task_runs row. Locate the in-flight row for this
        # `task_index`: after CIRCULAR / ROUND_TRIP wraparound multiple list
        # positions can share the same task_index — the in-flight one is the
        # latest entry whose finished_at is still None.
        self._sync_task_runs(route, exec_state)
        row_pos = self._in_flight_row_pos_for_task(exec_state, task_index)
        if row_pos < 0:
            return False
        row = exec_state.task_runs[row_pos]

        # Sample the dwell row's start on the first pass, so the hold is measured
        # from the moment the train reached the dwell.
        if row.started_at is None:
            row.started_at = self._now()
            row.started_at_operating = self._now_operating()

        # EB does not stop the dwell clock and does not suppress departure: the
        # walk advances when the timer expires, and the later reservation grant
        # is gated by is_clean_for_create (which returns False under EB). A
        # paused (not-engaged) ATO train behaves the same way — its grant waits
        # on the same engage gate — so EB and paused ATO are not special-cased
        # here (design_new.md "Planning Kernel", step 70c3, EB during a dwell).

        # Read the row's start on the route's own basis so the duration never
        # subtracts a wall instant from an operating one.
        realtime = getattr(route, "timing_basis", "operating") == "realtime"
        dwell_started_at_basis = (
            row.started_at if realtime else row.started_at_operating
        )
        if dwell_started_at_basis is None:
            return False

        if not task.should_depart(
            now, self._now_basis(route), dwell_started_at_basis
        ):
            return False

        # Dwell satisfied: mark the dwell row finished and advance through the
        # canonical finished_at + delay_class + walk-append writer (delay_class
        # no-ops for dwell rows per `_record_delay_class`). No controller state
        # transition — the train was READY throughout and stays READY.
        self.advance_walk_position(exec_state, route, row_pos)
        return True

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
        """Plan step `6a0d`: accrue a `run` task's timer, then turn it into a
        stop at the run's current authority end.

        A `run` keeps the train moving for `task_time` seconds of simulated
        time. Once the timer elapses the run becomes a stop at the run's current
        authority end (design_new.md "run task"): this step freezes the run
        row's `resolved_target` on the live authority-end block and stops
        acquiring onward authority. With no fresh authority granted, the train
        brakes to that authority end under ordinary ATC, and the ordinary
        stop-arrival check completes the run there — no service brake and no
        cancel. This step is `run_elapsed_seconds`'s only writer.

        The timer is measured in accumulated simulated `delta_seconds` (not
        wall-clock), so "run 20 s" means 20 s of simulation regardless of tick
        cadence.

        Returns:
            True  — the run window has elapsed; the caller must not acquire
                    fresh authority (the authority end is frozen).
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

        # Accrue whenever the run task is not held: EB is not latched and the
        # train is not a paused ATO train (design_new.md step `6a0d`). An active
        # EB or a not-engaged ATO train (`train_status == ATO and not
        # commands.engaged`) each pauses accrual for that pass — EB and a paused
        # ATO train pause the run timer the same way. Accrual does not depend on
        # the direction-seed target resolving this pass: a run is a stop timed by
        # the clock, so the timer keeps counting even on a pass where the seed
        # target is momentarily unavailable.
        from enums import TrainStatus

        paused_ato = (
            controller.ops.train_status == TrainStatus.ATO
            and not controller.commands.engaged
        )
        if not controller.eb.active and not paused_ato:
            run_row.run_elapsed_seconds = min(
                run_row.run_elapsed_seconds + max(0.0, delta_seconds), run_time
            )

        # Still inside the run window (or accrual paused) → keep rolling toward
        # the direction seed / defer to broken-unreachable.
        if run_row.run_elapsed_seconds < run_time:
            return False

        # Window elapsed: the run is now a stop at the current authority end.
        # Freeze the run row's target on that block so the stop-arrival check
        # matches the live authority end there (per **CompletionTaskProjection
        # schema**), and stop acquiring onward authority by returning True.
        active_path = (
            self.path_dispatcher.get_active_path(train_id)
            if self.path_dispatcher is not None
            else None
        )
        if active_path is not None:
            end_block = active_path.get_reservation_end_block_id()
            if end_block is not None:
                direction = (
                    projection.resolved_target.requested_destination_direction
                    if projection.resolved_target is not None
                    else exec_state.direction
                )
                run_row.resolved_target = ResolvedRouteTarget(
                    requested_destination_block_id=end_block,
                    requested_destination_direction=direction or "forward",
                )
        else:
            # No live reservation to arrive at. A `run` stop is position-
            # agnostic (the train coasts to a stand wherever it is), and with
            # no path the Stop-arrival check will never fire to finish this
            # row — so finish it here, or the walk would wedge with the run row
            # forever unfinished (design_new.md, "Run task resolution").
            self.advance_walk_position(exec_state, route, walk_pos)
        return True

    def _covered_task_index_advance(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        now: float,
    ) -> None:
        """Plan step 4: skip already-covered `waypoint` and same-block `stop` tasks.

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
        if task_count == 0:
            return
        # Per design, the loop iterates the next un-finished planning index;
        # `current_waypoint_index` is the largest finished walk-position
        # (or None) and is not the right cursor here.
        #
        # `next_planning_index` is a `Route.tasks` index (always < task_count),
        # so on a fully-covered CIRCULAR / ROUND_TRIP walk `_append_next_walk_
        # task` keeps supplying an unfinished row and the condition never falls
        # false on its own. The scan budget bounds the advance to one full
        # `len(Route.tasks)` scan per Plan pass (design_new.md "Covered-task index advance").
        scanned = 0
        while exec_state.next_planning_index < task_count:
            if scanned >= task_count:
                break
            scanned += 1
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

            # The trailing unfinished `task_runs` row for this `task_index` —
            # its walk-position. Resolved before the coverage decision so the
            # post-wrap head condition below can read it. For CIRCULAR /
            # ROUND_TRIP routes several rows can share one `task_index` once
            # `_append_next_walk_task` has wrapped; walk from the trailing entry
            # back and take the first unfinished one.
            row = None
            pos = None
            for p in range(len(exec_state.task_runs) - 1, -1, -1):
                r = exec_state.task_runs[p]
                if r.task_index == idx and r.finished_at is None:
                    row = r
                    pos = p
                    break
            if row is None:
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
                # design_new.md "Covered-task index advance", describes the train already parked AT
                # the destination, where head and span agree; head-anchoring is
                # the safe reading of that case and preserves turnaround safety.)
                if not controller.is_stopped():
                    break
                if self._get_active_path_id(train.train_id) is not None:
                    break
                if not self._is_train_at_block(train, target_block):
                    break
            else:
                # waypoint coverage (design_new.md "Covered-task index advance"): covered when the
                # resolved target block is anywhere in the train's current span
                # (a waypoint the train physically passed mid-span is covered
                # even after its head has moved beyond it), or — for a
                # platform-anchored task — the head sits at the platform tile.
                if not self._target_block_in_current_span(controller, target_block):
                    if not self._is_train_at_platform_for_task(train, task):
                        break
                # A post-wrap row (walk_position >= len(Route.tasks)) needs more
                # than span membership: the head must have moved on past the
                # block (design_new.md "Covered-task index advance"). Otherwise a cyclic all-in-span
                # waypoint walk would advance every row forever without the train
                # ever leaving the covering position.
                if pos >= task_count and self._is_train_at_block(
                    train, target_block
                ):
                    break

            # Write finished_at on the matching task_runs row (its walk-position
            # `pos`, resolved above). The derived `current_waypoint_index` then
            # advances on the next loop iteration, and `_append_next_walk_task`
            # appends the next walk-position entry for CIRCULAR / ROUND_TRIP
            # routes.
            self.advance_walk_position(exec_state, route, pos)

    @staticmethod
    def _target_block_in_current_span(
        controller: TrainController, block_id: str
    ) -> bool:
        """True when ``block_id`` is anywhere in the train's current span.

        Covered-task / covered-stop coverage reads the train's full physical
        span, not just the head-tile block (design_new.md "Covered-task index advance"): the
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

    def _apply_waypoint_lookahead(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        projection: ResolvedNextTaskProjection,
    ) -> ResolvedNextTaskProjection:
        """Arm the task after the served waypoint when the train is running out
        of cleared track ahead of it.

        design_new.md Planning Kernel step `66aa`, **Waypoint lookahead**. A
        waypoint is track the train passes through, not a place it stops, so
        re-arming one the authority already reaches would leave the train
        braking toward a terminal it is meant to run straight past. This step
        arms the *next* task's target instead, so authority can grow forward
        past the reached waypoint; the **Braking-distance lock** then bounds how
        far it actually grows (there is no fixed block-count lookahead).

        It applies when the armed task is a `waypoint`, or a `run` whose timer
        is still running — a waypoint for reservation growth per the
        **Reservation termination rule** — and the live authority already
        reaches it (nothing is owed on the current target).

        Returns the projection to arm this tick: the lookahead one when it
        applies and a next task exists, otherwise the one passed in. Everything
        downstream — the `resolved_target` write, the `direction` write, and the
        extend retries — then uses the returned projection's `walk_position`.

        WHY EAGER: v1 arms the next target as soon as live authority reaches the
        current one — it does not wait for the braking-distance lock end to catch
        up to the reservation end (design_new.md "Waypoint lookahead"). Eager
        arming keeps the reservation one waypoint ahead of the braking curve on a
        running lap: a train slowing toward its reservation end never lets the
        lock reach that end, so waiting for lock-end == reservation-end would
        stall the reservation and brake the train short of the waypoint (and
        deadlock a lapping train). The only cost is a slightly larger reserved
        footprint, which the dispatcher's holding-point chunking still bounds.
        """
        # A waypoint, or a run whose timer is still running (a waypoint for
        # reservation growth), may reserve onward past its target.
        armable = projection.task_kind == "waypoint"
        if projection.task_kind == "run" and 0 <= projection.walk_position < len(
            exec_state.task_runs
        ):
            run_row = exec_state.task_runs[projection.walk_position]
            task = route.tasks[run_row.task_index]
            run_time = (
                task.task_time if (task.task_time and task.task_time > 0.0) else 0.0
            )
            armable = run_row.run_elapsed_seconds < run_time
        if not armable:
            return projection
        path = (
            self.path_dispatcher.get_active_path(train.train_id)
            if self.path_dispatcher is not None
            else None
        )
        if path is None:
            return projection
        target_block = (
            projection.resolved_target.requested_destination_block_id
            if projection.resolved_target
            else None
        )
        if not target_block or not path.reaches_target(target_block):
            return projection
        ahead = self._find_next_task_target(
            train.train_id, after_walk_position=projection.walk_position
        )
        if ahead is None or ahead.resolved_target is None:
            # Terminal walk, a dwell, or a target that will not resolve — keep
            # the served waypoint armed and let the ordinary flow handle it.
            return projection
        return ahead

    def _braking_lock_block_ids(
        self,
        train: "Train",
        controller: TrainController,
        target_block: str,
        target_dir: str,
        manual_override: bool,
    ) -> List[str]:
        """The forward blocks within the train's braking distance — the
        braking-distance lock subset (design_new.md "Braking-distance lock",
        Computing the locked blocks).

        Walks the forward route from the head toward `target_block`, summing each
        block's `Block.length_cm` from the head's current position, and returns
        every block up to and including the first whose far seam is at or beyond
        the braking distance. The scan starts at the next block ahead (index 1),
        so a stopped train (braking distance rounding under one block) still
        locks one whole block — the floor.
        """
        if self.path_dispatcher is None:
            return []
        head_block = controller._get_current_block_id()
        if not head_block or head_block == target_block:
            return []
        candidates = self.path_dispatcher.find_walk_candidates(
            head_block,
            target_block,
            train.train_id,
            direction_constraint=target_dir,
            manual_override=manual_override,
        )
        ordered = candidates[0].ordered_blocks if candidates else None
        if not ordered or len(ordered) < 2:
            return []
        braking_cm = controller.braking_distance_cm(
            getattr(self, "_current_delta_seconds", 0.0)
        )
        far_seam_cm = float(controller._get_remaining_distance_in_current_block())
        lock_ids: List[str] = []
        for i in range(1, len(ordered)):
            bid = ordered[i]
            block = self.network.blocks.get(bid) if self.network else None
            far_seam_cm += float(getattr(block, "length_cm", 0.0) or 0.0)
            lock_ids.append(bid)  # floor: always at least ordered[1]
            if far_seam_cm >= braking_cm:
                break
        return lock_ids

    def _write_braking_distance_lock(
        self,
        train: "Train",
        controller: TrainController,
        target_block: str,
        target_dir: str,
        manual_override: bool,
    ) -> None:
        """Write the braking-distance lock onto the reserved blocks within the
        train's stopping distance (design_new.md "Braking-distance lock", step
        66aa).

        Called after a successful create/extend. The lock is the braking-distance
        subset of the reservation, not the whole reserved prefix: reserved blocks
        beyond the stopping distance stay reserved-but-not-locked (preemptible).
        The lock only grows forward while the train rolls; a stopped run's
        surplus lock is released by `_run_stopped_lock_shrink`. Re-locking an
        already-locked own block is idempotent.
        """
        if self.path_dispatcher is None:
            return
        path = self.path_dispatcher.get_active_path(train.train_id)
        if path is None:
            return
        block_manager = getattr(self.network, "block_manager", None)
        if block_manager is None:
            return
        blocks = getattr(self.network, "blocks", {})
        lock_ids = [
            bid
            for bid in self._braking_lock_block_ids(
                train, controller, target_block, target_dir, manual_override
            )
            if getattr(blocks.get(bid), "reserved_by_path", None) == path.path_id
        ]
        if lock_ids:
            block_manager.lock_blocks_for_path(lock_ids, path.path_id)

    def _run_stopped_lock_shrink(
        self, train_id: str, train: "Train", controller: TrainController
    ) -> None:
        """Release a stopped run's surplus lock back to the floor, keeping the
        reservation (design_new.md "Stopped-run lock shrink").

        When the run is stopped holding more locked track than the floor (the
        next block ahead of the head), release the lock flag on every locked
        block beyond that first one through
        `BlockManager.unlock_blocks_for_path(...)`. The reservation, the head
        resources, and `control_mode` are untouched, so the freed blocks stay
        reserved-but-not-locked — preemptible by a strictly higher-priority run.
        Only while stopped, so it never unlocks track ahead of a rolling train.
        """
        if self.path_dispatcher is None or not controller.is_stopped():
            return
        path = self.path_dispatcher.get_active_path(train_id)
        if path is None:
            return
        block_manager = getattr(self.network, "block_manager", None)
        if block_manager is None:
            return
        blocks = getattr(self.network, "blocks", {})
        locked_forward = [
            entry.resource_id
            for entry in path.resource_sequence
            if entry.resource_type == "block"
            and entry.sequence_index > path.current_resource_index
            and getattr(blocks.get(entry.resource_id), "locked_by_path", None)
            == path.path_id
        ]
        # Keep the floor block (the first locked block ahead of the head); release
        # the surplus lock beyond it. The lock is a forward-contiguous prefix, so
        # locked_forward[0] is the floor.
        if len(locked_forward) > 1:
            block_manager.unlock_blocks_for_path(locked_forward[1:], path.path_id)

    def _acquire_authority_66aa(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        projection: "ResolvedNextTaskProjection",
    ) -> bool:
        """Planning Kernel step `66aa` authority acquisition (create/extend).

        Gated by ``_eligible_for_authority_acquisition`` — a not-eligible train
        (OFF, EB, pending teardown, or a not-engaged ATO train) acquires nothing
        and this returns ``False`` at once, so the caller still runs the ungated
        stopped-run lock shrink and the broken/stall handling. Returns whether a
        ``create_path`` / ``extend_path`` succeeded this pass.
        """
        if not self._eligible_for_authority_acquisition(controller, exec_state):
            return False
        train_id = train.train_id

        # Live-path target re-resolution invalidation (design_new.md, "route
        # path-event callback schema", case `162a`): when a live path exists and a
        # fresh resolve of the CURRENT
        # walk-position now yields a different target BLOCK than the path was
        # built toward — e.g. a StationTarget re-resolving to a different
        # platform — the already-acquired authority toward the old block is
        # potentially stale. Latch a `revoked` cancel and wait for the ordinary
        # Action cancel / replan flow instead of extending toward a different
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
        # (`failure_kind = no_viable_path`, design_new.md "Path extension"),
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
                    ErrorRecord(
                        "live-path target re-resolved for walk-position "
                        f"{projection.walk_position} "
                        f"({stored_target.requested_destination_block_id} -> "
                        f"{new_target.requested_destination_block_id}); "
                        "cancelling stale authority so the next pass replans "
                        "toward the new target",
                        "revoked",
                    ),
                    Kind.INFO,
                )
                return False

        # Per design_new.md (fix #14, part a): wrap authority acquisition in a
        # retry loop that accumulates previously-failed resolved_target values
        # into an `exclude` set and re-calls `_find_next_task_target(train_id,
        # exclude=exclude)` so StationTarget tasks can try alternate platforms
        # within the same tick. Cap iterations at the number of platforms in
        # the resolved station group (or a conservative maximum of 8) to
        # prevent an infinite loop when every candidate is blocked.
        exclude: List[ResolvedRouteTarget] = []
        _max_iterations = 8
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
                # Emit the unreachable diagnostic only for a target that is
                # present but unusable this tick (target_unavailable). A BROKEN
                # task (missing_target) gets its own `task_skipped` event from
                # the broken-skip, so emitting `planning_failed` for it here too
                # would double-report it (design_new.md "Unreachable and broken
                # task handling": broken -> task_skipped, unreachable ->
                # planning_failed).
                if (
                    projection.failure_kind
                    != TargetResolutionFailureKind.MISSING_TARGET
                    and projection.failure_reason
                    and 0 <= projection.walk_position < len(exec_state.task_runs)
                ):
                    self._emit_run_error(
                        exec_state,
                        projection.walk_position,
                        "planning_failed",
                        projection.failure_reason,
                        condition_key=self._run_condition_key(
                            exec_state.run_id, "planning_failed"
                        ),
                    )
                break  # sentinel projection — no more candidates this tick
            # Plan step `66aa`: dispatch via PathDispatcher.create_path or
            # extend_path (v1 signature). Both methods take train_id +
            # requested_destination_block_id + requested_destination_direction +
            # reservation_cap_block_id and return a PathAcquisitionResult.
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
        return any_success

    def _authority_acquisition_step(
        self,
        train: "Train",
        route: "Route",
        exec_state: RouteExecutionState,
        controller: TrainController,
        projection: ResolvedNextTaskProjection,
    ) -> bool:
        """Plan step 7: create or extend authority for the resolved target.

        Returns True when authority was successfully acquired (create/extend
        succeeded), False otherwise. The return value is used by the fix #14
        retry loop in tick_train_plan.

        On a successful create/extend, write
        ``task_runs[projection.walk_position].resolved_target`` (Plan-only
        writer). There is no departure handshake (design_new.md "ato.engaged"):
        an engaged ATO train, or a manual train with a command, drives on its
        own once authority exists, and the reactive rule moves it
        READY -> RUNNING_MOVING from the observed motion.
        """
        if self.path_dispatcher is None:
            return False
        if projection.resolved_target is None:
            return False

        projection = self._apply_waypoint_lookahead(
            train, route, exec_state, projection
        )
        if projection.resolved_target is None:
            return False

        target_block = projection.resolved_target.requested_destination_block_id
        target_dir = projection.resolved_target.requested_destination_direction

        # Decide create vs extend by active authority presence.
        active_path_id = self._get_active_path_id(train.train_id)

        # An extend approaches the target from the live authority end, not the
        # train's current block. When the armed task authored
        # `task_direction == "either"`, `RouteTargetResolver.resolve_task_direction`
        # resolved it from the current block by shortest walk, which on a
        # CIRCULAR lap can name the block frame whose walk reverses at the
        # authority end — candidate search then rejects it and the lap is
        # cancelled and re-created at every seam instead of extending across it.
        # Re-resolve to the frame that continues forward from the authority end
        # (design_new.md "`create_path()` flow", direction-aware candidate
        # search). An explicitly authored `"forward"` / `"reverse"` is never
        # flipped: if it is incompatible with forward continuation the extend
        # fails and replans, as before.
        if active_path_id is not None:
            armed_task = None
            wp = projection.walk_position
            if route is not None and 0 <= wp < len(exec_state.task_runs):
                armed_task = route.get_task_at(
                    exec_state.task_runs[wp].task_index
                )
            if (
                armed_task is not None
                and getattr(armed_task, "task_direction", None) == "either"
            ):
                forward_dir = self.path_dispatcher.forward_continuing_arrival_direction(
                    active_path_id, target_block
                )
                if forward_dir is not None:
                    target_dir = forward_dir

        # Meet-coordination ceiling + candidate pin (design_new.md "Meet
        # coordination" + "Reservation-cap rule"):
        # when this train carries a meet entry, pass its `candidate_resource_keys`
        # as `required_candidate_resource_keys` so the dispatcher acquires the
        # exact route meet coordination evaluated, and — when the entry also caps
        # short of the real route target — keep the request at that target and
        # pass the ceiling as the reservation cap. The dispatcher acquires toward
        # the real target but stores no authority past the cap, so
        # `reservation_end_block_id` lands on the ceiling while
        # `requested_destination_block_id` (and the `task_runs[...].resolved_target`
        # written on success below) stay the farther route target. The capped
        # authority never completes, so the train holds at the ceiling. A train
        # outside a meet passes neither constraint and requests its route target
        # normally.
        required_candidate_resource_keys: Optional[List[Tuple[str, str, str, str]]] = None
        meet_ceiling_block: Optional[str] = None
        ceiling = self.meet_assignment.get(train.train_id)
        if ceiling is not None:
            if ceiling.candidate_resource_keys:
                required_candidate_resource_keys = list(ceiling.candidate_resource_keys)
            if ceiling.block_id and ceiling.block_id != target_block:
                meet_ceiling_block = ceiling.block_id

        # design_new.md "`disabled` and `manual` availability flags": forward the assigned route's
        # operator override into the acquisition chain so an operator-initiated
        # route can serve `manual` resources. Route-level only (never a RouteRun
        # override); an automatic route leaves this False.
        manual_override = bool(getattr(route, "manual_override", False))

        # Reservation cap (design_new.md "Reservation-cap rule"): MEET-ONLY. The
        # braking distance sizes the lock (per "Braking-distance lock"), not this
        # cap — the lock is a tier inside the reservation, written block-by-block
        # after acquisition, never a bound on how far reservation is acquired. So
        # the only cap source is a meet ceiling; `None` acquires to the target.
        reservation_cap_block_id = (
            meet_ceiling_block
            if meet_ceiling_block and meet_ceiling_block != target_block
            else None
        )

        success = False
        result = None
        try:
            if active_path_id is not None:
                result = self.path_dispatcher.extend_path(
                    path_id=active_path_id,
                    requested_destination_block_id=target_block,
                    requested_destination_direction=target_dir,
                    manual_override=manual_override,
                    reservation_cap_block_id=reservation_cap_block_id,
                    required_candidate_resource_keys=required_candidate_resource_keys,
                )
                success = bool(getattr(result, "success", False))
            else:
                result = self.path_dispatcher.create_path(
                    train_id=train.train_id,
                    requested_destination_block_id=target_block,
                    requested_destination_direction=target_dir,
                    manual_override=manual_override,
                    reservation_cap_block_id=reservation_cap_block_id,
                    required_candidate_resource_keys=required_candidate_resource_keys,
                )
                success = bool(getattr(result, "success", False))
        except Exception as exc:  # pragma: no cover — defensive
            success = False

        if not success:
            # Auto-recover from a structural extend_path failure: the live path
            # cannot be grown toward this target at all, whatever the live
            # traffic does. NO_VIABLE_PATH means no legal forward suffix exists
            # to it — including when the only route there runs back through a
            # block this path still holds, and including a target that would
            # need the train to turn around at the current authority end, since
            # extension is forward-only. That does not clear by waiting, so
            # retrying against the same path would spin until the train ran out
            # its authority and stopped. Latch a cancellation so Action tears
            # the stale path down once the train stops; the next eligible
            # Plan will see no active path and acquire via create_path from
            # the train's current span instead.
            # Per the **Planning Kernel** section this is the failure kind that
            # "latches cancellation before a fresh create attempt", and per the
            # latch-table rules this is the same pattern Plan uses for any
            # "live path must be replaced" condition: write the latch, stop
            # planning this tick, let Action commit. BLOCKED_BEFORE_TARGET is
            # deliberately not here — it is a temporary live conflict, and the
            # path stays valid while Plan retries.
            fkind = getattr(result, "failure_kind", None)
            if (
                active_path_id is not None
                and fkind is PathAcquisitionFailureKind.NO_VIABLE_PATH
            ):
                cancel_reason = (
                    getattr(result, "reason", None)
                    or "extend cannot grow this path toward the target"
                )
                controller.route_hooks.phase_latches.set_path_cancel_request(
                    ErrorRecord(
                        f"{cancel_reason}; auto-cancelling so the next "
                        "acquisition uses create_path from the train's "
                        "current span",
                        "revoked",
                    ),
                    Kind.INFO,
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
                condition_key=self._run_condition_key(
                    exec_state.run_id, "planning_failed"
                ),
            )
            return False

        # Authority acquisition succeeded for this task: the blocked-planning
        # predicate is now false, so resolve its condition (design_new.md
        # "Error events", Resolution and recurrence). This is the first
        # transition to false — the run panel's `latest_run_error` read stops
        # showing the prior blocked reason, and if planning blocks again later
        # the now-inactive key lets the fresh event through. The no-authority
        # banner and the Quick Route toast keep gating on live state (a
        # `received_path_plan` on the controller payload) as before.
        self._resolve_run_condition(exec_state, "planning_failed")

        # Braking-distance lock (design_new.md "Braking-distance lock", step
        # 66aa): after the reservation is stored, lock only the reserved blocks
        # within the train's stopping distance ahead of the head. Blocks reserved
        # beyond that stay reserved-but-not-locked (preemptible). The lock is a
        # tier inside the reservation and never bounds acquisition.
        self._write_braking_distance_lock(
            train, controller, target_block, target_dir, manual_override
        )

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
            # A successful fresh create gives the run a live path again, so the
            # read-model `display_state` stops projecting "CANCELLED" on its own
            # (design_new.md "Route-run state", `display_state`) — there is no
            # stored field to clear.
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

        # design_new.md "ato.engaged": there is no departure handshake. Once
        # authority is acquired, an engaged ATO train (or a manual train with a
        # command) drives on its own through the Action driving rule, and the
        # reactive rule moves READY -> RUNNING_MOVING from the observed motion.
        return True

    def _finish_or_defer_terminal_walk(self, train_id: str, controller) -> None:
        """Terminal-walk completion with the doc's live-path guard.

        ``_find_next_task_target(train_id)`` has returned ``None``, so the
        directed walk is terminal. Per design_new.md "Canonical Next-Target
        Selection" (Terminal-walk rule) and step ``592a``:

          * When a live dispatcher path still exists — reachable when the
            last-armed task was a ``waypoint`` that head entry consumed without
            releasing authority, or when every remaining task was broken-skipped
            — do NOT finish the run yet. Latch the ordinary ``revoked`` cancel
            (only when no cancel is already pending) so the Action cancel/replan
            flow releases that stale authority, then return.
            ``finish_route_run`` runs on the later pass this same check finds no
            live path.
          * Otherwise (no live path) call ``finish_route_run(train_id)``
            directly.

        Landing this guard is what releases a terminal ``waypoint``'s orphaned
        path now that ``PathDispatcher.try_complete_active_path(...)`` completes
        only ``stop`` tasks (design_new.md "Stop-arrival check"): the ungated
        completion that used to spend that authority is gone, so a terminal walk
        that still holds authority must hand the path to the cancel/replan flow
        instead of archiving the run while its path is still live.
        """
        if self._get_active_path_id(train_id) is not None:
            latches = controller.route_hooks.phase_latches
            if latches.path_cancel_request is None:
                latches.set_path_cancel_request(
                    ErrorRecord(
                        "terminal walk reached with authority still held for "
                        f"{train_id}; cancelling the spent authority so the run "
                        "finishes once no live path remains",
                        "revoked",
                    ),
                    Kind.INFO,
                )
            return
        self.finish_route_run(train_id)

    def finish_route_run(self, train_id: str) -> None:
        """Plan terminal-walk handler.

        Per design_new.md ``current_route_status`` write contract:
          * sets ``RouteRun.current_route_status = FINISHED``
          * calls ``route.unassign_train(train_id)``
          * archives the run as a historical FINISHED row (``execution_states``
            slot frees up so a later route assignment can populate a fresh run)
          * for ``RouteMode.ONE_OFF`` routes with no remaining assigned trains,
            also calls ``self.deactivate_route(route_id)`` (its latch walk is
            empty because no assigned trains remain)
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
            self.deactivate_route(route_id)

        # A natural finish may have removed this route's last assignment / live
        # run, satisfying a deferred deletion that was waiting on it
        # (design_new.md "Route Edit And Reconciliation").
        self.finalize_pending_route_deletions()

    def deactivate_route(self, route_id: str) -> None:
        """Canonical latch-writing route deactivation (design_new.md, "Arrival detection").

        Calls ``Route.deactivate()`` (which writes only ``Route.is_active =
        False``), then walks every assigned train and writes
        ``pending_route_deactivate`` through
        ``controller.route_hooks.phase_latches.set_route_deactivate(...)`` per the
        inclusive-latch rule. This is the one home every latch-writing
        deactivation reaches — admin/UI, shared-route structural edit, and the
        ``ONE_OFF`` finish cascade. Idempotent: an already-inactive route with no
        assigned trains is a no-op with an empty walk. Does not delete any
        ``RouteRun``; the per-train teardown (to ``DEACTIVATED``) happens later in
        each train's Action ``pending_route_deactivate`` consumer.
        """
        route = self.route_registry.get(route_id)
        if route is None:
            return
        route.deactivate()
        diagnostic = f"route_deactivated:{route_id}"
        for train_id in list(getattr(route, "assigned_train_ids", [])):
            controller = self._controller_by_id(train_id)
            if controller is None:
                continue
            controller.route_hooks.phase_latches.set_route_deactivate(
                reason=diagnostic,
                has_active_path=controller.path_projection.received_path_plan
                is not None,
            )

    def _archive_run(self, exec_state: RouteExecutionState) -> None:
        """Move a now-terminal `RouteRun` to `_historical_runs`.

        After this call, `execution_states.get(exec_state.train_id)` returns
        the next live run (or `None`) rather than this terminal one.
        """
        train_id = exec_state.train_id
        if self.execution_states.get(train_id) is exec_state:
            del self.execution_states[train_id]
        self._historical_runs.append(exec_state)

    # --------------------------------------------------- dispatcher callbacks
    def handle_train_advanced(
        self,
        train_id: str,
        path_id: str,
        resource_event: ResourceEvent,
    ) -> None:
        """Plan-settlement head-advance callback. Consumes at most one task per
        call. Observe records the head-entry (design_new.md step `640a`); this
        callback is replayed over that record in `_run_plan_settlement(...)`.

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
        # Match on block AND direction (design_new.md, "Head-mapping rule"). Both sides are
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
        self.advance_walk_position(exec_state, route, idx)

    def handle_path_cancellation(
        self,
        train_id: str,
        path_id: str,
        error: Optional["ErrorRecord"] = None,
    ) -> None:
        """Action cancellation callback.

        Per design_new.md "route path-event callback schema"
        the v1 callback signature is ``(train_id, path_id, error)``. Every
        cancellation kind shares the same body; the deviation classification is
        not passed here — it rides the ``cancel_path`` event payload (and
        ``error.origin == "out_of_sequence"`` for a movement deviation), not this
        callback. ``error`` is diagnostic context only and must not be used for
        branching. The callback is bookkeeping only: it must not choose a fresh
        target or create fresh authority.
        """
        # Read the RUNNING run via the canonical accessor so a stale
        # DEACTIVATED/FINISHED slot is never treated as live (design_new.md
        # "Route-run state"). A committed cancellation with no RUNNING run is an
        # invariant violation — "every live path is backed by a RouteRun"
        # (design_new.md, "route path-event callback schema") — so surface it on the event stream rather
        # than swallowing it. It is deliberately not raised: that invariant is
        # strained by calibration legs and broken-skip finishes, so a hard crash
        # could hit a legal edge; an ERROR event flags it without stopping the tick.
        exec_state = self.execution_state_for(train_id)
        if exec_state is None:
            from notification_center import Kind as _Kind

            self._emit_event(
                operation="handle_path_cancellation",
                message=(
                    f"cancellation callback for train {train_id} path {path_id} "
                    "found no RUNNING RouteRun (invariant violation)"
                ),
                train_id=train_id,
                path_id=path_id,
                kind=_Kind.ERROR,
            )
            return
        # Ordinary cancellation: this callback writes no `RouteRun` field and
        # emits no event route-side (design_new.md "Route-run state"). The
        # cancellation's `ErrorRecord` rides the `cancel_path` event alone;
        # `RouteRun.display_state` reads `"CANCELLED"` from that event and the
        # terminated `Path` record while the run has no live path (with the
        # reason on the event's `error.origin`), and returns to `"RUNNING"` once
        # a fresh create_path gives the run a live path again.
        return

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        walk_position: int,
    ) -> bool:
        """Observe completion callback — step `bc55` (Finished-stop route commit).

        Per design_new.md step `bc55`: this records the served stop
        as finished by finishing exactly `walk_position` through
        `RouteExecutor.advance_walk_position(...)`, which samples and writes the row's
        wall and operating finish fields locally; the derived
        `current_waypoint_index` then advances to `walk_position`.

        `PathDispatcher.try_complete_active_path(train_id)` has already resolved
        the served row through `find_next_completion_task(...)` and its
        Stop-arrival check has confirmed that row is a `stop` whose stored
        `resolved_target` block equals the live authority end. This callback
        therefore neither re-resolves nor re-matches the row, and it does NOT
        re-check arrival direction — direction is guaranteed at acquisition, not
        re-checked later (design_new.md "Path field schema", the Stop-arrival
        check bullet). A dwell after this stop is handled by the next eligible
        `tick_train_plan(...)`, which holds onward authority while the train
        sits in READY.
        """
        train, route, exec_state = self._resolve_run_route(train_id)
        if train is None or route is None or exec_state is None:
            return False

        self._sync_task_runs(route, exec_state)
        if not (0 <= walk_position < len(exec_state.task_runs)):
            # The caller resolved `walk_position` from `find_next_completion_task`,
            # so an out-of-range index is a broken invariant rather than a normal
            # miss. Diagnose and refuse rather than finish a fabricated row.
            self._emit_route_warning(
                train_id,
                "planning_failed",
                f"handle_path_completion: walk_position {walk_position} out of "
                f"range for {len(exec_state.task_runs)} task_runs; Plan must "
                f"resolve the served stop before the path completes.",
                operation="handle_path_completion",
            )
            return False

        row = exec_state.task_runs[walk_position]
        # Idempotency: `advance_walk_position(...)` no-ops on an already-finished
        # row, so a re-delivered completion neither double-advances the walk nor
        # double-appends. ROUND_TRIP reflection is carried by the appended row's
        # stored `walk_direction` (design_new.md "Route-run state"), not a
        # `RouteRun.direction` flip here.
        self.advance_walk_position(exec_state, route, walk_position)
        # design_new.md "Event log": the train reached its stop target. The
        # served block is the run's own copy of the target
        # (`task_runs[walk_position].resolved_target`), never re-read off the
        # dispatcher `Path`.
        target = row.resolved_target
        arrived_block = (
            target.requested_destination_block_id if target is not None else None
        )
        self._emit_event(
            "handle_path_completion",
            f"{train_id} arrived at {arrived_block}",
            train_id=train_id,
            path_id=path_id,
            payload={"block_id": arrived_block},
        )
        # A dwell after this stop is handled by the next eligible Plan dwell
        # step, which holds onward authority; the train stays READY.
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
        position's finish (`RouteRun.started_at` for row 0). It is computed for
        `waypoint` and `stop` rows only — never for `dwell` or `run`, because a
        dwell time is a hold duration and a run time is a simulated-time
        movement window tracked separately by `run_elapsed_seconds` — and never
        for a task with no `task_time`. Tasks pre-finished by start-mode seeding
        or skipped as broken are never routed here, so they keep
        `delay_class = None`.
        """
        if not (0 <= row_pos < len(exec_state.task_runs)):
            return
        row = exec_state.task_runs[row_pos]
        if not (0 <= row.task_index < len(route.tasks)):
            return
        task = route.tasks[row.task_index]
        if task.task_kind in ("dwell", "run") or task.task_time is None:
            return
        started = self._predecessor_finish_basis(exec_state, route, row_pos)
        if started is None:
            return
        finished = (
            row.finished_at
            if getattr(route, "timing_basis", "operating") == "realtime"
            else row.finished_at_operating
        )
        if finished is None:
            return
        elapsed = finished - started
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
    ) -> None:
        """Advance one walk-position: write `task_runs[walk_position].finished_at`,
        classify `delay_class`, and append the next walk task.

        This is `RouteExecutor.advance_walk_position(...)` per design_new.md
        "Route-run state" — the single canonical writer of the
        finished_at + delay_class + walk-append sequence. Every completion site
        (stop arrival, head-advance, covered-task advance, dwell-complete)
        routes through it so the three steps cannot drift. Idempotent: a
        walk-position whose `finished_at` is already set is a no-op.
        `_record_delay_class` no-ops for `dwell` and `run` rows and for tasks
        without `task_time`.

        Callers pass no clock value: this call samples both clocks itself, so
        the wall and operating readings of one finish are always taken at the
        same moment and no caller can hand in a stale `now` (design_new.md
        "`RouteExecutor.advance_walk_position(walk_position)`").
        """
        if not (0 <= walk_position < len(exec_state.task_runs)):
            return
        row = exec_state.task_runs[walk_position]
        if row.finished_at is not None:
            return
        now = self._now()
        row.finished_at = now
        # The operating-clock reading of the same moment, so a duration
        # measured across this row never mixes the two clocks.
        row.finished_at_operating = self._now_operating()
        self._record_delay_class(exec_state, route, walk_position, now)
        exec_state._append_next_walk_task(route, walk_position)

    # ------------------------------------------------------------------ removal
    def remove_train_route_run(self, train_id: str) -> None:
        """Tear down a `RouteRun` for `pending_route_abort` /
        `pending_power_off` / `pending_route_deactivate`.

        Per design_new.md "Route Edit And Reconciliation": looks up the train's
        currently-RUNNING `RouteRun`.

        When a run exists it performs the canonical teardown, then commits any
        held route-assignment replacement as the last operation:

          1. Call `Route.unassign_train(train_id)` on the run's `route_id`.
          2. Set `current_route_status = DEACTIVATED` and archive the
             row as a historical run (visible to `history_for_train(...)`).
             The derived `execution_state_for(train_id)` no longer returns it.
          3. Consume `_pending_route_assignments[train_id]` and apply its
             requested replacement, if any (design_new.md "Route Edit And Reconciliation"). The
             narrower same-route `_pending_reassign` entry is NOT applied here;
             the deactivate consumer applies it after this method returns.

        When NO run exists, it still removes any stored assignment and consumes
        `_pending_route_assignments[train_id]` exactly as the commit contract
        specifies (design_new.md "Route Edit And Reconciliation") -- this covers a train assigned
        before its first Plan allocation and an earlier teardown consumer in the
        same Action pass.

        Either way, `finalize_pending_route_deletions()` runs at the end so a
        route awaiting deferred deletion is archived + unregistered as soon as
        this teardown left it with no assignment and no live run.

        The originating cause is recorded only on the `path_cancellation` and
        `command_result` notifications, not on any field of the `RouteRun`.
        """
        exec_state = self.execution_state_for(train_id)
        if exec_state is None:
            # No active run to remove (a train assigned before its first Plan
            # allocation, or an earlier teardown consumer already ran this
            # pass). Per design_new.md "Route Edit And Reconciliation" the method still removes any
            # stored assignment, then commits any held replacement.
            old_route_id = self.assigned_route_id_for(train_id)
            if old_route_id:
                old_route = self.route_registry.get(old_route_id)
                if old_route is not None:
                    old_route.unassign_train(train_id)
            self._commit_pending_route_assignment(train_id)
            # A deferred-deletion route may now have lost its last assignment.
            self.finalize_pending_route_deletions()
            return

        # A dwelling train is READY (dwell is not a controller state), so there
        # is no dwell to exit before teardown.

        route_id = exec_state.route_id
        if route_id:
            route = self.route_registry.get(route_id)
            if route is not None:
                route.unassign_train(train_id)

        # Mark DEACTIVATED and move to the historical archive.
        exec_state.current_route_status = RouteRunStatus.DEACTIVATED
        self._archive_run(exec_state)

        # Clear the ATO engage bit as the run is torn down (design_new.md "ATO
        # status": `remove_train_route_run` clears `commands.engaged` in Action),
        # so the now-idle train and any replacement run start not engaged
        # (`ato_status == READY`) rather than `HELD`.
        controller = self._controller_by_id(train_id)
        if controller is not None:
            controller.commands.engaged = False

        # Commit any held route-assignment replacement as the last state
        # mutation, now that the old run is archived and unassigned
        # (design_new.md "Route Edit And Reconciliation"). The narrower same-route `_pending_reassign`
        # entry is applied separately by the deactivate consumer after this
        # method returns.
        self._commit_pending_route_assignment(train_id)

        # The route just unassigned may have been awaiting deferred deletion and
        # may now have no assignment and no live run.
        self.finalize_pending_route_deletions()

        # This call is the consume-side emitter for whichever of
        # `phase_latches.route_abort` / `route_deactivate` drove the teardown
        # (design_new.md "Latch consumption events"). The Action step that
        # invoked it clears the field once this returns. `latch_event=True`
        # keeps the commit half of the write/commit pair out of the
        # unchanged-repeat suppression.
        sc = self.simulation_controller
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is not None:
            try:
                from notification_center import train_ref as _tref, route_run_ref as _rref

                nc.emit(
                    source="RouteExecutor",
                    operation="remove_train_route_run",
                    kind=Kind.INFO,
                    message=f"route run torn down for {train_id}",
                    entities=[_tref(train_id), _rref(exec_state.run_id)],
                    event_time=time.time(),
                    payload={"route_id": exec_state.route_id},
                    latch_event=True,
                )
            except Exception:
                pass

    def finalize_pending_route_deletions(self) -> None:
        """Archive + unregister any route whose deferred deletion is now
        satisfied (design_new.md "Route deletion waits for live teardown").

        Called after each Action teardown (`remove_train_route_run`) and each
        natural finish (`finish_route_run`). For every route awaiting deletion,
        remove it from the active registry ONLY when it has no assignments and
        no live RUNNING run naming it. A pending route with a still-`RUNNING`
        run, or a train still in `assigned_train_ids` (which covers the window
        where a `route_deactivate` latch is armed but not yet consumed, since
        the train is not unassigned until that consumer runs), stays retained so
        Plan and Action keep resolving it through `get_route(...)`.

        Before removal the immutable final definition moves to
        `_historical_routes`, so historical `RouteRun.task_runs[*].task_index`
        and run-panel serialization can still resolve the task definitions they
        reference. Idempotent: a route no longer in the registry is simply
        dropped from the pending set.
        """
        for route_id in list(self.pending_route_deletions):
            route = self.route_registry.get(route_id)
            if route is None:
                # Already archived/removed; drop the stale pending marker.
                self.pending_route_deletions.discard(route_id)
                continue
            has_assignments = bool(getattr(route, "assigned_train_ids", []))
            has_live_run = any(
                st.route_id == route_id
                and st.current_route_status == RouteRunStatus.RUNNING
                for st in self.execution_states.values()
            )
            if has_assignments or has_live_run:
                continue
            # Nothing holds it: archive the immutable final definition, then
            # remove it from the active registry.
            self._historical_routes[route_id] = route
            self.unregister_route(route_id)
            self.pending_route_deletions.discard(route_id)

    def get_historical_route(self, route_id: str) -> Optional["Route"]:
        """Return the archived final definition of a deleted route, or ``None``.

        A route removed from the active registry by
        `finalize_pending_route_deletions()` is retained here (design_new.md
        "Route deletion waits for live teardown"). Used by the DELETE service for post-archival idempotency and
        by `get_route(...)`'s historical fallback.
        """
        return self._historical_routes.get(route_id)

    def record_pending_reassign(self, train_id: str, route_id: str) -> None:
        """Hold a same-route re-assignment until the Action deactivate clear
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

        Called only by the Action `pending_route_deactivate` consumer, after
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

        Per design_new.md "Shared-route edit teardown": when an
        earlier Action consumer (abort / power-off) or a natural finish ended
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
        after_walk_position: Optional[int] = None,
    ) -> Optional[ResolvedNextTaskProjection]:
        """Forward to `RouteTargetResolver.find_next_task_target(...)`."""
        return self._get_route_target_resolver().find_next_task_target(
            train_id, exclude=exclude, after_walk_position=after_walk_position
        )

    def find_next_move_target_from(
        self, walk_position: int, train_id: str
    ) -> Optional[str]:
        """Forward to `RouteTargetResolver.find_next_move_target_from(...)`.

        The seed-position variant of `find_next_move_target(...)`, used by
        **Run start-mode seeding**'s "No reversal first" ranking rule
        (design_new.md, item 11 under "Path And PathDispatcher").
        """
        return self._get_route_target_resolver().find_next_move_target_from(
            walk_position, train_id
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
        Plan step `6a0d`. Neither is ever finished by dispatcher path
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
