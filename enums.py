"""
Railway System Enums

Definition:
This module contains all the enumeration types used throughout the railway management system.
These enums define the possible states, types, and directions for various railway components.
"""

from enum import Enum
from typing import Optional


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


class TrainStatus(Enum):
    """Train operational status (legacy - use TrainState for new code)"""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    AT_STATION = "at_station"
    DELAYED = "delayed"


class TrainState(Enum):
    """
    Train power and operational states for DC analog control.

    States determine voltage output and lighting:
    - OFF: 0V, no lights
    - IDLE: Lighting voltage, red lights both ends (if safe)
    - STANDBY: Lighting voltage, headlight/taillight, ready to depart
    - RUNNING_MOVING: Speed-based voltage, actively moving
    - RUNNING_WAITING: Lighting voltage, stopped waiting for path/signal
    - RUNNING_AT_STATION: Lighting voltage, dwelling at station
    - MANUAL: User-controlled speed, ATC protected
    """

    OFF = "off"  # 0V, no lights
    IDLE = "idle"  # Lighting V, red both ends if safe
    STANDBY = "standby"  # Lighting V, headlight/taillight, ready
    RUNNING_MOVING = "running_moving"  # Speed-based V, actively moving
    RUNNING_WAITING = "running_waiting"  # Lighting V, waiting for path/signal
    RUNNING_AT_STATION = "running_at_station"  # Lighting V, dwelling at station
    MANUAL = "manual"  # User-controlled speed, ATC protected


class PlatformSide(Enum):
    """Which side(s) of the track have platforms"""

    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"


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
