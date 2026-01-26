"""
Path Utilities

Definition:
This module provides utility functions for train-to-path assignment, including:
- Prepending tiles from train position to path start
- Appending tiles to complete destination block to far edge
- Validating train and path are in same block
- Syncing path_index when train is ahead of path[0]

Key Concepts:
- When a path is assigned to a train, the path should start from the train's position
- Train and path[0] must be in the same block (blocks don't contain switches)
- If train is before path[0], prepend tiles; if train is past path[0], advance path_index
- Path should extend through all tiles of destination block to the far edge
"""

from typing import List, Optional, Tuple, Dict, Any, Union
from dataclasses import dataclass

from enums import Direction


@dataclass
class PathPrepareResult:
    """Result of preparing a path for train assignment."""

    success: bool
    error: Optional[str] = None

    # If prepending tiles at start
    prepend_tiles: Optional[List[Tuple[int, int, Direction]]] = None

    # If appending tiles at end
    append_tiles: Optional[List[Tuple[int, int, Direction]]] = None

    # If train is already on path, which index to start at
    path_index: int = 0


def prepare_path_for_train(
    network,
    train,
    path_tiles: List[Tuple[int, int, Direction]],
) -> PathPrepareResult:
    """
    Prepare a path for assignment to a train by prepending tiles if needed.

    This ensures the path starts at the train's current position:
    1. Validate train position and path[0] are in the same block
    2. If train is before path[0], prepend tiles from train to path[0]
    3. If train is at or past path[0], find correct path_index

    Args:
        network: RailwayNetwork instance
        train: Train to assign path to
        path_tiles: Original path as List[(x, y, Direction)]

    Returns:
        PathPrepareResult with:
        - success: True if path can be assigned
        - error: Error message if not successful
        - prepend_tiles: Tiles to prepend (or None if not needed)
        - path_index: Starting index in path (0 if prepending, or index if train ahead)
    """
    if not path_tiles:
        return PathPrepareResult(success=False, error="Empty path")

    if not train.position:
        return PathPrepareResult(success=False, error="Train has no position")

    train_pos = train.position.head_tile
    path_start = (path_tiles[0][0], path_tiles[0][1])

    # Get block IDs for train position and path start
    train_track = network.get_track(*train_pos)
    path_start_track = network.get_track(*path_start)

    if not train_track:
        return PathPrepareResult(
            success=False, error=f"No track at train position {train_pos}"
        )

    if not path_start_track:
        return PathPrepareResult(
            success=False, error=f"No track at path start {path_start}"
        )

    train_block_id = train_track.block_section_id
    path_start_block_id = path_start_track.block_section_id

    if not train_block_id:
        return PathPrepareResult(
            success=False, error=f"Train at {train_pos} is not in a block"
        )

    if not path_start_block_id:
        return PathPrepareResult(
            success=False, error=f"Path start {path_start} is not in a block"
        )

    # Case 1: Train is exactly at path[0]
    if train_pos == path_start:
        return PathPrepareResult(success=True, path_index=0)

    # Case 2: Check if train position is somewhere in the path
    for i, (px, py, _) in enumerate(path_tiles):
        if (px, py) == train_pos:
            # Train is already at path[i], start from there
            return PathPrepareResult(success=True, path_index=i)

    # Case 3: Train not in path - need to prepend or reject
    # Validate same block
    if train_block_id != path_start_block_id:
        return PathPrepareResult(
            success=False,
            error=f"Train (block {train_block_id}) and path start (block {path_start_block_id}) are in different blocks",
        )

    # Get ordered tiles in the block
    block_positions = _get_block_positions(network, train_block_id)
    if not block_positions:
        return PathPrepareResult(
            success=False, error=f"Block {train_block_id} has no positions"
        )

    # Sort block positions in traversal order
    sorted_positions = _sort_block_positions_for_direction(
        block_positions, network, train.facing_direction
    )

    # Find train position and path_start in sorted list
    try:
        train_idx = None
        path_start_idx = None
        for i, pos in enumerate(sorted_positions):
            if pos == train_pos:
                train_idx = i
            if pos == path_start:
                path_start_idx = i

        if train_idx is None:
            return PathPrepareResult(
                success=False,
                error=f"Train position {train_pos} not found in block {train_block_id}",
            )

        if path_start_idx is None:
            return PathPrepareResult(
                success=False,
                error=f"Path start {path_start} not found in block {train_block_id}",
            )

        if train_idx > path_start_idx:
            # Train is past path start - this shouldn't happen in same block
            # Path start should be ahead of or at train
            return PathPrepareResult(
                success=False,
                error=f"Train at {train_pos} is past path start {path_start} in block",
            )

        # Train is before path start - prepend tiles
        prepend_positions = sorted_positions[train_idx:path_start_idx]

        # Convert positions to path format with directions
        prepend_tiles = _positions_to_path_tiles(
            prepend_positions, train.facing_direction, network
        )

        return PathPrepareResult(
            success=True, prepend_tiles=prepend_tiles, path_index=0
        )

    except Exception as e:
        return PathPrepareResult(success=False, error=f"Error preparing path: {str(e)}")


