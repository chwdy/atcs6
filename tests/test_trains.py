"""
Train Tests - Train placement, movement, and occupancy

Run with: pytest tests/test_trains.py -v
"""

import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState
from train import Train, TrainCar, TrainPosition
from dispatcher import TrainDispatcher
from simulation import SimulationController


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


class TestTrainDispatcher:
    """Tests for TrainDispatcher class"""

    def test_dispatcher_creation(self, network):
        dispatcher = TrainDispatcher(network)
        assert dispatcher is not None
        assert dispatcher.network == network


class TestSimulationController:
    """Tests for SimulationController class"""

    def test_simulation_creation(self, network):
        simulation = SimulationController(network)
        assert simulation is not None
        assert simulation.network == network
        assert simulation.power_controller is not None
        assert simulation.route_executor is not None
        assert simulation.speed_profile_registry is not None


class TestTrainManualControlBugFixes:
    """Tests for train manual control and ATC fallback fixes."""

    def test_clear_direction_from_standby(self):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.EAST
        train.travel_direction = "reverse"
        train.manual_target_speed = 20.0
        train.target_speed = 15.0
        assert train.clear_direction() is True
        assert train.state == TrainState.IDLE
        assert train.facing_direction is None
        assert train.travel_direction == "forward"
        assert train.manual_target_speed == 0.0
        assert train.target_speed == 0.0

    def test_clear_direction_rejects_moving(self):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_02", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.NORTH
        train.current_speed = 5.0
        assert train.clear_direction() is False
        assert train.state == TrainState.STANDBY

    def test_manual_speed_requires_manual_and_clamps(self):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_03", consist=[car], max_speed=40.0)
        assert train.set_manual_speed(30.0) is False
        train.state = TrainState.MANUAL
        assert train.set_manual_speed(60.0) is True
        assert train.manual_target_speed == 40.0

    def test_travel_direction_requires_manual(self):
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_04", consist=[car], max_speed=60.0)
        assert train.set_travel_direction("reverse") is False
        train.state = TrainState.MANUAL
        assert train.set_travel_direction("reverse") is True
        assert train.travel_direction == "reverse"


class TestTrainPlacementOccupancy:
    """Tests for train placement and occupancy."""

    def test_train_update_occupancy_sets_block_occupied(self, network):
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        block = Block(block_id="BLK_001", name="Test Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0)]
        track.block_section_id = "BLK_001"
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.set_position((0, 0), Direction.EAST)
        assert block.state == BlockState.FREE
        network.add_train(train)
        train.update_occupancy(network)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_01"

    def test_train_update_occupancy_with_multiple_tiles(self, network):
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
            track.block_section_id = "BLK_001"
        block = Block(block_id="BLK_001", name="Long Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0), (1, 0), (2, 0)]
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_03", consist=[car], max_speed=60.0)
        train.set_position((1, 0), Direction.EAST)
        network.add_train(train)
        train.update_occupancy(network)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_03"


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
        assert train.current_pos == (0, 0)
        assert train.current_direction == Direction.EAST
        tile_length_cm = network._get_tile_length_cm(0, 0)
        assert train.distance_on_track == tile_length_cm

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
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0)],
            occupied_blocks=["BLOCK_1"],
        )
        assert pos.end_a_tile == (1, 0)
        assert pos.end_b_tile == (0, 0)
        assert pos.leading_end == "a"

    def test_train_position_head_tail_properties(self):
        pos = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
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
            leading_end="a",
            facing_direction=Direction.EAST,
        )
        pos.reverse()
        assert pos.leading_end == "b"
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
            leading_end="a",
            facing_direction=Direction.EAST,
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


class TestTrainPlacementValidation:
    """Tests for train placement with length validation."""

    def test_40cm_train_on_150cm_block_fits(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert train.position is not None
        assert train.position.head_tile == (1, 0)

    def test_80cm_train_spans_two_tiles(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert train.position.head_tile == (1, 0)
        assert train.position.tail_tile == (0, 0)
        assert len(train.position.occupied_tiles) == 2

    def test_train_too_long_raises_error(self, network):
        for x in range(2):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=100.0)
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=120.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        with pytest.raises(ValueError, match="no track at"):
            network.place_train(train, 1, 0, Direction.EAST)


class TestTrainOccupiedTilesRich:
    """Tests for get_occupied_tiles_rich method."""

    def test_rich_tiles_single_tile_train(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        rich = network.get_occupied_tiles_rich(train)
        assert len(rich) == 1
        assert rich[0]["tile"] == (1, 0)
        assert rich[0]["block_id"] == "BLOCK_1"


class TestTrainReverse:
    """Tests for train reversal."""

    def test_reverse_train_swaps_leading_end(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert train.position.leading_end == "a"
        assert train.position.facing_direction == Direction.EAST
        old_head = train.position.head_tile
        network.reverse_train(train)
        assert train.position.leading_end == "b"
        assert train.position.facing_direction == Direction.WEST


class TestRaceConditionFix:
    """Tests for race condition fixes."""

    def test_block_manager_prevents_premature_release(self, network):
        block = network.create_block("B1", "Block 1")
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED
        network.block_manager.set_block_to_caution("B1")
        assert block.state == BlockState.CAUTION
        assert block.state != BlockState.FREE

    def test_simulation_no_longer_calls_update_occupancy(self):
        import simulation
        import inspect

        source = inspect.getsource(simulation.SimulationController._advance_train_track)
        assert "update_occupancy" not in source


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_trains.py -v")
