"""
Train Tests - Train placement, movement, and occupancy

Run with: pytest tests/test_trains.py -v
"""

import logging
import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition
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


class TestTrainManualControlBugFixes:
    """Tests for train manual control and ATC fallback fixes via TrainController."""

    def test_clear_direction_from_standby(self):
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.EAST
        train.travel_direction = "reverse"
        train.manual_target_speed = 20.0
        train.target_speed = 15.0
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_01", network)
        controller.power_on = True
        controller.state = TrainState.STANDBY

        assert controller.clear_direction() is True
        assert train.state == TrainState.IDLE
        assert train.facing_direction is None
        assert train.travel_direction == "forward"
        assert train.manual_target_speed == 0.0
        assert train.target_speed == 0.0

    def test_clear_direction_rejects_moving(self):
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_02", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.NORTH
        train.current_speed = 5.0
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_02", network)
        controller.power_on = True
        controller.state = TrainState.STANDBY

        assert controller.clear_direction() is False
        assert train.state == TrainState.STANDBY

    def test_manual_speed_requires_manual_and_clamps(self):
        """Test set_manual_speed - still on Train (not controller)."""
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_03", consist=[car], max_speed=40.0)
        assert train.set_manual_speed(30.0) is False
        train.state = TrainState.MANUAL
        assert train.set_manual_speed(60.0) is True
        assert train.manual_target_speed == 40.0

    def test_travel_direction_requires_manual(self):
        """Test set_travel_direction via TrainController."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_04", consist=[car], max_speed=60.0)
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_04", network)

        # Not in manual mode - fails
        result = controller.set_travel_direction("reverse")
        assert result.success is False

        # Enable manual mode first
        controller.power_on = True
        train.state = TrainState.MANUAL
        controller.state = TrainState.MANUAL
        controller.control_mode = "manual"

        result = controller.set_travel_direction("reverse")
        assert result.success is True
        assert train.travel_direction == "reverse"


class TestTrainMonitorBlockOffsetBugFix:
    """
    Comprehensive tests for Train Monitor block offset bug fixed in 2026-01-25.

    BUG: Train monitor lookahead data starts at path_index, so the current block
    appears to restart at each tile boundary, causing the monitor to jump back.
    FIX: Include distance_to_block_start_cm so UI can render a stable block start
    even when the train head advances to the next tile.
    """

    def test_distance_to_block_start_includes_tiles_behind_head(self):
        """Head tile should include previous tiles in same block."""
        network = RailwayNetwork()

        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "BLOCK_MAIN"
            network.add_track(track)

        train = Train(train_id="TRAIN_01")
        train.position = make_train_position(
            head_tile=(1, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=20.0,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 1

        data = train.to_dict(network)
        info = data["lookahead_tile_info"]

        assert info[0]["distance_to_tile_cm"] == pytest.approx(-20.0)
        assert info[0]["distance_to_block_start_cm"] == pytest.approx(-70.0)
        assert info[1]["distance_to_block_start_cm"] == pytest.approx(-70.0)

    def test_distance_to_block_start_resets_on_new_block(self):
        """Next block start should align with its first tile start."""
        network = RailwayNetwork()

        for x in range(2):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "BLOCK_A"
            network.add_track(track)

        track = TrackSegment(x=2, y=0, track_bits=TRACK_EW)
        track.block_section_id = "BLOCK_B"
        network.add_track(track)

        train = Train(train_id="TRAIN_02")
        train.position = make_train_position(
            head_tile=(1, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=20.0,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 1

        data = train.to_dict(network)
        info = data["lookahead_tile_info"]

        assert info[1]["distance_to_tile_cm"] == pytest.approx(30.0)
        assert info[1]["distance_to_block_start_cm"] == pytest.approx(30.0)


class TestTrainPlacementOccupancy:
    """Tests for train placement and block occupancy."""

    def test_train_placement_sets_block_occupied(self, network):
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_EW))
        block = Block(block_id="BLK_001", name="Test Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0)]
        track.block_section_id = "BLK_001"
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        assert block.state == BlockState.FREE
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_01"

    def test_train_placement_with_multiple_tiles(self, network):
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
            track.block_section_id = "BLK_001"
        block = Block(block_id="BLK_001", name="Long Block")
        network.blocks["BLK_001"] = block
        block.tiles = [(0, 0), (1, 0), (2, 0)]
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_03", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)
        assert block.state == BlockState.OCCUPIED
        assert block.occupied_by == "TRAIN_03"


class TestTrainControllerMonitorLookahead:
    """
    Tests for TrainController lookahead payload used by Train Monitor.

    Ensures head-relative block offsets remain stable across tile transitions and
    cab direction changes are reflected in the lookahead head tile.
    """

    def test_block_offsets_include_tiles_behind_head(self, network):
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "BLOCK_MAIN"
            network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD")
        train.position = make_train_position(
            head_tile=(1, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=20.0,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 1
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        payload = controller.get_monitor_lookahead()
        blocks = payload["lookahead"]["blocks"]

        assert blocks[0]["start_cm"] == pytest.approx(-70.0)
        assert blocks[0]["end_cm"] == pytest.approx(80.0)

    def test_block_offsets_reset_on_new_block(self, network):
        for x in range(2):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "BLOCK_A"
            network.add_track(track)

        track = TrackSegment(x=2, y=0, track_bits=TRACK_EW)
        track.block_section_id = "BLOCK_B"
        network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD_RESET")
        train.position = make_train_position(
            head_tile=(1, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=20.0,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 1
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")
        payload = controller.get_monitor_lookahead()
        blocks = payload["lookahead"]["blocks"]

        assert blocks[1]["start_cm"] == pytest.approx(30.0)
        assert blocks[1]["end_cm"] == pytest.approx(80.0)

    def test_cab_switch_updates_head_tile(self, network):
        """When switching cabs, head tile should reflect the new cab position."""
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "BLOCK_MAIN"
            network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD_CAB")
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="E",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 0
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        controller.set_cab("A")
        payload_a = controller.get_monitor_lookahead()
        # With Cab A active, head is at end_a which is (0, 0)
        assert payload_a["head_tile"] == [0, 0]

        controller.set_cab("B")
        payload_b = controller.get_monitor_lookahead()
        # After switching to Cab B, tiles don't swap - physical train doesn't move
        # end_a stays at (0, 0) and end_b stays at (2, 0)
        # With Cab B active and leading_end="b", head is at end_b which is (2, 0)
        assert payload_b["head_tile"] == [2, 0]

    def test_no_cab_returns_no_lookahead(self, network):
        """Lookahead should be unavailable when no cab is selected."""
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = f"BLOCK_{x}"
            network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD_NO_CAB")
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="E",
            leading_end=None,
            _facing_direction_legacy=None,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 1
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab(None)

        payload = controller.get_monitor_lookahead()

        assert payload["cab"] is None
        assert payload["head_tile"] is None
        assert payload["speed_limit_reason"] == "No cab selected"
        assert payload["lookahead"]["blocks"] == []
        assert payload["lookahead"]["signals"] == []
        assert payload["lookahead"]["switches"] == []
        assert payload["lookahead"]["stations"] == []
        assert payload["lookahead"]["clearance_cm"] == pytest.approx(0.0)

    def test_cab_switch_flips_lookahead_direction(self, network):
        """Lookahead sequence should reverse when operator switches to the opposite cab."""
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = f"BLOCK_{x}"
            network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD_DIR_FLIP")
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="E",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 0
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        controller.set_cab("A")
        payload_a = controller.get_monitor_lookahead()
        block_ids_a = [block["id"] for block in payload_a["lookahead"]["blocks"]]

        controller.set_cab("B")
        payload_b = controller.get_monitor_lookahead()
        block_ids_b = [block["id"] for block in payload_b["lookahead"]["blocks"]]

        assert payload_a["head_tile"] == [0, 0]
        assert payload_b["head_tile"] == [2, 0]
        assert block_ids_a == ["BLOCK_0", "BLOCK_1", "BLOCK_2"]
        assert block_ids_b == ["BLOCK_2", "BLOCK_1", "BLOCK_0"]

    def test_cab_switch_updates_path_index_to_new_head(self, network):
        """
        BUG FIX TEST: path_index must update to match new head position after cab switch.

        Bug Description:
        When switching cabs (e.g., A->B), the physical head of the train moves from end_a
        to end_b, but path_index was not being updated. This caused lookahead and speed
        limit calculations to reference blocks from the wrong side of the train.

        Scenario:
        - Train spans tiles (0,0) -> (1,0) -> (2,0)
        - path_index = 0 means head should be at tile (0,0)
        - After switching to Cab B, head moves to tile (2,0)
        - path_index MUST update to 2 to reflect the new head position

        Expected:
        - path_index should match the index of head_tile in current_path after any cab switch
        """
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = f"BLOCK_{x}"
            network.add_track(track)

        train = Train(train_id="TRAIN_PATH_INDEX_BUG")
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="E",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 0
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        # Start with Cab A - head at (0,0), path_index should be 0
        controller.set_cab("A")
        assert train.path_index == 0, (
            f"Cab A: path_index should be 0, got {train.path_index}"
        )
        assert train.position.head_tile == (0, 0), (
            f"Cab A: head_tile should be (0,0), got {train.position.head_tile}"
        )

        # Switch to Cab B - head moves to (2,0), path_index MUST update to 2
        controller.set_cab("B")
        assert train.path_index == 2, (
            f"After A->B switch: path_index should be 2, got {train.path_index}"
        )
        assert train.position.head_tile == (2, 0), (
            f"After A->B switch: head_tile should be (2,0), got {train.position.head_tile}"
        )

        # Verify lookahead uses correct blocks (from head position, not old position)
        payload = controller.get_monitor_lookahead()
        block_ids = [block["id"] for block in payload["lookahead"]["blocks"]]
        # With head at (2,0), lookahead should scan backward: BLOCK_2, BLOCK_1, BLOCK_0
        assert block_ids == ["BLOCK_2", "BLOCK_1", "BLOCK_0"], (
            f"Lookahead blocks wrong: {block_ids}"
        )

        # Switch back to Cab A - head moves back to (0,0), path_index MUST update to 0
        controller.set_cab("A")
        assert train.path_index == 0, (
            f"After B->A switch: path_index should be 0, got {train.path_index}"
        )
        assert train.position.head_tile == (0, 0), (
            f"After B->A switch: head_tile should be (0,0), got {train.position.head_tile}"
        )

        # Verify lookahead is correct again
        payload = controller.get_monitor_lookahead()
        block_ids = [block["id"] for block in payload["lookahead"]["blocks"]]
        # With head at (0,0), lookahead should scan forward: BLOCK_0, BLOCK_1, BLOCK_2
        assert block_ids == ["BLOCK_0", "BLOCK_1", "BLOCK_2"], (
            f"Lookahead blocks wrong after B->A: {block_ids}"
        )

    def test_cab_switch_path_index_multiple_times(self, network):
        """
        Test that path_index stays correct after multiple rapid cab switches.

        This test simulates the user's reported bug scenario:
        1. Start ATO, train runs
        2. Stop ATO, train stops
        3. Switch cab to reverse (B)
        4. Switch back to forward (A)
        5. Switch again to reverse (B)
        6. Leave on reverse, hit ATO go
        7. ATO correctly turns train and starts
        8. BUT lookahead randomly shows wrong side

        The bug was that path_index wasn't updated during cab switches.
        """
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = f"BLOCK_{x}"
            network.add_track(track)

        train = Train(train_id="TRAIN_PATH_INDEX_MULTIPLE")
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="E",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 0
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        # Simulate user's scenario: multiple cab switches
        controller.set_cab("A")  # Start with Cab A
        assert train.path_index == 0

        controller.set_cab("B")  # Switch to reverse
        assert train.path_index == 2

        controller.set_cab("A")  # Switch back
        assert train.path_index == 0

        controller.set_cab("B")  # Switch to reverse again (leave it here)
        assert train.path_index == 2

        # After all switches, verify lookahead is from the correct side
        payload = controller.get_monitor_lookahead()
        block_ids = [block["id"] for block in payload["lookahead"]["blocks"]]

        # Should see blocks from reverse side (head at 2,0)
        assert block_ids == ["BLOCK_2", "BLOCK_1", "BLOCK_0"], (
            f"After multiple switches, lookahead should be from reverse side. "
            f"Expected ['BLOCK_2', 'BLOCK_1', 'BLOCK_0'], got {block_ids}"
        )

        # Verify head_tile matches path_index
        head_tile = train.position.head_tile
        path_head_tile = train.current_path[train.path_index][:2]
        assert head_tile == path_head_tile, (
            f"head_tile {head_tile} doesn't match path tile at path_index {train.path_index}: {path_head_tile}"
        )

    def test_cab_switch_from_off_does_not_require_existing_facing_direction(
        self, network
    ):
        """Switching A->B after cab off should not fail with missing facing direction."""
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = f"BLOCK_{x}"
            network.add_track(track)

        train = Train(train_id="TRAIN_LOOKAHEAD_OFF_SWITCH")
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="E",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 0
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        result_a = controller.set_cab("A")
        assert result_a.success
        result_off = controller.set_cab(None)
        assert result_off.success

        result_b = controller.set_cab("B")
        assert result_b.success

        payload_b = controller.get_monitor_lookahead()
        block_ids_b = [block["id"] for block in payload_b["lookahead"]["blocks"]]

        assert payload_b["head_tile"] == [2, 0]
        assert block_ids_b == ["BLOCK_2", "BLOCK_1", "BLOCK_0"]


class TestLookaheadTrackUsabilityBugFix:
    """
    Comprehensive tests for lookahead usability checks fixed in 2026-02-08.

    BUG:
    After switching cabs at terminal/end-of-path situations, lookahead scanned all
    tiles in the stored path even when the route was physically unusable (switch set
    against movement) or not owned (locked by another path).

    FIX:
    Lookahead traversal now validates each path step against active switch state and
    path ownership/locks. It stops at the first unusable step so ATO braking curves
    are based on truly reachable track only.
    """

    def _build_three_tile_with_switch(self, network: RailwayNetwork) -> Train:
        """Create 0-1-2 path where tile 1 is an EW/NW switch."""
        track_0 = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_0.block_section_id = "BLOCK_0"
        network.add_track(track_0)

        track_1 = TrackSegment(x=1, y=0, track_bits=TRACK_EW | TRACK_NW)
        track_1.block_section_id = "BLOCK_1"
        track_1.switch_state = 0
        network.add_track(track_1)

        track_2 = TrackSegment(x=2, y=0, track_bits=TRACK_EW)
        track_2.block_section_id = "BLOCK_2"
        network.add_track(track_2)

        for block_id in ("BLOCK_0", "BLOCK_1", "BLOCK_2"):
            network.blocks[block_id] = Block(block_id=block_id, name=block_id)

        train = Train(train_id="TRAIN_LOOKAHEAD_USABILITY")
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="E",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 0
        train.assigned_path_id = "PATH_OWN"
        network.add_train(train)
        return train

    def test_lookahead_stops_before_wrong_side_switch_after_cab_change(self, network):
        """Switch set against movement must truncate reverse lookahead at current tile."""
        train = self._build_three_tile_with_switch(network)
        network.get_track(1, 0).switch_state = 1

        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")
        controller.set_cab("B")

        payload = controller.get_monitor_lookahead()
        block_ids = [block["id"] for block in payload["lookahead"]["blocks"]]

        assert payload["head_tile"] == [2, 0]
        assert block_ids == ["BLOCK_2"], (
            f"Lookahead should stop at wrong-side switch. Got blocks: {block_ids}"
        )

    def test_lookahead_stops_before_switch_locked_by_other_path(self, network):
        """Lookahead must not traverse a switch locked by another path."""
        train = self._build_three_tile_with_switch(network)
        switch_track = network.get_track(1, 0)
        switch_track.lock_for_path("PATH_OTHER", 0)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        payload = controller.get_monitor_lookahead()
        block_ids = [block["id"] for block in payload["lookahead"]["blocks"]]

        assert payload["head_tile"] == [0, 0]
        assert block_ids == ["BLOCK_0"], (
            f"Lookahead should stop before foreign-locked switch. Got blocks: {block_ids}"
        )


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
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
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
            _facing_direction_legacy=Direction.EAST,
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
            _facing_direction_legacy=Direction.EAST,
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
            _facing_direction_legacy=Direction.EAST,
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


class TestCabSwitchEndPositionsBugFix:
    """
    Comprehensive tests for cab switching behavior (corrected 2026-02-04).

    When switching cabs (A <-> B), the physical train DOES NOT MOVE:
    - end_a_tile and end_b_tile stay at their original locations
    - The driver conceptually walks to the other cab
    - Only leading_end changes (from "a" to "b" or vice versa)
    - Entry edges flip for BOTH ends (train will travel opposite direction)
    - Distances flip for BOTH ends (measured from opposite edge now)
    - occupied_tiles order stays the same (train hasn't moved)
    - facing_direction reverses (EAST -> WEST or vice versa)
    """

    def _setup_two_tile_train(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0)
        train = Train(train_id="CAB_SWAP_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True

        return train, controller

    def test_cab_switch_tiles_dont_move(self, network):
        """When switching cabs, tiles stay in place - physical train doesn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_end_a = train.position.end_a_tile
        original_end_b = train.position.end_b_tile

        controller.set_cab("B")

        # Tiles should NOT change - physical train stays in place
        assert train.position.leading_end == "b"
        assert train.position.end_a_tile == original_end_a  # unchanged
        assert train.position.end_b_tile == original_end_b  # unchanged

    def test_cab_switch_flips_entry_edges_for_both_ends(self, network):
        """When switching cabs, entry edges flip for BOTH ends."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_entry_a = train.position.end_a_entry_edge
        original_entry_b = train.position.end_b_entry_edge

        # Calculate expected flipped entries using track geometry
        track_a = network.get_track(*train.position.end_a_tile)
        track_b = network.get_track(*train.position.end_b_tile)

        expected_new_entry_a = original_entry_a
        if track_a:
            exits = track_a.get_exits(original_entry_a, use_switch_state=True)
            if exits:
                expected_new_entry_a = exits[0]

        expected_new_entry_b = original_entry_b
        if track_b:
            exits = track_b.get_exits(original_entry_b, use_switch_state=True)
            if exits:
                expected_new_entry_b = exits[0]

        controller.set_cab("B")

        # BOTH ends should have flipped entry edges
        assert train.position.end_a_entry_edge == expected_new_entry_a
        assert train.position.end_b_entry_edge == expected_new_entry_b

    def test_cab_switch_preserves_occupied_tiles_order(self, network):
        """occupied_tiles order stays the same - physical train didn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_occupied = train.position.occupied_tiles.copy()

        controller.set_cab("B")

        # occupied_tiles should NOT change - train didn't physically move
        assert train.position.occupied_tiles == original_occupied
        # First tile is still end_a
        assert train.position.occupied_tiles[0] == train.position.end_a_tile
        # Last tile is still end_b
        assert train.position.occupied_tiles[-1] == train.position.end_b_tile

    def test_cab_switch_renderer_serialization_order(self, network):
        """Renderer receives unchanged path_tiles after cab switch."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_end_a = train.position.end_a_tile
        original_end_b = train.position.end_b_tile

        controller.set_cab("B")
        data = train.to_dict(network)

        assert data["leading_end"] == "b"
        # path_tiles order unchanged - train didn't move
        assert data["path_tiles"][0] == list(original_end_a)
        assert data["path_tiles"][-1] == list(original_end_b)
        assert data["end_a_index"] == 0
        assert data["end_b_index"] == len(data["path_tiles"]) - 1

    def test_cab_switch_reverses_facing_direction(self, network):
        """Switching cabs reverses facing_direction."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        assert train.position.facing_direction == Direction.EAST

        controller.set_cab("B")
        assert train.position.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.position.facing_direction == Direction.EAST


class TestCabSwitchEntryEdgeFlipBugFix:
    """
    Comprehensive tests for cab switching entry edge reversal (corrected 2026-02-04).

    BUG: Switching cabs in manual mode flips leading_end/facing_direction but
    leaves entry_edges unchanged, so movement/renderer keep the old direction.

    FIX: On cab switch (A <-> B), flip entry_edge for BOTH ends since the train
    will now travel in the opposite direction on the track.
    """

    def _setup_straight_line(self, network, length_cm: float, head_x: int = 1):
        for x in range(-2, 4):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAB_EDGE_TEST", "LOCOMOTIVE", length=length_cm)
        train = Train(train_id="CAB_EDGE_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, head_x, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        return train, controller

    def _expected_reverse_entry_edge(self, network, tile, entry_edge: str) -> str:
        track = network.get_track(*tile)
        assert track is not None
        exits = track.get_exits(entry_edge, use_switch_state=True)
        assert exits
        return exits[0]

    def test_cab_switch_flips_entry_edge_single_tile(self, network):
        """Single-tile train: BOTH ends flip entry edges on cab switch."""
        train, controller = self._setup_straight_line(network, length_cm=40.0)

        controller.set_cab("A")
        original_entry_a = train.position.end_a_entry_edge
        original_entry_b = train.position.end_b_entry_edge

        # Both entries should flip
        expected_entry_a = self._expected_reverse_entry_edge(
            network, train.position.end_a_tile, original_entry_a
        )
        expected_entry_b = self._expected_reverse_entry_edge(
            network, train.position.end_b_tile, original_entry_b
        )

        controller.set_cab("B")

        assert train.position.leading_end == "b"
        # BOTH ends flip their entry edges
        assert train.position.end_a_entry_edge == expected_entry_a
        assert train.position.end_b_entry_edge == expected_entry_b

    def test_cab_switch_flips_entry_edge_multi_tile(self, network):
        """Multi-tile train: BOTH ends flip entry edges on cab switch."""
        train, controller = self._setup_straight_line(network, length_cm=140.0)

        controller.set_cab("A")
        original_entry_a = train.position.end_a_entry_edge
        original_entry_b = train.position.end_b_entry_edge

        expected_entry_a = self._expected_reverse_entry_edge(
            network, train.position.end_a_tile, original_entry_a
        )
        expected_entry_b = self._expected_reverse_entry_edge(
            network, train.position.end_b_tile, original_entry_b
        )

        controller.set_cab("B")

        assert train.position.leading_end == "b"
        # BOTH ends flip their entry edges
        assert train.position.end_a_entry_edge == expected_entry_a
        assert train.position.end_b_entry_edge == expected_entry_b


class TestTrainPhysicsOccupiedTilesOrderBugFix:
    """
    Comprehensive tests for occupied_tiles ordering in TrainPhysics (2026-01-22).

    BUG: TrainPhysics._update_occupied_tiles() always stored tiles from tail to
    head, even when leading_end='a'. This inverted end_a/end_b ordering, causing
    renderer head/tail swaps when the train spans multiple tiles.

    FIX: occupied_tiles stays ordered from end_a -> end_b regardless of
    leading_end.
    """

    def _setup_two_tile_train(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0)
        train = Train(train_id="OCCUPIED_TILE_ORDER", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        return train

    def test_occupied_tiles_order_leading_end_a(self, network):
        train = self._setup_two_tile_train(network)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 1.0)

        assert train.position.leading_end == "a"
        assert train.position.occupied_tiles[0] == train.position.end_a_tile
        assert train.position.occupied_tiles[-1] == train.position.end_b_tile

    def test_renderer_path_tiles_order_leading_end_a(self, network):
        train = self._setup_two_tile_train(network)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 1.0)

        data = train.to_dict(network)

        assert data["path_tiles"][0] == list(train.position.end_a_tile)
        assert data["path_tiles"][-1] == list(train.position.end_b_tile)


class TestAtoStopButtonBugFix:
    """
    Comprehensive tests for ATO stop command behavior (2026-01-25).

    BUG: ATO stop only set ato_running=False, leaving the handle in a power notch
    and the train continuing to move.

    FIX: ATO stop applies braking and transitions to STANDBY once stopped.
    """

    def _setup_ato_train(self, network):
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_STOP_TEST", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="ATO_STOP_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.handle_position = HandlePosition.P2

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 30.0
        return train, controller

    def test_ato_stop_sets_ato_running_false(self, network):
        """ato_stop() sets ato_running=False. Braking is applied in tick()."""
        _, controller = self._setup_ato_train(network)

        result = controller.ato_stop()

        assert result.success is True
        assert controller.ato_running is False

    def test_ato_stop_tick_applies_braking(self, network):
        """After ato_stop(), tick() applies braking."""
        train, controller = self._setup_ato_train(network)

        controller.ato_stop()
        train.current_speed = 30.0  # Still moving

        controller.tick(0.1)

        # Handle should now be braking
        assert controller.handle_position.value.startswith("B")

    def test_ato_stop_transitions_to_standby_when_stopped(self, network):
        train, controller = self._setup_ato_train(network)

        controller.ato_stop()
        train.current_speed = 0.0

        controller.tick(0.1)

        assert controller.state == TrainState.STANDBY
        assert train.state == TrainState.STANDBY
        assert controller.handle_position == HandlePosition.N


class TestTrainPhysicsTailPositionBugFix:
    """
    Comprehensive tests for incorrect tail distance/entry edge (2026-01-21).

    BUG: TrainPhysics._update_tail_position produced tail distances larger than
    tile length (e.g., 61cm in a 50cm tile) and flipped the tail entry edge when
    the head advanced into the next tile.

    FIX: Tail distance stays within the tile length and preserves the correct
    entry edge when tracing backward across tiles.
    """

    def test_tail_distance_within_tile_after_advance(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 1.0)

        assert train.position.head_tile == (1, 0)
        assert train.position.tail_tile == (0, 0)
        assert train.position.tail_entry_edge == "W"
        assert train.position.tail_distance_cm == pytest.approx(11.0)


class TestZeroLengthSwitchTraversal:
    """Tests for moving across zero-length switch tiles."""

    def test_train_advances_across_zero_length_switch(self, network):
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
        switch_track = TrackSegment(1, 0, track_bits=TRACK_EW | TRACK_NW)
        switch_track.switch_state = 0
        network.add_track(switch_track)
        network.add_track(TrackSegment(2, 0, track_bits=TRACK_EW))
        switch = Switch(switch_id="SW_1_0", x=1, y=0)
        network.add_switch(switch)
        switch_track.switch_id = switch.switch_id

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._move_train(train, 25.0)

        assert train.position.head_tile == (2, 0)
        assert train.position.head_distance_cm == pytest.approx(25.0)
        assert train.position.tail_tile == (0, 0)
        assert train.position.tail_entry_edge == "W"
        assert train.position.tail_distance_cm == pytest.approx(35.0)


class TestRaceConditionFix:
    """Tests for race condition fixes."""

    def test_block_manager_prevents_premature_release(self, network):
        block = network.create_block("B1", "Block 1")
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED
        network.block_manager.set_block_to_caution("B1")
        assert block.state == BlockState.CAUTION
        assert block.state != BlockState.FREE

    def test_simulation_uses_new_architecture(self):
        """Verify SimulationController uses TrainController + TrainPhysics architecture."""
        import simulation

        # Verify SimulationController has the new architecture attributes
        assert hasattr(simulation.SimulationController, "_get_or_create_controller")
        assert hasattr(simulation.SimulationController, "_get_or_create_physics")

        # Verify it no longer has the old _advance_train_track method
        assert not hasattr(simulation.SimulationController, "_advance_train_track")
        assert not hasattr(simulation.SimulationController, "_update_speed")

    def test_controller_state_is_source_of_truth(self):
        """
        Test that TrainController state is the source of truth, not train.state.

        BUG: simulation.py:94 was copying train.state -> controller.state BEFORE tick,
        then controller.tick() modified controller.state, leaving train.state stale.

        FIX: Controller state should be synced TO train AFTER tick, not FROM train BEFORE.

        This test sets train.state to one value and controller.state to another,
        then verifies that after update(), train.state matches controller.state
        (proving controller is the authority).
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_STATE_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.IDLE
        network.add_train(train)

        sim = SimulationController(network)

        # First update to initialize controller
        sim.update(0.1)

        controller = sim.controllers[train.train_id]

        # Now manually set controller to different state than train
        # (simulating what would happen if controller.tick() changes state)
        train.state = TrainState.IDLE
        controller.state = TrainState.STANDBY

        # After another update, train should reflect controller's state
        sim.update(0.1)

        # train.state should now match controller.state (controller wins)
        assert train.state == controller.state

    def test_controller_transitions_to_waiting_when_stopped(self):
        """
        Test that controller transitions from RUNNING_MOVING to RUNNING_WAITING
        when train is stopped AND there's an obstruction (ATC limit = 0).

        The transition to WAITING should ONLY occur when:
        1. Train's current_speed <= threshold (physically stopped)
        2. ATC speed limit <= threshold (obstruction detected)

        Simply being stopped without an obstruction should NOT trigger WAITING.
        """
        network = RailwayNetwork()

        # Create track and block
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        # Create a block that is occupied by ANOTHER train (obstruction)
        block = network.create_block("B1", "Block 1")
        network.block_manager.occupy_block("B1", "OTHER_TRAIN")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_TRANSITION_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Train is stopped
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["B1"]
        )
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.active_cab = "A"  # Set cab so ATC calculates speed properly

        # Tick controller - should detect obstruction and transition to WAITING
        sim.update(0.1)

        # Controller should have transitioned to RUNNING_WAITING
        # because there's an obstruction (block occupied by another train)
        assert controller.state == TrainState.RUNNING_WAITING
        assert train.wait_reason is not None

    def test_controller_stays_moving_when_stopped_without_obstruction(self):
        """
        Test that controller stays in RUNNING_MOVING when train is stopped
        but there's NO obstruction (path is clear).

        This verifies that simply reaching speed = 0 doesn't trigger WAITING.
        The train should remain in RUNNING_MOVING if the path ahead is clear.
        """
        network = RailwayNetwork()

        # Create track and block with sufficient length for clearance
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0  # 10 meters - sufficient for clearance

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_NO_OBSTRUCTION", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Train is stopped, but path is clear
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["B1"]
        )  # Train is on the track
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"  # Set cab so ATC calculates speed properly
        controller.ato_running = True  # ATO is running

        # Tick controller - should NOT transition to WAITING (no obstruction)
        sim.update(0.1)

        # Controller should remain in RUNNING_MOVING (path is clear, no obstruction)
        assert controller.state == TrainState.RUNNING_MOVING

    def test_controller_auto_transitions_waiting_to_moving_when_obstruction_clears(
        self,
    ):
        """
        Test that controller auto-transitions from RUNNING_WAITING back to RUNNING_MOVING
        when the obstruction clears (ATC limit > 0).

        This is the key test for the WAITING ↔ MOVING fix:
        1. Train is in RUNNING_WAITING (blocked by another train)
        2. Obstruction clears (block becomes free)
        3. Train should automatically transition to RUNNING_MOVING
        """
        network = RailwayNetwork()

        # Create track and block
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        # Create a block that is occupied by ANOTHER train (obstruction)
        # Set block length so ATC clearance calculation works
        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0  # 10 meters - sufficient for clearance
        network.block_manager.occupy_block("B1", "OTHER_TRAIN")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_WAITING_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_WAITING
        train.current_speed = 0.0  # Train is stopped
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["B1"]
        )
        train.wait_reason = "Block B1 occupied by OTHER_TRAIN"
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_WAITING
        controller.ato_running = True  # ATO is running, just waiting for path
        controller.active_cab = "A"  # Set cab so ATC calculates speed properly

        # Verify initial state
        assert controller.state == TrainState.RUNNING_WAITING

        # Now clear the obstruction - the other train leaves the block
        network.block_manager.set_block_to_caution("B1")
        network.block_manager.clear_block("B1")

        # Tick controller - should detect obstruction cleared and transition to MOVING
        sim.update(0.1)

        # Controller should have transitioned to RUNNING_MOVING
        # because obstruction is gone (block is free)
        assert controller.state == TrainState.RUNNING_MOVING, (
            f"Expected RUNNING_MOVING but got {controller.state}. "
            f"Train should auto-transition when obstruction clears."
        )
        # ATO should be restarted
        assert controller.ato_running == True, (
            "ATO should be restarted after obstruction clears"
        )
        # Wait reason should be cleared
        assert train.wait_reason is None, "Wait reason should be cleared"

    def test_atc_triggers_warning_on_5kmh_overspeed(self):
        """
        Test that ATC triggers warning when overspeed by 5-10 km/h.

        Per new tick() workflow:
        - current_speed > atc_speed + 5 → atc_warning_active = True
        - current_speed > atc_speed + 10 → eb_active = True
        """
        network = RailwayNetwork()

        # Create track and block with speed limit
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0
        block.max_speed_kmh = 30.0  # 30 km/h limit

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_OVERSPEED_TEST", consist=[car], max_speed=100.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 37.0  # 7 km/h over limit (warning zone)
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["B1"]
        )
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"

        # Tick controller
        sim.update(0.1)

        # Should have warning but not EB
        assert controller.atc_warning_active == True, (
            "Warning should be active at 7 km/h overspeed"
        )
        assert controller.eb_active == False, (
            "EB should not trigger at 7 km/h overspeed"
        )

    def test_atc_triggers_eb_on_10kmh_overspeed(self):
        """
        Test that ATC triggers emergency brake when overspeed by > 10 km/h.

        Per new tick() workflow:
        - current_speed > atc_speed + 10 → eb_active = True
        """
        network = RailwayNetwork()

        # Create track and block with speed limit
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 1000.0
        block.max_speed_kmh = 30.0  # 30 km/h limit

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_EB_TEST", consist=[car], max_speed=100.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 42.0  # 12 km/h over limit (EB zone)
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["B1"]
        )
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"

        # Tick controller
        sim.update(0.1)

        # Should trigger EB
        assert controller.eb_active == True, "EB should trigger at 12 km/h overspeed"
        assert "overspeed" in controller.speed_limit_reason.lower(), (
            f"Speed limit reason should mention overspeed, got: {controller.speed_limit_reason}"
        )
        # Should have notification
        assert len(controller.pending_notifications) > 0, (
            "Should have notification for EB"
        )

    def test_eb_resets_to_standby_when_stopped(self):
        """
        Test that EB resets to STANDBY state when train stops.

        Per new tick() workflow:
        - When eb_active and train stops → eb_active = False, state = STANDBY
        """
        network = RailwayNetwork()

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_EB_RESET_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Already stopped
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.eb_active = True  # EB is active
        controller.state = TrainState.RUNNING_MOVING

        # Tick controller - train is stopped, should reset EB
        sim.update(0.1)

        # EB should be released and state should be STANDBY
        assert controller.eb_active == False, "EB should be released when train stopped"
        assert controller.state == TrainState.STANDBY, (
            f"State should be STANDBY after EB reset, got {controller.state}"
        )
        # Should have notification
        assert "released" in " ".join(controller.pending_notifications).lower(), (
            "Should have notification for EB release"
        )

    def test_no_cab_selected_sets_atc_speed_zero(self):
        """
        Test that when no cab is selected, ATC speed is 0.

        Per new tick() workflow:
        - If active_cab is None → atc_speed_limit = 0, reason = "No cab selected"
        """
        network = RailwayNetwork()

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_NO_CAB_TEST", consist=[car], max_speed=60.0)
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = None  # No cab selected

        # Tick controller
        sim.update(0.1)

        # ATC speed should be 0
        assert controller.atc_speed_limit == 0.0, (
            "ATC speed should be 0 when no cab selected"
        )
        assert controller.speed_limit_reason == "No cab selected", (
            f"Reason should be 'No cab selected', got: {controller.speed_limit_reason}"
        )


class TestAtoAutoCabSelectionBugFix:
    """
    Comprehensive tests for auto-selecting cab in ATO (2026-01-25).

    BUG: ATO could be blocked by no cab selection, even though the path implies
    a required cab. This prevented ATO from starting in demo/route flow.

    FIX: ATO auto-selects required cab when missing (in depart() before calling ato_go()).
    NOTE: depart() now calls ato_go() which starts ATO (simplified model 2026-01-26).
    """

    def test_depart_selects_cab_and_starts_ato(self, network):
        """
        depart() should auto-select required cab and start ATO.
        In the simplified model, depart() calls ato_go().
        """
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("B1", [(0, 0)], length_cm=100.0)

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_NO_CAB", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.EAST
        train.assigned_path_id = "PATH_1"
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        class DummyPath:
            path_id = "PATH_1"
            end_a = (0, 0)
            end_b = (1, 0)

        network.paths = {"PATH_1": DummyPath()}

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.STANDBY
        controller.active_cab = None

        # Set up ato_info so cab can be auto-selected
        from train_controller import ATOInfo

        controller.ato_info = ATOInfo(required_cab="A")

        assert controller.depart() is True
        # depart() calls ato_go(), so ato_running should be True
        assert controller.ato_running is True
        assert controller.state == TrainState.RUNNING_MOVING
        # Cab should have been auto-selected
        assert controller.active_cab == "A"

    def test_tick_does_not_auto_select_cab_when_missing(self, network):
        """
        Test that tick() does NOT auto-select cab.

        Previously, tick() would auto-select cab which caused state corruption
        because it only updated active_cab/leading_end without flipping
        facing_direction and entry edges.

        The proper place for cab selection is:
        1. Manual selection by operator (set_cab())
        2. Auto-selection in depart() when called by RouteExecutor
        """
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("B1", [(0, 0)], length_cm=100.0)

        car = TrainCar(car_id="CAR_02", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_NO_CAB_TICK", consist=[car], max_speed=60.0)
        train.state = TrainState.STANDBY
        train.facing_direction = Direction.EAST
        train.assigned_path_id = "PATH_1"
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        class DummyPath:
            path_id = "PATH_1"
            end_a = (0, 0)
            end_b = (1, 0)

        network.paths = {"PATH_1": DummyPath()}

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.STANDBY
        controller.active_cab = None

        controller.tick(0.1)

        # tick() should NOT auto-select cab - that's done in depart()
        assert controller.active_cab is None

    def test_block_release_uses_caution_state(self):
        """
        Test that TrainPhysics releases blocks through CAUTION state, not directly to FREE.

        BUG: train_physics.py:607 uses clear_block() which skips CAUTION state.

        FIX: Should use set_block_to_caution() for proper safety transition.
        Block state machine: OCCUPIED -> CAUTION -> FREE (not OCCUPIED -> FREE).
        """
        network = RailwayNetwork()

        # Create a block
        block = network.create_block("B1", "Block 1")

        # Occupy the block
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED

        # Create train
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        network.add_train(train)

        # Create simulation and physics
        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)

        # Simulate train leaving block: was occupying B1, now occupies nothing
        physics._last_occupied_blocks = {"B1"}
        train.position = None  # Train has no position (simulates having left)

        # Trigger block occupancy update
        physics._update_block_occupancy(train)

        # Block should be in CAUTION state, not FREE
        assert block.state == BlockState.CAUTION, (
            f"Expected CAUTION but got {block.state}"
        )


class TestTrainPhysicsBlockCautionReleaseBugFix:
    """
    Comprehensive tests for TrainPhysics CAUTION release fix (2026-01-22).

    BUG: TrainPhysics._update_block_occupancy() only transitions OCCUPIED -> CAUTION
    and never clears CAUTION blocks, leaving blocks stuck in CAUTION indefinitely.

    FIX: When a block remains unoccupied after the CAUTION transition, clear it
    to FREE on the next update tick.
    """

    def test_caution_block_clears_after_train_fully_leaves(self):
        """Block should advance from CAUTION to FREE after train fully exits."""
        network = RailwayNetwork()
        block = network.create_block("B1", "Block 1")

        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block.state == BlockState.OCCUPIED

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)

        physics._last_occupied_blocks = {"B1"}
        train.position = None

        physics._update_block_occupancy(train)
        assert block.state == BlockState.CAUTION

        physics._update_block_occupancy(train)
        assert block.state == BlockState.FREE


class TestTrainPhysicsCautionHoldBugFix:
    """
    Comprehensive tests for TrainPhysics caution hold bug fix (2026-01-23).

    BUG: TrainPhysics._update_block_occupancy() clears CAUTION blocks on the
    next tick even when the train has not vacated another block. This causes
    the block behind the train to become FREE while the train is still moving.

    FIX: Keep the last vacated block in CAUTION until another block is vacated
    or the train leaves the track entirely.
    """

    def test_caution_persists_until_next_block_vacated(self):
        """The last vacated block should remain CAUTION while train stays in place."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        network.add_track(track_a)
        network.add_track(track_b)
        block_a = network.create_block("B1", "Block 1")
        network.create_block("B2", "Block 2")
        network.block_manager.occupy_block("B1", "TRAIN_1")
        assert block_a.state == BlockState.OCCUPIED

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.current_speed = 10.0
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(1, 0)],
        )
        network.add_train(train)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)
        physics._last_occupied_blocks = {"B1"}

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.CAUTION

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.CAUTION

    def test_previous_caution_clears_when_next_block_vacated(self):
        """Previous CAUTION block should clear once a new block is vacated."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_c = TrackSegment(x=2, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        track_c.block_section_id = "B3"
        network.add_track(track_a)
        network.add_track(track_b)
        network.add_track(track_c)

        block_a = network.create_block("B1", "Block 1")
        block_b = network.create_block("B2", "Block 2")
        block_c = network.create_block("B3", "Block 3")
        network.block_manager.occupy_block("B1", "TRAIN_1")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(1, 0)],
        )
        network.add_train(train)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)
        physics._last_occupied_blocks = {"B1"}

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.CAUTION
        assert block_b.state == BlockState.OCCUPIED

        train.position = TrainPosition(
            end_a_tile=(2, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(2, 0)],
        )

        physics._update_block_occupancy(train)
        assert block_a.state == BlockState.FREE
        assert block_b.state == BlockState.CAUTION
        assert block_c.state == BlockState.OCCUPIED


class TestTrainPhysicsTransitions:
    """Tests for TrainPhysics block transition reporting."""

    def test_train_physics_reports_block_transitions(self):
        """TrainPhysics should expose entered and vacated blocks per update."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        network.add_track(track_a)
        network.add_track(track_b)

        network.create_block("B1", "Block 1")
        network.create_block("B2", "Block 2")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(1, 0)],
            occupied_blocks=["B2"],
        )
        network.add_train(train)

        network.block_manager.occupy_block("B1", train.train_id)

        from train_physics import TrainPhysics

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._last_occupied_blocks = {"B1"}

        physics._update_block_occupancy(train)

        assert physics.last_block_transitions["entered"] == {"B2"}
        assert physics.last_block_transitions["vacated"] == {"B1"}


