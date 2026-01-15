"""
Interlocking Tests - Switch locking, routes, and path operations

Run with: pytest tests/test_interlocking.py -v
"""

import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction
from interlocking import InterlockingSystem, InterlockingRoute
from train import Train, TrainCar
from path_utils import (
    prepare_path_for_train,
    append_destination_block_tiles,
    _positions_to_path_tiles,
)


@pytest.fixture
def network():
    return RailwayNetwork()


class TestInterlockingSystem:
    """Tests for InterlockingSystem class"""

    def test_interlocking_creation(self, network):
        interlocking = InterlockingSystem()
        assert interlocking is not None
        assert interlocking.routes == {}


class TestSwitchLocking:
    """Tests for switch locking functionality."""

    def test_switch_lock_unlock(self):
        from switch import Switch

        switch = Switch(switch_id="SW_1", x=0, y=0)
        assert switch.lock("PATH_1", 0) is True
        assert switch.is_locked() is True
        assert switch.locked_by_path == "PATH_1"
        assert switch.locked_state == 0
        assert switch.unlock("PATH_1") is True
        assert switch.is_locked() is False

    def test_switch_lock_by_different_path_fails(self):
        from switch import Switch

        switch = Switch(switch_id="SW_1", x=0, y=0)
        assert switch.lock("PATH_1", 0) is True
        assert switch.lock("PATH_2", 1) is False

    def test_switch_state_change_blocked_when_locked(self):
        from switch import Switch

        network = RailwayNetwork()
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW | TRACK_NE))
        switch = Switch(switch_id="SW_1", x=0, y=0)
        network.add_switch(switch)
        switch.lock("PATH_1", 0)
        success, msg = switch.request_state_change(1, network)
        assert success is False
        assert "locked" in msg.lower()

    def test_switch_state_change_allowed_when_unlocked(self):
        from switch import Switch

        network = RailwayNetwork()
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW | TRACK_NE))
        switch = Switch(switch_id="SW_1", x=0, y=0)
        network.add_switch(switch)
        success, msg = switch.request_state_change(1, network)
        assert success is True
        track = network.get_track(0, 0)
        assert track.switch_state == 1

    def test_switch_serialization_includes_lock_state(self):
        from switch import Switch

        switch = Switch(switch_id="SW_1", x=0, y=0)
        switch.lock("PATH_1", 1)
        data = switch.to_dict()
        assert data["locked_by_path"] == "PATH_1"
        assert data["locked_state"] == 1

    def test_switch_deserialization_restores_lock_state(self):
        from switch import Switch

        data = {
            "switch_id": "SW_1",
            "x": 0,
            "y": 0,
            "locked_by_path": "PATH_1",
            "locked_state": 1,
        }
        switch = Switch.from_dict(data)
        assert switch.is_locked() is True
        assert switch.locked_by_path == "PATH_1"
        assert switch.locked_state == 1


