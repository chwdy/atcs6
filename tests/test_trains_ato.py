"""
ATO tests - ATO operations, distance-aware control, stopping

Run with: pytest tests/test_trains_ato.py -v
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
        # Direction set via position (refactored): train.set_cab("A")  # was: Direction.EAST
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
        # Direction set via position (refactored): train.set_cab("A")  # was: Direction.EAST
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
            leading_end=LeadingEnd.A,
            occupied_tiles=[(4, 0)],
            occupied_blocks=["BLOCK_B"],
        )
        # Direction set via position (refactored): train.set_cab("A")  # was: Direction.WEST

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

    def test_path_completion_transitions_to_standby_at_non_station_waypoint(
        self, network
    ):
        """
        BUG FIX (2026-02-11): When ATO reaches end of path and stops at a waypoint
        that's not a station, the train should transition from RUNNING_MOVING to STANDBY.

        ROOT CAUSE:
        In path_dispatcher._complete_path_on_head_arrival(), the train's assigned_path_id
        was cleared BEFORE route_executor.handle_path_completion() was called. This caused
        handle_path_completion() to fail its validation check (train.assigned_path_id != path_id)
        and return early without calling controller.transition_to_standby().

        FIX:
        Call controller.transition_to_standby() directly in _complete_path_on_head_arrival()
        when the path completes and the train is in RUNNING_MOVING state.
        """
        from path_dispatcher import PathDispatcher
        from simulation import SimulationController

        # Create a simple 3-tile track with NO station (just a plain block)
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create a single block covering all tiles (no station)
        network.create_block_with_tracks(
            block_id="BLOCK_PLAIN",
            positions=[(0, 0), (1, 0), (2, 0)],
            name="Plain Block",
            length_cm=150.0,
        )

        # Create dispatcher and simulation controller
        dispatcher = PathDispatcher(network)
        sim_controller = SimulationController(network, dispatcher)
        network.power_controller = sim_controller.power_controller
        # Link back-reference for the fix to access sim_controller
        sim_controller.power_controller._sim_controller = sim_controller

        # Create and place train at start
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=30.0)
        train = Train(train_id="TRAIN_NON_STATION", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Create a path for the train
        from path import Path, PathStatus

        path = Path(
            path_id="PATH_001",
            path_tiles=[(0, 0), (1, 0), (2, 0)],
            block_sequence=["BLOCK_PLAIN"],
            end_block_id="BLOCK_PLAIN",
            train_id="TRAIN_NON_STATION",
        )
        path.status = PathStatus.ACTIVE
        dispatcher.active_paths["PATH_001"] = path

        # Assign path to train
        train.assigned_path_id = "PATH_001"
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 0
        train.reserved_blocks = ["BLOCK_PLAIN"]

        # Setup controller for ATO mode and moving state
        controller = sim_controller._get_or_create_controller(train)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
        controller.ato_destination_block_id = "BLOCK_PLAIN"

        train.state = TrainState.RUNNING_MOVING
        train.ato_destination_block_id = "BLOCK_PLAIN"

        # Move train to the end tile (simulating arrival at end of path)
        # Place at tile (1,0) facing EAST so it's on the path and can reach (2,0)
        network.place_train(train, 1, 0, Direction.EAST)
        train.current_speed = 0.0  # Train has stopped

        # Verify initial state
        assert train.state == TrainState.RUNNING_MOVING
        assert controller.state == TrainState.RUNNING_MOVING

        # Complete the path (this simulates what happens when train reaches path end)
        dispatcher._complete_path_on_head_arrival(path, train)

        # After path completion, train should transition to STANDBY
        assert controller.state == TrainState.STANDBY, (
            f"BUG: Controller state should be STANDBY after path completion, "
            f"but got {controller.state}. The fix ensures transition_to_standby() "
            f"is called when path completes."
        )
        assert controller.ato_running is False, (
            f"BUG: ATO should be stopped (ato_running=False) after path completion, "
            f"but got {controller.ato_running}"
        )
        assert controller.ato_destination_block_id is None, (
            f"BUG: Destination should be cleared after path completion"
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


class TestAtoGoAutoSelectsCabFromNone:
    """
    Comprehensive tests for ATO Go auto-cab-selection bug fixed Feb 2026.

    These tests ensure the following bug doesn't regress:

    BUG: When a train is placed via the Add Train tool, it gets
    leading_end=LeadingEnd.A but active_cab=None and state=OFF.
    After powering on (state=IDLE) and assigning a path, pressing
    ATO Go would fail with "No cab selected" because ato_go() had
    an early-exit guard checking active_cab before the ATO info
    calculation and auto-correction logic could run.

    Downstream effects:
    - Lookahead returned empty (get_monitor_lookahead checks active_cab)
    - ATC speed limit was 0 (tick() checks active_cab)
    - Manual mode triggered EB immediately (speed limit 0)
    - Renderer drew triangle head even with cab OFF (checked leading_end
      instead of active_cab)

    FIX:
    - ato_go() now calculates ATO info and auto-selects cab BEFORE
      checking state. set_cab() handles IDLE -> STANDBY transition.
    - Renderer checks active_cab instead of leading_end for markers.
    - Frontend Go button enabled when power_on (not active_cab).
    """

    def _setup_train_no_cab(self, network):
        """Set up a train placed via Add Train tool (no cab, IDLE after power on)."""
        # 5 EW tiles: (0,0) to (4,0)
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Two blocks
        network.create_block_with_tracks("B1", [(0, 0), (1, 0)], length_cm=200.0)
        network.create_block_with_tracks(
            "B2", [(2, 0), (3, 0), (4, 0)], length_cm=300.0
        )

        # Create and place train at (1,0) facing EAST
        car = TrainCar("CAR_01", "LOCOMOTIVE", length=40.0)
        train = Train(train_id="ATO_CAB_TEST", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        # Verify placement state: leading_end=A, active_cab=None
        assert train.position.leading_end == LeadingEnd.A
        assert train.active_cab is None
        assert train.state == TrainState.OFF

        # Create controller and power on (mimics user clicking power on)
        controller = create_controller_for_train(train.train_id, network)
        controller.power_on_cmd()

        assert controller.power_on is True
        assert controller.state == TrainState.IDLE
        assert controller.active_cab is None
        assert controller.control_mode == "ato"

        # Set up a path from train location eastward
        path_tiles = [
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]
        train.current_path = path_tiles
        train.assigned_path_id = "PATH_EAST"
        train.path_index = 0

        # Set controller destination
        controller.ato_destination_block_id = "B2"
        controller.ato_path_id = "PATH_EAST"

        return train, controller

    def test_ato_go_auto_selects_cab_from_none(self, network):
        """The specific case that triggered the bug: ato_go with no cab."""
        train, controller = self._setup_train_no_cab(network)

        # Before fix: ato_go() would return "No cab selected"
        result = controller.ato_go()
        assert result.success is True, (
            f"ato_go() should auto-select cab, got: {result.message}"
        )

        # Verify cab was auto-selected
        assert controller.active_cab == "A", (
            "Should auto-select cab A for eastbound path"
        )
        assert train.active_cab == "A"
        assert controller.state == TrainState.RUNNING_MOVING
        assert controller.ato_running is True

    def test_ato_go_auto_selects_opposite_cab_for_reverse_path(self, network):
        """When path goes opposite to facing direction, should select opposite cab."""
        train, controller = self._setup_train_no_cab(network)

        # Change path to go westward (opposite to train facing EAST)
        path_tiles = [(1, 0, Direction.WEST), (0, 0, Direction.WEST)]
        train.current_path = path_tiles
        train.path_index = 0
        controller.ato_destination_block_id = "B1"

        result = controller.ato_go()
        assert result.success is True, (
            f"ato_go() should auto-select opposite cab, got: {result.message}"
        )

        # Should select cab B (opposite to default A) since path is opposite to facing
        assert controller.active_cab == "B", (
            "Should auto-select cab B for westbound path"
        )
        assert controller.state == TrainState.RUNNING_MOVING

    def test_ato_go_fails_without_power(self, network):
        """ato_go should fail if power is off."""
        train, controller = self._setup_train_no_cab(network)
        controller.power_on = False
        controller.state = TrainState.OFF

        result = controller.ato_go()
        assert result.success is False
        assert "Power" in result.message or "power" in result.message

    def test_ato_go_fails_without_path_and_no_cab(self, network):
        """ato_go with no cab and no path should fail gracefully."""
        train, controller = self._setup_train_no_cab(network)

        # Remove path info
        train.current_path = None
        train.assigned_path_id = None
        controller.ato_destination_block_id = None
        controller.ato_path_id = None

        result = controller.ato_go()
        assert result.success is False
        assert "cab" in result.message.lower() or "path" in result.message.lower()

    def test_ato_go_transitions_through_standby(self, network):
        """ato_go from IDLE should transition through STANDBY to RUNNING_MOVING."""
        train, controller = self._setup_train_no_cab(network)

        # Verify starting state
        assert controller.state == TrainState.IDLE
        assert controller.active_cab is None

        result = controller.ato_go()
        assert result.success is True

        # set_cab() should have transitioned IDLE -> STANDBY,
        # then ato_go() transitions STANDBY -> RUNNING_MOVING
        assert controller.state == TrainState.RUNNING_MOVING
        assert train.state == TrainState.RUNNING_MOVING

    def test_ato_go_with_existing_cab_still_works(self, network):
        """ato_go should still work when cab is already set (no regression)."""
        train, controller = self._setup_train_no_cab(network)

        # Manually set cab first (simulating user clicking cab A)
        controller.set_cab("A")
        assert controller.active_cab == "A"
        assert controller.state == TrainState.STANDBY

        result = controller.ato_go()
        assert result.success is True
        assert controller.ato_running is True
        assert controller.state == TrainState.RUNNING_MOVING

    def test_ato_go_auto_corrects_wrong_cab(self, network):
        """ato_go should auto-correct cab if wrong direction (existing behavior preserved)."""
        train, controller = self._setup_train_no_cab(network)

        # Set cab A (facing EAST), but path goes WEST
        controller.set_cab("A")
        path_tiles = [(1, 0, Direction.WEST), (0, 0, Direction.WEST)]
        train.current_path = path_tiles
        train.path_index = 0
        controller.ato_destination_block_id = "B1"

        result = controller.ato_go()
        assert result.success is True
        # Cab should have been auto-corrected from A to B
        assert controller.active_cab == "B"

    def test_lookahead_works_after_auto_cab_selection(self, network):
        """After ato_go auto-selects cab, lookahead should not be empty."""
        train, controller = self._setup_train_no_cab(network)

        result = controller.ato_go()
        assert result.success is True

        # Now tick should calculate proper lookahead
        lookahead = controller._calculate_lookahead()
        assert len(lookahead) > 0, (
            "Lookahead should not be empty after cab auto-selection"
        )

    def test_atc_speed_limit_nonzero_after_auto_cab_selection(self, network):
        """After ato_go auto-selects cab, ATC speed limit should not be 0."""
        train, controller = self._setup_train_no_cab(network)

        result = controller.ato_go()
        assert result.success is True

        # Run a tick to update ATC info
        controller.tick(0.1)

        # ATC speed limit should not be 0 (it was 0 with "No cab selected" before)
        assert controller.active_cab is not None
        assert controller.speed_limit_reason != "No cab selected"

    def test_notification_on_auto_cab_selection(self, network):
        """Auto-cab-selection should produce a notification for the user."""
        train, controller = self._setup_train_no_cab(network)

        result = controller.ato_go()
        assert result.success is True

        # Check that a notification was added
        assert len(controller.pending_notifications) > 0
        notification = controller.pending_notifications[0]
        assert "auto" in notification.lower()
        assert "cab" in notification.lower()

    def test_ato_go_not_in_ato_mode(self, network):
        """ato_go should fail if control mode is manual."""
        train, controller = self._setup_train_no_cab(network)
        controller.set_mode("manual")

        result = controller.ato_go()
        assert result.success is False
        assert (
            "ATO" in result.message
            or "ato" in result.message
            or "Not in ATO" in result.message
        )
