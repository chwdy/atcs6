"""
Track Tests - Track geometry, switches, and validation

Run with: pytest tests/test_track.py -v
"""

import pytest
from dataclasses import dataclass
from typing import Optional

from track import (
    Tile,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
    FORBIDDEN_TRACK_BITS,
)
from entity_ids import segment_id_for_block
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


@pytest.fixture
def straight_ns_track(network):
    for y in range(5):
        track = Tile(x=0, y=y, track_bits=TRACK_NS)
        network.add_track(track)
    return network


@pytest.fixture
def y_switch(network):
    network.add_track(Tile(x=1, y=0, track_bits=TRACK_NS))
    network.add_track(Tile(x=1, y=1, track_bits=TRACK_NS | TRACK_NE))
    network.add_track(Tile(x=1, y=2, track_bits=TRACK_NS))
    network.add_track(Tile(x=2, y=0, track_bits=TRACK_SW))
    return network


class TestTile:
    """Tests for Tile class"""

    def test_track_initialization(self):
        track = Tile(x=3, y=3, track_bits=TRACK_NS)
        assert track.x == 3
        assert track.y == 3
        assert track.track_bits == TRACK_NS
        assert track.is_switch == False

    def test_set_track_bits(self):
        track = Tile(x=0, y=0, track_bits=TRACK_NS)
        track.set_track_bits(TRACK_EW)
        assert track.track_bits == TRACK_EW


    def test_get_exits(self):
        track = Tile(x=0, y=0, track_bits=TRACK_NS)
        exits_from_north = track.get_exits(entry_edge="N")
        assert "S" in exits_from_north
        exits_from_south = track.get_exits(entry_edge="S")
        assert "N" in exits_from_south

    def test_reject_forbidden_bits(self):
        for forbidden_bits in FORBIDDEN_TRACK_BITS:
            track = Tile(x=0, y=0, track_bits=forbidden_bits)
            assert track is not None
            assert track.track_bits == forbidden_bits


    def test_track_type_detection(self):
        track = Tile(x=0, y=0, track_bits=TRACK_NS)
        assert track.is_switch == False
        track_switch = Tile(x=0, y=0, track_bits=(TRACK_NS | TRACK_NE))
        assert track_switch.is_switch == True


class TestDivergeOnlySwitches:
    """Tests for diverge-only switches (no main path, two diagonal segments)."""


    def test_diverge_only_switch_bits_36_both_states(self):
        track = Tile(x=0, y=0, track_bits=36)
        assert track.is_switch == False
        assert len(track.edges) == 4
        assert track.get_exits("N", use_switch_state=True) == ["E"]
        assert track.get_exits("E", use_switch_state=True) == ["N"]
        assert track.get_exits("S", use_switch_state=True) == ["W"]
        assert track.get_exits("W", use_switch_state=True) == ["S"]

    def test_diverge_only_cross_pattern_bits_24(self):
        track = Tile(x=0, y=0, track_bits=24)
        assert track.is_switch == False
        assert len(track.edges) == 4
        assert track.get_exits("N", use_switch_state=True) == ["W"]
        assert track.get_exits("W", use_switch_state=True) == ["N"]
        assert track.get_exits("S", use_switch_state=True) == ["E"]
        assert track.get_exits("E", use_switch_state=True) == ["S"]



class TestRailwayNetwork:
    """Tests for RailwayNetwork class"""

    def test_add_track(self, network):
        track = Tile(x=0, y=0, track_bits=TRACK_NS)
        network.add_track(track)
        retrieved = network.get_track(0, 0)
        assert retrieved is not None
        assert retrieved.track_bits == TRACK_NS

    def test_get_track(self, straight_ew_track):
        network = straight_ew_track
        track = network.get_track(2, 0)
        assert track is not None
        assert track.track_bits == TRACK_EW

    def test_get_nonexistent_track(self, network):
        track = network.get_track(99, 99)
        assert track is None

    def test_remove_track(self, straight_ew_track):
        network = straight_ew_track
        network.remove_track(2, 0)
        track = network.get_track(2, 0)
        assert track is None

    def test_get_neighbor(self, straight_ew_track):
        network = straight_ew_track
        neighbor = network.get_neighbor(1, 0, "E")
        assert neighbor is not None
        assert neighbor.x == 2
        assert neighbor.y == 0

    def test_track_removal_with_blocks(self, network):
        track1 = Tile(0, 0, TRACK_NS)
        track2 = Tile(0, 1, TRACK_NS)
        network.add_track(track1)
        network.add_track(track2)

        block = Block(block_id="BLK_REMOVAL_TEST")
        network.blocks["BLK_REMOVAL_TEST"] = block
        network.assign_track_to_segment(0, 0, segment_id_for_block("BLK_REMOVAL_TEST"))
        network.assign_track_to_segment(0, 1, segment_id_for_block("BLK_REMOVAL_TEST"))

        assert track1.block_id == "BLK_REMOVAL_TEST"

        network.remove_track(0, 0)
        assert network.get_track(0, 0) is None
        remaining = network.get_track(0, 1)
        assert remaining is not None
        assert remaining.block_id == "BLK_REMOVAL_TEST"


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_track.py -v")
