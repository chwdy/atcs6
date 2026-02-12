"""
Test Extend Path Bug

These tests reproduce and verify the fix for the extend path bug where:
1. Extending a path doesn't update the train's current_path
2. The train's ato_destination_block_id remains pointing to the old destination
3. This causes lookahead glitches and wrong stopping behavior

Bug Report:
- User extends default demo path from East Terminal to east branch Station
- Path is extended in PathDispatcher but train still aims for East Terminal
- Lookahead glitches showing incorrect block lengths
"""

import pytest
from datetime import datetime, timedelta

from network import RailwayNetwork
from track import TrackSegment, TRACK_EW, TRACK_NS, TRACK_NW, TRACK_SE
from train import Train, TrainCar, TrainPosition
from path_dispatcher import PathDispatcher
from route_executor import RouteExecutor
from train_controller import create_controller_for_train
from enums import Direction
from block import BlockState
from path_utils import assign_complete_path_to_train


class TestExtendPathBug:
    """
    Comprehensive tests for the extend path bug.

    BUG DESCRIPTION:
    When extending a path to a new destination, the PathDispatcher correctly
    updates the Path object (path_tiles, block_sequence, end_block_id), but
    the train's current_path and ato_destination_block_id are NOT updated.

    This causes:
    1. Train continues following old path (current_path)
    2. Train aims to stop at old destination (ato_destination_block_id)
    3. Lookahead calculations use stale data, showing wrong block lengths

    FIX:
    After extending a path, update the assigned train's:
    - current_path (append new tiles)
    - ato_destination_block_id (set to new end_block_id)
    """

    @pytest.fixture
    def simple_network(self):
        """Create a simple linear network for testing extend path."""
        network = RailwayNetwork()

        # Create a simple linear track: A - B - C - D - E
        # All connected with EW tracks
        for x in range(0, 10):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create 5 blocks
        network.create_block_with_tracks(
            block_id="BLOCK_A",
            positions=[(0, 0), (1, 0)],
            name="Block A",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_B",
            positions=[(2, 0), (3, 0)],
            name="Block B",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_C",
            positions=[(4, 0), (5, 0)],
            name="Block C",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_D",
            positions=[(6, 0), (7, 0)],
            name="Block D",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_E",
            positions=[(8, 0), (9, 0)],
            name="Block E",
            length_cm=100.0,
        )

        return network

    @pytest.fixture
    def demo_train(self, simple_network):
        """Create and place a demo train."""
        car = TrainCar(car_id="DEMO_CAR_01", car_type="LOCOMOTIVE", length=40.0)
        train = Train(train_id="DEMO_TRAIN", consist=[car], max_speed=60.0)
        simple_network.add_train(train)
        simple_network.place_train(train, 0, 0, Direction.EAST)

        # Occupy the starting block
        start_track = simple_network.get_track(0, 0)
        if (
            start_track
            and start_track.block_section_id
            and simple_network.block_manager
        ):
            simple_network.block_manager.occupy_block(
                start_track.block_section_id, train.train_id
            )

        return train

    def test_extend_path_updates_train_destination(self, simple_network, demo_train):
        """
        Test that extending a path updates the destination visible to the controller.

        After extending, the controller (reading from the dispatcher) should see
        the new destination and extended path tiles.
        """
        dispatcher = PathDispatcher(simple_network)
        executor = RouteExecutor(simple_network, dispatcher)

        # Create initial path from A to C
        path_result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_C",
            train_id=demo_train.train_id,
        )

        assert path_result.success, f"Failed to create path: {path_result.error}"
        path = path_result.path

        # Assign path to train
        raw_path = executor._convert_path_to_train_path(path)
        success, error = assign_complete_path_to_train(
            simple_network, demo_train, raw_path
        )
        assert success, f"Failed to assign path: {error}"
        demo_train.assigned_path_id = path.path_id

        # Create controller WITH dispatcher
        controller = create_controller_for_train(
            train_id=demo_train.train_id,
            network=simple_network,
            path_dispatcher=dispatcher,
        )
        controller.ato_destination_block_id = path.end_block_id

        # Store initial path info
        initial_tiles = controller.get_path_tiles()
        initial_path_length = len(initial_tiles)

        # Extend path from C to E
        extend_result = dispatcher.extend_path(path.path_id, "BLOCK_E")
        assert extend_result.success, f"Failed to extend path: {extend_result.error}"

        # Verify dispatcher's Path object is updated
        updated_path = dispatcher.get_path(path.path_id)
        assert updated_path.end_block_id == "BLOCK_E"

        # Verify controller sees extended path immediately via get_path_tiles()
        extended_tiles = controller.get_path_tiles()
        assert len(extended_tiles) > initial_path_length, (
            f"Controller should see extended path immediately.\n"
            f"  Initial: {initial_path_length} tiles\n"
            f"  After extension: {len(extended_tiles)} tiles"
        )

        # The extended path should end at a tile in BLOCK_E
        final_tile = extended_tiles[-1]
        final_track = simple_network.get_track(final_tile[0], final_tile[1])
        assert final_track is not None
        assert final_track.block_section_id == "BLOCK_E"

    def test_extend_path_updates_lookahead(self, simple_network, demo_train):
        """
        Test that extending a path properly updates the lookahead data.

        The lookahead should reflect the extended path, not the old one.
        """
        dispatcher = PathDispatcher(simple_network)
        executor = RouteExecutor(simple_network, dispatcher)

        # Create and assign initial path A -> C
        path_result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_C",
            train_id=demo_train.train_id,
        )
        assert path_result.success

        raw_path = executor._convert_path_to_train_path(path_result.path)
        assign_complete_path_to_train(simple_network, demo_train, raw_path)
        demo_train.assigned_path_id = path_result.path.path_id

        # Create controller WITH dispatcher
        controller = create_controller_for_train(
            train_id=demo_train.train_id,
            network=simple_network,
            path_dispatcher=dispatcher,
        )
        controller.ato_destination_block_id = path_result.path.end_block_id

        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)
        controller.depart()

        # Sync train state
        demo_train.state = controller.state
        demo_train.ato_running = controller.ato_running

        # Get initial lookahead
        initial_lookahead = controller.get_monitor_lookahead()
        initial_blocks = initial_lookahead.get("lookahead", {}).get("blocks", [])
        initial_block_ids = [b["id"] for b in initial_blocks]

        print(f"\nInitial lookahead blocks: {initial_block_ids}")

        # Extend path to E
        extend_result = dispatcher.extend_path(path_result.path.path_id, "BLOCK_E")
        assert extend_result.success

        # Force controller to recalculate lookahead
        controller.lookahead = controller._calculate_lookahead()

        # Get updated lookahead
        updated_lookahead = controller.get_monitor_lookahead()
        updated_blocks = updated_lookahead.get("lookahead", {}).get("blocks", [])
        updated_block_ids = [b["id"] for b in updated_blocks]

        print(f"Updated lookahead blocks: {updated_block_ids}")

        # BUG VERIFICATION: Lookahead should include the new destination block
        assert "BLOCK_E" in updated_block_ids, (
            f"BUG: Extended destination block (BLOCK_E) not in lookahead!\n"
            f"  Lookahead blocks: {updated_block_ids}\n"
            f"  Expected: BLOCK_E"
        )

        # The lookahead should have more blocks now (path is longer)
        assert len(updated_blocks) >= len(initial_blocks), (
            f"BUG: Lookahead has fewer blocks after extension!\n"
            f"  Initial: {len(initial_blocks)} blocks\n"
            f"  Updated: {len(updated_blocks)} blocks"
        )

    def test_extend_path_with_train_in_motion(self, simple_network, demo_train):
        """
        Test extending path while train is in motion.

        This simulates the real-world scenario where the user extends the path
        while the train is already traveling toward the original destination.
        The controller reads from the dispatcher, so the extension is visible
        immediately without any sync.
        """
        dispatcher = PathDispatcher(simple_network)
        executor = RouteExecutor(simple_network, dispatcher)

        # Create and assign path A -> C
        path_result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_C",
            train_id=demo_train.train_id,
        )
        assert path_result.success

        raw_path = executor._convert_path_to_train_path(path_result.path)
        assign_complete_path_to_train(simple_network, demo_train, raw_path)
        demo_train.assigned_path_id = path_result.path.path_id

        # Create controller WITH dispatcher and start train
        controller = create_controller_for_train(
            train_id=demo_train.train_id,
            network=simple_network,
            path_dispatcher=dispatcher,
        )
        controller.ato_destination_block_id = path_result.path.end_block_id

        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)
        controller.depart()

        # Simulate train moving to BLOCK_B (middle of route)
        from train import LeadingEnd

        demo_train.position = TrainPosition(
            end_a_tile=(3, 0),
            end_a_distance_cm=25.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=25.0,
            end_b_entry_edge="W",
            leading_end=LeadingEnd.A,
            occupied_tiles=[(2, 0), (3, 0)],
            occupied_blocks=["BLOCK_B"],
        )
        demo_train.path_index = 3  # Approximate position in path

        # Update block occupancy to match
        simple_network.block_manager.clear_block("BLOCK_A")
        simple_network.block_manager.occupy_block("BLOCK_B", demo_train.train_id)

        # Get path tiles before extension
        tiles_before = controller.get_path_tiles()
        count_before = len(tiles_before)

        # Extend path to E
        extend_result = dispatcher.extend_path(path_result.path.path_id, "BLOCK_E")
        assert extend_result.success, f"Extension failed: {extend_result.error}"

        # Verify dispatcher's Path object is updated
        updated_path = dispatcher.get_path(path_result.path.path_id)
        assert updated_path.end_block_id == "BLOCK_E"

        # Controller should see extended path immediately
        tiles_after = controller.get_path_tiles()
        assert len(tiles_after) > count_before, (
            f"Controller should see extended path while train is in motion.\n"
            f"  Before: {count_before} tiles\n"
            f"  After: {len(tiles_after)} tiles"
        )

        # path_index should still be valid
        assert len(tiles_after) > demo_train.path_index, (
            f"Path index should still be valid after extension.\n"
            f"  Path index: {demo_train.path_index}\n"
            f"  Path length: {len(tiles_after)}"
        )


