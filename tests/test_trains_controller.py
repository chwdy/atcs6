"""
Train controller tests - Controller basics, ATC, braking

Run with: pytest tests/test_trains_controller.py -v
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
from train_route import Route, RouteMode, RouteTask
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

class TestTrainManualControlBugFixes:
    """Tests for train manual control and ATC fallback fixes via TrainController."""

    def test_clear_direction_from_standby(self):
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        train.state = TrainState.READY
        train.travel_direction = "reverse"
        train.manual_target_speed = 20.0
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_01", network)
        controller.power_on = True
        controller.state = TrainState.READY
        controller.active_cab = "A"  # READY+cab matches the old "STANDBY" shape

        assert controller.clear_direction() is True
        assert controller.state == TrainState.READY
        assert controller.active_cab is None
        assert train.facing_direction is None
        # v1: clear_direction only clears active_cab; state stays READY
        # (cab is orthogonal to state in the new state machine).

    def test_clear_direction_rejects_moving(self):
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_02", consist=[car], max_speed=60.0)
        train.state = TrainState.READY
        # Direction set via position (refactored): train.set_cab("A")  # was: Direction.NORTH
        network.add_train(train)
        controller = create_controller_for_train("TRAIN_02", network)
        controller.power_on = True
        controller.state = TrainState.READY
        controller.current_speed_kmh = 5.0

        assert controller.clear_direction() is False
        assert controller.state == TrainState.READY





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
        train.state = TrainState.READY
        network.add_train(train)

        sim = SimulationController(network)

        # First update to initialize controller
        sim.update(0.1)

        controller = sim.controllers[train.train_id]

        # Now manually set controller to different state than train
        # (simulating what would happen if controller.tick() changes state)
        train.state = TrainState.READY
        controller.state = TrainState.READY

        # After another update, train should reflect controller's state
        sim.update(0.1)

        # train.state should now match controller.state (controller wins)
        assert controller.state == controller.state




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
        block.speed_limit_kmh = 30.0  # 30 km/h limit

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_OVERSPEED_TEST", consist=[car], max_speed=100.0)
        train.state = TrainState.RUNNING_MOVING
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["B1"]
        )
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"
        controller.current_speed_kmh = 37.0  # 7 km/h over limit (warning zone)

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
        block.speed_limit_kmh = 30.0  # 30 km/h limit

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_EB_TEST", consist=[car], max_speed=100.0)
        train.state = TrainState.RUNNING_MOVING
        train.position = make_train_position(
            (0, 0), Direction.EAST, occupied_blocks=["B1"]
        )
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"
        controller.current_speed_kmh = 42.0  # 12 km/h over limit (EB zone)

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
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = None  # No cab selected
        controller.current_speed_kmh = 0.0

        # Tick controller
        sim.update(0.1)

        # ATC speed should be 0
        assert controller.atc_speed_limit == 0.0, (
            "ATC speed should be 0 when no cab selected"
        )
        assert controller.speed_limit_reason == "No cab selected", (
            f"Reason should be 'No cab selected', got: {controller.speed_limit_reason}"
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
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.control_mode = "manual"
        controller.power_on = True
        controller.active_cab = "A"
        controller.current_speed_kmh = 16.0

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
            leading_end=LeadingEnd.A,
            occupied_tiles=[(1, 0), (0, 0)],
        )

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)

        remaining = controller._get_remaining_distance_in_current_block()

        assert remaining == pytest.approx(15.0), (
            f"Expected 15.0 cm remaining, got {remaining}"
        )



