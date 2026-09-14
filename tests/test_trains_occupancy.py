"""
Train occupancy tests - Block occupancy and validation

Run with: pytest tests/test_trains_occupancy.py -v
"""

import logging
import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, HandlePosition
from train import Train, TrainCar
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode
from switch import Switch
from tests.segment_test_helpers import make_segment_block_with_tracks
from entity_ids import segment_id_for_block


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


class TestTrainPlacementOccupancy:
    """Tests for train placement and block occupancy."""

    def test_train_placement_sets_block_occupied(self, network):
        track = Tile(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(Tile(x=1, y=0, track_bits=TRACK_EW))
        block = Block(block_id="BLK_001", name="Test Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0)]
        track.segment_id = segment_id_for_block("BLK_001")
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        assert block.state == BlockState.FREE
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_01"

    def test_train_placement_with_multiple_tiles(self, network):
        for x in range(3):
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
            track.segment_id = segment_id_for_block("BLK_001")
        block = Block(block_id="BLK_001", name="Long Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0), (1, 0), (2, 0)]
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_03", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_03"


class TestTrainPlacementValidation:
    """Tests for train placement with length validation."""

    def test_40cm_train_on_150cm_block_fits(self, network):
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)
        assert span is not None
        assert span.end_a.tile == (1, 0)  # head end at placement

    def test_80cm_train_spans_two_tiles(self, network):
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)
        assert span.end_a.tile == (1, 0)  # head end at placement
        assert span.end_b.tile == (0, 0)  # tail end
        assert len(span.occupied_tiles) == 2

    def test_train_too_long_raises_error(self, network):
        for x in range(2):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0)], length_cm=100.0)
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=120.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        with pytest.raises(ValueError, match="no track at"):
            network.place_train(train, 1, 0, Direction.EAST)


class TestTrainOccupiedTilesRich:
    """Tests for get_occupied_tiles_rich method."""

    def test_rich_tiles_single_tile_train(self, network):
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)
        rich = network.get_occupied_tiles_rich(span, head_is_a=True)
        assert len(rich) == 1
        assert rich[0]["tile"] == (1, 0)
        assert rich[0]["block_id"] == "BLOCK_1"
