"""
Path Utilities

Definition:
This module provides utility functions for train-to-path assignment, including:
- Prepending tiles from train position to path start
- Appending tiles to complete destination block to far edge
- Validating train and path are in same block

Key Concepts:
- When a path is assigned to a train, the path should start from the train's position
- Train and path[0] must be in the same block (blocks don't contain switches)
- Path should extend through all tiles of destination block to the far edge
- The dispatcher's `Path` is the source of truth; these helpers only validate.
"""

from typing import List, Literal, Optional, Set, Tuple, Dict, Any
from dataclasses import dataclass, field

from enums import Direction


# =============================================================================
# Graph walk types (canonical home: the pathfinding function table and
# GraphWalkCandidate payload in design_new.md "Path And PathDispatcher")
# =============================================================================


@dataclass
class GraphLocator:
    """Tile-precise locator used by the operational graph query API.

    Canonical home: design_new.md "Pathfinding home" subsection.

    Fields:
        tile: Tile coordinate anchoring the locator.
        resource_type: Resource type containing the locator, when known.
        resource_id: Resource ID containing the locator, when known.
        block_id: Convenience block ID when the locator is inside a block;
            None for switch-only locators.
        edge: Tile edge when the locator is anchored at a graph boundary or
            the caller knows the relevant edge.
        distance_cm: In-tile distance from `edge` along the directed traversal
            when the caller has a precise position; None for tile-only searches.
        physical_end: Set only for current-train-span start locators so the
            caller can apply the final `end_a` dead-heat tie-break outside the
            graph query.
    """

    tile: Tuple[int, int]
    resource_type: Optional[Literal["block", "switch"]] = None
    resource_id: Optional[str] = None
    block_id: Optional[str] = None
    edge: Optional[Literal["N", "E", "S", "W"]] = None
    distance_cm: Optional[float] = None
    physical_end: Optional[Literal["end_a", "end_b"]] = None


@dataclass
class DirectedBlockFrame:
    """Terminal directed block frame returned inside GraphWalkCandidate.

    Canonical home: design_new.md "Pathfinding home" subsection.

    Fields:
        block_id: Block whose directed traversal is represented.
        block_direction: Direction relative to Block.ordered_tiles.
        path_tiles: Ordered tile slice through the block as (x, y, Direction)
            tuples.
        entry_edge: Edge by which the traversal enters the block frame.
        exit_edge: Edge by which the traversal exits the block frame.
    """

    block_id: str
    block_direction: Literal["forward", "reverse"]
    path_tiles: List[Tuple[int, int, Direction]]
    entry_edge: Literal["N", "E", "S", "W"]
    exit_edge: Literal["N", "E", "S", "W"]


@dataclass
class GraphWalkStep:
    """One resource step inside an ordered_walk list.

    Canonical home: design_new.md "Pathfinding home" subsection.

    Fields:
        step_index: Zero-based step index inside ordered_walk.
        resource_type: Resource traversed by this step.
        resource_id: Block or switch ID for the traversed resource.
        tile: Tile coordinate for this graph step.
        entry_edge: Edge used to enter this tile/resource step.
        exit_edge: Edge used to exit this tile/resource step.
        block_id: Block ID when resource_type == "block".
        block_direction: Block-relative traversal direction when the step is
            inside a block.
        switch_required_state: Required switch state for switch traversal when
            topology search determines one.
        length_cm: Physical length contribution of this step.
    """

    step_index: int
    resource_type: Literal["block", "switch"]
    resource_id: str
    tile: Tuple[int, int]
    entry_edge: Literal["N", "E", "S", "W"]
    exit_edge: Literal["N", "E", "S", "W"]
    block_id: Optional[str] = None
    block_direction: Optional[Literal["forward", "reverse"]] = None
    switch_required_state: Optional[int] = None
    length_cm: float = 0.0


