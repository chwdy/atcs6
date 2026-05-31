"""
Train and Schedule Management

Definition:
This module defines train entities and their scheduling system. It includes
train properties, operational status, and time-based scheduling with stops and
routing information.

Live physical position/span is NOT stored on `Train` in v1 — it lives on
`TrainController.physics_sync.current_span` (`TrainSpan`), seeded at placement
through `SimulationController.add_train(...)`. `Train` carries only
persistent/configuration state.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime


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
    length_cm: float  # Car length in centimetres
    max_speed: float = 120.0  # Max speed for this car


@dataclass
class Train:
    """
    Railway train with physical properties and operational state.

    Represents a complete train with its persistent/configuration state and
    assigned schedule. Live physical position/span, speed, ATO/ATC/path-runtime
    state, power/cab state, and operational state are NOT stored here — they
    live on `TrainController` (the live span is
    `TrainController.physics_sync.current_span`). See design_new.md
    "Design Direction".

    Key Properties:
    - model: Train model name (e.g., "BR 218", "Shinkansen E5")
    - min_radius_cm: Minimum curve radius train can navigate
    - speed_profile_id: SpeedProfile for this train

    Note: the live "which route is this train on?" read is derived from
    `RouteExecutor.assigned_route_id_for(train_id)`; it is not stored on
    `Train`. target_speed and speed_limit_reason are derived from
    `TrainController.lookahead[0]`; head/tail and travel direction are derived
    on the controller from `(cab, current_span)`. UI/API consumers read all of
    these from the controller directly.
    """

    train_id: str

    consist: List[TrainCar] = field(default_factory=list)

    # Persistent/configuration scalars. Live speed lives on
    # `TrainController.physics_sync.current_speed_kmh` per design_new.md.
    max_speed: float = 100.0  # Operational max speed (min of cars)
    acceleration: float = 2.0  # Acceleration in m/s² (config; physics owns live)

    # Number of cars in train (for display purposes)
    num_cars: int = 1  # Number of cars/wagons in train consist

    # Priority (for dispatcher system)
    priority: int = 5  # 1-10, higher = higher priority, default 5

    # Braking
    max_braking_rate: float = 3.0  # Maximum braking deceleration in m/s²

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
    # Live cab, control mode, ATO state, EB state, and operational state live on
    # `TrainController` per design_new.md; consumers read them from the
    # controller, not from `Train`.
    speed_profile_id: Optional[str] = None  # SpeedProfile for this train

    # `assigned_route_id` is not carried on `Train`. The canonical store is
    # `RouteExecutor.execution_states[train_id].route_id`; callers ask
    # `RouteExecutor.assigned_route_id_for(train_id)`.

    @property
    def length_m(self) -> float:
        """Total length of train in metres (approx track units for now).

        Both `length_m` and `length_cm` return the same physical length in
        different units; the suffix makes the unit explicit at every call site.
        """
        # Assuming 1 track unit = 100m
        total_meters = sum(car.length_cm for car in self.consist)
        return total_meters / 100.0 if self.consist else 1.0

    @property
    def length_cm(self) -> float:
        """Total length of train in centimetres."""
        return sum(car.length_cm for car in self.consist) if self.consist else 50.0

    def get_next_scheduled_stop(self, current_time: datetime) -> Optional[ScheduleStop]:
        """Get the next stop in the train's schedule"""
        if self.schedule:
            return self.schedule.get_current_target(current_time)
        return None

    def to_dict(self, network=None) -> dict:
        """Convert train to dictionary (persistent/configuration only).

        Per design_new.md, `Train` carries only persistent/config state. Live
        position/span/speed/path/operational state is exposed by
        `TrainController.to_dict()` and dispatcher progress payloads.

        `network` is accepted for compatibility with callers but is currently
        unused by the persistent-only payload.
        """
        del network  # reserved for future schema-aware exports
        data = {
            "id": self.train_id,
            "length_m": self.length_m,
            "length_cm": self.length_cm,
            "max_speed": self.max_speed,
            "num_cars": self.num_cars,
            "model": self.model,
            "car_number": self.car_number,
            "min_radius_cm": self.min_radius_cm,
            "speed_profile_id": self.speed_profile_id,
        }

        if self.schedule:
            # Simple schedule info
            data["schedule"] = {"id": self.schedule.schedule_id, "next_stop": None}
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

        Per design_new.md, only persistent/configuration state loads here. Live
        runtime fields (position/span, current_speed, state, active_cab,
        travel_direction, ato_running, control_mode, wait_reason,
        emergency_brake) live on `TrainController` and are seeded through
        `add_train(...)` or explicit controller commands; any such values
        present in legacy saved snapshots are silently ignored on load.
        """
        train = cls(
            train_id=data.get("id", ""),
            max_speed=data.get("max_speed", 100.0),
            num_cars=int(data.get("num_cars", 1)),
        )

        train.model = data.get("model", "")
        train.car_number = data.get("car_number", "")
        train.min_radius_cm = float(data.get("min_radius_cm", 30.0))

        train.speed_profile_id = data.get("speed_profile_id")

        return train
