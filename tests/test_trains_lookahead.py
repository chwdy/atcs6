"""
Lookahead and monitor tests - Monitor, lookahead, UI data

Run with: pytest tests/test_trains_lookahead.py -v
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
from train_route import Route, RouteMode
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




class TestTrainControllerMonitorLookahead:
    """
    Tests for TrainController lookahead payload used by Train Monitor.

    Ensures head-relative block offsets remain stable across tile transitions and
    cab direction changes are reflected in the lookahead head tile.
    """



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
            leading_end=LeadingEnd.A,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        controller.set_cab("A")
        payload_a = controller.get_lookahead()
        # With Cab A active, head is at end_a which is (0, 0)
        assert payload_a["head_tile"] == [0, 0]

        controller.set_cab("B")
        payload_b = controller.get_lookahead()
        # After switching to Cab B, tiles don't swap - physical train doesn't move
        # end_a stays at (0, 0) and end_b stays at (2, 0)
        # With Cab B active and leading_end=LeadingEnd.B, head is at end_b which is (2, 0)
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
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab(None)

        payload = controller.get_lookahead()

        assert payload["cab"] is None
        assert payload["head_tile"] is None
        assert payload["speed_limit_reason"] == "No cab selected"
        assert payload["lookahead"]["blocks"] == []
        assert payload["lookahead"]["signals"] == []
        assert payload["lookahead"]["switches"] == []
        assert payload["lookahead"]["stations"] == []
        assert payload["lookahead"]["clearance_cm"] == pytest.approx(0.0)







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
            leading_end=LeadingEnd.A,
        )
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
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

        payload = controller.get_lookahead()
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

        payload = controller.get_lookahead()
        block_ids = [block["id"] for block in payload["lookahead"]["blocks"]]

        assert payload["head_tile"] == [0, 0]
        assert block_ids == ["BLOCK_0"], (
            f"Lookahead should stop before foreign-locked switch. Got blocks: {block_ids}"
        )



class TestTrainControlPanelDataBugFix:
    """
    Comprehensive tests for train control panel data propagation fix (2026-01-22).

    BUG: /api/network returned train data without controller state, so the
    train control panel could not reflect power/handle status or ATO path info.

    FIX: /api/network exposes a sibling `controllers_by_train_id` map per
    design_new.md "Data Flow To UI" (~line 2065).
    """

    def test_network_includes_controller_sibling(self):
        """Network response should include controllers_by_train_id sibling map."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.assigned_path_id = "PATH_1"
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        controller.power_on = True
        controller.handle_position = HandlePosition.P2
        controller.control_mode = "ato"

        payload = build_network_payload(network, sim_controller)
        controllers = payload["controllers_by_train_id"]
        assert "TRAIN_01" in controllers
        controller_data = controllers["TRAIN_01"]
        assert controller_data["power_on"] is True
        assert controller_data["handle_position"] == "P2"
        assert controller_data["control_mode"] == "ato"
        assert "ato_info" in controller_data



class TestEmergencyResetButtonDataBugFix:
    """
    Comprehensive tests for emergency reset button data fix (2026-01-24).

    BUG: Controller payload only included `eb_active`, while the control
    panel expects `emergency_stop_active`, so the reset button never
    appears even when emergency braking is active.

    FIX: Provide `emergency_stop_active` in controller snapshots alongside
    `eb_active`.
    """

    def test_controller_payload_includes_emergency_stop_active(self):
        """Controller payload should expose emergency_stop_active for UI state."""
        from simulation import build_network_payload

        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim_controller = SimulationController(network)
        controller = sim_controller._get_or_create_controller(train)
        controller.eb_active = True

        payload = build_network_payload(network, sim_controller)
        controller_data = payload["controllers_by_train_id"]["TRAIN_01"]
        assert controller_data["emergency_stop_active"] is True



class TestTrainAssignedPathSerializationBugFix:
    """
    Comprehensive tests for assigned_path_id serialization fix (2026-01-22).

    BUG: Train.to_dict omitted assigned_path_id, causing frontend to show "No path"
    after UI refresh or cab switch.

    FIX: Train.to_dict includes assigned_path_id for frontend state.
    """


    def test_train_physics_power_off_does_not_update_speed(self):
        """TrainPhysics.tick should not update speed when power is off.

        v1: controller owns canonical current_speed_kmh; physics reads from
        controller.current_speed_kmh, not controller.current_speed_kmh.
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_LOG_2", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)
        controller.power_on = False
        # v1: write canonical speed onto the controller; controller.current_speed_kmh is mirrored.
        controller.current_speed_kmh = 10.0
        controller.current_speed_kmh = 10.0

        initial_speed = controller.current_speed_kmh
        physics.tick(0.1)

        # Speed should remain unchanged when power is off
        assert controller.current_speed_kmh == initial_speed
        assert controller.current_speed_kmh == initial_speed




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
        # v1: state is a computed @property; set primitive occupied_by instead.
        block = Block(block_id="TERMINAL", length_cm=150.0)
        block.occupied_by = "TRAIN_FALLBACK"
        network.blocks["TERMINAL"] = block

        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
            track.block_section_id = "TERMINAL"

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
        lookahead = controller.get_lookahead()

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
            head_distance_cm=25.0,  # Halfway through middle tile
        )
        train.position.occupied_tiles = [(0, 0), (1, 0)]
        train.position.occupied_blocks = ["TERMINAL"]
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        lookahead = controller.get_lookahead()
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
            head_distance_cm=40.0,  # Near end of last tile
        )
        train.position.occupied_tiles = [(0, 0), (1, 0), (2, 0)]
        train.position.occupied_blocks = ["TERMINAL"]
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        lookahead = controller.get_lookahead()
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
            head_distance_cm=25.0,
        )
        train.position.occupied_tiles = [(0, 0)]
        train.position.occupied_blocks = ["TERMINAL"]
        train.current_path = None
        train.reserved_blocks = []

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        controller.set_cab("A")

        lookahead = controller.get_lookahead()
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

        lookahead = controller.get_lookahead()
        clearance_cm = lookahead["lookahead"]["clearance_cm"]

        # Should use the prior clearance value
        assert clearance_cm == 200.0