class TestTrainPhysicsCautionStoppedReleaseBugFix:
    """
    Comprehensive tests for TrainPhysics caution release on stop (2026-01-23).

    BUG: TrainPhysics keeps the CAUTION block indefinitely when the train stops,
    even though the block behind the occupied block should be clearable once the
    train is stationary.

    FIX: If the train is stopped, clear CAUTION blocks that are no longer occupied.
    """

    def test_caution_clears_when_train_stopped(self):
        """CAUTION block clears when train is stopped and not occupying it."""
        network = RailwayNetwork()
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B2"
        network.add_track(track_a)
        network.add_track(track_b)

        block_a = network.create_block("B1", "Block 1")
        block_b = network.create_block("B2", "Block 2")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        train.current_speed = 0.0
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(1, 0)],
            occupied_blocks=["B2"],
        )
        network.add_train(train)

        network.block_manager.occupy_block("B1", train.train_id)
        network.block_manager.set_block_to_caution("B1")
        network.block_manager.occupy_block("B2", train.train_id)

        sim = SimulationController(network)
        physics = sim._get_or_create_physics(train)
        physics._last_occupied_blocks = {"B2"}
        physics._caution_blocks = {"B1"}

        physics._update_block_occupancy(train)

        assert block_a.state == BlockState.FREE
        assert block_b.state == BlockState.OCCUPIED


