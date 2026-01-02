"""
Signal module for the railway signaling system.

Signals control train movement by monitoring blocks, switches, or other signals.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any
from enums import SignalState


class SignalType(Enum):
    """Types of signals in the railway system."""
    BLOCK = "block"           # Monitors block occupancy
    SWITCH = "switch"         # Controls turnout position (future)
    PRE_SIGNAL = "pre_signal" # Shows next signal state (future)
    ROUTE = "route"           # Combined route/speed (future)


@dataclass
class Signal:
    """
    Represents a signal in the railway network.

    Attributes:
        signal_id: Unique identifier for the signal
        position: (x, y) grid coordinates where signal is placed
        detection_direction: Direction the signal monitors/detects (where trains travel TO).
            This is the direction behind the signal light, where the signal detects block activity.
        signal_type: Type of signal (BLOCK, SWITCH, etc.)
        state: Current signal state (RED, GREEN, YELLOW)
        name: Optional human-readable name
        monitored_block_id: ID of block this signal monitors (for BLOCK type)
        monitored_switch_id: ID of switch this signal controls (for SWITCH type)
        monitored_signal_id: ID of signal this signal monitors (for PRE_SIGNAL type)
        approaching_train_id: ID of train assigned to this signal by dispatcher.
            When set, if the monitored block is RESERVED for this train, signal shows GREEN.

    Properties:
        approach_direction: Direction the signal light faces (where trains approach FROM).
            Computed as the opposite of detection_direction.
    """
    signal_id: str
    position: Tuple[int, int]
    detection_direction: str  # Direction trains travel TO (stored, required)
    signal_type: SignalType = SignalType.BLOCK
    state: SignalState = SignalState.RED
    name: Optional[str] = None
    monitored_block_id: Optional[str] = None
    monitored_switch_id: Optional[str] = None
    monitored_signal_id: Optional[str] = None
    approaching_train_id: Optional[str] = None  # Train ID assigned by dispatcher

    def __post_init__(self):
        """Validate and normalize detection_direction after initialization."""
        valid_directions = {'NORTH', 'SOUTH', 'EAST', 'WEST'}
        if not self.detection_direction:
            raise ValueError(f"Signal {self.signal_id}: detection_direction is required")
        normalized = self.detection_direction.upper()
        if normalized not in valid_directions:
            raise ValueError(f"Signal {self.signal_id}: invalid detection_direction '{self.detection_direction}'. "
                           f"Must be one of {valid_directions}")
        # Store normalized uppercase version
        object.__setattr__(self, 'detection_direction', normalized)

    @property
    def approach_direction(self) -> str:
        """
        Direction the signal light faces (where trains approach FROM).
        Computed as the opposite of detection_direction.
        """
        opposites = {'NORTH': 'SOUTH', 'SOUTH': 'NORTH', 'EAST': 'WEST', 'WEST': 'EAST'}
        return opposites.get(self.detection_direction.upper(), self.detection_direction)

    def to_dict(self) -> Dict[str, Any]:
        """Convert signal to dictionary for JSON serialization."""
        return {
            'signal_id': self.signal_id,
            'x': self.position[0],
            'y': self.position[1],
            'detection_direction': self.detection_direction,
            'approach_direction': self.approach_direction,  # Computed property for frontend
            'signal_type': self.signal_type.value,
            'state': self.state.value,
            'name': self.name,
            'monitored_block_id': self.monitored_block_id,
            'monitored_switch_id': self.monitored_switch_id,
            'monitored_signal_id': self.monitored_signal_id,
            'approaching_train_id': self.approaching_train_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Signal':
        """Create a Signal from a dictionary."""
        return cls(
            signal_id=data['signal_id'],
            position=(data['x'], data['y']),
            detection_direction=data['detection_direction'],
            signal_type=SignalType(data.get('signal_type', 'block')),
            state=SignalState(data.get('state', 'red')),
            name=data.get('name'),
            monitored_block_id=data.get('monitored_block_id'),
            monitored_switch_id=data.get('monitored_switch_id'),
            monitored_signal_id=data.get('monitored_signal_id'),
            approaching_train_id=data.get('approaching_train_id'),
        )

    def update_from_block(self, block_occupied: bool) -> None:
        """
        Update signal state based on monitored block occupancy.

        For BLOCK type signals:
        - RED if block is occupied
        - GREEN if block is clear
        """
        if self.signal_type == SignalType.BLOCK:
            self.state = SignalState.RED if block_occupied else SignalState.GREEN
