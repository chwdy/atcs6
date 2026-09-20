"""A block voltage carries a direction — design_new.md **Power**.

`track_voltage` is a magnitude and says nothing about which way a locomotive
runs; a DC locomotive's direction is the polarity on its rails. So the value
`BlockPowerController` is commanded is that magnitude given a sign: positive
when the train runs the block in its `forward` direction, negative when it runs
it in `reverse`, over `Block.ordered_tiles`.

The sign is per block, not per train. Each block's tile order is drawn on its
own, so two blocks a train holds at the same moment may run opposite ways
against its line of travel — the case these tests build with
`reverse_segment_orientation(...)`. One sign for the whole train would drive the
locomotive backwards on every block whose order was drawn against it.
"""

from types import SimpleNamespace

import pytest

from block_power import BlockPowerController, SimulatedTrackDriver
from network import RailwayNetwork
from track import Tile
from train_physics import TrainPhysics, _SpanWorkState


# --------------------------------------------------------------- geometry


def _line_network(flip_b: bool = False) -> RailwayNetwork:
    """A straight EW run of two blocks: `BLK_A` at x0-1, `BLK_B` at x2-3.

    With `flip_b`, block B's stored tile order is reversed, so a train running
    west to east crosses A in its `forward` direction and B in its `reverse`
    one. That is the whole point: the two orders are drawn independently and
    nothing makes them agree.
    """
    net = RailwayNetwork()
    for x in range(4):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight, all joined
    net.create_segment_with_tracks("A", [(0, 0), (1, 0)], name="A")
    net.create_segment_with_tracks("B", [(2, 0), (3, 0)], name="B")
    if flip_b:
        net._get_segment_geometry_manager().reverse_segment_orientation("B")
    return net


WEST_TO_EAST = [(0, 0), (1, 0), (2, 0), (3, 0)]
EAST_TO_WEST = list(reversed(WEST_TO_EAST))


# ------------------------------------------- Block.block_direction_for_tile_walk


def test_a_walk_up_the_block_order_is_forward():
    net = _line_network()
    assert net.blocks["BLK_A"].block_direction_for_tile_walk(WEST_TO_EAST) == "forward"


def test_a_walk_down_the_block_order_is_reverse():
    net = _line_network()
    assert net.blocks["BLK_A"].block_direction_for_tile_walk(EAST_TO_WEST) == "reverse"


def test_one_walk_tile_does_not_fix_a_direction():
    """One tile is not enough to say which way the walk is going, so the caller
    is told nothing rather than guessed at."""
    net = _line_network()
    assert net.blocks["BLK_A"].block_direction_for_tile_walk([(0, 0)]) is None


def test_tiles_outside_the_block_are_skipped():
    """A caller may hand over a whole train's line of travel; each block reads
    only its own tiles out of it."""
    net = _line_network(flip_b=True)
    assert net.blocks["BLK_A"].block_direction_for_tile_walk(WEST_TO_EAST) == "forward"
    assert net.blocks["BLK_B"].block_direction_for_tile_walk(WEST_TO_EAST) == "reverse"


# ------------------------------------------------------------- emission sign


def _physics(net: RailwayNetwork, controller=None) -> TrainPhysics:
    """A physics instance wired to a real power controller.

    `_energized_blocks_for_train` is pinned to the two blocks so the test reads
    the sign the emission puts on each, not the district-expansion rules that
    choose the set (those are **Energized set: calculated span plus
    reservations**, covered elsewhere).
    """
    physics = TrainPhysics(train_id="T1", network=net, controller=controller)
    power = BlockPowerController(
        network=net, output_interface=SimulatedTrackDriver()
    )
    physics._get_power_controller = lambda: power  # type: ignore[method-assign]
    physics._energized_blocks_for_train = lambda train: {  # type: ignore[method-assign]
        "BLK_A",
        "BLK_B",
    }
    return physics


