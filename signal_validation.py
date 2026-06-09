"""
Signal validation rules for the railway signaling system.

This module contains the validation logic that determines where signals can be
placed and which directions are valid for each track type.
"""

from typing import Set, Dict, List, Tuple, Optional


# Track bits that CANNOT have signals placed on them
# These are junctions, crossings, and complex track configurations
FORBIDDEN_SIGNAL_BITS: Set[int] = {
    # Crossings (perpendicular tracks)
    3,   # NS + EW crossing
    24,  # NE + SW crossing
    36,  # NW + SE crossing

    # Y-switches (NS track + diagonal)
    5,   # NS + NE
    9,   # NS + NW
    17,  # NS + SE
    33,  # NS + SW

    # Y-switches (EW track + diagonal)
    6,   # EW + NE
    10,  # EW + NW
    18,  # EW + SE
    34,  # EW + SW

    # Diagonal switches
    12,  # NE + NW (north junction)
    20,  # NE + SE (east junction)
    40,  # NW + SW (west junction)
    48,  # SE + SW (south junction)

    # Complex junctions (3+ connections)
    7,   # NS + EW + NE
    11,  # NS + EW + NW
    19,  # NS + EW + SE
    35,  # NS + EW + SW
    14,  # EW + NE + NW
    38,  # EW + NW + SE
    26,  # EW + NE + SE
    50,  # EW + SE + SW
    21,  # NS + NE + SE
    37,  # NS + NE + SW
    25,  # NS + NW + SE
    41,  # NS + NW + SW
}


# Valid detection directions for each simple track type
# Key: track_bits, Value: list of valid detection_directions (where trains travel TO)
VALID_DETECTION_DIRECTIONS: Dict[int, List[str]] = {
    1:  ['N', 'S'],      # NS straight track - trains can go N or S
    2:  ['E', 'W'],      # EW straight track - trains can go E or W
    4:  ['N', 'E'],      # NE diagonal - trains can go N or E
    8:  ['N', 'W'],      # NW diagonal - trains can go N or W
    16: ['S', 'E'],      # SE diagonal - trains can go S or E
    32: ['S', 'W'],      # SW diagonal - trains can go S or W
}


# Direction to movement vector mapping
# Uses isometric grid convention: North = +Y, South = -Y (matches edge_offsets in network.py)
DIRECTION_VECTORS: Dict[str, Tuple[int, int]] = {
    'N': (0, 1),
    'S': (0, -1),
    'E': (1, 0),
    'W': (-1, 0),
}


def can_place_signal(track_bits: int) -> bool:
    """
    Check if a signal can be placed on a track with the given bits.

    Args:
        track_bits: The track configuration bits

    Returns:
        True if the track can have a signal, False otherwise
    """
    if track_bits == 0:
        return False  # No track
    return track_bits not in FORBIDDEN_SIGNAL_BITS


def get_valid_directions(track_bits: int) -> List[str]:
    """
    Get the list of valid detection directions for a given track type.

    Detection direction is where trains travel TO (the direction behind the signal).

    Args:
        track_bits: The track configuration bits

    Returns:
        List of valid detection direction strings, empty if no signals allowed
    """
    if not can_place_signal(track_bits):
        return []
    return VALID_DETECTION_DIRECTIONS.get(track_bits, [])


def is_valid_direction(track_bits: int, detection_direction: str) -> bool:
    """
    Check if a specific detection direction is valid for a signal on the given track.

    Args:
        track_bits: The track configuration bits
        detection_direction: The detection direction to check (where trains travel TO)

    Returns:
        True if the detection direction is valid, False otherwise
    """
    return detection_direction.upper() in get_valid_directions(track_bits)


def get_direction_vector(direction: str) -> Optional[Tuple[int, int]]:
    """
    Get the movement vector for a direction.

    Args:
        direction: The direction string (NORTH, SOUTH, EAST, WEST)

    Returns:
        Tuple of (dx, dy) or None if invalid direction
    """
    return DIRECTION_VECTORS.get(direction.upper())


def validate_signal_placement(track_bits: int, detection_direction: str) -> Tuple[bool, Optional[str]]:
    """
    Validate if a signal can be placed with the given parameters.

    Args:
        track_bits: The track configuration bits
        detection_direction: The detection direction (where trains travel TO)

    Returns:
        Tuple of (is_valid, error_message)
        error_message is None if valid
    """
    if track_bits == 0:
        return False, "Cannot place signal: no track at this position"

    if not can_place_signal(track_bits):
        return False, "Cannot place signal on switches, crossings, or complex junctions"

    if not is_valid_direction(track_bits, detection_direction):
        valid_dirs = get_valid_directions(track_bits)
        if valid_dirs:
            return False, f"Invalid detection direction '{detection_direction}' for this track. Valid directions: {valid_dirs}"
        else:
            return False, f"No valid signal directions for track_bits={track_bits}"

    return True, None