@dataclass
class GraphWalkQuery:
    """Request payload for RailwayPathfinder.query_graph_walks(...).

    Canonical home: design_new.md "Pathfinding home" subsection.

    Fields:
        start_locator: Tile-precise start locator.
        end_locator: Tile-precise end locator.
        switch_handling: Whether the search respects current Switch.position
            or searches topology-only for later acquisition.
        requested_destination_direction: Optional terminal direction filter;
            candidates whose terminal traversal does not match are rejected.
        exclude_resources: Optional set of resource IDs to exclude from the
            search (used by retry / extension logic).
        return_tiles: Whether to populate GraphWalkCandidate.ordered_tiles.
        return_blocks: Whether to populate GraphWalkCandidate.ordered_blocks.
    """

    start_locator: GraphLocator
    end_locator: GraphLocator
    switch_handling: Literal["respect_current_state", "topology_only"] = (
        "topology_only"
    )
    requested_destination_direction: Optional[Literal["forward", "reverse"]] = None
    exclude_resources: Optional[Set[str]] = None
    return_tiles: bool = True
    return_blocks: bool = True
    manual_override: bool = False
    """Operator-override flag per the `disabled` and `manual` availability flags
    canonical home in design_new.md (under TrackResource). When False (every
    automatic caller), manual blocks/switches are filtered out exactly like
    disabled ones. When True (operator-initiated paths only), manual resources
    participate normally. Disabled resources are always filtered regardless."""

    train_min_radius_cm: Optional[float] = None
    """The train's minimum curve radius capability (centimetres), or None when
    the caller has no train context (geometry-only queries). When set, the
    pathfinder rejects any candidate that traverses a block or switch leg whose
    curve radius is tighter than this value -- the train physically cannot take
    that curve. None disables the check entirely. ``manual_override`` does NOT
    relax it: a curve the train cannot navigate is a physical limit, not an
    availability policy."""

    train_max_speed_kmh: Optional[float] = None
    """The routed train's maximum speed (km/h), or None when the caller has no
    train context (geometry-only queries). When set, the pathfinder ranks
    surviving candidates by estimated travel time -- the sum over the walk of
    each segment's length divided by its effective speed (the posted
    ``Block.speed_limit_kmh`` capped at this max, or this max where no limit is
    posted) -- so a faster route wins over an equal-or-shorter-distance slow
    one. ``tile_count`` and ``walk_length_cm`` remain the tie-breakers. None
    falls back to the distance-only ``GraphWalkCandidate.comparison_key()``
    ordering, which every geometry-only caller keeps."""

    train_max_slope_percent: Optional[float] = None
    """The routed train's maximum climbable gradient (percent grade), or None
    when the caller has no train context, or 0.0 (the ``Train.max_slope``
    "unspecified" sentinel) to disable the check. When set above zero, the
    pathfinder rejects any candidate that climbs a block whose uphill gradient
    in the traversed direction exceeds this value -- the train would stall on
    that grade. ``Block.grade_percent`` is signed (+ uphill in the block's
    forward direction), so a reverse traversal flips the sign; only an uphill
    climb steeper than the train can manage rejects, a descent never does.
    ``manual_override`` does NOT relax it: a grade the train cannot climb is a
    physical limit, not an availability policy."""


