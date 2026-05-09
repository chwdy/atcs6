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
from switch_manager import SwitchManager
from resource_coordinator import ResourceCoordinator
from enums import (
    TrackType,
    Direction,
    LeadingEnd,
    SwitchPosition,
    BlockState,
)
from railway_signal import Signal, SignalType, SignalState
from station import Station
from railway_platform import Platform
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
        self.platforms: Dict[str, Platform] = {}
        self.stations = self.platforms
        self.station_groups: Dict[str, Station] = {}
        self.switches: Dict[str, "Switch"] = {}  # NEW: Switch entity management
        self.schedules: Dict[str, TrainSchedule] = {}

        self.speed_profiles: Dict[str, "SpeedProfile"] = {}

        self.block_manager: Optional[BlockManager] = None
        self.switch_manager: Optional[SwitchManager] = None
        self.resource_coordinator: Optional[ResourceCoordinator] = None
        self.power_controller: Optional["BlockPowerController"] = None

        # Reverse indices from tile -> owning block / platform.
        # Maintained in sync by network mutators (block creation, deletion,
        # platform creation, platform deletion, etc.). Readers should call
        # tile_to_block_id / tile_to_platform_id / tile_to_station_id below.
        self._tile_to_block: Dict[Tuple[int, int], str] = {}
        self._tile_to_platform: Dict[Tuple[int, int], str] = {}

        # Helper modules (signal computation, station registry, block
        # geometry mutation). Lazy-initialised on first access to keep
        # `__init__` cheap.
        self._signal_calculator = None

    def _ensure_block_manager(self):
        if self.block_manager is None:
            self.block_manager = BlockManager(self.blocks, network=self)

    def _get_signal_calculator(self):
        """Lazy-init `SignalCalculator`. The actual signal-state /
        reachability logic lives in `signal_calculator.py`; this network
        keeps thin forwarders so existing call sites stay unchanged."""
        if self._signal_calculator is None:
            from signal_calculator import SignalCalculator

            self._signal_calculator = SignalCalculator(self)
        return self._signal_calculator

    def _ensure_resource_managers(self):
        """Construct BlockManager, SwitchManager, and ResourceCoordinator if not
        yet wired. The coordinator is the dispatcher-facing facade per
        design_new.md "ResourceCoordinator"; constructed last because it
        composes the other two managers.
        """
        self._ensure_block_manager()
        if self.switch_manager is None:
            self.switch_manager = SwitchManager(self.switches, network=self)
        if self.resource_coordinator is None:
            self.resource_coordinator = ResourceCoordinator(
                self.block_manager, self.switch_manager
            )

    # === Tile -> owning entity reverse indices (v1) ===

    def tile_to_block_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning Block id, or None.

        Per design_new.md: TrackSegment does not store block membership.
        All "which block owns this tile?" lookups go through this method.
        """
        return self._tile_to_block.get((x, y))

    def tile_to_platform_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning Platform id, or None.

        Per design_new.md: TrackSegment does not store platform membership.
        Returns the concrete physical Platform that owns this tile.
        """
        return self._tile_to_platform.get((x, y))

    def tile_to_station_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning logical Station id.

        v1: a Station is a logical group of platforms. This resolves the
        platform first, then walks Station.platform_ids to find the group.
        Returns None if the tile has no platform or the platform is not in
        any station group.
        """
        platform_id = self._tile_to_platform.get((x, y))
        if platform_id is None:
            return None
        for station_id, station in self.station_groups.items():
            if platform_id in station.platform_ids:
                return station_id
        return None

    def _set_tile_block(self, x: int, y: int, block_id: Optional[str]) -> None:
        """Update the tile -> block reverse index.

        Network-internal helper. Use this whenever a tile's block membership
        changes (block creation, extension, deletion, station split, etc.).
        """
        key = (x, y)
        if block_id is None:
            self._tile_to_block.pop(key, None)
        else:
            self._tile_to_block[key] = block_id

    def _set_tile_platform(self, x: int, y: int, platform_id: Optional[str]) -> None:
        """Update the tile -> platform reverse index.

        Network-internal helper. Use this whenever a tile's platform
        membership changes (platform creation, extension, deletion).
        """
        key = (x, y)
        if platform_id is None:
            self._tile_to_platform.pop(key, None)
        else:
            self._tile_to_platform[key] = platform_id

    def add_track(self, track: TrackSegment):
        """Add a track segment to the network and propagate updates"""
        key = (track.x, track.y)
        self.tracks[key] = track

        # Move any pre-attachment local-fallback values for station_id /
        # block_section_id into the network reverse indices so that reads
        # via the property continue to see the same value after attach.
        local_block = track._block_section_id_local
        local_station = track._station_id_local
        track.network = self
        if local_block is not None:
            track.block_section_id = local_block
        if local_station is not None:
            track.station_id = local_station

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
        self._ensure_resource_managers()
        return block

    def create_block_with_tracks(
        self,
        block_id: str,
        positions: List[Tuple[int, int]],
        name: str = "",
        length_cm: float = 0.0,
        speed_limit_kmh: Optional[float] = None,
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
            speed_limit_kmh: Optional speed limit
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
                speed_limit_kmh=80.0
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
                block_id, name, length_cm, speed_limit_kmh
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
        if speed_limit_kmh:
            block.speed_limit_kmh = speed_limit_kmh

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
        Assign a track to a block, validating block membership and continuity.

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
        """Forward to `SignalCalculator.can_reach_block(signal, target)`."""
        return self._get_signal_calculator().can_reach_block(signal, target_block_id)

    def _get_distance_to_block(
        self, signal: Signal, target_block_id: str
    ) -> Optional[int]:
        """Forward to `SignalCalculator.distance_to_block(signal, target)`."""
        return self._get_signal_calculator().distance_to_block(signal, target_block_id)

    def get_reachable_blocks(self, signal_id: str) -> list:
        """Forward to `SignalCalculator.reachable_blocks(signal_id)`."""
        return self._get_signal_calculator().reachable_blocks(signal_id)

    def is_signal_on_entry_tile(self, signal: Signal) -> bool:
        """Forward to `SignalCalculator.is_signal_on_entry_tile(signal)`."""
        return self._get_signal_calculator().is_signal_on_entry_tile(signal)

    def _find_block_ahead(
        self, x: int, y: int, direction: str, exclude_block: Optional[str] = None
    ) -> Optional[str]:
        """Forward to `SignalCalculator.find_block_ahead(x, y, direction, exclude_block)`."""
        return self._get_signal_calculator().find_block_ahead(
            x, y, direction, exclude_block
        )

    def get_all_signals(self) -> List[Signal]:
        """Get all signals in the network."""
        return list(self.signals.values())

    def update_signals_from_blocks(self) -> None:
        """Forward to `SignalCalculator.update_all()`."""
        self._get_signal_calculator().update_all()

    def _calculate_signal_state(self, signal: Signal) -> SignalState:
        """Forward to `SignalCalculator.compute_state(signal)`."""
        return self._get_signal_calculator().compute_state(signal)

    def _check_switch_routing(self, signal: Signal) -> bool:
        """Forward to `SignalCalculator.check_switch_routing(signal)`."""
        return self._get_signal_calculator().check_switch_routing(signal)

    def _find_next_block_after(
        self, block_id: str, detection_direction: str
    ) -> Optional[str]:
        """Forward to `SignalCalculator.find_next_block_after(block_id, direction)`."""
        return self._get_signal_calculator().find_next_block_after(
            block_id, detection_direction
        )

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
            leading_end=LeadingEnd.A,  # end_a is head
            occupied_tiles=occupied_tiles_end_a_to_b,
            occupied_blocks=occupied_blocks,
        )

        # facing_direction is now calculated from position geometry

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
            total_length = sum(car.length_cm for car in train.consist)
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
        # When leading_end==LeadingEnd.A: end_a=head, end_b=tail, so order is head-to-tail
        # When leading_end==LeadingEnd.B: end_a=tail, end_b=head, so order is tail-to-head
        is_head_first = pos.leading_end == LeadingEnd.A

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
    ) -> Platform:
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
        # ATC/ATO uses block's speed_limit_kmh, not station's top_speed_kmh
        if top_speed_kmh is not None:
            station_block.speed_limit_kmh = top_speed_kmh

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

        station = Platform(
            platform_id=station_id,
            name=name,
            positions=[(x, y)],
            length_cm=length_cm,
            platform_side=platform_enum,
            top_speed_kmh=top_speed_kmh,
            initial_orientation=initial_orientation,
        )

        # Store physical platform (legacy station semantics)
        self.platforms[station_id] = station

        # Update track's station reference
        track.station_id = station_id

        return station

    def add_platform(
        self,
        x: int,
        y: int,
        platform_id: Optional[str] = None,
        name: str = "",
        length_cm: float = 0.0,
        platform_side: str = "both",
        top_speed_kmh: Optional[float] = None,
    ) -> Platform:
        """Compatibility wrapper: create physical platform via station implementation."""
        return self.add_station(
            x=x,
            y=y,
            station_id=platform_id,
            name=name,
            length_cm=length_cm,
            platform_side=platform_side,
            top_speed_kmh=top_speed_kmh,
        )

    def get_station(self, station_id: str) -> Optional[Platform]:
        """Get physical platform by legacy station ID."""
        return self.platforms.get(station_id)

    def get_platform(self, platform_id: str) -> Optional[Platform]:
        """Get physical platform by ID."""
        return self.platforms.get(platform_id)

    def delete_station(self, station_id: str) -> bool:
        """
        Delete station and clear references from tracks.
        Also deletes all blocks that are fully contained within this station.

        Args:
            station_id: ID of station to delete

        Returns:
            True if deleted, False if not found
        """
        if station_id not in self.platforms:
            return False

        station = self.platforms[station_id]

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
        del self.platforms[station_id]

        # Remove this platform from any logical station groups
        for station_group in self.station_groups.values():
            if station_id in station_group.platform_ids:
                station_group.remove_platform(station_id)
        return True

    def delete_platform(self, platform_id: str) -> bool:
        """Compatibility wrapper for physical platform deletion."""
        return self.delete_station(platform_id)

    def extend_station(
        self, station_id: str, new_positions: List[Tuple[int, int]]
    ) -> Platform:
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
        if station_id not in self.platforms:
            raise ValueError(f"Station {station_id} not found")

        station = self.platforms[station_id]

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
        station_block_id = f"{station_id}_BLOCK"

        for x, y in new_positions:
            if (x, y) not in station.positions:
                station.add_position(x, y)
                track = self.get_track(x, y)
                if track:
                    # Handle block splitting if needed - use lifecycle method
                    if track.block_section_id:
                        self.remove_tile_from_block(x, y)
                    # Assign to station's block
                    if station_block_id in self.blocks:
                        track.block_section_id = station_block_id
                    track.station_id = station_id

        return station

    def extend_platform(
        self, platform_id: str, new_positions: List[Tuple[int, int]]
    ) -> Platform:
        """Compatibility wrapper for physical platform extension."""
        return self.extend_station(platform_id, new_positions)

    def get_station_for_platform(self, platform_id: str) -> Optional[str]:
        """Get logical station ID for a platform if station groups exist."""
        for station_id, station in self.station_groups.items():
            if platform_id in station.platform_ids:
                return station_id
        return None

    def get_platforms_for_station(self, station_id: str) -> List[Platform]:
        """Get physical platforms for a logical station."""
        station = self.station_groups.get(station_id)
        if not station:
            return []

        result = []
        for pid in station.platform_ids:
            platform = self.platforms.get(pid)
            if platform:
                result.append(platform)
        return result

    def create_station_group(
        self,
        station_id: Optional[str] = None,
        name: str = "",
        platform_ids: Optional[List[str]] = None,
    ) -> Station:
        """Create a logical station group for one or more platforms."""
        if station_id is None:
            station_id = f"STA_{uuid.uuid4().hex[:6].upper()}"
        if station_id in self.station_groups:
            raise ValueError(f"Station {station_id} already exists")

        normalized_platform_ids: List[str] = []
        for platform_id in platform_ids or []:
            if platform_id not in self.platforms:
                raise ValueError(f"Platform {platform_id} not found")
            existing_station_id = self.get_station_for_platform(platform_id)
            if existing_station_id:
                raise ValueError(
                    f"Platform {platform_id} already belongs to station {existing_station_id}"
                )
            if platform_id not in normalized_platform_ids:
                normalized_platform_ids.append(platform_id)

        station = Station(
            station_id=station_id,
            name=name,
            platform_ids=normalized_platform_ids,
        )
        self.station_groups[station_id] = station
        return station

    def delete_station_group(self, station_id: str) -> bool:
        """Delete a logical station group."""
        if station_id not in self.station_groups:
            return False
        del self.station_groups[station_id]
        return True

    def add_platform_to_station_group(self, station_id: str, platform_id: str) -> bool:
        """Add a platform to a logical station group."""
        station = self.station_groups.get(station_id)
        if not station:
            raise ValueError(f"Station {station_id} not found")
        if platform_id not in self.platforms:
            raise ValueError(f"Platform {platform_id} not found")

        existing_station_id = self.get_station_for_platform(platform_id)
        if existing_station_id and existing_station_id != station_id:
            raise ValueError(
                f"Platform {platform_id} already belongs to station {existing_station_id}"
            )

        return station.add_platform(platform_id)

    def remove_platform_from_station_group(
        self, station_id: str, platform_id: str
    ) -> bool:
        """Remove a platform from a logical station group."""
        station = self.station_groups.get(station_id)
        if not station:
            raise ValueError(f"Station {station_id} not found")
        return station.remove_platform(platform_id)

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
        - min_radius_cm, speed_limit_kmh (copied as-is)
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
            # v1: Block.state is a derived @property; do not pass it to the
            # constructor. occupied_by defaults to None which yields FREE.
            child_block = Block(
                block_id=child_id,
                name=child_name,
                length_cm=child_length_cm,  # Calculated based on tile count
                min_radius_cm=parent_block.min_radius_cm,
                speed_limit_kmh=parent_block.speed_limit_kmh,
                occupied_by=None,
            )

            self.blocks[child_id] = child_block
            self._ensure_resource_managers()

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
            "platforms": [
                p.to_dict_with_orientation(self) for p in self.platforms.values()
            ],
            "station_groups": [s.to_dict() for s in self.station_groups.values()],
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
        self.platforms.clear()
        self.station_groups.clear()
        self.switches.clear()  # NEW: Clear switches
        self.schedules.clear()

        for block_id, block_data in data.get("blocks", {}).items():
            self.blocks[block_id] = Block.from_dict(block_data)

        self._ensure_resource_managers()

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

        # Rebuild physical platforms
        platforms_data = data.get("platforms", [])

        for platform_data in platforms_data:
            platform = Platform.from_dict(platform_data)
            self.platforms[platform.platform_id] = platform

        # Rebuild logical station groups (optional in legacy saves)
        for station_data in data.get("station_groups", []):
            station = Station.from_dict(station_data)
            self.station_groups[station.station_id] = station

        # Rebuild trains
        for train_data in data.get("trains", []):
            train = Train.from_dict(train_data)
            self.trains[train.train_id] = train
