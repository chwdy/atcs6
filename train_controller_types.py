"""
Shared dataclasses for `TrainController` and the helper modules
(`LookaheadBuilder`, `ATCCalculator`, `ATONavigator`) extracted out of
`train_controller.py`. Re-exported from `train_controller` so other
modules keep a single import home.
"""

from dataclasses import dataclass
from typing import Optional

from enums import HandlePosition, SignalState


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
    """

    kind: str
    handle: HandlePosition = HandlePosition.N
    target_speed_kmh: float = 0.0
    voltage_source_speed_kmh: float = 0.0