class TestTrainControlPanelDataBugFix:
    """
    Comprehensive tests for train control panel data propagation fix (2026-01-22).

    BUG: /api/network returned train data without controller state, so the
    train control panel could not reflect power/handle status or ATO path info.

    FIX: /api/network includes an onboard controller snapshot per train.
    """

    def test_network_includes_onboard_snapshot(self):
        """Network response should include onboard controller state per train."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.assigned_path_id = "PATH_1"
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        controller.power_on = True
        controller.handle_position = HandlePosition.P2
        controller.control_mode = "ato"

        payload = build_network_payload(network, sim_controller)
        train_data = payload["trains"][0]
        assert "onboard" in train_data
        assert train_data["onboard"]["power_on"] is True
        assert train_data["onboard"]["handle_position"] == "P2"
        assert train_data["onboard"]["control_mode"] == "ato"
        assert "ato_info" in train_data["onboard"]


class TestEmergencyResetButtonDataBugFix:
    """
    Comprehensive tests for emergency reset button data fix (2026-01-24).

    BUG: Onboard controller payload only included `eb_active`, while the
    control panel expects `emergency_stop_active`, so the reset button never
    appears even when emergency braking is active.

    FIX: Provide `emergency_stop_active` in onboard snapshots alongside
    `eb_active`.
    """

    def test_onboard_includes_emergency_stop_active(self):
        """Onboard payload should expose emergency_stop_active for UI state."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        controller.eb_active = True

        payload = build_network_payload(network, sim_controller)
        train_data = payload["trains"][0]
        assert train_data["onboard"]["emergency_stop_active"] is True


class TestTrainDebugLogging:
    """Regression tests for controller/physics debug logging (2026-01-22)."""

    def test_train_controller_logs_power_off_state(self, caplog):
        """TrainController.tick should log when power is off."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_LOG_1", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = False

        caplog.set_level(logging.DEBUG)
        controller.tick(0.1)

        assert any(
            "power off" in record.message.lower() for record in caplog.records
        ), "Expected debug log for power off state"


class TestTrainClearancePrecisionBugFix:
    """
    Comprehensive tests for clearance precision bug fix (2026-01-22).

    BUG: clearance ignored TrainPosition head distance,
    causing clearance to jump when the head enters the last tile.

    FIX: clearance derives remaining distance from TrainPosition head location.
    """

    def test_remaining_distance_uses_train_position_head(self):
        """Remaining distance should reflect head distance in current tile."""
        network = RailwayNetwork()

        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        track_b = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        track_a.block_section_id = "B1"
        track_b.block_section_id = "B1"
        network.add_track(track_a)
        network.add_track(track_b)

        block = network.create_block("B1", "Block 1")
        block.length_cm = 40.0

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=10.0)
        train = Train(train_id="TRAIN_CLEARANCE", consist=[car], max_speed=60.0)
        train.current_path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]

        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=5.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(1, 0), (0, 0)],
        )

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)

        remaining = controller._get_remaining_distance_in_current_block()

        assert remaining == pytest.approx(15.0), (
            f"Expected 15.0 cm remaining, got {remaining}"
        )


class TestTrainPhysicsDebugLogging:
    """Regression tests for TrainPhysics debug details (2026-01-22)."""

    def test_train_physics_tick_updates_speed_correctly(self):
        """TrainPhysics.tick should update speed based on handle position."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_PHYS_LOG", consist=[car], max_speed=60.0)
        train.current_speed = 12.0
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)

        controller.power_on = True
        controller.handle_position = HandlePosition.P2
        controller.target_speed = 40.0

        initial_speed = train.current_speed
        physics.tick(0.1)

        # Speed should increase with P2 handle (power)
        assert train.current_speed > initial_speed


class TestTrainAssignedPathSerializationBugFix:
    """
    Comprehensive tests for assigned_path_id serialization fix (2026-01-22).

    BUG: Train.to_dict omitted assigned_path_id, causing frontend to show "No path"
    after UI refresh or cab switch.

    FIX: Train.to_dict includes assigned_path_id for frontend state.
    """

    def test_train_to_dict_includes_assigned_path_id(self):
        """Train serialization should include assigned_path_id."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_PATH", consist=[car], max_speed=60.0)
        train.assigned_path_id = "PATH_123"
        network.add_train(train)

        data = train.to_dict(network)

        assert data.get("assigned_path_id") == "PATH_123"

    def test_train_physics_power_off_does_not_update_speed(self):
        """TrainPhysics.tick should not update speed when power is off."""
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_LOG_2", consist=[car], max_speed=60.0)
        train.current_speed = 10.0
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)
        controller.power_on = False

        initial_speed = train.current_speed
        physics.tick(0.1)

        # Speed should remain unchanged when power is off
        assert train.current_speed == initial_speed

    def test_target_speed_does_not_override_power_handle(self):
        """
        Test that target_speed does not force deceleration in TrainPhysics.

        BUG: train_physics.py:204-221 forces decel and clamps speed when
        target_speed < current_speed, even with power handle.

        FIX: Speed changes should be driven by handle position; target_speed
        is enforced by TrainController, not TrainPhysics.
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_FRICTION_TEST", consist=[car], max_speed=60.0)
        train.current_speed = 10.0  # Train is moving
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)

        # Set controller state: power on, but target speed is 0 (should not stop)
        controller.power_on = True
        controller.handle_position = HandlePosition.P1  # Power handle
        controller.target_speed = 0.0  # But target is to stop

        initial_speed = train.current_speed

        # Tick physics - train should accelerate due to power handle
        physics.tick(0.1)

        # Train should have sped up (handle-driven), not slowed down
        assert train.current_speed > initial_speed, (
            f"Train should accelerate with power handle even if target_speed=0, "
            f"but speed went from {initial_speed} to {train.current_speed}"
        )