@dataclass(order=False)
class GraphWalkCandidate:
    """Canonical candidate-comparison payload per design_new.md.

    Returned by the shared `RailwayPathfinder.find_ordered_walk(...)`
    helper. Every site that compares candidate paths — `RouteExecutor`
    platform-end resolution, `PathDispatcher.create_path` start-side
    selection, station-target candidate enumeration — uses this exact
    shape and the canonical comparison ordering defined here so the two
    layers cannot drift on tie-break rules.

    Comparison order (lowest wins):
      1. `tile_count` — fewer tiles is better
      2. `walk_length_cm` — shorter physical distance is better
      3. `candidate_rank` — caller-provided secondary rank (lower is
         better); used by route logic to honour explicit candidate
         ordering such as "earlier in `Station.platform_ids`"
      4. `end_a` dead-heat — when every prior key is exactly equal,
         prefer the candidate whose start side is `end_a`. This is the
         final deterministic tie-break.

    Use `comparison_key()` to obtain a sortable tuple matching this
    order.
    """

    tile_count: int
    walk_length_cm: float
    candidate_rank: int = 0
    # Which physical end of the train this candidate started from.
    # `"end_a"` or `"end_b"`; the former wins all dead-heats per the
    # canonical tie-break rule.
    end_a: Literal["end_a", "end_b"] = "end_a"
    # Optional payload carried alongside the comparison metrics. The
    # walk ordering helper consumes only the metric fields; downstream
    # callers (e.g. PathDispatcher) read `walk` to build
    # `Path.resource_sequence` and `metadata` for diagnostics.
    walk: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # --- Canonical graph-walk API fields (the GraphWalkCandidate payload in
    # design_new.md "Path And PathDispatcher") ---

    # Canonical directed walk over graph steps in traversal order. Each step
    # contains enough resource/tile/frame data for dispatcher acquisition,
    # route target comparison, and later distance projection.
    ordered_walk: Optional[List[GraphWalkStep]] = None
    # Ordered tile IDs for callers that requested tile payload. None when
    # return_tiles=False in the originating GraphWalkQuery.
    ordered_tiles: Optional[List[Tuple[int, int]]] = None
    # Ordered block IDs visited by the walk. None when return_blocks=False.
    ordered_blocks: Optional[List[str]] = None
    # The resolved tile-precise start locator used by the graph search.
    start_locator: Optional[GraphLocator] = None
    # The resolved tile-precise terminal locator reached by the candidate.
    terminal_locator: Optional[GraphLocator] = None
    # Block ID of the terminal block reached by the candidate.
    terminal_block_id: Optional[str] = None
    # Terminal block-relative traversal direction derived from the candidate's
    # directed frame against Block.ordered_tiles.
    terminal_block_direction: Optional[Literal["forward", "reverse"]] = None
    # Terminal directed block frame, including path tiles plus entry_edge /
    # exit_edge metadata needed by dispatcher/controller projection helpers.
    terminal_frame: Optional[DirectedBlockFrame] = None

    def comparison_key(self) -> Tuple[int, float, int, int]:
        """Return the sortable tuple for canonical candidate comparison.

        `end_a` is encoded as `0` for end_a and `1` for end_b so end_a
        wins all dead-heats per the design's tie-break rule.
        """
        return (
            self.tile_count,
            self.walk_length_cm,
            self.candidate_rank,
            0 if self.end_a == "end_a" else 1,
        )


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


def _resolve_head_tile(controller):
    """Train head tile from its controller's cab-derived span (design_new.md).
    Head/tail live on the controller, not on Train."""
    return controller._get_head_tile() if controller is not None else None


def _resolve_facing(controller):
    """Train facing `Direction` from its controller (cab + span)."""
    return controller._facing_direction() if controller is not None else None


def prepare_path_for_train(
    network,
    train,
    path_tiles: List[Tuple[int, int, Direction]],
    controller=None,
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

    train_pos = _resolve_head_tile(controller)
    if train_pos is None:
        return PathPrepareResult(success=False, error="Train has no position")

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
        block_positions, network, _resolve_facing(controller)
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
            # The train is "ahead" of the path start in the current facing
            # direction, which means the new path goes in the reverse
            # direction.  Return success with no prepend tiles so that
            # _path_needs_cab_flip() can detect the reversal and flip the
            # cab correctly.  (The old hard failure prevented the head tile
            # from ever being added to the path tile list, which caused
            # _path_needs_cab_flip() to return False and let the train
            # accelerate in the wrong direction.)
            return PathPrepareResult(success=True, prepend_tiles=[], path_index=0)

        # Train is before path start - prepend tiles
        prepend_positions = sorted_positions[train_idx:path_start_idx]

        # Convert positions to path format with directions
        prepend_tiles = _positions_to_path_tiles(
            prepend_positions, _resolve_facing(controller), network
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
    positions: List[Tuple[int, int]], default_direction: Optional[Direction], _network
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
        _network: RailwayNetwork (unused; reserved for future track-aware inference)

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
    controller=None,
) -> Tuple[bool, Optional[str]]:
    """
    Validate and prepare a path for a train.

    `Train.current_path` and `Train.path_index` are not part of v1 — the
    dispatcher's `Path` is the source of truth for path
    geometry. This helper now only runs the validation logic and returns
    its outcome; UI consumers read the active path tiles from
    `PathDispatcher.get_active_path_progress(train_id)`.

    Args:
        network: RailwayNetwork instance
        train: Train to validate against
        path_tiles: Original path as List[(x, y, Direction)]

    Returns:
        (success, error_message)
    """
    result = prepare_path_for_train(network, train, path_tiles, controller=controller)
    if not result.success:
        return False, result.error
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
    controller=None,
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
    result = prepare_path_for_train(network, train, path_tiles, controller=controller)

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
    controller=None,
) -> Tuple[bool, Optional[str]]:
    """
    Validate and prepare a complete path (prepend + append) for a train.

    `Train.current_path` and `Train.path_index` are not part of v1 — the
    dispatcher's `Path` is the source of truth. This
    helper now only validates the path and returns its outcome.

    Args:
        network: RailwayNetwork instance
        train: Train to validate against
        path_tiles: Original path as List[(x, y, Direction)]

    Returns:
        (success, error_message)
    """
    result = prepare_complete_path(network, train, path_tiles, controller=controller)
    if not result.success:
        return False, result.error
    return True, None


