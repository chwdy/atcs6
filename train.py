"""
Train Management

Definition:
This module defines train entities: their physical properties, consist, and
persistent configuration.

Live physical position/span is NOT stored on `Train` in v1 — it lives on
`TrainController.physics_sync.current_span` (`TrainSpan`), seeded at placement
through `SimulationController.add_train(...)`. `Train` carries only
persistent/configuration state.
"""

from dataclasses import dataclass, field
from typing import List, Optional


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

    Represents a complete train with its persistent/configuration state. Live
    physical position/span, speed, ATO/ATC/path-runtime
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

    # Number of cars in train (for display purposes)
    num_cars: int = 1  # Number of cars/wagons in train consist

    # Priority (for dispatcher system)
    priority: int = 5  # 1-10, higher = higher priority, default 5

    # ========================================================================
    # TRAIN PROPERTIES (Phase 1 - Train System)
    # ========================================================================

    # Train model info
    model: str = ""  # Train model name (e.g., "BR 218", "Shinkansen E5")
    car_number: str = ""  # Unique entity car number
    min_radius_cm: float = 30.0  # Minimum curve radius train can navigate
    max_slope: float = 0.0  # Maximum gradient the train can climb, percent grade (0 = unspecified)

    # Speed profile and route assignment (persistent/configuration only).
    # Live cab, control mode, ATO state, EB state, and operational state live on
    # `TrainController` per design_new.md; consumers read them from the
    # controller, not from `Train`.
    speed_profile_id: Optional[str] = None  # SpeedProfile for this train
    # The `RosterEntry` this train was placed from, when placement named one.
    # Speed calibration's Save-as-new commit relinks both the train and this
    # entry to the profile it creates, so the measurement reaches the locomotive
    # it was taken on rather than every locomotive of the model
    # (design_new.md "Speed calibration", What commit writes).
    roster_entry_id: Optional[str] = None

    # Maintenance counters (see design_new.md "Maintenance counters"): durable
    # cumulative usage, written during simulation and persisted with the layout.
    total_distance_cm: float = 0.0  # Cumulative distance travelled (cm)
    total_run_time_seconds: float = 0.0  # Cumulative powered-on time (s)

    # `False` on every train the operator places, `True` only on a runtime-created
    # unidentified train (design_new.md "Power -> Unidentified trains"). Set once at creation and
    # never edited; it is the predicate every rule about unidentified trains tests. Unidentified trains are
    # runtime-only — they are not written to saved state and not restored.
    is_unidentified: bool = False

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
            "max_slope": self.max_slope,
            "speed_profile_id": self.speed_profile_id,
            "roster_entry_id": self.roster_entry_id,
            "total_distance_cm": self.total_distance_cm,
            "total_run_time_seconds": self.total_run_time_seconds,
            # Read by the UI so an unidentified train is shown as itself, marked apart from the
            # trains the operator controls (design_new.md "Unidentified trains").
            "is_unidentified": self.is_unidentified,
        }

        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Train":
        """
        Reconstruct a Train from a persisted snapshot.

        Per design_new.md, only persistent/configuration state loads here. Live
        runtime fields (position/span, current_speed, powered, train_status,
        travel_direction, engaged, control_mode, eb.active) live on
        `TrainController` and are seeded through `add_train(...)` or explicit
        controller commands; any such values present in legacy saved snapshots
        are silently ignored on load.
        """
        train = cls(
            train_id=data.get("id", ""),
            max_speed=data.get("max_speed", 100.0),
            num_cars=int(data.get("num_cars", 1)),
        )

        train.model = data.get("model", "")
        train.car_number = data.get("car_number", "")
        train.min_radius_cm = float(data.get("min_radius_cm", 30.0))
        train.max_slope = float(data.get("max_slope", 0.0))

        train.speed_profile_id = data.get("speed_profile_id")
        train.roster_entry_id = data.get("roster_entry_id")

        train.total_distance_cm = float(data.get("total_distance_cm", 0.0))
        train.total_run_time_seconds = float(data.get("total_run_time_seconds", 0.0))

        return train
