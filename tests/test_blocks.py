"""
Block Tests - Block state management, validation, and extensions

Run with: pytest tests/test_blocks.py -v
"""

import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW
from network import RailwayNetwork
from block import Block
from segment_validation import (
    is_crossing,
    crossing_disallowed_in_segment,
    validate_track_for_segment,
    get_adjacent_positions,
)
from entity_ids import segment_id_for_block
from tests.segment_test_helpers import make_segment_block


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


@pytest.fixture
def diamond_crossing(network):
    network.add_track(Tile(x=0, y=1, track_bits=TRACK_EW))
    network.add_track(Tile(x=2, y=1, track_bits=TRACK_EW))
    network.add_track(Tile(x=1, y=0, track_bits=TRACK_NS))
    network.add_track(Tile(x=1, y=2, track_bits=TRACK_NS))
    network.add_track(Tile(x=1, y=1, track_bits=TRACK_NS | TRACK_EW))
    return network


class TestBlock:
    """Tests for Block class"""


    def test_serialization(self):
        block = Block(
            block_id="BLK_TEST", name="Test Block", length_cm=1000, speed_limit_kmh=80
        )
        block_dict = block.to_dict()
        assert block_dict["block_id"] == "BLK_TEST"
        assert block_dict["name"] == "Test Block"
        assert block_dict["length_cm"] == 1000
        assert block_dict["speed_limit_kmh"] == 80
        restored = Block.from_dict(block_dict)
        assert restored.block_id == block.block_id
        assert restored.name == block.name

    def test_load_drops_a_name_copied_onto_a_one_segment_block(self):
        """A save from before one-name-per-record loads with the copy gone.

        The block and its only member held one string; the segment keeps it and
        the block loads unnamed (design_new.md **Block grouping**, one name per
        record).
        """
        block = Block(block_id="BLK_LEFT", length_cm=100)
        block.segments[0].name = "Left"
        stored = block.to_layout_dict()
        stored["name"] = "Left"  # what a pre-rule save carried

        restored = Block.from_dict(stored)
        assert restored.name == ""
        assert restored.segments[0].name == "Left"

    def test_load_keeps_a_block_name_of_its_own(self):
        """Only an exact copy is dropped; a distinct block name survives."""
        block = Block(block_id="BLK_YARD", length_cm=100)
        block.segments[0].name = "Left"
        stored = block.to_layout_dict()
        stored["name"] = "Yard Feed"

        restored = Block.from_dict(stored)
        assert restored.name == "Yard Feed"
        assert restored.segments[0].name == "Left"


class TestDisabledBlock:
    """Tests for disabled block behavior."""



    def test_disabled_block_serialization(self):
        block = Block(block_id="BLK_001", disabled=True)
        block_dict = block.to_dict()
        assert block_dict["disabled"] == True
        restored = Block.from_dict(block_dict)
        assert restored.disabled == True



class TestBlockValidation:
    """Tests for segment_validation.py functions"""

    def test_is_crossing(self):
        assert is_crossing(TRACK_NS | TRACK_EW) == True
        assert is_crossing(TRACK_NW | TRACK_SE) == True
        assert is_crossing(TRACK_NE | TRACK_SW) == True
        assert is_crossing(TRACK_NS) == False
        assert is_crossing(TRACK_NS | TRACK_NE) == False

    def test_crossing_disallowed_in_segment(self):
        # The orthogonal NS+EW crossing (3) is allowed inside a block.
        assert crossing_disallowed_in_segment(TRACK_NS | TRACK_EW) == False
        # Diagonal crossings remain disallowed.
        assert crossing_disallowed_in_segment(TRACK_NW | TRACK_SE) == True
        assert crossing_disallowed_in_segment(TRACK_NE | TRACK_SW) == True
        # Non-crossings are unaffected.
        assert crossing_disallowed_in_segment(TRACK_NS) == False

    def test_validate_track_for_segment_allows_orthogonal_crossing(self, diamond_crossing):
        # The orthogonal NS+EW crossing (track_bits 3) may belong to a block.
        network = diamond_crossing
        track = network.get_track(1, 1)
        is_valid, _ = validate_track_for_segment(track, network)
        assert is_valid == True

    def test_validate_track_for_segment_rejects_diagonal_crossing(self, network):
        # Diagonal crossings (NW+SE, NE+SW) stay out of blocks.
        network.add_track(Tile(x=0, y=0, track_bits=TRACK_NW | TRACK_SE))
        track = network.get_track(0, 0)
        is_valid, msg = validate_track_for_segment(track, network)
        assert is_valid == False
        assert "crossing" in msg.lower()

    def test_validate_track_for_segment_rejects_switch(self, y_switch):
        network = y_switch
        track = network.get_track(1, 1)
        result = validate_track_for_segment(track, network)
        assert result == False or "switch" in str(result).lower()

    def test_get_adjacent_positions(self):
        adjacent = get_adjacent_positions(5, 5)
        assert len(adjacent) <= 8
        assert (5, 4) in adjacent
        assert (6, 5) in adjacent
        assert (5, 6) in adjacent
        assert (4, 5) in adjacent



if __name__ == "__main__":
    print("Run tests with: pytest tests/test_blocks.py -v")