def _get_block_positions(network, block_id: str) -> List[Tuple[int, int]]:
    """Get all track positions in a block."""
    positions = []
    for pos, track in network.tracks.items():
        if track.block_section_id == block_id:
            positions.append(pos)
    return positions


def _sort_block_positions_for_direction(
    positions: List[Tuple[int, int]], network, facing_direction: Optional[Direction]
) -> List[Tuple[int, int]]:
    """
    Sort block positions in traversal order based on train's facing direction.

    This uses the block's track geometry to determine sort order:
    - NS block + train facing NORTH: sort by y ascending (low to high)
    - NS block + train facing SOUTH: sort by y descending (high to low)
    - EW block + train facing EAST: sort by x ascending (low to high)
    - EW block + train facing WEST: sort by x descending (high to low)

    Args:
        positions: List of (x, y) positions in block
        network: RailwayNetwork for track geometry info
        facing_direction: Train's facing direction

    Returns:
        Sorted positions in order the train would traverse them
    """
    from collections import Counter
    from track import BIT_TO_GROUP

    if len(positions) <= 1:
        return list(positions)

    # Count track bit groups to detect dominant orientation
    orientation_counts = Counter()
    for x, y in positions:
        track = network.get_track(x, y)
        if track:
            for bit_idx, group in BIT_TO_GROUP.items():
                if track.track_bits & (1 << bit_idx):
                    orientation_counts[group] += 1

    if not orientation_counts:
        return list(positions)

    dominant_orientation = orientation_counts.most_common(1)[0][0]

    # Determine sort order based on orientation and facing direction
    if dominant_orientation == "NS":
        if facing_direction == Direction.NORTH:
            # Moving north = ascending y
            return sorted(positions, key=lambda pos: pos[1])
        else:
            # Moving south or unspecified = descending y
            return sorted(positions, key=lambda pos: -pos[1])

    elif dominant_orientation == "EW":
        if facing_direction == Direction.WEST:
            # Moving west = descending x
            return sorted(positions, key=lambda pos: -pos[0])
        else:
            # Moving east or unspecified = ascending x
            return sorted(positions, key=lambda pos: pos[0])

    elif dominant_orientation == "NE_SW":
        if facing_direction in (Direction.NORTH, Direction.EAST):
            # Moving NE direction = ascending (x-y)
            return sorted(positions, key=lambda pos: (pos[0] - pos[1], pos[0]))
        else:
            # Moving SW direction = descending (x-y)
            return sorted(positions, key=lambda pos: (-(pos[0] - pos[1]), -pos[0]))

    elif dominant_orientation == "NW_SE":
        if facing_direction in (Direction.SOUTH, Direction.EAST):
            # Moving SE direction = ascending (x+y)
            return sorted(positions, key=lambda pos: (pos[0] + pos[1], pos[0]))
        else:
            # Moving NW direction = descending (x+y)
            return sorted(positions, key=lambda pos: (-(pos[0] + pos[1]), -pos[0]))

    # Fallback
    return list(positions)


