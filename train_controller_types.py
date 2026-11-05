"""
Shared dataclasses for `TrainController` and the helper modules
(`LookaheadBuilder`, `ATCCalculator`, `ATONavigator`) extracted out of
`train_controller.py`. Re-exported from `train_controller` so other
modules keep a single import home.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from enums import HandlePosition, SignalState

if TYPE_CHECKING:
    from path import PathTileEntry, ResourceSequenceEntry


@dataclass(frozen=True)
class ReceivedPathPlan:
    """The controller's read-only view of dispatcher-owned movement authority.

    design_new.md "received_path_plan schema": the projection carries exactly
    `path_id`, `resource_sequence`, `reservation_end_block_id`, and
    `zero_reservation_end_standoff`. The
    dispatcher-only fields on the `Path` record — `status`,
    `current_resource_index`, `last_status_updated_time` — are deliberately
    absent, because "the controller does not see those fields." The requested
    target is route-owned and is not stored on `Path` at all.

    `resource_sequence` is held by reference, not copied: `PathDispatcher` owns
    the list, and this projection is rebuilt live on every read (cross-tick
    projection caching is Out of Scope), so a per-read copy would be pure cost.
    """

    path_id: str
    resource_sequence: List["ResourceSequenceEntry"] = field(default_factory=list)
    # When True, TrainController lookahead selects a zero reservation-end safety
    # margin for this path (a speed-calibration leg); design_new.md,
    # "A calibration leg holds no reservation-end standoff".
    zero_reservation_end_standoff: bool = False

    @property
    def reservation_end_block_id(self) -> str:
        """Terminal block of the live authority; `""` when the sequence is empty."""
        if not self.resource_sequence:
            return ""
        return self.resource_sequence[-1].resource_id

    def get_reservation_end_block_id(self) -> str:
        """Function form, matching `Path.get_reservation_end_block_id()`."""
        return self.reservation_end_block_id

    def get_path_tiles(self) -> List["PathTileEntry"]:
        """Derived ordered tile list — `resource_sequence[*].path_tiles`
        concatenated in `sequence_index` order. Computed live, never cached."""
        tiles: List["PathTileEntry"] = []
        for entry in self.resource_sequence:
            tiles.extend(entry.path_tiles)
        return tiles


@dataclass
class LookaheadPoint:
    """One point in the lookahead representing block information ahead.

    Used by both ATC (for safety speed calculation) and ATO (for navigation).
    Signal state is included for display purposes but does not affect ATC
    speed calculation (this is a CBTC-like block-based system).
    """

    distance_cm: float  # Distance from train head to start of this block
    speed_limit_kmh: float  # Block speed limit (or 0 if path end)
    block_id: str
    signal_state: Optional[SignalState] = None
    station_id: Optional[str] = None
    is_path_end: bool = False


@dataclass
class ATOInfo:
    """ATO navigation information derived from the train's assigned path.

    Contains path/destination details and determines which cab should be
    active based on path direction.
    """

    path_id: Optional[str] = None
    destination_block_id: Optional[str] = None
    destination_station_name: Optional[str] = None
    path_heading: str = "a"  # "a" or "b" — which end the path heads toward
    required_cab: str = "A"  # "A" or "B" based on path_heading


# Tagged-union kinds for `PhysicsCommand.kind`. Choosing strings keeps the
# dataclass JSON-friendly without needing an Enum import in physics.
PHYSICS_KIND_HANDLE = "handle"
PHYSICS_KIND_TARGET_SPEED = "target_speed"
PHYSICS_KIND_EMERGENCY_BRAKE = "emergency_brake"


@dataclass
class PhysicsCommand:
    """Per-tick command from `TrainController` to `TrainPhysics`.

    Per design_new.md "Ownership split", `TrainPhysics` is an executor only and
    must not decide manual-mode policy. The controller pre-computes which
    speed-axis to apply this tick (handle integration, snap to a target
    speed, or emergency brake) and what speed the voltage emitter should
    treat as the reference scalar. Physics dispatches on `kind` and never
    inspects controller mode fields.

    Fields used per kind:
      - PHYSICS_KIND_HANDLE: `handle` is integrated from the previous speed
        via the standard handle→accel mapping (used for ATO and the
        manual `simulation` submode). `atc_limit_kmh` is set only for manual
        `simulation`: it is the live distance-0 ATC limit the integrated speed
        may not exceed, and the raw driver notch is never rewritten, so physics
        holds the train at the limit and decelerates toward it at the service
        brake rate when it is already over, rather than snapping the speed down
        (design_new.md, Driver/operator inputs). ATO leaves it `None` — its
        handle already aims at an ATC-aware target, so it needs no physics cap.
        For ATO, `target_speed_kmh` carries the ATO output's commanded speed,
        which physics reads to tell a train ATO drives from one it stops
        (design_new.md "TrainPhysics", A moving train never runs below its
        starting speed).
      - PHYSICS_KIND_TARGET_SPEED: speed snaps directly to
        `target_speed_kmh` (used for the manual `power_percent` submode).
        The controller has already clamped this against the live ATC limit
        and EB rule.
      - PHYSICS_KIND_EMERGENCY_BRAKE: physics applies the speed profile's
        `emergency_brake_rate` deceleration from the previous speed.

    `voltage_source_speed_kmh` is the speed the voltage emitter maps to the
    per-tick voltage scalar through `voltage_speed_curve`: the integrated
    current speed for ATO and manual `simulation`, the percent's speed for
    `power_percent` (even before integration converges), and 0 otherwise. A
    speed of 0 maps to `lighting_voltage`, or 0 V when the profile sets none
    (design_new.md "TrainPhysics", A powered train that nothing drives outputs
    `lighting_voltage`).

    `voltage_source_track_voltage` is the exact voltage magnitude the emitter
    must put on the rails this tick, in place of a voltage derived from a speed.
    It is set under an emergency cut-out (`0`), while an open lighting-voltage
    run drives (with handle `N`, because the candidate never feeds the speed
    solution), and while a speed-calibration step drives a voltage no ceiling
    lowered (design_new.md "Speed calibration", Controller owns calibration
    output).
    """

    kind: str
    handle: HandlePosition = HandlePosition.N
    target_speed_kmh: float = 0.0
    voltage_source_speed_kmh: float = 0.0
    voltage_source_track_voltage: Optional[float] = None
    atc_limit_kmh: Optional[float] = None