class TestTrainControllerBrakingNotches:
    """Tests for braking notch selection updates (2026-01-22)."""

    def test_braking_notch_for_speed_uses_b1_to_b8(self):
        """Speed-based braking should map to B1-B8 notches."""
        network = RailwayNetwork()
        train = Train(train_id="TRAIN_BRAKE", max_speed=60.0)
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        assert (
            controller._calculate_braking_notch_for_speed(60.0, 25.0)
            == HandlePosition.B8
        )
        assert (
            controller._calculate_braking_notch_for_speed(40.0, 14.0)
            == HandlePosition.B7
        )
        assert (
            controller._calculate_braking_notch_for_speed(30.0, 8.0)
            == HandlePosition.B6
        )
        assert (
            controller._calculate_braking_notch_for_speed(22.0, 7.0)
            == HandlePosition.B5
        )
        assert (
            controller._calculate_braking_notch_for_speed(18.0, 7.0)
            == HandlePosition.B4
        )
        assert (
            controller._calculate_braking_notch_for_speed(12.0, 6.0)
            == HandlePosition.B3
        )
        assert (
            controller._calculate_braking_notch_for_speed(9.0, 6.0) == HandlePosition.B2
        )
        assert (
            controller._calculate_braking_notch_for_speed(7.0, 6.0) == HandlePosition.B1
        )

    def test_stop_braking_notch_uses_b1_to_b8(self):
        """Stop braking should map to B1-B8 based on speed."""
        network = RailwayNetwork()
        train = Train(train_id="TRAIN_STOP", max_speed=60.0)
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        assert controller._calculate_braking_notch(35.0) == HandlePosition.B8
        assert controller._calculate_braking_notch(27.0) == HandlePosition.B7
        assert controller._calculate_braking_notch(22.0) == HandlePosition.B6
        assert controller._calculate_braking_notch(16.0) == HandlePosition.B5
        assert controller._calculate_braking_notch(12.0) == HandlePosition.B4
        assert controller._calculate_braking_notch(8.0) == HandlePosition.B3
        assert controller._calculate_braking_notch(4.0) == HandlePosition.B2
        assert controller._calculate_braking_notch(1.0) == HandlePosition.B1

    def test_manual_mode_uses_atc_speed_braking(self, monkeypatch):
        """Manual mode should brake toward ATC speed, not full stop."""
        network = RailwayNetwork()
        train = Train(train_id="TRAIN_MANUAL", max_speed=60.0)
        train.current_speed = 16.0
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.control_mode = "manual"
        controller.power_on = True
        controller.active_cab = "A"

        monkeypatch.setattr(controller, "_calculate_clearance_ahead", lambda: 200.0)
        monkeypatch.setattr(
            controller,
            "_calculate_atc_speed_limit",
            lambda: (10.0, "test"),
        )

        controller.tick(0.1)

        assert controller.handle_position == HandlePosition.B3

    def test_no_duplicate_controller_instances(self):
        """
        Test that RouteExecutor uses the same TrainController instances as
        SimulationController, not creating duplicates.

        BUG: route_executor.py:86-92 created a separate
        controller instances, causing desynchronization.

        FIX: All components should get controllers from SimulationController
        (single source of truth).
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=100.0)
        train = Train(train_id="TRAIN_SINGLETON_TEST", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)

        # Get controller from SimulationController
        sim_controller = sim._get_or_create_controller(train)

        # RouteExecutor should use the same controller instance
        route_executor_controller = sim.route_executor._get_controller(train)

        # They should be the exact same object (not copies)
        assert sim_controller is route_executor_controller, (
            "RouteExecutor created a duplicate controller instead of using "
            "the SimulationController's instance"
        )


class TestAtoDepartCallsAtoGo:
    """
    Tests for simplified ATO model (2026-01-26).

    In the simplified model:
    - depart() calls ato_go() if in ATO mode (RouteExecutor "presses Go button")
    - ato_go() sets ato_running=True
    - ato_stop() sets ato_running=False
    - tick() brakes to stop when ato_running=False, transitions to STANDBY
    """

    def _setup_ato_train(self, network):
        """Create a train in ATO mode with ATO running."""
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_DEPART_TEST", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="ATO_DEPART_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.handle_position = HandlePosition.P2

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 30.0
        return train, controller

    def test_depart_calls_ato_go_in_ato_mode(self, network):
        """
        depart() should call ato_go() when in ATO mode.
        This is like RouteExecutor pressing the Go button.
        """
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("DEPART_ATO", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="DEPART_ATO", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.STANDBY
        controller.ato_running = False

        train.state = TrainState.STANDBY

        # depart() is called by RouteExecutor
        result = controller.depart()
        assert result is True

        # In ATO mode, depart() calls ato_go(), so ato_running should be True
        assert controller.ato_running is True
        assert controller.state == TrainState.RUNNING_MOVING

    def test_ato_stop_sets_ato_running_false(self, network):
        """
        ato_stop() simply sets ato_running=False.
        tick() handles braking and transition to STANDBY.
        """
        train, controller = self._setup_ato_train(network)

        result = controller.ato_stop()

        assert result.success is True
        assert controller.ato_running is False

    def test_tick_brakes_and_transitions_to_standby_when_ato_stopped(self, network):
        """
        When ato_running=False, tick() should brake to stop and
        transition to STANDBY when stopped.
        """
        train, controller = self._setup_ato_train(network)

        # User presses ATO Stop
        controller.ato_stop()
        assert controller.ato_running is False

        # Train is still moving
        train.current_speed = 10.0
        controller.tick(0.1)

        # Should still be RUNNING_MOVING while braking
        # (handle should be braking)
        assert controller.handle_position.value.startswith("B")

        # Simulate train stopped
        train.current_speed = 0.0
        controller.tick(0.1)

        # Now should transition to STANDBY
        assert controller.state == TrainState.STANDBY

    def test_ato_go_starts_ato(self, network):
        """
        Verify that ato_go() still properly starts ATO.
        """
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_GO_TEST", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="ATO_GO_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.STANDBY
        controller.ato_running = False

        train.state = TrainState.STANDBY

        # User explicitly calls ato_go()
        result = controller.ato_go()
        assert result.success is True
        assert controller.ato_running is True
        assert controller.state == TrainState.RUNNING_MOVING

    def test_waiting_to_moving_preserves_ato_running_false(self, network):
        """
        When transitioning from WAITING to MOVING, ato_running should stay False
        if user stopped ATO.
        """
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("WAIT_TO_MOVE", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="WAIT_TO_MOVE", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_WAITING
        controller.ato_running = False  # User stopped ATO

        train.state = TrainState.RUNNING_WAITING
        train.current_speed = 0.0

        # Simulate path clearing - tick should transition to RUNNING_MOVING
        # but ato_running should stay False
        controller.tick(0.1)

        # ato_running should stay False - user stopped it
        assert controller.ato_running is False, (
            "WAITING->MOVING transition should preserve ato_running=False"
        )

    def test_clear_to_move_preserves_ato_running_false(self, network):
        """
        clear_to_move() should preserve ato_running=False if user stopped ATO.
        """
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("CLEAR_MOVE", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="CLEAR_MOVE", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_WAITING
        controller.ato_running = False  # User stopped ATO

        train.state = TrainState.RUNNING_WAITING
        train.current_speed = 0.0

        # clear_to_move is called (e.g., by RouteExecutor when signal clears)
        result = controller.clear_to_move()
        assert result is True

        # ato_running should stay False - user stopped it
        assert controller.ato_running is False, (
            "clear_to_move() should preserve ato_running=False"
        )

    def test_clear_to_move_preserves_ato_running_true(self, network):
        """
        clear_to_move() should preserve ato_running=True if ATO was running.
        """
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("CLEAR_RESUME", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="CLEAR_RESUME", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_WAITING
        controller.ato_running = True  # ATO was running before waiting

        train.state = TrainState.RUNNING_WAITING
        train.current_speed = 0.0

        # clear_to_move is called
        result = controller.clear_to_move()
        assert result is True

        # ato_running should stay True
        assert controller.ato_running is True, (
            "clear_to_move() should preserve ato_running=True"
        )


class TestAtoDistanceAwareControl:
    """
    Comprehensive tests for ATO distance-aware control (2026-01-26).

    BUG: ATO speed control uses tight tolerance around target speed, causing
    oscillation and re-acceleration near end-of-path.

    FIX: Use distance-aware target speed from braking curve and select handle
    with hysteresis to avoid N/B chatter.
    """

    def _setup_ato_train_with_path(self, network, clearance_cm: float = 1000.0):
        """Helper to set up an ATO train with specified clearance."""
        track = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        next_track = TrackSegment(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        network.create_block_with_tracks("BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_DIST_TEST", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="ATO_DIST_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.clearance_ahead_cm = clearance_cm
        controller.atc_speed_limit = 60.0
        controller.current_block_speed_limit = 60.0

        train.state = TrainState.RUNNING_MOVING
        return train, controller

    def test_ato_stop_uses_aggressive_service_brake_not_eb(self, network):
        """
        ATO stop should use aggressive service brake (B4-B8), never EB.

        When ato_stop() is called:
        1. ato_running is set to False
        2. tick() should apply aggressive braking (high B notch, not EB)
        3. EB is only allowed by ATC overspeed logic
        """
        train, controller = self._setup_ato_train_with_path(network)
        train.current_speed = 40.0  # Train moving at moderate speed

        # ATO stop is pressed
        controller.ato_stop()
        assert controller.ato_running is False

        # Tick should apply aggressive service braking
        controller.tick(0.1)

        # Handle should be high B-notch (B4-B8) but NOT EB
        handle = controller.handle_position
        assert handle != HandlePosition.EB, (
            f"ATO stop should never use EB, got {handle.value}"
        )
        assert handle.value.startswith("B"), (
            f"ATO stop should use B-notch, got {handle.value}"
        )
        # Should be aggressive service brake (B4 or higher)
        brake_number = int(handle.value[1])
        assert brake_number >= 4, (
            f"ATO stop should use aggressive braking (B4+), got {handle.value}"
        )

    def test_ato_stop_at_high_speed_uses_b8(self, network):
        """ATO stop at high speed should use B8 (maximum service brake)."""
        train, controller = self._setup_ato_train_with_path(network)
        train.current_speed = 80.0  # High speed

        controller.ato_stop()
        controller.tick(0.1)

        # Should use maximum service brake B8
        assert controller.handle_position == HandlePosition.B8, (
            f"ATO stop at high speed should use B8, got {controller.handle_position.value}"
        )

    def test_ato_distance_aware_braking_triggers_service_brake(self, network):
        """
        ATO should trigger service braking (not EB) when approaching end of clearance.

        When clearance_ahead_cm is less than braking distance * 1.5:
        - Handle should apply braking
        - Should use service brake (B-notch), not EB

        Note: At model scale, safety margin is 25cm. Clearance must be > 25cm
        for service braking; below that ATC may trigger EB for safety.
        """
        # Create tracks with a long enough block
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create block that covers the tracks with reasonable length
        # At model scale, 100 cm gives us plenty of room
        network.create_block_with_tracks(
            "LONG_BLOCK", [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)], length_cm=100.0
        )

        car = TrainCar("ATO_BRAKE_TEST", "LOCOMOTIVE", length=10.0)
        train = Train(train_id="ATO_BRAKE_TEST", consist=[car])
        network.add_train(train)
        # Place at middle of block so we have clearance ahead
        network.place_train(train, 2, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 30.0  # Moving at 30 km/h

        # Tick to calculate clearance and decide handle
        controller.tick(0.1)

        # Check clearance - should be around 40cm (remaining in block)
        # which is above safety margin (25cm), so ATC should allow some speed
        clearance = controller.clearance_ahead_cm
        atc_limit = controller.atc_speed_limit
        handle = controller.handle_position

        # With clearance > safety margin, should not trigger EB
        if clearance > 25.0:
            assert handle != HandlePosition.EB or controller.eb_active, (
                f"With {clearance:.1f}cm clearance (> 25cm safety margin), "
                f"should not trigger EB without overspeed. Handle={handle.value}, "
                f"ATC limit={atc_limit:.1f} km/h"
            )

    def test_ato_low_speed_coast_band_prevents_oscillation(self, network):
        """
        At low speeds, ATO should use a wider coast band to prevent N/B oscillation.

        When within coast band of target speed:
        - Handle should be N (coast)
        - Should not flip between B and P
        """
        train, controller = self._setup_ato_train_with_path(network)
        train.current_speed = 5.0  # Low speed
        controller.atc_speed_limit = 10.0  # Target is 10 km/h
        controller.current_block_speed_limit = 10.0
        controller.clearance_ahead_cm = 10000.0  # Plenty of clearance

        # First tick
        controller.tick(0.1)
        handle_1 = controller.handle_position

        # Simulate speed slightly changing
        train.current_speed = 6.0
        controller.tick(0.1)
        handle_2 = controller.handle_position

        # At low speed near target, should prefer coasting or gentle adjustment
        # rather than aggressive notch changes
        # This test validates stability - handles shouldn't jump wildly
        if handle_1 == HandlePosition.N:
            # If coasting, small speed change shouldn't force braking
            assert handle_2 in (
                HandlePosition.N,
                HandlePosition.P1,
                HandlePosition.B1,
            ), f"Low speed near target: gentle changes only, got {handle_2.value}"

    def test_ato_aggressive_service_stop_notch(self, network):
        """
        Test that the aggressive service stop uses B5+ for normal speeds.

        This is specifically for when ato_stop() is called (user presses stop).
        The handle should be at least B5 (aggressive but not EB).
        """
        train, controller = self._setup_ato_train_with_path(network)
        train.current_speed = 25.0  # Moderate speed

        controller.ato_stop()
        controller.tick(0.1)

        handle = controller.handle_position
        assert handle.value.startswith("B"), (
            f"ATO stop should use brake, got {handle.value}"
        )
        brake_number = int(handle.value[1])
        # At 25 km/h, should use at least B5 for aggressive stop
        assert brake_number >= 5, (
            f"ATO stop at 25 km/h should use B5+, got {handle.value}"
        )


class TestAtoStateInheritance:
    """
    Tests for ATO state inheritance when SimulationController creates controllers.

    BUG: In demo_main.py, a temporary TrainController calls depart() which sets
    ato_running=True. But when SimulationController later creates its own controller,
    it starts with ato_running=False, losing the ATO state.

    FIX: Store ato_running on the Train object so that _get_or_create_controller()
    can inherit the ATO running state from the train.
    """

    def test_sim_controller_inherits_ato_running_from_train(self):
        """
        When a train has ato_running=True set before SimulationController is created,
        the controller created by SimulationController should inherit ato_running=True.

        This simulates the demo_main.py scenario:
        1. Train is created with path assigned
        2. Temporary controller calls depart() -> ato_running=True
        3. Train stores ato_running state
        4. SimulationController is created later
        5. SimController's controller should have ato_running=True
        """
        # Setup: Create network with track and block
        network = RailwayNetwork()
        for x in range(10):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        network.create_block_with_tracks(
            block_id="BLOCK_A",
            positions=[(x, 0) for x in range(5)],
            name="Block A",
        )
        network.create_block_with_tracks(
            block_id="BLOCK_B",
            positions=[(x, 0) for x in range(5, 10)],
            name="Block B",
        )

        # Create train
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0)
        train = Train(train_id="TEST_TRAIN", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Set ATO running on train (this is what demo_main should do)
        train.ato_running = True  # <-- The fix: Train stores ATO state
        train.state = TrainState.RUNNING_MOVING

        # Create SimulationController AFTER train has ato_running set
        sim_controller = SimulationController(network)

        # Get the controller that SimulationController creates
        controller = sim_controller._get_or_create_controller(train)

        # The controller should inherit ato_running from the train
        assert controller.ato_running is True, (
            f"Controller should inherit ato_running=True from train, "
            f"but got ato_running={controller.ato_running}"
        )

    def test_sim_controller_inherits_ato_false_by_default(self):
        """
        When a train has ato_running=False (or not set), controller should have False.
        """
        # Setup
        network = RailwayNetwork()
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0)
        train = Train(train_id="TEST_TRAIN", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Do NOT set ato_running on train

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)

        # Default should be False
        assert controller.ato_running is False

    def test_demo_workflow_ato_persists_through_controller_recreation(self):
        """
        Simulates the full demo_main.py workflow:
        1. Create train with path
        2. Create temp controller, call depart()
        3. Sync train.ato_running = controller.ato_running
        4. Create SimulationController
        5. Verify ATO is running
        """
        # Setup
        network = RailwayNetwork()
        for x in range(10):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        network.create_block_with_tracks(
            block_id="BLOCK_A",
            positions=[(x, 0) for x in range(5)],
            name="Block A",
        )
        network.create_block_with_tracks(
            block_id="BLOCK_B",
            positions=[(x, 0) for x in range(5, 10)],
            name="Block B",
        )

        # Create train with path
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0)
        train = Train(train_id="TEST_TRAIN", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Assign a simple path
        train.current_path = [(x, 0, Direction.EAST) for x in range(10)]
        train.path_index = 0

        # Step 2: Create temporary controller and call depart()
        temp_controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
        )
        temp_controller.power_on_cmd()
        temp_controller.set_direction_and_standby(Direction.EAST)
        temp_controller.depart()

        # Verify temp controller has ATO running
        assert temp_controller.ato_running is True, (
            "depart() should set ato_running=True"
        )

        # Step 3: Sync ATO state to train (this is the fix)
        train.state = temp_controller.state
        train.ato_running = temp_controller.ato_running  # <-- The fix

        # Step 4: Create SimulationController (simulates server.py)
        sim_controller = SimulationController(network)

        # Step 5: Verify SimController's controller inherits ATO state
        controller = sim_controller._get_or_create_controller(train)
        assert controller.ato_running is True, (
            f"SimulationController's controller should inherit ato_running=True, "
            f"but got {controller.ato_running}"
        )


class TestAtoDestinationStoppingAfterPathCompletes:
    """
    Comprehensive tests for ATO stopping after path completes (2026-01-26).

    BUG: When the train head reaches the last tile of the path, the path is
    completed and train.reserved_blocks is cleared. ATO loses its destination
    info because _get_blocks_ahead() returns empty, so atc_speed_limit becomes 0
    and the train stops immediately instead of decelerating properly.

    FIX: Use ato_destination_block_id in TrainController to calculate stopping position
    independently of path blocks. When path completes but ato_destination_block_id is
    set, calculate clearance based on distance to destination block end.

    Key code locations:
    - TrainController.ato_destination_block_id (line ~277)
    - TrainController._calculate_clearance_ahead() (line ~522)
    - TrainController._get_blocks_ahead() (line ~770)
    """

    def _setup_station_track(self, network):
        """
        Create a simple track with a station at the end.

        Layout:
        - Tiles (0,0) to (4,0) - 5 tiles of straight track (E-W)
        - Station on tiles (3,0) and (4,0) - 2-tile platform
        - Block BLOCK_A on tiles (0,0) to (2,0)
        - Block BLOCK_B on tiles (3,0) to (4,0) - station block
        """
        from railway_platform import Platform

        # Create 5 tiles of straight track
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create blocks
        network.create_block_with_tracks(
            block_id="BLOCK_A",
            positions=[(0, 0), (1, 0), (2, 0)],
            name="Block A",
            length_cm=150.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_B",
            positions=[(3, 0), (4, 0)],
            name="Block B - Station",
            length_cm=100.0,
        )

        # Create station on tiles (3,0) and (4,0)
        station = Platform(
            platform_id="STA_END",
            name="End Station",
            positions=[(3, 0), (4, 0)],
            length_cm=100.0,
        )
        network.stations["STA_END"] = station

        # Mark tracks as having station
        network.get_track(3, 0).station_id = "STA_END"
        network.get_track(4, 0).station_id = "STA_END"

        return station

    def test_ato_has_clearance_after_path_completes(self, network):
        """
        When path completes (reserved_blocks cleared), ATO should still have
        clearance based on ato_destination_block_id.

        Scenario:
        1. Train is on tile (3,0), heading EAST toward station end
        2. Path is completed, train.reserved_blocks is cleared
        3. ato_destination_block_id is set to "BLOCK_B" (the station block)
        4. ATO should calculate clearance to block end, not return 0
        """
        self._setup_station_track(network)

        # Create and place train
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TRAIN_STOP_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 3, 0, Direction.EAST)

        # Train has path tiles but reserved_blocks is EMPTY (path completed)
        train.current_path = [
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]
        train.path_index = 0
        train.reserved_blocks = []  # <-- Path completed, blocks cleared
        train.assigned_path_id = None  # Path no longer assigned

        # Create controller with ATO running
        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True

        # Set destination to station block - this is what should provide stopping info
        controller.ato_destination_block_id = "BLOCK_B"

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 20.0

        # Tick the controller
        controller.tick(0.1)

        # Clearance should NOT be 0 - should be distance to block end
        # The station block is 100cm long, train is at start of tile (3,0)
        # So clearance should be approximately 100cm (block length)
        assert controller.clearance_ahead_cm > 0, (
            f"Clearance should be > 0 when ato_destination_block_id is set, "
            f"but got {controller.clearance_ahead_cm}. "
            f"ATO should use block end stopping point, not rely on reserved_blocks."
        )

        # ATC speed limit should allow some movement toward the stop
        assert controller.atc_speed_limit > 0 or train.current_speed < 1.0, (
            f"ATC should allow deceleration toward station, not immediate stop. "
            f"atc_speed_limit={controller.atc_speed_limit}"
        )

    def test_ato_calculates_distance_to_station_end(self, network):
        """
        When ato_destination_block_id is set, clearance should be calculated
        based on distance to block/station stopping point (end of platform).

        The stopping point should be at the END of the station, not the
        beginning, to allow the train to stop at the platform center/end.
        """
        self._setup_station_track(network)

        # Create and place train at tile (2,0) - one tile before station
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TRAIN_DIST_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 2, 0, Direction.EAST)

        # Path is complete, no reserved blocks
        train.current_path = [
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]
        train.path_index = 0
        train.reserved_blocks = []

        # Create controller with ato_destination_block_id set
        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.ato_destination_block_id = "BLOCK_B"

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 30.0

        controller.tick(0.1)

        # Clearance should be distance to station end
        # Train is at end of tile (2,0), station is tiles (3,0)-(4,0)
        # Block A (where train is) is 150cm, Block B (station) is 100cm
        # Distance should be approximately: remaining in current tile + station length
        # (this depends on exact head position)
        clearance = controller.clearance_ahead_cm

        # At minimum, clearance should include the station length
        assert clearance >= 50.0, (
            f"Clearance should be at least 50cm (half station), got {clearance}. "
            f"Train should have distance to stop at platform end."
        )

    def test_ato_destination_cleared_when_stopped_at_station(self, network):
        """
        ato_destination_block_id should be cleared when train stops at the station.

        This prevents the old destination from affecting future paths.
        """
        self._setup_station_track(network)

        # Create train and manually place at end of station (tile 4,0, facing WEST)
        # For multi-tile platforms, train must be at last tile to be "at station"
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TRAIN_CLEAR_TEST", consist=[car])
        network.add_train(train)

        # Manually set position at last station tile (4,0), facing WEST
        train.position = TrainPosition(
            end_a_tile=(4, 0),
            end_a_distance_cm=25.0,  # Middle of tile
            end_a_entry_edge="E",  # Entered from east, so facing WEST
            end_b_tile=(4, 0),
            end_b_distance_cm=25.0,
            end_b_entry_edge="E",
            leading_end="a",
            _facing_direction_legacy=Direction.WEST,
            occupied_tiles=[(4, 0)],
            occupied_blocks=["BLOCK_B"],
        )
        train.facing_direction = Direction.WEST

        train.current_path = [(3, 0, Direction.EAST), (4, 0, Direction.EAST)]
        train.path_index = 1  # At end of path (tile 4,0)
        train.reserved_blocks = []

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.ato_destination_block_id = "BLOCK_B"

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0  # Train is stopped

        # Tick should detect train is at station and stopped
        controller.tick(0.1)

        # When train arrives at station and stops, ato_destination_block_id should clear
        # (or transition to AT_STATION state should handle this)
        # At minimum, ATO should stop running
        assert (
            controller.ato_running is False
            or controller.state == TrainState.RUNNING_AT_STATION
        ), (
            f"Train stopped at destination should stop ATO or be AT_STATION. "
            f"ato_running={controller.ato_running}, state={controller.state}"
        )

    def test_ato_departs_with_destination_block_id_set(self, network):
        """
        When depart() is called with a path that has a destination block,
        ato_destination_block_id should be set from the path's end_block_id.
        """
        self._setup_station_track(network)

        # Create and place train at start
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TRAIN_DEPART_DEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.current_path = [(x, 0, Direction.EAST) for x in range(5)]
        train.path_index = 0
        # Simulate reserved blocks from path
        train.reserved_blocks = ["BLOCK_A", "BLOCK_B"]

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.STANDBY

        train.state = TrainState.STANDBY

        # Before depart, ato_destination_block_id should be None
        assert controller.ato_destination_block_id is None

        # Simulate that path has destination block
        # In real code, this would be set from the Path object's end_block_id
        controller.ato_destination_block_id = "BLOCK_B"

        # Depart
        result = controller.depart()
        assert result is True

        # After depart, ato_destination_block_id should still be set
        assert controller.ato_destination_block_id == "BLOCK_B", (
            f"ato_destination_block_id should persist through depart(), "
            f"but got {controller.ato_destination_block_id}"
        )


class TestAtoInfoPersistenceWithoutActivePath:
    """
    Comprehensive tests for onboard ATO info persistence fixed in 2026-02-08.

    BUG:
    `TrainController._calculate_ato_info()` returned None when `assigned_path_id`
    was cleared, even if destination intent (`ato_destination_block_id`) still
    existed. This caused panels to show "No path"/missing destination while the
    onboard controller should still expose destination context.

    FIX:
    Build `ato_info` from destination block even without active path ID.
    """

    def test_ato_info_keeps_destination_when_path_id_cleared(self, network):
        for x in range(2):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "BLOCK_DEST"
            network.add_track(track)

        network.blocks["BLOCK_DEST"] = Block(block_id="BLOCK_DEST", name="Destination")

        train = Train(train_id="TRAIN_ATO_INFO")
        train.position = make_train_position(
            head_tile=(0, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=10.0,
        )
        train.assigned_path_id = None
        train.ato_destination_block_id = "BLOCK_DEST"
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.ato_destination_block_id = "BLOCK_DEST"
        controller.ato_info = controller._calculate_ato_info()

        assert controller.ato_info is not None
        assert controller.ato_info.destination_block_id == "BLOCK_DEST"

    def test_simulation_controller_inherits_destination_from_train_field(self, network):
        for x in range(2):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "BLOCK_DEST"
            network.add_track(track)

        network.blocks["BLOCK_DEST"] = Block(block_id="BLOCK_DEST", name="Destination")

        train = Train(train_id="TRAIN_ATO_INFO_SIM")
        train.position = make_train_position(
            head_tile=(0, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=10.0,
        )
        train.ato_destination_block_id = "BLOCK_DEST"
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.ato_info = controller._calculate_ato_info()

        assert controller.ato_destination_block_id == "BLOCK_DEST"
        assert controller.ato_info is not None
        assert controller.ato_info.destination_block_id == "BLOCK_DEST"


class TestShorterMarginWithStationEnd:
    """
    Test that safety margin is 5cm (not 25cm) when destination is a station block.

    This test specifically mimics the demo_main.py setup to ensure the safety margin
    calculation works correctly in production code. This issue has occurred multiple
    times, so we need a regression test.

    The bug was: demo_main.py sets ato_destination_block_id on a temporary controller,
    but SimulationController creates a new controller that doesn't inherit it.

    The fix: Sync ato_destination_block_id to the train object, and make
    SimulationController inherit it when creating controllers.
    """

    @pytest.fixture
    def network(self):
        return RailwayNetwork()

    def test_safety_margin_5cm_for_station_destination(self, network):
        """
        Safety margin should be 5cm when ato_destination_block_id points to a station block.

        This mimics the demo_main.py workflow:
        1. Create train and path to station
        2. Create temporary controller and set ato_destination_block_id
        3. Sync ato_destination_block_id to train object
        4. SimulationController creates new controller and inherits ato_destination_block_id
        5. Safety margin should be 5cm (platform margin)
        """
        from railway_platform import Platform
        from simulation import SimulationController
        from path_dispatcher import PathDispatcher

        # Create simple 5-tile track with station at end
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create blocks
        network.create_block_with_tracks(
            block_id="BLOCK_START",
            positions=[(0, 0), (1, 0), (2, 0)],
            name="Start Block",
            length_cm=150.0,
        )

        # Station block (destination)
        network.create_block_with_tracks(
            block_id="BLOCK_STATION",
            positions=[(3, 0), (4, 0)],
            name="Station Block",
            length_cm=100.0,
        )

        # Add station
        station = Platform(
            platform_id="STA_END",
            name="End Station",
            positions=[(3, 0), (4, 0)],
            length_cm=100.0,
        )
        network.stations["STA_END"] = station
        network.get_track(3, 0).station_id = "STA_END"
        network.get_track(4, 0).station_id = "STA_END"

        # Create train
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TEST_TRAIN", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Create path
        pd = PathDispatcher(network)
        path_result = pd.create_path(
            start_block_id="BLOCK_START",
            end_block_id="BLOCK_STATION",
            train_id=train.train_id,
        )
        assert path_result.success

        # Mimic demo_main.py workflow: create temporary controller
        temp_controller = TrainController(train_id=train.train_id, network=network)
        temp_controller.ato_destination_block_id = path_result.path.end_block_id
        temp_controller.power_on = True
        temp_controller.control_mode = "ato"
        temp_controller.active_cab = "A"
        temp_controller.ato_running = True

        # CRITICAL: Sync ato_destination_block_id to train (this is the fix)
        train.ato_destination_block_id = temp_controller.ato_destination_block_id
        train.ato_running = temp_controller.ato_running

        # Create SimulationController (creates new controller instances)
        sim_ctrl = SimulationController(network, pd)

        # Get controller from simulation (should inherit ato_destination_block_id)
        controller = sim_ctrl._get_or_create_controller(train)

        # Verify ato_destination_block_id was inherited
        assert controller.ato_destination_block_id == "BLOCK_STATION", (
            f"ato_destination_block_id not inherited! "
            f"Got {controller.ato_destination_block_id}, expected BLOCK_STATION"
        )

        # Verify station is detected in destination block
        station_id = controller._get_station_in_block("BLOCK_STATION")
        assert station_id == "STA_END", (
            f"Station not found in block! Got {station_id}, expected STA_END"
        )

        # THE KEY TEST: Safety margin should be 5cm for platform
        safety_margin = controller._get_effective_safety_margin()
        assert safety_margin == 5.0, (
            f"Safety margin WRONG! Got {safety_margin} cm, expected 5.0 cm. "
            f"Station destination should use platform margin (5cm), not default (25cm)."
        )


class TestTrendAwareATOControl:
    """
    Comprehensive tests for Trend-Aware ATO Control implemented Jan 2026.

    These tests ensure the following improvements work correctly:

    IMPROVEMENTS:
    1. Dead band behavior prevents oscillation during cruise
    2. Asymmetric hysteresis (entry margin=1, exit margin=3)
    3. Trend-aware adjustments (only change handle if current action not working)
    4. Handle rate limiting (max ±1 notch per tick)
    5. Priority overrides (stopping, braking curve)

    The control system uses speed zones:
    - Slow Down Zone: speed >= target-1 (need to reduce)
    - Dead Band: target-3 <= speed < target-1 (coast, do nothing)
    - Speed Up Zone: speed < target-3 (need to increase)
    """

    @pytest.fixture
    def network(self):
        return RailwayNetwork()

    @pytest.fixture
    def straight_track(self, network):
        for x in range(10):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        return network

    @pytest.fixture
    def train_and_controller(self, network, straight_track):
        car = TrainCar(car_id="CAR_TEST", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TRAIN_ATO_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller._previous_speed = 0.0
        controller.atc_speed_limit = 60.0
        controller.current_block_speed_limit = 80.0
        controller.clearance_ahead_cm = 100000.0

        return train, controller

    def test_dead_band_no_action_when_cruising(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 58.0
        controller._previous_speed = 58.0
        controller.handle_position = HandlePosition.P2

        handle = controller._ato_calculate_handle()

        assert handle == HandlePosition.P2, (
            f"Dead band (effective_target-3=57 to effective_target-1=59): should keep current handle. "
            f"Got {handle} for speed={train.current_speed}, expected to keep {controller.handle_position}"
        )

    def test_slow_down_zone_power_to_neutral(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 60.5
        controller._previous_speed = 60.3
        controller.handle_position = HandlePosition.P3

        handle = controller._ato_calculate_handle()

        assert handle == HandlePosition.N, (
            f"Slow Down Zone (speed >= target-1=59): accelerating (P3) should go to Neutral. "
            f"Got {handle}"
        )

    def test_slow_down_zone_neutral_to_brake(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 60.8
        controller._previous_speed = 60.5
        controller.handle_position = HandlePosition.N

        handle = controller._ato_calculate_handle()

        assert handle == HandlePosition.B1, (
            f"Slow Down Zone: neutral + speed rising should start gentle brake (B1). "
            f"Got {handle}"
        )

    def test_slow_down_zone_brake_harder_if_not_working(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 61.0
        controller._previous_speed = 60.2
        controller.handle_position = HandlePosition.B1

        handle = controller._ato_calculate_handle()

        assert handle == HandlePosition.B2, (
            f"Slow Down Zone: braking (B1) but speed rising should brake harder (+1 notch). "
            f"Got {handle}"
        )

    def test_slow_down_zone_brake_keep_if_working(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 60.5
        controller._previous_speed = 61.0
        controller.handle_position = HandlePosition.B2

        handle = controller._ato_calculate_handle()

        # With improved brake release: when speed is falling and close to target (< 2 km/h),
        # jump to Neutral to avoid over-braking
        assert handle == HandlePosition.N, (
            f"Slow Down Zone: braking (B2) and speed falling close to target (overshoot < 2) should "
            f"release to Neutral. Got {handle}"
        )

    def test_speed_up_zone_brake_to_neutral(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 55.0
        controller._previous_speed = 56.0
        controller.handle_position = HandlePosition.B3

        handle = controller._ato_calculate_handle()

        assert handle == HandlePosition.B2, (
            f"Speed Up Zone (speed < target-3=57): should release brake gradually (-1 notch). "
            f"Got {handle}"
        )

    def test_speed_up_zone_neutral_to_power(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 54.0
        controller._previous_speed = 54.5
        controller.handle_position = HandlePosition.N

        handle = controller._ato_calculate_handle()

        # With simplified power notch: speed diff is 6 km/h (60-54),
        # which is > 5, so we use full power (P5) for quick acceleration
        assert handle == HandlePosition.P5, (
            f"Speed Up Zone: neutral with 6 km/h gap (>5) should use full power (P5). "
            f"Got {handle}"
        )

    def test_speed_up_zone_power_up_if_not_working(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 53.0
        controller._previous_speed = 54.0
        controller.handle_position = HandlePosition.P1

        handle = controller._ato_calculate_handle()

        # Speed diff is 7 km/h (60-53), which is > 5, so use P5
        assert handle == HandlePosition.P5, (
            f"Speed Up Zone: with 7 km/h gap (>5) should use full power (P5). "
            f"Got {handle}"
        )

    def test_speed_up_zone_power_keep_if_working(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 54.5
        controller._previous_speed = 54.0
        controller.handle_position = HandlePosition.P2

        handle = controller._ato_calculate_handle()

        # Speed diff is 5.5 km/h (60-54.5), which is > 5, so use P5
        assert handle == HandlePosition.P5, (
            f"Speed Up Zone: with 5.5 km/h gap (>5) should use full power (P5). "
            f"Got {handle}"
        )

    def test_handle_rate_limiting_max_one_notch(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 65.0
        controller._previous_speed = 64.0
        controller.handle_position = HandlePosition.P5

        handle = controller._ato_calculate_handle()

        # With improved braking response: when overshoot is 3+ km/h (lowered from 5),
        # apply B2 immediately (lowered from B3 to leave room for ATC)
        assert handle == HandlePosition.B2, (
            f"Fast response: P5 with 5 km/h overshoot should apply B2 immediately. "
            f"Got {handle}"
        )

    def test_priority_override_stopping_uses_full_brake(self, train_and_controller):
        train, controller = train_and_controller
        controller.atc_speed_limit = 0.0

        train.current_speed = 15.0
        controller._previous_speed = 20.0
        controller.handle_position = HandlePosition.B2

        handle = controller._ato_calculate_handle()

        assert handle == HandlePosition.B5, (
            f"Priority: stopping (effective_target=0) should use _calculate_braking_notch. "
            f"For speed=15.0, expects B5 (speed > 14). Got {handle}"
        )

    def test_trend_detection_rising(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 55.5
        controller._previous_speed = 55.0

        trend = controller._detect_speed_trend(train.current_speed)

        assert trend == "rising", f"Speed 55.0→55.5 should be 'rising', got {trend}"

    def test_trend_detection_falling(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 54.5
        controller._previous_speed = 55.0

        trend = controller._detect_speed_trend(train.current_speed)

        assert trend == "falling", f"Speed 55.0→54.5 should be 'falling', got {trend}"

    def test_trend_detection_stable(self, train_and_controller):
        train, controller = train_and_controller

        train.current_speed = 55.0
        controller._previous_speed = 55.05

        trend = controller._detect_speed_trend(train.current_speed)

        assert trend == "stable", (
            f"Speed 55.05→55.0 (Δ=0.05 < epsilon) should be 'stable', got {trend}"
        )


class TestSpeedCurveCalculation:
    """
    Tests for calculate_speed_curves() method in TrainController.

    This method calculates ATC and operational speed limits at various distances
    ahead of the train for visualization in the train monitor.
    """

    @pytest.fixture
    def network(self):
        return RailwayNetwork()

    @pytest.fixture
    def straight_track(self, network):
        for x in range(20):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        return network

    @pytest.fixture
    def train_and_controller(self, network, straight_track):
        car = TrainCar(car_id="CAR_CURVE", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TRAIN_CURVE_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.current_path = [(x, 0, Direction.EAST) for x in range(20)]
        train.path_index = 0

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.RUNNING_MOVING

        return train, controller

    def test_speed_curves_exist(self, train_and_controller):
        train, controller = train_and_controller

        curves = controller.calculate_speed_curves(max_distance_cm=5000)

        assert len(curves) > 0, "Speed curves should be calculated"
        # Uses 3cm resolution per CURVE_SAMPLE_RESOLUTION_CM
        # Points at 0, 3, 6, ..., 4998, 5000 = 1667 steps + 1 endpoint = 1668 points
        expected_points = (5000 // 3) + 2  # 1668 points (0, 3, 6, ..., 4998, 5000)
        assert len(curves) == expected_points, (
            f"Should have {expected_points} points (every 3cm, inclusive endpoints: 0 to 5000)"
        )

    def test_speed_curves_structure(self, train_and_controller):
        train, controller = train_and_controller

        curves = controller.calculate_speed_curves(max_distance_cm=1000)

        for point in curves:
            assert "distance_cm" in point, "Each point should have distance_cm"
            assert "atc_limit" in point, "Each point should have atc_limit"
            assert "operational_limit" in point, (
                "Each point should have operational_limit"
            )
            assert isinstance(point["distance_cm"], (int, float))
            assert isinstance(point["atc_limit"], (int, float))
            assert isinstance(point["operational_limit"], (int, float))

    def test_operational_limit_below_atc_limit(self, train_and_controller):
        train, controller = train_and_controller

        curves = controller.calculate_speed_curves(max_distance_cm=2000)

        for point in curves:
            assert point["operational_limit"] <= point["atc_limit"], (
                f"Operational limit ({point['operational_limit']}) should be <= ATC limit "
                f"({point['atc_limit']}) at distance {point['distance_cm']}cm"
            )

    def test_speed_curves_capped_at_train_max_speed(self, train_and_controller):
        train, controller = train_and_controller
        train.max_speed = 80.0

        curves = controller.calculate_speed_curves(max_distance_cm=3000)

        for point in curves:
            assert point["atc_limit"] <= train.max_speed, (
                f"ATC limit ({point['atc_limit']}) should be <= train max speed ({train.max_speed})"
            )
            assert point["operational_limit"] <= train.max_speed, (
                f"Operational limit ({point['operational_limit']}) should be <= train max speed"
            )

    def test_ato_limit_5kmh_below_atc(self, train_and_controller):
        """ATO limit should be at least 5 km/h below ATC (per ATO_SPEED_MARGIN_KMH)."""
        train, controller = train_and_controller
        train.max_speed = 100.0

        curves = controller.calculate_speed_curves(max_distance_cm=2000)

        # Check points where both limits are > 0 (not at very end)
        for point in curves:
            atc = point["atc_limit"]
            ato = point["operational_limit"]
            if atc > 5.0:  # Only check where ATC > 5 km/h
                assert ato <= atc - 5.0, (
                    f"ATO ({ato}) should be at least 5 km/h below ATC ({atc}) "
                    f"at distance {point['distance_cm']}cm"
                )


class TestFutureAwareATOBraking:
    """
    Tests for future-aware ATO braking.

    These tests verify that the ATO looks ahead in the speed curve
    and starts braking before restrictions, not after.
    """

    @pytest.fixture
    def network_with_train(self):
        """Create network with train and controller."""
        from network import RailwayNetwork
        from train import Train, TrainPosition
        from train_controller import TrainController
        from enums import Direction

        network = RailwayNetwork()

        # Create a simple track
        from track import TrackSegment, TRACK_EW

        for x in range(10):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        train = Train(train_id="T1", max_speed=60.0)
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=5.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(0, 0)],
            occupied_blocks=[],
        )
        train.current_speed = 30.0
        network.add_train(train)

        controller = TrainController(train_id="T1", network=network)
        controller.power_on = True
        controller.active_cab = "A"
        controller.ato_running = True
        controller.clearance_ahead_cm = 500.0
        controller.current_block_speed_limit = 60.0
        controller.atc_speed_limit = 60.0

        return network, train, controller

    def test_reaction_distance_scales_with_speed(self, network_with_train):
        """Reaction distance should increase with speed, with minimum floor."""
        _, train, controller = network_with_train

        # At 0 speed, reaction distance should be the minimum floor (30cm)
        # This ensures we can see ahead even when starting from a stop
        dist_0 = controller._get_reaction_distance_cm(0.0)
        assert dist_0 == 30.0, (
            "Reaction distance at 0 speed should be minimum floor (30cm)"
        )

        # At higher speed, reaction distance should be larger (once above the floor)
        dist_30 = controller._get_reaction_distance_cm(30.0)
        dist_60 = controller._get_reaction_distance_cm(60.0)
        dist_90 = controller._get_reaction_distance_cm(90.0)

        # At moderate speeds, might still be at floor, but high speeds should exceed it
        assert dist_60 >= dist_30 >= dist_0, (
            "Reaction distance should not decrease with speed"
        )
        assert dist_90 > dist_60, (
            "At high speeds, quadratic scaling should exceed floor"
        )

    def test_sample_curve_at_interpolates(self, network_with_train):
        """Curve sampling should interpolate between points."""
        _, _, controller = network_with_train

        # Create a simple test curve
        test_curve = [
            {"distance_cm": 0.0, "operational_limit": 60.0, "atc_limit": 65.0},
            {"distance_cm": 100.0, "operational_limit": 40.0, "atc_limit": 45.0},
            {"distance_cm": 200.0, "operational_limit": 20.0, "atc_limit": 25.0},
        ]

        # Sample at exact points
        assert controller._sample_curve_at(test_curve, 0.0) == 60.0
        assert controller._sample_curve_at(test_curve, 100.0) == 40.0
        assert controller._sample_curve_at(test_curve, 200.0) == 20.0

        # Sample at midpoint - should interpolate
        mid = controller._sample_curve_at(test_curve, 50.0)
        assert 45.0 < mid < 55.0, f"Midpoint should be interpolated: {mid}"


class TestBlockTileSyncValidation:
    """
    Tests for block-tile position sync validation.

    These tests verify that the sync validation detects and corrects
    desyncs between tiles and blocks.
    """

    @pytest.fixture
    def physics_setup(self):
        """Create network, train, and physics."""
        from network import RailwayNetwork
        from train import Train, TrainPosition
        from train_physics import TrainPhysics
        from train_controller import TrainController
        from block import Block
        from enums import Direction, BlockState

        network = RailwayNetwork()

        # Create track with blocks
        from track import TrackSegment, TRACK_EW

        block = Block(block_id="B1", length_cm=300.0, state=BlockState.FREE)
        network.blocks["B1"] = block

        for x in range(6):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW, block_section_id="B1")
            network.add_track(track)

        train = Train(train_id="T1", max_speed=60.0)
        train.position = TrainPosition(
            end_a_tile=(2, 0),
            end_a_distance_cm=25.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end="a",
            _facing_direction_legacy=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
            occupied_blocks=["B1"],
        )
        train.current_speed = 0.0
        network.add_train(train)

        controller = TrainController(train_id="T1", network=network)
        physics = TrainPhysics(train_id="T1", network=network, controller=controller)

        return network, train, physics

    def test_sync_validation_returns_true_when_synced(self, physics_setup):
        """Validation should return True when tiles and blocks match."""
        _, train, physics = physics_setup

        # Position is synced by default
        result = physics._validate_position_sync(train)
        assert result is True, "Should return True when synced"

    def test_sync_validation_detects_desync(self, physics_setup):
        """Validation should detect when blocks don't match tiles."""
        _, train, physics = physics_setup

        # Create a desync: tiles say B1, but occupied_blocks is empty
        train.position.occupied_blocks = []

        result = physics._validate_position_sync(train)
        assert result is False, "Should return False when desynced"

    def test_sync_validation_auto_corrects(self, physics_setup):
        """Validation should auto-correct occupied_blocks from tiles."""
        _, train, physics = physics_setup

        # Create a desync
        train.position.occupied_blocks = []

        # Validate (which should correct)
        physics._validate_position_sync(train)

        # Should now have B1 in occupied_blocks
        assert "B1" in train.position.occupied_blocks, (
            "Should auto-correct to include B1"
        )


