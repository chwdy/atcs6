"""
Helpers for setting TrainPosition in tests.
"""

from typing import List, Optional, Tuple

from enums import Direction
from train import TrainPosition


def make_train_position(
    head_tile: Tuple[int, int],
    facing_direction: Direction,
    entry_edge: str = "W",
    head_distance_cm: float = 0.0,
    occupied_tiles: Optional[List[Tuple[int, int]]] = None,
    occupied_blocks: Optional[List[str]] = None,
) -> TrainPosition:
    """Create a minimal TrainPosition for tests."""
    tiles = occupied_tiles or [head_tile]
    blocks = occupied_blocks or []
    return TrainPosition(
        end_a_tile=head_tile,
        end_a_distance_cm=head_distance_cm,
        end_a_entry_edge=entry_edge,
        end_b_tile=head_tile,
        end_b_distance_cm=head_distance_cm,
        end_b_entry_edge=entry_edge,
        leading_end="a",
        facing_direction=facing_direction,
        occupied_tiles=tiles,
        occupied_blocks=blocks,
    )
