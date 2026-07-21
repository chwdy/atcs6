"""
Switch and Path Tests - Switch locking, routes, and path operations

Run with: pytest tests/test_interlocking.py -v
"""

import pytest
from types import SimpleNamespace

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction
from train import Train, TrainCar
from tests.train_test_helpers import set_train_span
from train_controller import create_controller_for_train
from path_utils import (
    prepare_path_for_train,
    append_destination_block_tiles,
    _positions_to_path_tiles,
)


def _stub_switch_manager(network=None):
    """Minimal stand-in for SwitchManager.request_position_change(...).

    Provides the BlockManager notification side-effect that the production
    `SwitchManager` performs after `Switch.apply_position_change(...)` succeeds.
    """
    from switch_manager import SwitchManager

    block_manager = SimpleNamespace(on_switch_position_changed=lambda switch_id: None)
    sim = SimpleNamespace(block_manager=block_manager, network=network)
    switches = network.switches if network is not None else {}
    return SwitchManager(
        switches=switches,
        simulation_controller=sim,
        network=network,
    )


@pytest.fixture
def network():
    return RailwayNetwork()


class TestSwitchLocking:
    """Tests for switch locking functionality."""

    def test_switch_lock_unlock(self):
        from switch import Switch
        from runtime_payloads import SwitchLockClaim

        switch = Switch(switch_id="SW_1", x=0, y=0)
        assert (
            switch.acquire_lock(
                SwitchLockClaim(
                    path_id="PATH_1",
                    train_id="TRAIN_A",
                    locked_state=0,
                    source_switch_id=None,
                )
            )
            is True
        )
        assert switch.is_locked() is True
        # v1: locks is a dict keyed by (path_id, source_switch_id).
        assert ("PATH_1", None) in switch.locks
        assert switch.locks[("PATH_1", None)] == ("TRAIN_A", 0)
        assert switch.release_lock("PATH_1", source_switch_id=None) is True
        assert switch.is_locked() is False

    def test_switch_lock_by_different_path_fails(self):
        from switch import Switch
        from runtime_payloads import SwitchLockClaim

        switch = Switch(switch_id="SW_1", x=0, y=0)
        # Same-state locks may co-exist; different-state locks conflict.
        assert (
            switch.acquire_lock(
                SwitchLockClaim(path_id="PATH_1", train_id="TRAIN_A", locked_state=0)
            )
            is True
        )
        assert (
            switch.acquire_lock(
                SwitchLockClaim(path_id="PATH_2", train_id="TRAIN_B", locked_state=1)
            )
            is False
        )

    def test_switch_position_change_blocked_when_locked(self):
        from switch import Switch
        from runtime_payloads import SwitchLockClaim

        network = RailwayNetwork()
        network.add_track(Tile(0, 0, track_bits=TRACK_EW | TRACK_NE))
        switch = Switch(switch_id="SW_1", x=0, y=0)
        network.add_switch(switch)
        switch_manager = _stub_switch_manager(network)
        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_1", train_id="TRAIN_A", locked_state=0)
        )
        success, msg = switch_manager.request_position_change("SW_1", 1)
        assert success is False
        assert "locked" in msg.lower()

    def test_switch_position_change_allowed_when_unlocked(self):
        from switch import Switch

        network = RailwayNetwork()
        network.add_track(Tile(0, 0, track_bits=TRACK_EW | TRACK_NE))
        switch = Switch(switch_id="SW_1", x=0, y=0)
        network.add_switch(switch)
        switch_manager = _stub_switch_manager(network)
        success, msg = switch_manager.request_position_change("SW_1", 1)
        assert success is True
        # v1: position is authoritative on the Switch entity (not the Tile).
        assert int(switch.position) == 1

    def test_switch_serialization_includes_lock_state(self):
        from switch import Switch
        from runtime_payloads import SwitchLockClaim

        switch = Switch(switch_id="SW_1", x=0, y=0)
        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_1", train_id="TRAIN_A", locked_state=1)
        )
        data = switch.to_dict()
        # v1: locks is a list of dicts.
        assert isinstance(data["locks"], list)
        assert len(data["locks"]) == 1
        lock_entry = data["locks"][0]
        assert lock_entry["path_id"] == "PATH_1"
        # claim_role is a derived diagnostic field; per design_new.md:651 it is
        # "main" when source_switch_id is None (else "flank").
        assert lock_entry["claim_role"] == "main"
        assert lock_entry["source_switch_id"] is None
        assert lock_entry["train_id"] == "TRAIN_A"
        assert lock_entry["locked_state"] == 1

    def test_switch_deserialization_ignores_runtime_locks(self):
        """Runtime lock state is not persisted (design_new.md Out of Scope:
        "Persistence / save-load of runtime state | In-memory only"), so
        legacy snapshots that carry `locks` must be loaded clean.
        """
        from switch import Switch

        data = {
            "switch_id": "SW_1",
            "x": 0,
            "y": 0,
            "main_length_cm": 0.0,
            "diverge_length_cm": 0.0,
            "locks": [
                {
                    "path_id": "PATH_1",
                    "source_switch_id": None,
                    "train_id": "TRAIN_A",
                    "locked_state": 1,
                }
            ],
        }
        switch = Switch.from_dict(data)
        assert switch.is_locked() is False
        assert switch.locks == {}