class TestExtendPathBugFix:
    """
    Tests to verify the fix for the extend path bug.

    These tests will pass after the fix is implemented.
    """

    pass


class TestGetPathTiles:
    """Tests for TrainController.get_path_tiles() reading from PathDispatcher."""

    @pytest.fixture
    def simple_network(self):
        """Create a simple linear network for testing."""
        network = RailwayNetwork()

        for x in range(0, 10):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(track)

        network.create_block_with_tracks(
            block_id="BLOCK_A",
            positions=[(0, 0), (1, 0)],
            name="Block A",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_B",
            positions=[(2, 0), (3, 0)],
            name="Block B",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_C",
            positions=[(4, 0), (5, 0)],
            name="Block C",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_D",
            positions=[(6, 0), (7, 0)],
            name="Block D",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_E",
            positions=[(8, 0), (9, 0)],
            name="Block E",
            length_cm=100.0,
        )

        return network

    def test_get_path_tiles_returns_dispatcher_path(self, simple_network):
        """get_path_tiles reads from dispatcher, not train.current_path."""
        network = simple_network
        dispatcher = PathDispatcher(network)

        # Create a train and place it
        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Occupy starting block
        if network.block_manager:
            network.block_manager.occupy_block("BLOCK_A", train.train_id)

        # Create a path
        result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_C",
            train_id=train.train_id,
        )
        assert result.success
        train.assigned_path_id = result.path.path_id

        # Create controller with dispatcher
        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        # get_path_tiles should return tiles from dispatcher
        path_tiles = controller.get_path_tiles()
        assert path_tiles is not None
        assert len(path_tiles) > 0

        # Each entry should be (x, y, Direction)
        for entry in path_tiles:
            assert len(entry) == 3
            assert isinstance(entry[2], Direction)

    def test_get_path_tiles_returns_none_when_no_path(self, simple_network):
        """get_path_tiles returns None when train has no assigned path."""
        network = simple_network
        dispatcher = PathDispatcher(network)
        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        assert controller.get_path_tiles() is None

    def test_get_path_tiles_reflects_extension(self, simple_network):
        """After extend_path, get_path_tiles returns the extended path immediately."""
        network = simple_network
        dispatcher = PathDispatcher(network)

        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Occupy starting block
        if network.block_manager:
            network.block_manager.occupy_block("BLOCK_A", train.train_id)

        # Create initial path
        result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_C",
            train_id=train.train_id,
        )
        assert result.success
        train.assigned_path_id = result.path.path_id

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        tiles_before = controller.get_path_tiles()
        count_before = len(tiles_before)

        # Extend the path
        ext_result = dispatcher.extend_path(result.path.path_id, "BLOCK_E")
        assert ext_result.success, f"Extension failed: {ext_result.error}"

        tiles_after = controller.get_path_tiles()
        assert len(tiles_after) > count_before, (
            "get_path_tiles should reflect extension immediately"
        )

    def test_get_path_tiles_fallback_without_dispatcher(self, simple_network):
        """Without a dispatcher, get_path_tiles falls back to train.current_path."""
        network = simple_network
        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)

        # Create controller WITHOUT dispatcher
        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
        )

        # Set a fake current_path and assigned_path_id on the train
        fake_path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]
        train.current_path = fake_path
        train.assigned_path_id = "FAKE_PATH"

        # Should fall back to train.current_path
        result = controller.get_path_tiles()
        assert result == fake_path