def _positions_to_path_tiles(
    positions: List[Tuple[int, int]], default_direction: Optional[Direction], network
) -> List[Tuple[int, int, Direction]]:
    """
    Convert position list to path tile format with directions.

    Direction is inferred from consecutive tiles:
    - dx > 0: EAST
    - dx < 0: WEST
    - dy > 0: NORTH
    - dy < 0: SOUTH

    Args:
        positions: List of (x, y) positions
        default_direction: Fallback direction if can't infer
        network: RailwayNetwork (unused but available for future)

    Returns:
        List of (x, y, Direction) tuples
    """
    if not positions:
        return []

    result = []
    for i, pos in enumerate(positions):
        if i + 1 < len(positions):
            next_pos = positions[i + 1]
            dx = next_pos[0] - pos[0]
            dy = next_pos[1] - pos[1]

            if dy > 0:
                direction = Direction.NORTH
            elif dy < 0:
                direction = Direction.SOUTH
            elif dx > 0:
                direction = Direction.EAST
            elif dx < 0:
                direction = Direction.WEST
            else:
                direction = default_direction or Direction.EAST
        else:
            # Last tile - use previous direction if available, otherwise fall back
            if result:  # We have at least one previous tile
                direction = result[-1][2]  # Use previous tile's direction
            else:
                direction = default_direction or Direction.EAST

        result.append((pos[0], pos[1], direction))

    return result


def assign_path_to_train_with_prepend(
    network,
    train,
    path_tiles: List[Tuple[int, int, Direction]],
) -> Tuple[bool, Optional[str]]:
    """
    Assign a path to a train, prepending tiles if necessary.

    This is the main entry point for path assignment. It:
    1. Validates and prepares the path using prepare_path_for_train()
    2. Prepends tiles if needed
    3. Sets train.current_path and train.path_index

    Args:
        network: RailwayNetwork instance
        train: Train to assign path to
        path_tiles: Original path as List[(x, y, Direction)]

    Returns:
        (success, error_message)
        - success: True if path was assigned
        - error_message: Error description if failed, None if success
    """
    result = prepare_path_for_train(network, train, path_tiles)

    if not result.success:
        return False, result.error

    # Build final path
    if result.prepend_tiles:
        # Prepend tiles to path
        final_path = result.prepend_tiles + list(path_tiles)
    else:
        final_path = list(path_tiles)

    # Assign to train
    train.current_path = final_path
    train.path_index = result.path_index

    return True, None


def append_destination_block_tiles(
    network,
    path_tiles: List[Tuple[int, int, Direction]],
    exit_direction: Optional[Direction] = None,
) -> Tuple[List[Tuple[int, int, Direction]], Optional[str]]:
    """
    Append remaining tiles of the destination block to complete the path.

    When a path ends at a station/block, the pathfinder stops at the first tile
    of the destination block. This function extends the path to include all
    remaining tiles of that block, ensuring the train stops at the far edge.

    Args:
        network: RailwayNetwork instance
        path_tiles: Original path as List[(x, y, Direction)]
        exit_direction: Direction the train is traveling (inferred if not provided)

    Returns:
        (extended_path_tiles, error_message)
        - extended_path_tiles: Path with destination block tiles appended
        - error_message: Error if failed, None if success
    """
    if not path_tiles:
        return path_tiles, "Empty path"

    # Get the last tile and its direction
    last_tile = path_tiles[-1]
    last_pos = (last_tile[0], last_tile[1])
    last_direction = last_tile[2] if len(last_tile) > 2 else exit_direction

    # Get track at last position
    track = network.get_track(*last_pos)
    if not track:
        return list(path_tiles), None  # No track, return as-is

    # Get block ID of the last tile
    block_id = track.block_section_id
    if not block_id:
        return list(path_tiles), None  # No block, return as-is

    # Get all positions in the destination block
    block_positions = _get_block_positions(network, block_id)
    if not block_positions or len(block_positions) <= 1:
        return list(path_tiles), None  # Single tile block, nothing to append

    # Determine exit direction from the path
    if not last_direction and len(path_tiles) >= 2:
        prev_tile = path_tiles[-2]
        dx = last_tile[0] - prev_tile[0]
        dy = last_tile[1] - prev_tile[1]
        if dy > 0:
            last_direction = Direction.NORTH
        elif dy < 0:
            last_direction = Direction.SOUTH
        elif dx > 0:
            last_direction = Direction.EAST
        elif dx < 0:
            last_direction = Direction.WEST

    if not last_direction:
        return list(path_tiles), None  # Can't determine direction

    # Sort block positions in traversal order
    sorted_positions = _sort_block_positions_for_direction(
        block_positions, network, last_direction
    )

    # Find the last tile's index in the sorted block positions
    try:
        last_idx = None
        for i, pos in enumerate(sorted_positions):
            if pos == last_pos:
                last_idx = i
                break

        if last_idx is None:
            return list(
                path_tiles
            ), f"Last tile {last_pos} not found in block {block_id}"

        # Get tiles after the last tile in the block (remaining tiles to append)
        remaining_positions = sorted_positions[last_idx + 1 :]

        if not remaining_positions:
            return list(path_tiles), None  # Already at the end of the block

        # Convert remaining positions to path tiles
        append_tiles = _positions_to_path_tiles(
            remaining_positions, last_direction, network
        )

        # Extend the path
        extended_path = list(path_tiles) + append_tiles
        return extended_path, None

    except Exception as e:
        return list(path_tiles), f"Error appending destination tiles: {str(e)}"


