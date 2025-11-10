"""
Railway System Enums

Definition:
This module contains all the enumeration types used throughout the railway management system.
These enums define the possible states, types, and directions for various railway components.

TODO List:
- [ ] Add validation methods for enum combinations
- [ ] Add enum conversion utilities
- [ ] Add documentation examples for each enum usage
- [ ] Consider adding enum flags for complex state combinations
"""

from enum import Enum


class TrackType(Enum):
    """Types of railway track segments"""
    STRAIGHT = "straight"
    CURVE = "curve" 
    SWITCH = "switch"
    SIGNAL = "signal"
    STATION = "station"
    BLOCK_BOUNDARY = "block_boundary"  # 闭塞分区边界


class Direction(Enum):
    """Cardinal directions for train movement and track orientation"""
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
            Direction.WEST: Direction.EAST
        }[self]


class SignalState(Enum):
    """Railway signal states for train control"""
    GREEN = "green"        # 允许通过
    RED = "red"           # 禁止通过
    YELLOW = "yellow"     # 注意，准备停车
    DOUBLE_YELLOW = "double_yellow"  # 下一个信号可能是红灯


class SwitchPosition(Enum):
    """Railway switch/turnout positions"""
    MAIN = "main"
    DIVERGING = "diverging"
    LOCKED = "locked"  # 锁定状态，不可切换


class BlockState(Enum):
    """Block section occupancy states"""
    FREE = "free"          # 空闲
    OCCUPIED = "occupied"  # 占用
    RESERVED = "reserved"  # 预约
    LOCKED = "locked"      # 锁定（用于联锁）


class TrainStatus(Enum):
    """Train operational status"""
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    AT_STATION = "at_station"
    DELAYED = "delayed"