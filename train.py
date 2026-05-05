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
from enums import TrainState, Direction, OPPOSITE_DIRECTIONS, LeadingEnd


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
    # LeadingEnd.A or LeadingEnd.B when cab is active, None when no cab selected
    leading_end: Optional[LeadingEnd] = LeadingEnd.A

    # Tiles occupied (ordered from end_a to end_b)
    occupied_tiles: List[Tuple[int, int]] = field(default_factory=list)

    # Blocks occupied
    occupied_blocks: List[str] = field(default_factory=list)

    @property
    def head_tile(self) -> Tuple[int, int]:
        """Get tile of leading end (head)."""
        return self.end_a_tile if self.leading_end == LeadingEnd.A else self.end_b_tile

    @property
    def head_distance_cm(self) -> float:
        """Get distance on tile for leading end (head)."""
        return (
            self.end_a_distance_cm
            if self.leading_end == LeadingEnd.A
            else self.end_b_distance_cm
        )

    @property
    def head_entry_edge(self) -> str:
        """Get entry edge for leading end (head)."""
        return (
            self.end_a_entry_edge
            if self.leading_end == LeadingEnd.A
            else self.end_b_entry_edge
        )

    @property
    def tail_tile(self) -> Tuple[int, int]:
        """Get tile of trailing end (tail)."""
        return self.end_b_tile if self.leading_end == LeadingEnd.A else self.end_a_tile

    @property
    def tail_distance_cm(self) -> float:
        """Get distance on tile for trailing end (tail)."""
        return (
            self.end_b_distance_cm
            if self.leading_end == LeadingEnd.A
            else self.end_a_distance_cm
        )

    @property
    def tail_entry_edge(self) -> str:
        """Get entry edge for trailing end (tail)."""
        return (
            self.end_b_entry_edge
            if self.leading_end == LeadingEnd.A
            else self.end_a_entry_edge
        )

    @property
    def facing_direction(self) -> Optional[Direction]:
        """
        Calculate train's facing direction from physical position.

        For multi-tile trains: Uses vector from adjacent tile to head tile
        For single-tile trains: Uses entry_edge and distance_cm to determine direction

        Returns:
            Direction the train is facing, or None if position is invalid
        """
        if not self.leading_end:
            return None

        # Multi-tile train: calculate from tile positions
        if self.end_a_tile != self.end_b_tile:
            # Determine head tile based on leading_end
            if self.leading_end == LeadingEnd.A:
                head_tile = self.end_a_tile
                # Find adjacent tile toward end_b
                if len(self.occupied_tiles) >= 2:
                    try:
                        head_idx = self.occupied_tiles.index(self.end_a_tile)
                        adjacent_tile = (
                            self.occupied_tiles[1]
                            if head_idx == 0
                            else self.occupied_tiles[head_idx - 1]
                        )
                    except (ValueError, IndexError):
                        adjacent_tile = self.end_b_tile
                else:
                    adjacent_tile = self.end_b_tile
            else:
                head_tile = self.end_b_tile
                if len(self.occupied_tiles) >= 2:
                    try:
                        head_idx = self.occupied_tiles.index(self.end_b_tile)
                        adjacent_tile = (
                            self.occupied_tiles[-2]
                            if head_idx == len(self.occupied_tiles) - 1
                            else self.occupied_tiles[head_idx + 1]
                        )
                    except (ValueError, IndexError):
                        adjacent_tile = self.end_a_tile
                else:
                    adjacent_tile = self.end_a_tile

            # Calculate direction vector from adjacent to head
            dx = head_tile[0] - adjacent_tile[0]
            dy = head_tile[1] - adjacent_tile[1]

            # Map to Direction enum
            for direction in Direction:
                if direction.value == (dx, dy):
                    return direction

            return None

        # Single-tile train: calculate from entry edge and distance
        head_entry = self.head_entry_edge
        head_distance = self.head_distance_cm
        tail_distance = self.tail_distance_cm

        if not head_entry:
            return None

        # Determine exit edge from entry edge
        # For straight track: opposite edge
        exit_edge = self._get_opposite_edge(head_entry)

        # If head is farther from entry than tail, facing toward exit
        # If head is closer to entry than tail, facing toward entry (backing up)
        # Equal distance means facing forward (toward exit)
        if head_distance >= tail_distance:
            # Facing toward exit
            return self._edge_to_direction(exit_edge)
        else:
            # Facing toward entry (reversed)
            return self._edge_to_direction(head_entry)

    def _get_opposite_edge(self, edge: str) -> str:
        """Get opposite edge for straight track."""
        mapping = {"N": "S", "S": "N", "E": "W", "W": "E"}
        return mapping.get(edge, edge)

    def _edge_to_direction(self, edge: str) -> Optional[Direction]:
        """Convert edge string to Direction enum."""
        mapping = {
            "N": Direction.NORTH,
            "E": Direction.EAST,
            "S": Direction.SOUTH,
            "W": Direction.WEST,
        }
        return mapping.get(edge)

    def reverse(self) -> None:
        """Swap leading end (train reverses direction)."""
        self.leading_end = (
            LeadingEnd.B if self.leading_end == LeadingEnd.A else LeadingEnd.A
        )

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
            "leading_end": self.leading_end.value if self.leading_end else None,
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

            # Parse leading_end
            leading_end_data = data.get("leading_end", "a")
            if leading_end_data == "a":
                leading_end = LeadingEnd.A
            elif leading_end_data == "b":
                leading_end = LeadingEnd.B
            else:
                leading_end = LeadingEnd.A  # Default

            return cls(
                end_a_tile=tuple(end_a.get("tile", [0, 0])),
                end_a_distance_cm=float(end_a.get("distance_cm", 0.0)),
                end_a_entry_edge=end_a.get("entry_edge", "W"),
                end_b_tile=tuple(end_b.get("tile", [0, 0])),
                end_b_distance_cm=float(end_b.get("distance_cm", 0.0)),
                end_b_entry_edge=end_b.get("entry_edge", "W"),
                leading_end=leading_end,
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
    - position: TrainPosition with both ends tracked precisely
    - facing_direction: Computed read-only property from position geometry

    Key Properties:
    - model: Train model name (e.g., "BR 218", "Shinkansen E5")
    - min_radius_cm: Minimum curve radius train can navigate
    - active_cab: Which cab is active ('A', 'B', or None)
    - travel_direction: 'forward' or 'reverse' (only for MANUAL mode)
    - state: TrainState for DC analog control
    - speed_profile_id: SpeedProfile for this train
    - assigned_route_id: Route (travel plan) assigned to train
    - manual_target_speed: User-set speed in MANUAL mode

    Note: target_speed and speed_limit_reason are derived from
    TrainController.lookahead[0] per design_new.md and are not stored on
    Train. UI/API consumers must read them from the controller directly.
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
    # current_path: Cached copy of path tiles for serialization (Train.to_dict).
    # NOT the source of truth for control decisions.
    # TrainController reads path data directly from PathDispatcher via
    # get_path_tiles(), which reads from Path.path_tiles (the canonical copy).
    # This cache is set by assign_complete_path_to_train() and may be stale
    # after path extension/cancellation — that's OK since it's only used for
    # frontend display (to_dict), not control logic.
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
    # TRAIN PROPERTIES (Phase 1 - Train System)
    # ========================================================================

    # Train model info
    model: str = ""  # Train model name (e.g., "BR 218", "Shinkansen E5")
    car_number: str = ""  # Unique entity car number
    min_radius_cm: float = 30.0  # Minimum curve radius train can navigate

    # Direction control (facing_direction is now a calculated property)
    active_cab: Optional[Literal["A", "B"]] = None  # Active cab: A, B, or None (off)
    travel_direction: Literal["forward", "reverse"] = "forward"  # For MANUAL mode

    # DC analog control state
    state: TrainState = TrainState.OFF  # Current power/operational state
    wait_reason: Optional[str] = None

    # Speed profile and route
    speed_profile_id: Optional[str] = None  # SpeedProfile for this train
    assigned_route_id: Optional[str] = None  # Route (travel plan) assigned

    # Speed targets
    # NOTE: per design_new.md, `target_speed` and `speed_limit_reason` are
    # controller-derived properties (read from TrainController.lookahead[0]).
    # They are no longer stored on Train; consumers must read them from the
    # controller directly.
    manual_target_speed: float = 0.0  # User-set speed in MANUAL mode

    # Emergency control
    emergency_brake: bool = False  # Emergency braking active

    # ATO state (for persistence across controller recreation)
    # When demo_main creates a temporary controller and calls depart(),
    # this field stores the ATO running state so SimulationController's
    # controller can inherit it.
    ato_running: bool = False
    ato_destination_block_id: Optional[str] = None

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

    @property
    def facing_direction(self) -> Optional[Direction]:
        """
        Get train's facing direction (calculated from position).

        This is READ-ONLY - calculated from position.leading_end and physical
        train geometry. To change direction, use set_cab(), TrainController.turn_around(), or
        update position.leading_end directly.

        Returns:
            Direction the train is facing, or None if no position
        """
        if not self.position:
            return None
        return self.position.facing_direction

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
            self.state == TrainState.RUNNING_MOVING
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
        self.manual_target_speed = 0.0
        return True

    def clear_direction(self) -> bool:
        """
        Clear facing direction and return to IDLE state (STANDBY -> IDLE).

        Allowed only when stopped and in STANDBY.
        Clears leading_end to None, travel_direction to 'forward',
        and speed targets to 0.

        Returns:
            True if successful, False if not allowed
        """
        if not self.is_stopped():
            return False

        if self.state != TrainState.STANDBY:
            return False

        # Clear leading_end (no direction)
        if self.position:
            self.position.leading_end = None

        self.travel_direction = "forward"
        self.manual_target_speed = 0.0
        self.state = TrainState.IDLE
        return True

    def enable_manual(self) -> bool:
        """removed in v1"""
        pass  # removed in v1

    def disable_manual(self) -> bool:
        """removed in v1"""
        pass  # removed in v1

    def set_manual_speed(self, speed: float) -> bool:
        """
        Set manual target speed.

        Args:
            speed: Target speed in km/h

        Returns:
            True if successful
        """
        self.manual_target_speed = max(0.0, min(speed, self.max_speed))
        return True

    def set_travel_direction(self, direction: Literal["forward", "reverse"]) -> bool:
        """
        Set travel direction.

        Args:
            direction: 'forward' or 'reverse'

        Returns:
            True if successful, False if invalid direction
        """
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
        """removed in v1"""
        pass  # removed in v1

    def clear_to_move(self) -> bool:
        """removed in v1"""
        pass  # removed in v1

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

        self.wait_reason = None
        return True

    def emergency_stop(self) -> bool:
        """
        Emergency stop from any operational state.

        Sets emergency braking and stays in RUNNING_MOVING.

        Returns:
            True if successful
        """
        self.is_emergency_braking = True
        self.wait_reason = "Emergency stop"
        return True

    def stop(self) -> bool:
        """
        Controlled stop - bring train to standby state.

        This is the normal ATO stop that brings the train to a controlled stop
        and transitions to STANDBY, ready to depart again.

        Can only be called from RUNNING_MOVING.

        Returns:
            True if successful, False if train is not in a running state
        """
        if self.state != TrainState.RUNNING_MOVING:
            return False
        self.current_speed = 0.0  # Immediately stop for now (simulation handles decel)
        self.state = TrainState.STANDBY
        self.wait_reason = None
        return True

    def set_direction_and_standby(self, direction: Direction) -> bool:
        """
        Set direction and transition to STANDBY (IDLE -> STANDBY).

        Sets direction by positioning the train so that leading_end=LeadingEnd.A faces
        the specified direction. For single-tile trains, this requires the
        position to already have correct entry_edge and distance_cm.

        Args:
            direction: Facing direction to set

        Returns:
            True if successful, False if not in IDLE state
        """
        if self.state != TrainState.IDLE:
            return False

        if self.position:
            # Set leading end to A (Cab A is front)
            self.position.leading_end = LeadingEnd.A
            # Note: For this to work correctly, position must already have
            # correct entry_edge and distances set up to make end_a face
            # the specified direction

        self.active_cab = "A"
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

            # Setting a cab: must be stopped (IDLE or STANDBY)
            if self.state not in (
                TrainState.IDLE,
                TrainState.STANDBY,
            ):
                return False

            # Check if switching from other cab (need to swap position data)
            old_cab = self.active_cab
            self.active_cab = cab

            # Update leading end based on cab selection
            if self.position:
                new_leading_end = LeadingEnd.A if cab == "A" else LeadingEnd.B

                # Switching cabs does NOT move the physical train.
                # end_a stays at its physical location, end_b stays at its physical location.
                # Only leading_end changes to indicate which end is now "front".
                # The entry_edge and distance flipping is handled by the controller
                # since it requires access to the track network.

                self.position.leading_end = new_leading_end

            self.state = TrainState.STANDBY
            return True
        elif cab is None:
            # Turning cab off: must be STANDBY (stopped, not moving)
            if self.state != TrainState.STANDBY:
                return False
            self.active_cab = None
            self.state = TrainState.IDLE
            # Clear leading_end when no cab selected - train has no "front"
            if self.position:
                self.position.leading_end = None
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
        # Note: Full per-car boundary checking requires per-car position tracking
        # which is not currently implemented. This simplified check is safe but
        # conservative - it may reject some valid multi-car opposite-polarity cases.
        return len(self.consist) == 1

    def to_dict(
        self,
        network=None,
        path_tiles: Optional[List[Tuple[int, int, "Direction"]]] = None,
        path_index: Optional[int] = None,
    ) -> dict:
        """Convert train to dictionary for serialization.

        Args:
            network: Optional RailwayNetwork to calculate tile length for distance ratio
            path_tiles: Optional live path tiles (overrides stale self.current_path)
            path_index: Optional live path index (overrides stale self.path_index)
        """
        # Resolve path data: prefer live params, fall back to stale fields
        effective_path = path_tiles if path_tiles is not None else self.current_path
        effective_index = path_index if path_index is not None else self.path_index
        data = {
            "id": self.train_id,
            "length": self.length,
            "length_cm": self.length_cm,
            "speed": self.current_speed,
            "max_speed": self.max_speed,
            "num_cars": self.num_cars,
            "model": self.model,
            "car_number": self.car_number,
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
            "ato_destination_block_id": self.ato_destination_block_id,
            "manual_target_speed": self.manual_target_speed,
            "wait_reason": self.wait_reason,
            # Path info for debug
            "path_index": effective_index,
            "path_length": len(effective_path) if effective_path else 0,
            # Current path as list of tiles with direction
            "current_path": [
                {"x": x, "y": y, "dir": d.name if d else None}
                for x, y, d in (effective_path or [])
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
            data["leading_end"] = pos.leading_end.value if pos.leading_end else None

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
        # Build lookahead_tiles from effective path starting at effective index (head position)
        # This provides info about ALL tiles ahead, not just occupied tiles
        #
        # Data structure:
        # - lookahead_tiles: List of (x, y) tiles from head forward along path
        # - lookahead_tile_info: Rich info for each tile (block, signals, switches, distance)
        # - head_distance_into_tile_cm: How far into current tile the head is
        #
        if network and effective_path and effective_index >= 0:
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

            current_block_id = None
            block_back_length_cm = 0.0
            if effective_index < len(effective_path):
                head_entry = effective_path[effective_index]
                head_x, head_y = head_entry[0], head_entry[1]
                head_track = network.get_track(head_x, head_y)
                current_block_id = head_track.block_section_id if head_track else None
                if current_block_id:
                    for j in range(effective_index - 1, -1, -1):
                        prev_entry = effective_path[j]
                        prev_x, prev_y = prev_entry[0], prev_entry[1]
                        prev_track = network.get_track(prev_x, prev_y)
                        if (
                            not prev_track
                            or prev_track.block_section_id != current_block_id
                        ):
                            break
                        block_back_length_cm += network._get_tile_length_cm(
                            prev_x, prev_y
                        )

            current_block_start_cm = (
                cumulative_distance_cm - block_back_length_cm
                if current_block_id
                else cumulative_distance_cm
            )

            # Iterate from current position to end of path
            for i in range(effective_index, len(effective_path)):
                path_entry = effective_path[i]
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
                tile_block_id = track.block_section_id if track else None

                if tile_block_id and tile_block_id != current_block_id:
                    current_block_id = tile_block_id
                    current_block_start_cm = cumulative_distance_cm

                tile_info = {
                    "tile": [x, y],
                    "length_cm": tile_length,
                    "block_id": tile_block_id,
                    # Distance from train head to START of this tile (cm)
                    # For first tile (head tile), this is negative
                    "distance_to_tile_cm": cumulative_distance_cm,
                    # Distance from train head to START of this block (cm)
                    # For current block, includes tiles behind head
                    "distance_to_block_start_cm": (
                        current_block_start_cm if tile_block_id else None
                    ),
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

            # NOTE: path_tiles is set to occupied_tiles above (for the renderer).
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
        train.car_number = data.get("car_number", "")
        train.min_radius_cm = float(data.get("min_radius_cm", 30.0))

        # Facing direction
        # facing_direction is now computed from position, so don't restore it

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
                print(f"[Train] Unknown state '{data['state']}', defaulting to OFF")
                train.state = TrainState.OFF

        # Speed profile and route
        train.speed_profile_id = data.get("speed_profile_id")
        train.assigned_route_id = data.get("assigned_route_id")

        # Speed targets
        # target_speed and speed_limit_reason are no longer stored on Train;
        # they are controller-derived. Persisted snapshots that still contain
        # them are ignored on load.
        train.manual_target_speed = float(data.get("manual_target_speed", 0.0))
        train.wait_reason = data.get("wait_reason")
        train.ato_destination_block_id = data.get("ato_destination_block_id")

        return train
