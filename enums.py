"""
Railway System Enums

Definition:
This module contains all the enumeration types used throughout the railway management system.
These enums define the possible states, types, and directions for various railway components.
"""

from enum import Enum, IntEnum


class OperationMode(Enum):
    """The session's operating environment (design_v2.md "Operating
    environment and activation").

    Both environments use the same Hardware Setup and the same runtime step.
    Virtual Hardware drives an in-memory stand-in for the rig; Real Hardware
    drives the rig.
    """

    VIRTUAL_HARDWARE = "virtual_hardware"
    REAL_HARDWARE = "real_hardware"


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
    BLOCK_BOUNDARY = "block_boundary"  # 闭塞分区边界


class Direction(Enum):
    """Cardinal directions for train movement and track orientation

    Convention: Y positive is NORTH (up), Y negative is SOUTH (down).
    """

    NORTH = (0, 1)
    EAST = (1, 0)
    SOUTH = (0, -1)
    WEST = (-1, 0)


class BlockState(Enum):
    """Block section occupancy states"""

    FREE = "free"  # 空闲
    OCCUPIED = "occupied"  # 占用
    RESERVED = "reserved"  # 预约


class SessionStateValue(Enum):
    """The three values of the session state (design_v2.md "Session state").

    Read through `AppState.session_state()`; nothing stores it.
    """

    SETUP = "setup"  # Building and testing the rig.
    LIVE = "live"  # Trains may be driven; the whole runtime step runs.
    HALTED = "halted"  # Nothing drives; always carries a HaltReason.


class HaltReason(Enum):
    """Why a Halted session is halted (design_v2.md "Session state", The two
    halt reasons)."""

    HARDWARE_FAILURE = "hardware_failure"
    NOT_ACTIVATED = "not_activated"


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


