"""
Path Dispatcher Module

Definition:
This module provides the PathDispatcher class for managing train paths through the railway
network. It handles path creation, validation, resource acquisition, lifecycle management,
and conflict resolution.

Key responsibilities:
- Find paths between blocks/stations using pathfinding
- Validate paths (block coverage, availability, switch availability)
- Acquire resources (reserve blocks, lock switches)
- Manage path lifecycle (SET -> ACTIVE -> CLEARING -> COMPLETED)
- Handle path extension and cancellation
- Calculate flank protection and approach locking
- Manage overlap acquisition

Note: "Path" refers to the dispatcher's reserved route through the network.
      "Route" is reserved for the train's travel plan (waypoints, schedule).
      See Reference_Train_System.md for the full naming convention.

See docs/DISPATCHER_DESIGN.md for full specification.
"""

from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple, Any, TYPE_CHECKING

from path import (
    Path,
    PathStatus,
    PathSegment,
    ValidationError,
    ValidationErrorType,
    ValidationResult,
    PathResult,
    ExtensionResult,
    CancellationResult,
    DEFAULT_PATH_PRIORITY,
)
from enums import BlockState, TrainState, direction_from_delta, DIRECTION_OFFSETS
from track import OPPOSITE_EDGE
from model_scale import kmh_to_cm_per_sec

if TYPE_CHECKING:
    from network import RailwayNetwork
    from block import Block
    from track import TrackSegment


# Edge offsets for navigation (use centralized version from enums)
EDGE_OFFSETS = DIRECTION_OFFSETS