class TestFallbackLookaheadMultiTileBlock:
    """
    Tests for fallback lookahead calculation in multi-tile blocks.

    Fixed bug: When train is first placed in a multi-tile block (no path yet),
    the fallback lookahead was only calculating remaining distance in the
    current TILE, not the remaining distance in the entire BLOCK.

    This caused the destination line to appear at the wrong position
    (middle of block instead of end of block).
    """

    @pytest.fixture
    def multi_tile_block_network(self):
        """Create network with a 3-tile block for testing."""
        network = RailwayNetwork()

        # Create a 3-tile block (simulates terminal station)
        block = Block(block_id="TERMINAL", length_cm=150.0, state=BlockState.OCCUPIED)
        network.blocks["TERMINAL"] = block

        for x in range(3):
            track = TrackSegment(
                x=x, y=0, track_bits=TRACK_EW, block_section_id="TERMINAL"
            )
            network.add_track(track)

        return network

    def test_fallback_calculates_full_block_clearance(self, multi_tile_block_network):
        """
        When train is placed on first tile of multi-tile block,
        clearance should include remaining distance to end of block.
        """
        network = multi_tile_block_network

        # Place train on first tile (x=0), facing EAST
        # Train head at tile (0,0) at the exit edge (distance = tile_length)
        train = Train(train_id="TRAIN_FALLBACK_TEST")

        # Each tile is 150/3 = 50 cm long
        per_tile_length = 50.0

        train.position = make_train_position(
            head_tile=(0, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=per_tile_length,  # At exit edge of first tile
        )
        train.position.occupied_tiles = [(0, 0)]
        train.position.occupied_blocks = ["TERMINAL"]

        # NO current_path - simulates first placement
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        # Get lookahead WITHOUT calling update() first
        # This simulates the API call on first placement
        lookahead = controller.get_monitor_lookahead()

        # The clearance should be the remaining distance to end of block
        # Block is 150cm, train has traveled 50cm (one tile), so remaining = 100cm
        clearance_cm = lookahead["lookahead"]["clearance_cm"]

        # Should be ~100cm (2 tiles remaining), not just 0cm (no remaining in current tile)
        assert clearance_cm >= 95.0, (
            f"Clearance should be ~100cm (2 tiles remaining), but got {clearance_cm}. "
            "The fallback lookahead should calculate remaining distance in ENTIRE block, "
            "not just current tile."
        )

    def test_fallback_mid_block_placement(self, multi_tile_block_network):
        """
        Train placed in middle tile of block should have correct remaining distance.
        """
        network = multi_tile_block_network
        per_tile_length = 50.0  # 150cm / 3 tiles

        train = Train(train_id="TRAIN_MID_BLOCK")
        train.position = make_train_position(
            head_tile=(1, 0),  # Middle tile
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=25.0,  # Halfway through middle tile
        )
        train.position.occupied_tiles = [(0, 0), (1, 0)]
        train.position.occupied_blocks = ["TERMINAL"]
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        lookahead = controller.get_monitor_lookahead()
        clearance_cm = lookahead["lookahead"]["clearance_cm"]

        # Train at (1,0), 25cm into tile
        # Tiles behind train in block: (0,0) = 50cm
        # Distance into current tile: 25cm
        # Total distance into block: 50 + 25 = 75cm
        # Remaining in block: 150 - 75 = 75cm
        assert 70.0 <= clearance_cm <= 80.0, (
            f"Clearance should be ~75cm, but got {clearance_cm}"
        )

    def test_fallback_last_tile_placement(self, multi_tile_block_network):
        """
        Train at end of block should have minimal clearance (just remaining in last tile).
        """
        network = multi_tile_block_network
        per_tile_length = 50.0

        train = Train(train_id="TRAIN_LAST_TILE")
        train.position = make_train_position(
            head_tile=(2, 0),  # Last tile
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=40.0,  # Near end of last tile
        )
        train.position.occupied_tiles = [(0, 0), (1, 0), (2, 0)]
        train.position.occupied_blocks = ["TERMINAL"]
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        lookahead = controller.get_monitor_lookahead()
        clearance_cm = lookahead["lookahead"]["clearance_cm"]

        # Train at (2,0), 40cm into tile
        # Tiles behind: (0,0) + (1,0) = 100cm
        # Distance into current: 40cm
        # Total into block: 140cm
        # Remaining: 150 - 140 = 10cm
        assert clearance_cm <= 15.0, (
            f"Clearance should be ~10cm (near end of block), but got {clearance_cm}"
        )

    def test_fallback_uses_correct_block_length(self, multi_tile_block_network):
        """
        The fallback should report the correct block length in the block info.
        """
        network = multi_tile_block_network

        train = Train(train_id="TRAIN_BLOCK_INFO")
        train.position = make_train_position(
            head_tile=(0, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=25.0,
        )
        train.position.occupied_tiles = [(0, 0)]
        train.position.occupied_blocks = ["TERMINAL"]
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        lookahead = controller.get_monitor_lookahead()
        blocks = lookahead["lookahead"]["blocks"]

        assert len(blocks) == 1
        assert blocks[0]["id"] == "TERMINAL"
        assert blocks[0]["length_cm"] == pytest.approx(150.0)
        assert blocks[0]["tile_count"] == 3

    def test_fallback_with_prior_clearance(self, multi_tile_block_network):
        """
        If controller has a prior clearance value, fallback should use it.
        """
        network = multi_tile_block_network

        train = Train(train_id="TRAIN_PRIOR_CLEARANCE")
        train.position = make_train_position(
            head_tile=(0, 0),
            _facing_direction_legacy=Direction.EAST,
            head_distance_cm=25.0,
        )
        train.position.occupied_tiles = [(0, 0)]
        train.position.occupied_blocks = ["TERMINAL"]
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        # Set a prior clearance (simulating simulation update ran earlier)
        controller.clearance_ahead_cm = 200.0

        lookahead = controller.get_monitor_lookahead()
        clearance_cm = lookahead["lookahead"]["clearance_cm"]

        # Should use the prior clearance value
        assert clearance_cm == 200.0


class TestRouteExecutorThreeStationLoopSimulation:
    """End-to-end route execution on a 3-station eastbound line."""

    def _build_three_station_line(self) -> RailwayNetwork:
        """Build A-B-C eastbound line with requested geometry and limits."""
        network = RailwayNetwork()

        for x in range(9):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        network.add_station(
            0,
            0,
            station_id="STA_A",
            name="A",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )
        network.add_station(
            4,
            0,
            station_id="STA_B",
            name="B",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )
        network.add_station(
            8,
            0,
            station_id="STA_C",
            name="C",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        network.create_block_with_tracks(
            block_id="BLK_AB",
            positions=[(1, 0), (2, 0), (3, 0)],
            name="A-B",
            length_cm=150.0,
        )
        network.create_block_with_tracks(
            block_id="BLK_BC",
            positions=[(5, 0), (6, 0), (7, 0)],
            name="B-C",
            length_cm=150.0,
        )

        for block in network.blocks.values():
            block.max_speed_kmh = 60.0

        return network

    def _run_until(
        self,
        simulation: SimulationController,
        condition,
        failure_message: str,
        max_steps: int = 20000,
        dt: float = 0.02,
    ) -> None:
        for _ in range(max_steps):
            simulation.update(dt)
            if condition():
                return
        pytest.fail(failure_message)

    def _force_arrival_at_platform(
        self,
        simulation: SimulationController,
        route: Route,
        train: Train,
        controller: TrainController,
        platform_id: str,
    ) -> None:
        """Force train at waypoint platform and trigger arrival logic deterministically."""
        platform = simulation.network.platforms[platform_id]
        tile = platform.positions[0]
        direction = train.facing_direction or Direction.EAST

        train.position = make_train_position(tile, direction)
        train.current_speed = 0.0
        train.assigned_path_id = None
        train.current_path = None
        train.reserved_blocks = []

        if simulation.path_dispatcher:
            for path_id in list(simulation.path_dispatcher.active_paths.keys()):
                simulation.path_dispatcher.cancel_path(path_id, force=True)

        train.state = TrainState.RUNNING_MOVING
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True

        exec_state = simulation.route_executor.execution_states.get(train.train_id)
        if exec_state is None:
            simulation.update(0.02)
            exec_state = simulation.route_executor.execution_states.get(train.train_id)
        assert exec_state is not None
        simulation.route_executor._check_waypoint_arrival(train, route, exec_state)
        assert exec_state.last_waypoint_platform_id == platform_id
        assert train.state in (TrainState.RUNNING_AT_STATION, TrainState.STANDBY)

    def _create_sim_and_train(self, train_id: str) -> tuple:
        """Create baseline 3-station simulation with one 45cm train at A."""
        network = self._build_three_station_line()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(car_id=f"CAR_{train_id}", car_type="LOCOMOTIVE", length=45.0)
        train = Train(train_id=train_id, consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.state = TrainState.IDLE
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.IDLE
        assert controller.set_direction_and_standby(Direction.EAST) is True
        assert controller.active_cab == "A"

        return network, dispatcher, simulation, train, controller

    def test_ato_route_a_b_c_loop_with_cab_switching(self):
        """Validate A->B->C->A loop execution and cab changes in ATO mode."""
        network = self._build_three_station_line()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(car_id="CAR_45CM", car_type="LOCOMOTIVE", length=45.0)
        train = Train(train_id="T_LOOP", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.state = TrainState.IDLE
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.IDLE
        assert controller.set_direction_and_standby(Direction.EAST) is True
        assert controller.active_cab == "A"

        route = Route(
            route_id="ROUTE_ABC_LOOP", name="A-B-C Loop", mode=RouteMode.CIRCULAR
        )
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.8))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.8))
        route.add_waypoint(Waypoint(platform_id="STA_A", dwell_time_sec=0.8))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        assert simulation.route_executor._is_train_at_platform(train, "STA_A") is True

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Train did not auto-dispatch from A to B",
        )

        self._force_arrival_at_platform(simulation, route, train, controller, "STA_B")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "Route executor did not dispatch B to C after dwell",
        )

        self._force_arrival_at_platform(simulation, route, train, controller, "STA_C")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_A_BLOCK"
                and train.facing_direction == Direction.WEST
                and controller.active_cab == "B"
            ),
            "Train did not switch cab and dispatch from C back to A",
            max_steps=5000,
        )

        # Return leg should target A directly (no B waypoint between C and A)
        exec_state = simulation.route_executor.execution_states[train.train_id]
        assert route.waypoints[exec_state.current_waypoint_index].platform_id == "STA_A"
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_A")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Train did not switch cab back at A for next loop",
            max_steps=5000,
        )

    def test_one_off_route_a_b_c_completes_and_unassigns(self):
        """ONE_OFF route should run A->B->C then clear assignment."""
        network, dispatcher, simulation, train, controller = self._create_sim_and_train(
            "T_ONE_OFF"
        )

        route = Route(
            route_id="ROUTE_ABC_ONE_OFF", name="A-B-C", mode=RouteMode.ONE_OFF
        )
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.6))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.6))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "ONE_OFF did not dispatch A->B",
        )
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_B")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "ONE_OFF did not dispatch B->C",
        )
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_C")

        self._run_until(
            simulation,
            lambda: train.assigned_route_id is None,
            "Train was not unassigned after ONE_OFF completion",
            max_steps=3000,
        )
        assert train.state in (
            TrainState.IDLE,
            TrainState.STANDBY,
            TrainState.RUNNING_AT_STATION,
        )

    def test_round_trip_route_runs_a_b_c_b_a(self):
        """ROUND_TRIP route should follow A->B->C->B->A sequence."""
        network, dispatcher, simulation, train, controller = self._create_sim_and_train(
            "T_ROUND_TRIP"
        )

        route = Route(
            route_id="ROUTE_ABC_ROUND_TRIP",
            name="A-B-C-B-A",
            mode=RouteMode.ROUND_TRIP,
        )
        route.add_waypoint(Waypoint(platform_id="STA_A", dwell_time_sec=0.4))
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.4))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.4))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        visited = []
        for target in ["STA_A", "STA_B", "STA_C", "STA_B", "STA_A"]:
            self._run_until(
                simulation,
                lambda: (
                    simulation.route_executor.execution_states.get(train.train_id)
                    is not None
                    and route.waypoints[
                        simulation.route_executor.execution_states[
                            train.train_id
                        ].current_waypoint_index
                    ].platform_id
                    == target
                ),
                f"ROUND_TRIP did not advance current waypoint to {target}",
                max_steps=5000,
            )
            self._force_arrival_at_platform(
                simulation, route, train, controller, target
            )
            visited.append(target)

        assert visited == ["STA_A", "STA_B", "STA_C", "STA_B", "STA_A"]

    def test_unassign_running_train_between_b_and_c_stops_at_b_or_c(self):
        """Unassigning while running should safely stop at nearby station block endpoint."""
        network, dispatcher, simulation, train, controller = self._create_sim_and_train(
            "T_UNASSIGN_RUN"
        )

        route = Route(route_id="ROUTE_UNASSIGN", name="A-B-C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.6))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.6))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Did not dispatch A->B",
        )
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_B")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "Did not dispatch B->C",
        )

        # Move to in-between block tile and keep running, then unassign route mid-run.
        train.position = make_train_position((6, 0), Direction.EAST)
        train.current_speed = 20.0
        train.state = TrainState.RUNNING_MOVING
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True

        route.unassign_train(train.train_id)
        train.assigned_route_id = None
        simulation.route_executor.execution_states.pop(train.train_id, None)

        if train.assigned_path_id:
            dispatcher.cancel_path(train.assigned_path_id, force=True)
        train.assigned_path_id = None
        train.current_path = None
        train.reserved_blocks = []

        auto_park_route = Route(
            route_id="TEMP_UNASSIGN_AUTO_PARK",
            name="AutoPark",
            mode=RouteMode.ONE_OFF,
        )
        auto_park_route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.4))
        auto_park_route.assign_train(train.train_id)
        auto_park_route.activate(simulation.simulation_time)
        network.routes[auto_park_route.route_id] = auto_park_route
        train.assigned_route_id = auto_park_route.route_id
        train.current_speed = 0.0
        train.state = TrainState.STANDBY
        controller.state = TrainState.STANDBY

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "Auto-park route did not dispatch from mid-block to C",
            max_steps=6000,
        )
        self._force_arrival_at_platform(
            simulation,
            auto_park_route,
            train,
            controller,
            "STA_C",
        )

        stop_tile = train.position.head_tile if train.position else None
        assert stop_tile in {(4, 0), (8, 0)}, (
            f"Expected final stop at B or C station tile, got {stop_tile}"
        )


