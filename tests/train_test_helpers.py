"""
Helpers for setting TrainPosition in tests, plus a minimal synthetic path
dispatcher for unit tests that need a `received_path_plan != None` (the
controller's no-authority branch under Option B publishes an empty
no-authority lookahead whenever the dispatcher has no active path).
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from enums import Direction, LeadingEnd
from path import Path, PathStatus, PathTileEntry, ResourceSequenceEntry
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


# ---------------------------------------------------------------------------
# Synthetic path / dispatcher for unit tests
# ---------------------------------------------------------------------------


@dataclass
class _StubPathDispatcher:
    """Minimal stand-in for `PathDispatcher` exposing only the surface the
    controller reads through `received_path_plan` (which calls
    `get_active_path(train_id)`).

    Use `attach_synthetic_path(controller, ...)` to install one of these
    onto a controller for unit tests that need authority but don't want to
    set up a full dispatcher / route / pathfinding stack.
    """

    paths_by_train: dict = field(default_factory=dict)

    def get_active_path(self, train_id):
        path = self.paths_by_train.get(train_id)
        if path is None:
            return None
        return path if path.is_active() else None


def make_synthetic_path(
    *,
    train_id: str,
    block_id: str,
    block_tiles: List[Tuple[int, int]],
    entry_edge: str = "W",
    exit_edge: str = "E",
    path_id: Optional[str] = None,
) -> Path:
    """Build a one-block synthetic `Path` whose `resource_sequence` covers
    the listed tiles. Useful for tests that need `received_path_plan` to be
    non-None so the canonical lookahead walks a real path instead of
    publishing the no-authority empty payload.
    """
    path_tiles = [
        PathTileEntry(
            x=x,
            y=y,
            entry_edge=entry_edge if idx == 0 else "",
            exit_edge=exit_edge if idx == len(block_tiles) - 1 else "",
            resource_sequence_index=0,
        )
        for idx, (x, y) in enumerate(block_tiles)
    ]
    return Path(
        path_id=path_id or f"PATH_{train_id}_SYNTHETIC",
        status=PathStatus.ACTIVE,
        train_id=train_id,
        requested_destination_block_id=block_id,
        requested_destination_direction="forward",
        resource_sequence=[
            ResourceSequenceEntry(
                sequence_index=0,
                resource_type="block",
                resource_id=block_id,
                path_tiles=path_tiles,
                entry_edge=entry_edge,
                exit_edge=exit_edge,
            )
        ],
    )


def attach_synthetic_path(controller, path: Path) -> None:
    """Install a stub dispatcher carrying `path` onto `controller` so its
    `received_path_plan` property returns `path` on every read.
    """
    dispatcher = getattr(controller, "path_dispatcher", None)
    if not isinstance(dispatcher, _StubPathDispatcher):
        dispatcher = _StubPathDispatcher()
        controller.path_dispatcher = dispatcher
    dispatcher.paths_by_train[controller.train_id] = path
