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
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

from runtime_payloads import AcquisitionMode, ErrorRecord

if TYPE_CHECKING:
    # Forward reference to ResolvedRouteTarget which lives in route_executor.py
    # for Wave 1 (S5 will move it to runtime_payloads.py).
    from route_executor import ResolvedRouteTarget


class TimingMode(Enum):
    """Timing mode for task departure."""

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
        task_time: Semantics depend on task_kind. For dwell: hold duration
            in seconds; omit (None) means depart immediately. For
            waypoint/stop: expected travel time in seconds from the previous
            task's location to this one (used for early/on-time/late
            classification).
        target_time: Optional absolute system-time Unix departure timestamp
            for `stop` tasks; granted immediately if the train arrives late.
        timing_mode: Timing classification (NONE / RELATIVE / ABSOLUTE).
        flexible_platform: For waypoint/stop on a platform_id task, allow
            covered-task index advance to count any compatible platform in
            the allowed set. No effect on station/block tasks.
        departure_mode: Optional per-task override of
            Route.default_departure_mode. Applies to stop tasks; ignored for
            others. None means inherit the route default.
        task_metadata: Optional kind-specific extension payload reserved for
            future fields; must not contain `flexible_platform` or
            `departure_mode` (those are top-level fields).
    """

    task_kind: Literal["waypoint", "stop", "dwell"]
    task_location: str
    task_direction: Literal["forward", "reverse", "either"] = "either"
    task_time: Optional[float] = None
    target_time: Optional[float] = None
    timing_mode: TimingMode = TimingMode.NONE
    flexible_platform: bool = False
    departure_mode: Optional[Literal["auto", "manual"]] = None
    task_metadata: Optional[Dict[str, Any]] = None

    def should_depart(
        self,
        now: float,
        dwell_started_at: Optional[float],
    ) -> bool:
        """Return True when the task is ready to depart.

        For `dwell` tasks, returns True when:
          * `target_time` is present and `now >= target_time`, or
          * `task_time` has elapsed since `dwell_started_at`, or
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

        if self.task_time is None or self.task_time <= 0.0:
            return True

        return (now - dwell_started_at) >= self.task_time

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
            return self.task_time if self.task_time is not None else 0.0

        if self.task_time is None or self.task_time <= 0.0:
            return 0.0

        return max(0.0, self.task_time - (now - dwell_started_at))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "task_kind": self.task_kind,
            "task_location": self.task_location,
            "task_direction": self.task_direction,
            "task_time": self.task_time,
            "target_time": self.target_time,
            "timing_mode": self.timing_mode.value,
            "flexible_platform": self.flexible_platform,
            "departure_mode": self.departure_mode,
            "task_metadata": self.task_metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteTask":
        """Reconstruct from dict after deserialization."""
        timing_mode_str = data.get("timing_mode", "none")
        try:
            timing_mode = TimingMode(timing_mode_str)
        except ValueError:
            timing_mode = TimingMode.NONE

        kind = data.get("task_kind", "waypoint")
        if kind not in ("waypoint", "stop", "dwell"):
            kind = "waypoint"

        direction = data.get("task_direction", "either")
        if direction not in ("forward", "reverse", "either"):
            direction = "either"

        # Accept the canonical `task_time` and fall back to legacy
        # `dwell_time_sec` saved data.
        if "task_time" in data and data["task_time"] is not None:
            task_time: Optional[float] = float(data["task_time"])
        elif "dwell_time_sec" in data:
            legacy = float(data.get("dwell_time_sec", 0.0))
            task_time = legacy if legacy > 0.0 else None
        else:
            task_time = None

        departure_mode = data.get("departure_mode")
        if departure_mode not in ("auto", "manual", None):
            departure_mode = None

        task_metadata = data.get("task_metadata")
        if task_metadata is not None and not isinstance(task_metadata, dict):
            task_metadata = None

        return cls(
            task_kind=kind,  # type: ignore[arg-type]
            task_location=str(data.get("task_location", "")),
            task_direction=direction,  # type: ignore[arg-type]
            task_time=task_time,
            target_time=data.get("target_time"),
            timing_mode=timing_mode,
            flexible_platform=bool(data.get("flexible_platform", False)),
            departure_mode=departure_mode,  # type: ignore[arg-type]
            task_metadata=task_metadata,
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
    last_error: Optional["ErrorRecord"] = None
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
    acquisition_mode: AcquisitionMode = AcquisitionMode.ALL_OR_NOTHING
    default_departure_mode: Literal["auto", "manual"] = "auto"
    route_version: int = 1
    reconciliation_policy: str = "nearest_valid_target"

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

    # ── Activation Methods ─────────────────────────────────────────

    def activate(self, simulation_time: float) -> None:
        """Activate the route for execution (per-run cursors are managed by RouteExecutor)."""
        self.is_active = True
        self.started_at = simulation_time

    def deactivate(self, simulation_controller: Optional[Any] = None) -> None:
        """Deactivate the route.

        Per design_new.md inclusive-latch rule: when a ``simulation_controller``
        is supplied, walk every assigned train and call
        ``controller.phase_latches.set_route_deactivate(...)``, which writes
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
            controller.phase_latches.set_route_deactivate(
                reason=diagnostic,
                has_active_path=controller.received_path_plan is not None,
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

    def is_train_assigned(self, train_id: str) -> bool:
        """Check whether a train is currently assigned."""
        return train_id in self.assigned_train_ids

    def get_assigned_train_count(self) -> int:
        """Return the count of trains assigned to this route."""
        return len(self.assigned_train_ids)

    # ── Progress ───────────────────────────────────────────────────

    def get_progress(self, task_index: int) -> float:
        """Get route progress as fraction (0.0 to 1.0) for ONE_OFF mode."""
        if self.mode != RouteMode.ONE_OFF or not self.tasks:
            return 0.0
        return min(1.0, task_index / len(self.tasks))

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
            "acquisition_mode": self.acquisition_mode.value,
            "default_departure_mode": self.default_departure_mode,
            "route_version": self.route_version,
            "reconciliation_policy": self.reconciliation_policy,
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

        raw_acq = data.get("acquisition_mode", AcquisitionMode.ALL_OR_NOTHING)
        if isinstance(raw_acq, AcquisitionMode):
            acquisition_mode = raw_acq
        else:
            try:
                acquisition_mode = AcquisitionMode(raw_acq)
            except ValueError:
                acquisition_mode = AcquisitionMode.ALL_OR_NOTHING

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
        )