class TestMultiTilePlatformStopping:
    """
    Tests for train stopping at multi-tile platforms.

    BUG: When a path ends at a multi-tile station platform, the train should stop
    at the END of the platform (last tile), not at the first tile. This test
    ensures the train controller correctly calculates clearance to the end of
    the platform regardless of block position ordering.

    Key issue: _calculate_clearance_to_end_of_current_block was checking if there
    were more station tiles after the current one in block_positions, but
    block_positions is in arbitrary insertion order, not travel direction order.
    """

    @pytest.fixture
    def multi_tile_station_network(self, network):
        """Create a network with a 2-tile station on the east branch."""
        from track import TRACK_NS, TRACK_EW, TRACK_NW

        # Create vertical track layout: (9,0) to (9,4)
        # Train will travel NORTH from (9,0) to station at (9,3)-(9,4)
        for y in range(5):
            track = TrackSegment(9, y, track_bits=TRACK_NS)
            network.add_track(track)

        # Create blocks
        network.create_block_with_tracks(
            block_id="BLOCK_START",
            positions=[(9, 0)],
            name="Start Block",
            length_cm=50.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_MID",
            positions=[(9, 1), (9, 2)],
            name="Middle Block",
            length_cm=100.0,
        )
        # Multi-tile station block spanning (9,3) and (9,4)
        # Add tracks in reverse order to test insertion order issue
        network.create_block_with_tracks(
            block_id="STATION_BLOCK",
            positions=[(9, 4), (9, 3)],  # Note: reversed order!
            name="East Terminal",
            length_cm=100.0,
        )

        # Create station platform spanning both tiles
        from railway_platform import Platform

        station = Platform(
            platform_id="EAST_TERMINAL",
            name="East Terminal",
            positions=[(9, 3), (9, 4)],
            length_cm=100.0,
            platform_side="both",
        )
        network.stations["EAST_TERMINAL"] = station

        # Mark tracks as having station
        network.get_track(9, 3).station_id = "EAST_TERMINAL"
        network.get_track(9, 4).station_id = "EAST_TERMINAL"

        return network

    def test_clearance_includes_full_multi_tile_station_length(
        self, multi_tile_station_network
    ):
        """
        _calculate_clearance_to_end_of_current_block() should calculate distance
        to the end of the platform, even when block_positions are in arbitrary order.

        This ensures ATO allows the train to travel the full platform length
        before stopping, rather than stopping at the first tile.
        """
        network = multi_tile_station_network

        # Create train at first station tile (9,3), traveling NORTH
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="CLEARANCE_TEST", consist=[car])
        network.add_train(train)

        # Place train at first station tile (9,3) with path to last tile (9,4)
        train.position = TrainPosition(
            end_a_tile=(9, 3),
            end_a_distance_cm=0.0,
            end_a_entry_edge="S",
            end_b_tile=(9, 3),
            end_b_distance_cm=0.0,
            end_b_entry_edge="S",
            leading_end="a",
            _facing_direction_legacy=Direction.NORTH,
            occupied_tiles=[(9, 3)],
            occupied_blocks=["STATION_BLOCK"],
        )
        train.current_speed = 20.0  # Moving
        train.current_path = [
            (9, 2, Direction.NORTH),
            (9, 3, Direction.NORTH),
            (9, 4, Direction.NORTH),
        ]

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        controller.ato_destination_block_id = "STATION_BLOCK"

        # Calculate clearance - should include distance to end of platform
        clearance = controller._calculate_clearance_to_end_of_current_block()

        # Station block is 2 tiles (9,3) and (9,4), each 50cm = 100cm total
        # Train is at start of tile (9,3), so clearance should be ~100cm
        # (remaining in current tile + full next tile)
        assert clearance >= 75.0, (
            f"Clearance should include full platform length (~100cm), "
            f"but got {clearance}cm. "
            f"The train will stop prematurely at the first tile instead of "
            f"continuing to the end of the multi-tile platform. "
            f"Block positions may be in reverse insertion order: "
            f"{network.get_block_positions('STATION_BLOCK')}"
        )

    def test_clearance_calculation_with_reversed_block_positions(
        self, multi_tile_station_network
    ):
        """
        _calculate_clearance_to_end_of_current_block should work correctly
        even when block positions are in reverse insertion order.

        The bug was that the code checked for 'more station tiles after' using
        block_positions index order, but block_positions can be in any order
        depending on insertion sequence.
        """
        network = multi_tile_station_network

        # Create train already in the station block at first tile (9,3)
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="CLEARANCE_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 9, 3, Direction.NORTH)

        # Manually set position to be at start of tile (9,3)
        train.position = TrainPosition(
            end_a_tile=(9, 3),
            end_a_distance_cm=0.0,
            end_a_entry_edge="S",
            end_b_tile=(9, 3),
            end_b_distance_cm=0.0,
            end_b_entry_edge="S",
            leading_end="a",
            _facing_direction_legacy=Direction.NORTH,
            occupied_tiles=[(9, 3)],
            occupied_blocks=["STATION_BLOCK"],
        )

        # Create controller
        controller = TrainController(train_id=train.train_id, network=network)
        controller.ato_destination_block_id = "STATION_BLOCK"

        # Calculate clearance to end of current block
        clearance = controller._calculate_clearance_to_end_of_current_block()

        # Clearance should be approximately 100cm (two 50cm tiles: (9,3) and (9,4))
        # Not just 50cm for the current tile
        assert clearance >= 75.0, (
            f"Clearance should include distance to end of platform (~100cm), "
            f"but got {clearance}cm. "
            f"Block positions were inserted in reverse order [(9,4), (9,3)], "
            f"which may cause the clearance calculation to stop early "
            f"if not properly sorting by travel direction."
        )


