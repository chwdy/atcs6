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
    `path_id`, `resource_sequence`, and `authority_end_block_id`. The
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

    @property
    def authority_end_block_id(self) -> str:
        """Terminal block of the live authority; `""` when the sequence is empty."""
        if not self.resource_sequence:
            return ""
        return self.resource_sequence[-1].resource_id

    def get_authority_end_block_id(self) -> str:
        """Function form, matching `Path.get_authority_end_block_id()`."""
        return self.authority_end_block_id

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

    Per design_new.md ~line 168, `TrainPhysics` is an executor only and
    must not decide manual-mode policy. The controller pre-computes which
    speed-axis to apply this tick (handle integration, snap to a target
    speed, or emergency brake) and what speed the voltage emitter should
    treat as the reference scalar. Physics dispatches on `kind` and never
    inspects controller mode fields.

    Fields used per kind:
      - PHYSICS_KIND_HANDLE: `handle` is integrated from the previous speed
        via the standard handle→accel mapping (used for ATO and the
        manual `simulation` submode).
      - PHYSICS_KIND_TARGET_SPEED: speed snaps directly to
        `target_speed_kmh` (used for the manual `power_percent` submode).
        The controller has already clamped this against the live ATC limit
        and EB rule.
      - PHYSICS_KIND_EMERGENCY_BRAKE: physics applies the speed profile's
        `emergency_brake_rate` deceleration from the previous speed.

    `voltage_source_speed_kmh` is what the voltage emitter should treat as
    the train's current speed for the per-tick voltage scalar (typically
    the same as the physics solution, but for `power_percent` mode it is
    the calibrated speed even before integration converges).

    `voltage_source_power_percent` is set only when the commanded power
    percent is the authoritative operating point for the tick (manual
    `power_percent` mode, no ATC clamp). Physics then maps it straight to
    `track_voltage` through `voltage_power_curve`; inverting
    `power_speed_curve` first would not round-trip on a calibrated curve.

    `voltage_source_track_voltage` is set only while an open lighting-voltage
    calibration run drives (design_new.md "Lighting-voltage calibration", What
    the run drives). It is the exact voltage magnitude the emitter must put on
    the rails this tick, in place of whatever the operational state would
    otherwise output; physics returns it directly rather than deriving a
    voltage from a speed or a percent. The command carries no motion (handle
    `N`) alongside it, because the candidate never feeds the speed solution.
    """

    kind: str
    handle: HandlePosition = HandlePosition.N
    target_speed_kmh: float = 0.0
    voltage_source_speed_kmh: float = 0.0
    voltage_source_power_percent: Optional[float] = None
    voltage_source_track_voltage: Optional[float] = None
