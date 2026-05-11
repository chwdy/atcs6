"""
Tests for Phase 6 - Multi-Block Occupancy & IDLE Polarity

This module tests:
- Train.get_occupied_blocks() based on length
- Train.is_opposite_polarity_safe() checks
- BlockPowerController opposite polarity voltage output
"""

import pytest
from train import Train, TrainCar
from tests.train_test_helpers import make_train_position
from network import RailwayNetwork
from block import Block
from track import TrackSegment
from block_power import BlockPowerController, SimulatedOutputInterface
from speed_profile import SpeedProfile
from enums import Direction, TrainState


class TestMultiBlockOccupancy:
    """Tests for multi-block occupancy based on train length."""

    @pytest.fixture
    def simple_network(self):
        """Create a simple network with multiple blocks."""
        network = RailwayNetwork()

        # Create 3 blocks
        block_a = Block(block_id="BLK_A", length_cm=100.0)
        block_b = Block(block_id="BLK_B", length_cm=100.0)
        block_c = Block(block_id="BLK_C", length_cm=100.0)

        network.blocks["BLK_A"] = block_a
        network.blocks["BLK_B"] = block_b
        network.blocks["BLK_C"] = block_c

        # Create tracks for each block
        track_a = TrackSegment(x=0, y=0)
        track_a.block_section_id = "BLK_A"
        track_a.length = 1.0  # 1 meter

        track_b = TrackSegment(x=1, y=0)
        track_b.block_section_id = "BLK_B"
        track_b.length = 1.0

        track_c = TrackSegment(x=2, y=0)
        track_c.block_section_id = "BLK_C"
        track_c.length = 1.0

        network.tracks[(0, 0)] = track_a
        network.tracks[(1, 0)] = track_b
        network.tracks[(2, 0)] = track_c

        return network

    @pytest.fixture
    def short_train(self):
        """Create a short train (fits in one block)."""
        train = Train(train_id="T_SHORT")
        # Add a single short car (0.5 meters = 50cm)
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=50.0)
        train.consist.append(car)
        return train

    @pytest.fixture
    def long_train(self):
        """Create a long train (spans multiple blocks)."""
        train = Train(train_id="T_LONG")
        # Add 3 cars, total 250cm = 2.5 meters
        train.consist.append(
            TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=100.0)
        )
        train.consist.append(
            TrainCar(car_id="CAR_2", car_type="PASSENGER", length_cm=100.0)
        )
        train.consist.append(
            TrainCar(car_id="CAR_3", car_type="PASSENGER", length_cm=50.0)
        )
        return train

    def test_short_train_single_block(self, simple_network, short_train):
        """Test short train occupies only one block."""
        short_train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["BLK_A"]
        )

        occupied = short_train.get_occupied_blocks(simple_network)

        assert occupied == ["BLK_A"]

    def test_long_train_multiple_blocks(self, simple_network, long_train):
        """Test long train occupies multiple blocks."""
        long_train.position = make_train_position(
            (1, 0),
            Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
            occupied_blocks=["BLK_A", "BLK_B", "BLK_C"],
        )

        # Long train is 2.5 meters
        occupied = long_train.get_occupied_blocks(simple_network)

        assert occupied == ["BLK_A", "BLK_B", "BLK_C"]

    def test_get_occupied_blocks_no_position(self, simple_network, short_train):
        """Test get_occupied_blocks with no current position."""
        short_train.position = None

        occupied = short_train.get_occupied_blocks(simple_network)

        assert occupied == []

    def test_get_occupied_blocks_no_network(self, short_train):
        """Test get_occupied_blocks with no network."""
        short_train.position = None

        occupied = short_train.get_occupied_blocks(None)

        assert occupied == []




