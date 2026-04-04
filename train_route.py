"""
Train Route Management

Definition:
This module defines the Route and Waypoint classes for train travel planning.
A Route is the train's travel plan - a sequence of waypoints (platforms) the train
will visit autonomously. This is distinct from the dispatcher's Path (infrastructure
reservation).

The module provides:
- Route: Train's travel plan with waypoints (supports multi-train)
- Waypoint: Single stop in a Route (targets platform_id)
- TimingMode: NONE, RELATIVE, ABSOLUTE
- RouteMode: ONE_OFF, CIRCULAR, ROUND_TRIP
- PlatformSide: LEFT, RIGHT, BOTH, EITHER
- TravelDirectionPolicy: PRESERVE, EITHER
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any

from enums import PlatformSide


class TimingMode(Enum):
    """Timing mode for waypoint departure."""

    NONE = "none"  # No timing, just dwell
    RELATIVE = "relative"  # Time since route started
    ABSOLUTE = "absolute"  # Synchronized simulation time


class RouteMode(Enum):
    """Route traversal mode."""

    ONE_OFF = "one_off"  # A -> B -> C -> stop
    CIRCULAR = "circular"  # A -> B -> C -> A -> B -> C -> ...
    ROUND_TRIP = "round_trip"  # A -> B -> C -> B -> A -> B -> ...


class TravelDirectionPolicy(Enum):
    """Policy for train approach direction to platform."""

    PRESERVE = "preserve"  # Must approach from same direction as previous
    EITHER = "either"  # Can approach from any direction


@dataclass
class Waypoint:
    """
    Single stop or pass-through point in a train's Route.

    Represents a planned stop at a platform with dwell time, optional
    timing constraints, and flexible platform routing options.

    Attributes:
        platform_id: Target platform ID (required)
        dwell_time_sec: Stop duration in seconds (0 for pass-through)
        flexible_platform: Allow routing to any compatible platform in same Station
        required_platform_side: Side constraint for flexible selection
        travel_direction_policy: Approach direction constraint
        timing_mode: How timing is applied (NONE, RELATIVE, ABSOLUTE)
        target_time: For RELATIVE/ABSOLUTE modes (seconds)
    """

    platform_id: str
    dwell_time_sec: float = 30.0
    flexible_platform: bool = False
    required_platform_side: PlatformSide = PlatformSide.EITHER
    travel_direction_policy: TravelDirectionPolicy = TravelDirectionPolicy.EITHER
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None

    def should_depart(
        self, time_at_platform: float, route_start_time: float, simulation_time: float
    ) -> bool:
        """
        Determine if train should depart based on timing mode.

        Args:
            time_at_platform: Seconds since train arrived at this platform
            route_start_time: Simulation time when route was activated
            simulation_time: Current simulation time

        Returns:
            True if train should depart, False if it should wait
        """
        if self.timing_mode == TimingMode.NONE:
            return time_at_platform >= self.dwell_time_sec

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return time_at_platform >= self.dwell_time_sec
            elapsed = simulation_time - route_start_time
            return elapsed >= self.target_time

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return time_at_platform >= self.dwell_time_sec
            return simulation_time >= self.target_time

        return False

    def get_remaining_dwell_time(
        self, time_at_platform: float, route_start_time: float, simulation_time: float
    ) -> float:
        """
        Get remaining time before departure.

        Args:
            time_at_platform: Seconds since train arrived at this platform
            route_start_time: Simulation time when route was activated
            simulation_time: Current simulation time

        Returns:
            Remaining time in seconds (0 if should depart)
        """
        if self.timing_mode == TimingMode.NONE:
            return max(0.0, self.dwell_time_sec - time_at_platform)

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_platform)
            elapsed = simulation_time - route_start_time
            return max(0.0, self.target_time - elapsed)

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_platform)
            return max(0.0, self.target_time - simulation_time)

        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "platform_id": self.platform_id,
            "dwell_time_sec": self.dwell_time_sec,
            "flexible_platform": self.flexible_platform,
            "required_platform_side": self.required_platform_side.value,
            "travel_direction_policy": self.travel_direction_policy.value,
            "timing_mode": self.timing_mode.value,
            "target_time": self.target_time,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Waypoint":
        """Reconstruct from dictionary after deserialization."""
        timing_mode_str = data.get("timing_mode", "none")
        try:
            timing_mode = TimingMode(timing_mode_str)
        except ValueError:
            timing_mode = TimingMode.NONE

        platform_id = data.get("platform_id", "")

        return cls(
            platform_id=platform_id,
            dwell_time_sec=float(data.get("dwell_time_sec", 30.0)),
            flexible_platform=bool(data.get("flexible_platform", False)),
            required_platform_side=PlatformSide(
                data.get("required_platform_side", "either")
            ),
            travel_direction_policy=TravelDirectionPolicy(
                data.get("travel_direction_policy", "either")
            ),
            timing_mode=timing_mode,
            target_time=data.get("target_time"),
        )


@dataclass
class Route:
    """
    Train's travel plan - sequence of waypoints to visit.

    A Route defines WHERE trains should go and HOW they traverse the waypoints.
    Execution state (current waypoint, direction) is stored per-train in
    RouteExecutionState, enabling multiple trains to share one Route.

    Attributes:
        route_id: Unique identifier
        name: Display name
        waypoints: Ordered list of stops
        mode: How to traverse waypoints (ONE_OFF, CIRCULAR, ROUND_TRIP)
        assigned_train_ids: Trains currently assigned to this route
        is_active: Whether route is being executed
        started_at: Simulation time when activated
        last_error: Last error message
        error_time: When error occurred
    """

    route_id: str
    name: str = ""
    waypoints: List[Waypoint] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_OFF

    # Assignment (supports multi-train routes)
    assigned_train_ids: List[str] = field(default_factory=list)

    # Activation state
    is_active: bool = False
    started_at: Optional[float] = None

    # Error tracking
    last_error: Optional[str] = None
    error_time: Optional[float] = None

    # Backward-compatible execution fields (runtime only)
    current_waypoint_index: int = 0
    direction: int = 1

    # ── Definition Methods ─────────────────────────────────────────

    def add_waypoint(self, waypoint: Waypoint) -> None:
        """Add a waypoint to the end of the route."""
        self.waypoints.append(waypoint)

    def insert_waypoint(self, index: int, waypoint: Waypoint) -> None:
        """Insert a waypoint at a specific position."""
        self.waypoints.insert(index, waypoint)

    def remove_waypoint(self, index: int) -> Optional[Waypoint]:
        """Remove a waypoint at a specific position."""
        if 0 <= index < len(self.waypoints):
            return self.waypoints.pop(index)
        return None

    def get_waypoint_count(self) -> int:
        """Get total number of waypoints."""
        return len(self.waypoints)

    def get_waypoint_at(self, index: int) -> Optional[Waypoint]:
        """Get waypoint at index without any execution state dependency."""
        if 0 <= index < len(self.waypoints):
            return self.waypoints[index]
        return None

    # ── Activation Methods ─────────────────────────────────────────

    def activate(self, simulation_time: float) -> None:
        """
        Activate the route for execution.

        Note: Does NOT reset per-train execution state - that is managed
        by RouteExecutor when it detects a newly active route.

        Args:
            simulation_time: Current simulation time
        """
        self.is_active = True
        self.started_at = simulation_time
        self.last_error = None
        self.error_time = None

    def deactivate(self) -> None:
        """Deactivate the route (pause execution)."""
        self.is_active = False

    # ── Assignment Methods ─────────────────────────────────────────

    def assign_train(self, train_id: str) -> bool:
        """
        Assign a train to this route.

        Args:
            train_id: Train to assign

        Returns:
            True if added, False if already assigned
        """
        if train_id in self.assigned_train_ids:
            return False
        self.assigned_train_ids.append(train_id)
        return True

    def unassign_train(self, train_id: str) -> bool:
        """
        Remove a train from this route.

        Args:
            train_id: Train to remove

        Returns:
            True if removed, False if not found
        """
        if train_id not in self.assigned_train_ids:
            return False
        self.assigned_train_ids.remove(train_id)
        return True

    def is_train_assigned(self, train_id: str) -> bool:
        """Check if train is assigned to this route."""
        return train_id in self.assigned_train_ids

    def get_assigned_train_count(self) -> int:
        """Get number of trains assigned to this route."""
        return len(self.assigned_train_ids)

    # ── Progress (requires external waypoint_index) ─────────────────

    def get_progress(self, waypoint_index: int) -> float:
        """
        Get route progress as fraction (0.0 to 1.0) for ONE_OFF mode.

        Args:
            waypoint_index: Current waypoint index (from RouteExecutionState)

        Returns:
            Progress fraction (0.0 = start, 1.0 = complete)
        """
        if self.mode != RouteMode.ONE_OFF or not self.waypoints:
            return 0.0
        return min(1.0, waypoint_index / len(self.waypoints))

    # Backward-compatible helpers used by legacy execution paths

    def get_current_waypoint(self) -> Optional[Waypoint]:
        """Get current waypoint using legacy route-level index."""
        return self.get_waypoint_at(self.current_waypoint_index)

    def advance_to_next_waypoint(self) -> Optional[Waypoint]:
        """Advance legacy route-level waypoint pointer."""
        if not self.waypoints:
            return None

        if self.mode == RouteMode.ONE_OFF:
            self.current_waypoint_index += 1
            if self.current_waypoint_index >= len(self.waypoints):
                return None

        elif self.mode == RouteMode.CIRCULAR:
            self.current_waypoint_index += 1
            if self.current_waypoint_index >= len(self.waypoints):
                self.current_waypoint_index = 0

        elif self.mode == RouteMode.ROUND_TRIP:
            self.current_waypoint_index += self.direction
            if self.current_waypoint_index >= len(self.waypoints):
                self.direction = -1
                self.current_waypoint_index = max(0, len(self.waypoints) - 2)
            elif self.current_waypoint_index < 0:
                self.direction = 1
                self.current_waypoint_index = 0 if len(self.waypoints) == 1 else 1

        return self.get_waypoint_at(self.current_waypoint_index)

    # ── Error Methods ──────────────────────────────────────────────

    def set_error(self, message: str, simulation_time: float) -> None:
        """Record an execution error."""
        self.last_error = message
        self.error_time = simulation_time

    def clear_error(self) -> None:
        """Clear any recorded error."""
        self.last_error = None
        self.error_time = None

    # ── Serialization ──────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "route_id": self.route_id,
            "name": self.name,
            "waypoints": [wp.to_dict() for wp in self.waypoints],
            "mode": self.mode.value,
            "assigned_train_ids": list(self.assigned_train_ids),
            "is_active": self.is_active,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "error_time": self.error_time,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Route":
        """Reconstruct from dictionary after deserialization."""
        mode_str = data.get("mode", "one_off")
        try:
            mode = RouteMode(mode_str)
        except ValueError:
            # Migration: old route mode names
            mode_map = {
                "one_way": RouteMode.ONE_OFF,
                "loop": RouteMode.CIRCULAR,
            }
            mode = mode_map.get(mode_str, RouteMode.ONE_OFF)

        waypoints = [
            Waypoint.from_dict(wp_data) for wp_data in data.get("waypoints", [])
        ]

        return cls(
            route_id=data.get("route_id", ""),
            name=data.get("name", ""),
            waypoints=waypoints,
            mode=mode,
            assigned_train_ids=list(data.get("assigned_train_ids", [])),
            is_active=bool(data.get("is_active", False)),
            started_at=data.get("started_at"),
            last_error=data.get("last_error"),
            error_time=data.get("error_time"),
        )
