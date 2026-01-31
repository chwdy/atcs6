"""
Railway Network Management

Definition:
This module provides the RailwayNetwork class which manages the complete railway infrastructure
including tracks, trains, block sections, and schedules. It serves as the central repository and
coordinator for all railway system components.
"""

from collections import deque
from typing import Dict, List, Optional, Tuple, Any, Union
import uuid
from track import TrackSegment
from train import Train, TrainSchedule
from block import Block
from block_manager import BlockManager
from enums import (
    TrackType,
    Direction,
    SwitchPosition,
    BlockState,
)
from railway_signal import Signal, SignalType, SignalState
from station import Station
from signal_validation import (
    validate_signal_placement,
    get_valid_directions,
    get_direction_vector,
    DIRECTION_VECTORS,
)


DEFAULT_TILE_LENGTH_CM = 50.0
DEFAULT_TRAIN_LENGTH_CM = 50.0


class RailwayNetwork:
    """
    Central railway network management system.

    Manages all railway infrastructure components including tracks, trains,
    block sections, and schedules.
    """

    def __init__(self):
        self.tracks: Dict[Tuple[int, int], TrackSegment] = {}
        self.trains: Dict[str, Train] = {}
        self.blocks: Dict[str, Block] = {}
        self.signals: Dict[str, Signal] = {}
        self.stations: Dict[str, Station] = {}
        self.switches: Dict[str, "Switch"] = {}  # NEW: Switch entity management
        self.schedules: Dict[str, TrainSchedule] = {}

        self.speed_profiles: Dict[str, "SpeedProfile"] = {}
        self.routes: Dict[str, "Route"] = {}

        self.block_manager: Optional[BlockManager] = None

    def _ensure_block_manager(self):
        if self.block_manager is None:
            self.block_manager = BlockManager(self.blocks)

    def add_track(self, track: TrackSegment):
        """Add a track segment to the network and propagate updates"""
        key = (track.x, track.y)
        self.tracks[key] = track

        # Propagate to this tile and neighbors
        self.propagate_from_tile(track.x, track.y)

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
            "N": (0, 1),  # North neighbor is at y+1
            "E": (1, 0),  # East neighbor is at x+1
            "S": (0, -1),  # South neighbor is at y-1
            "W": (-1, 0),  # West neighbor is at x-1
        }

        if edge not in offsets:
            return None

        dx, dy = offsets[edge]
        neighbor_key = (x + dx, y + dy)
        return self.tracks.get(neighbor_key)

    def _get_neighbor_coords(
        self, x: int, y: int, edge: str
    ) -> Optional[Tuple[int, int]]:
        """Get neighbor coordinates in the specified direction.

        Args:
            x, y: Current tile coordinates
            edge: Direction to look ('N', 'E', 'S', 'W')

        Returns:
            (nx, ny) tuple if valid direction, None otherwise
        """
        offsets = {
            "N": (0, 1),
            "E": (1, 0),
            "S": (0, -1),
            "W": (-1, 0),
        }

        if edge not in offsets:
            return None

        dx, dy = offsets[edge]
        return (x + dx, y + dy)

    def propagate_from_tile(self, x: int, y: int) -> None:
        """Propagate updates from a tile using BFS wave.

        This updates derived data (dead-ends, switch states) when:
        - A tile's track_bits changes
        - A tile is added or removed
        - A tile's switch_states changes

        The wave visits each affected tile once and calls propagate_update().
        """
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
            for edge in ["N", "E", "S", "W"]:
                neighbor = self.get_neighbor(tx, ty, edge)
                if neighbor and (neighbor.x, neighbor.y) not in visited:
                    queue.append((neighbor.x, neighbor.y))

    def remove_track(self, x: int, y: int) -> bool:
        """Remove track segment and propagate updates to neighbors.

        Also handles block lifecycle - if track was part of a block,
        checks connectivity and splits if needed.
        """
        key = (x, y)
        if key not in self.tracks:
            return False

        track = self.tracks[key]

        # Handle block lifecycle before deletion
        if track.block_section_id:
            # This will check connectivity and split if needed
            self.remove_tile_from_block(x, y)

        del self.tracks[key]

        # Propagate to neighbors (this tile is gone, so check each neighbor)
        # N=Y+, E=X+, S=Y-, W=X-
        offsets = {"N": (0, 1), "E": (1, 0), "S": (0, -1), "W": (-1, 0)}
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

    # === Centralized Block Management ===

    def create_block(self, block_id: str, name: str = "") -> Block:
        """
        Create a new empty block.

        Args:
            block_id: Unique identifier for the block
            name: Optional human-readable name

        Returns:
            Created Block instance

        Raises:
            ValueError: If block_id already exists
        """
        if block_id in self.blocks:
            raise ValueError(f"Block {block_id} already exists")
        block = Block(block_id=block_id, name=name)
        self.blocks[block_id] = block
        self._ensure_block_manager()
        return block

    def create_block_with_tracks(
        self,
        block_id: str,
        positions: List[Tuple[int, int]],
        name: str = "",
        length_cm: float = 0.0,
        max_speed_kmh: Optional[float] = None,
        validate: bool = True,
    ) -> Block:
        """
        Create a new block and assign tracks to it with full validation.

        This is the RECOMMENDED way to create blocks. Validates:
        - Block ID uniqueness
        - Block properties are valid
        - All positions have valid tracks
        - No tracks are switches, signals, or stations
        - No tracks are already in other blocks
        - All tracks form a continuous path

        Args:
            block_id: Unique block identifier
            positions: List of (x, y) track positions
            name: Human-readable block name
            length_cm: Physical track length
            max_speed_kmh: Optional speed limit
            validate: If True, perform full validation (recommended)

        Returns:
            Created Block instance

        Raises:
            ValueError: If validation fails

        Example:
            # Create a block with validated tracks
            block = network.create_block_with_tracks(
                block_id="BLOCK_MAIN_1",
                positions=[(0,0), (1,0), (2,0)],
                name="Main Line Section 1",
                length_cm=150.0,
                max_speed_kmh=80.0
            )
        """
        from block_validation import (
            validate_block_properties,
            validate_track_for_block,
            validate_block_assignment_continuity,
            validate_station_boundary,
        )

        # Validate block properties
        if validate:
            is_valid, errors = validate_block_properties(
                block_id, name, length_cm, max_speed_kmh
            )
            if not is_valid:
                raise ValueError(f"Invalid block properties: {', '.join(errors)}")

            # Validate station boundaries - blocks cannot cross station boundaries
            valid, error = validate_station_boundary(positions, self)
            if not valid:
                raise ValueError(f"Station boundary violation: {error}")

        # Check block doesn't already exist
        if block_id in self.blocks:
            raise ValueError(f"Block {block_id} already exists")

        # Validate all tracks
        if validate:
            for x, y in positions:
                track = self.get_track(x, y)
                is_valid, error = validate_track_for_block(track, self)
                if not is_valid:
                    raise ValueError(f"Track at ({x}, {y}): {error}")

                # Check not already in another block
                if track.block_section_id:
                    raise ValueError(
                        f"Track at ({x}, {y}) already in block {track.block_section_id}"
                    )

            # Validate continuity
            is_continuous, errors = validate_block_assignment_continuity(
                [], positions, self
            )
            if not is_continuous:
                raise ValueError(f"Block tracks not continuous: {', '.join(errors)}")

        # Create block
        block = self.create_block(block_id, name)
        block.length_cm = length_cm
        if max_speed_kmh:
            block.max_speed_kmh = max_speed_kmh

        # Assign tracks
        for x, y in positions:
            track = self.get_track(x, y)
            if track:
                track.block_section_id = block_id

        return block

    def extend_block(
        self,
        block_id: str,
        new_positions: List[Tuple[int, int]],
        force: bool = False,
        force_overwrite: bool = False,
    ) -> Block:
        """
        Extend an existing block with new track positions.

        Validates:
        - Block exists
        - All new tracks are valid for blocks (unless force=True for signals)
        - New tracks are adjacent to existing block
        - Extended block remains continuous

        Args:
            block_id: ID of block to extend
            new_positions: List of (x, y) positions to add
            force: If True, removes signals from tiles to allow block extension.
                   This allows extending a block past a signal boundary.
            force_overwrite: If True, allows overwriting tracks that belong to
                   other blocks (reassigns them to this block).

        Returns:
            Updated Block instance

        Raises:
            ValueError: If validation fails
        """
        from block_validation import (
            validate_track_for_block,
            validate_block_assignment_continuity,
            get_adjacent_positions,
            is_crossing,
        )

        if block_id not in self.blocks:
            raise ValueError(f"Block {block_id} not found")

        block = self.blocks[block_id]
        existing_positions = self.get_block_positions(block_id)

        # Track signals that need to be removed when force=True
        signals_to_remove = []
        # Track blocks that will be affected by force_overwrite
        overwritten_blocks = set()

        # Validate each new position
        for x, y in new_positions:
            track = self.get_track(x, y)
            if not track:
                raise ValueError(f"No track at position ({x}, {y})")

            # Check for crossings (never allowed, even with force)
            if is_crossing(track.track_bits):
                raise ValueError(
                    f"Track at ({x}, {y}): Cannot add crossing to block - crossings isolate paths"
                )

            # Check for switches (never allowed, even with force)
            if track.is_switch:
                raise ValueError(
                    f"Track at ({x}, {y}): Cannot add switch to block - switches are block boundaries"
                )

            # Check for signals - can be overridden with force
            # When force=True, we clear the signal's monitored block (shows warning triangle)
            # but we do NOT remove the signal itself
            # Only invalidate signals that monitor the block being extended (block_id)
            if track.signal_ids:
                if force:
                    # Collect signals to invalidate (clear monitored block)
                    # Only invalidate if the signal monitors the block we're extending
                    for signal_id in track.signal_ids:
                        signal = self.signals.get(signal_id)
                        if signal and signal.monitored_block_id == block_id:
                            signals_to_remove.append((signal_id, x, y))
                else:
                    raise ValueError(
                        f"Track at ({x}, {y}): Has signal(s) - use force=true to extend past signal"
                    )

            if track.block_section_id and track.block_section_id != block_id:
                if force_overwrite:
                    # Track the block that will lose this track
                    overwritten_blocks.add(track.block_section_id)
                else:
                    raise ValueError(
                        f"Track at ({x}, {y}) already in block {track.block_section_id}"
                    )

        # Check adjacency
        is_adjacent = False
        for new_x, new_y in new_positions:
            for old_x, old_y in existing_positions:
                if abs(new_x - old_x) + abs(new_y - old_y) == 1:
                    is_adjacent = True
                    break
            if is_adjacent:
                break

        if not is_adjacent and len(existing_positions) > 0:
            raise ValueError("New positions must be adjacent to existing block")

        # Validate station boundaries - block cannot cross station boundaries
        from block_validation import validate_station_boundary

        all_positions = existing_positions + new_positions
        valid, error = validate_station_boundary(all_positions, self)
        if not valid:
            raise ValueError(f"Station boundary violation: {error}")

        # Invalidate signals in force mode - clear their monitored block
        # This causes them to show an orange warning triangle (signal needs reconfiguration)
        invalidated_signals = []
        for signal_id, x, y in signals_to_remove:
            if signal_id in self.signals:
                signal = self.signals[signal_id]
                # detection_direction can be either a string or an enum, handle both
                detection_direction = signal.detection_direction
                if hasattr(detection_direction, "name"):
                    detection_direction = detection_direction.name
                invalidated_signals.append(
                    {
                        "signal_id": signal_id,
                        "position": [x, y],
                        "detection_direction": detection_direction,
                        "approach_direction": signal.approach_direction,
                        "previous_block": signal.monitored_block_id,
                    }
                )
                # Clear the monitored block - signal will show warning triangle
                signal.monitored_block_id = None
                # Set signal to RED since it's no longer monitoring a block
                signal.state = SignalState.RED

        # Validate continuity
        is_continuous, errors = validate_block_assignment_continuity(
            existing_positions, new_positions, self
        )
        if not is_continuous:
            raise ValueError(f"Extension would break continuity: {', '.join(errors)}")

        # Assign new tracks
        for x, y in new_positions:
            track = self.get_track(x, y)
            if track:
                track.block_section_id = block_id

        # Store invalidated signals info on block for API response
        block._invalidated_signals = invalidated_signals

        return block

    def delete_block(self, block_id: str) -> bool:
        """
        Delete block and clear references from all tracks.
        Also disconnects signals monitoring this block.

        Args:
            block_id: ID of block to delete

        Returns:
            True if deleted, False if not found
        """
        if block_id not in self.blocks:
            return False

        # Clear block_section_id from all tracks in this block
        for track in self.tracks.values():
            if track.block_section_id == block_id:
                track.block_section_id = None

        # Disconnect signals monitoring this block (don't delete them)
        for signal in self.signals.values():
            if signal.monitored_block_id == block_id:
                signal.monitored_block_id = None

        del self.blocks[block_id]
        return True

    def set_block_state(self, block_id: str, state: BlockState) -> bool:
        """
        Set the occupancy state of a block.

        This is the recommended way to change block state as it:
        - Validates the block exists
        - Updates the block's state property
        - Triggers signal recalculation for all signals monitoring this block
        - Updates track display colors (via signal updates)

        Args:
            block_id: Block identifier
            state: New state (FREE, RESERVED, OCCUPIED, CAUTION)

        Returns:
            True if successful, False if block not found

        Side Effects:
            - Updates block.state
            - Triggers signal recalculation for all signals monitoring this block
        """
        block = self.blocks.get(block_id)
        if not block:
            return False

        block.state = state

        # Trigger signal recalculation for affected signals
        self.update_signals_from_blocks()

        return True

    def cycle_block_state(self, block_id: str) -> Optional[BlockState]:
        """
        Cycle block state: FREE -> RESERVED -> OCCUPIED -> CAUTION -> FREE

        Used by UI right-click to manually cycle through states.

        Args:
            block_id: Block identifier

        Returns:
            New state after cycling, or None if block not found
        """
        block = self.blocks.get(block_id)
        if not block:
            return None

        # Define state cycle order
        state_cycle = [
            BlockState.FREE,
            BlockState.RESERVED,
            BlockState.OCCUPIED,
            BlockState.CAUTION,
        ]

        # Find current state index and advance
        try:
            current_index = state_cycle.index(block.state)
            next_index = (current_index + 1) % len(state_cycle)
        except ValueError:
            next_index = 0  # Default to FREE if unknown state

        new_state = state_cycle[next_index]
        self.set_block_state(block_id, new_state)

        return new_state

    def get_block(self, block_id: str) -> Optional[Block]:
        """Get block by ID"""
        return self.blocks.get(block_id)

    def get_block_tracks(self, block_id: str) -> List[TrackSegment]:
        """Get all tracks belonging to a block"""
        return [t for t in self.tracks.values() if t.block_section_id == block_id]

    def get_block_positions(self, block_id: str) -> List[Tuple[int, int]]:
        """Get grid positions of all tracks in block"""
        return [(t.x, t.y) for t in self.get_block_tracks(block_id)]

    def assign_track_to_block(
        self, x: int, y: int, block_id: Optional[str], validate_continuity: bool = True
    ) -> None:
        """
        Assign a track to a block with comprehensive validation.

        Args:
            x, y: Track position
            block_id: Block ID to assign (None to unassign)
            validate_continuity: If True, check that assignment maintains continuity

        Raises:
            ValueError: If validation fails with detailed error message
        """
        from block_validation import (
            validate_track_for_block,
            validate_block_assignment_continuity,
        )

        track = self.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # If assigning to a block
        if block_id:
            # Check block exists
            if block_id not in self.blocks:
                raise ValueError(f"Block {block_id} does not exist")

            # Validate track can be in a block
            is_valid, error = validate_track_for_block(track, self)
            if not is_valid:
                raise ValueError(f"Cannot assign track at ({x}, {y}) to block: {error}")

            # Check if already assigned to a different block
            if track.block_section_id and track.block_section_id != block_id:
                raise ValueError(
                    f"Track at ({x}, {y}) already assigned to block {track.block_section_id}. "
                    f"Unassign first or use reassign method."
                )

            # Validate continuity if requested
            if validate_continuity:
                existing_positions = self.get_block_positions(block_id)
                if len(existing_positions) > 0:
                    is_continuous, errors = validate_block_assignment_continuity(
                        existing_positions, [(x, y)], self
                    )
                    if not is_continuous:
                        raise ValueError(
                            f"Adding track at ({x}, {y}) would create discontinuous block: "
                            f"{', '.join(errors)}"
                        )

        # Assignment is valid - update
        track.block_section_id = block_id

    def validate_block_continuity(self, block_id: str) -> Tuple[bool, List[str]]:
        """
        Check if block tracks form a connected graph.

        Uses BFS to verify all tracks in the block are reachable from
        any starting track via edge connections.

        Args:
            block_id: Block ID to validate

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        tracks = self.get_block_tracks(block_id)
        if len(tracks) <= 1:
            return True, []

        positions = set((t.x, t.y) for t in tracks)
        visited = set()
        errors = []

        # BFS from first track
        start = tracks[0]
        to_visit = [(start.x, start.y)]

        while to_visit:
            pos = to_visit.pop()
            if pos in visited:
                continue
            visited.add(pos)

            track = self.get_track(*pos)
            if not track:
                continue

            # Get connected neighbors via edges
            for neighbor_pos in self._get_block_connected_neighbors(track):
                if neighbor_pos in positions and neighbor_pos not in visited:
                    to_visit.append(neighbor_pos)

        if len(visited) != len(positions):
            unvisited = positions - visited
            errors.append(f"Disconnected tracks at: {list(unvisited)}")
            return False, errors

        return True, []

    def _get_block_connected_neighbors(
        self, track: TrackSegment
    ) -> List[Tuple[int, int]]:
        """
        Get positions of tracks connected via edges.

        A neighbor is connected if this track has an edge pointing to it
        and the neighbor has the opposite edge pointing back.
        """
        neighbors = []
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        opposite_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}

        for edge in track.edges:
            if edge not in edge_offsets:
                continue
            dx, dy = edge_offsets[edge]
            neighbor_pos = (track.x + dx, track.y + dy)
            neighbor = self.get_track(*neighbor_pos)
            if neighbor:
                opp = opposite_edge[edge]
                if opp in neighbor.edges:
                    neighbors.append(neighbor_pos)
        return neighbors

    # === Centralized Signal Management ===

    def add_signal(
        self,
        x: int,
        y: int,
        detection_direction: str,
        signal_type: Union[SignalType, str] = SignalType.BLOCK,
        name: Optional[str] = None,
        auto_connect: bool = True,
        signal_id: Optional[str] = None,
    ) -> Signal:
        """
        Create and add a signal at the specified position.

        Args:
            x, y: Grid coordinates for signal placement
            detection_direction: Direction trains travel TO (NORTH, SOUTH, EAST, WEST)
            signal_type: Type of signal (BLOCK, SWITCH, etc.)
            name: Optional human-readable name
            auto_connect: If True, automatically find and connect to block ahead
            signal_id: Optional specific ID, auto-generated if not provided

        Returns:
            Created Signal instance

        Raises:
            ValueError: If position invalid, direction invalid, or signal already exists
        """
        # Convert string signal_type to enum if needed
        if isinstance(signal_type, str):
            signal_type = SignalType(signal_type.lower())

        # Check if track exists
        track = self.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # Check if tile is part of a station (signals not allowed on platform tiles)
        if track.station_id is not None:
            raise ValueError(
                f"Cannot place signal on station tile at ({x}, {y}). "
                f"Tile belongs to station {track.station_id}"
            )

        # Validate signal placement
        is_valid, error_msg = validate_signal_placement(
            track.track_bits, detection_direction.upper()
        )
        if not is_valid:
            raise ValueError(error_msg)

        # Check for existing signal in this detection direction
        existing = self.get_signal_at_direction(x, y, detection_direction.upper())
        if existing:
            raise ValueError(
                f"Signal already exists at ({x}, {y}) with detection direction {detection_direction}: {existing.signal_id}"
            )

        # Determine max signals based on track type:
        # - Diagonal tracks (NE=4, NW=8, SE=16, SW=32): 1 signal max (single position at center)
        # - Straight tracks (NS=1, EW=2): 2 signals max (two positions)
        is_diagonal = track.track_bits in (4, 8, 16, 32)
        max_signals = 1 if is_diagonal else 2

        if len(track.signal_ids) >= max_signals:
            if is_diagonal:
                raise ValueError(
                    f"Diagonal track at ({x}, {y}) can only have 1 signal (center position)"
                )
            else:
                raise ValueError(
                    f"Cannot add more than 2 signals per tile at ({x}, {y})"
                )

        # Generate signal ID if not provided
        if signal_id is None:
            signal_id = f"SIG_{x}_{y}_{uuid.uuid4().hex[:6]}"

        # NOTE: Signals CAN be placed on tiles that belong to blocks.
        # The signal tile remains in its block. The signal can monitor:
        # - Any block AHEAD (normal case)
        # - Its OWN block, but ONLY if positioned on the entry tile
        # (The entry tile validation is enforced in get_reachable_blocks and connect_signal_to_block)

        # Create signal
        signal = Signal(
            signal_id=signal_id,
            position=(x, y),
            detection_direction=detection_direction.upper(),
            signal_type=signal_type,
            state=SignalState.RED,
            name=name,
        )

        # Store signal first (required for get_reachable_blocks to work)
        self.signals[signal_id] = signal

        # Auto-connect to block if requested using proper pathfinding
        if auto_connect and signal_type == SignalType.BLOCK:
            # Use the same pathfinding logic as the UI dropdown
            reachable_blocks = self.get_reachable_blocks(signal_id)
            if reachable_blocks:
                # Connect to the nearest reachable block (first in sorted list)
                signal.monitored_block_id = reachable_blocks[0]

        # Calculate signal state based on monitored block (if connected)
        if signal.monitored_block_id and signal_type == SignalType.BLOCK:
            signal.state = self._calculate_signal_state(signal)

        # Add signal ID to track's signal_ids list (max 2 per tile)
        if signal.signal_id not in track.signal_ids:
            track.signal_ids.append(signal.signal_id)

        return signal

    def get_signal(self, signal_id: str) -> Optional[Signal]:
        """Get signal by ID."""
        return self.signals.get(signal_id)

    def get_signal_at(self, x: int, y: int) -> Optional[Signal]:
        """Get first signal at the specified position."""
        for signal in self.signals.values():
            if signal.position == (x, y):
                return signal
        return None

    def get_signals_at(self, x: int, y: int) -> List[Signal]:
        """Get all signals at the specified position."""
        return [s for s in self.signals.values() if s.position == (x, y)]

    def get_signal_at_direction(
        self, x: int, y: int, detection_direction: str
    ) -> Optional[Signal]:
        """Get signal at position with a specific detection direction."""
        for signal in self.signals.values():
            if (
                signal.position == (x, y)
                and signal.detection_direction == detection_direction.upper()
            ):
                return signal
        return None

    def remove_signal(self, signal_id: str) -> bool:
        """
        Remove signal by ID.

        Returns:
            True if removed, False if not found
        """
        signal = self.signals.get(signal_id)
        if not signal:
            return False

        # Remove signal ID from track's signal_ids list
        track = self.get_track(*signal.position)
        if track and signal_id in track.signal_ids:
            track.signal_ids.remove(signal_id)

        del self.signals[signal_id]
        return True

    def remove_signal_at(self, x: int, y: int) -> bool:
        """
        Remove signal at position.

        Returns:
            True if removed, False if not found
        """
        signal = self.get_signal_at(x, y)
        if signal:
            return self.remove_signal(signal.signal_id)
        return False

    def update_signal(self, signal_id: str, **kwargs) -> Optional[Signal]:
        """
        Update signal properties.

        Allowed updates:
            - state: SignalState or string
            - name: str
            - monitored_block_id: str
            - monitored_switch_id: str
            - monitored_signal_id: str

        Args:
            signal_id: Signal to update
            **kwargs: Properties to update

        Returns:
            Updated Signal or None if not found

        Raises:
            ValueError: If trying to change immutable properties
        """
        signal = self.signals.get(signal_id)
        if not signal:
            return None

        # Prevent changing immutable properties
        immutable = {"signal_id", "position", "direction"}
        for key in kwargs:
            if key in immutable:
                raise ValueError(f"Cannot change immutable property: {key}")

        # Update allowed properties
        if "state" in kwargs:
            state_val = kwargs["state"]
            if isinstance(state_val, str):
                state_val = SignalState(state_val.lower())
            signal.state = state_val

        if "name" in kwargs:
            signal.name = kwargs["name"]

        if "monitored_block_id" in kwargs:
            new_block_id = kwargs["monitored_block_id"]

            # Validate: If monitoring own block, signal MUST be on entry tile
            if new_block_id:
                signal_track = self.get_track(*signal.position)
                if signal_track and signal_track.block_section_id == new_block_id:
                    if not self.is_signal_on_entry_tile(signal):
                        raise ValueError(
                            f"Signal cannot monitor block '{new_block_id}' from the middle. "
                            f"Signal at {signal.position} must be on the entry tile of the block to monitor it."
                        )

            signal.monitored_block_id = new_block_id

            # Recalculate signal state based on new monitored block
            if signal.signal_type == SignalType.BLOCK:
                if signal.monitored_block_id:
                    # Connected to a block - calculate state based on block occupancy
                    new_state = self._calculate_signal_state(signal)
                else:
                    # Disconnected - show RED (no block to monitor)
                    new_state = SignalState.RED
                signal.state = new_state

        if "monitored_switch_id" in kwargs:
            signal.monitored_switch_id = kwargs["monitored_switch_id"]

        if "monitored_signal_id" in kwargs:
            signal.monitored_signal_id = kwargs["monitored_signal_id"]

        return signal

    def connect_signal_to_block(self, signal_id: str, block_id: str) -> bool:
        """
        Manually connect a signal to monitor a specific block.

        Args:
            signal_id: Signal to connect
            block_id: Block to monitor

        Returns:
            True if successful

        Raises:
            ValueError: If signal/block not found, or no valid path exists, or
                       signal is in the middle of its own block (not on entry tile)
        """
        signal = self.signals.get(signal_id)
        if not signal:
            raise ValueError(f"Signal '{signal_id}' not found")

        if block_id not in self.blocks:
            raise ValueError(f"Block '{block_id}' not found")

        # Get the signal's own block (the block at the signal's position)
        signal_track = self.get_track(*signal.position)
        signal_own_block = signal_track.block_section_id if signal_track else None

        # Check if trying to monitor own block
        if signal_own_block and block_id == signal_own_block:
            # Can only monitor own block if on entry tile
            if not self.is_signal_on_entry_tile(signal):
                raise ValueError(
                    f"Signal cannot monitor block '{block_id}' from the middle. "
                    f"Signal at {signal.position} must be on the entry tile of the block to monitor it."
                )
            # Signal is on entry tile - allowed to monitor own block
            # Skip the path validation since we're already in the block
        else:
            # Validate that a path exists from signal to target block
            if not self._can_reach_block(signal, block_id):
                raise ValueError(
                    f"No valid path exists from signal at {signal.position} "
                    f"(facing {signal.approach_direction}, trains travel {signal.detection_direction}) to block '{block_id}'. "
                    f"Check track connectivity and switch geometry."
                )

        signal.monitored_block_id = block_id

        # Recalculate signal state based on new monitored block
        if signal.signal_type == SignalType.BLOCK:
            signal.state = self._calculate_signal_state(signal)

        if signal_track:
            signal_track.signal_block_id = block_id

        return True

    def _can_reach_block(self, signal: Signal, target_block_id: str) -> bool:
        """
        Check if ANY path exists from signal to target block.

        Unlike _check_switch_routing which follows current switch states,
        this method explores ALL possible paths through switches to determine
        if the target block is reachable via any switch configuration.

        Uses BFS to explore all possible paths through the track network.

        Args:
            signal: The signal to start from
            target_block_id: The block we're trying to reach

        Returns:
            True if target block is reachable, False otherwise
        """
        x, y = signal.position
        # detection_direction is where trains travel TO
        travel_dir = signal.detection_direction.upper()
        # approach_direction is where trains come FROM (signal faces this way)
        approach_dir = signal.approach_direction.upper()

        # Edge offsets: edge letter -> (dx, dy) to move to adjacent tile
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}

        # Opposite edges for determining entry edge
        opposite_edges = {"N": "S", "S": "N", "E": "W", "W": "E"}

        # Convert travel direction to initial exit edge
        # detection_direction is already single letter (N/S/E/W)
        initial_exit = travel_dir[0] if travel_dir else None
        if not initial_exit:
            return False

        # BFS: queue of (x, y, exit_edge)
        # We use exit_edge to track which direction we're leaving each tile
        queue = deque([(x, y, initial_exit)])
        visited = set()

        while queue:
            curr_x, curr_y, exit_edge = queue.popleft()

            # Prevent infinite loops
            state = (curr_x, curr_y, exit_edge)
            if state in visited:
                continue
            visited.add(state)

            # Safety limit
            if len(visited) > 500:
                return False

            # Move to next tile based on exit edge
            offset = edge_offsets.get(exit_edge)
            if not offset:
                continue
            dx, dy = offset
            next_x, next_y = curr_x + dx, curr_y + dy

            # Get next track
            next_track = self.get_track(next_x, next_y)
            if not next_track:
                continue  # End of line in this direction

            # Check if we reached the target block
            if next_track.block_section_id == target_block_id:
                return True  # Path found!

            # Determine entry edge (opposite of how we exited previous tile)
            entry_edge = opposite_edges.get(exit_edge)
            if not entry_edge:
                continue

            # Check if track allows entry from this edge
            if entry_edge not in next_track.edges:
                continue  # Can't enter this track from this direction

            # Get ALL possible exits from this track (ignore switch state)
            # This explores all paths through switches
            all_exits = next_track.get_exits(entry_edge, use_switch_state=False)

            if not all_exits:
                continue  # Dead end

            # Add all possible exits to queue
            for next_exit in all_exits:
                queue.append((next_x, next_y, next_exit))

        return False  # No path found

    def _get_distance_to_block(
        self, signal: Signal, target_block_id: str
    ) -> Optional[int]:
        """
        Get the shortest path distance from signal to target block.

        Similar to _can_reach_block but returns the shortest path distance
        (in tiles) instead of just a boolean. Uses BFS which guarantees
        shortest path is found first.

        Args:
            signal: The signal to start from
            target_block_id: The block we're trying to reach

        Returns:
            Distance in tiles if reachable, None otherwise
        """
        x, y = signal.position
        # detection_direction is where trains travel TO
        detection_dir = signal.detection_direction.upper()
        # approach_direction is where trains come FROM (signal faces this way)
        approach_dir = signal.approach_direction.upper()

        # Get the track at signal position
        signal_track = self.get_track(x, y)
        if not signal_track:
            return None

        # Determine entry edge - trains enter from the approach direction
        # IMPORTANT: For diagonal tracks, approach_direction (geometric opposite) is wrong!
        # Diagonal tracks only connect two adjacent edges, not opposite edges.
        # e.g., NE diagonal connects N and E, not N and S
        # Solution: Use detection_direction to find the exit edge, then get all other edges as entry

        track_bits = signal_track.track_bits
        is_diagonal = track_bits in (4, 8, 16, 32)

        if is_diagonal:
            # For diagonal tracks, determine the two edges from track_bits
            diagonal_edges = {
                4: ("N", "E"),  # NE
                8: ("N", "W"),  # NW
                16: ("S", "E"),  # SE
                32: ("S", "W"),  # SW
            }
            edge1, edge2 = diagonal_edges.get(track_bits, ("N", "E"))

            # Detection direction tells us which edge the train exits from
            # Entry edge is the OTHER edge of the diagonal
            detection_edge = detection_dir[0]  # 'N', 'S', 'E', or 'W'
            entry_edge = edge2 if detection_edge == edge1 else edge1
        else:
            # For straight tracks, approach_direction is already single letter (N/S/E/W)
            entry_edge = approach_dir[0] if approach_dir else None
            if not entry_edge:
                return None

        # Get the actual exit edges from the signal's tile based on track geometry
        # This is crucial for diagonal tracks where exit != opposite of entry
        initial_exits = signal_track.get_exits(entry_edge, use_switch_state=False)
        if not initial_exits:
            return None

        # Edge offsets: edge letter -> (dx, dy) to move to adjacent tile
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}

        # Opposite edges for determining entry edge
        opposite_edges = {"N": "S", "S": "N", "E": "W", "W": "E"}

        # BFS: queue of (x, y, exit_edge, distance)
        queue = deque()

        # Start with all possible exits from the signal tile
        for exit_edge in initial_exits:
            queue.append((x, y, exit_edge, 0))

        visited = set()

        while queue:
            curr_x, curr_y, exit_edge, dist = queue.popleft()

            # Prevent infinite loops
            state = (curr_x, curr_y, exit_edge)
            if state in visited:
                continue
            visited.add(state)

            # Safety limit
            if len(visited) > 500:
                return None

            # Move to next tile based on exit edge
            offset = edge_offsets.get(exit_edge)
            if not offset:
                continue
            dx, dy = offset
            next_x, next_y = curr_x + dx, curr_y + dy

            # Get next track
            next_track = self.get_track(next_x, next_y)
            if not next_track:
                continue  # End of line in this direction

            # Check if we reached the target block
            if next_track.block_section_id == target_block_id:
                return dist + 1  # Path found! Return distance

            # Determine entry edge (opposite of how we exited previous tile)
            entry_edge = opposite_edges.get(exit_edge)
            if not entry_edge:
                continue

            # Check if track allows entry from this edge
            if entry_edge not in next_track.edges:
                continue  # Can't enter this track from this direction

            # Get ALL possible exits from this track (ignore switch state)
            all_exits = next_track.get_exits(entry_edge, use_switch_state=False)

            if not all_exits:
                continue  # Dead end

            # Add all possible exits to queue with incremented distance
            for next_exit in all_exits:
                queue.append((next_x, next_y, next_exit, dist + 1))

        return None  # No path found

    def get_reachable_blocks(self, signal_id: str) -> list:
        """
        Get all blocks that are reachable from a signal, sorted by path distance.

        This is used by the UI to populate the "block to monitor" dropdown
        with only valid options. Blocks are sorted from nearest to farthest.

        Note: A signal's own block is only included if the signal is on the
        entry tile of that block (first tile trains encounter when approaching).

        Args:
            signal_id: The signal ID

        Returns:
            List of block IDs that are reachable from this signal, sorted by distance
        """
        signal = self.signals.get(signal_id)
        if not signal:
            return []

        # Get the block at the signal's position (if any)
        x, y = signal.position
        track = self.get_track(x, y)
        own_block = track.block_section_id if track else None

        reachable_with_distance = []
        for block_id in self.blocks:
            # Check if this is the signal's own block
            if block_id == own_block:
                # Only include own block if signal is on the entry tile
                if not self.is_signal_on_entry_tile(signal):
                    continue  # Skip - signal is in the middle of this block
                # Signal is on entry tile - include own block with distance 0
                reachable_with_distance.append((0, block_id))
                continue

            # Get distance to block (None if not reachable)
            distance = self._get_distance_to_block(signal, block_id)
            if distance is not None:
                reachable_with_distance.append((distance, block_id))

        # Sort by distance (nearest first)
        reachable_with_distance.sort(key=lambda x: x[0])

        return [block_id for _, block_id in reachable_with_distance]

    def is_signal_on_entry_tile(self, signal: Signal) -> bool:
        """
        Check if signal is on the entry tile of its own block.

        The entry tile is the FIRST tile of the block that trains would encounter
        when approaching in the signal's detection direction. This is determined
        by the track geometry (track_bits), not simple offsets.

        Logic:
        1. Get the track's path at the detection edge
        2. The OTHER edge of this path is where trains approach FROM
        3. Check the neighbor tile in that approach direction
        4. If neighbor is NOT in the same block, this is the entry tile

        A signal on the entry tile of its block CAN monitor that block.
        A signal NOT on the entry tile (in the middle) CANNOT monitor that block.

        Args:
            signal: The Signal to check

        Returns:
            True if signal is on entry tile of its block, False otherwise
        """
        x, y = signal.position
        track = self.get_track(x, y)
        if not track or not track.block_section_id:
            return False  # Not in any block

        block_id = track.block_section_id
        detection_dir = signal.detection_direction.upper()

        # Get the path at the detection edge using track geometry
        # For simple tracks (signals can only be on simple tracks), this gives us
        # (detection_edge, approach_edge) where approach_edge is where trains come FROM
        path = track.get_path_at_edge(detection_dir)
        if not path:
            return (
                False  # No path at detection edge (shouldn't happen for valid signal)
            )

        # path = (entry_edge, exit_edge) - for our purposes, the other edge is approach edge
        # If detection_dir is the first element, the second is approach
        # If detection_dir is the second element, the first is approach
        if path[0] == detection_dir:
            approach_edge = path[1]
        else:
            approach_edge = path[0]

        # Get the neighbor tile in the approach direction
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        offset = edge_offsets.get(approach_edge)
        if not offset:
            return False

        dx, dy = offset
        neighbor = self.get_track(x + dx, y + dy)

        detect_offset = edge_offsets.get(detection_dir)
        if not detect_offset:
            return False
        detect_dx, detect_dy = detect_offset
        detect_neighbor = self.get_track(x + detect_dx, y + detect_dy)
        if not detect_neighbor or detect_neighbor.block_section_id != block_id:
            return False

        return neighbor is None or neighbor.block_section_id != block_id

    def _find_block_ahead(
        self, x: int, y: int, direction: str, exclude_block: Optional[str] = None
    ) -> Optional[str]:
        """
        Find the block ahead of a signal position in the direction of train travel.

        Signal semantics:
        - Signal "faces" a direction = the light is visible from that direction
        - Trains approach FROM the facing direction
        - Trains travel in the OPPOSITE direction (away from the signal face)
        - We search in the train's travel direction to find the block ahead

        Args:
            x, y: Signal position
            direction: Signal facing direction (trains come FROM this direction)
            exclude_block: Block ID to exclude (typically the signal's own block)

        Returns:
            Block ID if found, None otherwise
        """
        # Signal faces DIRECTION, trains come FROM that direction and travel OPPOSITE
        # So we look in the OPPOSITE direction for the block ahead
        opposite_directions = {
            "NORTH": "SOUTH",
            "SOUTH": "NORTH",
            "EAST": "WEST",
            "WEST": "EAST",
        }
        travel_direction = opposite_directions.get(direction.upper(), direction)
        vector = get_direction_vector(travel_direction)
        if not vector:
            return None

        dx, dy = vector
        current_x, current_y = x + dx, y + dy

        # Traverse up to 50 tiles looking for a block
        for _ in range(50):
            track = self.get_track(current_x, current_y)
            if not track:
                break

            # Found a block that's not the excluded one
            if track.block_section_id and track.block_section_id != exclude_block:
                return track.block_section_id

            # Continue in same direction (simplified - doesn't follow curves)
            current_x += dx
            current_y += dy

        return None

    def get_all_signals(self) -> List[Signal]:
        """Get all signals in the network."""
        return list(self.signals.values())

    def update_signals_from_blocks(self) -> None:
        """
        Update all BLOCK-type signals based on block occupancy and switch positions.

        Signal state logic:
        - RED: First block occupied OR switch doesn't route to monitored block
        - YELLOW: First block clear, but second block ahead is occupied
        - GREEN: Both blocks clear (or only one block exists)

        Call this after train positions change to update signal states.
        """
        for signal in self.signals.values():
            if signal.signal_type == SignalType.BLOCK and signal.monitored_block_id:
                # Determine signal state
                new_state = self._calculate_signal_state(signal)
                signal.state = new_state

    def _calculate_signal_state(self, signal: Signal) -> SignalState:
        """
        Calculate signal state based on block states and switch positions.

        Implements the comprehensive specification from Reference_Signals_and_Blocks.md Section 10.

        Signal Color Logic:
        - RED: No block assigned, block doesn't exist, switch blocks route,
               Block A is OCCUPIED, or Block A is RESERVED by different train
        - YELLOW: Block A is CAUTION, or Block A is FREE but Block B is not FREE
        - GREEN: Block A is FREE, OR Block A is RESERVED for this signal's approaching_train_id
                 AND (no Block B exists or Block B is FREE or reserved for same train)

        The key enhancement: When a block is RESERVED for the same train that is assigned
        to this signal (via approaching_train_id), the signal shows GREEN because the
        route is set for that train.

        Returns SignalState (RED, YELLOW, GREEN)
        """
        # Step 1: Check block assignment (fail-safe)
        if not signal.monitored_block_id:
            return SignalState.RED  # No block configured

        first_block = self.blocks.get(signal.monitored_block_id)
        if not first_block:
            return SignalState.RED  # Block doesn't exist

        # Step 2: Check if switch blocks route to monitored block
        if not self._check_switch_routing(signal):
            return SignalState.RED  # Switch is set against this route

        # Step 3: Evaluate Block A state (priority order)
        if first_block.state == BlockState.OCCUPIED:
            return SignalState.RED

        if first_block.state == BlockState.RESERVED:
            # Check if signal is on the SAME path as the monitored block
            # If so, the path is set up for the train to pass through - show GREEN
            # BUT: also verify that the path direction matches the signal's detection direction
            signal_track = self.get_track(*signal.position)
            if signal_track and signal_track.block_section_id:
                signal_block = self.blocks.get(signal_track.block_section_id)
                if (
                    signal_block
                    and signal_block.reserved_by_path
                    and signal_block.reserved_by_path == first_block.reserved_by_path
                ):
                    # Same path! But check if path direction matches signal direction
                    # A signal facing N should only show GREEN if path is going N at that tile
                    if signal_track.path_direction:
                        signal_dir = signal.detection_direction.upper()
                        path_dir = signal_track.path_direction.upper()
                        if signal_dir != path_dir:
                            # Path is going opposite direction to signal
                            return SignalState.RED
                    # Direction matches (or no direction set) - proceed to Block B check
                    pass  # Continue to Block B evaluation
                else:
                    # Different path or signal's block not reserved
                    # Check direction and train assignment
                    if first_block.reserved_direction:
                        signal_dir = signal.detection_direction.upper()
                        block_dir = first_block.reserved_direction.upper()
                        if signal_dir != block_dir:
                            return (
                                SignalState.RED
                            )  # Block reserved for opposite direction path

                    # Check if reserved for THIS signal's approaching train
                    if (
                        signal.approaching_train_id
                        and first_block.occupied_by == signal.approaching_train_id
                    ):
                        # Block is reserved for our train in same direction
                        pass  # Continue to Block B evaluation
                    else:
                        return SignalState.RED  # RESERVED for different train
            else:
                # Signal not on a block - use existing logic
                if first_block.reserved_direction:
                    signal_dir = signal.detection_direction.upper()
                    block_dir = first_block.reserved_direction.upper()
                    if signal_dir != block_dir:
                        return SignalState.RED

                if (
                    signal.approaching_train_id
                    and first_block.occupied_by == signal.approaching_train_id
                ):
                    pass  # Continue to Block B evaluation
                else:
                    return SignalState.RED

        elif first_block.state == BlockState.CAUTION:
            return SignalState.YELLOW  # CAUTION = train just left, proceed with caution

        # Block A is FREE or RESERVED for our train - check Block B (two-block lookahead)

        # Step 4: Find Block B
        second_block_id = self._find_next_block_after(
            signal.monitored_block_id, signal.detection_direction
        )

        if not second_block_id:
            return SignalState.GREEN  # No second block exists

        second_block = self.blocks.get(second_block_id)
        if not second_block:
            return SignalState.GREEN  # Block B doesn't exist

        # Step 5: Evaluate Block B state
        if second_block.state == BlockState.FREE:
            return SignalState.GREEN
        elif (
            second_block.state == BlockState.RESERVED
            and signal.approaching_train_id
            and second_block.occupied_by == signal.approaching_train_id
        ):
            # Block B is also reserved for our train
            return SignalState.GREEN
        else:
            # CAUTION, RESERVED (for other train), or OCCUPIED in Block B → YELLOW
            return SignalState.YELLOW

    def _check_switch_routing(self, signal: Signal) -> bool:
        """
        Check if there's a valid path from signal to its monitored block.

        This function traces the actual track connectivity (following switch states)
        rather than assuming a straight-line path.

        CRITICAL: Also checks if approach tile (switch) allows routing TO the signal.

        Returns True if route is clear, False if blocked by switch position or no path exists.
        """
        if not signal.monitored_block_id:
            return False  # No block to check

        # Get signal position and directions
        x, y = signal.position
        detection_dir = signal.detection_direction.upper()
        approach_dir = signal.approach_direction.upper()

        # Get the track at signal position
        signal_track = self.get_track(x, y)
        if not signal_track:
            return False  # No track at signal position

        # EARLY CHECK: If signal is already ON its monitored block, return True
        # This handles the case where a signal monitors its own block (common for
        # signals placed at block entry points looking in the direction of travel)
        if signal_track.block_section_id == signal.monitored_block_id:
            return True  # Signal is on the monitored block, no path tracing needed

        # Determine entry edge - trains enter from the approach direction
        # IMPORTANT: For diagonal tracks, approach_direction (geometric opposite) is wrong!
        # Diagonal tracks only connect two adjacent edges, not opposite edges.
        # Solution: Use detection_direction to find the exit edge, then get all other edges as entry

        track_bits = signal_track.track_bits
        is_diagonal = track_bits in (4, 8, 16, 32)

        if is_diagonal:
            # For diagonal tracks, determine the two edges from track_bits
            diagonal_edges = {
                4: ("N", "E"),  # NE
                8: ("N", "W"),  # NW
                16: ("S", "E"),  # SE
                32: ("S", "W"),  # SW
            }
            edge1, edge2 = diagonal_edges.get(track_bits, ("N", "E"))

            # Detection direction tells us which edge the train exits from
            # Entry edge is the OTHER edge of the diagonal
            detection_edge = detection_dir[0]  # 'N', 'S', 'E', or 'W'
            entry_edge = edge2 if detection_edge == edge1 else edge1
        else:
            # For straight tracks, use approach_direction (geometric opposite works here)
            # approach_dir is already a single letter ('N', 'S', 'E', 'W')
            entry_edge = approach_dir
            if not entry_edge:
                return True  # Can't determine, assume OK

        # NOTE: We intentionally do NOT check the approach switch here.
        # The approach switch is BEHIND the signal and doesn't affect whether
        # the signal can see its monitored block (which is in the DETECTION direction).
        #
        # A signal's state is determined by:
        # 1. The state of its monitored block (free/occupied/reserved)
        # 2. Whether there's a valid path from the signal to its monitored block
        #
        # The approach switch affects train ROUTING (whether trains can reach
        # the signal), but not the signal's indication of block status ahead.

        # Get actual exit edges from the signal's tile based on track geometry
        # This is crucial for diagonal tracks
        initial_exits = signal_track.get_exits(entry_edge, use_switch_state=True)
        if not initial_exits:
            return False  # Can't exit the signal tile

        # Edge offsets: direction -> (dx, dy)
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}

        # Try all possible initial exits
        for initial_exit in initial_exits:
            current_exit_edge = initial_exit
            current_x, current_y = x, y
            visited = set()

            # Trace path following actual track connectivity
            for _ in range(50):
                # Prevent infinite loops
                pos_key = (current_x, current_y, current_exit_edge)
                if pos_key in visited:
                    break  # Loop detected, try next initial exit
                visited.add(pos_key)

                # Move to next tile based on exit edge
                offset = edge_offsets.get(current_exit_edge)
                if not offset:
                    break  # Invalid edge
                dx, dy = offset
                next_x, next_y = current_x + dx, current_y + dy

                # Get next track
                next_track = self.get_track(next_x, next_y)
                if not next_track:
                    break  # End of line, try next initial exit

                # Check if we reached the monitored block
                if next_track.block_section_id == signal.monitored_block_id:
                    return True  # Path found!

                # Determine entry edge (opposite of how we exited previous tile)
                entry_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}.get(
                    current_exit_edge
                )
                if not entry_edge:
                    break

                # Check if track allows entry from this edge
                if entry_edge not in next_track.edges:
                    break  # Can't enter this track from this direction

                # Get exit edge(s) from this track
                # For switches, use current switch state
                exits = next_track.get_exits(entry_edge, use_switch_state=True)

                if not exits:
                    break  # No exit from this entry - dead end or blocked

                # Use the first available exit (switch state determines which one)
                current_exit_edge = (
                    exits[0] if isinstance(exits, list) else list(exits)[0]
                )
                current_x, current_y = next_x, next_y

        return False  # Couldn't find block via any path

    def _find_next_block_after(
        self, block_id: str, detection_direction: str
    ) -> Optional[str]:
        """
        Find the block after the given block (for two-block lookahead).

        This function traces actual track connectivity (following switch states)
        starting from the furthest edge of block_id in the travel direction.

        Args:
            block_id: The first block (Block A) to search beyond
            detection_direction: The direction trains travel TO

        Returns:
            The block_id of the next block (Block B), or None if not found
        """
        # detection_direction IS the travel direction
        travel_dir = detection_direction.upper()

        # Edge offsets: edge -> (dx, dy)
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}

        # Find all tracks in the given block
        block_tracks = [
            (pos, t) for pos, t in self.tracks.items() if t.block_section_id == block_id
        ]
        if not block_tracks:
            return None

        # Convert travel direction to exit edge
        exit_edge_for_travel = {
            "NORTH": "N",
            "SOUTH": "S",
            "EAST": "E",
            "WEST": "W",
        }.get(travel_dir.upper())
        if not exit_edge_for_travel:
            return None

        # Find block tracks that have an exit in the travel direction
        # These are candidates for "edge of block"
        edge_tracks = []
        for pos, track in block_tracks:
            if exit_edge_for_travel in track.edges:
                # Check if the next tile in this direction is NOT in this block
                offset = edge_offsets.get(exit_edge_for_travel)
                if offset:
                    next_x, next_y = pos[0] + offset[0], pos[1] + offset[1]
                    next_track = self.get_track(next_x, next_y)
                    if not next_track or next_track.block_section_id != block_id:
                        edge_tracks.append((pos, track))

        if not edge_tracks:
            # Fallback: use furthest track in travel direction
            vector = get_direction_vector(travel_dir)
            if not vector:
                return None
            dx, dy = vector
            furthest_pos = max(
                block_tracks, key=lambda pt: pt[0][0] * dx + pt[0][1] * dy
            )[0]
            furthest_track = self.get_track(*furthest_pos)
            if furthest_track:
                edge_tracks = [(furthest_pos, furthest_track)]

        # Try each edge track to find path to next block
        for start_pos, start_track in edge_tracks:
            # Start tracing from this track's exit
            current_x, current_y = start_pos
            current_exit_edge = exit_edge_for_travel
            visited = set()

            for _ in range(50):
                # Prevent infinite loops
                pos_key = (current_x, current_y, current_exit_edge)
                if pos_key in visited:
                    break  # Loop, try next edge track
                visited.add(pos_key)

                # Move to next tile
                offset = edge_offsets.get(current_exit_edge)
                if not offset:
                    break
                dx, dy = offset
                next_x, next_y = current_x + dx, current_y + dy

                # Get next track
                next_track = self.get_track(next_x, next_y)
                if not next_track:
                    break  # End of line

                # Found a different block!
                if (
                    next_track.block_section_id
                    and next_track.block_section_id != block_id
                ):
                    return next_track.block_section_id

                # Determine entry edge
                entry_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}.get(
                    current_exit_edge
                )
                if not entry_edge:
                    break

                # Check if track allows entry
                if entry_edge not in next_track.edges:
                    break

                # Get exit edge(s) following switch state
                exits = next_track.get_exits(entry_edge, use_switch_state=True)
                if not exits:
                    break

                # Use the first available exit
                current_exit_edge = (
                    exits[0] if isinstance(exits, list) else list(exits)[0]
                )
                current_x, current_y = next_x, next_y

        return None  # No next block found

    def add_train(self, train: Train):
        """Add a train to the network"""
        self.trains[train.train_id] = train

    def place_train(
        self,
        train: Train,
        tile_x: int,
        tile_y: int,
        direction: Optional[Direction] = None,
    ) -> Tuple[int, int, float]:
        """
        Place a train on a tile with proper positioning based on direction and length.

        Train placement rules:
        1. Train HEAD (end_a) is always placed at the EXIT edge of the specified tile
        2. Train tail (end_b) extends backward - MUST fit on available track
        3. Creates TrainPosition with both ends tracked precisely
        4. Occupies all blocks the train covers

        Args:
            train: Train to place
            tile_x, tile_y: Target tile coordinates
            direction: Direction the train will be heading (random if None)

        Returns:
            Tuple of (final_tile_x, final_tile_y, head_distance_cm)

        Raises:
            ValueError: If no track at position, or train doesn't fit on available track
        """
        from train import TrainPosition
        import random

        track = self.get_track(tile_x, tile_y)
        if not track:
            raise ValueError(f"No track at ({tile_x}, {tile_y})")

        # If no direction provided, pick one from available edges
        if direction is None:
            available_edges = list(track.edges)
            if not available_edges:
                raise ValueError(f"Track at ({tile_x}, {tile_y}) has no edges")
            edge = random.choice(available_edges)
            DIR_FROM_EDGE = {
                "N": Direction.NORTH,
                "E": Direction.EAST,
                "S": Direction.SOUTH,
                "W": Direction.WEST,
            }
            direction = DIR_FROM_EDGE[edge]

        DIR_TO_EDGE = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
        OPPOSITE_EDGE = {"N": "S", "S": "N", "E": "W", "W": "E"}
        exit_edge = DIR_TO_EDGE.get(direction.name, "E")

        if exit_edge not in track.edges:
            raise ValueError(
                f"Track at ({tile_x}, {tile_y}) does not support direction {direction.name}"
            )

        neighbor_coords = self._get_neighbor_coords(tile_x, tile_y, exit_edge)
        neighbor = self.get_track(*neighbor_coords)
        if not neighbor:
            raise ValueError(
                f"Cannot place train facing {direction.name}: no track at ({neighbor_coords[0]}, {neighbor_coords[1]}) "
                f"to connect to"
            )

        train_length_cm = self._get_train_length_cm(train)
        tile_length_cm = self._get_tile_length_cm(tile_x, tile_y)

        entry_edge = OPPOSITE_EDGE[exit_edge]

        # Head is at exit edge, so head_distance = tile_length
        head_tile = (tile_x, tile_y)
        head_distance_cm = tile_length_cm
        head_entry_edge = entry_edge

        # Calculate occupied tiles and tail position
        # This will raise ValueError if train doesn't fit
        (occupied_tiles, tail_tile, tail_distance_cm, tail_entry_edge) = (
            self._calculate_occupied_tiles(
                head_tile, head_distance_cm, head_entry_edge, train_length_cm
            )
        )

        # Calculate occupied blocks from occupied tiles
        occupied_blocks = []
        seen_blocks = set()
        for tile in occupied_tiles:
            tile_track = self.get_track(*tile)
            if tile_track and tile_track.block_section_id:
                block_id = tile_track.block_section_id
                if block_id not in seen_blocks:
                    occupied_blocks.append(block_id)
                    seen_blocks.add(block_id)

        # Create TrainPosition
        # Note: _calculate_occupied_tiles returns tiles in tail-to-head order,
        # but TrainPosition expects end_a to end_b order. Since end_a=head,
        # we need to reverse the list to get head-to-tail (end_a to end_b) order.
        occupied_tiles_end_a_to_b = list(reversed(occupied_tiles))
        train.position = TrainPosition(
            end_a_tile=head_tile,
            end_a_distance_cm=head_distance_cm,
            end_a_entry_edge=head_entry_edge,
            end_b_tile=tail_tile,
            end_b_distance_cm=tail_distance_cm,
            end_b_entry_edge=tail_entry_edge,
            leading_end="a",  # end_a is head
            facing_direction=direction,
            occupied_tiles=occupied_tiles_end_a_to_b,
            occupied_blocks=occupied_blocks,
        )

        train.facing_direction = direction

        # Occupy all blocks the train covers
        if self.block_manager and occupied_blocks:
            self.block_manager.occupy_blocks(occupied_blocks, train.train_id)
        else:
            for block_id in occupied_blocks:
                block = self.blocks.get(block_id)
                if block:
                    block.occupy(train.train_id)

        return (tile_x, tile_y, head_distance_cm)

    def _get_train_length_cm(self, train: Train) -> float:
        """Get train length in cm."""
        if train.consist:
            total_length = sum(car.length for car in train.consist)
            return total_length
        return DEFAULT_TRAIN_LENGTH_CM

    def _get_tile_length_cm(self, x: int, y: int) -> float:
        """
        Get physical length of a tile in cm.

        Returns DEFAULT_TILE_LENGTH_CM if track is unconfigured.
        """
        track = self.get_track(x, y)
        if not track:
            return DEFAULT_TILE_LENGTH_CM

        if track.is_switch:
            switch_config = (
                self.get_switch_at(x, y) if hasattr(self, "get_switch_at") else None
            )
            if switch_config:
                return switch_config.get_active_length(track.switch_state)
            # Switches without entity should have 0 length by design
            # Do NOT fall back to DEFAULT_TILE_LENGTH_CM
            return 0.0

        if track.block_section_id:
            block = self.blocks.get(track.block_section_id)
            if block and block.length_cm > 0:
                positions = self.get_block_positions(track.block_section_id)
                tile_count = len(positions) if positions else 1
                return block.length_cm / tile_count
            return DEFAULT_TILE_LENGTH_CM

        return DEFAULT_TILE_LENGTH_CM

    def _get_neighbor_coords(self, x: int, y: int, edge: str) -> Tuple[int, int]:
        """Get coordinates of neighbor tile in given direction."""
        offsets = {"N": (0, 1), "E": (1, 0), "S": (0, -1), "W": (-1, 0)}
        dx, dy = offsets.get(edge, (0, 0))
        return (x + dx, y + dy)

    def _is_connected(
        self, track1: TrackSegment, track2: TrackSegment, edge: str
    ) -> bool:
        """Check if two adjacent tracks are connected via the given edge."""
        if not track1 or not track2:
            return False
        opposite = {"N": "S", "E": "W", "S": "N", "W": "E"}
        return edge in track1.edges and opposite[edge] in track2.edges

    def _calculate_occupied_tiles(
        self,
        head_tile: Tuple[int, int],
        head_distance_cm: float,
        head_entry_edge: str,
        train_length_cm: float,
        path: Optional[List[Tuple[int, int, Direction]]] = None,
        path_index: int = 0,
    ) -> Tuple[List[Tuple[int, int]], Tuple[int, int], float, str]:
        """
        Calculate all tiles occupied by a train given its head position and length.

        Traces backward from head position through connected tiles to find where
        the tail (end_b) is located.

        Args:
            head_tile: (x, y) coordinates of tile where head is located
            head_distance_cm: Distance from entry edge to head on that tile (cm)
            head_entry_edge: Edge where head entered the tile (N/E/S/W)
            train_length_cm: Total train length (cm)
            path: Optional train path for correct backward tracing through switches
            path_index: Current index in path (head position)

        Returns:
            Tuple of:
            - occupied_tiles: List of (x, y) from tail to head
            - tail_tile: (x, y) coordinates where tail is located
            - tail_distance_cm: Distance from entry edge to tail on tail tile
            - tail_entry_edge: Edge where tail tile was entered from

        Raises:
            ValueError: If train is too long for available track
        """
        OPPOSITE_EDGE = {"N": "S", "E": "W", "S": "N", "W": "E"}
        DIR_TO_EDGE = {
            Direction.NORTH: "N",
            Direction.SOUTH: "S",
            Direction.EAST: "E",
            Direction.WEST: "W",
        }

        # Build path lookup: tile -> (entry_edge, exit_direction)
        # This helps us trace backward correctly through switches
        path_lookup: Dict[Tuple[int, int], str] = {}
        if path:
            for i, (px, py, pdir) in enumerate(path):
                if pdir and i <= path_index:
                    # Exit direction -> entry is opposite
                    exit_edge = DIR_TO_EDGE.get(pdir, "N")
                    entry_edge = OPPOSITE_EDGE.get(exit_edge, "S")
                    path_lookup[(px, py)] = entry_edge

        occupied = []
        accumulated_length = 0.0

        current_tile = head_tile
        current_distance = head_distance_cm
        current_entry_edge = head_entry_edge

        # First, account for the portion of the head tile behind the head
        head_tile_length = self._get_tile_length_cm(*head_tile)
        length_behind_head = current_distance  # Distance from entry to head

        occupied.append(current_tile)
        accumulated_length += length_behind_head

        if accumulated_length >= train_length_cm:
            # Tail is on same tile as head
            tail_distance = current_distance - train_length_cm
            return (occupied, current_tile, tail_distance, current_entry_edge)

        # Trace backward through tiles
        visited = {current_tile}
        max_iterations = 1000  # Safety limit

        for _ in range(max_iterations):
            # Get previous tile (in direction opposite to entry)
            backward_edge = current_entry_edge
            prev_coords = self._get_neighbor_coords(*current_tile, backward_edge)
            prev_track = self.get_track(*prev_coords)

            if not prev_track:
                # No track behind - train extends off layout
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"No track at {prev_coords} behind {current_tile}."
                )

            # Check connectivity
            opposite_edge = OPPOSITE_EDGE[backward_edge]
            current_track = self.get_track(*current_tile)
            if not (
                backward_edge in current_track.edges
                and opposite_edge in prev_track.edges
            ):
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"Tiles {current_tile} and {prev_coords} are not connected."
                )

            if prev_coords in visited:
                # Circular track - train wraps around
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"Circular track detected at {prev_coords}."
                )

            visited.add(prev_coords)

            # Get length of previous tile
            prev_tile_length = self._get_tile_length_cm(*prev_coords)

            # Check if tail is in this previous tile
            remaining_length = train_length_cm - accumulated_length

            if remaining_length <= prev_tile_length:
                # Tail is in this tile
                # Entry edge is where the train entered this tile when moving forward.
                # We traced backward from head through backward_edge to reach prev_tile.
                # So the train entered prev_tile from the same direction as backward_edge
                # (e.g., if we went back through W, train entered prev_tile from W)
                tail_entry_edge = backward_edge
                tail_distance = prev_tile_length - remaining_length

                # Insert at beginning (we're building tail-to-head list)
                occupied.insert(0, prev_coords)

                return (occupied, prev_coords, tail_distance, tail_entry_edge)

            # Continue to next previous tile
            accumulated_length += prev_tile_length
            occupied.insert(0, prev_coords)

            # Update for next iteration
            current_tile = prev_coords

            # Determine entry edge for prev_tile
            # The train exited prev_tile through opposite_edge to reach current_tile.
            # We need to find: which edge did the train ENTER prev_tile from?
            #
            # For switches: use get_entry_for_exit() which respects switch state
            # For simple track: use get_entry_for_exit() as well (works for all tracks)
            #
            # First try path lookup (most accurate for switches with path info)
            if prev_coords in path_lookup:
                current_entry_edge = path_lookup[prev_coords]
            else:
                # Use the track's get_entry_for_exit method which respects switch state
                entry_edge = prev_track.get_entry_for_exit(
                    opposite_edge, use_switch_state=True
                )
                if entry_edge:
                    current_entry_edge = entry_edge
                else:
                    # Fallback: if no valid entry found (shouldn't happen for valid paths)
                    current_entry_edge = opposite_edge

        # Should never reach here
        raise ValueError(
            f"Unable to calculate tail position after {max_iterations} iterations"
        )

    def get_occupied_tiles_rich(self, train: Train) -> List[Dict[str, Any]]:
        """
        Get rich information about all tiles occupied by a train.

        Returns detailed data for each tile the train occupies, useful for
        rendering train segments with correct proportions and block colors.

        Args:
            train: Train to get occupied tiles for

        Returns:
            List of dicts (ordered from end_a to end_b), each containing:
            - tile: (x, y) coordinates
            - block_id: Block ID if tile is in a block, else None
            - segment_start_cm: Distance from entry edge where train starts on this tile
            - segment_end_cm: Distance from entry edge where train ends on this tile
            - tile_length_cm: Total length of this tile
        """
        if not train.position:
            return []

        pos = train.position
        occupied_tiles = pos.occupied_tiles

        if not occupied_tiles:
            return []

        result = []
        num_tiles = len(occupied_tiles)

        # occupied_tiles is ordered from end_a to end_b
        # When leading_end="a": end_a=head, end_b=tail, so order is head-to-tail
        # When leading_end="b": end_a=tail, end_b=head, so order is tail-to-head
        is_head_first = pos.leading_end == "a"

        for i, tile in enumerate(occupied_tiles):
            tile_track = self.get_track(*tile)
            tile_length = self._get_tile_length_cm(*tile)

            block_id = tile_track.block_section_id if tile_track else None

            # Determine segment boundaries based on position in list
            is_first = i == 0
            is_last = i == num_tiles - 1

            if is_first and is_last:
                # Single tile - both ends on same tile
                if is_head_first:
                    # end_a (head) is at index 0, end_b (tail) also at index 0
                    segment_start = pos.tail_distance_cm
                    segment_end = pos.head_distance_cm
                else:
                    # end_a (tail) is at index 0, end_b (head) also at index 0
                    segment_start = pos.tail_distance_cm
                    segment_end = pos.head_distance_cm
            elif is_first:
                # First tile in list (end_a's tile)
                if is_head_first:
                    # end_a is head - this is head tile
                    segment_start = 0.0
                    segment_end = pos.head_distance_cm
                else:
                    # end_a is tail - this is tail tile
                    segment_start = pos.tail_distance_cm
                    segment_end = tile_length
            elif is_last:
                # Last tile in list (end_b's tile)
                if is_head_first:
                    # end_b is tail - this is tail tile
                    segment_start = pos.tail_distance_cm
                    segment_end = tile_length
                else:
                    # end_b is head - this is head tile
                    segment_start = 0.0
                    segment_end = pos.head_distance_cm
            else:
                # Middle tile - train occupies entire tile
                segment_start = 0.0
                segment_end = tile_length

            result.append(
                {
                    "tile": tile,
                    "block_id": block_id,
                    "segment_start_cm": segment_start,
                    "segment_end_cm": segment_end,
                    "tile_length_cm": tile_length,
                }
            )

        return result

    def reverse_train(self, train: Train) -> bool:
        """
        Reverse a train's direction of travel.

        This swaps which end is the leading end. Physical positions don't change,
        only the interpretation of which end is "front".

        Args:
            train: Train to reverse

        Returns:
            True if reversed, False if train has no position
        """
        if not train.position:
            return False

        train.position.reverse()
        train.facing_direction = train.position.facing_direction
        return True

    def remove_train(self, train_id: str) -> bool:
        train = self.trains.get(train_id)
        if not train:
            return False

        if train.position:
            track = self.get_track(*train.position.head_tile)
            if track:
                if track.occupied_by == train.train_id:
                    track.occupied_by = None
                if track.block_section_id:
                    block = self.blocks.get(track.block_section_id)
                    if block and block.occupied_by == train.train_id:
                        if self.block_manager:
                            self.block_manager.clear_block(track.block_section_id)
                        else:
                            block.release()

        self.trains.pop(train_id, None)
        self.update_signals_from_blocks()
        return True

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
            "total_blocks": len(self.blocks),
            "total_schedules": len(self.schedules),
        }

    # === Centralized Station Management ===

    def _is_track_on_straight_path(self, x: int, y: int) -> bool:
        """
        Check if a track tile is part of a logical straight path.

        This function allows stations to be placed on:
        1. Pure straight tracks (TRACK_NS or TRACK_EW)
        2. Simple diagonal tracks (NE, NW, SE, SW) that can form zigzag patterns

        Args:
            x, y: Grid coordinates of the track

        Returns:
            bool: True if the track is on a straight path (pure or diagonal)
        """
        from track import TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW

        track = self.get_track(x, y)
        if not track:
            return False

        # Pure straight tracks are always valid
        if track.track_bits in (TRACK_NS, TRACK_EW):
            return True

        # Simple diagonal tracks are also valid for zigzag patterns
        # The user can extend the station to cover multiple tiles forming the zigzag
        if track.track_bits in (TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW):
            return True

        # Complex tracks (multiple paths) are not valid
        return False

    def add_station(
        self,
        x: int,
        y: int,
        station_id: Optional[str] = None,
        name: str = "",
        length_cm: float = 0.0,
        platform_side: str = "both",
        top_speed_kmh: Optional[float] = None,
    ) -> Station:
        """
        Create and add a station at the specified position.

        Args:
            x, y: Grid coordinates for station placement
            station_id: Optional specific ID, auto-generated if not provided
            name: Human-readable station name
            length_cm: Physical track length in cm (independent of tile count)
            platform_side: 'left', 'right', or 'both'
            top_speed_kmh: Optional speed limit through the station

        Returns:
            Created Station instance

        Raises:
            ValueError: If position invalid, track is not straight, switch, signal exists, or station exists
        """
        from track import TRACK_NS, TRACK_EW

        # Check if track exists
        track = self.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # Cannot place station on switches
        if track.is_switch:
            raise ValueError(f"Cannot place station on switch at ({x}, {y})")

        # Stations can only be placed on straight paths (pure NS/EW or zigzag forming a straight line)
        if not self._is_track_on_straight_path(x, y):
            raise ValueError(
                f"Cannot place station at ({x}, {y}). Stations require tracks on straight paths (pure NS/EW or zigzag patterns forming straight lines)."
            )

        # Cannot place station on tile with signal
        if track.signal_ids:
            raise ValueError(f"Cannot place station on tile with signal at ({x}, {y})")

        # Check if tile is already part of another station
        if track.station_id is not None:
            raise ValueError(
                f"Tile ({x}, {y}) already belongs to station {track.station_id}"
            )

        # Generate station ID if not provided
        if station_id is None:
            station_id = f"STA_{uuid.uuid4().hex[:6].upper()}"

        # Handle block splitting if track belongs to a block
        # Stations are block boundaries, so placing a station may split the block
        original_block_id = track.block_section_id
        original_block = None
        length_per_tile_cm = (
            None  # None means no block, 0.0 means block with zero length
        )

        if original_block_id:
            # Get the original block before removal
            original_block = self.blocks.get(original_block_id)
            if original_block:
                # Calculate length per tile from the original block
                original_positions = self.get_block_positions(original_block_id)
                if original_positions:
                    length_per_tile_cm = original_block.length_cm / len(
                        original_positions
                    )
                    # Store this on the original block so child blocks can use it
                    original_block._length_per_tile_cm = length_per_tile_cm

            # Remove this tile from its block (triggers connectivity check/split)
            self.remove_tile_from_block(x, y)

        # Create station block for this station
        station_block_id = f"{station_id}_BLOCK"

        # Calculate station block length:
        # - If station was on a block, use calculated length per tile × 1 tile (may be 0)
        # - If not on a block, use the input platform length
        if length_per_tile_cm is not None:
            # Was on a block (even if that block had zero length)
            station_block_length_cm = length_per_tile_cm
        else:
            # Was not on a block, use input platform length
            station_block_length_cm = length_cm if length_cm > 0 else 0.0

        # Use station name in block name if provided, otherwise use station_id
        block_name = (
            f"Station Block - {name}" if name else f"Station Block - {station_id}"
        )

        station_block = self.create_block(block_id=station_block_id, name=block_name)

        # Set the calculated length on the block
        station_block.length_cm = station_block_length_cm

        # Inherit speed limit from platform to station block
        # ATC/ATO uses block's max_speed_kmh, not station's top_speed_kmh
        if top_speed_kmh is not None:
            station_block.max_speed_kmh = top_speed_kmh

        # Assign station tile to station block
        track.block_section_id = station_block_id

        # Create station
        from enums import PlatformSide

        platform_enum = PlatformSide(platform_side.lower())

        # Compute initial orientation from the first tile's track_bits
        from track import BIT_TO_GROUP

        initial_orientation = None
        for bit_idx, group in BIT_TO_GROUP.items():
            if track.track_bits & (1 << bit_idx):
                initial_orientation = group
                break

        station = Station(
            station_id=station_id,
            name=name,
            positions=[(x, y)],
            length_cm=length_cm,
            platform_side=platform_enum,
            top_speed_kmh=top_speed_kmh,
            initial_orientation=initial_orientation,
        )

        # Store station
        self.stations[station_id] = station

        # Update track's station reference
        track.station_id = station_id

        return station

    def get_station(self, station_id: str) -> Optional[Station]:
        """Get station by ID."""
        return self.stations.get(station_id)

    def delete_station(self, station_id: str) -> bool:
        """
        Delete station and clear references from tracks.
        Also deletes all blocks that are fully contained within this station.

        Args:
            station_id: ID of station to delete

        Returns:
            True if deleted, False if not found
        """
        if station_id not in self.stations:
            return False

        station = self.stations[station_id]

        # Collect all blocks that are fully within this station
        # A block is "within the station" if ALL its tiles have station_id matching this station
        blocks_to_delete = set()

        # Check all blocks to see if they're fully within this station
        for block_id in list(self.blocks.keys()):
            block_positions = self.get_block_positions(block_id)
            if not block_positions:
                continue

            # Check if ALL tiles in this block belong to this station
            all_in_station = True
            for bx, by in block_positions:
                track = self.get_track(bx, by)
                if not track or track.station_id != station_id:
                    all_in_station = False
                    break

            if all_in_station:
                blocks_to_delete.add(block_id)
                print(
                    f"[Station Delete] Block {block_id} is fully within station {station_id}, will be deleted"
                )

        # Delete all blocks that are fully within the station
        for block_id in blocks_to_delete:
            self.delete_block(block_id)
            print(f"[Station Delete] Deleted block {block_id}")

        # Clear station_id from all tracks in this station
        for x, y in station.positions:
            track = self.get_track(x, y)
            if track and track.station_id == station_id:
                track.station_id = None

        # Delete from storage
        del self.stations[station_id]
        return True

    def extend_station(
        self, station_id: str, new_positions: List[Tuple[int, int]]
    ) -> Station:
        """
        Add adjacent continuous tiles to existing station.

        Args:
            station_id: ID of station to extend
            new_positions: List of (x, y) positions to add

        Returns:
            Updated Station instance

        Raises:
            ValueError: If validation fails or platform would be discontinuous
        """
        if station_id not in self.stations:
            raise ValueError(f"Station {station_id} not found")

        station = self.stations[station_id]

        # Validate each new position
        for x, y in new_positions:
            track = self.get_track(x, y)
            if not track:
                raise ValueError(f"No track at position ({x}, {y})")

            if track.is_switch:
                raise ValueError(f"Cannot add switch at ({x}, {y}) to station")

            # Use same validation as add_station: straight or simple diagonal tracks
            if not self._is_track_on_straight_path(x, y):
                raise ValueError(
                    f"Cannot add track at ({x}, {y}) to station. Stations require tracks on straight paths (pure NS/EW or simple diagonal tracks)."
                )

            # Check orientation matches station's initial_orientation exactly
            from track import BIT_TO_GROUP

            new_tile_orientation = None
            for bit_idx, group in BIT_TO_GROUP.items():
                if track.track_bits & (1 << bit_idx):
                    new_tile_orientation = group
                    break

            if (
                station.initial_orientation
                and new_tile_orientation != station.initial_orientation
            ):
                raise ValueError(
                    f"Cannot extend {station.initial_orientation} station to tile at ({x}, {y}). "
                    f"Tile has {new_tile_orientation} orientation. "
                    f"Station extension requires exact orientation match."
                )

            if track.signal_ids:
                raise ValueError(
                    f"Cannot add tile with signal at ({x}, {y}) to station"
                )

            if track.station_id is not None and track.station_id != station_id:
                raise ValueError(
                    f"Tile ({x}, {y}) already belongs to station {track.station_id}"
                )

        # Check if new positions are adjacent to existing station
        # (simplified check - at least one new position must be adjacent to existing)
        is_adjacent = False
        for new_x, new_y in new_positions:
            for old_x, old_y in station.positions:
                if abs(new_x - old_x) + abs(new_y - old_y) == 1:
                    is_adjacent = True
                    break
            if is_adjacent:
                break

        if not is_adjacent and len(station.positions) > 0:
            raise ValueError("New positions must be adjacent to existing station")

        # Validate continuity (all positions would form connected path)
        all_positions = station.positions + new_positions
        is_continuous, errors = self._validate_station_continuity(all_positions)
        if not is_continuous:
            raise ValueError(f"Station would not be continuous: {', '.join(errors)}")

        # Add positions to station
        for x, y in new_positions:
            if (x, y) not in station.positions:
                station.add_position(x, y)
                track = self.get_track(x, y)
                if track:
                    # Handle block splitting if needed - use lifecycle method
                    if track.block_section_id:
                        self.remove_tile_from_block(x, y)
                    # Assign to station's block
                    station_block_id = f"{station_id}_BLOCK"
                    if station_block_id in self.blocks:
                        track.block_section_id = station_block_id
                    track.station_id = station_id

        return station

    def _validate_station_continuity(
        self, positions: List[Tuple[int, int]]
    ) -> Tuple[bool, List[str]]:
        """
        Validate that station positions form a continuous path.

        Uses BFS similar to block continuity validation.

        Args:
            positions: List of (x, y) tile positions

        Returns:
            Tuple of (is_continuous, error_messages)
        """
        if len(positions) == 0:
            return True, []

        if len(positions) == 1:
            return True, []

        errors = []
        position_set = set(positions)

        # Start BFS from first position
        start = positions[0]
        visited = {start}
        queue = [start]

        while queue:
            x, y = queue.pop(0)
            track = self.get_track(x, y)
            if not track:
                continue

            # Check all four neighbors
            for dx, dy in [(0, 1), (1, 0), (0, -1), (-1, 0)]:
                nx, ny = x + dx, y + dy
                neighbor_pos = (nx, ny)

                # Only consider positions that are in the station
                if neighbor_pos in position_set and neighbor_pos not in visited:
                    # Check if tracks are actually connected via edges
                    neighbor = self.get_track(nx, ny)
                    if neighbor:
                        # Simplified: just check if neighbor exists in position set
                        visited.add(neighbor_pos)
                        queue.append(neighbor_pos)

        # Check if all positions were visited
        if len(visited) != len(positions):
            unvisited = position_set - visited
            errors.append(f"Disconnected tiles: {unvisited}")
            return False, errors

        return True, []

    def _disconnect_signals_for_block(self, block_id: str) -> None:
        """
        Disconnect all signals monitoring the specified block.

        Sets monitored_block_id = None for all signals monitoring this block.

        Args:
            block_id: Block ID to disconnect signals from
        """
        for signal in self.signals.values():
            if signal.monitored_block_id == block_id:
                signal.monitored_block_id = None

    # === Unified Block Lifecycle Management ===

    def remove_tile_from_block(self, x: int, y: int) -> List[str]:
        """
        Remove a tile from its block and handle connectivity check.

        This is the core lifecycle method for block management. When a tile
        is removed from a block (due to station/signal placement, or track deletion),
        this method:
        1. Clears the tile's block_section_id
        2. Checks if remaining block tiles are still connected
        3. If disconnected, splits into new child blocks

        Args:
            x, y: Tile position to remove from its block

        Returns:
            List of affected block IDs (original + any new child blocks created)
        """
        track = self.get_track(x, y)
        if not track or not track.block_section_id:
            return []

        block_id = track.block_section_id
        track.block_section_id = None

        # Check and potentially split the block
        return self._check_block_connectivity(block_id)

    def remove_tiles_from_block(self, positions: List[Tuple[int, int]]) -> List[str]:
        """
        Remove multiple tiles from their block(s) and handle connectivity.

        This is a batch version of remove_tile_from_block for efficiency.
        All tiles should belong to the same block for proper splitting.

        Args:
            positions: List of (x, y) positions to remove

        Returns:
            List of affected block IDs
        """
        if not positions:
            return []

        # Group positions by block
        blocks_affected = {}
        for x, y in positions:
            track = self.get_track(x, y)
            if track and track.block_section_id:
                block_id = track.block_section_id
                if block_id not in blocks_affected:
                    blocks_affected[block_id] = []
                blocks_affected[block_id].append((x, y))
                track.block_section_id = None

        # Check connectivity for each affected block
        all_affected = []
        for block_id in blocks_affected:
            affected = self._check_block_connectivity(block_id)
            all_affected.extend(affected)

        return all_affected

    def _check_block_connectivity(self, block_id: str) -> List[str]:
        """
        Check block connectivity and split if disconnected.

        After tiles are removed from a block, this method:
        1. Gets remaining tile positions
        2. Finds connected components
        3. If multiple components, deletes original and creates child blocks
        4. Remaps orphaned signals to nearest reachable child block

        Args:
            block_id: Block ID to check

        Returns:
            List of affected block IDs (may include new child block IDs)
        """
        block = self.blocks.get(block_id)
        if not block:
            return []

        remaining_positions = self.get_block_positions(block_id)

        # Case 1: Block is now empty
        if not remaining_positions:
            self._disconnect_signals_for_block(block_id)
            self.delete_block(block_id)
            return [block_id]

        # Find connected components
        components = self._find_connected_components(remaining_positions)

        # Case 2: Single component - block is still connected
        if len(components) == 1:
            return [block_id]

        # Case 3: Multiple components - split into child blocks
        # Get signals monitoring this block before deletion
        orphaned_signals = [
            signal
            for signal in self.signals.values()
            if signal.monitored_block_id == block_id
        ]

        # Create child blocks
        new_block_ids = self._create_child_blocks(block, components)

        # Delete original block (but don't disconnect signals yet - we'll remap them)
        if block_id in self.blocks:
            del self.blocks[block_id]

        # Remap orphaned signals to nearest reachable child block
        self._remap_orphaned_signals(orphaned_signals, new_block_ids)

        return [block_id] + new_block_ids

    def _find_connected_components(
        self, positions: List[Tuple[int, int]]
    ) -> List[List[Tuple[int, int]]]:
        """
        Find connected components among tile positions using track connectivity.

        Two tiles are connected if:
        1. They are adjacent (Manhattan distance = 1)
        2. Their track_bits allow connection (one has edge pointing to other)

        Uses BFS to group tiles into connected components.

        Args:
            positions: List of (x, y) tile positions

        Returns:
            List of components, where each component is a list of positions
        """
        if not positions:
            return []

        position_set = set(positions)
        visited = set()
        components = []

        for start_pos in positions:
            if start_pos in visited:
                continue

            # BFS to find all connected tiles from this starting point
            component = []
            queue = [start_pos]
            visited.add(start_pos)

            while queue:
                current = queue.pop(0)
                component.append(current)

                # Find connected neighbors within the position set
                for neighbor in self._get_track_connected_neighbors(current):
                    if neighbor in position_set and neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

            components.append(component)

        return components

    def _get_track_connected_neighbors(
        self, pos: Tuple[int, int]
    ) -> List[Tuple[int, int]]:
        """
        Get positions of tracks connected via edges (track_bits connectivity).

        A neighbor is connected if:
        1. This track has an edge pointing toward the neighbor
        2. The neighbor has an edge pointing back

        Args:
            pos: Position to check neighbors for

        Returns:
            List of connected neighbor positions
        """
        track = self.get_track(*pos)
        if not track:
            return []

        neighbors = []
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        opposite_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}

        for edge in track.edges:
            if edge not in edge_offsets:
                continue
            dx, dy = edge_offsets[edge]
            neighbor_pos = (pos[0] + dx, pos[1] + dy)
            neighbor = self.get_track(*neighbor_pos)
            if neighbor:
                opp = opposite_edge[edge]
                if opp in neighbor.edges:
                    neighbors.append(neighbor_pos)

        return neighbors

    def _create_child_blocks(
        self, parent_block: Block, components: List[List[Tuple[int, int]]]
    ) -> List[str]:
        """
        Create child blocks from connected components, inheriting parent properties.

        Child blocks inherit:
        - name: "{parent.name}_1", "{parent.name}_2", etc.
        - length_cm: Calculated based on tile count × length_per_tile_cm from parent
        - min_radius_cm, max_speed_kmh (copied as-is)
        - state: Reset to FREE (original occupancy doesn't transfer cleanly)

        Args:
            parent_block: Original block being split
            components: List of connected components (each is list of positions)

        Returns:
            List of new block IDs
        """
        new_block_ids = []
        base_name = parent_block.name if parent_block.name else parent_block.block_id

        # Calculate length per tile from parent block
        # Check if parent has stored length_per_tile_cm (from station split)
        length_per_tile_cm = getattr(parent_block, "_length_per_tile_cm", None)

        if length_per_tile_cm is None:
            if (
                not hasattr(parent_block, "length_cm")
                or parent_block.length_cm is None
                or parent_block.length_cm <= 0
            ):
                parent_block.length_cm = 0.0
            parent_positions = self.get_block_positions(parent_block.block_id)
            if parent_positions and parent_block.length_cm > 0:
                length_per_tile_cm = parent_block.length_cm / len(parent_positions)
            else:
                total_tiles = sum(len(comp) for comp in components)
                if total_tiles > 0 and parent_block.length_cm > 0:
                    length_per_tile_cm = parent_block.length_cm / total_tiles
                else:
                    length_per_tile_cm = 0.0

        for i, component_positions in enumerate(components, start=1):
            # Generate child block ID and name
            child_id = f"{parent_block.block_id}_{i}"
            child_name = f"{base_name}_{i}"

            # Calculate child block length based on tile count
            child_length_cm = (
                len(component_positions) * length_per_tile_cm
                if length_per_tile_cm > 0
                else 0.0
            )

            # Create child block with inherited properties
            child_block = Block(
                block_id=child_id,
                name=child_name,
                length_cm=child_length_cm,  # Calculated based on tile count
                min_radius_cm=parent_block.min_radius_cm,
                max_speed_kmh=parent_block.max_speed_kmh,
                state=BlockState.FREE,  # Reset state
                occupied_by=None,
            )

            self.blocks[child_id] = child_block
            self._ensure_block_manager()

            for x, y in component_positions:
                track = self.get_track(x, y)
                if track:
                    track.block_section_id = child_id

            new_block_ids.append(child_id)

        return new_block_ids

    def _remap_orphaned_signals(
        self, orphaned_signals: List[Signal], new_block_ids: List[str]
    ) -> None:
        """
        Remap signals that monitored a deleted block to nearest reachable child block.

        For each orphaned signal, uses _can_reach_block to find which new block
        is reachable, and updates monitored_block_id accordingly.

        Args:
            orphaned_signals: List of signals that need remapping
            new_block_ids: List of child block IDs to consider
        """
        for signal in orphaned_signals:
            # Try to find a reachable block among the new child blocks
            found_block = None
            for block_id in new_block_ids:
                if self._can_reach_block(signal, block_id):
                    found_block = block_id
                    break

            # Update signal
            signal.monitored_block_id = found_block

            # Recalculate signal state based on new monitored block
            if signal.signal_type == SignalType.BLOCK:
                if signal.monitored_block_id:
                    signal.state = self._calculate_signal_state(signal)
                else:
                    signal.state = SignalState.RED

    # === Helper methods for track_bits-based construction ===
    def add_simple_straight_track(
        self, x: int, y: int, orientation: str
    ) -> TrackSegment:
        """Create straight track (NS or EW) using track_bits.

        Args:
            x, y: Tile coordinates
            orientation: 'NS' or 'EW'

        Returns:
            Created TrackSegment
        """
        from track import TRACK_NS, TRACK_EW

        track_bits = TRACK_NS if orientation == "NS" else TRACK_EW
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

        curve_map = {"NE": TRACK_NE, "NW": TRACK_NW, "SE": TRACK_SE, "SW": TRACK_SW}
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
        from track import (
            TRACK_NS,
            TRACK_EW,
            TRACK_NE,
            TRACK_NW,
            TRACK_SE,
            TRACK_SW,
            FORBIDDEN_TRACK_BITS,
        )

        path_map = {
            "NS": TRACK_NS,
            "EW": TRACK_EW,
            "NE": TRACK_NE,
            "NW": TRACK_NW,
            "SE": TRACK_SE,
            "SW": TRACK_SW,
        }

        track_bits = 0
        for path in paths:
            track_bits |= path_map.get(path, 0)

        if track_bits in FORBIDDEN_TRACK_BITS:
            raise ValueError(f"Forbidden track configuration: {track_bits}")

        track = TrackSegment(x, y, track_bits=track_bits)
        self.add_track(track)
        return track

    # === Centralized Switch Management ===

    def add_switch(self, switch: "Switch") -> None:
        """
        Register a switch configuration at a track tile.

        Args:
            switch: Switch entity to add

        Raises:
            ValueError: If no track at position or track is not a switch
        """
        track = self.get_track(switch.x, switch.y)
        if not track:
            raise ValueError(f"No track at ({switch.x}, {switch.y})")
        if not track.is_switch:
            raise ValueError(f"Track at ({switch.x}, {switch.y}) is not a switch")

        # Store by ID
        self.switches[switch.switch_id] = switch
        # Link track to switch
        track.switch_id = switch.switch_id

    def get_switch(self, switch_id: str) -> Optional["Switch"]:
        """Get switch configuration by ID."""
        return self.switches.get(switch_id)

    def get_switch_at(self, x: int, y: int) -> Optional["Switch"]:
        """Get switch configuration at a tile position."""
        for switch in self.switches.values():
            if switch.x == x and switch.y == y:
                return switch
        return None

    def remove_switch(self, switch_id: str) -> bool:
        """
        Remove switch configuration.

        Args:
            switch_id: Switch ID to remove

        Returns:
            True if removed, False if not found
        """
        switch = self.switches.get(switch_id)
        if not switch:
            return False

        # Clear reference on track tile
        track = self.get_track(switch.x, switch.y)
        if track:
            track.switch_id = None

        del self.switches[switch_id]
        return True

    def update_switch(self, switch_id: str, **kwargs) -> Optional["Switch"]:
        """
        Update switch configuration.

        Allowed updates:
            - main_length_cm: float
            - diverge_length_cm: float
            - main_radius_cm: float
            - diverge_radius_cm: float
            - name: str
            - notes: str
            - switch_type: str

        Args:
            switch_id: Switch to update
            **kwargs: Properties to update

        Returns:
            Updated Switch or None if not found
        """
        switch = self.switches.get(switch_id)
        if not switch:
            return None

        # Update allowed properties
        if "main_length_cm" in kwargs:
            switch.main_length_cm = float(kwargs["main_length_cm"])
        if "diverge_length_cm" in kwargs:
            switch.diverge_length_cm = float(kwargs["diverge_length_cm"])
        if "main_radius_cm" in kwargs:
            value = kwargs["main_radius_cm"]
            switch.main_radius_cm = float(value) if value is not None else None
        if "diverge_radius_cm" in kwargs:
            value = kwargs["diverge_radius_cm"]
            switch.diverge_radius_cm = float(value) if value is not None else None
        if "name" in kwargs:
            switch.name = kwargs["name"]
        if "notes" in kwargs:
            switch.notes = kwargs["notes"]
        if "switch_type" in kwargs:
            switch.switch_type = kwargs["switch_type"]

        return switch

    def to_dict(self) -> dict:
        """Convert network to dictionary for serialization"""
        # Enrich tracks with full signal data for frontend
        enriched_tracks = []
        for t in self.tracks.values():
            track_dict = t.to_dict()
            # Add full signal objects for frontend rendering
            track_dict["signals"] = [
                self.signals[sid].to_dict()
                for sid in t.signal_ids
                if sid in self.signals
            ]
            enriched_tracks.append(track_dict)

        return {
            "tracks": enriched_tracks,
            "trains": [t.to_dict(self) for t in self.trains.values()],
            "blocks": {k: v.to_dict() for k, v in self.blocks.items()},
            "signals": {k: v.to_dict() for k, v in self.signals.items()},
            "stations": [
                s.to_dict_with_orientation(self) for s in self.stations.values()
            ],
            "switches": {
                k: v.to_dict() for k, v in self.switches.items()
            },  # NEW: Switch persistence
        }

    def from_dict(self, data: dict):
        """Reconstruct network from dictionary"""
        # Clear existing
        self.tracks.clear()
        self.trains.clear()
        self.blocks.clear()
        self.signals.clear()
        self.stations.clear()
        self.switches.clear()  # NEW: Clear switches
        self.schedules.clear()

        for block_id, block_data in data.get("blocks", {}).items():
            self.blocks[block_id] = Block.from_dict(block_data)

        self._ensure_block_manager()

        for t_data in data.get("tracks", []):
            track = TrackSegment.from_dict(t_data)
            self.add_track(track)

        # Rebuild signals
        for signal_id, signal_data in data.get("signals", {}).items():
            signal = Signal.from_dict(signal_data)
            self.signals[signal_id] = signal
            # Update track's signal_ids list
            track = self.get_track(*signal.position)
            if track and signal_id not in track.signal_ids:
                track.signal_ids.append(signal_id)

        # Rebuild switches
        from switch import Switch

        for switch_id, switch_data in data.get("switches", {}).items():
            switch = Switch.from_dict(switch_data)
            self.switches[switch_id] = switch

        # Rebuild stations
        for station_data in data.get("stations", []):
            station = Station.from_dict(station_data)
            self.stations[station.station_id] = station

        # Rebuild trains
        for train_data in data.get("trains", []):
            train = Train.from_dict(train_data)
            self.trains[train.train_id] = train
