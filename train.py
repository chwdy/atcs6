"""
Train and Schedule Management

Definition:
This module defines train entities and their scheduling system. It includes train properties,
operational status, and time-based scheduling with stops and routing information.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Literal
from datetime import datetime
from enums import TrainStatus, TrainState, Direction, OPPOSITE_DIRECTIONS


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

    def get_next_stop_after(
        self, station_pos: Tuple[int, int]
    ) -> Optional[ScheduleStop]:
        """Get the next stop after a given station position"""
        found_current = False
        for stop in self.stops:
            if found_current:
                return stop
            if stop.station_pos == station_pos:
                found_current = True
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

    def get_total_journey_time(self) -> float:
        """Get total journey time in minutes"""
        if self.stops:
            first_stop = min(self.stops, key=lambda s: s.arrival_time)
            last_stop = max(self.stops, key=lambda s: s.departure_time)
            return (
                last_stop.departure_time - first_stop.arrival_time
            ).total_seconds() / 60
        return 0


@dataclass
class TrainCar:
    """
    Individual train car/wagon.
    """

    car_id: str
    car_type: str  # "LOCOMOTIVE", "PASSENGER", "FREIGHT"
    length: float  # Length in meters
    mass: float  # Mass in kg
    max_speed: float = 120.0  # Max speed for this car


@dataclass
class Train:
    """
    Railway train with physical properties and operational state.

    Represents a complete train with its current position, movement capabilities,
    operational status, and assigned schedule.

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

    # Priority (for dispatcher system)
    priority: int = 5  # 1-10, higher = higher priority, default 5

    # Braking
    max_braking_rate: float = 3.0  # Maximum braking deceleration in m/s²
    is_emergency_braking: bool = False  # True if currently emergency braking

    # Energy metrics
    energy_consumed: float = 0.0  # kWh
    efficiency: float = 0.85  # Motor efficiency

    # Position and movement
    current_pos: Optional[Tuple[int, int]] = None
    current_direction: Optional[Direction] = None
    distance_on_track: float = 0.0  # Distance traveled on current track segment
    occupied_tracks: List[Tuple[int, int]] = field(default_factory=list)

    # Operational status (legacy)
    status: TrainStatus = TrainStatus.IDLE

    # Schedule management
    schedule: Optional[TrainSchedule] = None

    # Pathfinding and movement
    current_path: Optional[List[Tuple[int, int, Direction]]] = None
    path_index: int = 0

    # Block system integration
    reserved_blocks: List[str] = field(default_factory=list)

    # Path assignment (dispatcher system)
    # Note: "path" = dispatcher's reserved route; "route" = train's travel plan
    assigned_path_id: Optional[str] = None

    # ========================================================================
    # NEW PROPERTIES (Phase 1 - Train System)
    # ========================================================================

    # Train model info
    model: str = ""  # Train model name (e.g., "BR 218", "Shinkansen E5")
    min_radius_cm: float = 30.0  # Minimum curve radius train can navigate

    # Direction control
    facing_direction: Optional[Direction] = None  # Which cab is active
    travel_direction: Literal["forward", "reverse"] = "forward"  # For MANUAL mode

    # DC analog control state
    state: TrainState = TrainState.OFF  # Current power/operational state

    # Speed profile and route
    speed_profile_id: Optional[str] = None  # SpeedProfile for this train
    assigned_route_id: Optional[str] = None  # Route (travel plan) assigned

    # Speed targets
    target_speed: float = 0.0  # ATC-limited target speed
    manual_target_speed: float = 0.0  # User-set speed in MANUAL mode

    @property
    def length(self) -> float:
        """Total length of train in meters (approx track units for now)"""
        # Assuming 1 track unit = 100m
        total_meters = sum(car.length for car in self.consist)
        return total_meters / 100.0 if self.consist else 1.0

    @property
    def total_mass(self) -> float:
        """Total mass of train in kg"""
        return sum(car.mass for car in self.consist) if self.consist else 100000.0

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

    def update_physics(
        self, delta_time: float, target_speed: float, track_length: float
    ):
        """
        Update train physics (speed and position).

        Args:
            delta_time: Time step in seconds
            target_speed: Target speed in km/h
            track_length: Length of current track segment
        """
        # Update speed
        if self.current_speed < target_speed:
            # Accelerate
            self.current_speed += (self.acceleration * 3.6) * delta_time
            if self.current_speed > target_speed:
                self.current_speed = target_speed
        elif self.current_speed > target_speed:
            # Decelerate
            self.current_speed -= (self.acceleration * 3.6) * delta_time
            if self.current_speed < target_speed:
                self.current_speed = target_speed

        # Update position
        # Distance = speed * time
        # speed in km/h, convert to track_units/sec?
        # Let's assume 1 track unit = 100m for simulation scale
        # speed (m/s) = km/h / 3.6
        # distance (m) = speed * dt
        # distance (units) = distance (m) / 100

        speed_ms = self.current_speed / 3.6
        dist_m = speed_ms * delta_time
        dist_units = dist_m / 100.0

        self.distance_on_track += dist_units

        # Calculate energy consumption
        # Power (W) = Force (N) * Velocity (m/s)
        # Force = mass * acceleration + resistance
        # Resistance approx = 0.002 * mass * g (rolling resistance) + air drag
        mass = self.total_mass
        g = 9.81
        resistance = 0.002 * mass * g + 0.5 * 1.2 * 10 * speed_ms**2  # Simplified drag

        force = resistance
        if self.current_speed < target_speed:
            force += mass * self.acceleration

        power_watts = force * speed_ms
        if power_watts < 0:
            power_watts = 0  # No regen braking for now

        energy_joules = power_watts * delta_time
        energy_kwh = energy_joules / 3600000.0

        self.energy_consumed += energy_kwh / self.efficiency

    def is_at_station(self, station_pos: Tuple[int, int]) -> bool:
        """Check if train is currently at a specific station"""
        return self.current_pos == station_pos and self.status == TrainStatus.AT_STATION

    def is_moving(self) -> bool:
        """Check if train is currently in motion"""
        return self.status == TrainStatus.RUNNING and self.current_speed > 0

    def is_delayed(self, current_time: datetime) -> bool:
        """Check if train is delayed according to its schedule"""
        if not self.schedule or not self.current_pos:
            return False
        return self.schedule.is_delayed(current_time, self.current_pos)

    def get_next_scheduled_stop(self, current_time: datetime) -> Optional[ScheduleStop]:
        """Get the next stop in the train's schedule"""
        if self.schedule:
            return self.schedule.get_current_target(current_time)
        return None

    def set_position(self, position: Tuple[int, int], direction: Direction):
        """Update train's current position and direction"""
        self.current_pos = position
        self.current_direction = direction

    def add_occupied_track(self, track_pos: Tuple[int, int]):
        """Add a track to the list of tracks occupied by this train"""
        if track_pos not in self.occupied_tracks:
            self.occupied_tracks.append(track_pos)

    def remove_occupied_track(self, track_pos: Tuple[int, int]):
        """Remove a track from the list of occupied tracks"""
        if track_pos in self.occupied_tracks:
            self.occupied_tracks.remove(track_pos)

    def clear_occupied_tracks(self):
        """Clear all occupied tracks"""
        self.occupied_tracks.clear()

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
        self.status = TrainStatus.WAITING  # Will become IDLE when stopped

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
            self.status = TrainStatus.IDLE
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
        return True

    def wait_for_path(self) -> bool:
        """
        Stop and wait for path/signal (RUNNING_MOVING -> RUNNING_WAITING).

        Train must be in RUNNING_MOVING state.

        Returns:
            True if successful, False if not in correct state
        """
        if self.state != TrainState.RUNNING_MOVING:
            return False
        self.state = TrainState.RUNNING_WAITING
        self.target_speed = 0.0  # Stop the train
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

        return True

    def emergency_stop(self) -> bool:
        """
        Emergency stop from any RUNNING state.

        Transitions RUNNING_MOVING -> RUNNING_WAITING and sets speed to 0.

        Returns:
            True if successful
        """
        if self.state == TrainState.RUNNING_MOVING:
            self.state = TrainState.RUNNING_WAITING
            self.target_speed = 0.0
            self.is_emergency_braking = True
            return True
        return False

    def set_direction_and_standby(self, direction: Direction) -> bool:
        """
        Set direction and transition to STANDBY (IDLE -> STANDBY).

        Convenience method that combines setting direction and transitioning.

        Args:
            direction: Facing direction to set

        Returns:
            True if successful, False if not in IDLE state
        """
        if self.state != TrainState.IDLE:
            return False
        self.facing_direction = direction
        self.state = TrainState.STANDBY
        return True

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
        if not self.current_pos:
            # No position - fall back to reserved_blocks if available
            if self.reserved_blocks:
                return self.reserved_blocks
            return []

        # If no network provided or train is short, just return current block
        if not network or self.length <= 1.0:
            # Simple case: train fits in one block
            if hasattr(network, "tracks") and network:
                track = network.tracks.get(self.current_pos)
                if track and track.block_section_id:
                    return [track.block_section_id]
            # Fallback to reserved_blocks
            if self.reserved_blocks:
                return self.reserved_blocks
            return []

        # Multi-block case: calculate based on length and path
        occupied = []
        remaining_length_cm = self.length * 100  # Convert meters to cm

        # Start at head position
        current_pos = self.current_pos
        distance_in_current_block = self.distance_on_track * 100  # Convert to cm

        # Add blocks as we trace back
        visited_positions = set()

        while remaining_length_cm > 0 and current_pos:
            if current_pos in visited_positions:
                # Avoid infinite loop
                break
            visited_positions.add(current_pos)

            track = network.tracks.get(current_pos)
            if not track:
                break

            # Add this block if it has one
            if track.block_section_id and track.block_section_id not in occupied:
                occupied.append(track.block_section_id)

            # Calculate how much of train is in this block
            if current_pos == self.current_pos:
                # Head position - use distance_on_track
                block_coverage = min(remaining_length_cm, distance_in_current_block)
            else:
                # Previous blocks - use full block length
                block_length_cm = (
                    track.length * 100 if hasattr(track, "length") else 100.0
                )
                block_coverage = min(remaining_length_cm, block_length_cm)

            remaining_length_cm -= block_coverage

            if remaining_length_cm <= 0:
                break

            # Move to previous track in path
            # This is simplified - ideally would follow the actual path backwards
            # For now, just stop if train is longer than current block
            break

        # If we got blocks from calculation, return them
        if occupied:
            return occupied

        # Otherwise fall back to reserved_blocks
        if self.reserved_blocks:
            return self.reserved_blocks

        return []

    def update_occupancy(self, network) -> None:
        """
        Update which blocks this train occupies based on position and length.

        Occupies new blocks as head enters, releases old blocks as tail exits.

        Args:
            network: Railway network
        """
        if not network or not hasattr(network, "blocks"):
            return

        new_occupied = set(self.get_occupied_blocks(network))
        old_occupied = set(self.reserved_blocks) if self.reserved_blocks else set()

        # Occupy new blocks
        for block_id in new_occupied - old_occupied:
            block = network.blocks.get(block_id)
            if block and hasattr(block, "occupy"):
                block.occupy(self.train_id)

        # Release old blocks (tail has exited)
        for block_id in old_occupied - new_occupied:
            block = network.blocks.get(block_id)
            if block and hasattr(block, "release"):
                block.release()

        # Update reserved blocks list
        self.reserved_blocks = list(new_occupied)

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

    def to_dict(self) -> dict:
        """Convert train to dictionary for serialization"""
        data = {
            "id": self.train_id,
            "length": self.length,
            "speed": self.current_speed,
            "max_speed": self.max_speed,
            "status": self.status.name,
            "occupied_tracks": self.occupied_tracks,
            # New Phase 1 properties
            "model": self.model,
            "min_radius_cm": self.min_radius_cm,
            "facing_direction": self.facing_direction.name
            if self.facing_direction
            else None,
            "travel_direction": self.travel_direction,
            "state": self.state.value,
            "speed_profile_id": self.speed_profile_id,
            "assigned_route_id": self.assigned_route_id,
            "target_speed": self.target_speed,
            "manual_target_speed": self.manual_target_speed,
        }

        if self.current_pos:
            data["position"] = {
                "x": self.current_pos[0],
                "y": self.current_pos[1],
                "distance": self.distance_on_track,
            }

        if self.current_direction:
            data["direction"] = self.current_direction.name

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

        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Train":
        """
        Create a Train from a dictionary.

        Note: This reconstructs basic train state. Schedule and path
        will be None and need to be re-established if needed.
        """
        from enums import TrainStatus, TrainState, Direction

        train = cls(
            train_id=data.get("id", ""),
            max_speed=data.get("max_speed", 100.0),
            current_speed=data.get("speed", 0.0),
        )

        # Position
        if "position" in data:
            pos = data["position"]
            train.current_pos = (pos.get("x", 0), pos.get("y", 0))
            train.distance_on_track = pos.get("distance", 0.0)

        # Direction (current movement direction)
        if "direction" in data:
            try:
                train.current_direction = Direction[data["direction"]]
            except KeyError:
                pass

        # Status (legacy)
        if "status" in data:
            try:
                train.status = TrainStatus[data["status"]]
            except KeyError:
                train.status = TrainStatus.IDLE

        # Occupied tracks
        train.occupied_tracks = [
            tuple(t) if isinstance(t, list) else t
            for t in data.get("occupied_tracks", [])
        ]

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

        return train
