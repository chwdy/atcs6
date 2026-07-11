"""
Block validation module for the railway signaling system.

Validates that blocks are created and modified correctly according to
railway signaling rules.
"""

from typing import Tuple, List, Optional, Set
from collections import deque


def validate_station_boundary(
    path: List[Tuple[int, int]], network
) -> Tuple[bool, Optional[str]]:
    """
    Validate that a block path respects station boundaries.

    Rules:
    1. A block cannot span station boundaries (partially in, partially out)
    2. A block can be fully inside a station
    3. A block can be fully outside all stations

    Args:
        path: List of (x, y) tile positions
        network: RailwayNetwork instance for lookups

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not path:
        return True, None

    # Collect station_ids for all tiles in the path
    station_ids = set()
    for x, y in path:
        track = network.get_track(x, y)
        if track and track.station_id:
            station_ids.add(track.station_id)

    # If no stations involved, block is outside all stations - OK
    if len(station_ids) == 0:
        return True, None

    # If exactly one station, all tiles must belong to that station
    if len(station_ids) == 1:
        station_id = list(station_ids)[0]
        # Verify ALL tiles belong to this station
        for x, y in path:
            track = network.get_track(x, y)
            if not track or track.station_id != station_id:
                return (
                    False,
                    f"Block cannot cross station boundary - tile ({x}, {y}) is outside station {station_id}",
                )
        return True, None

    # Multiple stations - this is not allowed
    station_list = ", ".join(sorted(station_ids))
    return False, f"Block cannot span multiple stations: {station_list}"


def is_crossing(track_bits: int) -> bool:
    """
    Check if track_bits represents a crossing (+ shape).

    A crossing is NS + EW combined (track_bits = 3), which creates
    two isolated paths that cross but don't connect.

    Args:
        track_bits: 6-bit track configuration

    Returns:
        True if this is a crossing
    """
    # NS (1) + EW (2) = 3 is a standard + crossing
    # Also check for diagonal crossings: NE+SW (36) and NW+SE (24)
    return track_bits in (3, 24, 36)


# Crossings whose tile may still belong to a block. The orthogonal NS+EW
# crossing (track_bits 3) is allowed: the whole tile is treated as a single
# occupancy unit, so a block can run straight through it on one axis. The
# diagonal crossings (24 = NW+SE, 36 = NE+SW) stay excluded.
BLOCK_ALLOWED_CROSSINGS = frozenset({3})


def crossing_disallowed_in_block(track_bits: int) -> bool:
    """
    Check if track_bits is a crossing that cannot belong to a block.

    All crossings isolate their two paths, but the orthogonal NS+EW crossing
    (3) is permitted inside a block; see BLOCK_ALLOWED_CROSSINGS. The diagonal
    crossings (24, 36) remain disallowed.

    Args:
        track_bits: 6-bit track configuration

    Returns:
        True if this crossing must be kept out of blocks
    """
    return is_crossing(track_bits) and track_bits not in BLOCK_ALLOWED_CROSSINGS


def validate_track_for_block(track, network) -> Tuple[bool, str]:
    """
    Validate if a track can be added to a block.

    Rules:
    1. Track must exist
    2. Track cannot be a switch (switches are block boundaries)
    3. Track cannot be a disallowed crossing (diagonal crossings isolate paths;
       the orthogonal NS+EW crossing is allowed - see crossing_disallowed_in_block)

    Note: This function does NOT validate station boundaries. Station boundary
    validation is performed separately by validate_station_boundary(), which
    ensures blocks are fully inside OR fully outside stations.

    Stations are allowed in blocks - a block can exist fully within a station.

    Args:
        track: Tile to validate
        network: RailwayNetwork instance for lookups

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not track:
        return False, "Track does not exist"

    if track.is_switch:
        return False, "Cannot add switch to block - switches are block boundaries"

    # Rule 3: Disallowed crossings. The orthogonal NS+EW crossing may belong to
    # a block; diagonal crossings may not (see crossing_disallowed_in_block).
    if crossing_disallowed_in_block(track.track_bits):
        return (
            False,
            "Cannot add crossing to block - crossings isolate paths and blocks cannot span them",
        )

    return True, ""


