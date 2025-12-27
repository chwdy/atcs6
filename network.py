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
        """Add a track segment to the network and propagate updates"""
        key = (track.x, track.y)
        self.tracks[key] = track

        # Propagate to this tile and neighbors
        self.propagate_from_tile(track.x, track.y)
    
    def add_stub_track(self, x: int, y: int, block_id: Optional[str] = None):
        """
        Add a stub/isolated track point (no connections).
        track_bits = 0, displays as a dot on the map.
        
        Args:
            x, y: Track position
            block_id: Optional block section ID
        """
        track = TrackSegment(x, y)
        # No paths - track_bits = 0
        track.block_section_id = block_id
        self.add_track(track)
    
    # Legacy helper functions removed - use track_bits directly
    # For straight tracks: TrackSegment(x, y, track_bits=TRACK_EW or TRACK_NS)
    # For curves: TrackSegment(x, y, track_bits=TRACK_NE, TRACK_NW, TRACK_SE, or TRACK_SW)
    # For switches: TrackSegment(x, y, track_bits=TRACK_NS | TRACK_NE, etc.)
    # Dead-ends are auto-detected by propagation system

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

    def get_neighbor(self, x: int, y: int, edge: str) -> Optional[TrackSegment]:
        """Get neighbor tile in the specified direction.

        Args:
            x, y: Current tile coordinates
            edge: Direction to look ('N', 'E', 'S', 'W')

        Returns:
            TrackSegment if neighbor exists, None otherwise

        Coordinate system:
            N (North) = Y+ direction (0, +1)
            E (East)  = X+ direction (+1, 0)
            S (South) = Y- direction (0, -1)
            W (West)  = X- direction (-1, 0)
        """
        offsets = {
            'N': (0, 1),   # North neighbor is at y+1
            'E': (1, 0),   # East neighbor is at x+1
            'S': (0, -1),  # South neighbor is at y-1
            'W': (-1, 0)   # West neighbor is at x-1
        }

        if edge not in offsets:
            return None

        dx, dy = offsets[edge]
        neighbor_key = (x + dx, y + dy)
        return self.tracks.get(neighbor_key)

    def propagate_from_tile(self, x: int, y: int) -> None:
        """Propagate updates from a tile using BFS wave.

        This updates derived data (dead-ends, switch states) when:
        - A tile's track_bits changes
        - A tile is added or removed
        - A tile's switch_states changes

        The wave visits each affected tile once and calls propagate_update().
        """
        from collections import deque

        queue = deque([(x, y)])
        visited = set()

        while queue:
            tx, ty = queue.popleft()

            if (tx, ty) in visited:
                continue
            visited.add((tx, ty))

            tile = self.get_track(tx, ty)
            if not tile:
                continue

            # Update this tile's derived data
            tile.propagate_update(self)

            # Enqueue all neighbors for update
            for edge in ['N', 'E', 'S', 'W']:
                neighbor = self.get_neighbor(tx, ty, edge)
                if neighbor and (neighbor.x, neighbor.y) not in visited:
                    queue.append((neighbor.x, neighbor.y))

    def remove_track(self, x: int, y: int) -> bool:
        """Remove track segment and propagate updates to neighbors"""
        key = (x, y)
        if key not in self.tracks:
            return False

        del self.tracks[key]

        # Propagate to neighbors (this tile is gone, so check each neighbor)
        # N=Y+, E=X+, S=Y-, W=X-
        offsets = {'N': (0, 1), 'E': (1, 0), 'S': (0, -1), 'W': (-1, 0)}
        for edge, (dx, dy) in offsets.items():
            neighbor_key = (x + dx, y + dy)
            if neighbor_key in self.tracks:
                self.propagate_from_tile(x + dx, y + dy)

        return True
    
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

    # === Helper methods for track_bits-based construction ===
    def add_simple_straight_track(self, x: int, y: int, orientation: str) -> TrackSegment:
        """Create straight track (NS or EW) using track_bits.

        Args:
            x, y: Tile coordinates
            orientation: 'NS' or 'EW'

        Returns:
            Created TrackSegment
        """
        from track import TRACK_NS, TRACK_EW

        track_bits = TRACK_NS if orientation == 'NS' else TRACK_EW
        track = TrackSegment(x, y, track_bits=track_bits)
        self.add_track(track)
        return track

    def add_simple_curve_track(self, x: int, y: int, curve: str) -> TrackSegment:
        """Create curve track (NE, NW, SE, SW) using track_bits.

        Args:
            x, y: Tile coordinates
            curve: 'NE', 'NW', 'SE', or 'SW'

        Returns:
            Created TrackSegment
        """
        from track import TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW

        curve_map = {
            'NE': TRACK_NE,
            'NW': TRACK_NW,
            'SE': TRACK_SE,
            'SW': TRACK_SW
        }
        track_bits = curve_map.get(curve, 0)
        track = TrackSegment(x, y, track_bits=track_bits)
        self.add_track(track)
        return track

    def add_simple_switch_track(self, x: int, y: int, paths: list) -> TrackSegment:
        """Create switch track with multiple paths using track_bits.

        Args:
            x, y: Tile coordinates
            paths: List of path names like ['NS', 'NE']

        Returns:
            Created TrackSegment

        Raises:
            ValueError if combination is forbidden
        """
        from track import TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW, FORBIDDEN_TRACK_BITS

        path_map = {
            'NS': TRACK_NS,
            'EW': TRACK_EW,
            'NE': TRACK_NE,
            'NW': TRACK_NW,
            'SE': TRACK_SE,
            'SW': TRACK_SW
        }

        track_bits = 0
        for path in paths:
            track_bits |= path_map.get(path, 0)

        if track_bits in FORBIDDEN_TRACK_BITS:
            raise ValueError(f"Forbidden track configuration: {track_bits}")

        track = TrackSegment(x, y, track_bits=track_bits)
        self.add_track(track)
        return track

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
        
        # Rebuild tracks using TrackSegment.from_dict
        for t_data in data.get("tracks", []):
            track = TrackSegment.from_dict(t_data)
            self.add_track(track)
            
            # Re-register blocks if needed
            if track.block_section_id:
                if track.block_section_id not in self.block_sections:
                    # Create placeholder block if not exists
                    pass