class TestSwitchLockingInRoutes:
    """Tests for switch locking when routes are created."""

    def test_get_required_switch_state_main_path(self):
        network = RailwayNetwork()
        switch = TrackSegment(0, 0, track_bits=18)  # EW + SE
        network.add_track(switch)
        network.propagate_from_tile(0, 0)
        assert switch.is_switch
        assert switch.get_required_switch_state("W", "E") == 0
        assert switch.get_required_switch_state("E", "W") == 0

    def test_get_required_switch_state_diverge_path(self):
        network = RailwayNetwork()
        switch = TrackSegment(0, 0, track_bits=18)
        network.add_track(switch)
        network.propagate_from_tile(0, 0)
        assert switch.get_required_switch_state("E", "S") == 1
        assert switch.get_required_switch_state("S", "E") == 1

    def test_get_required_switch_state_invalid_path(self):
        network = RailwayNetwork()
        switch = TrackSegment(0, 0, track_bits=18)
        network.add_track(switch)
        network.propagate_from_tile(0, 0)
        assert switch.get_required_switch_state("W", "S") is None
        assert switch.get_required_switch_state("N", "E") is None

    def test_path_locks_switch_entering_from_non_switch_point(self):
        from path_dispatcher import PathDispatcher

        network = RailwayNetwork()
        t0 = TrackSegment(0, 0, track_bits=2)
        t0.block_section_id = "BLOCK_A"
        network.add_track(t0)
        t1 = TrackSegment(1, 0, track_bits=2)
        t1.block_section_id = "BLOCK_A"
        network.add_track(t1)
        switch = TrackSegment(2, 0, track_bits=18)
        network.add_track(switch)
        network.propagate_from_tile(2, 0)
        t3 = TrackSegment(3, 0, track_bits=2)
        t3.block_section_id = "BLOCK_B"
        network.add_track(t3)
        network.blocks["BLOCK_A"] = Block("BLOCK_A", state=BlockState.FREE)
        network.blocks["BLOCK_B"] = Block("BLOCK_B", state=BlockState.FREE)
        dispatcher = PathDispatcher(network)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result.success
        assert (2, 0) in result.path.switch_positions
        assert switch.is_locked_by_path()

    def test_path_cancellation_unlocks_switch(self):
        from path_dispatcher import PathDispatcher

        network = RailwayNetwork()
        t0 = TrackSegment(0, 0, track_bits=2)
        t0.block_section_id = "BLOCK_A"
        network.add_track(t0)
        switch = TrackSegment(1, 0, track_bits=18)
        network.add_track(switch)
        network.propagate_from_tile(1, 0)
        t2 = TrackSegment(2, 0, track_bits=2)
        t2.block_section_id = "BLOCK_B"
        network.add_track(t2)
        network.blocks["BLOCK_A"] = Block("BLOCK_A", state=BlockState.FREE)
        network.blocks["BLOCK_B"] = Block("BLOCK_B", state=BlockState.FREE)
        dispatcher = PathDispatcher(network)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result.success
        assert switch.is_locked_by_path()
        path_id = result.path.path_id
        dispatcher.cancel_path(path_id)
        assert not switch.is_locked_by_path()


class TestPathPrepending:
    """Tests for path prepending functionality."""

    def test_prepend_tiles_when_train_before_path_start(self):
        """Test that tiles are prepended when train is before path start."""
        from path_utils import prepare_path_for_train
        from enums import Direction

        network = RailwayNetwork()
        for x in range(4):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "BLK_1"
            network.add_track(track)
        network.blocks["BLK_1"] = Block(block_id="BLK_1")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        train.current_pos = (0, 0)
        train.facing_direction = Direction.EAST
        path_tiles = [
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
        ]
        result = prepare_path_for_train(network, train, path_tiles)
        assert result.success is True
        assert result.prepend_tiles is not None
        assert len(result.prepend_tiles) == 2

    def test_no_prepend_when_train_at_path_start(self):
        """Test no prepending when train is at path start."""
        network = RailwayNetwork()
        for x in range(4):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "BLK_1"
            network.add_track(track)
        network.blocks["BLK_1"] = Block(block_id="BLK_1")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        train.current_pos = (0, 0)
        train.facing_direction = Direction.EAST
        path_tiles = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
        ]
        result = prepare_path_for_train(network, train, path_tiles)
        assert result.success is True
        assert result.prepend_tiles is None

    def test_reject_when_train_and_path_in_different_blocks(self):
        """Test rejection when train and path are in different blocks."""
        network = RailwayNetwork()
        for x in range(2):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "BLK_A"
            network.add_track(track)
        for x in range(3, 5):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "BLK_B"
            network.add_track(track)
        network.blocks["BLK_A"] = Block(block_id="BLK_A")
        network.blocks["BLK_B"] = Block(block_id="BLK_B")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        train.current_pos = (0, 0)
        train.facing_direction = Direction.EAST
        path_tiles = [
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]
        result = prepare_path_for_train(network, train, path_tiles)
        assert result.success is False
        assert "different blocks" in result.error

    def test_reject_empty_path(self):
        """Test rejection of empty path."""
        network = RailwayNetwork()
        track = TrackSegment(0, 0, track_bits=TRACK_EW)
        track.block_section_id = "BLK_1"
        network.add_track(track)
        network.blocks["BLK_1"] = Block(block_id="BLK_1")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        train.current_pos = (0, 0)
        train.facing_direction = Direction.EAST
        result = prepare_path_for_train(network, train, [])
        assert result.success is False
        assert "Empty path" in result.error