def validate_block_assignment_continuity(
    existing_positions: List[Tuple[int, int]],
    new_positions: List[Tuple[int, int]],
    network,
) -> Tuple[bool, List[str]]:
    """
    Validate that adding new positions maintains block continuity.

    Uses BFS to check all positions (existing + new) form a connected path.

    Args:
        existing_positions: Current block positions
        new_positions: Positions to add
        network: RailwayNetwork instance

    Returns:
        Tuple of (is_continuous, error_messages)
    """
    if not new_positions:
        return True, []

    all_positions = set(existing_positions + new_positions)
    if not all_positions:
        return True, []

    # BFS from first position to check connectivity
    start_pos = list(all_positions)[0]
    visited = set()
    queue = deque([start_pos])
    visited.add(start_pos)

    while queue:
        current = queue.popleft()
        x, y = current

        # Check all 4 cardinal neighbors
        neighbors = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]

        for neighbor in neighbors:
            if neighbor in all_positions and neighbor not in visited:
                # Check if there's actually a track connection
                current_track = network.get_track(x, y)
                neighbor_track = network.get_track(neighbor[0], neighbor[1])

                if current_track and neighbor_track:
                    # Check if tracks are connected via their edges
                    if _tracks_are_connected(current_track, neighbor_track, network):
                        visited.add(neighbor)
                        queue.append(neighbor)

    # Check if all positions were reached
    if len(visited) != len(all_positions):
        unreached = all_positions - visited
        errors = [
            f"Block would be discontinuous. Unreached positions: {list(unreached)[:5]}"
        ]
        if len(unreached) > 5:
            errors.append(f"...and {len(unreached) - 5} more")
        return False, errors

    return True, []


def _tracks_are_connected(track1, track2, network) -> bool:
    """
    Check if two adjacent tracks are actually connected via their track geometry.

    Args:
        track1: First track segment
        track2: Second track segment (must be adjacent to track1)
        network: RailwayNetwork instance

    Returns:
        bool: True if tracks are connected via their edges
    """
    # Calculate direction from track1 to track2
    dx = track2.x - track1.x
    dy = track2.y - track1.y

    # Must be exactly one tile apart
    if abs(dx) + abs(dy) != 1:
        return False

    # Map direction to edge
    direction_to_edge = {
        (1, 0): "east",
        (-1, 0): "west",
        (0, 1): "north",
        (0, -1): "south",
    }

    edge1 = direction_to_edge.get((dx, dy))
    edge2 = direction_to_edge.get((-dx, -dy))  # Opposite direction

    if not edge1 or not edge2:
        return False

    # Get available edges from track bits
    edges1 = _get_edges_from_track_bits(track1.track_bits)
    edges2 = _get_edges_from_track_bits(track2.track_bits)

    # Tracks are connected if track1 has an edge toward track2 and vice versa
    return edge1 in edges1 and edge2 in edges2


def _get_edges_from_track_bits(track_bits: int) -> Set[str]:
    """
    Extract edge connections from track_bits.

    Track bits encoding: Each bit represents a pair of connected edges
    - Bit 0 (value 1): North-South connection
    - Bit 1 (value 2): East-West connection
    - Bit 2 (value 4): North-East connection
    - Bit 3 (value 8): North-West connection
    - Bit 4 (value 16): South-East connection
    - Bit 5 (value 32): South-West connection

    Args:
        track_bits: Bitmask representing track connections

    Returns:
        Set of edge names: {'north', 'south', 'east', 'west'}
    """
    # Bit index -> edge pair mapping
    BIT_TO_EDGES = {
        0: ("north", "south"),  # TRACK_NS = 1
        1: ("east", "west"),  # TRACK_EW = 2
        2: ("north", "east"),  # TRACK_NE = 4
        3: ("north", "west"),  # TRACK_NW = 8
        4: ("south", "east"),  # TRACK_SE = 16
        5: ("south", "west"),  # TRACK_SW = 32
    }

    edges = set()

    # Check each bit position
    for bit_index, (edge1, edge2) in BIT_TO_EDGES.items():
        if track_bits & (1 << bit_index):
            edges.add(edge1)
            edges.add(edge2)

    return edges


def validate_block_properties(
    block_id: str, name: str, length_cm: float, speed_limit_kmh: Optional[float]
) -> Tuple[bool, List[str]]:
    """
    Validate block properties are reasonable.

    Rules:
    1. block_id must be non-empty and valid format
    2. name should be non-empty (warning only)
    3. length_cm must be >= 0
    4. speed_limit_kmh must be > 0 if provided
    """
    errors = []

    if not block_id or not block_id.strip():
        errors.append("block_id cannot be empty")

    if length_cm < 0:
        errors.append(f"length_cm cannot be negative: {length_cm}")

    if speed_limit_kmh is not None and speed_limit_kmh <= 0:
        errors.append(f"speed_limit_kmh must be positive: {speed_limit_kmh}")

    return len(errors) == 0, errors


def get_adjacent_positions(x: int, y: int) -> List[Tuple[int, int]]:
    """
    Get the 4 cardinal neighbor positions for a tile.

    Args:
        x, y: Tile position

    Returns:
        List of (x, y) tuples for adjacent positions
    """
    return [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
