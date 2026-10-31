"""`RailwayNetwork.project_reservation_boundary_delta(...)` (design_new.md
"Boundary-projection helper").

The helper measures along the live reservation's directed walk, forward from
the head to the reservation end's last tile, and subtracts the safety margin.
It is never negative.
"""

from enums import Direction
from network import RailwayNetwork
from path_utils import DirectedBlockFrame, GraphLocator, GraphWalkStep, ReservationEnd


def _walk(tiles, length_cm=10.0):
    return [
        GraphWalkStep(
            step_index=i,
            resource_type="block",
            resource_id="B_END" if i >= len(tiles) - 2 else "B_HEAD",
            tile=tile,
            entry_edge="W",
            exit_edge="E",
            length_cm=length_cm,
        )
        for i, tile in enumerate(tiles)
    ]


def _end(tiles):
    return ReservationEnd(
        block_id="B_END",
        terminal_frame=DirectedBlockFrame(
            block_id="B_END",
            block_direction="forward",
            path_tiles=[(x, y, Direction.EAST) for x, y in tiles],
            entry_edge="W",
            exit_edge="E",
        ),
    )


def test_distance_is_measured_along_the_walk_minus_the_margin():
    tiles = [(0, 0), (1, 0), (2, 0), (3, 0)]
    head = GraphLocator(tile=(0, 0), edge="W", distance_cm=4.0)
    delta = RailwayNetwork().project_reservation_boundary_delta(
        ordered_walk=_walk(tiles),
        head_locator=head,
        reservation_end=_end(tiles[2:]),
        safety_margin_cm=5.0,
    )
    # Four 10 cm tiles, 4 cm of the first already behind the head, 5 cm margin.
    assert delta == 40.0 - 4.0 - 5.0


def test_head_inside_the_margin_returns_zero():
    tiles = [(0, 0), (1, 0)]
    head = GraphLocator(tile=(0, 0), edge="W", distance_cm=8.0)
    delta = RailwayNetwork().project_reservation_boundary_delta(
        ordered_walk=_walk(tiles),
        head_locator=head,
        reservation_end=_end(tiles),
        safety_margin_cm=25.0,
    )
    assert delta == 0.0
