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
from constants import STOPPED_SPEED_THRESHOLD_KMH
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

    Note: the live "which route is this train on?" read is derived from
    `RouteExecutor.assigned_route_id_for(train_id)`; it is not stored on
    `Train` (per design_new.md "Train persistent-only contract").
    target_speed and speed_limit_reason are derived from
    TrainController.lookahead[0] per design_new.md and are not stored on
    Train. UI/API consumers must read them from the controller directly.
    """

    train_id: str
    # length: float  # Now derived from consist
    # min_curve_radius: float  # Now derived from consist

    consist: List[TrainCar] = field(default_factory=list)

    # Persistent/configuration scalars. Live speed lives on
    # `TrainController.current_speed_kmh` per design_new.md ~line 1292.
    max_speed: float = 100.0  # Operational max speed (min of cars)
    acceleration: float = 2.0  # Acceleration in m/s² (config; physics owns live)

    # Number of cars in train (for display purposes)
    num_cars: int = 1  # Number of cars/wagons in train consist

    # Priority (for dispatcher system)
    priority: int = 5  # 1-10, higher = higher priority, default 5

    # Braking
    max_braking_rate: float = 3.0  # Maximum braking deceleration in m/s²

    # ========================================================================
    # NEW POSITION SYSTEM - TrainPosition with both ends tracked
    # ========================================================================
    position: Optional[TrainPosition] = None

    # Schedule management
    schedule: Optional[TrainSchedule] = None

    # ========================================================================
    # TRAIN PROPERTIES (Phase 1 - Train System)
    # ========================================================================

    # Train model info
    model: str = ""  # Train model name (e.g., "BR 218", "Shinkansen E5")
    car_number: str = ""  # Unique entity car number
    min_radius_cm: float = 30.0  # Minimum curve radius train can navigate

    # Speed profile and route assignment (persistent/configuration only).
    # Live cab, control mode, ATO state, EB state, and operational state
    # (`active_cab`, `travel_direction`, `state`, `ato_running`,
    # `control_mode`, `emergency_brake`, `wait_reason`) live on
    # `TrainController` per design_new.md ~line 1292; consumers must read
    # them from the controller, not from `Train`.
    speed_profile_id: Optional[str] = None  # SpeedProfile for this train

    # `assigned_route_id` is no longer carried on `Train`. The canonical store
    # is `RouteExecutor.execution_states[train_id].route_id` (with
    # `Route.assigned_train_ids` as the registry-side mirror). Callers ask
    # `RouteExecutor.assigned_route_id_for(train_id)` rather than reading off
    # `Train`. See design_new.md "Train persistent-only contract".

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
    def facing_direction(self) -> Optional[Direction]:
        """
        Get train's facing direction (calculated from position).

        Read-only — derived from `position.leading_end` and train geometry.
        To change direction use `TrainController.set_cab(...)`.
        """
        if not self.position:
            return None
        return self.position.facing_direction

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
    # NEW METHODS (Phase 1 - Train System)
    # ========================================================================

    def update_position_for_cab(self, cab: Optional[Literal["A", "B"]]) -> None:
        """Update `position.leading_end` based on the active cab.

        Called by `TrainController.set_cab(...)` after it has authorized the
        cab change. The state-machine transitions live entirely on the
        controller per design_new.md ~line 1292; this helper exists only to
        keep position/leading_end in sync with cab selection.
        """
        if not self.position:
            return
        if cab == "A":
            self.position.leading_end = LeadingEnd.A
        elif cab == "B":
            self.position.leading_end = LeadingEnd.B
        else:
            self.position.leading_end = None

    # ========================================================================
    # MULTI-BLOCK OCCUPANCY (Phase 6)
    # ========================================================================

    def get_occupied_blocks(self, network=None) -> List[str]:
        """
        Get all blocks this train physically occupies, derived from
        `position.occupied_blocks` / `position.occupied_tiles`.

        Per design_new.md ~line 1292, `Train.reserved_blocks` is forbidden;
        future-authority blocks live on the dispatcher's `Path` and physical
        occupancy is read from `position`.
        """
        if not self.position:
            return []
        if self.position.occupied_blocks:
            return list(self.position.occupied_blocks)
        if not network:
            return []
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

    def to_dict(self, network=None) -> dict:
        """Convert train to dictionary (persistent/configuration only).

        Per design_new.md, `Train` carries only persistent/config state.
        Live position/span/speed/path/operational state is exposed by
        `TrainController.to_dict()` and dispatcher progress payloads.

        `network` is accepted for compatibility with callers but is currently
        unused by the persistent-only payload.
        """
        del network  # reserved for future schema-aware exports
        data = {
            "id": self.train_id,
            "length": self.length,
            "length_cm": self.length_cm,
            "max_speed": self.max_speed,
            "num_cars": self.num_cars,
            "model": self.model,
            "car_number": self.car_number,
            "min_radius_cm": self.min_radius_cm,
            "facing_direction": self.facing_direction.name
            if self.facing_direction
            else None,
            "speed_profile_id": self.speed_profile_id,
        }

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
        Reconstruct a Train from a persisted snapshot.

        Per design_new.md ~line 1292, only persistent/configuration state
        loads here. Live runtime fields (`current_speed`, `state`,
        `active_cab`, `travel_direction`, `ato_running`, `control_mode`,
        `wait_reason`, `emergency_brake`, `is_emergency_braking`) live on
        `TrainController` and are seeded through `add_train(...)` or via
        explicit controller commands; legacy values present in saved
        snapshots are intentionally ignored on load.
        """
        train = cls(
            train_id=data.get("id", ""),
            max_speed=data.get("max_speed", 100.0),
            num_cars=int(data.get("num_cars", 1)),
        )

        if "position" in data and isinstance(data["position"], dict):
            pos_data = data["position"]
            if "end_a" in pos_data:
                train.position = TrainPosition.from_dict(pos_data)

        train.model = data.get("model", "")
        train.car_number = data.get("car_number", "")
        train.min_radius_cm = float(data.get("min_radius_cm", 30.0))

        train.speed_profile_id = data.get("speed_profile_id")

        return train