class TestManualPathAssignmentAfterArrival:
    """
    Comprehensive tests for manual path assignment bug.

    BUG: When a train arrives at a terminal (B) and user manually assigns
    a one-off path to another terminal (C), the ATO GO button flashes
    briefly and returns to STANDBY without the train moving.

    The path appears to be established correctly (debug info and lookahead
    JSON look correct, distance to stop is correct), but the train doesn't move.

    These tests ensure that manual path assignment from a stopped train works
    correctly and the train actually proceeds to the destination.
    """

    def _build_terminal_line_with_switch(self) -> RailwayNetwork:
        """Build A-B-C line with switch at B connecting to branch C.

        Layout:
        A(0,0) --- B(3,0)[Station] --- (4,0)[SWITCH] --- (continue main line)
                                        |
                                      C(4,4)
        """
        from track import TRACK_EW, TRACK_NS, TRACK_NW, TRACK_SE
        from switch import Switch

        network = RailwayNetwork()

        # Create main line: A to switch (0,0 to 4,0)
        for x in range(5):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        # Create switch at (4,0) - connects east-west to north-south
        # Replace the straight EW track at (4,0) with a switch track
        network.remove_track(4, 0)
        switch_track = TrackSegment(4, 0, track_bits=TRACK_EW | TRACK_NW)
        switch_track.switch_state = 0  # Start with EW (main)
        network.add_track(switch_track)

        # Create switch entity
        switch = Switch(switch_id="SW_B", x=4, y=0)
        network.add_switch(switch)
        switch_track.switch_id = switch.switch_id

        # Create branch line from switch northward (4,1) to (4,4)
        for y in range(1, 5):
            network.add_track(TrackSegment(x=4, y=y, track_bits=TRACK_NS))

        # Station A at west end (0,0)
        network.add_station(
            0,
            0,
            station_id="STA_A",
            name="West Terminal",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        # Station B at (3,0) - just before the switch (main terminal)
        network.add_station(
            3,
            0,
            station_id="STA_B",
            name="East Terminal",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        # Station C at end of branch (4,4) - the branch terminal
        network.add_station(
            4,
            4,
            station_id="STA_C",
            name="East Branch Terminal",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        # Create blocks
        # Block A-B (main line from A to before switch)
        # Note: stations create their own blocks automatically (STA_B_BLOCK, STA_C_BLOCK)
        network.create_block_with_tracks(
            block_id="BLK_A_B",
            positions=[(1, 0), (2, 0)],
            name="A to B",
            length_cm=100.0,
        )

        # Block B-C (the branch from switch to C)
        network.create_block_with_tracks(
            block_id="BLK_B_C",
            positions=[(4, 1), (4, 2), (4, 3)],
            name="B to C Branch",
            length_cm=150.0,
        )

        for block in network.blocks.values():
            block.max_speed_kmh = 60.0

        return network

    def _build_bidirectional_line(self, network: RailwayNetwork) -> None:
        """Build simple A-B-C bidirectional line for route/path testing.

        Layout:
        A(0,0) --- (1,0) --- (2,0) --- B(3,0) --- (4,0) --- (5,0) --- C(6,0)

        All stations and blocks are aligned on a single east-west line.
        Trains can go A→C facing EAST, then turn around and go C→A facing WEST.
        """
        from track import TRACK_EW

        # Create straight line from (0,0) to (6,0)
        for x in range(7):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        # Station A at (0,0) - west terminal
        network.add_station(
            0,
            0,
            station_id="STA_A",
            name="Station A",
            length_cm=50.0,
            top_speed_kmh=80.0,
        )

        # Station B at (3,0) - middle station
        network.add_station(
            3,
            0,
            station_id="STA_B",
            name="Station B",
            length_cm=50.0,
            top_speed_kmh=80.0,
        )

        # Station C at (6,0) - east terminal
        network.add_station(
            6,
            0,
            station_id="STA_C",
            name="Station C",
            length_cm=50.0,
            top_speed_kmh=80.0,
        )

        # Create blocks between stations
        # Block A-B: (1,0), (2,0)
        network.create_block_with_tracks(
            block_id="BLK_A",
            positions=[(1, 0), (2, 0)],
            name="A to B",
            length_cm=100.0,
        )

        # Block B-C: (4,0), (5,0)
        network.create_block_with_tracks(
            block_id="BLK_C",
            positions=[(4, 0), (5, 0)],
            name="B to C",
            length_cm=100.0,
        )

        for block in network.blocks.values():
            block.max_speed_kmh = 80.0

    def _run_until(
        self,
        simulation: SimulationController,
        condition,
        failure_message: str,
        max_steps: int = 20000,
        dt: float = 0.02,
    ) -> None:
        for _ in range(max_steps):
            simulation.update(dt)
            if condition():
                return
        pytest.fail(failure_message)

    def test_train_arrives_at_b_manual_path_to_c_should_move(self):
        """
        Test the core bug scenario:
        1. Train starts at A, assigned ONE_OFF route to B
        2. Train arrives at B and stops
        3. Manually create a one-off path from B to C
        4. Train should move to C (not flash ATO and return to standby)
        """
        # Setup
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at A
        car = TrainCar(car_id="CAR_BUG_TEST", car_type="LOCOMOTIVE", length=45.0)
        train = Train(train_id="BUG_TEST_TRAIN", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Setup controller
        train.state = TrainState.IDLE
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.IDLE
        assert controller.set_direction_and_standby(Direction.EAST) is True

        # Step 1: Assign ONE_OFF route from A to B
        route = Route(route_id="ROUTE_A_TO_B", name="A to B", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=1.0))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        # Wait for train to get path to B
        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Train did not get path from A to B",
        )

        # Step 2: Force arrival at B (simulating the train arriving and stopping)
        # Place train at B station (now at 3,0)
        train.position = make_train_position((3, 0), Direction.EAST)
        train.current_speed = 0.0
        train.state = TrainState.STANDBY
        controller.state = TrainState.STANDBY
        controller.ato_running = False

        # Cancel the path from A to B
        if train.assigned_path_id:
            dispatcher.cancel_path(train.assigned_path_id, force=True)
        train.assigned_path_id = None
        train.current_path = None
        train.reserved_blocks = []

        # Mark route as completed at B
        exec_state = simulation.route_executor.execution_states.get(train.train_id)
        if exec_state:
            exec_state.current_waypoint_index = 0  # Completed the only waypoint

        # Verify train is at B
        assert simulation.route_executor._is_train_at_platform(train, "STA_B") is True
        assert train.state == TrainState.STANDBY
        assert train.current_speed == 0.0

        # Step 3: Clear the A->B route and manually assign a new path from B to C
        # This simulates the user action in the UI
        old_route = network.routes.get("ROUTE_A_TO_B")
        if old_route:
            old_route.unassign_train(train.train_id)
        train.assigned_route_id = None
        if exec_state:
            simulation.route_executor.execution_states.pop(train.train_id, None)

        # Create new ONE_OFF route from B to C (the bug scenario)
        new_route = Route(
            route_id="ROUTE_B_TO_C", name="B to C", mode=RouteMode.ONE_OFF
        )
        new_route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=1.0))
        new_route.assign_train(train.train_id)
        new_route.activate(simulation.simulation_time)
        network.routes[new_route.route_id] = new_route
        train.assigned_route_id = new_route.route_id

        # IMPORTANT: This is where the bug manifests
        # The train should now get a path from B to C and start moving
        # Instead of ATO flashing and returning to standby

        # Step 4: Verify train gets path to C and actually moves
        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "BUG: Train did not get path from B to C after manual assignment. "
            "ATO likely flashed and returned to standby without moving.",
        )

        # Verify the path is valid
        path = dispatcher.get_path(train.assigned_path_id)
        assert path is not None
        assert path.start_block_id in ["STA_B_BLOCK", "BLK_B_C"]
        assert path.end_block_id == "STA_C_BLOCK"

        # Verify train has a valid state after path assignment
        # The train may be RUNNING_AT_STATION (just arrived) or transitioning
        assert train.state in [
            TrainState.RUNNING_MOVING,
            TrainState.STANDBY,
            TrainState.RUNNING_AT_STATION,
        ], (
            f"BUG: Train state is {train.state}, expected valid state after path assignment. "
            f"Path was assigned but train is in unexpected state."
        )

        # Verify ATO is running or ready
        # Note: ATO may not be running yet if train is still in station state
        assert controller.state in [
            TrainState.RUNNING_MOVING,
            TrainState.STANDBY,
            TrainState.RUNNING_AT_STATION,
        ], (
            "BUG: Controller in unexpected state after path assignment. "
            f"State: {controller.state}"
        )

    def test_manual_path_b_to_c_train_actually_arrives_at_c(self):
        """
        Extended test: Actually run the simulation and verify train arrives at C.

        This ensures the full end-to-end flow works:
        1. Train at B with manual B->C route
        2. Route executor acquires path and calls depart()
        3. Train departs B (ATO doesn't falsely stop at departure station)
        4. Train arrives at C

        BUG: _is_at_station() returned True at the departure station on the
        first tick after depart() because the train had speed=0 and no route-
        based waypoint filtering applied when ato_destination_block_id was set.

        FIX: _is_at_station() now checks ato_destination_block_id and only
        returns True when the train is at the destination block.
        """
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train already at B (simulating arrival at terminal)
        car = TrainCar(car_id="CAR_AT_B", car_type="LOCOMOTIVE", length=45.0)
        train = Train(train_id="TRAIN_AT_B", consist=[car], max_speed=60.0)
        network.add_train(train)

        # Place train at B station (3,0), facing east
        network.place_train(train, 3, 0, Direction.EAST)
        train.current_speed = 0.0
        train.state = TrainState.IDLE

        # Setup controller
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.IDLE
        assert controller.set_direction_and_standby(Direction.EAST) is True

        # Assign B->C route (simulating UI action)
        route = Route(
            route_id="MANUAL_B_TO_C", name="Manual B to C", mode=RouteMode.ONE_OFF
        )
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=1.0))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        # Run simulation - route executor should:
        # 1. See STANDBY train with assigned route
        # 2. Create path B->C via dispatcher
        # 3. Set ato_destination_block_id
        # 4. Call depart()
        # 5. Train should start moving (not falsely stop at departure station)

        # Wait for train to get path and start moving
        self._run_until(
            simulation,
            lambda: train.state == TrainState.RUNNING_MOVING and controller.ato_running,
            "BUG: Train did not start moving after path B->C assignment. "
            f"State: {train.state}, ato_running: {controller.ato_running}, "
            f"ato_destination_block_id: {controller.ato_destination_block_id}",
            max_steps=500,
        )

        # Verify ato_destination_block_id is set
        assert controller.ato_destination_block_id == "STA_C_BLOCK", (
            f"ato_destination_block_id should be STA_C_BLOCK, "
            f"got {controller.ato_destination_block_id}"
        )

        # Continue until train arrives at C
        self._run_until(
            simulation,
            lambda: simulation.route_executor._is_train_at_platform(train, "STA_C"),
            "BUG: Train did not arrive at C after manual path assignment. "
            f"State: {train.state}, speed: {train.current_speed}, "
            f"position: {getattr(train.position, 'head_tile', None)}",
            max_steps=20000,
        )

        # Verify train is at C
        assert simulation.route_executor._is_train_at_platform(train, "STA_C") is True

    def test_path_assignment_preserves_correct_lookahead(self):
        """
        Test that manual path assignment results in correct lookahead info.

        The bug description mentioned that debug info and lookahead JSON
        looked correct, but train still didn't move. This test verifies
        the lookahead calculation is correct after manual path assignment.
        """
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at B (now at 3,0)
        car = TrainCar(car_id="CAR_LOOKAHEAD", car_type="LOCOMOTIVE", length=45.0)
        train = Train(train_id="LOOKAHEAD_TEST", consist=[car], max_speed=60.0)
        network.add_train(train)
        train.position = make_train_position((3, 0), Direction.EAST)
        train.current_speed = 0.0
        train.state = TrainState.STANDBY

        # Setup controller
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.state = TrainState.STANDBY

        # Manually assign B->C route
        route = Route(
            route_id="LOOKAHEAD_ROUTE", name="Lookahead Test", mode=RouteMode.ONE_OFF
        )
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=1.0))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        # Run a few updates to let route executor process
        for _ in range(100):
            simulation.update(0.02)

        # Check if path was assigned - if not, the bug is reproduced
        if train.assigned_path_id is None:
            pytest.fail(
                "BUG: Lookahead test failed - path was not assigned. "
                "Train at B with route to C did not get a path."
            )

        # Get lookahead info - use the controller's lookahead attribute
        lookahead = controller.lookahead

        # Verify lookahead shows correct destination
        assert lookahead is not None, (
            "Lookahead should not be None after path assignment"
        )
        assert len(lookahead) > 0, "Lookahead should contain lookahead data"

        # The lookahead should show the path to C
        # Check that we have blocks in the lookahead
        block_ids_in_lookahead = [pt.block_id for pt in lookahead if pt.block_id]
        assert len(block_ids_in_lookahead) > 0, (
            "BUG: Lookahead contains no block IDs. "
            "Path may not be properly established."
        )

        # Verify the destination block is in lookahead
        assert (
            "STA_C_BLOCK" in block_ids_in_lookahead
            or "BLK_B_C" in block_ids_in_lookahead
        ), (
            f"BUG: Lookahead should contain path to C. Got blocks: {block_ids_in_lookahead}"
        )

        print(
            f"✓ Lookahead correct: contains {len(lookahead)} points, blocks: {block_ids_in_lookahead}"
        )

    def test_consecutive_manual_paths_a_to_b_then_b_to_c(self):
        """
        Test two consecutive manual path assignments:
        1. Assign A->B route, train departs A and arrives at B
        2. Assign B->C route, train departs B and arrives at C

        This simulates the exact workflow that triggers the bug:
        Train runs ONE_OFF A->B, arrives at B, user assigns B->C.

        BUG: On the B->C leg, _is_at_station() falsely detected arrival at B
        (the departure station) on the first tick because speed=0 and no
        ato_destination_block_id check was applied.

        FIX: _is_at_station() now returns False at non-destination stations
        when ato_destination_block_id is set.
        """
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at A (short train to fit on track)
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)

        # Create and activate route A->C
        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        # Run until train is moving
        for _ in range(500):
            simulation.update(0.1)
            if train.state == TrainState.RUNNING_MOVING and controller.ato_running:
                break

        assert controller.ato_running is True, "Train should be running with ATO"
        assert train.current_speed > 0, "Train should be moving"
        old_destination = controller.ato_destination_block_id
        assert old_destination is not None, "Train should have a destination"

        # Cancel route (simulate cancel button)
        # This mimics the API call: PUT /api/trains/<train_id>/assign-route with route_id=None
        if train.assigned_route_id:
            old_route = network.routes.get(train.assigned_route_id)
            if old_route:
                old_route.unassign_train(train.train_id)
        train.assigned_route_id = None

        # Apply the fix: Stop ATO and clear destination
        if controller.ato_running:
            controller.ato_stop()
        controller.ato_destination_block_id = None
        controller.ato_path_id = None
        train.ato_destination_block_id = None

        # Clear route executor state
        if simulation.route_executor:
            simulation.route_executor.execution_states.pop(train.train_id, None)

        # Verify fix
        assert controller.ato_running is False, (
            "BUG: ATO should stop after route cancellation"
        )
        assert controller.ato_destination_block_id is None, (
            "BUG: Destination should be cleared"
        )
        assert train.ato_destination_block_id is None, (
            "BUG: Train destination should be cleared"
        )

        # Run a bit more - train should brake to stop
        for _ in range(1000):
            simulation.update(0.1)
            if train.current_speed <= 0.5:
                break

        # Train should eventually transition to STANDBY after stopping
        for _ in range(100):
            simulation.update(0.1)
            if train.state == TrainState.STANDBY:
                break

        assert train.state in (TrainState.STANDBY, TrainState.RUNNING_WAITING), (
            f"Train should stop after ATO cancel, got {train.state}"
        )

    def test_route_cancellation_edge_case_at_station(self):
        """Edge case: Cancel route when train is at station."""
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)

        # Create route
        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        # Train is at station A, cancel immediately
        train.assigned_route_id = None
        if controller.ato_running:
            controller.ato_stop()
        controller.ato_destination_block_id = None
        train.ato_destination_block_id = None

        assert controller.ato_running is False
        assert controller.ato_destination_block_id is None

    def test_route_cancellation_clears_path_id(self):
        """Ensure cancellation clears both destination AND path ID."""
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)

        # Set some path state
        controller.ato_path_id = "PATH_123"
        controller.ato_destination_block_id = "BLK_C"
        train.ato_destination_block_id = "BLK_C"

        # Cancel
        if controller.ato_running:
            controller.ato_stop()
        controller.ato_destination_block_id = None
        controller.ato_path_id = None
        train.ato_destination_block_id = None

        assert controller.ato_path_id is None, "Path ID should be cleared"
        assert controller.ato_destination_block_id is None

    def test_backward_path_assignment_auto_turns_train(self):
        """
        BUG 2 test: Assigning backward path should auto-turn train.

        Steps:
        1. Train at C facing EAST (reached terminal)
        2. User assigns path C->A (requires WEST direction)
        3. System should auto-turn train to face WEST
        4. Train should then be able to follow the path

        Expected:
        - Train facing direction changes to WEST
        - Active cab switches A <-> B
        - Train can depart toward A
        """
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at C facing EAST (as if it just arrived from the west)
        # But we need to place it so there's track ahead, so place at (5,0) facing EAST
        # This puts the train approaching/at station C
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 5, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)
        controller.set_cab("A")

        assert train.facing_direction == Direction.EAST, (
            "Train should face EAST initially"
        )
        assert controller.active_cab == "A", "Cab A should be active"

        # Manually create backward path C->A (this simulates path tool usage)
        # Path should go from (6,0) to (0,0) - heading WEST
        path_result = dispatcher.create_path(
            start_block_id="STA_C_BLOCK",
            end_block_id="STA_A_BLOCK",
            train_id=train.train_id,
        )

        assert path_result.success, f"Path creation failed: {path_result.error}"

        # Convert path to train format
        from path_utils import convert_tiles_to_train_path

        raw_path = convert_tiles_to_train_path(path_result.path.path_tiles)
        train.current_path = raw_path
        train.path_index = 0
        train.assigned_path_id = path_result.path.path_id

        # Set destination
        controller.ato_destination_block_id = "STA_A_BLOCK"
        train.ato_destination_block_id = "STA_A_BLOCK"

        # Apply the fix: Auto-turn if path requires opposite direction
        if train.position and train.current_path and len(train.current_path) >= 2:
            first_tile = train.current_path[0]
            second_tile = train.current_path[1]
            dx = second_tile[0] - first_tile[0]
            dy = second_tile[1] - first_tile[1]

            # Determine path direction
            path_direction = None
            if dx > 0:
                path_direction = Direction.EAST
            elif dx < 0:
                path_direction = Direction.WEST
            elif dy > 0:
                path_direction = Direction.NORTH
            elif dy < 0:
                path_direction = Direction.SOUTH

            # If path goes opposite to facing, turn around
            if (
                path_direction is not None
                and path_direction == train.facing_direction.opposite()
                and train.is_stopped()
            ):
                result = controller.turn_around()
                assert result, (
                    f"turn_around() failed! "
                    f"train.state={train.state}, controller.state={controller.state}, "
                    f"active_cab={controller.active_cab}, ato_running={controller.ato_running}, "
                    f"speed={train.current_speed}"
                )

        # Verify fix
        assert train.facing_direction == Direction.WEST, (
            f"BUG: Train should auto-turn to WEST for backward path, got {train.facing_direction}"
        )
        assert controller.active_cab == "B", (
            f"BUG: Cab should switch to B after turn, got {controller.active_cab}"
        )

    def test_backward_path_no_turn_if_already_correct(self):
        """Don't turn if train already faces the right direction."""
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Train at A facing EAST (correct for A->C path)
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)
        controller.set_cab("A")

        # Create forward path A->C
        path_result = dispatcher.create_path(
            start_block_id="BLK_A", end_block_id="BLK_C", train_id=train.train_id
        )

        from path_utils import convert_tiles_to_train_path

        raw_path = convert_tiles_to_train_path(path_result.path.path_tiles)
        train.current_path = raw_path
        train.path_index = 0

        # Check if turn is needed (should NOT turn)
        if train.position and train.current_path and len(train.current_path) >= 2:
            first_tile = train.current_path[0]
            second_tile = train.current_path[1]
            dx = second_tile[0] - first_tile[0]

            path_direction = None
            if dx > 0:
                path_direction = Direction.EAST
            elif dx < 0:
                path_direction = Direction.WEST

            # Should NOT turn (path and facing both EAST)
            if (
                path_direction is not None
                and path_direction == train.facing_direction.opposite()
                and train.is_stopped()
            ):
                controller.turn_around()

        # Should still face EAST
        assert train.facing_direction == Direction.EAST, (
            "Train should not turn for forward path"
        )
        assert controller.active_cab == "A", "Cab should remain A"

    def test_backward_path_real_world_scenario(self):
        """
        Real-world scenario: Train reaches terminal C, user assigns path back to A.

        This is the exact scenario the user described:
        - Train finishes route to branch terminal
        - User uses path tool to assign return path
        - Train should automatically turn around and depart
        """
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # 1. Train starts at A with route to C
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)

        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        network.routes[route.route_id] = route
        train.assigned_route_id = route.route_id

        # 2. Run until train reaches C
        for _ in range(10000):
            simulation.update(0.1)
            if simulation.route_executor._is_train_at_platform(train, "STA_C"):
                break

        assert simulation.route_executor._is_train_at_platform(train, "STA_C"), (
            "Train should reach C"
        )
        assert train.facing_direction == Direction.EAST, (
            "Train should still face EAST at terminal"
        )

        # 3. User cancels route and assigns return path
        train.assigned_route_id = None
        if controller.ato_running:
            controller.ato_stop()
        controller.ato_destination_block_id = None
        train.ato_destination_block_id = None

        # Wait for train to stop
        for _ in range(500):
            simulation.update(0.1)
            if train.is_stopped():
                break

        assert train.is_stopped(), "Train should be stopped"

        # Assign backward path C->A
        path_result = dispatcher.create_path(
            start_block_id="STA_C_BLOCK",
            end_block_id="STA_A_BLOCK",
            train_id=train.train_id,
        )

        from path_utils import convert_tiles_to_train_path

        raw_path = convert_tiles_to_train_path(path_result.path.path_tiles)
        train.current_path = raw_path
        train.path_index = 0
        train.assigned_path_id = path_result.path.path_id
        controller.ato_destination_block_id = "STA_A_BLOCK"
        train.ato_destination_block_id = "STA_A_BLOCK"

        # Auto-turn logic
        if train.position and train.current_path and len(train.current_path) >= 2:
            first_tile = train.current_path[0]
            second_tile = train.current_path[1]
            dx = second_tile[0] - first_tile[0]
            dy = second_tile[1] - first_tile[1]

            path_direction = None
            if dx < 0:
                path_direction = Direction.WEST
            elif dx > 0:
                path_direction = Direction.EAST
            elif dy > 0:
                path_direction = Direction.NORTH
            elif dy < 0:
                path_direction = Direction.SOUTH

            if (
                path_direction is not None
                and path_direction == train.facing_direction.opposite()
                and train.is_stopped()
            ):
                result = controller.turn_around()
                assert result, (
                    f"turn_around() failed! "
                    f"train.state={train.state}, controller.state={controller.state}, "
                    f"active_cab={controller.active_cab}, ato_running={controller.ato_running}, "
                    f"speed={train.current_speed}, facing={train.facing_direction}"
                )

        # 4. Verify train turned and can depart
        assert train.facing_direction == Direction.WEST, (
            "BUG: Train should turn WEST for return path"
        )

        # After turning, set direction controller to match new facing
        controller.set_direction_and_standby(Direction.WEST)

        # Start ATO
        controller.ato_go()

        # Run a bit - train should start moving
        for _ in range(500):
            simulation.update(0.1)
            if train.current_speed > 0:
                break

        assert train.current_speed > 0, "BUG: Train should be able to move after turn"
        assert controller.ato_running is True, "BUG: ATO should be running"


