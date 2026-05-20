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
"""

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


def task_location_id(loc: TaskLocation) -> str:
    """Return the underlying id from a TaskLocation variant."""
    if isinstance(loc, BlockTarget):
        return loc.block_id
    if isinstance(loc, PlatformTarget):
        return loc.platform_id
    return loc.station_id


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


class RouteRunStatus(Enum):
    """Per-run lifecycle state.

    Per design_new.md `current_route_status.state` enum. v1 stores exactly
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
        task_kind: "waypoint" | "stop" | "dwell".
        task_location: One of BlockTarget / PlatformTarget / StationTarget.
            The tagged union enforces exactly one targeting mode is present.
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
    """

    task_kind: Literal["waypoint", "stop", "dwell"]
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
        if kind not in ("waypoint", "stop", "dwell"):
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

    # ── Definition Methods ─────────────────────────────────────────

    def add_task(self, task: RouteTask) -> None:
        """Append a RouteTask."""
        self.tasks.append(task)

    def insert_task(self, index: int, task: RouteTask) -> None:
        """Insert a RouteTask at a specific position."""
        self.tasks.insert(index, task)

    def remove_task(self, index: int) -> Optional[RouteTask]:
        """Remove the RouteTask at `index` and return it."""
        if 0 <= index < len(self.tasks):
            return self.tasks.pop(index)
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
        )
