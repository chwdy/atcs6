"""
Core train tests - SimulationController, basic placement, position

Run with: pytest tests/test_trains_core.py -v
"""

import logging
import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, LeadingEnd, TrainState, HandlePosition
from train import Train, TrainCar, TrainPosition
from tests.train_test_helpers import make_train_position
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode, Waypoint
from switch import Switch


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
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

    def test_simulation_update_without_crash(self, network):
        """Test that simulation.update() can run without crashing (regression test)."""
        simulation = SimulationController(network)

        # Should not crash even with no trains
        simulation.update(0.1)

        # Add a train and update again
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)

        # Should not crash with train present
        simulation.update(0.1)
        simulation.update(0.1)

    def test_simulation_handles_path_completion_events(self, network):
        """Test that simulation correctly handles path completion events from PathDispatcher.

        This is a regression test for the bug where simulation.update() crashed with:
        TypeError: RouteExecutor.handle_path_completion() got an unexpected keyword argument 'event_time'

        The fix changed event_time to _event_time to match the method signature.
        """
        from path_dispatcher import PathDispatcher
        from datetime import datetime

        simulation = SimulationController(network)
        simulation.path_dispatcher = PathDispatcher(network)

        # Simulate scenario with path completion event
        # Even if event data is incomplete, it should not crash with TypeError
        completion_event = {
            "train_id": "NONEXISTENT",
            "path_id": "PATH_001",
            "end_tile": (0, 0),
            "time": datetime.now(),
        }

        # Directly append event to test the code path
        if hasattr(simulation.path_dispatcher, "_completion_events"):
            simulation.path_dispatcher._completion_events.append(completion_event)

        # This should not crash with "unexpected keyword argument 'event_time'"
        try:
            simulation.update(0.1)
            passed = True
        except TypeError as e:
            if "event_time" in str(e) and "unexpected keyword argument" in str(e):
                passed = False
            else:
                # Some other TypeError - re-raise
                raise

        assert passed, (
            "Simulation crashed with event_time parameter mismatch (regression detected)"
        )



class TestTrainPlacement:
    """Tests for proper train placement based on direction and length."""

    def test_place_train_head_at_exit_edge(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        final_pos = network.place_train(train, 0, 0, Direction.EAST)
        assert train.position is not None
        assert train.position.head_tile == (0, 0)
        assert train.position.facing_direction == Direction.EAST
        tile_length_cm = network._get_tile_length_cm(0, 0)
        assert train.position.head_distance_cm == tile_length_cm

    def test_place_train_no_track_raises_error(self, network):
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=30.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        with pytest.raises(ValueError):
            network.place_train(train, 0, 0, Direction.EAST)



class TestTrainPosition:
    """Tests for TrainPosition dataclass and basic functionality."""

    def test_train_position_creation(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end=LeadingEnd.A,
            occupied_tiles=[(0, 0), (1, 0)],
            occupied_blocks=["BLOCK_1"],
        )
        assert pos.end_a_tile == (1, 0)
        assert pos.end_b_tile == (0, 0)
        assert pos.leading_end == LeadingEnd.A

    def test_train_position_head_tail_properties(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end=LeadingEnd.A,
        )
        assert pos.head_tile == (1, 0)
        assert pos.head_distance_cm == 50.0
        assert pos.tail_tile == (0, 0)
        assert pos.tail_distance_cm == 10.0

    def test_train_position_reverse(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end=LeadingEnd.A,
        )
        pos.reverse()
        assert pos.leading_end == LeadingEnd.B
        assert pos.facing_direction == Direction.WEST
        assert pos.head_tile == (0, 0)
        assert pos.tail_tile == (1, 0)

    def test_train_position_to_dict(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end=LeadingEnd.A,
            occupied_tiles=[(0, 0), (1, 0)],
            occupied_blocks=["BLOCK_1"],
        )
        data = pos.to_dict()
        assert data["end_a"]["tile"] == [1, 0]
        assert data["end_a"]["distance_cm"] == 50.0
        assert data["leading_end"] == "a"
        assert data["facing_direction"] == "EAST"

    def test_train_position_from_dict(self):
        data = {
            "end_a": {"tile": [1, 0], "distance_cm": 50.0, "entry_edge": "W"},
            "end_b": {"tile": [0, 0], "distance_cm": 10.0, "entry_edge": "W"},
            "leading_end": "a",
            "facing_direction": "EAST",
            "occupied_tiles": [[0, 0], [1, 0]],
            "occupied_blocks": ["BLOCK_1"],
        }
        pos = TrainPosition.from_dict(data)
        assert pos is not None
        assert pos.end_a_tile == (1, 0)
        assert pos.facing_direction == Direction.EAST



class TestTrainPropertyUpdate:
    """Tests for train property updates."""

    def test_train_name_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.name = "Original Name"
        network.add_train(train)
        train.name = "Updated Name"
        assert network.trains["TRAIN_01"].name == "Updated Name"

    def test_train_max_speed_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        train.max_speed = 120.0
        assert network.trains["TRAIN_01"].max_speed == 120.0

    def test_train_length_update(self, network):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)
        assert train.length_cm == 20.0
        train.consist.append(TrainCar(car_id="CAR_02", car_type="FREIGHT", length=30.0))
        assert train.length_cm == 50.0