def _span_work(tiles, blocks, head_is_a: bool) -> _SpanWorkState:
    """A movement scratchpad whose span covers `tiles`, ordered end_a to end_b."""
    return _SpanWorkState(
        head_is_a=head_is_a,
        end_a_tile=tiles[0],
        end_a_distance_cm=0.0,
        end_a_entry_edge="W",
        end_b_tile=tiles[-1],
        end_b_distance_cm=0.0,
        end_b_entry_edge="E",
        occupied_tiles=list(tiles),
        occupied_blocks=list(blocks),
    )


def _path_plan(*entries) -> SimpleNamespace:
    """A controller whose active authority carries these resource entries."""
    plan = SimpleNamespace(resource_sequence=list(entries))
    return SimpleNamespace(
        path_projection=SimpleNamespace(received_path_plan=plan)
    )


def _block_entry(block_id: str, tiles) -> SimpleNamespace:
    return SimpleNamespace(
        resource_type="block",
        resource_id=block_id,
        path_tiles=[SimpleNamespace(x=x, y=y) for (x, y) in tiles],
    )


def test_two_blocks_drawn_opposite_ways_get_opposite_signs():
    """The headline case. The train runs one way geographically, but the two
    blocks it holds were drawn in opposite senses, so one is driven positive and
    the other negative — at the same magnitude."""
    net = _line_network(flip_b=True)
    controller = _path_plan(
        _block_entry("BLK_A", [(0, 0), (1, 0)]),
        _block_entry("BLK_B", [(2, 0), (3, 0)]),
    )
    physics = _physics(net, controller)

    physics._set_voltage_for_blocks(SimpleNamespace(train_id="T1"), 9.0)

    power = physics._get_power_controller()
    assert power.get_block_voltage("BLK_A") == 9.0
    assert power.get_block_voltage("BLK_B") == -9.0


def test_blocks_drawn_the_same_way_share_one_sign():
    net = _line_network()
    controller = _path_plan(
        _block_entry("BLK_A", [(0, 0), (1, 0)]),
        _block_entry("BLK_B", [(2, 0), (3, 0)]),
    )
    physics = _physics(net, controller)

    physics._set_voltage_for_blocks(SimpleNamespace(train_id="T1"), 9.0)

    power = physics._get_power_controller()
    assert power.get_block_voltage("BLK_A") == 9.0
    assert power.get_block_voltage("BLK_B") == 9.0


def test_a_train_running_the_other_way_gets_the_other_sign():
    net = _line_network()
    controller = _path_plan(
        _block_entry("BLK_B", [(3, 0), (2, 0)]),
        _block_entry("BLK_A", [(1, 0), (0, 0)]),
    )
    physics = _physics(net, controller)

    physics._set_voltage_for_blocks(SimpleNamespace(train_id="T1"), 9.0)

    power = physics._get_power_controller()
    assert power.get_block_voltage("BLK_A") == -9.0
    assert power.get_block_voltage("BLK_B") == -9.0


def test_a_block_no_source_can_fix_is_driven_positive():
    """Nothing names a direction for these blocks, so the emission does not
    invent one — it drives them positive."""
    net = _line_network(flip_b=True)
    physics = _physics(net, controller=None)

    physics._set_voltage_for_blocks(SimpleNamespace(train_id="T1"), 9.0)

    power = physics._get_power_controller()
    assert power.get_block_voltage("BLK_A") == 9.0
    assert power.get_block_voltage("BLK_B") == 9.0


