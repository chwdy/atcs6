"""
Railway System Enums

Definition:
This module contains all the enumeration types used throughout the railway management system.
These enums define the possible states, types, and directions for various railway components.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class TrackType(Enum):
    """Types of railway track segments

    Geometric types (single tile classification):
    - STRAIGHT: NS or EW axial path
    - DIAGONAL: NE, NW, SE, SW path (geometric shape, NOT a curve by itself)
    - SWITCH, DIAMOND, WYE, etc.: multi-path configurations

    Connection-based curve detection:
    - A CURVE is when connected paths are in DIFFERENT orientation groups
    - Groups: (NS), (EW), (NE+SW), (NW+SE)
    - Use TrackSegment.is_curve_connection(exit_edge, neighbor) to check
    """

    EMPTY = "empty"
    STRAIGHT = "straight"  # NS or EW axial path
    DIAGONAL = "diagonal"  # NE, NW, SE, SW path (geometric shape)
    CURVE = "curve"  # Connection-based: different orientation groups
    SWITCH = "switch"
    DIAMOND = "diamond"  # 2-path crossing without switches
    CROSSING = "diamond"  # Alias for compatibility
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


class SwitchPosition(Enum):
    """Railway switch/turnout positions"""

    MAIN = "main"
    DIVERGING = "diverging"
    LOCKED = "locked"  # 锁定状态，不可切换


class BlockState(Enum):
    """Block section occupancy states"""

    FREE = "free"  # 空闲
    OCCUPIED = "occupied"  # 占用
    RESERVED = "reserved"  # 预约
    CAUTION = "caution"  # 警告（列车刚离开，下一区段仍被占用，需减速）


class TrainState(Enum):
    """
    Train power and operational states.

    Per design_new.md, the canonical operational state set in v1 is:
    - OFF: power off
    - IDLE: powered on, no cab selected
    - STANDBY: powered on, cab selected, stopped, not dwelling
    - RUNNING_MOVING: depart accepted; physically moving or stopped at zero
      ATC clearance under the same live authority
    - RUNNING_AT_STATION: route-managed dwell at a confirmed stop task

    Manual driving is a control mode (control_mode == "manual"), not a
    separate operational state; a stopped manual train sits in STANDBY/IDLE
    just like an ATO train.
    """

    OFF = "off"
    IDLE = "idle"
    STANDBY = "standby"
    RUNNING_MOVING = "running_moving"
    RUNNING_AT_STATION = "running_at_station"


class PlatformSide(Enum):
    """Which side(s) of the track have platforms"""

    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"
    EITHER = "either"  # For flexible platform selection


class LeadingEnd(Enum):
    """Which end of the train is currently leading (head)"""

    A = "a"
    B = "b"


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

# Deceleration multipliers for brake notches
BRAKE_MULTIPLIERS = {
    HandlePosition.B1: 0.125,
    HandlePosition.B2: 0.25,
    HandlePosition.B3: 0.375,
    HandlePosition.B4: 0.5,
    HandlePosition.B5: 0.625,
    HandlePosition.B6: 0.75,
    HandlePosition.B7: 0.875,
    HandlePosition.B8: 1.0,
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
