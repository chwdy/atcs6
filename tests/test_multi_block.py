"""
Tests for Phase 6 - Multi-Block Occupancy

This module tests:
- Train occupancy spans across one or more blocks based on length
"""

import pytest
from train import Train, TrainCar
from train_controller import create_controller_for_train
from tests.train_test_helpers import set_train_span
from network import RailwayNetwork
from block import Block
from track import Tile
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
        track_a = Tile(x=0, y=0)
        track_a.segment_id = "BLK_A"
        track_a.length = 1.0  # 1 meter

        track_b = Tile(x=1, y=0)
        track_b.segment_id = "BLK_B"
        track_b.length = 1.0

        track_c = Tile(x=2, y=0)
        track_c.segment_id = "BLK_C"
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
        simple_network.add_train(short_train)
        controller = create_controller_for_train(short_train.train_id, simple_network)
        set_train_span(
            controller, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLK_A"]
        )

        # Occupied blocks now live on the controller's live span.
        occupied = controller.get_current_span().occupied_blocks

        assert occupied == ["BLK_A"]

    def test_long_train_multiple_blocks(self, simple_network, long_train):
        """Test long train occupies multiple blocks."""
        simple_network.add_train(long_train)
        controller = create_controller_for_train(long_train.train_id, simple_network)
        set_train_span(
            controller,
            (1, 0),
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
            occupied_blocks=["BLK_A", "BLK_B", "BLK_C"],
        )

        # Long train is 2.5 meters; occupied blocks live on the live span.
        occupied = controller.get_current_span().occupied_blocks

        assert occupied == ["BLK_A", "BLK_B", "BLK_C"]

    def test_get_occupied_blocks_no_span(self, simple_network, short_train):
        """A controller with no live span reports no occupancy.

        Replaces the legacy "no position" case: when the controller has not
        received a physics span yet, `get_current_span()` is None and there
        are no occupied blocks to read.
        """
        simple_network.add_train(short_train)
        controller = create_controller_for_train(short_train.train_id, simple_network)

        assert controller.get_current_span() is None

    def test_get_occupied_blocks_empty_span(self, simple_network, short_train):
        """A span carrying no occupied blocks reports an empty list.

        Replaces the legacy "no network" case: occupancy now lives directly
        on the span (not derived from a network), so a span with no
        `occupied_blocks` yields `[]` independent of any network.
        """
        simple_network.add_train(short_train)
        controller = create_controller_for_train(short_train.train_id, simple_network)
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)

        occupied = controller.get_current_span().occupied_blocks

        assert occupied == []