def prepare_complete_path(
    network,
    train,
    path_tiles: List[Tuple[int, int, Direction]],
) -> PathPrepareResult:
    """
    Prepare a complete path for train assignment.

    This combines:
    1. Prepending tiles at the start (from train position to path start)
    2. Appending tiles at the end (remaining tiles of destination block)

    Args:
        network: RailwayNetwork instance
        train: Train to assign path to
        path_tiles: Original path as List[(x, y, Direction)]

    Returns:
        PathPrepareResult with prepend_tiles, append_tiles, and path_index
    """
    # First, handle the start (prepending)
    result = prepare_path_for_train(network, train, path_tiles)

    if not result.success:
        return result

    # Now handle the end (appending)
    # Get the direction from the last tile
    exit_direction = None
    if path_tiles:
        last_tile = path_tiles[-1]
        exit_direction = last_tile[2] if len(last_tile) > 2 else None

    # Append destination block tiles
    extended_tiles, append_error = append_destination_block_tiles(
        network, path_tiles, exit_direction
    )

    if append_error:
        # Non-fatal - just use original path without appending
        result.append_tiles = None
    elif len(extended_tiles) > len(path_tiles):
        # Calculate the appended tiles (difference between extended and original)
        append_count = len(extended_tiles) - len(path_tiles)
        result.append_tiles = extended_tiles[-append_count:]

    return result


def assign_complete_path_to_train(
    network,
    train,
    path_tiles: List[Tuple[int, int, Direction]],
) -> Tuple[bool, Optional[str]]:
    """
    Assign a complete path to a train, with both prepending and appending.

    This is the recommended entry point for path assignment. It:
    1. Prepends tiles from train position to path start
    2. Appends tiles to complete the destination block
    3. Sets train.current_path and train.path_index

    Args:
        network: RailwayNetwork instance
        train: Train to assign path to
        path_tiles: Original path as List[(x, y, Direction)]

    Returns:
        (success, error_message)
        - success: True if path was assigned
        - error_message: Error description if failed, None if success
    """
    result = prepare_complete_path(network, train, path_tiles)

    if not result.success:
        return False, result.error

    # Build final path
    final_path = list(path_tiles)

    if result.prepend_tiles:
        final_path = result.prepend_tiles + final_path

    if result.append_tiles:
        final_path = final_path + result.append_tiles

    # Assign to train
    train.current_path = final_path
    train.path_index = result.path_index

    return True, None


def convert_tiles_to_train_path(
    tiles: List[Tuple[int, int]], default_direction: Direction = Direction.EAST
) -> List[Tuple[int, int, Direction]]:
    """Convert tile positions to train path format with directions.

    Args:
        tiles: List of (x, y) tile positions
        default_direction: Direction for single-tile paths (default: EAST)

    Returns:
        List of (x, y, direction) tuples for train path
    """
    if not tiles:
        return []

    result = []
    last_direction = default_direction

    for i, tile in enumerate(tiles):
        if i + 1 < len(tiles):
            next_tile = tiles[i + 1]
            dx = next_tile[0] - tile[0]
            dy = next_tile[1] - tile[1]

            if dy > 0:
                direction = Direction.NORTH
            elif dy < 0:
                direction = Direction.SOUTH
            elif dx > 0:
                direction = Direction.EAST
            else:
                direction = Direction.WEST
            last_direction = direction
        else:
            direction = last_direction

        result.append((tile[0], tile[1], direction))

    return result
