"""
Track Segment Module

Definition:
This module defines the TrackSegment class which represents individual sections of railway track.
Each track segment has a type (straight, curve, switch, etc.), position, and connection rules
that determine how trains can move through them.

TODO List:
- [ ] Add track segment validation methods
- [ ] Implement track wear and maintenance tracking
- [ ] Add visual representation methods for track segments
- [ ] Implement track segment serialization for save/load functionality
- [ ] Add support for track gradient and elevation
"""

from dataclasses import dataclass
from typing import Optional
from enums import TrackType, Direction, SignalState, SwitchPosition


@dataclass
class TrackSegment:
    """
    Railway track segment with position, type, and connection information.
    
    The connections dictionary maps entry_direction -> list of possible exit_directions
    where entry_direction is where the train enters FROM (opposite of movement direction).
    """
    x: int
    y: int
    track_type: TrackType
    length: float = 1.0
    connections: dict = None  # Dict[Direction, List[Direction]] - maps entry_direction -> list of possible exit_directions
    curve_radius: Optional[float] = None
    
    # Switch/turnout properties
    switch_position: Optional[SwitchPosition] = None
    main_route: Optional[tuple] = None  # (entry_direction, exit_direction)
    diverging_route: Optional[tuple] = None  # (entry_direction, exit_direction)
    
    # Signal properties
    signal_state: Optional[SignalState] = None
    signal_direction: Optional[Direction] = None
    
    # Occupancy tracking
    occupied_by: Optional[str] = None
    reserved_by: Optional[str] = None
    
    # Block system integration
    block_section_id: Optional[str] = None
    
    # Station properties
    station_name: Optional[str] = None
    
    def __post_init__(self):
        """Initialize connections dictionary if not provided"""
        if self.connections is None:
            self.connections = {}
    
    def is_occupied(self) -> bool:
        """Check if track segment is currently occupied"""
        return self.occupied_by is not None
    
    def is_reserved(self) -> bool:
        """Check if track segment is currently reserved"""
        return self.reserved_by is not None
    
    def is_available_for(self, train_id: str) -> bool:
        """Check if track is available for the specified train"""
        if self.occupied_by and self.occupied_by != train_id:
            return False
        if self.reserved_by and self.reserved_by != train_id:
            return False
        return True
    
    def can_enter_from(self, direction: Direction) -> bool:
        """Check if trains can enter this track from the specified direction"""
        return direction in self.connections
    
    def get_exit_directions(self, entry_direction: Direction) -> list:
        """Get possible exit directions for trains entering from specified direction"""
        return self.connections.get(entry_direction, [])