class PathDispatcher:
    """
    Manages path creation, validation, and lifecycle for the railway network.

    The dispatcher is responsible for:
    - Finding all possible paths between endpoints
    - Validating paths for resource availability
    - Acquiring resources (blocks, switches) for paths
    - Managing path lifecycle as trains progress
    - Handling path extension and cancellation
    """

    def __init__(self, network: "RailwayNetwork"):
        self.network = network
        self.active_paths: Dict[str, Path] = {}
        self.path_sequence_counter: int = 0
        self.completed_path_events: List[Dict[str, Any]] = []

    # ========================================================================
    # Path ID Generation
    # ========================================================================

    def generate_path_id(self, start_block_id: str, end_block_id: str) -> str:
        """
        Generate a human-readable path ID.

        Format: PATH_{START}_{END}_{SEQUENCE}
        """
        self.path_sequence_counter += 1

        # Get display names (use block name if available, otherwise ID)
        start_name = self._get_block_display_name(start_block_id)
        end_name = self._get_block_display_name(end_block_id)

        return f"PATH_{start_name}_TO_{end_name}_{self.path_sequence_counter:03d}"

    def _get_block_display_name(self, block_id: str) -> str:
        """Get a display-friendly name for a block."""
        block = self.network.blocks.get(block_id)
        if block and block.name:
            # Sanitize name for use in ID
            name = block.name.upper().replace(" ", "_")[:20]
            return name
        return block_id.upper().replace(" ", "_")[:20]

    # ========================================================================
    # Endpoint Discovery
    # ========================================================================

    def get_reachable_endpoints(
        self, start_block_id: str, include_stations: bool = True
    ) -> List[str]:
        """
        Get all blocks/stations reachable from a starting block.

        This is used by the UI to show valid destination options.
        Uses BFS to explore all paths through any switch configuration.
        Does NOT allow turn-around at switches (entering exit and leaving different exit).

        Args:
            start_block_id: The starting block ID
            include_stations: If True, also return station IDs

        Returns:
            List of reachable block IDs (and optionally station IDs)
        """
        if start_block_id not in self.network.blocks:
            return []

        # Get edge tiles of the start block (tiles with exits to outside the block)
        start_positions = self.network.get_block_positions(start_block_id)
        if not start_positions:
            return []

        reachable_blocks: Set[str] = set()
        visited: Set[Tuple[int, int, str]] = set()  # (x, y, exit_edge)

        # Find all edge tiles of the starting block
        for pos in start_positions:
            track = self.network.get_track(*pos)
            if not track:
                continue

            # Check each edge of this tile
            for edge in track.edges:
                # Check if this edge leads outside the block
                neighbor = self.network.get_neighbor(*pos, edge)
                if neighbor and neighbor.block_section_id != start_block_id:
                    # This is an edge tile - start BFS from here
                    queue = deque([(pos[0], pos[1], edge)])

                    while queue:
                        curr_x, curr_y, exit_edge = queue.popleft()

                        state = (curr_x, curr_y, exit_edge)
                        if state in visited:
                            continue
                        visited.add(state)

                        # Safety limit
                        if len(visited) > 1000:
                            break

                        # Move to next tile
                        offset = EDGE_OFFSETS.get(exit_edge)
                        if not offset:
                            continue
                        dx, dy = offset
                        next_x, next_y = curr_x + dx, curr_y + dy

                        next_track = self.network.get_track(next_x, next_y)
                        if not next_track:
                            continue

                        # Check if we found a new block
                        next_block_id = next_track.block_section_id
                        if next_block_id and next_block_id != start_block_id:
                            reachable_blocks.add(next_block_id)

                        # Determine entry edge
                        entry_edge = OPPOSITE_EDGE.get(exit_edge)
                        if not entry_edge or entry_edge not in next_track.edges:
                            continue

                        # Get all possible exits (ignore switch state - explore all paths)
                        all_exits = next_track.get_exits(
                            entry_edge, use_switch_state=False
                        )

                        for next_exit in all_exits:
                            queue.append((next_x, next_y, next_exit))

        result = list(reachable_blocks)

        # Optionally include stations
        if include_stations:
            for station_id, station in self.network.stations.items():
                # Check if any station tile's block is reachable
                for pos in station.positions:
                    track = self.network.get_track(*pos)
                    if track and track.block_section_id in reachable_blocks:
                        if station_id not in result:
                            result.append(station_id)
                        break

        return result

    def get_reachable_endpoints_for_extension(
        self, path_id: str, include_stations: bool = True
    ) -> Tuple[List[str], Optional[str]]:
        """
        Get all blocks/stations reachable from a path's end in the FORWARD direction only.

        This is used for path extension to ensure users can only extend in the
        direction the path is already heading (no backward extension).

        Algorithm:
        1. Get the path's last tile and entry edge (how we entered the last tile)
        2. Get valid exits from the last tile based on track geometry
        3. BFS explore only from those valid exits
        4. Exclude blocks already in the path's block_sequence

        Args:
            path_id: The path to get extension endpoints for
            include_stations: If True, also return station IDs

        Returns:
            Tuple of (reachable_blocks, error_message):
            - reachable_blocks: List of block IDs reachable in forward direction
            - error_message: None on success, error string on failure
        """
        path = self.active_paths.get(path_id)
        if not path:
            return ([], f"Path {path_id} not found")

        if not path.can_be_extended():
            return ([], f"Path cannot be extended (status: {path.status.value})")

        if not path.path_tiles or len(path.path_tiles) < 2:
            return ([], "Path has insufficient tiles to determine direction")

        # Get the last tile and the entry edge
        last_tile = path.path_tiles[-1]
        entry_edge = path.get_last_tile_entry_edge()
        if not entry_edge:
            return ([], "Cannot determine path exit direction")

        # Get the track at the last tile
        last_track = self.network.get_track(*last_tile)
        if not last_track:
            return ([], f"No track at path end position {last_tile}")

        # Get valid exits from the last tile (respecting track geometry)
        # This handles Y-switches, crossovers, etc.
        valid_exits = last_track.get_exits(entry_edge, use_switch_state=False)
        if not valid_exits:
            return ([], "No valid exits from path end tile")

        # Blocks already in path - exclude from results
        existing_blocks = set(path.block_sequence)

        reachable_blocks: Set[str] = set()
        visited: Set[Tuple[int, int, str]] = set()  # (x, y, exit_edge)

        # Start BFS from each valid exit of the last tile
        for exit_edge in valid_exits:
            queue = deque([(last_tile[0], last_tile[1], exit_edge)])

            while queue:
                curr_x, curr_y, curr_exit = queue.popleft()

                state = (curr_x, curr_y, curr_exit)
                if state in visited:
                    continue
                visited.add(state)

                # Safety limit
                if len(visited) > 1000:
                    break

                # Move to next tile
                offset = EDGE_OFFSETS.get(curr_exit)
                if not offset:
                    continue
                dx, dy = offset
                next_x, next_y = curr_x + dx, curr_y + dy

                next_track = self.network.get_track(next_x, next_y)
                if not next_track:
                    continue

                # Check if we found a new block (not already in path)
                next_block_id = next_track.block_section_id
                if next_block_id and next_block_id not in existing_blocks:
                    reachable_blocks.add(next_block_id)

                # Determine entry edge for next tile
                next_entry_edge = OPPOSITE_EDGE.get(curr_exit)
                if not next_entry_edge or next_entry_edge not in next_track.edges:
                    continue

                # Get all possible exits (explore all switch configurations)
                all_exits = next_track.get_exits(
                    next_entry_edge, use_switch_state=False
                )

                for next_exit in all_exits:
                    queue.append((next_x, next_y, next_exit))

        result = list(reachable_blocks)

        # Optionally include stations
        if include_stations:
            for station_id, station in self.network.stations.items():
                # Check if any station tile's block is reachable
                for pos in station.positions:
                    track = self.network.get_track(*pos)
                    if track and track.block_section_id in reachable_blocks:
                        if station_id not in result:
                            result.append(station_id)
                        break

        return (result, None)

    # ========================================================================
    # Pathfinding
    # ========================================================================

    def find_paths(
        self, start_block_id: str, end_block_id: str, train_id: Optional[str] = None
    ) -> List[PathSegment]:
        """
        Find all possible paths between two blocks.

        Uses BFS to explore all switch configurations and find paths.
        Returns paths sorted by distance (shortest first).

        Args:
            start_block_id: Starting block ID
            end_block_id: Destination block ID
            train_id: Optional train ID (for checking if we can start from occupied block)

        Returns:
            List of PathSegment objects, sorted by distance
        """
        if start_block_id not in self.network.blocks:
            return []
        if end_block_id not in self.network.blocks:
            return []

        # Get edge tiles of start block
        start_positions = self.network.get_block_positions(start_block_id)
        if not start_positions:
            return []

        paths: List[PathSegment] = []

        # Find exit points from start block
        for start_pos in start_positions:
            track = self.network.get_track(*start_pos)
            if not track:
                continue

            for edge in track.edges:
                neighbor = self.network.get_neighbor(*start_pos, edge)
                if neighbor and neighbor.block_section_id != start_block_id:
                    # Start BFS from this exit
                    found_paths = self._bfs_find_paths(
                        start_pos, edge, start_block_id, end_block_id
                    )
                    paths.extend(found_paths)

        # Deduplicate and sort by distance
        unique_paths = self._deduplicate_paths(paths)
        unique_paths.sort(key=lambda p: p.distance_cm)

        return unique_paths

    def _bfs_find_paths(
        self,
        start_pos: Tuple[int, int],
        start_exit: str,
        start_block_id: str,
        end_block_id: str,
    ) -> List[PathSegment]:
        """
        BFS to find all paths from start position to end block.

        Explores all switch configurations to find all possible paths.
        """
        paths: List[PathSegment] = []

        # Queue: (x, y, exit_edge, path_tiles, blocks_visited, switches_needed, distance)
        PathState = Tuple[
            int,
            int,
            str,
            List[Tuple[int, int]],
            List[str],
            Dict[Tuple[int, int], int],
            float,
        ]

        queue: deque[PathState] = deque()
        queue.append(
            (
                start_pos[0],
                start_pos[1],
                start_exit,
                [start_pos],
                [start_block_id],
                {},
                0.0,
            )
        )

        visited: Set[Tuple[int, int, str, frozenset]] = set()

        while queue:
            curr_x, curr_y, exit_edge, path_tiles, blocks, switches, distance = (
                queue.popleft()
            )

            # Create visited key (position + direction + blocks visited so far)
            visited_key = (curr_x, curr_y, exit_edge, frozenset(blocks))
            if visited_key in visited:
                continue
            visited.add(visited_key)

            # Safety limit
            if len(path_tiles) > 200:
                continue

            # Move to next tile
            offset = EDGE_OFFSETS.get(exit_edge)
            if not offset:
                continue
            dx, dy = offset
            next_x, next_y = curr_x + dx, curr_y + dy

            next_track = self.network.get_track(next_x, next_y)
            if not next_track:
                continue

            # Calculate distance for this segment
            segment_distance = (
                next_track.length * 100.0
            )  # Convert to cm (assuming length is in meters/100)
            new_distance = distance + segment_distance

            # Update path
            new_path_tiles = path_tiles + [(next_x, next_y)]
            new_blocks = list(blocks)

            # Check if we entered a new block
            next_block_id = next_track.block_section_id
            if next_block_id and next_block_id not in new_blocks:
                new_blocks.append(next_block_id)

            # Check if we reached destination
            if next_block_id == end_block_id:
                paths.append(
                    PathSegment(
                        tiles=new_path_tiles,
                        distance_cm=new_distance,
                        required_switches=dict(switches),
                        blocks=new_blocks,
                    )
                )
                continue  # Found a path, don't continue from here

            # Determine entry edge
            entry_edge = OPPOSITE_EDGE.get(exit_edge)
            if not entry_edge or entry_edge not in next_track.edges:
                continue

            # Get all possible exits
            all_exits = next_track.get_exits(entry_edge, use_switch_state=False)
            if not all_exits:
                continue

            # For switches, we need to track which state is needed for each path
            if next_track.is_switch:
                if len(all_exits) > 1:
                    # Multiple exits from switch point - create branch for each
                    for next_exit in all_exits:
                        new_switches = dict(switches)
                        # Determine required state for this entry→exit traversal
                        required_state = next_track.get_required_switch_state(
                            entry_edge, next_exit
                        )
                        if required_state is not None:
                            new_switches[(next_x, next_y)] = required_state

                        queue.append(
                            (
                                next_x,
                                next_y,
                                next_exit,
                                new_path_tiles,
                                new_blocks,
                                new_switches,
                                new_distance,
                            )
                        )
                else:
                    # Single exit but still a switch - need to record required state
                    for next_exit in all_exits:
                        new_switches = dict(switches)
                        # Determine required state for this entry→exit traversal
                        required_state = next_track.get_required_switch_state(
                            entry_edge, next_exit
                        )
                        if required_state is not None:
                            new_switches[(next_x, next_y)] = required_state

                        queue.append(
                            (
                                next_x,
                                next_y,
                                next_exit,
                                new_path_tiles,
                                new_blocks,
                                new_switches,
                                new_distance,
                            )
                        )
            else:
                # Non-switch track
                for next_exit in all_exits:
                    queue.append(
                        (
                            next_x,
                            next_y,
                            next_exit,
                            new_path_tiles,
                            new_blocks,
                            dict(switches),
                            new_distance,
                        )
                    )

        return paths

    def _deduplicate_paths(self, paths: List[PathSegment]) -> List[PathSegment]:
        """Remove duplicate paths (same blocks in same order)."""
        seen: Set[Tuple[Tuple[str, ...], Tuple[Tuple[Tuple[int, int], int], ...]]] = (
            set()
        )
        unique = []

        for path in paths:
            key = (tuple(path.blocks), tuple(sorted(path.required_switches.items())))
            if key not in seen:
                seen.add(key)
                unique.append(path)

        return unique

    # ========================================================================
    # Validation
    # ========================================================================

    def validate_path(
        self, path: PathSegment, path_id: str, train_id: Optional[str] = None
    ) -> ValidationResult:
        """
        Validate that a path can be used for a dispatcher path.

        Checks:
        1. All tiles are in blocks
        2. All blocks are available (not occupied/reserved by others)
        3. All required switches are available (not locked by others)
        4. Flank protection switches are available

        Args:
            path: The PathSegment to validate
            path_id: The path ID that would use this path
            train_id: Optional train ID that would use the path

        Returns:
            ValidationResult with valid flag and any errors
        """
        errors: List[ValidationError] = []

        # Check 1: All tiles in blocks (except switches, which are boundaries)
        for tile in path.tiles:
            track = self.network.get_track(*tile)
            if not track:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.UNBLOCKED_TILE,
                        tile=tile,
                        message=f"No track at position {tile}",
                    )
                )
                continue

            # Switches are block boundaries and do NOT belong to blocks
            # Skip the block check for switch tiles
            if track.is_switch:
                continue

            if not track.block_section_id:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.UNBLOCKED_TILE,
                        tile=tile,
                        message=f"Tile {tile} is not in any block",
                    )
                )

        # Check 2: Block availability
        for block_id in path.blocks:
            block = self.network.blocks.get(block_id)
            if not block:
                continue

            if block.disabled:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.BLOCK_DISABLED,
                        block_id=block_id,
                        message=f"Block {block_id} is disabled",
                    )
                )
                continue

            if not block.is_available_for_path(path_id, train_id):
                if block.state == BlockState.OCCUPIED:
                    errors.append(
                        ValidationError(
                            type=ValidationErrorType.BLOCK_OCCUPIED,
                            block_id=block_id,
                            occupied_by=block.occupied_by,
                            message=f"Block {block_id} is occupied by {block.occupied_by}",
                        )
                    )
                elif block.state == BlockState.RESERVED:
                    errors.append(
                        ValidationError(
                            type=ValidationErrorType.BLOCK_RESERVED,
                            block_id=block_id,
                            locked_by_path=block.reserved_by_path,
                            message=f"Block {block_id} is reserved for path {block.reserved_by_path}",
                        )
                    )

        # Check 3: Switch availability
        for pos, required_state in path.required_switches.items():
            track = self.network.get_track(*pos)
            if not track:
                continue

            if not track.can_lock_for_path(path_id, required_state):
                lock_info = track.get_lock_info()
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.SWITCH_LOCKED,
                        position=pos,
                        locked_by_path=lock_info["path_id"] if lock_info else None,
                        message=f"Switch at {pos} is locked by path {lock_info['path_id'] if lock_info else 'unknown'}",
                    )
                )

        # Check 4: Flank protection switches
        flank_switches = self._calculate_flank_protection(path)
        for pos, safe_state in flank_switches.items():
            if pos in path.required_switches:
                continue  # Already handled

            track = self.network.get_track(*pos)
            if not track:
                continue

            if not track.can_lock_for_path(path_id, safe_state):
                lock_info = track.get_lock_info()
                if lock_info and lock_info["locked_state"] != safe_state:
                    errors.append(
                        ValidationError(
                            type=ValidationErrorType.FLANK_SWITCH_CONFLICT,
                            position=pos,
                            locked_by_path=lock_info["path_id"],
                            message=f"Flank protection switch at {pos} is locked in wrong state",
                        )
                    )

        return ValidationResult(valid=len(errors) == 0, errors=errors, path=path)

    def _calculate_flank_protection(
        self, path: PathSegment
    ) -> Dict[Tuple[int, int], int]:
        """
        Calculate switches needed for flank protection.

        Flank protection locks nearby switches to safe positions to prevent
        collisions from merging tracks.
        """
        flank_switches: Dict[Tuple[int, int], int] = {}
        path_tile_set = set(path.tiles)

        # For each tile in the path, find nearby switches
        for tile_pos in path.tiles:
            # Simple distance-based check (Manhattan distance)
            # In a real implementation, this would use track distance
            for track_pos, track in self.network.tracks.items():
                if not track.is_switch:
                    continue
                if track_pos in path.required_switches:
                    continue  # Already part of path
                if track_pos in path_tile_set:
                    continue  # On the path

                # Check distance (simplified)
                distance = abs(track_pos[0] - tile_pos[0]) + abs(
                    track_pos[1] - tile_pos[1]
                )
                if distance > 5:  # Simplified distance check
                    continue

                # Check if any exit leads to path
                for entry, exits in track.switch_points.items():
                    for exit_dir in exits:
                        neighbor_pos = self._get_neighbor_pos(track_pos, exit_dir)
                        if neighbor_pos in path_tile_set:
                            # This switch could route trains into our path
                            # Lock it to safe state (away from path)
                            # This is simplified - real implementation would be more sophisticated
                            safe_state = 0  # Default to MAIN
                            flank_switches[track_pos] = safe_state

        return flank_switches

    def _get_neighbor_pos(self, pos: Tuple[int, int], edge: str) -> Tuple[int, int]:
        """Get neighbor position in given direction."""
        offset = EDGE_OFFSETS.get(edge, (0, 0))
        return (pos[0] + offset[0], pos[1] + offset[1])

    # ========================================================================
    # Path Creation
    # ========================================================================

    def create_path(
        self,
        start_block_id: str,
        end_block_id: str,
        dispatcher_id: str = "system",
        priority: int = DEFAULT_PATH_PRIORITY,
        train_id: Optional[str] = None,
    ) -> PathResult:
        """
        Create a path between two blocks.

        Algorithm:
        1. Find all paths, sorted by distance
        2. Validate shortest path
        3. If valid, acquire resources
        4. If acquisition fails, try next path
        5. If all paths fail, return error with conflict info

        Args:
            start_block_id: Starting block ID
            end_block_id: Destination block ID
            dispatcher_id: ID of the dispatcher creating this path
            priority: Path priority (1-10, default 5)
            train_id: Optional train ID that will use this path

        Returns:
            PathResult with success flag and path or error info
        """
        # Find all possible paths
        paths = self.find_paths(start_block_id, end_block_id, train_id)

        if not paths:
            return PathResult(
                success=False,
                error=f"No path exists between {start_block_id} and {end_block_id}",
            )

        # Generate path ID
        path_id = self.generate_path_id(start_block_id, end_block_id)

        attempted_paths: List[Tuple[PathSegment, ValidationResult]] = []
        conflict_tiles: Set[Tuple[int, int]] = set()

        # Try each path (shortest first)
        for path_segment in paths:
            validation = self.validate_path(path_segment, path_id, train_id)
            attempted_paths.append((path_segment, validation))

            if validation.valid:
                # Try to acquire resources
                success = self._acquire_path_resources(
                    path_id, path_segment, priority, train_id
                )

                if success:
                    # Extend path tiles to include all tiles of destination block
                    # This ensures multi-tile stations are fully traversed
                    extended_tiles = self._extend_path_to_destination_end(
                        path_segment.tiles, end_block_id
                    )

                    # Create path object
                    path = Path(
                        path_id=path_id,
                        status=PathStatus.SET,
                        priority=priority,
                        start_block_id=start_block_id,
                        end_block_id=end_block_id,
                        path_tiles=extended_tiles,
                        block_sequence=path_segment.blocks,
                        switch_positions=path_segment.required_switches,
                        train_id=train_id,
                        dispatcher_id=dispatcher_id,
                        flank_protection_switches=self._calculate_flank_protection(
                            path_segment
                        ),
                    )

                    # Store path
                    self.active_paths[path_id] = path

                    # Populate train.reserved_blocks if train_id provided
                    if train_id:
                        train = self.network.trains.get(train_id)
                        if train:
                            train.reserved_blocks = path.block_sequence.copy()

                    return PathResult(success=True, path=path)
            else:
                # Collect conflict tiles for UI feedback
                for error in validation.errors:
                    if error.tile:
                        conflict_tiles.add(error.tile)
                    if error.position:
                        conflict_tiles.add(error.position)

        # All paths failed
        return PathResult(
            success=False,
            error="All paths blocked",
            attempted_paths=attempted_paths,
            conflict_tiles=list(conflict_tiles),
        )

    def _acquire_path_resources(
        self, path_id: str, path: PathSegment, priority: int, train_id: Optional[str]
    ) -> bool:
        """
        Acquire all resources for a path (blocks and switches).

        This is atomic - if any acquisition fails, all are rolled back.
        """
        reserved_blocks: List[str] = []
        locked_switches: List[Tuple[int, int]] = []
        direction_set_tiles: List[Tuple[int, int]] = []

        # Calculate direction for each block based on path tiles
        block_directions = self._calculate_block_directions(path)

        # Calculate direction for each tile
        tile_directions = self._calculate_tile_directions(path)

        try:
            # Reserve all blocks
            for block_id in path.blocks:
                block = self.network.blocks.get(block_id)
                if not block:
                    raise ValueError(f"Block {block_id} not found")

                # Get the direction for this block
                direction = block_directions.get(block_id)

                if not block.reserve_for_path(path_id, train_id, direction):
                    raise ValueError(f"Failed to reserve block {block_id}")

                reserved_blocks.append(block_id)

            # Set path direction on each tile
            for pos, direction in tile_directions.items():
                track = self.network.get_track(*pos)
                if track:
                    track.path_direction = direction
                    direction_set_tiles.append(pos)

            # Lock all switches
            for pos, state in path.required_switches.items():
                track = self.network.get_track(*pos)
                if not track:
                    raise ValueError(f"Track at {pos} not found")

                if not track.lock_for_path(path_id, state):
                    raise ValueError(f"Failed to lock switch at {pos}")

                locked_switches.append(pos)

            # Lock flank protection switches
            flank_switches = self._calculate_flank_protection(path)
            for pos, state in flank_switches.items():
                track = self.network.get_track(*pos)
                if track and track.is_switch:
                    if track.lock_for_path(path_id, state):
                        locked_switches.append(pos)

            return True

        except Exception:
            # Rollback
            for block_id in reserved_blocks:
                block = self.network.blocks.get(block_id)
                if block:
                    block.release_path_reservation()

            # Clear path directions
            for pos in direction_set_tiles:
                track = self.network.get_track(*pos)
                if track:
                    track.path_direction = None

            for pos in locked_switches:
                track = self.network.get_track(*pos)
                if track:
                    track.unlock_for_path(path_id)

            return False

    def _calculate_block_directions(self, path: PathSegment) -> Dict[str, str]:
        """
        Calculate the direction of travel for each block along a path.

        This determines which direction a train will travel through each block,
        used for directional signal calculations.

        Args:
            path: The path segment with tiles and blocks

        Returns:
            Dictionary mapping block_id to direction (N/E/S/W)
        """
        block_directions: Dict[str, str] = {}

        if not path.tiles or len(path.tiles) < 2:
            return block_directions

        # For each pair of consecutive tiles, determine direction
        for i in range(len(path.tiles) - 1):
            curr_tile = path.tiles[i]
            next_tile = path.tiles[i + 1]

            # Get the track at current tile to find its block
            track = self.network.get_track(*curr_tile)
            if track and track.block_section_id:
                block_id = track.block_section_id
                if block_id not in block_directions:
                    # Calculate direction from curr_tile to next_tile
                    dx = next_tile[0] - curr_tile[0]
                    dy = next_tile[1] - curr_tile[1]

                    direction = direction_from_delta(dx, dy)
                    if direction:
                        block_directions[block_id] = direction

        # Handle the last tile's block (use direction from previous tile)
        if path.tiles:
            last_track = self.network.get_track(*path.tiles[-1])
            if last_track and last_track.block_section_id:
                block_id = last_track.block_section_id
                if block_id not in block_directions and len(path.tiles) >= 2:
                    prev_tile = path.tiles[-2]
                    last_tile = path.tiles[-1]
                    dx = last_tile[0] - prev_tile[0]
                    dy = last_tile[1] - prev_tile[1]

                    direction = direction_from_delta(dx, dy)
                    if direction:
                        block_directions[block_id] = direction

        return block_directions

    def _calculate_tile_directions(
        self, path: PathSegment
    ) -> Dict[Tuple[int, int], str]:
        """
        Calculate the direction of travel for each tile in a path.

        This is used to set path_direction on each track tile, which allows
        signals to check if they're facing the same direction as the path.

        Args:
            path: The path segment with tiles

        Returns:
            Dictionary mapping (x, y) to direction (N/E/S/W)
        """
        tile_directions: Dict[Tuple[int, int], str] = {}

        if not path.tiles or len(path.tiles) < 2:
            return tile_directions

        # For each pair of consecutive tiles, determine direction
        for i in range(len(path.tiles) - 1):
            curr_tile = path.tiles[i]
            next_tile = path.tiles[i + 1]

            dx = next_tile[0] - curr_tile[0]
            dy = next_tile[1] - curr_tile[1]

            direction = direction_from_delta(dx, dy)
            if direction is None:
                continue  # Same tile (shouldn't happen)

            tile_directions[curr_tile] = direction

        # For the last tile, use the same direction as the previous tile
        if len(path.tiles) >= 2:
            prev_tile = path.tiles[-2]
            last_tile = path.tiles[-1]

            dx = last_tile[0] - prev_tile[0]
            dy = last_tile[1] - prev_tile[1]

            direction = direction_from_delta(dx, dy)
            if direction:
                tile_directions[last_tile] = direction

        return tile_directions

    # ========================================================================
    # Path Lifecycle
    # ========================================================================

    def on_train_advances_from_physics(
        self, path_id: str, entered_block_id: str
    ) -> bool:
        """Advance path position without modifying block states.

        Args:
            path_id: The path
            entered_block_id: The block just entered

        Returns:
            True if successful
        """
        path = self.active_paths.get(path_id)
        if not path:
            return False

        if entered_block_id in path.block_sequence:
            path.current_block_index = path.block_sequence.index(entered_block_id)

        if entered_block_id == path.block_sequence[-1]:
            path.status = PathStatus.CLEARING

        return True

    def on_train_progress_from_physics(self, path_id: str) -> bool:
        """Update cleared blocks/switches using physics-driven positions.

        Args:
            path_id: Path to update

        Returns:
            True if the path was updated, False if not applicable
        """
        path = self.active_paths.get(path_id)
        if not path or path.status not in (
            PathStatus.SET,
            PathStatus.ACTIVE,
            PathStatus.CLEARING,
        ):
            return False

        if not path.train_id:
            return False

        train = self.network.trains.get(path.train_id)
        if not train or not train.position:
            return False

        if not path.path_tiles:
            return False

        # If the train has entered the destination block (CLEARING) and is fully
        # stopped there, complete the path even if it has not reached the exact
        # final tile. This matches station-stop behavior where ATO may stop before
        # the physical end tile of the block.
        if path.status == PathStatus.CLEARING and train.is_stopped():
            head_track = self.network.get_track(*train.position.head_tile)
            if head_track and head_track.block_section_id == path.end_block_id:
                self._complete_path_on_head_arrival(path, train)
                return True

        if train.position.head_tile == path.path_tiles[-1]:
            self._complete_path_on_head_arrival(path, train)
            return True

        occupied_block_ids = set(train.position.occupied_blocks)

        for i, block_id in enumerate(path.block_sequence):
            if i >= path.current_block_index:
                break
            if block_id in path.blocks_cleared:
                continue
            if block_id in occupied_block_ids:
                continue
            block = self.network.blocks.get(block_id)
            if not block or block.state != BlockState.FREE:
                continue
            path.blocks_cleared.add(block_id)
            self._release_switches_for_block(path, block_id)

        return True

    def get_and_clear_completion_events(self) -> List[Dict[str, Any]]:
        """Return completion events and clear the queue."""
        events = list(self.completed_path_events)
        self.completed_path_events.clear()
        return events

    def _complete_path_on_head_arrival(self, path: Path, train: "Train") -> None:
        """Complete path when train head reaches the end tile."""
        path.status = PathStatus.COMPLETED
        path.completed_at = datetime.now()

        occupied_block_ids = set(train.position.occupied_blocks)

        for block_id in path.block_sequence:
            block = self.network.blocks.get(block_id)
            if not block:
                continue

            if block_id not in occupied_block_ids:
                if self.network.block_manager:
                    self.network.block_manager.clear_block(block_id)
                else:
                    block.release()

            block.reserved_by_path = None
            block.reserved_direction = None

        for block_id in path.overlap_blocks:
            block = self.network.blocks.get(block_id)
            if not block:
                continue

            if block_id not in occupied_block_ids:
                if self.network.block_manager:
                    self.network.block_manager.clear_block(block_id)
                else:
                    block.release()

            block.reserved_by_path = None
            block.reserved_direction = None

        for pos in path.path_tiles:
            track = self.network.get_track(*pos)
            if track:
                track.path_direction = None

        for pos in path.switch_positions:
            track = self.network.get_track(*pos)
            if track:
                track.unlock_for_path(path.path_id)

        for pos in path.flank_protection_switches:
            track = self.network.get_track(*pos)
            if track:
                track.unlock_for_path(path.path_id)

        self._record_completion_event(path, train.position.head_tile)

        # Clear train's stale path/destination state — path is done
        if path.train_id:
            train_obj = self.network.trains.get(path.train_id)
            if train_obj:
                train_obj.ato_destination_block_id = None
                train_obj.assigned_path_id = None
                train_obj.current_path = None
                train_obj.path_index = 0
                train_obj.reserved_blocks = []

                # Stop ATO and transition to standby when path completes
                # This ensures train state changes from RUNNING_MOVING to STANDBY
                # even when stopped at a waypoint that's not a station
                if (
                    hasattr(self.network, "power_controller")
                    and self.network.power_controller
                ):
                    sim_controller = getattr(
                        self.network.power_controller, "_sim_controller", None
                    )
                    if sim_controller:
                        controller = sim_controller.get_controller(train_obj.train_id)
                        if controller:
                            controller.ato_running = False
                            controller.ato_destination_block_id = None
                            controller.ato_path_id = None
                            if train_obj.state in (
                                TrainState.RUNNING_MOVING,
                                TrainState.RUNNING_WAITING,
                            ):
                                controller.transition_to_standby()

        del self.active_paths[path.path_id]

    def _record_completion_event(self, path: Path, end_tile: Tuple[int, int]) -> None:
        """Record a completion event for consumption by other systems."""
        self.completed_path_events.append(
            {
                "train_id": path.train_id,
                "path_id": path.path_id,
                "end_tile": end_tile,
                "time": datetime.now(),
            }
        )

    def _release_blocks_behind_train(self, path: Path) -> None:
        """
        Release blocks behind the train tail while maintaining safety distance.

        This method uses path-based position tracking to determine when the train
        tail has completely cleared a block, transitioning it through:
        OCCUPIED (head enters) → CAUTION (tail passes) → FREE (safety margin satisfied)
        """
        if not path.train_id:
            return

        train = self.network.trains.get(path.train_id)
        if not train or not train.position:
            return

        # Get list of blocks the train currently occupies (from TrainPosition)
        occupied_block_ids = set(train.position.occupied_blocks)

        # Process each block in the path sequence
        for i, block_id in enumerate(path.block_sequence):
            # Don't process blocks at or ahead of current head position
            if i >= path.current_block_index:
                break

            # Skip already cleared blocks
            if block_id in path.blocks_cleared:
                continue

            block = self.network.blocks.get(block_id)
            if not block:
                continue

            # Check if train tail has passed this block
            tail_has_cleared = block_id not in occupied_block_ids

            # State transition logic
            if tail_has_cleared:
                # Train tail has completely left this block
                if block.state == BlockState.OCCUPIED:
                    # OCCUPIED → CAUTION (tail just cleared, maintain safety margin)
                    if self.network.block_manager:
                        self.network.block_manager.set_block_to_caution(block_id)
                    else:
                        block.set_to_caution()
                elif block.state == BlockState.CAUTION:
                    # CAUTION → FREE (safety margin satisfied, complete release)
                    if self.network.block_manager:
                        self.network.block_manager.clear_block(block_id)
                    else:
                        block.clear()
                    block.reserved_by_path = None
                    path.blocks_cleared.add(block_id)
                    self._release_switches_for_block(path, block_id)
            else:
                # Train tail still in this block - keep as OCCUPIED
                if block.state != BlockState.OCCUPIED:
                    # Ensure block is marked as occupied
                    if self.network.block_manager:
                        self.network.block_manager.occupy_block(
                            block_id, train.train_id
                        )
                    else:
                        block.occupy(train.train_id)

    def _release_switches_for_block(self, path: Path, block_id: str) -> None:
        """Release switches that are only associated with a cleared block."""
        if not path.path_tiles:
            return

        # Get current position index in path_tiles based on current block
        current_pos_index = 0
        current_block = path.get_current_block()
        if current_block:
            # Find first tile of current block in path_tiles
            for i, tile in enumerate(path.path_tiles):
                track = self.network.get_track(*tile)
                if track and track.block_section_id == current_block:
                    current_pos_index = i
                    break

        for pos in list(path.switch_positions.keys()):
            track = self.network.get_track(*pos)
            if not track:
                continue

            # Check if switch is still ahead in the path
            # A switch is still needed if it appears in path_tiles at or after current position
            can_release = True
            try:
                switch_index = path.path_tiles.index(pos)
                # If switch is at or after current position, don't release
                if switch_index >= current_pos_index:
                    can_release = False
            except ValueError:
                print(
                    f"[PathDispatcher] Switch at {pos} not found in path_tiles for path {path.path_id}, allowing release"
                )
                can_release = True

            if can_release:
                track.unlock_for_path(path.path_id)

    def complete_path(self, path_id: str) -> bool:
        """
        Complete a path and release all resources.

        Args:
            path_id: Path to complete

        Returns:
            True if successful
        """
        path = self.active_paths.get(path_id)
        if not path:
            return False

        path.status = PathStatus.COMPLETED
        path.completed_at = datetime.now()

        # Release all blocks using BlockManager
        for block_id in path.block_sequence:
            block = self.network.blocks.get(block_id)
            if not block:
                continue

            # Only clear blocks that are not currently occupied by a train
            if block.state != BlockState.OCCUPIED:
                if self.network.block_manager:
                    self.network.block_manager.clear_block(block_id)
                else:
                    block.release()

            block.reserved_by_path = None

        # Release overlap blocks
        for block_id in path.overlap_blocks:
            block = self.network.blocks.get(block_id)
            if not block:
                continue

            # Only clear blocks that are not currently occupied by a train
            if block.state != BlockState.OCCUPIED:
                if self.network.block_manager:
                    self.network.block_manager.clear_block(block_id)
                else:
                    block.release()

            block.reserved_by_path = None

        # Clear path direction on all path tiles
        for pos in path.path_tiles:
            track = self.network.get_track(*pos)
            if track:
                track.path_direction = None

        # Unlock all switches
        for pos in path.switch_positions:
            track = self.network.get_track(*pos)
            if track:
                track.unlock_for_path(path_id)

        for pos in path.flank_protection_switches:
            track = self.network.get_track(*pos)
            if track:
                track.unlock_for_path(path_id)

        # Remove from active paths
        # Clear train's stale path/destination state — path is done
        if path.train_id:
            train_obj = self.network.trains.get(path.train_id)
            if train_obj:
                train_obj.ato_destination_block_id = None
                train_obj.assigned_path_id = None
                train_obj.current_path = None
                train_obj.path_index = 0
                train_obj.reserved_blocks = []

                # Clear ato_running on the controller so that
                # _is_standalone_path_complete() and the
                # RUNNING_WAITING → RUNNING_MOVING guard work correctly
                # after this path is torn down.
                if (
                    hasattr(self.network, "power_controller")
                    and self.network.power_controller
                ):
                    sim_controller = getattr(
                        self.network.power_controller, "_sim_controller", None
                    )
                    if sim_controller:
                        controller = sim_controller.get_controller(path.train_id)
                        if controller:
                            controller.ato_running = False
                            controller.ato_destination_block_id = None
                            controller.ato_path_id = None

        del self.active_paths[path_id]

        return True

    # ========================================================================
    # Path Extension
    # ========================================================================

    def extend_path(self, path_id: str, new_end_block_id: str) -> ExtensionResult:
        """
        Extend an existing path to a new destination in the FORWARD direction only.

        This method enforces direction-aware extension:
        - Cannot extend to a block already in the path (prevents backward extension)
        - Only explores paths that continue in the path's current direction

        Args:
            path_id: Path to extend
            new_end_block_id: New destination block

        Returns:
            ExtensionResult with success flag and extended blocks
        """
        path = self.active_paths.get(path_id)
        if not path:
            return ExtensionResult(success=False, error=f"Path {path_id} not found")

        if not path.can_be_extended():
            return ExtensionResult(
                success=False,
                error=f"Path must be SET or ACTIVE to extend (current: {path.status.value})",
            )

        # Validation 1: Cannot extend to a block already in the path
        if new_end_block_id in path.block_sequence:
            return ExtensionResult(
                success=False,
                error=f"Cannot extend backwards: block '{new_end_block_id}' is already in path",
            )

        # Use direction-aware pathfinding for extension
        extension_paths = self._find_extension_paths(path, new_end_block_id)

        if not extension_paths:
            return ExtensionResult(
                success=False,
                error=f"No forward path exists to {new_end_block_id}",
            )

        conflict_tiles: Set[Tuple[int, int]] = set()

        # Try each extension path
        for path_segment in extension_paths:
            validation = self.validate_path(path_segment, path_id, path.train_id)

            if validation.valid:
                success = self._acquire_path_resources(
                    path_id, path_segment, path.priority, path.train_id
                )

                if success:
                    # Merge extension into path
                    path.path_tiles.extend(
                        path_segment.tiles[1:]
                    )  # Skip first (connection point)
                    path.block_sequence.extend(
                        path_segment.blocks[1:]
                    )  # Skip first (already in path)
                    path.switch_positions.update(path_segment.required_switches)
                    path.end_block_id = new_end_block_id

                    # Update flank protection
                    new_flank = self._calculate_flank_protection(path_segment)
                    path.flank_protection_switches.update(new_flank)

                    # Train path data is read directly from this Path object
                    # by TrainController.get_path_tiles() — no sync needed.

                    return ExtensionResult(
                        success=True, extended_blocks=path_segment.blocks[1:]
                    )
            else:
                for error in validation.errors:
                    if error.tile:
                        conflict_tiles.add(error.tile)

        return ExtensionResult(
            success=False,
            error="All extension paths blocked",
            conflict_tiles=list(conflict_tiles),
        )

    def _find_extension_paths(
        self, path: Path, new_end_block_id: str
    ) -> List[PathSegment]:
        """
        Find paths for extending a path in the FORWARD direction only.

        Unlike find_paths which explores all directions, this method
        only starts from valid exits based on the path's current direction.

        Args:
            path: The path to extend
            new_end_block_id: The target block for extension

        Returns:
            List of PathSegment objects, sorted by distance (shortest first)
        """
        if new_end_block_id not in self.network.blocks:
            return []

        if not path.path_tiles or len(path.path_tiles) < 2:
            return []

        # Get the last tile and the entry edge
        last_tile = path.path_tiles[-1]
        entry_edge = path.get_last_tile_entry_edge()
        if not entry_edge:
            return []

        # Get the track at the last tile
        last_track = self.network.get_track(*last_tile)
        if not last_track:
            return []

        # Get valid exits from the last tile (respecting track geometry)
        valid_exits = last_track.get_exits(entry_edge, use_switch_state=False)
        if not valid_exits:
            return []

        # Blocks already in path - extension paths should not go through these
        existing_blocks = set(path.block_sequence)

        paths: List[PathSegment] = []

        # Start BFS from each valid exit of the last tile
        for exit_edge in valid_exits:
            found_paths = self._bfs_find_extension_paths(
                last_tile, exit_edge, existing_blocks, new_end_block_id
            )
            paths.extend(found_paths)

        # Deduplicate and sort by distance
        unique_paths = self._deduplicate_paths(paths)
        unique_paths.sort(key=lambda p: p.distance_cm)

        return unique_paths

    def _bfs_find_extension_paths(
        self,
        start_pos: Tuple[int, int],
        start_exit: str,
        existing_blocks: Set[str],
        end_block_id: str,
    ) -> List[PathSegment]:
        """
        BFS to find extension paths from start position to end block.

        This is similar to _bfs_find_paths but:
        1. Starts from a specific exit edge (not all edges)
        2. Excludes paths that go through blocks already in the path
        """
        paths: List[PathSegment] = []

        # Queue: (x, y, exit_edge, path_tiles, blocks_visited, switches_needed, distance)
        PathState = Tuple[
            int,
            int,
            str,
            List[Tuple[int, int]],
            List[str],
            Dict[Tuple[int, int], int],
            float,
        ]

        # Include the start tile in the path (for proper merging later)
        queue: deque[PathState] = deque()

        # Get the block of the start tile to include in blocks
        start_track = self.network.get_track(*start_pos)
        start_block_id = start_track.block_section_id if start_track else None
        initial_blocks = [start_block_id] if start_block_id else []

        queue.append(
            (
                start_pos[0],
                start_pos[1],
                start_exit,
                [start_pos],
                initial_blocks,
                {},
                0.0,
            )
        )

        visited: Set[Tuple[int, int, str, frozenset]] = set()

        while queue:
            curr_x, curr_y, exit_edge, path_tiles, blocks, switches, distance = (
                queue.popleft()
            )

            # Create visited key (position + direction + blocks visited so far)
            visited_key = (curr_x, curr_y, exit_edge, frozenset(blocks))
            if visited_key in visited:
                continue
            visited.add(visited_key)

            # Safety limit
            if len(path_tiles) > 200:
                continue

            # Move to next tile
            offset = EDGE_OFFSETS.get(exit_edge)
            if not offset:
                continue
            dx, dy = offset
            next_x, next_y = curr_x + dx, curr_y + dy

            next_track = self.network.get_track(next_x, next_y)
            if not next_track:
                continue

            # Calculate distance for this segment
            segment_distance = (
                next_track.length * 100.0
            )  # Convert to cm (assuming length is in meters/100)
            new_distance = distance + segment_distance

            # Update path
            new_path_tiles = path_tiles + [(next_x, next_y)]
            new_blocks = list(blocks)

            # Check if we entered a new block
            next_block_id = next_track.block_section_id
            if next_block_id and next_block_id not in new_blocks:
                # Check if this block is already in the path (would be backward)
                if next_block_id in existing_blocks:
                    # Skip this path - it goes backward through existing path
                    continue
                new_blocks.append(next_block_id)

            # Check if we reached destination
            if next_block_id == end_block_id:
                paths.append(
                    PathSegment(
                        tiles=new_path_tiles,
                        distance_cm=new_distance,
                        required_switches=dict(switches),
                        blocks=new_blocks,
                    )
                )
                continue  # Found a path, don't continue from here

            # Determine entry edge
            entry_edge = OPPOSITE_EDGE.get(exit_edge)
            if not entry_edge or entry_edge not in next_track.edges:
                continue

            # Get all possible exits
            all_exits = next_track.get_exits(entry_edge, use_switch_state=False)
            if not all_exits:
                continue

            # For switches, we need to track which state is needed for each path
            if next_track.is_switch:
                for next_exit in all_exits:
                    new_switches = dict(switches)
                    # Determine required state for this entry→exit traversal
                    required_state = next_track.get_required_switch_state(
                        entry_edge, next_exit
                    )
                    if required_state is not None:
                        new_switches[(next_x, next_y)] = required_state

                    queue.append(
                        (
                            next_x,
                            next_y,
                            next_exit,
                            new_path_tiles,
                            new_blocks,
                            new_switches,
                            new_distance,
                        )
                    )
            else:
                # Non-switch track
                for next_exit in all_exits:
                    queue.append(
                        (
                            next_x,
                            next_y,
                            next_exit,
                            new_path_tiles,
                            new_blocks,
                            dict(switches),
                            new_distance,
                        )
                    )

        return paths

    # ========================================================================
    # Path Cancellation
    # ========================================================================

    def cancel_path(self, path_id: str, force: bool = False) -> CancellationResult:
        """
        Cancel a path and release resources.

        Args:
            path_id: Path to cancel
            force: If True, cancel even approach-locked blocks

        Returns:
            CancellationResult with released resources
        """
        path = self.active_paths.get(path_id)
        if not path:
            return CancellationResult(
                success=False, message=f"Path {path_id} not found"
            )

        if not path.can_be_cancelled():
            return CancellationResult(
                success=False,
                message=f"Path cannot be cancelled (status: {path.status.value})",
            )

        path.status = PathStatus.CANCELLED

        blocks_released: List[str] = []
        switches_released: List[Tuple[int, int]] = []
        approach_locked: List[str] = list(path.approach_locked_blocks)

        # Get train's occupied tiles and blocks if train is assigned to this path
        train_occupied_tiles: set[Tuple[int, int]] = set()
        train_occupied_blocks: set[str] = set()
        if path.train_id:
            train = self.network.trains.get(path.train_id)
            if train:
                # Train will need new path after stopping
                train.state = (
                    train.state
                )  # Keep current state, train will handle stopping

                # Collect all tiles and blocks the train physically occupies
                if train.position and train.position.occupied_tiles:
                    train_occupied_tiles = set(train.position.occupied_tiles)
                if train.position and train.position.occupied_blocks:
                    train_occupied_blocks = set(train.position.occupied_blocks)

        # Release non-approach-locked blocks
        for block_id in path.block_sequence:
            if block_id not in path.approach_locked_blocks or force:
                block = self.network.blocks.get(block_id)
                if not block:
                    continue

                # Skip blocks that the train is physically occupying
                if block_id in train_occupied_blocks:
                    continue

                # Only clear blocks that are not currently occupied by a train
                if block.state != BlockState.OCCUPIED:
                    if self.network.block_manager:
                        self.network.block_manager.clear_block(block_id)
                    else:
                        block.release()

                block.reserved_by_path = None
                blocks_released.append(block_id)

        # Release switches
        for pos in path.switch_positions:
            track = self.network.get_track(*pos)
            if track:
                # Skip switches that the train is physically over
                if pos in train_occupied_tiles:
                    continue

                # Check if switch is in approach-locked block
                if force or not self._is_switch_in_approach_locked_block(path, pos):
                    track.unlock_for_path(path_id)
                    switches_released.append(pos)

        # Clear path direction on all path tiles (if fully releasing or forced)
        if not path.approach_locked_blocks or force:
            for pos in path.path_tiles:
                track = self.network.get_track(*pos)
                if track:
                    track.path_direction = None

        # Release flank protection switches
        for pos in path.flank_protection_switches:
            track = self.network.get_track(*pos)
            if track:
                # Skip switches that the train is physically over
                if pos not in train_occupied_tiles:
                    track.unlock_for_path(path_id)
                    switches_released.append(pos)

        # Remove from active paths if fully released
        if not path.approach_locked_blocks or force:
            del self.active_paths[path_id]

        return CancellationResult(
            success=True,
            blocks_released=blocks_released,
            switches_released=switches_released,
            approach_locked=approach_locked,
            message="Approach-locked blocks will release when train stops"
            if approach_locked
            else None,
        )

    def _is_switch_in_approach_locked_block(
        self, path: Path, switch_pos: Tuple[int, int]
    ) -> bool:
        """Check if a switch is in an approach-locked block."""
        for block_id in path.approach_locked_blocks:
            positions = self.network.get_block_positions(block_id)
            if switch_pos in positions:
                return True
        return False

    # ========================================================================
    # Approach Locking
    # ========================================================================

    def update_approach_locking(self, path_id: str) -> None:
        """
        Update approach locking based on train position and speed.

        Called each simulation tick for active paths.
        """
        path = self.active_paths.get(path_id)
        if not path or not path.train_id:
            return

        train = self.network.trains.get(path.train_id)
        if not train:
            return

        # Calculate braking distance
        braking_distance = self._calculate_braking_distance(train)

        # Clear previous and recalculate
        path.approach_locked_blocks.clear()

        # Find blocks within braking distance
        distance_ahead = 0.0
        for i in range(path.current_block_index, len(path.block_sequence)):
            block_id = path.block_sequence[i]
            block = self.network.blocks.get(block_id)

            if distance_ahead <= braking_distance:
                path.approach_locked_blocks.add(block_id)

            if block:
                distance_ahead += block.length_cm

    def _calculate_braking_distance(self, train) -> float:
        """Calculate braking distance for a train."""
        if hasattr(train, "current_speed") and hasattr(train, "acceleration"):
            speed_cm_s = kmh_to_cm_per_sec(train.current_speed)
            deceleration = train.acceleration * 100  # m/s^2 to cm/s^2
            if deceleration > 0:
                return (speed_cm_s**2) / (2 * deceleration)
        return 0.0

    # ========================================================================
    # Query Methods
    # ========================================================================

    def get_path(self, path_id: str) -> Optional[Path]:
        """Get a path by ID."""
        return self.active_paths.get(path_id)

    def get_active_paths(self) -> List[Path]:
        """Get all active paths."""
        return list(self.active_paths.values())

    def get_paths_using_switch(self, position: Tuple[int, int]) -> List[Path]:
        """Get all paths that use a specific switch."""
        return [
            p
            for p in self.active_paths.values()
            if position in p.switch_positions or position in p.flank_protection_switches
        ]

    # ========================================================================
    # Path Extension for Multi-Tile Destinations
    # ========================================================================

    def _extend_path_to_destination_end(
        self,
        path_tiles: List[Tuple[int, int]],
        end_block_id: str,
    ) -> List[Tuple[int, int]]:
        """
        Extend path tiles to include all tiles of the destination block.

        When a path ends at a multi-tile block (e.g., a station platform),
        the pathfinder stops at the first tile of that block. This method
        extends the path to include all remaining tiles of the destination
        block, ensuring the train can traverse the entire platform.

        Args:
            path_tiles: Original path tiles from pathfinder
            end_block_id: Destination block ID

        Returns:
            Extended path tiles including all destination block tiles
        """
        if not path_tiles:
            return path_tiles

        # Get all positions in the destination block
        dest_block_positions = self.network.get_block_positions(end_block_id)
        if not dest_block_positions or len(dest_block_positions) <= 1:
            return path_tiles  # Single tile block, nothing to extend

        # Find which destination tiles are already in the path
        path_set = set(path_tiles)
        remaining_dest_tiles = [
            pos for pos in dest_block_positions if pos not in path_set
        ]

        if not remaining_dest_tiles:
            return path_tiles  # All destination tiles already in path

        # Determine travel direction from the last few tiles
        if len(path_tiles) >= 2:
            prev_tile = path_tiles[-2]
            last_tile = path_tiles[-1]
            dx = last_tile[0] - prev_tile[0]
            dy = last_tile[1] - prev_tile[1]
        else:
            # Single tile path - try to infer from block layout
            dx, dy = 0, 0

        # Sort remaining destination tiles in travel direction
        if dy > 0:
            # Moving north - sort by y ascending
            remaining_dest_tiles.sort(key=lambda pos: pos[1])
        elif dy < 0:
            # Moving south - sort by y descending
            remaining_dest_tiles.sort(key=lambda pos: -pos[1])
        elif dx > 0:
            # Moving east - sort by x ascending
            remaining_dest_tiles.sort(key=lambda pos: pos[0])
        elif dx < 0:
            # Moving west - sort by x descending
            remaining_dest_tiles.sort(key=lambda pos: -pos[0])
        else:
            # No clear direction - sort by distance from last tile
            last_tile = path_tiles[-1]
            remaining_dest_tiles.sort(
                key=lambda pos: abs(pos[0] - last_tile[0]) + abs(pos[1] - last_tile[1])
            )

        # Extend path with remaining destination tiles
        extended_path = list(path_tiles) + remaining_dest_tiles
        return extended_path