class TestSwitchLockingInRoutes:
    """Tests for switch locking when routes are created."""

    def test_get_required_switch_state_main_path(self):
        network = RailwayNetwork()
        switch = Tile(0, 0, track_bits=18)  # EW + SE
        network.add_track(switch)
        network.propagate_from_tile(0, 0)
        assert switch.is_switch
        assert switch.get_required_switch_state("W", "E") == 0
        assert switch.get_required_switch_state("E", "W") == 0

    def test_get_required_switch_state_diverge_path(self):
        network = RailwayNetwork()
        switch = Tile(0, 0, track_bits=18)
        network.add_track(switch)
        network.propagate_from_tile(0, 0)
        assert switch.get_required_switch_state("E", "S") == 1
        assert switch.get_required_switch_state("S", "E") == 1

    def test_get_required_switch_state_invalid_path(self):
        network = RailwayNetwork()
        switch = Tile(0, 0, track_bits=18)
        network.add_track(switch)
        network.propagate_from_tile(0, 0)
        assert switch.get_required_switch_state("W", "S") is None
        assert switch.get_required_switch_state("N", "E") is None




class TestPathPrepending:
    """Tests for path prepending functionality."""

    def test_prepend_tiles_when_train_before_path_start(self):
        """Test that tiles are prepended when train is before path start."""
        from path_utils import prepare_path_for_train
        from enums import Direction

        network = RailwayNetwork()
        for x in range(4):
            track = Tile(x, 0, track_bits=TRACK_EW)
            track.segment_id = "BLK_1"
            network.add_track(track)
        network.blocks["BLK_1"] = Block(block_id="BLK_1")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        controller = create_controller_for_train(train.train_id, network)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)
        path_tiles = [
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
        ]
        result = prepare_path_for_train(network, train, path_tiles, controller=controller)
        assert result.success is True
        assert result.prepend_tiles is not None
        assert len(result.prepend_tiles) == 2

    def test_no_prepend_when_train_at_path_start(self):
        """Test no prepending when train is at path start."""
        network = RailwayNetwork()
        for x in range(4):
            track = Tile(x, 0, track_bits=TRACK_EW)
            track.segment_id = "BLK_1"
            network.add_track(track)
        network.blocks["BLK_1"] = Block(block_id="BLK_1")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        controller = create_controller_for_train(train.train_id, network)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)
        path_tiles = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
        ]
        result = prepare_path_for_train(network, train, path_tiles, controller=controller)
        assert result.success is True
        assert result.prepend_tiles is None

    def test_reject_when_train_and_path_in_different_blocks(self):
        """Test rejection when train and path are in different blocks."""
        network = RailwayNetwork()
        for x in range(2):
            track = Tile(x, 0, track_bits=TRACK_EW)
            track.segment_id = "BLK_A"
            network.add_track(track)
        for x in range(3, 5):
            track = Tile(x, 0, track_bits=TRACK_EW)
            track.segment_id = "BLK_B"
            network.add_track(track)
        network.blocks["BLK_A"] = Block(block_id="BLK_A")
        network.blocks["BLK_B"] = Block(block_id="BLK_B")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        controller = create_controller_for_train(train.train_id, network)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)
        path_tiles = [
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]
        result = prepare_path_for_train(network, train, path_tiles, controller=controller)
        assert result.success is False
        assert "different blocks" in result.error

    def test_reject_empty_path(self):
        """Test rejection of empty path."""
        network = RailwayNetwork()
        track = Tile(0, 0, track_bits=TRACK_EW)
        track.segment_id = "BLK_1"
        network.add_track(track)
        network.blocks["BLK_1"] = Block(block_id="BLK_1")
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=50.0)
        train = Train(train_id="TRN_1", consist=[car], max_speed=100.0)
        controller = create_controller_for_train(train.train_id, network)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)
        result = prepare_path_for_train(network, train, [], controller=controller)
        assert result.success is False
        assert "Empty path" in result.error


class TestPathAppending:
    """Tests for path appending functionality."""

    def test_append_tiles_to_complete_destination_block(self):
        from path_utils import append_destination_block_tiles
        from enums import Direction

        network = RailwayNetwork()
        for x in range(2):
            track = Tile(x, 0, track_bits=TRACK_EW)
            track.segment_id = "BLK_SOURCE"
            network.add_track(track)
        network.blocks["BLK_SOURCE"] = Block(block_id="BLK_SOURCE")
        for x in range(2, 5):
            track = Tile(x, 0, track_bits=TRACK_EW)
            track.segment_id = "BLK_DEST"
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
            track = Tile(x, 0, track_bits=TRACK_EW)
            track.segment_id = "BLK_DEST"
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


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_interlocking.py -v")