def test_a_block_past_the_span_takes_the_line_of_travel():
    """A block the detector credits to a train just past its calculated span is
    in neither the path nor the span walk. It is signed by the train's line of
    travel continued into it, so the locomotive keeps going the way it was
    going. Driving it positive by default would, on a block drawn against the
    travel as `BLK_B` is here, push the locomotive backwards."""
    net = _line_network(flip_b=True)
    physics = _physics(net, controller=None)
    # The span covers BLK_A only; the cab is at end_b (1, 0), which the head
    # entered through its west edge, so the train travels west to east and
    # BLK_B is the next block along.
    physics._work = _SpanWorkState(
        head_is_a=False,
        end_a_tile=(0, 0),
        end_a_distance_cm=0.0,
        end_a_entry_edge="E",
        end_b_tile=(1, 0),
        end_b_distance_cm=0.0,
        end_b_entry_edge="W",
        occupied_tiles=[(0, 0), (1, 0)],
        occupied_blocks=["BLK_A"],
    )

    physics._set_voltage_for_blocks(SimpleNamespace(train_id="T1"), 6.0)

    power = physics._get_power_controller()
    assert power.get_block_voltage("BLK_A") == 6.0
    assert power.get_block_voltage("BLK_B") == -6.0


# --------------------------------------------------- the span-walk fallback


def test_the_span_walk_answers_when_no_path_is_assigned():
    """A train driving manually has no `resource_sequence`, so its directed span
    walk — tail end to cab end — is what signs its blocks."""
    net = _line_network(flip_b=True)
    physics = _physics(net, controller=None)
    # Span covers both blocks, ordered end_a -> end_b west to east, cab at end_b:
    # the line of travel runs west to east.
    physics._work = _span_work(WEST_TO_EAST, ["BLK_A", "BLK_B"], head_is_a=False)

    physics._set_voltage_for_blocks(SimpleNamespace(train_id="T1"), 6.0)

    power = physics._get_power_controller()
    assert power.get_block_voltage("BLK_A") == 6.0
    assert power.get_block_voltage("BLK_B") == -6.0


def test_a_cab_change_reverses_every_sign():
    """Selecting the other cab turns the line of travel around, so the next
    emission re-signs every block the train holds. This is how a train
    reverses."""
    net = _line_network(flip_b=True)
    physics = _physics(net, controller=None)
    train = SimpleNamespace(train_id="T1")
    power = physics._get_power_controller()

    physics._work = _span_work(WEST_TO_EAST, ["BLK_A", "BLK_B"], head_is_a=False)
    physics._set_voltage_for_blocks(train, 6.0)
    assert (power.get_block_voltage("BLK_A"), power.get_block_voltage("BLK_B")) == (
        6.0,
        -6.0,
    )

    # Same span, other cab: the walk is the same tiles read the other way.
    physics._work = _span_work(WEST_TO_EAST, ["BLK_A", "BLK_B"], head_is_a=True)
    physics._set_voltage_for_blocks(train, 6.0)
    assert (power.get_block_voltage("BLK_A"), power.get_block_voltage("BLK_B")) == (
        -6.0,
        6.0,
    )


def test_the_path_wins_over_the_span_walk():
    """The active path runs from the train's tail to its authority end, so it
    covers blocks the span has not reached; where both answer, the path is the
    line of travel and the span is the fallback."""
    net = _line_network(flip_b=True)
    controller = _path_plan(
        _block_entry("BLK_A", [(0, 0), (1, 0)]),
        _block_entry("BLK_B", [(2, 0), (3, 0)]),
    )
    physics = _physics(net, controller)
    # A span walking the opposite way would flip both signs if it were consulted.
    physics._work = _span_work(EAST_TO_WEST, ["BLK_A", "BLK_B"], head_is_a=False)

    physics._set_voltage_for_blocks(SimpleNamespace(train_id="T1"), 9.0)

    power = physics._get_power_controller()
    assert power.get_block_voltage("BLK_A") == 9.0
    assert power.get_block_voltage("BLK_B") == -9.0


# ------------------------------------------------------------ the clamp range


@pytest.mark.parametrize("commanded, landed", [(-15.0, -12.0), (15.0, 12.0)])
def test_the_commandable_range_is_symmetric(commanded, landed):
    """A signed command needs both ends of the range; a floor at 0 would swallow
    every reverse traversal."""
    power = BlockPowerController(output_interface=SimulatedTrackDriver())
    power.set_block_voltage("BLK_A", commanded)
    assert power.get_block_voltage("BLK_A") == landed
