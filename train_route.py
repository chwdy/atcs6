"""
Train Route Management

Per design_new.md "Route And RouteExecutor" section. A Route is the shared
travel plan — an ordered list of RouteTask entries (waypoint / stop / dwell)
plus route-level policy (mode, priority, acquisition mode, departure mode,
reconciliation policy). Per-train mutable execution state lives separately
in the route run (RouteExecutionState / RouteRun) owned by RouteExecutor.

The module provides:
- RouteTask: one task in a Route (kind, location, direction, timing).
- RouteTaskRun: per-run task table entry parallel to Route.tasks.
- RouteRunStatus: enum of run-level diagnostic states.
- Route: shared travel plan (ordered tasks + route-level policy).
- RouteMode: ONE_OFF, CIRCULAR, ROUND_TRIP.
- TimingMode: NONE, RELATIVE, ABSOLUTE.
- PlatformSide: re-exported from enums.

Removed in v1: Waypoint (replaced by RouteTask), TravelDirectionPolicy
(replaced by block-relative task_direction).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

from enums import PlatformSide

if TYPE_CHECKING:
    # Forward reference to ResolvedRouteTarget which lives in route_executor.py
    # for Wave 1 (S5 will move it to runtime_payloads.py).
    from route_executor import ResolvedRouteTarget


class TimingMode(Enum):
    """Timing mode for task departure (legacy field, retained for serialization)."""

    NONE = "none"
    RELATIVE = "relative"
    ABSOLUTE = "absolute"


class RouteMode(Enum):
    """Route traversal mode."""

    ONE_OFF = "one_off"  # A -> B -> C -> stop
    CIRCULAR = "circular"  # A -> B -> C -> A -> B -> C -> ...
    ROUND_TRIP = "round_trip"  # A -> B -> C -> B -> A -> B -> ...


class RouteRunStatus(Enum):
    """Per-run diagnostic state.

    Per design_new.md `current_route_status.state` enum. All values are
    diagnostic only; runtime branching keys on controller state, latches,
    and current span coverage, not on this enum.
    """

    PLANNING = "PLANNING"  # Run created, no planning yet
    RUNNING = "RUNNING"  # Ordinary execution (incl. dwell, between-legs)
    AT_TASK = "AT_TASK"  # At a task target (UI-only intermediate)
    DEPARTING = "DEPARTING"  # Departure in progress (UI-only intermediate)
    FINISHED = "FINISHED"  # Terminal walk complete
    DEACTIVATED = "DEACTIVATED"  # Shared route deactivated mid-run
    NO_PATH = "NO_PATH"  # Last Phase 1 exhausted same-tick targets w/o authority
    DEVIATED = "DEVIATED"  # Cancellation-backed recovery pending


@dataclass
class RouteTask:
    """One unified task entry in Route.tasks.

    Per design_new.md "RouteTask data". Every entry in Route.tasks is a
    RouteTask. The kind discriminator (task_kind) plus the single populated
    targeting field on task_location decides everything else.

    Attributes:
        task_kind: "waypoint" | "stop" | "dwell".
        task_location: A block_id, platform_id, or station_id (depending on
            kind). Validation enforces exactly one targeting concept; in v1
            we store it as a single string and let the Network resolver
            distinguish.
        task_direction: Block-relative traversal preference. RouteExecutor
            resolves "either" to a concrete direction before any dispatcher
            call; the dispatcher never receives "either".
        dwell_time_sec: Hold duration in seconds for `dwell` kind. Equivalent
            to the `task_time` semantic on a dwell task in design_new.md.
        target_time: Optional absolute system-time Unix departure timestamp
            for `stop` tasks; granted immediately if the train arrives late.
        timing_mode: Legacy timing classification (NONE / RELATIVE / ABSOLUTE).
            Retained for serialization of timing intent.
        flexible_platform: For waypoint/stop on a platform_id task, allow
            covered-task index advance to count any compatible platform in
            the allowed set. No effect on station/block tasks.
        required_platform_side: Side filter for flexible-platform selection.
            Defaults to EITHER.
    """

    task_kind: Literal["waypoint", "stop", "dwell"]
    task_location: str
    task_direction: Literal["forward", "reverse", "either"] = "either"
    dwell_time_sec: float = 0.0
    target_time: Optional[float] = None
    timing_mode: TimingMode = TimingMode.NONE
    flexible_platform: bool = False
    required_platform_side: PlatformSide = PlatformSide.EITHER

    def should_depart(
        self,
        now: float,
        dwell_started_at: Optional[float],
    ) -> bool:
        """Return True when the task is ready to depart.

        For `dwell` tasks, returns True when:
          * `target_time` is present and `now >= target_time`, or
          * `dwell_time_sec` has elapsed since `dwell_started_at`, or
          * neither is set (depart immediately).

        For `waypoint` and `stop` tasks, returns True (the caller controls
        movement; this method is consulted only by the Phase 1 dwell-complete
        check).
        """
        if self.task_kind != "dwell":
            return True

        if self.target_time is not None:
            return now >= self.target_time

        if dwell_started_at is None:
            # No predecessor finish yet → not ready.
            return False

        if self.dwell_time_sec <= 0.0:
            return True

        return (now - dwell_started_at) >= self.dwell_time_sec

    def get_remaining_dwell_time(
        self,
        now: float,
        dwell_started_at: Optional[float],
    ) -> float:
        """Return remaining seconds before depart-ready (`dwell` only)."""
        if self.task_kind != "dwell":
            return 0.0

        if self.target_time is not None:
            return max(0.0, self.target_time - now)

        if dwell_started_at is None:
            return self.dwell_time_sec

        if self.dwell_time_sec <= 0.0:
            return 0.0

        return max(0.0, self.dwell_time_sec - (now - dwell_started_at))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "task_kind": self.task_kind,
            "task_location": self.task_location,
            "task_direction": self.task_direction,
            "dwell_time_sec": self.dwell_time_sec,
            "target_time": self.target_time,
            "timing_mode": self.timing_mode.value,
            "flexible_platform": self.flexible_platform,
            "required_platform_side": self.required_platform_side.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteTask":
        """Reconstruct from dict after deserialization."""
        timing_mode_str = data.get("timing_mode", "none")
        try:
            timing_mode = TimingMode(timing_mode_str)
        except ValueError:
            timing_mode = TimingMode.NONE

        side_value = data.get("required_platform_side", "either")
        try:
            required_side = PlatformSide(side_value)
        except ValueError:
            required_side = PlatformSide.EITHER

        kind = data.get("task_kind", "waypoint")
        if kind not in ("waypoint", "stop", "dwell"):
            kind = "waypoint"

        direction = data.get("task_direction", "either")
        if direction not in ("forward", "reverse", "either"):
            direction = "either"

        return cls(
            task_kind=kind,  # type: ignore[arg-type]
            task_location=str(data.get("task_location", "")),
            task_direction=direction,  # type: ignore[arg-type]
            dwell_time_sec=float(data.get("dwell_time_sec", 0.0)),
            target_time=data.get("target_time"),
            timing_mode=timing_mode,
            flexible_platform=bool(data.get("flexible_platform", False)),
            required_platform_side=required_side,
        )


@dataclass
class RouteTaskRun:
    """Per-run task-state entry; one per task index in Route.tasks.

    Per design_new.md "RouteTaskRun schema". Owns per-task timing,
    resolved dispatcher target, and per-task error context.
    """

    task_index: int
    resolved_target: Optional["ResolvedRouteTarget"] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    delay_class: Optional[Literal["early", "on_time", "late"]] = None
    last_error: Optional[str] = None
    error_time: Optional[float] = None
    current_route_status: RouteRunStatus = RouteRunStatus.PLANNING


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
            ("all_or_nothing" or "best_effort").
        default_departure_mode: Ordinary departure policy ("auto" / "manual").
        route_version: Monotonic shared-route revision; incremented on any
            route edit that changes task order or execution policy.
        reconciliation_policy: How active runs reconcile against later
            shared-route edits ("nearest_valid_target" / "restart_from_top").
        last_error: Per-train errors live on the run, not here, but kept on
            Route for legacy callers that still write definition-level errors.
        error_time: Same as last_error.
        current_task_index: Legacy route-level cursor; route runs own per-train
            cursors. Kept for legacy code paths still mutating it.
        direction: Legacy route-level direction; superseded by per-run direction.
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
    acquisition_mode: Literal["all_or_nothing", "best_effort"] = "all_or_nothing"
    default_departure_mode: Literal["auto", "manual"] = "auto"
    route_version: int = 1
    reconciliation_policy: str = "nearest_valid_target"

    # Error tracking (kept for legacy callers; per-run errors are canonical)
    last_error: Optional[str] = None
    error_time: Optional[float] = None

    # Legacy execution fields retained for current-code callers; route runs
    # are the canonical owner of per-train cursor state.
    current_task_index: int = 0
    direction: int = 1

    # ── Definition Methods ─────────────────────────────────────────

    def add_task(self, task: RouteTask) -> None:
        """Append a RouteTask. Bumps route_version."""
        self.tasks.append(task)
        self.route_version += 1

    def insert_task(self, index: int, task: RouteTask) -> None:
        """Insert a RouteTask at a specific position. Bumps route_version."""
        self.tasks.insert(index, task)
        self.route_version += 1

    def remove_task(self, index: int) -> Optional[RouteTask]:
        """Remove the RouteTask at `index` and return it. Bumps route_version."""
        if 0 <= index < len(self.tasks):
            removed = self.tasks.pop(index)
            self.route_version += 1
            return removed
        return None

    def get_task_count(self) -> int:
        """Return the number of tasks."""
        return len(self.tasks)

    def get_task_at(self, index: int) -> Optional[RouteTask]:
        """Return the RouteTask at `index`, or None when out of range."""
        if 0 <= index < len(self.tasks):
            return self.tasks[index]
        return None

    def get_current_task(self) -> Optional[RouteTask]:
        """Return the task at the legacy route-level cursor."""
        return self.get_task_at(self.current_task_index)

    def advance_to_next_task(self) -> Optional[RouteTask]:
        """Advance the legacy route-level cursor and return the new task."""
        if not self.tasks:
            return None

        if self.mode == RouteMode.ONE_OFF:
            self.current_task_index += 1
            if self.current_task_index >= len(self.tasks):
                return None

        elif self.mode == RouteMode.CIRCULAR:
            self.current_task_index += 1
            if self.current_task_index >= len(self.tasks):
                self.current_task_index = 0

        elif self.mode == RouteMode.ROUND_TRIP:
            self.current_task_index += self.direction
            if self.current_task_index >= len(self.tasks):
                self.direction = -1
                self.current_task_index = max(0, len(self.tasks) - 2)
            elif self.current_task_index < 0:
                self.direction = 1
                self.current_task_index = 0 if len(self.tasks) == 1 else 1

        return self.get_task_at(self.current_task_index)

    # ── Activation Methods ─────────────────────────────────────────

    def activate(self, simulation_time: float) -> None:
        """Activate the route for execution (per-run cursors are managed by RouteExecutor)."""
        self.is_active = True
        self.started_at = simulation_time
        self.last_error = None
        self.error_time = None

    def deactivate(self) -> None:
        """Deactivate the route (pause execution)."""
        self.is_active = False

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

    def is_train_assigned(self, train_id: str) -> bool:
        """Check whether a train is currently assigned."""
        return train_id in self.assigned_train_ids

    def get_assigned_train_count(self) -> int:
        """Return the count of trains assigned to this route."""
        return len(self.assigned_train_ids)

    # ── Progress (legacy helpers) ──────────────────────────────────

    def get_progress(self, task_index: int) -> float:
        """Get route progress as fraction (0.0 to 1.0) for ONE_OFF mode."""
        if self.mode != RouteMode.ONE_OFF or not self.tasks:
            return 0.0
        return min(1.0, task_index / len(self.tasks))

    # ── Error Methods ──────────────────────────────────────────────

    def set_error(self, message: str, simulation_time: float) -> None:
        """Record an execution error on the route definition (legacy)."""
        self.last_error = message
        self.error_time = simulation_time

    def clear_error(self) -> None:
        """Clear any recorded definition-level error (legacy)."""
        self.last_error = None
        self.error_time = None

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
            "priority": self.priority,
            "acquisition_mode": self.acquisition_mode,
            "default_departure_mode": self.default_departure_mode,
            "route_version": self.route_version,
            "reconciliation_policy": self.reconciliation_policy,
            "last_error": self.last_error,
            "error_time": self.error_time,
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

        acquisition_mode = data.get("acquisition_mode", "all_or_nothing")
        if acquisition_mode not in ("all_or_nothing", "best_effort"):
            acquisition_mode = "all_or_nothing"

        default_departure_mode = data.get("default_departure_mode", "auto")
        if default_departure_mode not in ("auto", "manual"):
            default_departure_mode = "auto"

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
            default_departure_mode=default_departure_mode,  # type: ignore[arg-type]
            route_version=int(data.get("route_version", 1)),
            reconciliation_policy=str(
                data.get("reconciliation_policy", "nearest_valid_target")
            ),
            last_error=data.get("last_error"),
            error_time=data.get("error_time"),
        )
