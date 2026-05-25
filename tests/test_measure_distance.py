"""Tests for `RailwayPathfinder.measure_distance` and the block-split
signal-remap behaviour that depends on it.
"""

from network import RailwayNetwork
from railway_pathfinder import RailwayPathfinder
from railway_signal import SignalType
from track import TrackSegment, TRACK_EW


def _seven_tile_ew_line() -> RailwayNetwork:
    """7 tiles connected EW at y=0, x=0..6."""
    network = RailwayNetwork()
    for x in range(7):
        network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))
    return network


# ---------------------------------------------------------- measure_distance

def test_measure_distance_tile_to_tile_returns_hops():
    network = _seven_tile_ew_line()
    pf = RailwayPathfinder(network)
    assert pf.measure_distance((0, 0), (3, 0)) == 3
    assert pf.measure_distance((6, 0), (0, 0)) == 6


def test_measure_distance_overlap_is_zero():
    network = _seven_tile_ew_line()
    pf = RailwayPathfinder(network)
    assert pf.measure_distance((2, 0), (2, 0)) == 0


def test_measure_distance_unreachable_is_none():
    # Two disconnected tiles.
    network = RailwayNetwork()
    network.add_track(TrackSegment(x=0, y=0, track_bits=TRACK_EW))
    network.add_track(TrackSegment(x=5, y=0, track_bits=TRACK_EW))
    pf = RailwayPathfinder(network)
    assert pf.measure_distance((0, 0), (5, 0)) is None


def test_measure_distance_resolves_block_id():
    network = _seven_tile_ew_line()
    network.create_block_with_tracks(
        block_id="BLK_LEFT", positions=[(0, 0), (1, 0), (2, 0)]
    )
    pf = RailwayPathfinder(network)
    # From tile (5,0) the closest block tile is (2,0) — 3 hops away.
    assert pf.measure_distance((5, 0), "BLK_LEFT") == 3
    # From inside the block — distance 0.
    assert pf.measure_distance((1, 0), "BLK_LEFT") == 0


def test_measure_distance_resolves_signal_id():
    network = _seven_tile_ew_line()
    signal = network.add_signal(
        x=1, y=0, detection_direction="E", auto_connect=False
    )
    pf = RailwayPathfinder(network)
    assert pf.measure_distance(signal.signal_id, (4, 0)) == 3


def test_measure_distance_rejects_unknown_string():
    network = _seven_tile_ew_line()
    pf = RailwayPathfinder(network)
    try:
        pf.measure_distance("NOPE", (0, 0))
    except ValueError as e:
        assert "NOPE" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown id")


# ---------------------------------------------------------- block-split remap

def test_block_split_reassigns_signals_to_closer_side():
    """Two signals on opposite ends of one block; remove a middle tile so
    the block splits; each signal should land on the closer new block."""
    network = _seven_tile_ew_line()
    network.create_block_with_tracks(
        block_id="BLK_TEST",
        positions=[(x, 0) for x in range(7)],
    )

    sig_left = network.add_signal(
        x=1, y=0, detection_direction="E", auto_connect=False, name="left"
    )
    sig_right = network.add_signal(
        x=5, y=0, detection_direction="W", auto_connect=False, name="right"
    )
    sig_left.monitored_block_id = "BLK_TEST"
    sig_right.monitored_block_id = "BLK_TEST"

    # Remove the middle tile — triggers split inside remove_tile_from_block.
    network.remove_track(3, 0)

    # Original block is gone.
    assert "BLK_TEST" not in network.blocks

    left_block = network.get_track(1, 0).block_section_id
    right_block = network.get_track(5, 0).block_section_id
    assert left_block is not None
    assert right_block is not None
    assert left_block != right_block

    assert sig_left.monitored_block_id == left_block
    assert sig_right.monitored_block_id == right_block


def test_block_split_picks_closest_when_both_reachable():
    """A signal on a tile that has BFS access to both new blocks should
    pick the one with smaller hop count, not just the first listed."""
    network = _seven_tile_ew_line()
    network.create_block_with_tracks(
        block_id="BLK_TEST",
        positions=[(x, 0) for x in range(7)],
    )
    # Signal at (5,0) — once we remove (3,0), it sits inside the right block.
    sig = network.add_signal(
        x=5, y=0, detection_direction="W", auto_connect=False
    )
    sig.monitored_block_id = "BLK_TEST"

    network.remove_track(3, 0)

    right_block = network.get_track(5, 0).block_section_id
    assert sig.monitored_block_id == right_block