class TestPathAppending:
    """Tests for path appending functionality."""

    def test_append_tiles_to_complete_destination_block(self):
        from path_utils import append_destination_block_tiles
        from enums import Direction

        network = RailwayNetwork()
        for x in range(2):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "BLK_SOURCE"
            network.add_track(track)
        network.blocks["BLK_SOURCE"] = Block(block_id="BLK_SOURCE")
        for x in range(2, 5):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "BLK_DEST"
            network.add_track(track)
        network.blocks["BLK_DEST"] = Block(block_id="BLK_DEST")
        path_tiles = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        extended_path, error = append_destination_block_tiles(network, path_tiles)
        assert error is None
        assert len(extended_path) == 5
        assert extended_path[3][:2] == (3, 0)
        assert extended_path[4][:2] == (4, 0)

    def test_no_append_when_path_ends_at_block_end(self):
        from path_utils import append_destination_block_tiles
        from enums import Direction

        network = RailwayNetwork()
        for x in range(3):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "BLK_DEST"
            network.add_track(track)
        network.blocks["BLK_DEST"] = Block(block_id="BLK_DEST")
        path_tiles = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        extended_path, error = append_destination_block_tiles(network, path_tiles)
        assert error is None
        assert len(extended_path) == 3


class TestPositionsToPathTilesDirections:
    """Tests for _positions_to_path_tiles direction inference."""

    def test_single_tile_defaults_to_east(self):
        from path_utils import _positions_to_path_tiles
        from enums import Direction

        positions = [(5, 5)]
        result = _positions_to_path_tiles(positions, None, None)
        assert len(result) == 1
        assert result[0] == (5, 5, Direction.EAST)

    def test_single_tile_uses_default_direction(self):
        from path_utils import _positions_to_path_tiles
        from enums import Direction

        positions = [(5, 5)]
        result = _positions_to_path_tiles(positions, Direction.NORTH, None)
        assert len(result) == 1
        assert result[0] == (5, 5, Direction.NORTH)

    def test_two_tiles_north_direction(self):
        from path_utils import _positions_to_path_tiles
        from enums import Direction

        positions = [(5, 0), (5, 1)]
        result = _positions_to_path_tiles(positions, None, None)
        assert len(result) == 2
        assert result[0] == (5, 0, Direction.NORTH)
        assert result[1] == (5, 1, Direction.NORTH)

    def test_two_tiles_south_direction(self):
        from path_utils import _positions_to_path_tiles
        from enums import Direction

        positions = [(5, 5), (5, 4)]
        result = _positions_to_path_tiles(positions, None, None)
        assert len(result) == 2
        assert result[0] == (5, 5, Direction.SOUTH)
        assert result[1] == (5, 4, Direction.SOUTH)

    def test_two_tiles_east_direction(self):
        from path_utils import _positions_to_path_tiles
        from enums import Direction

        positions = [(0, 5), (1, 5)]
        result = _positions_to_path_tiles(positions, None, None)
        assert len(result) == 2
        assert result[0] == (0, 5, Direction.EAST)
        assert result[1] == (1, 5, Direction.EAST)

    def test_two_tiles_west_direction(self):
        from path_utils import _positions_to_path_tiles
        from enums import Direction

        positions = [(5, 5), (4, 5)]
        result = _positions_to_path_tiles(positions, None, None)
        assert len(result) == 2
        assert result[0] == (5, 5, Direction.WEST)
        assert result[1] == (4, 5, Direction.WEST)


class TestTrainPathTiles:
    """Tests for train path tile operations."""

    def test_train_path_queue_initialization(self):
        """Test train path initialization (placeholder - module not available)."""
        pass

    def test_train_path_serialization(self):
        """Test train path serialization (placeholder - module not available)."""
        pass


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_interlocking.py -v")
