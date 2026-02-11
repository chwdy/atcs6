"""
Helpers for setting TrainPosition in tests.
"""

from typing import List, Optional, Tuple

from enums import Direction, LeadingEnd
from train import TrainPosition


def make_train_position(
    head_tile: Tuple[int, int],
    facing_direction: Optional[Direction] = None,
    entry_edge: str = "W",
    head_distance_cm: float = 0.0,
    occupied_tiles: Optional[List[Tuple[int, int]]] = None,
    occupied_blocks: Optional[List[str]] = None,
) -> TrainPosition:
    """Create a minimal TrainPosition for tests.

    Note: facing_direction is now computed from position geometry.
    For single-tile trains, the entry_edge is set based on facing_direction.

    Args:
        head_tile: Position of the head
        facing_direction: Desired facing direction (sets entry_edge accordingly)
        entry_edge: Manual entry edge (ignored if facing_direction is provided)
        head_distance_cm: Distance from entry edge
        occupied_tiles: List of tiles (if None, uses single tile)
        occupied_blocks: List of block IDs
    """
    tiles = occupied_tiles or [head_tile]
    blocks = occupied_blocks or []

    # If facing_direction is provided, calculate the correct entry_edge
    # For a single-tile train facing a direction, it entered from the opposite side
    if facing_direction:
        direction_to_entry = {
            Direction.NORTH: "S",  # Facing north means entered from south
            Direction.SOUTH: "N",
            Direction.EAST: "W",
            Direction.WEST: "E",
        }
        entry_edge = direction_to_entry.get(facing_direction, entry_edge)

    return TrainPosition(
        end_a_tile=head_tile,
        end_a_distance_cm=head_distance_cm,
        end_a_entry_edge=entry_edge,
        end_b_tile=head_tile,
        end_b_distance_cm=head_distance_cm,
        end_b_entry_edge=entry_edge,
        leading_end=LeadingEnd.A,
        occupied_tiles=tiles,
        occupied_blocks=blocks,
    )
