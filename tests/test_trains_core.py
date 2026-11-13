"""
Core train tests - SimulationController, basic placement, position

Run with: pytest tests/test_trains_core.py -v
"""

import logging
import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, HandlePosition, PathStatus
from train import Train, TrainCar
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode, RouteTask
from switch import Switch
from tests.segment_test_helpers import make_segment_block_with_tracks


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network

class TestSimulationController:
    """Tests for SimulationController class"""

    def test_simulation_creation(self, network):
        simulation = SimulationController(network)
        assert simulation is not None
        assert simulation.network == network
        assert simulation.power_controller is not None
        assert simulation.route_executor is not None
        assert simulation.speed_profile_registry is not None


class TestTrainPlacement:
    """Tests for proper train placement based on direction and length."""

    def test_place_train_head_at_exit_edge(self, network):
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 0, 0, Direction.EAST)
        assert span is not None
        # end_a is the head at placement; it sits at the exit edge of (0,0)
        # having entered from the west edge (facing EAST).
        assert span.end_a.tile == (0, 0)
        assert span.end_a.entry_edge == "W"
        tile_length_cm = network._get_tile_length_cm(0, 0)
        assert span.end_a.distance_cm == tile_length_cm

    def test_place_train_no_track_raises_error(self, network):
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        with pytest.raises(ValueError):
            network.place_train(train, 0, 0, Direction.EAST)



class TestTrainPropertyUpdate:
    """Tests for train property updates."""

    def test_train_name_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.name = "Original Name"
        network.add_train(train)
        train.name = "Updated Name"
        assert network.trains["TRAIN_01"].name == "Updated Name"

    def test_train_max_speed_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        train.max_speed = 120.0
        assert network.trains["TRAIN_01"].max_speed == 120.0

    def test_train_length_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        assert train.length_cm == 20.0
        train.consist.append(TrainCar(car_id="CAR_02", car_type="FREIGHT", length_cm=30.0))
        assert train.length_cm == 50.0
