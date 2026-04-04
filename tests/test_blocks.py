"""
Block Tests - Block state management, validation, and extensions

Run with: pytest tests/test_blocks.py -v
"""

import pytest

from track import (
    TrackSegment,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
    FORBIDDEN_TRACK_BITS,
)
from network import RailwayNetwork
from block import Block
from railway_signal import Signal, SignalType, SignalState
from enums import BlockState, PlatformSide, Direction, TrainState
from tests.train_test_helpers import make_train_position
from block_validation import (
    validate_block_path,
    is_crossing,
    validate_track_for_block,
    validate_block_assignment_continuity,
    get_adjacent_positions,
)
from signal_validation import (
    can_place_signal,
    get_valid_directions,
    is_valid_direction,
    validate_signal_placement,
    FORBIDDEN_SIGNAL_BITS,
    VALID_DETECTION_DIRECTIONS,
)


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


@pytest.fixture
def straight_ns_track(network):
    for y in range(5):
        track = TrackSegment(x=0, y=y, track_bits=TRACK_NS)
        network.add_track(track)
    return network


@pytest.fixture
def y_switch(network):
    network.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_NS))
    network.add_track(TrackSegment(x=1, y=1, track_bits=TRACK_NS | TRACK_NE))
    network.add_track(TrackSegment(x=1, y=2, track_bits=TRACK_NS))
    network.add_track(TrackSegment(x=2, y=0, track_bits=TRACK_SW))
    return network


@pytest.fixture
def diamond_crossing(network):
    network.add_track(TrackSegment(x=0, y=1, track_bits=TRACK_EW))
    network.add_track(TrackSegment(x=2, y=1, track_bits=TRACK_EW))
    network.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_NS))
    network.add_track(TrackSegment(x=1, y=2, track_bits=TRACK_NS))
    network.add_track(TrackSegment(x=1, y=1, track_bits=TRACK_NS | TRACK_EW))
    return network


class TestBlock:
    """Tests for Block class"""

    def test_state_transitions(self):
        block = Block(block_id="BLK_TEST")
        assert block.state == BlockState.FREE
        assert block.is_free() == True

        # Simulate reserve by setting state directly (reserve() removed as dead code)
        block.state = BlockState.RESERVED
        block.occupied_by = "TRAIN_01"
        assert block.state == BlockState.RESERVED
        assert block.is_reserved() == True

        block.occupy(train_id="TRAIN_01")
        assert block.state == BlockState.OCCUPIED

        block.set_to_caution()
        assert block.state == BlockState.CAUTION

        block.clear()
        assert block.state == BlockState.FREE

    def test_occupy_block(self):
        block = Block(block_id="BLK_TEST")
        block.state = BlockState.RESERVED
        block.occupied_by = "TRAIN_01"
        block.occupy(train_id="TRAIN_01")
        assert block.state == BlockState.OCCUPIED

    def test_release_block(self):
        block = Block(block_id="BLK_TEST")
        block.state = BlockState.RESERVED
        block.occupied_by = "TRAIN_01"
        block.occupy(train_id="TRAIN_01")
        block.release()
        assert block.state == BlockState.FREE

    def test_caution_state_cycle(self):
        block = Block(block_id="BLK_TEST")
        assert block.state == BlockState.FREE
        block.state = BlockState.RESERVED
        block.occupied_by = "TRAIN_01"
        assert block.state == BlockState.RESERVED
        block.occupy("TRAIN_01")
        assert block.state == BlockState.OCCUPIED
        block.set_to_caution()
        assert block.state == BlockState.CAUTION
        block.clear()
        assert block.state == BlockState.FREE

    def test_serialization(self):
        block = Block(
            block_id="BLK_TEST", name="Test Block", length_cm=1000, max_speed_kmh=80
        )
        block_dict = block.to_dict()
        assert block_dict["block_id"] == "BLK_TEST"
        assert block_dict["name"] == "Test Block"
        assert block_dict["length_cm"] == 1000
        assert block_dict["max_speed_kmh"] == 80
        restored = Block.from_dict(block_dict)
        assert restored.block_id == block.block_id
        assert restored.name == block.name


