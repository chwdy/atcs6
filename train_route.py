"""
Train Route Management

Per design_new.md "Route And RouteExecutor" section. A Route is the shared
travel plan — an ordered list of RouteTask entries (waypoint / stop / dwell)
plus route-level policy (mode, priority, acquisition mode). Per-train
mutable execution state lives separately in the route run
(RouteExecutionState / RouteRun) owned by RouteExecutor.

The module provides:
- BlockTarget / PlatformTarget / StationTarget: tagged-union dataclasses
  for `RouteTask.task_location`.
- RouteTask: one task in a Route (kind, location, direction, timing).
- RouteTaskRun: per-run task table entry parallel to Route.tasks.
- RouteRunStatus: enum of run-level diagnostic states.
- Route: shared travel plan (ordered tasks + route-level policy).
- RouteMode: ONE_OFF, CIRCULAR, ROUND_TRIP.
- RouteStartMode: FIRST, NEAREST, SPECIFIC (which stop a fresh run begins at).
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union

from runtime_payloads import AcquisitionMode, ErrorRecord

if TYPE_CHECKING:
    # Forward reference to ResolvedRouteTarget which lives in route_executor.py
    # for Wave 1 (S5 will move it to runtime_payloads.py).
    from route_executor import ResolvedRouteTarget


# =============================================================================
# Task location tagged union
# =============================================================================
#
# Per design_new.md line 1125, `RouteTask.task_location` is a tagged union of
# three concrete target dataclasses. The union enforces that exactly one
# targeting mode is present and dispatch is structural rather than probe-in-order.


@dataclass(frozen=True)
class BlockTarget:
    """Direct block target."""

    block_id: str


@dataclass(frozen=True)
class PlatformTarget:
    """Concrete platform target."""

    platform_id: str


@dataclass(frozen=True)
class StationTarget:
    """Logical station-group target; resolved to a concrete platform at runtime."""

    station_id: str


TaskLocation = Union[BlockTarget, PlatformTarget, StationTarget]


def _coerce_task_location(value: Any) -> TaskLocation:
    """Coerce input into one of the three concrete `TaskLocation` variants.

    Accepted shapes:
      * an already-built `BlockTarget` / `PlatformTarget` / `StationTarget`.
      * a structured dict carrying exactly one of the three id keys
        (`{"block_id": ...}`, `{"platform_id": ...}`, `{"station_id": ...}`).
      * a plain string — convenience for in-process constructors, interpreted
        as a platform_id. Persisted JSON and HTTP payloads must use the
        structured dict form.
    """
    if isinstance(value, (BlockTarget, PlatformTarget, StationTarget)):
        return value
    if isinstance(value, dict):
        present = [
            k for k in ("block_id", "platform_id", "station_id") if k in value
        ]
        if len(present) != 1:
            raise ValueError(
                f"task_location dict must carry exactly one of "
                f"block_id/platform_id/station_id, got keys: {list(value.keys())}"
            )
        key = present[0]
        ident = str(value[key])
        if key == "block_id":
            return BlockTarget(block_id=ident)
        if key == "platform_id":
            return PlatformTarget(platform_id=ident)
        return StationTarget(station_id=ident)
    if isinstance(value, str):
        return PlatformTarget(platform_id=value)
    raise TypeError(
        f"task_location must be a BlockTarget / PlatformTarget / StationTarget "
        f"or a coercible dict/str; got {type(value).__name__}"
    )


def task_location_to_dict(loc: TaskLocation) -> Dict[str, str]:
    """Serialize a TaskLocation to its canonical wire shape."""
    if isinstance(loc, BlockTarget):
        return {"block_id": loc.block_id}
    if isinstance(loc, PlatformTarget):
        return {"platform_id": loc.platform_id}
    return {"station_id": loc.station_id}


class RouteMode(Enum):
    """Route traversal mode."""

    ONE_OFF = "one_off"  # A -> B -> C -> stop
    CIRCULAR = "circular"  # A -> B -> C -> A -> B -> C -> ...
    ROUND_TRIP = "round_trip"  # A -> B -> C -> B -> A -> B -> ...


class RouteStartMode(Enum):
    """Policy for which stop a fresh route run begins at.

    Per design_new.md "Run start-mode seeding" canonical home. Consulted once,
    when Phase 1 allocates a fresh RouteRun; it seeds the run's starting
    walk-position and is never re-read after that. This is why it is a
    route-level default plus a per-assignment input, not a live RouteRun
    override.

    Values:
        FIRST: (default) start at the first task (``task_runs[0]``) — the train
            then auto-advances past already-covered waypoints exactly as it
            does today.
        NEAREST: seed the run at the best ``task_kind == "stop"`` task for the
            train to begin at. A stop is eligible only when the train has a
            viable directed path to its resolved block (respecting the task's
            direction and ``Block.allowed_travel_directions``). Eligible stops
            are ranked, each criterion breaking ties left by the ones before:
            (1) no-reversal — prefer a stop the train can drive straight on
            from over one it would have to reverse out of; (2) free — prefer a
            stop no other train has taken; (3) nearest by track distance; (4)
            lowest task index. Falls back to FIRST when no stop has a viable
            directed path.
        SPECIFIC: start at the stop named by ``Route.start_task_index`` (or the
            per-assignment override). Falls back to FIRST when the index is out
            of range or does not name a ``stop`` task.
    """

    FIRST = "first"
    NEAREST = "nearest"
    SPECIFIC = "specific"


class RouteRunStatus(Enum):
    """Per-run lifecycle state.

    Per design_new.md `current_route_status` enum. v1 stores exactly
    these three states; deviation is expressed via the `path_cancelled`
    latch on `RouteRun` and surfaced through the derived `display_state`
    projection. Runtime branching keys on controller state, latches, and
    current span coverage, not on this enum.
    """

    RUNNING = "RUNNING"  # Ordinary execution (incl. dwell, between-legs)
    FINISHED = "FINISHED"  # Terminal walk complete
    DEACTIVATED = "DEACTIVATED"  # Shared route deactivated mid-run


@dataclass
class RouteTask:
    """One unified task entry in Route.tasks.

    Per design_new.md "RouteTask data". Every entry in Route.tasks is a
    RouteTask. The kind discriminator (`task_kind`) plus the variant of
    `task_location` decides everything else.

    Attributes:
        task_kind: "waypoint" | "stop" | "dwell" | "run".
        task_location: One of BlockTarget / PlatformTarget / StationTarget.
            The tagged union enforces exactly one targeting mode is present.
            For a `run` task the location is only the direction seed for the
            leg the run rolls along; the run ends on a timer, not on arrival.
        task_direction: Block-relative traversal preference. RouteExecutor
            resolves "either" to a concrete direction before any dispatcher
            call; the dispatcher never receives "either".
        task_time: Semantics depend on task_kind. For dwell: hold duration
            in seconds; omit (None) means depart immediately. For run: how
            long in seconds the train keeps moving before the executor lets
            it coast to a smooth ATC stop (position-agnostic). For
            waypoint/stop: expected travel time in seconds from the previous
            task's location to this one (used for early/on-time/late
            classification).
        target_time: Optional absolute system-time Unix departure timestamp
            for `stop` tasks; granted immediately if the train arrives late.
    """

    task_kind: Literal["waypoint", "stop", "dwell", "run"]
    task_location: TaskLocation
    task_direction: Literal["forward", "reverse", "either"] = "either"
    task_time: Optional[float] = None
    target_time: Optional[float] = None

    def __post_init__(self) -> None:
        # Coerce convenience inputs (plain string, dict) into the canonical
        # tagged-union form. Outside callers should pass a concrete variant.
        if not isinstance(
            self.task_location, (BlockTarget, PlatformTarget, StationTarget)
        ):
            object.__setattr__(
                self,
                "task_location",
                _coerce_task_location(self.task_location),
            )

    def should_depart(
        self,
        now: float,
        dwell_started_at: Optional[float],
    ) -> bool:
        """Return True when the task is ready to depart.

        For a `dwell` task the canonical rule (design_new.md "Task time
        semantics") is: depart when

            now >= max(dwell_started_at, target_time) + task_time

        treating `target_time` as -inf when unset and `task_time` as 0 when
        unset. So with both set, the train holds for `task_time` after the
        later of its actual arrival and the scheduled start — a late arrival
        still serves the full hold, an early arrival waits for the schedule.

        For `waypoint` and `stop` tasks, returns True (the caller controls
        movement; this method is consulted only by the Phase 1 dwell-complete
        check).
        """
        if self.task_kind != "dwell":
            return True

        task_t = self.task_time if (self.task_time is not None and self.task_time > 0.0) else 0.0

        if dwell_started_at is None:
            # Predecessor not finished, so the dwell has not actually started.
            # Only an absolute scheduled departure can authorize leaving early.
            if self.target_time is None:
                return False
            return now >= self.target_time + task_t

        if self.target_time is None:
            base = dwell_started_at
        else:
            base = max(dwell_started_at, self.target_time)
        return now >= base + task_t

    def get_remaining_dwell_time(
        self,
        now: float,
        dwell_started_at: Optional[float],
    ) -> float:
        """Return remaining seconds before depart-ready (`dwell` only).

        Mirrors `should_depart`: remaining = (depart threshold) - now, clamped
        at 0, where the threshold is `max(dwell_started_at, target_time) +
        task_time` (design_new.md "Task time semantics").
        """
        if self.task_kind != "dwell":
            return 0.0

        task_t = self.task_time if (self.task_time is not None and self.task_time > 0.0) else 0.0

        if dwell_started_at is None:
            if self.target_time is None:
                return task_t
            return max(0.0, self.target_time + task_t - now)

        if self.target_time is None:
            base = dwell_started_at
        else:
            base = max(dwell_started_at, self.target_time)
        return max(0.0, base + task_t - now)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict.

        `task_location` is emitted as the canonical structured shape
        (`{"block_id"/"platform_id"/"station_id": "..."}`).
        """
        return {
            "task_kind": self.task_kind,
            "task_location": task_location_to_dict(self.task_location),
            "task_direction": self.task_direction,
            "task_time": self.task_time,
            "target_time": self.target_time,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteTask":
        """Reconstruct from dict after deserialization.

        `task_location` must use the canonical structured form
        (`{"block_id"/"platform_id"/"station_id": "..."}`).
        """
        kind = data.get("task_kind", "waypoint")
        if kind not in ("waypoint", "stop", "dwell", "run"):
            kind = "waypoint"

        direction = data.get("task_direction", "either")
        if direction not in ("forward", "reverse", "either"):
            direction = "either"

        task_time: Optional[float] = (
            float(data["task_time"])
            if data.get("task_time") is not None
            else None
        )

        return cls(
            task_kind=kind,  # type: ignore[arg-type]
            task_location=_coerce_task_location(data.get("task_location", "")),
            task_direction=direction,  # type: ignore[arg-type]
            task_time=task_time,
            target_time=data.get("target_time"),
        )


@dataclass
class RouteTaskRun:
    """Per-run task-state entry; one per task index in Route.tasks.

    Per design_new.md "RouteTaskRun schema". Owns per-task `finished_at`,
    resolved dispatcher target, delay classification, and error context.
    The `started_at` field is derived: read it via
    `RouteExecutionState.task_started_at(i)`, which returns
    `RouteRun.started_at` for i==0 and `task_runs[i-1].finished_at` otherwise.
    """

    task_index: int
    resolved_target: Optional["ResolvedRouteTarget"] = None
    finished_at: Optional[float] = None
    delay_class: Optional[Literal["early", "on_time", "late"]] = None
    last_error: Optional["ErrorRecord"] = None
    # Stored simulated-time accumulator for a `task_kind == "run"` task's
    # timer. Per design_new.md "RouteTaskRun schema": Phase 1 step `6a0d`
    # is its only writer. Meaningless for every other task_kind.
    run_elapsed_seconds: float = 0.0


@dataclass
class Route:
    """Shared travel plan — ordered tasks plus route-level policy.

    Per design_new.md "Route data". Per-train mutable execution state lives
    in RouteExecutionState (RouteRun), keyed by train_id; this class owns
    only definition data and route-level policy.

    Attributes:
        route_id: Unique identifier.
        name: Display name.
        tasks: Ordered list of RouteTask entries.
        mode: How to traverse tasks (ONE_OFF, CIRCULAR, ROUND_TRIP).
        assigned_train_ids: Trains assigned to this shared route.
        is_active: Whether route is enabled for execution.
        started_at: System-time Unix seconds when route was activated.
        priority: Route-executor scheduling priority (0..9, default 5).
        acquisition_mode: Default route path-acquisition policy
            ("all_or_nothing" or "best_effort"). Default is ``best_effort``
            per design_new.md line 1052.
        manual_override: Operator override for ``manual`` resources (default
            False); route-level only, not a per-run override. See
            design_new.md "manual_override" (Route field schema).
        start_mode: Which stop a fresh run begins at (FIRST / NEAREST /
            SPECIFIC). Default FIRST. See "Run start-mode seeding".
        start_task_index: Stop index used only when ``start_mode == SPECIFIC``.
    """

    route_id: str
    name: str = ""
    tasks: List[RouteTask] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_OFF

    # Assignment
    assigned_train_ids: List[str] = field(default_factory=list)

    # Activation state
    is_active: bool = False
    started_at: Optional[float] = None

    # Route-level policy (v1)
    priority: int = 5
    acquisition_mode: AcquisitionMode = AcquisitionMode.BEST_EFFORT
    # Operator override for `manual` resources (default False). Set True only on
    # operator-initiated routes (the Quick Route tool). RouteExecutor passes this
    # into every PathDispatcher.create_path / extend_path call for the run, and
    # the target resolver applies it to `manual` target filtering, per
    # design_new.md "manual_override" (Route field schema) and the `disabled` and
    # `manual` availability flags canonical home. Route-level only — never a
    # per-run override.
    manual_override: bool = False
    # Start-mode default (per design_new.md "Run start-mode seeding"). Read once
    # when Phase 1 allocates a fresh RouteRun to seed its starting walk-position;
    # NOT a live RouteRun override (it has no meaning after allocation). A
    # per-assignment choice can override this for a single run. `start_task_index`
    # is the stop index used only when `start_mode == SPECIFIC`.
    start_mode: RouteStartMode = RouteStartMode.FIRST
    start_task_index: Optional[int] = None

    # Edit timestamps (per design_new.md "Route edit timestamps"). `created_at`
    # is sampled once in `__post_init__`; `last_edited_at` is written by the
    # POST/PUT route-definition handlers when name/tasks/mode/priority/
    # acquisition_mode is mutated. Neither field
    # is touched by assign/unassign or activate/deactivate.
    created_at: float = 0.0
    last_edited_at: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()

    # ── Definition Methods ─────────────────────────────────────────

    def add_task(self, task: RouteTask) -> None:
        """Append a RouteTask."""
        self.tasks.append(task)

    def get_task_count(self) -> int:
        """Return the number of tasks."""
        return len(self.tasks)

    def get_task_at(self, index: int) -> Optional[RouteTask]:
        """Return the RouteTask at `index`, or None when out of range."""
        if 0 <= index < len(self.tasks):
            return self.tasks[index]
        return None

    # ── Activation Methods ─────────────────────────────────────────

    def activate(self, simulation_time: float) -> None:
        """Activate the route for execution (per-run cursors are managed by RouteExecutor)."""
        self.is_active = True
        self.started_at = simulation_time

    def deactivate(self, simulation_controller: Optional[Any] = None) -> None:
        """Deactivate the route.

        Per design_new.md inclusive-latch rule: when a ``simulation_controller``
        is supplied, walk every assigned train and call
        ``controller.route_hooks.phase_latches.set_route_deactivate(...)``, which writes
        the deactivate latch and (when an active path exists) fans out to
        ``set_path_cancel_request(initiator="revoked")``. Phase 3 then commits
        the cancel and the route-run teardown.

        ``simulation_controller`` is optional so direct-pause callers (tests,
        internal route definition tooling) can flip the flag without needing
        the runtime surface; production deactivation through the admin
        handler MUST pass it so the controller mailboxes are written.
        """
        self.is_active = False
        if simulation_controller is None:
            return

        diagnostic = f"route_deactivated:{self.route_id}"
        for train_id in list(self.assigned_train_ids):
            controller = simulation_controller.get_controller(train_id)
            if controller is None:
                continue
            controller.route_hooks.phase_latches.set_route_deactivate(
                reason=diagnostic,
                has_active_path=controller.path_projection.received_path_plan is not None,
            )

    # ── Assignment Methods ─────────────────────────────────────────

    def assign_train(self, train_id: str) -> bool:
        """Assign a train. Returns True when added, False when already assigned."""
        if train_id in self.assigned_train_ids:
            return False
        self.assigned_train_ids.append(train_id)
        return True

    def unassign_train(self, train_id: str) -> bool:
        """Remove a train from this route. Returns True when removed."""
        if train_id not in self.assigned_train_ids:
            return False
        self.assigned_train_ids.remove(train_id)
        return True

    # ── Serialization ──────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "route_id": self.route_id,
            "name": self.name,
            "tasks": [t.to_dict() for t in self.tasks],
            "mode": self.mode.value,
            "assigned_train_ids": list(self.assigned_train_ids),
            "is_active": self.is_active,
            "started_at": self.started_at,
            "created_at": self.created_at,
            "last_edited_at": self.last_edited_at,
            "priority": self.priority,
            "acquisition_mode": self.acquisition_mode.value,
            "manual_override": self.manual_override,
            "start_mode": self.start_mode.value,
            "start_task_index": self.start_task_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Route":
        """Reconstruct from a dict."""
        mode_str = data.get("mode", "one_off")
        try:
            mode = RouteMode(mode_str)
        except ValueError:
            mode = RouteMode.ONE_OFF

        tasks = [RouteTask.from_dict(td) for td in data.get("tasks", [])]

        raw_acq = data.get("acquisition_mode", AcquisitionMode.BEST_EFFORT)
        if isinstance(raw_acq, AcquisitionMode):
            acquisition_mode = raw_acq
        else:
            try:
                acquisition_mode = AcquisitionMode(raw_acq)
            except ValueError:
                acquisition_mode = AcquisitionMode.BEST_EFFORT

        raw_start = data.get("start_mode", RouteStartMode.FIRST)
        if isinstance(raw_start, RouteStartMode):
            start_mode = raw_start
        else:
            try:
                start_mode = RouteStartMode(raw_start)
            except ValueError:
                start_mode = RouteStartMode.FIRST

        raw_start_index = data.get("start_task_index")
        try:
            start_task_index = None if raw_start_index is None else int(raw_start_index)
        except (TypeError, ValueError):
            start_task_index = None

        return cls(
            route_id=str(data.get("route_id", "")),
            name=str(data.get("name", "")),
            tasks=tasks,
            mode=mode,
            assigned_train_ids=list(data.get("assigned_train_ids", [])),
            is_active=bool(data.get("is_active", False)),
            started_at=data.get("started_at"),
            priority=int(data.get("priority", 5)),
            acquisition_mode=acquisition_mode,  # type: ignore[arg-type]
            manual_override=bool(data.get("manual_override", False)),
            start_mode=start_mode,
            start_task_index=start_task_index,
            created_at=float(data.get("created_at") or 0.0),
            last_edited_at=data.get("last_edited_at"),
        )
