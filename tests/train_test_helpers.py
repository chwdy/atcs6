"""
Helpers for setting TrainPosition in tests, plus a minimal synthetic path
dispatcher for unit tests that need a `received_path_plan != None` (the
controller's no-authority branch under Option B publishes an empty
no-authority lookahead whenever the dispatcher has no active path).
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from enums import Direction
from path import Path, PathStatus, PathTileEntry, ResourceSequenceEntry
from train_span import SpanEndLocator, TrainSpan


def make_train_span(
    head_tile: Tuple[int, int],
    facing_direction: Optional[Direction] = None,
    entry_edge: str = "W",
    head_distance_cm: float = 0.0,
    occupied_tiles: Optional[List[Tuple[int, int]]] = None,
    occupied_blocks: Optional[List[str]] = None,
) -> TrainSpan:
    """Create a minimal single-tile `TrainSpan` for tests (end_a == end_b).

    The v1 replacement for `make_train_position`: the canonical live span lives
    on `TrainController.physics_sync.current_span`, not on `Train`. `end_a` and
    `end_b` are cab-independent physical ends; the cab (set on the controller)
    selects which is the head. When `facing_direction` is given, `entry_edge`
    is derived as the edge the train entered from (opposite of facing), so a
    cab-"A" controller reads this span's `travel_direction` as `facing_direction`.

    Args mirror `make_train_position`. Seed it with, e.g.:
        controller.physics_sync.current_span = make_train_span((1, 0), Direction.EAST)
    or via the `set_train_span(controller, ...)` convenience below.
    """
    tiles = occupied_tiles or [head_tile]
    blocks = occupied_blocks or []
    if facing_direction:
        direction_to_entry = {
            Direction.NORTH: "S",
            Direction.SOUTH: "N",
            Direction.EAST: "W",
            Direction.WEST: "E",
        }
        entry_edge = direction_to_entry.get(facing_direction, entry_edge)
    return TrainSpan(
        end_a=SpanEndLocator(
            tile=head_tile, distance_cm=head_distance_cm, entry_edge=entry_edge
        ),
        end_b=SpanEndLocator(
            tile=head_tile, distance_cm=head_distance_cm, entry_edge=entry_edge
        ),
        occupied_tiles=list(tiles),
        occupied_blocks=list(blocks),
    )


def set_train_span(controller, head_tile, **kwargs) -> TrainSpan:
    """Seed `controller.physics_sync.current_span` with a single-tile span built
    by `make_train_span(head_tile, **kwargs)`; returns the span.

    Convenience for tests migrating off `train.position = make_train_position(...)`.
    The controller's cab (default "A" -> end_a is head) selects the head end.
    """
    span = make_train_span(head_tile, **kwargs)
    controller.physics_sync.current_span = span
    return span


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


def skip_calibration_trials(run):
    """Put a freshly opened speed pass straight onto its sweep, as a successful
    It moves leaves it (design_new.md "Speed calibration", The pass starts with
    two trial stages), for tests that measure the sweep itself. The starting
    voltage stays the one the pass opened with."""
    from speed_calibration import PASS_SWEEP

    run.pass_stage = PASS_SWEEP
    run.trial_voltage = 0.0
    run.current_step_voltage = run.max_voltage
    return run