class TestAtoDestinationCleanup:
    """Tests that ato_destination_block_id is cleared on path completion."""

    @pytest.fixture
    def simple_network(self):
        """Create a simple linear network for testing."""
        network = RailwayNetwork()

        for x in range(0, 10):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(track)

        network.create_block_with_tracks(
            block_id="BLOCK_A",
            positions=[(0, 0), (1, 0)],
            name="Block A",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_B",
            positions=[(2, 0), (3, 0)],
            name="Block B",
            length_cm=100.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_C",
            positions=[(4, 0), (5, 0)],
            name="Block C",
            length_cm=100.0,
        )

        return network

    def test_destination_cleared_on_complete_path(self, simple_network):
        """ato_destination_block_id should be None after complete_path()."""
        network = simple_network
        dispatcher = PathDispatcher(network)

        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        if network.block_manager:
            network.block_manager.occupy_block("BLOCK_A", train.train_id)

        result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_C",
            train_id=train.train_id,
        )
        assert result.success

        train.assigned_path_id = result.path.path_id
        train.ato_destination_block_id = "BLOCK_C"

        # Complete the path
        dispatcher.complete_path(result.path.path_id)

        # ato_destination_block_id should be cleared on the train
        assert train.ato_destination_block_id is None, (
            "train.ato_destination_block_id should be cleared on path completion"
        )
        assert train.assigned_path_id is None, (
            "train.assigned_path_id should be cleared on path completion"
        )
        assert train.current_path is None, (
            "train.current_path should be cleared on path completion"
        )

    def test_destination_cleared_on_internal_completion(self, simple_network):
        """ato_destination_block_id cleared via _complete_path_on_head_arrival."""
        network = simple_network
        dispatcher = PathDispatcher(network)

        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        if network.block_manager:
            network.block_manager.occupy_block("BLOCK_A", train.train_id)

        result = dispatcher.create_path(
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_C",
            train_id=train.train_id,
        )
        assert result.success

        train.assigned_path_id = result.path.path_id
        train.ato_destination_block_id = "BLOCK_C"

        # Simulate internal completion (called by tick when train arrives)
        path = dispatcher.get_path(result.path.path_id)
        dispatcher._complete_path_on_head_arrival(path, train)

        assert train.ato_destination_block_id is None
        assert train.assigned_path_id is None
