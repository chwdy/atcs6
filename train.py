"""
Train and Schedule Management

Definition:
This module defines train entities and their scheduling system. It includes train properties,
operational status, and time-based scheduling with stops and routing information.

Train Position System:
- TrainPosition tracks both ends of train with precise tile + distance_cm
- end_a and end_b are independent physical endpoints
- leading_end indicates which end is currently "front" (swaps on reverse)
- occupied_tiles lists all tiles the train covers from end_a to end_b
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Literal, Dict, Any
from datetime import datetime
from enums import TrainState, Direction, OPPOSITE_DIRECTIONS


@dataclass
class TrainPosition:
    """
    Complete train position tracking with both ends and occupied tiles.

    Both ends (end_a and end_b) have independent, precise positions.
    When train reverses, positions don't move - only leading_end swaps.

    Attributes:
        end_a_tile: One end of train (x, y) coordinates
        end_a_distance_cm: Distance from entry edge to end_a on that tile
        end_a_entry_edge: Which edge end_a entered from (N/E/S/W)
        end_b_tile: Other end of train (x, y) coordinates
        end_b_distance_cm: Distance from entry edge to end_b on that tile
        end_b_entry_edge: Which edge end_b entered from (N/E/S/W)
        leading_end: Which end is currently front ("a" or "b")
        facing_direction: Direction the train is facing
        occupied_tiles: Ordered list of tiles from end_a to end_b
        occupied_blocks: List of block IDs the train occupies
    """

    # End A position (precise)
    end_a_tile: Tuple[int, int]
    end_a_distance_cm: float
    end_a_entry_edge: str

    # End B position (precise)
    end_b_tile: Tuple[int, int]
    end_b_distance_cm: float
    end_b_entry_edge: str

    # Which end is leading (front of travel direction)
    leading_end: str = "a"  # "a" or "b"

    # Direction train is facing
    facing_direction: Optional[Direction] = None

    # Tiles occupied (ordered from end_a to end_b)
    occupied_tiles: List[Tuple[int, int]] = field(default_factory=list)

    # Blocks occupied
    occupied_blocks: List[str] = field(default_factory=list)

    @property
    def head_tile(self) -> Tuple[int, int]:
        """Get tile of leading end (head)."""
        return self.end_a_tile if self.leading_end == "a" else self.end_b_tile

    @property
    def head_distance_cm(self) -> float:
        """Get distance on tile for leading end (head)."""
        return (
            self.end_a_distance_cm
            if self.leading_end == "a"
            else self.end_b_distance_cm
        )

    @property
    def head_entry_edge(self) -> str:
        """Get entry edge for leading end (head)."""
        return (
            self.end_a_entry_edge if self.leading_end == "a" else self.end_b_entry_edge
        )

    @property
    def tail_tile(self) -> Tuple[int, int]:
        """Get tile of trailing end (tail)."""
        return self.end_b_tile if self.leading_end == "a" else self.end_a_tile

    @property
    def tail_distance_cm(self) -> float:
        """Get distance on tile for trailing end (tail)."""
        return (
            self.end_b_distance_cm
            if self.leading_end == "a"
            else self.end_a_distance_cm
        )

    @property
    def tail_entry_edge(self) -> str:
        """Get entry edge for trailing end (tail)."""
        return (
            self.end_b_entry_edge if self.leading_end == "a" else self.end_a_entry_edge
        )

    def reverse(self) -> None:
        """Swap leading end (train reverses direction)."""
        self.leading_end = "b" if self.leading_end == "a" else "a"
        if self.facing_direction:
            self.facing_direction = self.facing_direction.opposite()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "end_a": {
                "tile": list(self.end_a_tile),
                "distance_cm": self.end_a_distance_cm,
                "entry_edge": self.end_a_entry_edge,
            },
            "end_b": {
                "tile": list(self.end_b_tile),
                "distance_cm": self.end_b_distance_cm,
                "entry_edge": self.end_b_entry_edge,
            },
            "leading_end": self.leading_end,
            "facing_direction": self.facing_direction.name
            if self.facing_direction
            else None,
            "occupied_tiles": [list(t) for t in self.occupied_tiles],
            "occupied_blocks": self.occupied_blocks.copy(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["TrainPosition"]:
        """Reconstruct from dictionary. Returns None if data is invalid/incompatible."""
        try:
            if not isinstance(data, dict):
                return None

            end_a = data.get("end_a", {})
            end_b = data.get("end_b", {})

            facing_dir = None
            if data.get("facing_direction"):
                facing_dir = Direction[data["facing_direction"]]

            return cls(
                end_a_tile=tuple(end_a.get("tile", [0, 0])),
                end_a_distance_cm=float(end_a.get("distance_cm", 0.0)),
                end_a_entry_edge=end_a.get("entry_edge", "W"),
                end_b_tile=tuple(end_b.get("tile", [0, 0])),
                end_b_distance_cm=float(end_b.get("distance_cm", 0.0)),
                end_b_entry_edge=end_b.get("entry_edge", "W"),
                leading_end=data.get("leading_end", "a"),
                facing_direction=facing_dir,
                occupied_tiles=[tuple(t) for t in data.get("occupied_tiles", [])],
                occupied_blocks=data.get("occupied_blocks", []),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return None  # Incompatible format - discard train position


@dataclass
class ScheduleStop:
    """
    Individual stop in a train's schedule.

    Represents a planned stop at a station with arrival and departure times.
    """

    station_pos: Tuple[int, int]
    station_name: str
    arrival_time: datetime
    departure_time: datetime
    platform: Optional[str] = None

    def get_dwell_time(self) -> float:
        """Get dwell time in minutes"""
        return (self.departure_time - self.arrival_time).total_seconds() / 60

    def is_arrival_time_passed(self, current_time: datetime) -> bool:
        """Check if scheduled arrival time has passed"""
        return current_time > self.arrival_time

    def is_departure_time_passed(self, current_time: datetime) -> bool:
        """Check if scheduled departure time has passed"""
        return current_time > self.departure_time


@dataclass
class TrainSchedule:
    """
    Complete schedule for a train including all stops and timing.

    Manages the planned journey of a train with multiple stops,
    timing requirements, and priority levels.
    """

    train_id: str
    schedule_id: str
    stops: List[ScheduleStop] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    priority: int = 0  # Higher number = higher priority

    def get_current_target(self, current_time: datetime) -> Optional[ScheduleStop]:
        """Get the next scheduled stop based on current time"""
        for stop in self.stops:
            if current_time < stop.arrival_time:
                return stop
        return None

    def is_delayed(self, current_time: datetime, current_pos: Tuple[int, int]) -> bool:
        """Check if train is currently delayed"""
        target = self.get_current_target(current_time)
        if target and target.station_pos == current_pos:
            return current_time > target.arrival_time
        return False

    def get_delay_minutes(
        self, current_time: datetime, target_stop: ScheduleStop
    ) -> float:
        """Calculate delay in minutes for a specific stop"""
        if current_time > target_stop.arrival_time:
            delay = current_time - target_stop.arrival_time
            return delay.total_seconds() / 60
        return 0

    def add_stop(self, stop: ScheduleStop):
        """Add a new stop to the schedule"""
        self.stops.append(stop)
        self.stops.sort(key=lambda s: s.arrival_time)


@dataclass
class TrainCar:
    """
    Individual train car/wagon.
    """

    car_id: str
    car_type: str  # "LOCOMOTIVE", "PASSENGER", "FREIGHT"
    length: float  # Length in cm
    max_speed: float = 120.0  # Max speed for this car


@dataclass
class Train:
    """
    Railway train with physical properties and operational state.

    Represents a complete train with its current position, movement capabilities,
    operational status, and assigned schedule.

    Position System:
    - position: TrainPosition with both ends tracked precisely (CURRENT SYSTEM)

    New in Phase 1:
    - model: Train model name (e.g., "BR 218", "Shinkansen E5")
    - min_radius_cm: Minimum curve radius train can navigate
    - facing_direction: Which cab is active (flips on turn-around)
    - travel_direction: 'forward' or 'reverse' (only for MANUAL mode)
    - state: TrainState for DC analog control
    - speed_profile_id: SpeedProfile for this train
    - assigned_route_id: Route (travel plan) assigned to train
    - target_speed: ATC-limited target speed
    - manual_target_speed: User-set speed in MANUAL mode
    """

    train_id: str
    # length: float  # Now derived from consist
    # min_curve_radius: float  # Now derived from consist

    consist: List[TrainCar] = field(default_factory=list)

    max_speed: float = 100.0  # Operational max speed (min of cars)
    current_speed: float = 0.0  # Current speed in km/h
    acceleration: float = 2.0  # Acceleration in m/s²

    # Number of cars in train (for display purposes)
    num_cars: int = 1  # Number of cars/wagons in train consist

    # Priority (for dispatcher system)
    priority: int = 5  # 1-10, higher = higher priority, default 5

    # Braking
    max_braking_rate: float = 3.0  # Maximum braking deceleration in m/s²
    is_emergency_braking: bool = False  # True if currently emergency braking

    # Energy metrics
    energy_consumed: float = 0.0  # kWh
    efficiency: float = 0.85  # Motor efficiency

    # ========================================================================
    # NEW POSITION SYSTEM - TrainPosition with both ends tracked
    # ========================================================================
    position: Optional[TrainPosition] = None

    # Schedule management
    schedule: Optional[TrainSchedule] = None

    # ========================================================================
    # NAVIGATION PATH - Where the train is going
    # ========================================================================
    # current_path: Full planned route from pathfinder (head tile to destination)
    # - List of (x, y, direction) tuples ordered from current position to goal
    # - Used by: TrainMonitor (lookahead display), ATC (speed calculation),
    #            simulation (next tile decisions)
    # - Updated when: New route assigned, recalculated after reaching destination
    current_path: Optional[List[Tuple[int, int, Direction]]] = None

    # path_index: Current position in current_path (0 = first tile in path)
    # - Train head is on current_path[path_index]
    # - Tiles before path_index have been passed
    # - Tiles after path_index are upcoming (lookahead)
    path_index: int = 0

    # Block system integration
    reserved_blocks: List[str] = field(default_factory=list)

    # Path assignment (dispatcher system)
    # Note: "path" = dispatcher's reserved route; "route" = train's travel plan
    assigned_path_id: Optional[str] = None

    # ========================================================================
    # PATH-QUEUE POSITION SYSTEM (Legacy - for future path-based renderer)
    # ========================================================================
    # NOTE: This system is NOT currently active. The current renderer uses
    # TrainPosition for head/tail display. These fields are kept for future
    # path-queue based rendering.
    #
    # path_tiles: Flattened tile list for position queue (NOT used for lookahead)
    path_tiles: List[Tuple[int, int]] = field(default_factory=list)

    # Two physical ends (don't change on reverse)
    end_a_index: int = 0  # Index into path_tiles
    end_a_distance_cm: float = 0.0  # Distance from entry edge on this tile
    end_a_entry_edge: Optional[str] = None  # 'N', 'E', 'S', 'W'

    end_b_index: int = 0
    end_b_distance_cm: float = 0.0
    end_b_entry_edge: Optional[str] = None

    # Which end is currently leading (flip on reverse)
    leading_end: str = "a"  # 'a' or 'b'

    # ========================================================================
    # NEW PROPERTIES (Phase 1 - Train System)
    # ========================================================================

    # Train model info
    model: str = ""  # Train model name (e.g., "BR 218", "Shinkansen E5")
    min_radius_cm: float = 30.0  # Minimum curve radius train can navigate

    # Direction control
    facing_direction: Optional[Direction] = None  # Which cab is active
    active_cab: Optional[Literal["A", "B"]] = None  # Active cab: A, B, or None (off)
    travel_direction: Literal["forward", "reverse"] = "forward"  # For MANUAL mode

    # DC analog control state
    state: TrainState = TrainState.OFF  # Current power/operational state
    wait_reason: Optional[str] = None

    # Speed profile and route
    speed_profile_id: Optional[str] = None  # SpeedProfile for this train
    assigned_route_id: Optional[str] = None  # Route (travel plan) assigned

    # Speed targets
    target_speed: float = 0.0  # ATC-limited target speed
    manual_target_speed: float = 0.0  # User-set speed in MANUAL mode
    speed_limit_reason: Optional[str] = None  # Reason for current speed limit

    # Emergency control
    emergency_brake: bool = False  # Emergency braking active

    @property
    def length(self) -> float:
        """Total length of train in meters (approx track units for now)"""
        # Assuming 1 track unit = 100m
        total_meters = sum(car.length for car in self.consist)
        return total_meters / 100.0 if self.consist else 1.0

    @property
    def length_cm(self) -> float:
        """Total length of train in centimeters."""
        return sum(car.length for car in self.consist) if self.consist else 50.0

    # Note: Mass-based calculations removed - using simplified physics model

    @property
    def braking_distance(self) -> float:
        """Calculate required braking distance for current speed"""
        # v^2 = u^2 + 2as -> s = (v^2 - u^2) / 2a
        # Assuming deceleration is same magnitude as acceleration for now
        # Convert speed from km/h to m/s
        v_ms = self.current_speed / 3.6
        if self.acceleration == 0:
            return 0
        return (v_ms * v_ms) / (2 * self.acceleration)

    # ========================================================================
    # PATH POSITION PROPERTIES (Task 1.1)
    # ========================================================================

    @property
    def head_index(self) -> int:
        """Index of head tile in path_tiles."""
        return self.end_a_index if self.leading_end == "a" else self.end_b_index

    @property
    def tail_index(self) -> int:
        """Index of tail tile in path_tiles."""
        return self.end_b_index if self.leading_end == "a" else self.end_a_index

    @property
    def head_distance_cm(self) -> float:
        """Distance from entry edge on head tile."""
        return (
            self.end_a_distance_cm
            if self.leading_end == "a"
            else self.end_b_distance_cm
        )

    @head_distance_cm.setter
    def head_distance_cm(self, value: float) -> None:
        if self.leading_end == "a":
            self.end_a_distance_cm = value
        else:
            self.end_b_distance_cm = value

    @property
    def tail_distance_cm(self) -> float:
        """Distance from entry edge on tail tile."""
        return (
            self.end_b_distance_cm
            if self.leading_end == "a"
            else self.end_a_distance_cm
        )

    @tail_distance_cm.setter
    def tail_distance_cm(self, value: float) -> None:
        if self.leading_end == "a":
            self.end_b_distance_cm = value
        else:
            self.end_a_distance_cm = value

    @property
    def head_entry_edge(self) -> Optional[str]:
        """Entry edge for head tile."""
        return (
            self.end_a_entry_edge if self.leading_end == "a" else self.end_b_entry_edge
        )

    @head_entry_edge.setter
    def head_entry_edge(self, value: Optional[str]) -> None:
        if self.leading_end == "a":
            self.end_a_entry_edge = value
        else:
            self.end_b_entry_edge = value

    @property
    def tail_entry_edge(self) -> Optional[str]:
        """Entry edge for tail tile."""
        return (
            self.end_b_entry_edge if self.leading_end == "a" else self.end_a_entry_edge
        )

    @tail_entry_edge.setter
    def tail_entry_edge(self, value: Optional[str]) -> None:
        if self.leading_end == "a":
            self.end_b_entry_edge = value
        else:
            self.end_a_entry_edge = value

    @property
    def occupied_tiles(self) -> List[Tuple[int, int]]:
        """Tiles currently occupied by train (tail to head)."""
        if not self.path_tiles:
            return []
        start = min(self.tail_index, self.head_index)
        end = max(self.tail_index, self.head_index)
        return self.path_tiles[start : end + 1]

    def reverse_direction(self) -> None:
        """Flip which end is the head (no position recalculation needed)."""
        self.leading_end = "b" if self.leading_end == "a" else "a"

    # === PATH QUEUE METHODS ===

    def advance_head(self, new_entry_edge: str) -> bool:
        """Move head to next tile in path.

        Args:
            new_entry_edge: Entry edge for the new tile ('N', 'E', 'S', 'W')

        Returns:
            True if advanced, False if at end of path
        """
        if self.leading_end == "a":
            if self.end_a_index + 1 >= len(self.path_tiles):
                return False
            self.end_a_index += 1
            self.end_a_entry_edge = new_entry_edge
            self.end_a_distance_cm = 0.0
        else:
            if self.end_b_index + 1 >= len(self.path_tiles):
                return False
            self.end_b_index += 1
            self.end_b_entry_edge = new_entry_edge
            self.end_b_distance_cm = 0.0
        return True

    def advance_tail(self, new_entry_edge: str) -> bool:
        """Move tail to next tile (called when tail leaves a tile).

        Args:
            new_entry_edge: Entry edge for the new tile

        Returns:
            True if advanced, False if tail would pass head
        """
        if self.leading_end == "a":
            if self.end_b_index + 1 > self.end_a_index:
                return False
            self.end_b_index += 1
            self.end_b_entry_edge = new_entry_edge
            self.end_b_distance_cm = 0.0
        else:
            if self.end_a_index + 1 > self.end_b_index:
                return False
            self.end_a_index += 1
            self.end_a_entry_edge = new_entry_edge
            self.end_a_distance_cm = 0.0
        return True

    def extend_path(self, tiles: List[Tuple[int, int]]) -> None:
        """Append tiles to the path (when path is extended).

        Args:
            tiles: List of (x, y) positions to append
        """
        self.path_tiles.extend(tiles)

    def clear_path(self) -> None:
        """Clear path and reset all position data (when path is cancelled)."""
        self.path_tiles = []
        self.end_a_index = 0
        self.end_b_index = 0
        self.end_a_distance_cm = 0.0
        self.end_b_distance_cm = 0.0
        self.end_a_entry_edge = None
        self.end_b_entry_edge = None

    def is_at_station(self, station_pos: Tuple[int, int]) -> bool:
        """Check if train is currently at a specific station"""
        return (
            self.position is not None
            and self.position.head_tile == station_pos
            and self.state == TrainState.RUNNING_AT_STATION
        )

    def is_moving(self) -> bool:
        """Check if train is currently in motion"""
        return (
            self.state in (TrainState.RUNNING_MOVING, TrainState.MANUAL)
            and self.current_speed > 0
        )

    def is_delayed(self, current_time: datetime) -> bool:
        """Check if train is delayed according to its schedule"""
        if not self.schedule or not self.position:
            return False
        return self.schedule.is_delayed(current_time, self.position.head_tile)

    def get_next_scheduled_stop(self, current_time: datetime) -> Optional[ScheduleStop]:
        """Get the next stop in the train's schedule"""
        if self.schedule:
            return self.schedule.get_current_target(current_time)
        return None

    def has_path(self) -> bool:
        """Check if train has a planned path"""
        return self.current_path is not None and len(self.current_path) > 0

    def get_remaining_path_length(self) -> int:
        """Get number of remaining steps in current path"""
        if not self.has_path():
            return 0
        return len(self.current_path) - self.path_index

    # ========================================================================
    # Emergency Stop and Braking (Dispatcher System)
    # ========================================================================

    def initiate_emergency_stop(self) -> None:
        """
        Begin emergency braking.

        Sets the train to emergency braking mode, which will cause it to
        decelerate at maximum rate until stopped. After stopping, the train
        status becomes IDLE.
        """
        self.is_emergency_braking = True
        if self.state == TrainState.RUNNING_MOVING:
            self.state = TrainState.RUNNING_WAITING
            self.wait_reason = "Emergency stop"
        else:
            self.wait_reason = "Emergency stop"

    def get_braking_distance(self) -> float:
        """
        Calculate current braking distance based on speed and deceleration.

        Uses physics formula: d = v² / (2 * a)

        Returns:
            Braking distance in meters
        """
        if self.current_speed <= 0:
            return 0.0

        speed_ms = self.current_speed / 3.6  # km/h to m/s
        deceleration = (
            self.max_braking_rate if self.max_braking_rate > 0 else self.acceleration
        )

        if deceleration <= 0:
            return float("inf")

        return (speed_ms**2) / (2 * deceleration)

    def get_braking_distance_cm(self) -> float:
        """
        Calculate current braking distance in centimeters.

        Returns:
            Braking distance in centimeters
        """
        return self.get_braking_distance() * 100.0

    def update_emergency_braking(self, delta_time: float) -> bool:
        """
        Update train during emergency braking.

        Args:
            delta_time: Time step in seconds

        Returns:
            True if still braking, False if stopped
        """
        if not self.is_emergency_braking:
            return False

        # Decelerate at maximum rate
        decel_rate = self.max_braking_rate * 3.6  # m/s² to km/h per second
        self.current_speed -= decel_rate * delta_time

        if self.current_speed <= 0:
            self.current_speed = 0.0
            self.is_emergency_braking = False
            self.state = TrainState.IDLE
            return False

        return True

    # ========================================================================
    # NEW METHODS (Phase 1 - Train System)
    # ========================================================================

    # Threshold for considering train stopped (km/h)
    STOPPED_SPEED_THRESHOLD_KMH: float = 0.1

    def turn_around(self) -> bool:
        """
        Flip facing direction (cab switch). Only allowed when stopped.

        This is like the driver switching to the other cab in a real train.
        The train now faces the opposite direction.

        Note: travel_direction stays 'forward' - only used in MANUAL mode
        for explicit reverse driving.

        Returns:
            True if successful, False if train is moving or no facing direction
        """
        if self.current_speed > self.STOPPED_SPEED_THRESHOLD_KMH:
            return False

        if self.facing_direction is None:
            return False

        # Use the OPPOSITE_DIRECTIONS mapping from enums
        # facing_direction is a Direction enum, need to get opposite
        self.facing_direction = self.facing_direction.opposite()

        return True

    def get_effective_direction(self) -> Optional[Direction]:
        """
        Get actual movement direction considering facing and travel.

        In normal operation (travel_direction='forward'), this equals facing_direction.
        In MANUAL reverse mode, this is the opposite of facing_direction.

        Returns:
            Effective movement direction, or None if no facing direction set
        """
        if self.facing_direction is None:
            return None

        if self.travel_direction == "forward":
            return self.facing_direction
        else:
            return self.facing_direction.opposite()

    def is_stopped(self) -> bool:
        """Check if train is effectively stopped."""
        return self.current_speed <= self.STOPPED_SPEED_THRESHOLD_KMH

    def power_on(self) -> bool:
        """
        Power on the train (OFF -> IDLE).

        Returns:
            True if successful, False if already on
        """
        if self.state != TrainState.OFF:
            return False
        self.state = TrainState.IDLE
        return True

    def power_off(self) -> bool:
        """
        Power off the train (any state -> OFF). Must be stopped.

        Returns:
            True if successful, False if train is moving
        """
        if not self.is_stopped():
            return False
        self.state = TrainState.OFF
        self.target_speed = 0.0
        self.manual_target_speed = 0.0
        return True

    def set_standby(self, direction: Direction) -> bool:
        """
        Set train to standby with specified facing direction.

        Transitions from IDLE to STANDBY.

        Args:
            direction: Facing direction for the train

        Returns:
            True if successful, False if not in IDLE state
        """
        if self.state != TrainState.IDLE:
            return False
        self.facing_direction = direction
        self.state = TrainState.STANDBY
        return True

    def clear_direction(self) -> bool:
        """
        Clear facing direction and return to IDLE state (STANDBY -> IDLE).

        Allowed only when stopped and in STANDBY (or MANUAL when stopped).
        Resets facing_direction to None, travel_direction to 'forward',
        and speed targets to 0.

        Returns:
            True if successful, False if not allowed
        """
        if not self.is_stopped():
            return False

        if self.state not in (TrainState.STANDBY, TrainState.MANUAL):
            return False

        self.facing_direction = None
        self.travel_direction = "forward"
        self.manual_target_speed = 0.0
        self.target_speed = 0.0
        self.state = TrainState.IDLE
        return True

    def enable_manual(self) -> bool:
        """
        Enable manual control mode. Must be stopped.

        Returns:
            True if successful, False if moving
        """
        if not self.is_stopped():
            return False
        if self.state == TrainState.OFF:
            return False
        self.state = TrainState.MANUAL
        return True

    def disable_manual(self) -> bool:
        """
        Disable manual control mode. Must be stopped.

        Returns to STANDBY if facing_direction set, else IDLE.

        Returns:
            True if successful, False if not in MANUAL mode or moving
        """
        if self.state != TrainState.MANUAL:
            return False
        if not self.is_stopped():
            return False

        if self.facing_direction is not None:
            self.state = TrainState.STANDBY
        else:
            self.state = TrainState.IDLE

        self.manual_target_speed = 0.0
        return True

    def set_manual_speed(self, speed: float) -> bool:
        """
        Set manual target speed (only in MANUAL mode).

        Args:
            speed: Target speed in km/h

        Returns:
            True if successful, False if not in MANUAL mode
        """
        if self.state != TrainState.MANUAL:
            return False
        self.manual_target_speed = max(0.0, min(speed, self.max_speed))
        return True

    def set_travel_direction(self, direction: Literal["forward", "reverse"]) -> bool:
        """
        Set travel direction for MANUAL mode.

        This allows the user to drive the train in reverse (tail-first).
        In MANUAL reverse mode, the train moves opposite to facing_direction.

        Note: This only affects MANUAL mode. In normal operation,
        travel_direction is always 'forward'.

        Args:
            direction: 'forward' or 'reverse'

        Returns:
            True if successful, False if not in MANUAL mode
        """
        if self.state != TrainState.MANUAL:
            return False
        if direction not in ("forward", "reverse"):
            return False
        self.travel_direction = direction
        return True

    # ========================================================================
    # RUNNING STATE TRANSITIONS (Phase 3 - State Machine)
    # ========================================================================

    def depart(self) -> bool:
        """
        Start moving (STANDBY -> RUNNING_MOVING).

        Train must be in STANDBY state and have a facing direction set.

        Returns:
            True if successful, False if preconditions not met
        """
        if self.state != TrainState.STANDBY:
            return False
        if self.facing_direction is None:
            return False
        self.state = TrainState.RUNNING_MOVING
        self.wait_reason = None
        return True

    def wait_for_path(self, reason: Optional[str] = None) -> bool:
        """
        Stop and wait for path/signal (RUNNING_MOVING -> RUNNING_WAITING).

        Train must be in RUNNING_MOVING state.

        Returns:
            True if successful, False if not in correct state
        """
        if self.state != TrainState.RUNNING_MOVING:
            return False
        self.state = TrainState.RUNNING_WAITING
        self.target_speed = 0.0
        self.wait_reason = reason
        return True

    def clear_to_move(self) -> bool:
        """
        Resume movement (RUNNING_WAITING -> RUNNING_MOVING).

        Train must be in RUNNING_WAITING state.

        Returns:
            True if successful, False if not in correct state
        """
        if self.state != TrainState.RUNNING_WAITING:
            return False
        self.state = TrainState.RUNNING_MOVING
        self.wait_reason = None
        return True

    def arrive_at_station(self) -> bool:
        """
        Arrive at station (RUNNING_MOVING -> RUNNING_AT_STATION).

        Train must be in RUNNING_MOVING state.

        Returns:
            True if successful, False if not in correct state
        """
        if self.state != TrainState.RUNNING_MOVING:
            return False
        self.state = TrainState.RUNNING_AT_STATION
        self.target_speed = 0.0  # Stop at station
        self.wait_reason = None
        return True

    def depart_from_station(self, has_more_waypoints: bool = True) -> bool:
        """
        Depart from station after dwell time.

        If has_more_waypoints is True, transitions to STANDBY.
        If False (route complete), transitions to IDLE and clears route assignment.

        Args:
            has_more_waypoints: Whether route has more waypoints

        Returns:
            True if successful, False if not in correct state
        """
        if self.state != TrainState.RUNNING_AT_STATION:
            return False

        if has_more_waypoints:
            self.state = TrainState.STANDBY
        else:
            # Route complete - unassign route and go to IDLE
            self.state = TrainState.IDLE
            self.assigned_route_id = None
            self.target_speed = 0.0

        self.wait_reason = None
        return True

    def emergency_stop(self) -> bool:
        """
        Emergency stop from any operational state.

        Transitions to RUNNING_WAITING and sets speed to 0.

        Returns:
            True if successful
        """
        self.state = TrainState.RUNNING_WAITING
        self.target_speed = 0.0
        self.is_emergency_braking = True
        self.wait_reason = "Emergency stop"
        return True

    def stop(self) -> bool:
        """
        Controlled stop - bring train to standby state.

        This is the normal ATO stop that brings the train to a controlled stop
        and transitions to STANDBY, ready to depart again.

        Can only be called from running states (RUNNING_MOVING, RUNNING_WAITING).

        Returns:
            True if successful, False if train is not in a running state
        """
        if self.state not in (TrainState.RUNNING_MOVING, TrainState.RUNNING_WAITING):
            return False
        self.target_speed = 0.0
        self.current_speed = 0.0  # Immediately stop for now (simulation handles decel)
        self.state = TrainState.STANDBY
        self.wait_reason = None
        return True

    def set_direction_and_standby(self, direction: Direction) -> bool:
        """
        Set direction and transition to STANDBY (IDLE -> STANDBY).

        Convenience method that combines setting direction and transitioning.
        Also sets active_cab to "A" since this method implies Cab A is leading.

        Args:
            direction: Facing direction to set

        Returns:
            True if successful, False if not in IDLE state
        """
        if self.state != TrainState.IDLE:
            return False
        self.facing_direction = direction
        self.active_cab = "A"  # Setting direction implies Cab A is leading
        if self.position:
            self.position.leading_end = "a"
        self.state = TrainState.STANDBY
        return True

    def set_cab(self, cab: Literal["A", "B", None]) -> bool:
        """
        Set active cab and manage state transitions.

        Cab control logic:
        - Setting cab A or B: Auto power-on if OFF, transitions to STANDBY
        - Setting cab to None (off): Train must be STANDBY (stopped), transitions to IDLE
        - Cab A uses end_a as leading end
        - Cab B uses end_b as leading end
        - When switching cabs, ALL position data is swapped (tiles, distance, entry_edge)

        Args:
            cab: Cab to activate ("A", "B", or None for off)

        Returns:
            True if successful, False if state doesn't allow the transition
        """
        if cab in ("A", "B"):
            # Auto power-on if train is OFF
            if self.state == TrainState.OFF:
                self.power_on()

            # Setting a cab: must be IDLE or STANDBY (not moving)
            if self.state not in (TrainState.IDLE, TrainState.STANDBY):
                return False

            # Check if switching from other cab (need to swap position data)
            old_cab = self.active_cab
            self.active_cab = cab

            # Update leading end based on cab selection
            if self.position:
                new_leading_end = cab.lower()  # "A" -> "a", "B" -> "b"

                # If switching cabs (A to B or B to A), swap ALL position data
                # between end_a and end_b. This is because the physical train
                # doesn't move, but the designation of which end is A or B swaps.
                if old_cab and old_cab != cab:
                    # Swap tiles
                    temp_tile = self.position.end_a_tile
                    self.position.end_a_tile = self.position.end_b_tile
                    self.position.end_b_tile = temp_tile

                    # Swap distances
                    temp_distance = self.position.end_a_distance_cm
                    self.position.end_a_distance_cm = self.position.end_b_distance_cm
                    self.position.end_b_distance_cm = temp_distance

                    # Swap entry edges
                    temp_entry = self.position.end_a_entry_edge
                    self.position.end_a_entry_edge = self.position.end_b_entry_edge
                    self.position.end_b_entry_edge = temp_entry

                self.position.leading_end = new_leading_end

            self.state = TrainState.STANDBY
            return True
        elif cab is None:
            # Turning cab off: must be STANDBY (stopped, not moving)
            if self.state != TrainState.STANDBY:
                return False
            self.active_cab = None
            self.facing_direction = None
            self.state = TrainState.IDLE
            return True
        return False

    # ========================================================================
    # MULTI-BLOCK OCCUPANCY (Phase 6)
    # ========================================================================

    def get_occupied_blocks(self, network=None) -> List[str]:
        """
        Get all blocks this train occupies based on its length.

        For trains longer than one block, this traces back from the head
        position to determine all occupied blocks.

        Args:
            network: Railway network (required for multi-block calculation)

        Returns:
            List of block IDs from head to tail
        """
        if not self.position:
            return self.reserved_blocks if self.reserved_blocks else []

        if self.position.occupied_blocks:
            return list(self.position.occupied_blocks)

        if not network:
            return self.reserved_blocks if self.reserved_blocks else []

        occupied = []
        for tile in self.position.occupied_tiles:
            track = network.tracks.get(tile)
            if not track or not track.block_section_id:
                continue
            if track.block_section_id not in occupied:
                occupied.append(track.block_section_id)

        return occupied

    def is_opposite_polarity_safe(self, network=None) -> bool:
        """
        Check if opposite polarity is safe for IDLE state lighting.

        Opposite polarity (red lights both ends) requires ≥2 blocks and
        no cars bridging block boundaries (would cause short circuit).

        Simplified implementation: assumes single locomotive or cars don't bridge.
        Full implementation would check each car's position.

        Args:
            network: Railway network

        Returns:
            True if opposite polarity is safe to use
        """
        if not network:
            return False

        occupied_blocks = self.get_occupied_blocks(network)

        # Need at least 2 blocks for opposite polarity
        if len(occupied_blocks) < 2:
            return False

        # Simplified safety check:
        # If consist is empty or has only 1 car, it's safe
        # (single unit won't bridge blocks)
        if not self.consist or len(self.consist) <= 1:
            return True

        # For multi-car consists, we'd need to check if any car bridges blocks
        # For now, conservative approach: only allow for single-unit trains
        # TODO: Implement detailed car position checking
        return len(self.consist) == 1

    def to_dict(self, network=None) -> dict:
        """Convert train to dictionary for serialization.

        Args:
            network: Optional RailwayNetwork to calculate tile length for distance ratio
        """
        data = {
            "id": self.train_id,
            "length": self.length,
            "length_cm": self.length_cm,
            "speed": self.current_speed,
            "max_speed": self.max_speed,
            "num_cars": self.num_cars,
            "model": self.model,
            "min_radius_cm": self.min_radius_cm,
            "facing_direction": self.facing_direction.name
            if self.facing_direction
            else None,
            "active_cab": self.active_cab,
            "travel_direction": self.travel_direction,
            "state": self.state.value,
            "speed_profile_id": self.speed_profile_id,
            "assigned_route_id": self.assigned_route_id,
            "assigned_path_id": self.assigned_path_id,
            "target_speed": self.target_speed,
            "manual_target_speed": self.manual_target_speed,
            "wait_reason": self.wait_reason,
            "speed_limit_reason": self.speed_limit_reason,
            # Path info for debug
            "path_index": self.path_index,
            "path_length": len(self.current_path) if self.current_path else 0,
            # Current path as list of tiles with direction
            "current_path": [
                {"x": x, "y": y, "dir": d.name if d else None}
                for x, y, d in (self.current_path or [])
            ],
        }

        # Position data for rendering
        if self.position:
            pos = self.position
            data["position"] = pos.to_dict()
            data["path_tiles"] = [list(t) for t in pos.occupied_tiles]
            data["end_a_index"] = 0
            data["end_b_index"] = max(0, len(pos.occupied_tiles) - 1)
            data["end_a_distance_cm"] = pos.end_a_distance_cm
            data["end_b_distance_cm"] = pos.end_b_distance_cm
            data["end_a_entry_edge"] = pos.end_a_entry_edge
            data["end_b_entry_edge"] = pos.end_b_entry_edge
            data["leading_end"] = pos.leading_end

            if network:
                data["path_tile_info"] = [
                    {"length_cm": network._get_tile_length_cm(tile[0], tile[1])}
                    for tile in pos.occupied_tiles
                ]

            if network and hasattr(network, "get_occupied_tiles_rich"):
                data["occupied_tiles_rich"] = network.get_occupied_tiles_rich(self)

            if pos.facing_direction:
                data["direction"] = pos.facing_direction.name

        if self.schedule:
            # Simple schedule info
            data["schedule"] = {"id": self.schedule.schedule_id, "next_stop": None}
            # Try to get next stop
            # We need current time for this, but we don't have it here easily.
            # For now, just list stops
            stops = []
            for stop in self.schedule.stops:
                stops.append(
                    {
                        "station": stop.station_name,
                        "arrival": stop.arrival_time.isoformat(),
                        "departure": stop.departure_time.isoformat(),
                    }
                )
            data["schedule"]["stops"] = stops

        # ========================================================================
        # LOOKAHEAD TILE INFO - Used by TrainMonitor for upcoming elements
        # ========================================================================
        # Build lookahead_tiles from current_path starting at path_index (head position)
        # This provides info about ALL tiles ahead, not just occupied tiles
        #
        # Data structure:
        # - lookahead_tiles: List of (x, y) tiles from head forward along path
        # - lookahead_tile_info: Rich info for each tile (block, signals, switches, distance)
        # - head_distance_into_tile_cm: How far into current tile the head is
        #
        if network and self.current_path and self.path_index >= 0:
            lookahead_tiles = []
            lookahead_tile_info = []

            # Distance from train head to start of each tile
            # Starts negative (train head is already into first tile)
            head_distance_into_tile = (
                self.position.head_distance_cm if self.position else 0.0
            )
            cumulative_distance_cm = (
                -head_distance_into_tile
            )  # Start from head position

            # Iterate from current position to end of path
            for i in range(self.path_index, len(self.current_path)):
                path_entry = self.current_path[i]
                x, y = path_entry[0], path_entry[1]
                # path_entry[2] is the Direction enum for train's travel direction
                travel_direction = (
                    path_entry[2].name
                    if len(path_entry) > 2 and hasattr(path_entry[2], "name")
                    else None
                )
                lookahead_tiles.append([x, y])

                track = network.get_track(x, y)
                tile_length = network._get_tile_length_cm(x, y) if track else 50.0

                tile_info = {
                    "tile": [x, y],
                    "length_cm": tile_length,
                    "block_id": track.block_section_id if track else None,
                    # Distance from train head to START of this tile (cm)
                    # For first tile (head tile), this is negative
                    "distance_to_tile_cm": cumulative_distance_cm,
                    # Train's travel direction on this tile (N/E/S/W)
                    "travel_direction": travel_direction,
                }

                # Add block state if block exists
                if track and track.block_section_id:
                    block = network.blocks.get(track.block_section_id)
                    if block:
                        tile_info["block_state"] = block.state.value

                # Add signal info if present
                if track and track.signal_ids:
                    tile_info["signals"] = []
                    for sig_id in track.signal_ids:
                        signal = network.signals.get(sig_id)
                        if signal:
                            tile_info["signals"].append(
                                {
                                    "id": sig_id,
                                    "name": signal.name or sig_id,
                                    "state": signal.state.value
                                    if signal.state
                                    else "unknown",
                                    "direction": signal.detection_direction,
                                }
                            )

                # Add switch info if present
                if track and track.is_switch:
                    tile_info["is_switch"] = True
                    tile_info["switch_state"] = track.switch_state
                    tile_info["switch_locked"] = track.is_locked_by_path()
                    # Get switch name from switch entity if available
                    switch_entity = (
                        network.get_switch_at(x, y)
                        if hasattr(network, "get_switch_at")
                        else None
                    )
                    if switch_entity and switch_entity.name:
                        tile_info["switch_name"] = switch_entity.name
                    else:
                        tile_info["switch_name"] = f"Switch ({x},{y})"

                lookahead_tile_info.append(tile_info)

                # Move cumulative distance forward by this tile's length
                # After first tile, distance becomes positive (ahead of head)
                cumulative_distance_cm += tile_length

            # Export lookahead data
            data["lookahead_tiles"] = lookahead_tiles
            data["lookahead_tile_info"] = lookahead_tile_info
            data["head_distance_into_tile_cm"] = head_distance_into_tile

            # NOTE: Do NOT overwrite path_tiles here!
            # path_tiles is already set correctly to occupied_tiles (line 1361)
            # for the path-based train renderer.
            # lookahead data is separate and used by the train monitor.

        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Train":
        """
        Create a Train from a dictionary.

        Note: This reconstructs basic train state. Schedule and path
        will be None and need to be re-established if needed.

        Position handling:
        - Uses TrainPosition format (requires 'end_a' in position dict)
        - If position format is incompatible, train position is discarded
        """
        from enums import TrainState, Direction

        train = cls(
            train_id=data.get("id", ""),
            max_speed=data.get("max_speed", 100.0),
            current_speed=data.get("speed", 0.0),
            num_cars=int(data.get("num_cars", 1)),
        )

        # Use new TrainPosition format only
        if "position" in data and isinstance(data["position"], dict):
            pos_data = data["position"]
            if "end_a" in pos_data:
                # New TrainPosition format
                train.position = TrainPosition.from_dict(pos_data)
                # If parsing failed, position will be None (incompatible format)

        # ========================================================================
        # NEW PROPERTIES (Phase 1 - Train System)
        # ========================================================================

        # Train model info
        train.model = data.get("model", "")
        train.min_radius_cm = float(data.get("min_radius_cm", 30.0))

        # Facing direction
        if data.get("facing_direction"):
            try:
                train.facing_direction = Direction[data["facing_direction"]]
            except KeyError:
                pass

        # Travel direction
        travel_dir = data.get("travel_direction", "forward")
        if travel_dir in ("forward", "reverse"):
            train.travel_direction = travel_dir
        else:
            train.travel_direction = "forward"

        # State (DC analog control)
        if data.get("state"):
            try:
                train.state = TrainState(data["state"])
            except ValueError:
                train.state = TrainState.OFF

        # Speed profile and route
        train.speed_profile_id = data.get("speed_profile_id")
        train.assigned_route_id = data.get("assigned_route_id")

        # Speed targets
        train.target_speed = float(data.get("target_speed", 0.0))
        train.manual_target_speed = float(data.get("manual_target_speed", 0.0))
        train.wait_reason = data.get("wait_reason")
        train.speed_limit_reason = data.get("speed_limit_reason")

        # IMPORTANT: Clear path_tiles data from saved files
        # path_tiles is only used by the NEW path-queue movement system
        # TrainPosition is the current system for head/tail tracking
        train.path_tiles = []
        train.end_a_index = 0
        train.end_b_index = 0
        train.end_a_distance_cm = 0.0
        train.end_b_distance_cm = 0.0
        train.end_a_entry_edge = None
        train.end_b_entry_edge = None

        return train
