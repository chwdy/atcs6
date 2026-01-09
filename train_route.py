"""
Train Route Management

Definition:
This module defines the Route and Waypoint classes for train travel planning.
A Route is the train's travel plan - a sequence of waypoints (stations) the train
will visit autonomously. This is distinct from the dispatcher's Path (infrastructure
reservation).

The module provides:
- Route: Train's travel plan with waypoints
- Waypoint: Single stop in a Route
- TimingMode: NONE, RELATIVE, ABSOLUTE
- RouteMode: ONE_WAY, LOOP, ROUND_TRIP
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any


class TimingMode(Enum):
    """Timing mode for waypoint departure."""

    NONE = "none"  # No timing, just dwell
    RELATIVE = "relative"  # Time since route started
    ABSOLUTE = "absolute"  # Synchronized simulation time


class RouteMode(Enum):
    """Route traversal mode."""

    ONE_WAY = "one_way"  # A -> B -> C -> stop
    LOOP = "loop"  # A -> B -> C -> A -> B -> C -> ...
    ROUND_TRIP = "round_trip"  # A -> B -> C -> B -> A -> B -> ...


@dataclass
class Waypoint:
    """
    Single stop in a train's Route.

    Represents a planned stop at a station with dwell time and optional
    timing constraints for coordinated schedules.

    Attributes:
        station_id: Target station ID
        dwell_time_sec: Default stop duration in seconds
        timing_mode: How timing is applied (NONE, RELATIVE, ABSOLUTE)
        target_time: For ABSOLUTE/RELATIVE modes (seconds)
    """

    station_id: str
    dwell_time_sec: float = 30.0
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None  # Meaning depends on timing_mode

    def should_depart(
        self, time_at_station: float, route_start_time: float, simulation_time: float
    ) -> bool:
        """
        Determine if train should depart based on timing mode.

        Args:
            time_at_station: Seconds since train arrived at this station
            route_start_time: Simulation time when route was activated
            simulation_time: Current simulation time

        Returns:
            True if train should depart, False if it should wait
        """
        if self.timing_mode == TimingMode.NONE:
            return time_at_station >= self.dwell_time_sec

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return time_at_station >= self.dwell_time_sec
            elapsed = simulation_time - route_start_time
            return elapsed >= self.target_time

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return time_at_station >= self.dwell_time_sec
            return simulation_time >= self.target_time

        return False

    def get_remaining_dwell_time(
        self, time_at_station: float, route_start_time: float, simulation_time: float
    ) -> float:
        """
        Get remaining time before departure.

        Args:
            time_at_station: Seconds since train arrived at this station
            route_start_time: Simulation time when route was activated
            simulation_time: Current simulation time

        Returns:
            Remaining time in seconds (0 if should depart)
        """
        if self.timing_mode == TimingMode.NONE:
            return max(0.0, self.dwell_time_sec - time_at_station)

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_station)
            elapsed = simulation_time - route_start_time
            return max(0.0, self.target_time - elapsed)

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_station)
            return max(0.0, self.target_time - simulation_time)

        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "station_id": self.station_id,
            "dwell_time_sec": self.dwell_time_sec,
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

        return cls(
            station_id=data.get("station_id", ""),
            dwell_time_sec=float(data.get("dwell_time_sec", 30.0)),
            timing_mode=timing_mode,
            target_time=data.get("target_time"),
        )


@dataclass
class Route:
    """
    Train's travel plan - sequence of waypoints to visit.

    A Route defines where the train should go and how it should traverse
    the waypoints (one-way, loop, or round-trip). The Route tracks execution
    state including current waypoint and direction.

    Attributes:
        route_id: Unique identifier
        name: Display name
        waypoints: Ordered list of stops
        mode: How to traverse waypoints
        current_waypoint_index: Current position in waypoints
        direction: +1 forward, -1 reverse (for ROUND_TRIP)
        is_active: Whether route is being executed
        started_at: Simulation time when activated
        last_error: Last error message (if path not found, etc.)
        error_time: Simulation time when error occurred
    """

    route_id: str
    name: str = ""
    waypoints: List[Waypoint] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_WAY

    # Execution state
    current_waypoint_index: int = 0
    direction: int = 1  # +1 forward, -1 reverse (for ROUND_TRIP)
    is_active: bool = False
    started_at: Optional[float] = None  # Simulation time when activated

    # Error tracking
    last_error: Optional[str] = None
    error_time: Optional[float] = None

    def get_current_waypoint(self) -> Optional[Waypoint]:
        """
        Get the current target waypoint.

        Returns:
            Current Waypoint or None if no valid waypoint
        """
        if 0 <= self.current_waypoint_index < len(self.waypoints):
            return self.waypoints[self.current_waypoint_index]
        return None

    def advance_to_next_waypoint(self) -> Optional[Waypoint]:
        """
        Move to next waypoint based on mode.

        Updates current_waypoint_index and direction (for ROUND_TRIP).

        Returns:
            New waypoint or None if route is complete (ONE_WAY only)
        """
        if not self.waypoints:
            return None

        if self.mode == RouteMode.ONE_WAY:
            self.current_waypoint_index += 1
            if self.current_waypoint_index >= len(self.waypoints):
                return None  # Route complete

        elif self.mode == RouteMode.LOOP:
            self.current_waypoint_index += 1
            if self.current_waypoint_index >= len(self.waypoints):
                self.current_waypoint_index = 0  # Wrap around

        elif self.mode == RouteMode.ROUND_TRIP:
            self.current_waypoint_index += self.direction

            # Bounce at ends
            if self.current_waypoint_index >= len(self.waypoints):
                self.direction = -1
                # Go to second-to-last (avoid revisiting end immediately)
                self.current_waypoint_index = len(self.waypoints) - 2
                if self.current_waypoint_index < 0:
                    self.current_waypoint_index = 0

            elif self.current_waypoint_index < 0:
                self.direction = 1
                # Go to second (avoid revisiting start immediately)
                self.current_waypoint_index = 1
                if self.current_waypoint_index >= len(self.waypoints):
                    self.current_waypoint_index = 0

        return self.get_current_waypoint()

    def is_complete(self) -> bool:
        """
        Check if route has completed (ONE_WAY only).

        LOOP and ROUND_TRIP routes never complete on their own.

        Returns:
            True if ONE_WAY route has finished all waypoints
        """
        if self.mode != RouteMode.ONE_WAY:
            return False
        return self.current_waypoint_index >= len(self.waypoints)

    def get_next_station_id(self) -> Optional[str]:
        """
        Get the next station the train should travel to.

        Returns:
            Station ID or None if route is complete
        """
        waypoint = self.get_current_waypoint()
        return waypoint.station_id if waypoint else None

    def activate(self, simulation_time: float) -> None:
        """
        Activate the route for execution.

        Args:
            simulation_time: Current simulation time
        """
        self.is_active = True
        self.started_at = simulation_time
        self.current_waypoint_index = 0
        self.direction = 1
        self.last_error = None
        self.error_time = None

    def deactivate(self) -> None:
        """Deactivate the route (pause execution)."""
        self.is_active = False

    def reset(self) -> None:
        """Reset route to initial state."""
        self.current_waypoint_index = 0
        self.direction = 1
        self.is_active = False
        self.started_at = None
        self.last_error = None
        self.error_time = None

    def set_error(self, message: str, simulation_time: float) -> None:
        """
        Record an execution error.

        Args:
            message: Error description
            simulation_time: When error occurred
        """
        self.last_error = message
        self.error_time = simulation_time

    def clear_error(self) -> None:
        """Clear any recorded error."""
        self.last_error = None
        self.error_time = None

    def get_waypoint_count(self) -> int:
        """Get total number of waypoints."""
        return len(self.waypoints)

    def get_progress(self) -> float:
        """
        Get route progress as fraction (0.0 to 1.0).

        For ONE_WAY routes only; LOOP and ROUND_TRIP return 0.

        Returns:
            Progress fraction (0.0 = start, 1.0 = complete)
        """
        if self.mode != RouteMode.ONE_WAY or not self.waypoints:
            return 0.0
        return min(1.0, self.current_waypoint_index / len(self.waypoints))

    def add_waypoint(self, waypoint: Waypoint) -> None:
        """
        Add a waypoint to the end of the route.

        Args:
            waypoint: Waypoint to add
        """
        self.waypoints.append(waypoint)

    def insert_waypoint(self, index: int, waypoint: Waypoint) -> None:
        """
        Insert a waypoint at a specific position.

        Args:
            index: Position to insert at
            waypoint: Waypoint to insert
        """
        self.waypoints.insert(index, waypoint)

    def remove_waypoint(self, index: int) -> Optional[Waypoint]:
        """
        Remove a waypoint at a specific position.

        Args:
            index: Position to remove from

        Returns:
            Removed waypoint or None if index invalid
        """
        if 0 <= index < len(self.waypoints):
            return self.waypoints.pop(index)
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "route_id": self.route_id,
            "name": self.name,
            "waypoints": [wp.to_dict() for wp in self.waypoints],
            "mode": self.mode.value,
            "current_waypoint_index": self.current_waypoint_index,
            "direction": self.direction,
            "is_active": self.is_active,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "error_time": self.error_time,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Route":
        """Reconstruct from dictionary after deserialization."""
        mode_str = data.get("mode", "one_way")
        try:
            mode = RouteMode(mode_str)
        except ValueError:
            mode = RouteMode.ONE_WAY

        waypoints = [
            Waypoint.from_dict(wp_data) for wp_data in data.get("waypoints", [])
        ]

        route = cls(
            route_id=data.get("route_id", ""),
            name=data.get("name", ""),
            waypoints=waypoints,
            mode=mode,
            current_waypoint_index=int(data.get("current_waypoint_index", 0)),
            direction=int(data.get("direction", 1)),
            is_active=bool(data.get("is_active", False)),
            started_at=data.get("started_at"),
            last_error=data.get("last_error"),
            error_time=data.get("error_time"),
        )

        return route
