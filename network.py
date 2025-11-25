"""
Railway Network Management

Definition:
This module provides the RailwayNetwork class which manages the complete railway infrastructure
including tracks, trains, block sections, interlocking routes, and schedules. It serves as the
central repository and coordinator for all railway system components.

TODO List:
- [ ] Add network validation and integrity checking
- [ ] Implement network serialization for save/load
- [ ] Add network visualization export (to SVG, JSON, etc.)
- [ ] Implement network statistics and analytics
- [ ] Add support for multiple disconnected track sections
- [ ] Implement automatic signal placement
"""

from typing import Dict, List, Optional, Tuple
from track import TrackSegment
from train import Train, TrainSchedule
from blocking import BlockSection
from interlocking import InterlockingSystem
from enums import TrackType, Direction, SignalState, SwitchPosition


class RailwayNetwork:
    """
    Central railway network management system.
    
    Manages all railway infrastructure components including tracks, trains,
    block sections, interlocking, and schedules.
    """
    
    def __init__(self):
        self.tracks: Dict[Tuple[int, int], TrackSegment] = {}
        self.trains: Dict[str, Train] = {}
        self.block_sections: Dict[str, BlockSection] = {}
        self.interlocking: InterlockingSystem = InterlockingSystem()
        self.schedules: Dict[str, TrainSchedule] = {}
    
    def add_track(self, track: TrackSegment):
        """Add a track segment to the network"""
        self.tracks[(track.x, track.y)] = track
    
    def add_straight_track(self, x: int, y: int, direction: Direction, 
                          block_id: Optional[str] = None):
        """
        Add a straight track segment.
        
        Args:
            x, y: Track position
            direction: Track orientation (e.g., EAST for horizontal track)
            block_id: Optional block section ID
        """
        track = TrackSegment(x, y, TrackType.STRAIGHT)
        opposite = direction.opposite()
        # For straight tracks: entry_direction -> [exit_direction]
        # If train enters from WEST (moving EAST), it exits EAST
        track.connections = {
            opposite: [direction],  # Enter from opposite of orientation, exit in orientation
            direction: [opposite]   # Enter from orientation direction, exit in opposite
        }
        track.block_section_id = block_id
        self.add_track(track)
    
    def add_curve_track(self, x: int, y: int, entry_dir: Direction, 
                       exit_dir: Direction, radius: float,
                       block_id: Optional[str] = None):
        """
        Add a curved track segment.
        
        Args:
            x, y: Track position
            entry_dir: Direction from which trains enter
            exit_dir: Direction in which trains exit
            radius: Curve radius
            block_id: Optional block section ID
        """
        track = TrackSegment(
            x, y, TrackType.CURVE, 
            curve_radius=radius,
            block_section_id=block_id
        )
        track.connections = {
            entry_dir: [exit_dir],
            exit_dir.opposite(): [entry_dir.opposite()]
        }
        self.add_track(track)
    
    def add_switch(self, x: int, y: int, 
                   entry_dir: Direction,
                   main_exit: Direction, 
                   diverging_exit: Direction,
                   block_id: Optional[str] = None):
        """
        Add a railway switch/turnout.
        
        Args:
            x, y: Switch position
            entry_dir: Direction from which trains enter the switch
            main_exit: Main route exit direction
            diverging_exit: Diverging route exit direction
            block_id: Optional block section ID
        """
        track = TrackSegment(
            x, y, TrackType.SWITCH,
            switch_position=SwitchPosition.MAIN,
            block_section_id=block_id
        )
        
        track.main_route = (entry_dir, main_exit)
        track.diverging_route = (entry_dir, diverging_exit)
        
        track.connections = {
            entry_dir: [main_exit, diverging_exit],
            main_exit.opposite(): [entry_dir.opposite()],
            diverging_exit.opposite(): [entry_dir.opposite()]
        }
        
        self.add_track(track)
    
    def add_signal(self, x: int, y: int, direction: Direction, 
                   state: SignalState = SignalState.RED,
                   block_id: Optional[str] = None):
        """
        Add a railway signal.
        
        Args:
            x, y: Signal position
            direction: Direction the signal faces
            state: Initial signal state
            block_id: Optional block section ID
        """
        track = TrackSegment(
            x, y, TrackType.SIGNAL,
            signal_state=state,
            signal_direction=direction,
            block_section_id=block_id
        )
        
        opposite = direction.opposite()
        track.connections = {
            opposite: [direction],  # Enter from opposite, exit in signal direction
            direction: [opposite]   # Enter from signal direction, exit in opposite
        }
        self.add_track(track)
    
    def add_station(self, x: int, y: int, direction: Direction,
                   station_name: str, block_id: Optional[str] = None):
        """
        Add a railway station.
        
        Args:
            x, y: Station position
            direction: Station platform orientation
            station_name: Name of the station
            block_id: Optional block section ID
        """
        track = TrackSegment(
            x, y, TrackType.STATION,
            block_section_id=block_id
        )
        track.station_name = station_name
        
        opposite = direction.opposite()
        track.connections = {
            opposite: [direction],  # Enter from opposite, exit in direction
            direction: [opposite]   # Enter from direction, exit in opposite
        }
        self.add_track(track)
    
    def create_block_section(self, block_id: str, 
                           track_positions: List[Tuple[int, int]],
                           entry_signal: Tuple[int, int],
                           exit_signal: Tuple[int, int]) -> BlockSection:
        """
        Create a block section covering specified tracks.
        
        Args:
            block_id: Unique identifier for the block section
            track_positions: List of track positions in the block
            entry_signal: Entry signal position
            exit_signal: Exit signal position
            
        Returns:
            Created BlockSection instance
        """
        block = BlockSection(
            block_id=block_id,
            tracks=track_positions,
            entry_signal=entry_signal,
            exit_signal=exit_signal
        )
        self.block_sections[block_id] = block
        
        # Set block section ID for all tracks in the section
        for pos in track_positions:
            track = self.get_track(*pos)
            if track:
                track.block_section_id = block_id
        
        return block

    def auto_generate_blocks(self):
        """
        Automatically detect and create block sections based on signal placement.
        
        Algorithm:
        1. Find all signals.
        2. For each signal, traverse track in signal direction.
        3. Collect tracks until next signal or end of line.
        4. Create block section.
        """
        self.block_sections.clear()
        block_counter = 1
        
        # Find all signals
        signals = []
        for pos, track in self.tracks.items():
            if track.track_type == TrackType.SIGNAL:
                signals.append((pos, track))
                
        # Also consider track starts (dead ends) as implicit signals for block start?
        # For now, just start from explicit signals
        
        visited_tracks = set()
        
        for sig_pos, sig_track in signals:
            # Start traversal from this signal
            current_block_tracks = []
            entry_signal = sig_pos
            
            # Get initial direction from signal
            # Signal faces 'signal_direction', so trains travel in that direction
            direction = sig_track.signal_direction
            
            # Start BFS/Traversal
            queue = [(sig_pos, direction)]
            
            # We need to be careful not to cross into other blocks
            # But we might have branching (switches)
            # A block can contain switches
            
            # Simplified: Just trace one path for now. 
            # Real block detection with switches is complex (interlocking limits).
            # Let's assume blocks are bounded by signals.
            
            # Actually, we should traverse from the track *after* the signal
            # The signal itself is usually the entry to the block ahead
            
            # Get next track
            # Signal at (x,y) facing EAST. Train goes EAST. Next track is (x+1, y).
            dx, dy = direction.value
            next_x, next_y = sig_pos[0] + dx, sig_pos[1] + dy
            
            if (next_x, next_y) in self.tracks:
                queue = [((next_x, next_y), direction)]
            else:
                continue
                
            block_tracks = []
            exit_signal = None
            
            # Traverse
            while queue:
                curr_pos, curr_dir = queue.pop(0)
                if curr_pos in visited_tracks:
                    continue
                    
                track = self.get_track(*curr_pos)
                if not track:
                    continue
                
                # If we hit a signal facing the same way, it's the end of this block
                if track.track_type == TrackType.SIGNAL and track.signal_direction == curr_dir:
                    exit_signal = curr_pos
                    # Don't add this signal to the block (it belongs to next block?)
                    # Actually, usually the block ends *at* the signal.
                    # Let's say the block includes the track up to the signal.
                    # But the signal object itself controls entry to the *next* block.
                    break
                
                block_tracks.append(curr_pos)
                visited_tracks.add(curr_pos)
                
                # Get next
                # If switch, we might have multiple branches.
                # A block can split? Usually blocks are linear sections between interlockings.
                # If we hit a switch, we might be entering an interlocking.
                # For this simple demo, let's just follow the path.
                
                exits = track.get_exit_directions(curr_dir.opposite())
                for exit_dir in exits:
                    dx, dy = exit_dir.value
                    nx, ny = curr_pos[0] + dx, curr_pos[1] + dy
                    if (nx, ny) in self.tracks:
                        queue.append(((nx, ny), exit_dir))
            
            if block_tracks:
                block_id = f"AUTO_BLOCK_{block_counter}"
                self.create_block_section(block_id, block_tracks, entry_signal, exit_signal)
                block_counter += 1
    
    def get_track(self, x: int, y: int) -> Optional[TrackSegment]:
        """Get track segment at specified position"""
        return self.tracks.get((x, y))
    
    def is_track_available(self, x: int, y: int, train_id: str) -> bool:
        """Check if track is available for specified train"""
        track = self.get_track(x, y)
        if not track:
            return False
        return track.is_available_for(train_id)
    
    def get_block_section(self, track_pos: Tuple[int, int]) -> Optional[BlockSection]:
        """Get block section containing specified track position"""
        track = self.get_track(*track_pos)
        if track and track.block_section_id:
            return self.block_sections.get(track.block_section_id)
        return None
    
    def add_train(self, train: Train):
        """Add a train to the network"""
        self.trains[train.train_id] = train
    
    def get_train(self, train_id: str) -> Optional[Train]:
        """Get train by ID"""
        return self.trains.get(train_id)
    
    def add_schedule(self, schedule: TrainSchedule):
        """Add a train schedule to the network"""
        self.schedules[schedule.schedule_id] = schedule
    
    def visualization(self):
        """Simple text-based visualization of the network"""
        if not self.tracks:
            print("Empty network")
            return
        
        min_x = min(pos[0] for pos in self.tracks.keys())
        max_x = max(pos[0] for pos in self.tracks.keys())
        min_y = min(pos[1] for pos in self.tracks.keys())
        max_y = max(pos[1] for pos in self.tracks.keys())
        
        print(f"\nNetwork Layout ({max_x - min_x + 1} x {max_y - min_y + 1}):")
        for y in range(max_y, min_y - 1, -1):
            row = ""
            for x in range(min_x, max_x + 1):
                track = self.get_track(x, y)
                if track:
                    if track.track_type == TrackType.STRAIGHT:
                        row += "-"
                    elif track.track_type == TrackType.CURVE:
                        row += "╭"
                    elif track.track_type == TrackType.SWITCH:
                        row += "S"
                    elif track.track_type == TrackType.SIGNAL:
                        row += "O"
                    elif track.track_type == TrackType.STATION:
                        row += "▦"
                    else:
                        row += "?"
                else:
                    row += " "
            print(row)
    
    def get_network_stats(self) -> dict:
        """Get network statistics"""
        return {
            "total_tracks": len(self.tracks),
            "total_trains": len(self.trains),
            "total_blocks": len(self.block_sections),
            "total_routes": len(self.interlocking.routes),
            "total_schedules": len(self.schedules)
        }

    def to_dict(self) -> dict:
        """Convert network to dictionary for serialization"""
        return {
            "tracks": [t.to_dict() for t in self.tracks.values()],
            "trains": [t.to_dict() for t in self.trains.values()]
        }

    def from_dict(self, data: dict):
        """Reconstruct network from dictionary"""
        # Clear existing
        self.tracks.clear()
        self.trains.clear()
        self.block_sections.clear()
        self.interlocking.routes.clear()
        self.schedules.clear()
        
        # Rebuild tracks
        for t_data in data.get("tracks", []):
            x = t_data["x"]
            y = t_data["y"]
            t_type = TrackType[t_data["type"]]
            
            track = TrackSegment(x, y, t_type)
            track.length = t_data.get("length", 1.0)
            track.block_section_id = t_data.get("block_section_id")
            
            if "curve_radius" in t_data:
                track.curve_radius = t_data["curve_radius"]
                
            if "switch_position" in t_data:
                track.switch_position = SwitchPosition[t_data["switch_position"]]
                
            if "signal_state" in t_data:
                track.signal_state = SignalState[t_data["signal_state"]]
                if t_data.get("signal_direction"):
                    track.signal_direction = Direction[t_data["signal_direction"]]
                    
            if "station_name" in t_data:
                track.station_name = t_data["station_name"]
                
            # Rebuild connections
            if "connections" in t_data:
                track.connections = {}
                for entry, exits in t_data["connections"].items():
                    entry_dir = Direction[entry]
                    track.connections[entry_dir] = [Direction[e] for e in exits]
            
            self.add_track(track)
            
            # Re-register blocks if needed
            if track.block_section_id:
                if track.block_section_id not in self.block_sections:
                    # Create placeholder block if not exists
                    # In a real scenario, we might want to serialize blocks separately
                    pass