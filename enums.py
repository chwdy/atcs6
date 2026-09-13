"""
Railway System Enums

Definition:
This module contains all the enumeration types used throughout the railway management system.
These enums define the possible states, types, and directions for various railway components.
"""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, Optional


class OperationMode(Enum):
    """The session's selected operating environment.

    The three environments share one control core. Simulation bypasses Hardware
    Setup, Virtual Hardware exercises that setup against a controllable fake
    device, and Real Hardware uses the physical device. The short names remain
    aliases for older callers during the migration; their serialized values are
    the canonical environment names.
    """

    SIMULATION = "simulation"
    VIRTUAL_HARDWARE = "virtual_hardware"
    REAL_HARDWARE = "real_hardware"

    SIM = SIMULATION
    REAL = REAL_HARDWARE


class PathStatus(Enum):
    """Path lifecycle states.

    A path is born ACTIVE on a successful create_path(...). It transitions to
    COMPLETED when the train reaches the requested destination, or to
    CANCELLED when the path is revoked. Both terminal states are sticky.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SwitchState(IntEnum):
    """Switch alignment state.

    Binary in v1: MAIN (0) selects the main route, DIVERGE (1) selects the
    diverging route. Used for stored switch state and for required-state
    locks.
    """

    MAIN = 0
    DIVERGE = 1


class TrackType(Enum):
    """Types of railway track segments

    Geometric types (single tile classification):
    - STRAIGHT: NS or EW axial path
    - DIAGONAL: NE, NW, SE, SW path (geometric shape, NOT a curve by itself)
    - SWITCH, DIAMOND, WYE, etc.: multi-path configurations

    Connection-based curve detection:
    - A CURVE is when connected paths are in DIFFERENT orientation groups
    - Groups: (NS), (EW), (NE+SW), (NW+SE)
    - Use Tile.is_curve_connection(exit_edge, neighbor) to check
    """

    EMPTY = "empty"
    STRAIGHT = "straight"  # NS or EW axial path
    DIAGONAL = "diagonal"  # NE, NW, SE, SW path (geometric shape)
    CURVE = "curve"  # Connection-based: different orientation groups
    SWITCH = "switch"
    DIAMOND = "diamond"  # 2-path crossing without switches
    WYE = "wye"
    COMPLEX = "complex"
    SCISSORS = "scissors"
    DOUBLE_CROSS = "double_cross"
    FORBIDDEN = "forbidden"
    SIGNAL = "signal"
    STATION = "station"
    BLOCK_BOUNDARY = "block_boundary"  # 闭塞分区边界


class Direction(Enum):
    """Cardinal directions for train movement and track orientation

    Convention: Y positive is NORTH (up), Y negative is SOUTH (down).
    """

    NORTH = (0, 1)
    EAST = (1, 0)
    SOUTH = (0, -1)
    WEST = (-1, 0)

    def opposite(self):
        """Get the opposite direction"""
        return {
            Direction.NORTH: Direction.SOUTH,
            Direction.SOUTH: Direction.NORTH,
            Direction.EAST: Direction.WEST,
            Direction.WEST: Direction.EAST,
        }[self]


class SignalState(Enum):
    """Railway signal states for train control"""

    GREEN = "green"  # 允许通过
    RED = "red"  # 禁止通过
    YELLOW = "yellow"  # 注意，准备停车
    DOUBLE_YELLOW = "double_yellow"  # 下一个信号可能是红灯


class BlockState(Enum):
    """Block section occupancy states"""

    FREE = "free"  # 空闲
    OCCUPIED = "occupied"  # 占用
    RESERVED = "reserved"  # 预约
    CAUTION = "caution"  # 警告（列车刚离开，下一区段仍被占用，需减速）


class TrainState(Enum):
    """
    Train power and operational states.

    Per design_new.md, the operational state set in v1 is exactly three:
    - OFF: power off
    - READY: powered and stopped (including a train dwelling at a stop, which
      is not a separate state — the planner withholds its onward authority)
    - RUNNING_MOVING: physically moving, or stopped at zero ATC clearance under
      the same active authority

    READY <-> RUNNING_MOVING is movement-derived. Manual driving is a control
    mode (control_mode == "manual"), not a separate operational state; a
    stopped manual train sits in READY just like an ATO train.
    """

    OFF = "off"
    READY = "ready"
    RUNNING_MOVING = "running_moving"


class ControllerCondition(Enum):
    """Derived projection of TrainController recovery-relevant flags.

    Per design_new.md "Controller condition set" canonical home, every member
    maps to one underlying controller field; TrainController.active_conditions
    returns a frozenset of currently-true conditions. RouteExecutor.is_clean_for_create reads a subset of this set, so adding a new latch in the future is one enum value plus one
    mapping line.
    """

    HAS_AUTHORITY = "has_authority"
    EB_LATCHED = "eb_latched"
    POWER_OFF_PENDING = "power_off_pending"
    PATH_CANCEL_PENDING = "path_cancel_pending"
    ROUTE_ABORT_PENDING = "route_abort_pending"
    ROUTE_DEACTIVATE_PENDING = "route_deactivate_pending"


class PlatformSide(Enum):
    """Which side(s) of the track have platforms"""

    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"
    EITHER = "either"  # For flexible platform selection


def direction_from_delta(dx: int, dy: int) -> Optional[str]:
    """
    Get N/E/S/W direction string from coordinate delta.

    Args:
        dx: X coordinate change (positive = East)
        dy: Y coordinate change (positive = North)

    Returns:
        Direction string ('N', 'E', 'S', 'W') or None if no movement
    """
    if dy > 0:
        return "N"
    elif dy < 0:
        return "S"
    elif dx > 0:
        return "E"
    elif dx < 0:
        return "W"
    return None


# Direction offsets for coordinate calculations
DIRECTION_OFFSETS = {
    "N": (0, 1),
    "S": (0, -1),
    "E": (1, 0),
    "W": (-1, 0),
}

# Opposite directions
OPPOSITE_DIRECTIONS = {
    "N": "S",
    "S": "N",
    "E": "W",
    "W": "E",
}


class AtcLimitReasonCode(Enum):
    """Canonical reason codes for the ATC speed-limit selection.

    Per design_new.md "canonical lookahead payload", every consumer that
    inspects the reason behind the current ATC limit must use one of these
    codes rather than a free-form string. The values here are the canonical
    `.value` strings emitted in the lookahead payload's
    `speed_curves[*].atc_limit_reason_code` field.
    """

    RESERVATION_STOP = "reservation_stop"
    CAUTION = "caution"
    NO_RESERVATION = "no_reservation"
    EB = "eb"
    # A posted block speed limit (`Block.speed_limit_kmh`) below the train's
    # current maximum: a permitted-speed restriction ahead, distinct from
    # `reservation_stop` (braking to the reservation/clearance end). Per
    # design_new.md canonical `atc_limit_reason_code` enumeration.
    SPEED_LIMIT = "speed_limit"
    # Per design_new.md line 1728: used when the train or network object is
    # not yet wired, so the empty anchor reports a structured reason code.
    NO_TRAIN = "no_train"
    # Per design_new.md lines 279/1825: a movement-derived deviation cancel is
    # latched (the live path stays ACTIVE), so the head is off its still-active
    # authority. The distance-0 anchor reports `atc_limit = 0` with this reason
    # so ATC brakes the train and the overspeed branch escalates to EB. Distinct
    # from `no_reservation`, which is the genuinely-absent-plan case.
    DEVIATION = "deviation"


class HandlePosition(Enum):
    """Throttle/brake handle positions for train control.

    Power notches (P1-P5): Apply acceleration
    Neutral (N): Coast (no power, no braking)
    Brake notches (B1-B8): Apply service braking
    Emergency Brake (EB): Maximum braking force
    """

    P5 = "P5"  # Power notch 5 (max acceleration: 100%)
    P4 = "P4"  # Power notch 4 (80%)
    P3 = "P3"  # Power notch 3 (60%)
    P2 = "P2"  # Power notch 2 (40%)
    P1 = "P1"  # Power notch 1 (min acceleration: 20%)
    N = "N"  # Neutral (coast)
    B1 = "B1"  # Brake notch 1 (min braking: 12.5%)
    B2 = "B2"  # Brake notch 2 (25%)
    B3 = "B3"  # Brake notch 3 (37.5%)
    B4 = "B4"  # Brake notch 4 (50%)
    B5 = "B5"  # Brake notch 5 (62.5%)
    B6 = "B6"  # Brake notch 6 (75%)
    B7 = "B7"  # Brake notch 7 (87.5%)
    B8 = "B8"  # Brake notch 8 (max service braking: 100%)
    EB = "EB"  # Emergency brake


# Handle position order (for UI slider)
HANDLE_ORDER = [
    "P5",
    "P4",
    "P3",
    "P2",
    "P1",
    "N",
    "B1",
    "B2",
    "B3",
    "B4",
    "B5",
    "B6",
    "B7",
    "B8",
    "EB",
]

HANDLE_POSITION_INDEX = {
    HandlePosition.P5: 0,
    HandlePosition.P4: 1,
    HandlePosition.P3: 2,
    HandlePosition.P2: 3,
    HandlePosition.P1: 4,
    HandlePosition.N: 5,
    HandlePosition.B1: 6,
    HandlePosition.B2: 7,
    HandlePosition.B3: 8,
    HandlePosition.B4: 9,
    HandlePosition.B5: 10,
    HandlePosition.B6: 11,
    HandlePosition.B7: 12,
    HandlePosition.B8: 13,
    HandlePosition.EB: 14,
}

INDEX_TO_HANDLE_POSITION = {v: k for k, v in HANDLE_POSITION_INDEX.items()}


# Acceleration multipliers for power notches
POWER_MULTIPLIERS = {
    HandlePosition.P5: 1.0,
    HandlePosition.P4: 0.8,
    HandlePosition.P3: 0.6,
    HandlePosition.P2: 0.4,
    HandlePosition.P1: 0.2,
}

# Deceleration multipliers for brake notches.
# These multiply the *operational* deceleration rate
# (`profile.deceleration_rate * profile.operational_decel_ratio`),
# i.e. the rate ATO uses when planning its slow-down curve.
# B6 = 1.00× = exactly tracks the planned curve.
# B7-B8 sit above 1× so a train that has fallen behind the curve can catch up.
# B8 is set high enough to exceed the raw `deceleration_rate` (the rate ATC
# uses for its curve, ≈1/operational_decel_ratio in op_decel units) so that
# B8 can close the gap when the train is behind the ATC curve.
# B1-B5 sit below 1× for gentler-than-curve braking.
# EB bypasses this table and applies `profile.emergency_brake_rate`.
BRAKE_MULTIPLIERS = {
    HandlePosition.B1: 0.20,
    HandlePosition.B2: 0.40,
    HandlePosition.B3: 0.55,
    HandlePosition.B4: 0.70,
    HandlePosition.B5: 0.85,
    HandlePosition.B6: 1.00,
    HandlePosition.B7: 1.30,
    HandlePosition.B8: 1.70,
}


@dataclass
class CommandResult:
    """Result of a TrainMover command execution.

    Provides a standardized response format for command validation and execution.
    """

    success: bool
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
        }
