"""set_cab re-expresses the span frame whenever the HEAD END changes.

The cab is the head selector (`"B"` -> end_b, anything else -> end_a), and the
span's two end locators are expressed in the head-forward travel frame. So the
frame must flip exactly when the head end changes — which includes `None -> B`
and `B -> None`, not just `A <-> B`. A frame left un-flipped there put the head
marker pointing into the body on the layout and made physics advance the head
opposite to the direction the cab commands.
"""

from block import Block
from enums import Direction
from network import RailwayNetwork
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar


def _train_on_line():
    network = RailwayNetwork()
    for x in range(6):
        t = Tile(x=x, y=0, track_bits=TRACK_EW)
        t.segment_id = f"BLK_{x}"
        network.add_track(t)
        network.blocks[f"BLK_{x}"] = Block(
            block_id=f"BLK_{x}", length_cm=100.0, speed_limit_kmh=80.0
        )
    sim = SimulationController(network)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 2, 0, Direction.EAST
    )
    controller.commands.set_mode("manual")
    return controller


def _frame(span):
    return {
        "a": (span.end_a.entry_edge, span.end_a.distance_cm),
        "b": (span.end_b.entry_edge, span.end_b.distance_cm),
    }


def test_none_to_b_flips_the_frame():
    c = _train_on_line()
    span = c.physics_sync.current_span
    assert _frame(span) == {"a": ("W", 100.0), "b": ("W", 60.0)}

    assert c.commands.set_cab("B").success

    # Head is end_b now and the whole frame is west-forward: entry edges
    # flipped, distances re-measured from the other side of each tile.
    assert _frame(span) == {"a": ("E", 0.0), "b": ("E", 40.0)}
    assert c._facing_direction() == Direction.WEST


def test_none_to_a_is_a_no_op_on_the_frame():
    c = _train_on_line()
    span = c.physics_sync.current_span
    before = _frame(span)

    assert c.commands.set_cab("A").success

    assert _frame(span) == before
    assert c._facing_direction() == Direction.EAST


def test_b_to_none_flips_back():
    c = _train_on_line()
    span = c.physics_sync.current_span
    assert c.commands.set_cab("B").success
    assert c.commands.set_cab(None).success

    # Head defaults back to end_a, and the frame follows it back east-forward.
    assert _frame(span) == {"a": ("W", 100.0), "b": ("W", 60.0)}
    assert c._facing_direction() == Direction.EAST


def test_a_to_b_still_flips():
    c = _train_on_line()
    span = c.physics_sync.current_span
    assert c.commands.set_cab("A").success
    assert c.commands.set_cab("B").success

    assert _frame(span) == {"a": ("E", 0.0), "b": ("E", 40.0)}
    assert c._facing_direction() == Direction.WEST


def test_round_trip_restores_the_placed_frame():
    c = _train_on_line()
    span = c.physics_sync.current_span
    placed = _frame(span)
    for cab in ("B", "A", None, "B", None, "A"):
        assert c.commands.set_cab(cab).success
    assert _frame(span) == placed
    assert c._facing_direction() == Direction.EAST


def test_failed_reframe_changes_neither_cab_nor_span():
    """A malformed endpoint cannot leave the head selector half-switched."""
    c = _train_on_line()
    span = c.physics_sync.current_span
    before = _frame(span)
    span.end_b.entry_edge = "N"  # TRACK_EW has no traversal entering from N.

    result = c.commands.set_cab("B")

    assert result.success is False
    assert c.commands.cab is None
    assert _frame(span) == {**before, "b": ("N", before["b"][1])}