class TestDisabledBlock:
    """Tests for disabled block behavior."""

    def test_disabled_block_cannot_be_entered(self):
        block = Block(block_id="BLK_001", disabled=True)
        assert block.can_enter("TRAIN_01") == False

    def test_disabled_block_cannot_be_occupied(self):
        block = Block(block_id="BLK_001", disabled=True)
        result = block.occupy("TRAIN_01")
        assert result == False
        assert block.state == BlockState.FREE

    def test_disabled_block_is_not_free(self):
        block = Block(block_id="BLK_001", disabled=True)
        assert block.state == BlockState.FREE
        assert block.is_free() == False

    def test_enabled_block_works_normally(self):
        block = Block(block_id="BLK_001", disabled=False)
        assert block.is_free() == True
        assert block.can_enter("TRAIN_01") == True
        assert block.occupy("TRAIN_01") == True

    def test_disabled_block_serialization(self):
        block = Block(block_id="BLK_001", disabled=True)
        block_dict = block.to_dict()
        assert block_dict["disabled"] == True
        restored = Block.from_dict(block_dict)
        assert restored.disabled == True


class TestBlockExtension:
    """Tests for block extension functionality."""

    def test_extend_block_with_force_removes_signal_monitoring(self):
        network = RailwayNetwork()
        for i in range(4):
            network.add_track(TrackSegment(0, i, TRACK_NS))
        network.create_block("BLOCK_A")
        network.assign_track_to_block(0, 0, "BLOCK_A")
        network.assign_track_to_block(0, 1, "BLOCK_A")
        signal = network.add_signal(0, 2, "N")
        signal.monitored_block_id = "BLOCK_A"

        block = network.extend_block("BLOCK_A", [(0, 2)], force=True)
        positions = network.get_block_positions("BLOCK_A")
        assert (0, 2) in positions
        assert len(positions) == 3
        signal_after = network.signals.get(signal.signal_id)
        assert signal_after.monitored_block_id is None

    def test_blocks_can_exist_within_stations(self):
        network = RailwayNetwork()
        for x in range(4):
            network.add_track(TrackSegment(x, 0, TRACK_EW))
        network.add_station(0, 0, station_id="STA_TEST", name="Test Station")
        network.extend_station("STA_TEST", [(1, 0), (2, 0), (3, 0)])
        network.create_block("PLATFORM_1")
        network.extend_block("PLATFORM_1", [(0, 0), (1, 0)], force_overwrite=True)
        block = network.get_block("PLATFORM_1")
        assert block is not None
        assert len(network.get_block_positions("PLATFORM_1")) == 2


class TestBlockValidation:
    """Tests for block_validation.py functions"""

    def test_is_crossing(self):
        assert is_crossing(TRACK_NS | TRACK_EW) == True
        assert is_crossing(TRACK_NW | TRACK_SE) == True
        assert is_crossing(TRACK_NE | TRACK_SW) == True
        assert is_crossing(TRACK_NS) == False
        assert is_crossing(TRACK_NS | TRACK_NE) == False

    def test_validate_track_for_block_rejects_crossing(self, diamond_crossing):
        network = diamond_crossing
        track = network.get_track(1, 1)
        result = validate_track_for_block(track, network)
        assert result == False or "crossing" in str(result).lower()

    def test_validate_track_for_block_rejects_switch(self, y_switch):
        network = y_switch
        track = network.get_track(1, 1)
        result = validate_track_for_block(track, network)
        assert result == False or "switch" in str(result).lower()

    def test_get_adjacent_positions(self):
        adjacent = get_adjacent_positions(5, 5)
        assert len(adjacent) <= 8
        assert (5, 4) in adjacent
        assert (6, 5) in adjacent
        assert (5, 6) in adjacent
        assert (4, 5) in adjacent


class TestSignalValidation:
    """Tests for signal_validation.py functions"""

    def test_can_place_signal_on_simple_track(self):
        result = can_place_signal(TRACK_NS)
        assert result == True
        result = can_place_signal(TRACK_EW)
        assert result == True

    def test_cannot_place_signal_on_switch(self):
        switch_bits = TRACK_NS | TRACK_NE
        result = can_place_signal(switch_bits)
        assert result == False

    def test_cannot_place_signal_on_crossing(self):
        crossing_bits = TRACK_NS | TRACK_EW
        result = can_place_signal(crossing_bits)
        assert result == False

    def test_get_valid_directions(self):
        directions = get_valid_directions(TRACK_NS)
        assert "N" in directions
        assert "S" in directions
        assert len(directions) == 2
        directions = get_valid_directions(TRACK_EW)
        assert "E" in directions
        assert "W" in directions

    def test_forbidden_signal_bits(self):
        for forbidden_bits in FORBIDDEN_SIGNAL_BITS:
            result = can_place_signal(forbidden_bits)
            assert result == False