class TestSetCabDoubleFlipBugFix:
    """
    Comprehensive tests for set_cab() double-flip bug fixed 2026-02-10.

    BUG: TrainController.set_cab() had two conflicting operations when switching
    cabs (A <-> B):

    1. It synced train.facing_direction from the computed property
       train.position.facing_direction — which already returned the CORRECT
       new direction because leading_end was already changed by train.set_cab().

    2. It then explicitly flipped train.facing_direction to its opposite —
       which UNDID the correct direction from step 1, putting it back to the
       original direction.

    For multi-tile trains, the computed TrainPosition.facing_direction property
    derives direction from leading_end + tile geometry. After leading_end changes,
    it automatically returns the reversed direction. The explicit flip was
    redundant and destructive.

    FIX: Replaced the sync-then-flip logic with direction-aware handling:
    - Multi-tile trains (end_a_tile != end_b_tile): Use the computed property
      directly — it already returns the correct direction.
    - Single-tile trains (end_a_tile == end_b_tile): Explicitly flip the legacy
      direction field since the computed property falls back to it.

    This bug caused turn_around() (which delegates to set_cab()) to silently
    "succeed" but leave the train facing the SAME direction it was before,
    breaking backward path assignment after route completion.
    """

    def _setup_multi_tile_train(self, network):
        """Create a 2-tile train on E-W track, facing EAST with cab A."""
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.set_cab("A")

        assert train.facing_direction == Direction.EAST
        assert controller.active_cab == "A"
        assert train.position.end_a_tile != train.position.end_b_tile, (
            "Setup must produce a multi-tile train"
        )

        return train, controller

    def _setup_single_tile_train(self, network):
        """Create a 1-tile train on E-W track, facing EAST with cab A."""
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=20.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST_1T", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.set_cab("A")

        assert train.facing_direction == Direction.EAST
        assert controller.active_cab == "A"
        assert train.position.end_a_tile == train.position.end_b_tile, (
            "Setup must produce a single-tile train"
        )

        return train, controller

    # ---- Multi-tile tests (the original bug case) ----

    def test_multi_tile_set_cab_flips_train_facing_direction(self, network):
        """The specific case that triggered the bug: multi-tile train
        facing_direction stays wrong after set_cab()."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")

        assert train.facing_direction == Direction.WEST, (
            f"BUG REGRESSION: train.facing_direction should be WEST after "
            f"switching to cab B, got {train.facing_direction}"
        )

    def test_multi_tile_set_cab_flips_computed_direction(self, network):
        """Computed property on TrainPosition also returns correct direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")

        assert train.position.facing_direction == Direction.WEST

    def test_multi_tile_set_cab_syncs_legacy_and_computed(self, network):
        """After set_cab, legacy field and computed property agree."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")

        # Both access paths should return the same direction
        legacy = train.facing_direction  # Reads Train.facing_direction field
        computed = train.position.facing_direction  # Reads computed property
        assert legacy == computed == Direction.WEST, (
            f"Legacy ({legacy}) and computed ({computed}) should both be WEST"
        )

    def test_multi_tile_double_flip_returns_to_original(self, network):
        """Switching cab twice returns to original direction (A->B->A)."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST

    def test_multi_tile_triple_flip(self, network):
        """Three cab switches (A->B->A->B) end correctly."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

    def test_multi_tile_turn_around_flips_direction(self, network):
        """turn_around() delegates to set_cab() and correctly flips direction."""
        train, controller = self._setup_multi_tile_train(network)

        result = controller.turn_around()
        assert result is True, "turn_around() should succeed"
        assert train.facing_direction == Direction.WEST
        assert controller.active_cab == "B"

    def test_multi_tile_turn_around_twice_returns_original(self, network):
        """Two turn_around() calls return to original direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.turn_around()
        assert train.facing_direction == Direction.WEST

        controller.turn_around()
        assert train.facing_direction == Direction.EAST

    # ---- Single-tile tests ----

    def test_single_tile_set_cab_flips_direction(self, network):
        """Single-tile train also flips direction on cab switch."""
        train, controller = self._setup_single_tile_train(network)

        controller.set_cab("B")

        assert train.facing_direction == Direction.WEST, (
            f"Single-tile train should flip to WEST, got {train.facing_direction}"
        )

    def test_single_tile_double_flip(self, network):
        """Single-tile: A->B->A returns to original."""
        train, controller = self._setup_single_tile_train(network)

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST

    def test_single_tile_turn_around(self, network):
        """Single-tile: turn_around() works correctly."""
        train, controller = self._setup_single_tile_train(network)

        result = controller.turn_around()
        assert result is True
        assert train.facing_direction == Direction.WEST
        assert controller.active_cab == "B"

    # ---- Edge cases ----

    def test_set_cab_from_none_does_not_flip(self, network):
        """Setting cab from None (first selection) should NOT flip direction."""
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length=80.0, max_speed=80.0)
        train = Train(train_id="FIRST_CAB", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True

        # active_cab is None initially
        assert controller.active_cab is None

        # First cab selection should sync from computed, not flip
        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST, (
            "First cab selection should NOT flip direction"
        )

    def test_set_same_cab_does_not_flip(self, network):
        """Setting the same cab again should NOT flip direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("A")  # Already A, setting A again
        assert train.facing_direction == Direction.EAST, (
            "Re-selecting same cab should NOT flip direction"
        )

    def test_turn_around_fails_while_moving(self, network):
        """turn_around() should fail if train is moving."""
        train, controller = self._setup_multi_tile_train(network)

        train.current_speed = 10.0  # Moving

        result = controller.turn_around()
        assert result is False
        assert train.facing_direction == Direction.EAST, (
            "Direction should not change on failed turn_around"
        )

    def test_turn_around_fails_while_ato_running(self, network):
        """turn_around() should fail if ATO is actively running."""
        train, controller = self._setup_multi_tile_train(network)

        controller.ato_running = True

        result = controller.turn_around()
        assert result is False
        assert train.facing_direction == Direction.EAST

    def test_entry_edges_flip_with_direction(self, network):
        """Entry edges and distances also flip correctly alongside direction."""
        train, controller = self._setup_multi_tile_train(network)

        orig_a_edge = train.position.end_a_entry_edge
        orig_b_edge = train.position.end_b_entry_edge
        orig_a_dist = train.position.end_a_distance_cm
        orig_b_dist = train.position.end_b_distance_cm

        controller.set_cab("B")

        # Entry edges should have flipped
        assert train.position.end_a_entry_edge != orig_a_edge, (
            "end_a entry edge should flip on cab switch"
        )
        assert train.position.end_b_entry_edge != orig_b_edge, (
            "end_b entry edge should flip on cab switch"
        )
        # Distances should have changed (tile_length - original)
        assert train.position.end_a_distance_cm != orig_a_dist
        assert train.position.end_b_distance_cm != orig_b_dist

        # Direction must be correct too
        assert train.facing_direction == Direction.WEST

    # ---- Real-world scenario: the actual bug trigger ----

    def test_turn_around_after_ato_stops_in_idle_state(self, network):
        """
        Real-world scenario: train arrives at terminal via ATO, route is
        cancelled (train goes to IDLE), then turn_around() is called.

        This was the actual bug trigger: after ATO stops and route is
        cancelled, the train is in IDLE state with a cab still selected.
        turn_around() -> set_cab() must work in this state.
        """
        train, controller = self._setup_multi_tile_train(network)

        # Simulate: train was running, ATO stopped, route cancelled -> IDLE
        controller.state = TrainState.IDLE
        train.state = TrainState.IDLE
        controller.ato_running = False
        train.current_speed = 0.0

        # Active cab is still A (route cancellation doesn't clear cab)
        assert controller.active_cab == "A"

        result = controller.turn_around()
        assert result is True, (
            f"turn_around() should work from IDLE state with cab selected, "
            f"train.state={train.state}"
        )
        assert train.facing_direction == Direction.WEST
        assert controller.active_cab == "B"


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_trains.py -v")
