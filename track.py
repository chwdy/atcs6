"""
Track Segment Module

Definition:
This module defines the TrackSegment class which represents individual sections of railway track.
Each track segment has a type (straight, curve, switch, etc.), position, and connection rules
that determine how trains can move through them.

TODO List:
- [ ] Add track segment validation methods
- [x] Implement track wear and maintenance tracking
- [x] Add visual representation methods for track segments
- [x] Implement track segment serialization for save/load functionality
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
    
    # Maintenance and condition
    wear_level: float = 0.0  # 0.0 = new, 1.0 = broken
    last_maintenance: Optional[str] = None  # ISO format date
    maintenance_count: int = 0
    
    def degrade(self, amount: float):
        """Increase wear level"""
        self.wear_level = min(1.0, self.wear_level + amount)
        
    def repair(self):
        """Reset wear level"""
        self.wear_level = 0.0
        self.maintenance_count += 1
        from datetime import datetime
        self.last_maintenance = datetime.now().isoformat()
    
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

    def get_display_symbol(self) -> str:
        """Get a visual symbol representing this track segment"""
        if self.track_type == TrackType.STRAIGHT:
            if Direction.NORTH in self.connections and Direction.SOUTH in self.connections:
                return "│"
            return "─"
        elif self.track_type == TrackType.CURVE:
            if Direction.SOUTH in self.connections and Direction.EAST in self.connections:
                return "╭"
            elif Direction.SOUTH in self.connections and Direction.WEST in self.connections:
                return "╮"
            elif Direction.NORTH in self.connections and Direction.EAST in self.connections:
                return "╰"
            elif Direction.NORTH in self.connections and Direction.WEST in self.connections:
                return "╯"
            return "C"
        elif self.track_type == TrackType.SWITCH:
            return "S"
        elif self.track_type == TrackType.CROSSING:
            return "┼"
        elif self.track_type == TrackType.STATION:
            return "🚉"
        elif self.track_type == TrackType.SIGNAL:
            return "🚦"
        return "?"

    def to_dict(self) -> dict:
        """Convert track segment to dictionary for serialization"""
        data = {
            "x": self.x,
            "y": self.y,
            "type": self.track_type.name,
            "length": self.length,
            "occupied_by": self.occupied_by,
            "reserved_by": self.reserved_by,
            "block_section_id": self.block_section_id
        }
        
        if self.curve_radius:
            data["curve_radius"] = self.curve_radius
            
        if self.switch_position:
            data["switch_position"] = self.switch_position.name
            
        if self.signal_state:
            data["signal_state"] = self.signal_state.name
            data["signal_direction"] = self.signal_direction.name if self.signal_direction else None
            
        if self.station_name:
            data["station_name"] = self.station_name
            
        # Serialize connections
        # Map keys (Direction enum) to strings
        if self.connections:
            conns = {}
            for entry_dir, exit_dirs in self.connections.items():
                conns[entry_dir.name] = [d.name for d in exit_dirs]
            data["connections"] = conns
            
        return data