class TestMultiBlockOccupancy:
    """Tests for multi-block occupancy implementation."""

    def test_short_train_single_block(self):
        network = RailwayNetwork()
        track1 = TrackSegment(0, 0, track_bits=TRACK_EW, length=1.0)
        track1.block_section_id = "BLOCK_1"
        network.add_track(track1)
        block1 = Block(block_id="BLOCK_1", length_cm=1000.0)
        network.blocks["BLOCK_1"] = block1
        from train import Train, TrainCar
        from enums import Direction

        train = Train(
            train_id="T1",
            consist=[TrainCar("CAR1", "LOCOMOTIVE", 5.0)],
        )
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["BLOCK_1"]
        )
        occupied = train.get_occupied_blocks(network)
        assert len(occupied) == 1
        assert occupied[0] == "BLOCK_1"

    def test_long_train_two_blocks(self):
        network = RailwayNetwork()
        track1 = TrackSegment(0, 0, track_bits=TRACK_EW, length=1.0)
        track1.block_section_id = "BLOCK_1"
        network.add_track(track1)
        track2 = TrackSegment(1, 0, track_bits=TRACK_EW, length=1.0)
        track2.block_section_id = "BLOCK_2"
        network.add_track(track2)
        block1 = Block(block_id="BLOCK_1", length_cm=10000.0)
        block2 = Block(block_id="BLOCK_2", length_cm=10000.0)
        network.blocks["BLOCK_1"] = block1
        network.blocks["BLOCK_2"] = block2
        from train import Train, TrainCar
        from enums import Direction

        train = Train(
            train_id="T1",
            consist=[TrainCar("CAR1", "LOCOMOTIVE", 15.0)],
        )
        train.position = make_train_position(
            (1, 0),
            Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0)],
            occupied_blocks=["BLOCK_1", "BLOCK_2"],
        )
        occupied = train.get_occupied_blocks(network)
        assert len(occupied) == 2
        assert "BLOCK_1" in occupied
        assert "BLOCK_2" in occupied


class TestBlockManager:
    """Tests for BlockManager class."""

    def test_block_manager_initialization(self):
        from block_manager import BlockManager

        blocks = {
            "B1": Block("B1", "Block 1"),
            "B2": Block("B2", "Block 2"),
        }
        manager = BlockManager(blocks)
        assert manager.blocks is blocks
        assert manager.get_block_state("B1") == BlockState.FREE
        assert manager.get_block_state("B2") == BlockState.FREE

    def test_occupy_block_from_free(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        result = manager.occupy_block("B1", "TRAIN_1")
        assert result == True
        assert blocks["B1"].state == BlockState.OCCUPIED
        assert blocks["B1"].occupied_by == "TRAIN_1"

    def test_occupy_block_idempotent(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        manager.occupy_block("B1", "TRAIN_1")
        result = manager.occupy_block("B1", "TRAIN_1")
        assert result == True
        assert blocks["B1"].state == BlockState.OCCUPIED

    def test_set_block_to_caution(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        blocks["B1"].state = BlockState.OCCUPIED
        blocks["B1"].occupied_by = "TRAIN_1"
        result = manager.set_block_to_caution("B1")
        assert result == True
        assert blocks["B1"].state == BlockState.CAUTION

    def test_clear_block_from_caution(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        manager = BlockManager(blocks)
        blocks["B1"].state = BlockState.CAUTION
        blocks["B1"].occupied_by = "TRAIN_1"
        result = manager.clear_block("B1")
        assert result == True
        assert blocks["B1"].state == BlockState.FREE
        assert blocks["B1"].occupied_by is None

    def test_is_block_available(self):
        from block_manager import BlockManager

        blocks = {
            "B1": Block("B1"),
            "B2": Block("B2"),
        }
        manager = BlockManager(blocks)
        assert manager.is_block_available("B1") == True
        manager.occupy_block("B2", "TRAIN_1")
        assert manager.is_block_available("B2") == False
        assert manager.is_block_available("B2", "TRAIN_1") == True

    def test_block_manager_integration_with_network(self):
        network = RailwayNetwork()
        assert network.block_manager is None
        block = network.create_block("B1", "Block 1")
        assert network.block_manager is not None
        assert network.block_manager.get_block_state("B1") == BlockState.FREE
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_1"

    def test_occupy_block_rejects_wrong_train_on_reserved_block(self):
        from block_manager import BlockManager

        blocks = {"B1": Block("B1")}
        blocks["B1"].state = BlockState.RESERVED
        blocks["B1"].occupied_by = "TRAIN_A"
        manager = BlockManager(blocks)
        result = manager.occupy_block("B1", "TRAIN_B")
        assert result == False
        assert blocks["B1"].state == BlockState.RESERVED
        assert blocks["B1"].occupied_by == "TRAIN_A"


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_blocks.py -v")