_EDGE_TO_DIR: dict = {
    "N": Direction.NORTH,
    "E": Direction.EAST,
    "S": Direction.SOUTH,
    "W": Direction.WEST,
}
_OPPOSITE_EDGE: dict = {"N": "S", "S": "N", "E": "W", "W": "E"}


def convert_tiles_to_train_path(
    tiles,
    default_direction: Direction = Direction.EAST,
    network=None,
) -> List[Tuple[int, int, Direction]]:
    """Convert tile positions to train path format with directions.

    Args:
        tiles: Iterable of `(x, y)` tuples OR `PathTileEntry` instances. The
            function reads `(x, y)` from either shape.
        default_direction: Direction for single-tile paths (default: EAST).
        network: Optional ``RailwayNetwork``. When provided, the per-tile
            direction is derived from
            ``network.get_connection_edges(tile, next_tile)`` so the result
            is correct across tunnel hops (where consecutive path tiles are
            not grid-adjacent and `(dx, dy)` is meaningless). When omitted,
            the legacy `(dx, dy)` inference is used — fine for grid-adjacent
            paths but wrong for tunnel hops.

    Returns:
        List of (x, y, direction) tuples for train path.
    """
    if not tiles:
        return []

    def _xy(item):
        if hasattr(item, "x") and hasattr(item, "y"):
            return (item.x, item.y)
        return (item[0], item[1])

    coords = [_xy(t) for t in tiles]

    def _dir_from_dx_dy(a, b) -> Optional[Direction]:
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dy > 0:
            return Direction.NORTH
        if dy < 0:
            return Direction.SOUTH
        if dx > 0:
            return Direction.EAST
        if dx < 0:
            return Direction.WEST
        return None

    def _dir_for(i: int) -> Optional[Direction]:
        # Prefer the exit edge from coords[i] toward coords[i+1] when a
        # network is available — this works whether the pair is grid-
        # adjacent or linked by a tunnel.
        if i + 1 < len(coords):
            if network is not None:
                edges = network.get_connection_edges(coords[i], coords[i + 1])
                if edges is not None:
                    return _EDGE_TO_DIR.get(edges[0])
            return _dir_from_dx_dy(coords[i], coords[i + 1])
        # Last tile: derive from the previous tile's connection so a path
        # that ends just past a tunnel still reports a correct direction.
        if i > 0 and network is not None:
            edges = network.get_connection_edges(coords[i - 1], coords[i])
            if edges is not None:
                entry_on_self = edges[1]
                forward_edge = _OPPOSITE_EDGE.get(entry_on_self)
                if forward_edge:
                    return _EDGE_TO_DIR.get(forward_edge)
        return None

    result = []
    last_direction = default_direction
    for i, tile in enumerate(coords):
        d = _dir_for(i)
        if d is not None:
            last_direction = d
        result.append((tile[0], tile[1], last_direction))

    return result
