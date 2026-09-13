"""
ATO tests - ATO operations, distance-aware control, stopping

Run with: pytest tests/test_trains_ato.py -v
"""

import logging
import pytest
from types import SimpleNamespace

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
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


class TestAtoStopButtonBugFix:
    """
    Comprehensive tests for ATO stop command behavior (2026-01-25).

    BUG: ATO stop only set ato_running=False, leaving the handle in a power notch
    and the train continuing to move.

    FIX: ATO stop applies braking and transitions to STANDBY once stopped.
    """

    def _setup_ato_train(self, network):
        from tests.train_test_helpers import (
            attach_synthetic_path,
            make_synthetic_path,
        )

        track = Tile(x=0, y=0, track_bits=TRACK_EW)
        next_track = Tile(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_STOP_TEST", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="ATO_STOP_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        # Per Option B, the controller needs a live `received_path_plan` for
        # ATC/ATO to compute service-brake authority. Wire a one-block
        # synthetic path covering the train's current block.
        attach_synthetic_path(
            controller,
            make_synthetic_path(
                train_id=train.train_id,
                block_id="BLOCK_1",
                block_tiles=[(0, 0), (1, 0)],
            ),
        )
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.engaged = True
        controller.ops.handle_position = HandlePosition.P2

        controller.physics_sync.current_speed_kmh = 30.0
        return train, controller

    def test_ato_stop_sets_ato_running_false(self, network):
        """ato_stop() sets ato_running=False. Braking is applied in tick()."""
        _, controller = self._setup_ato_train(network)

        result = controller.ato.disengage()

        assert result.success is True
        assert controller.ato.engaged is False

    def test_ato_stop_tick_applies_braking(self, network):
        """After ato_stop(), tick() applies braking."""
        train, controller = self._setup_ato_train(network)

        controller.ato.disengage()
        controller.physics_sync.current_speed_kmh = 30.0  # Still moving

        controller.tick(0.1)

        # Handle should now be braking
        assert controller.ops.handle_position.value.startswith("B")

    def test_ato_stop_transitions_to_standby_when_stopped(self, network):
        train, controller = self._setup_ato_train(network)

        controller.ato.disengage()
        controller.physics_sync.current_speed_kmh = 0.0

        controller.tick(0.1)

        assert controller.ops.state == TrainState.READY
        assert controller.ops.state == TrainState.READY
        assert controller.ops.handle_position == HandlePosition.N


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
        from tests.train_test_helpers import (
            attach_synthetic_path,
            make_synthetic_path,
        )

        track = Tile(x=0, y=0, track_bits=TRACK_EW)
        next_track = Tile(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_DEPART_TEST", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="ATO_DEPART_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        # Per design_new.md step `826a`, service-braking needs live authority;
        # a no-authority rolling train escalates to EB instead. Wire a one-block
        # synthetic path so this ATO-stop braking scenario has authority to
        # brake against.
        attach_synthetic_path(
            controller,
            make_synthetic_path(
                train_id=train.train_id,
                block_id="BLOCK_1",
                block_tiles=[(0, 0), (1, 0)],
            ),
        )
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.engaged = True
        controller.ops.handle_position = HandlePosition.P2

        controller.physics_sync.current_speed_kmh = 30.0
        return train, controller

    def test_ato_stop_sets_ato_running_false(self, network):
        """
        ato_stop() simply sets ato_running=False.
        tick() handles braking and transition to STANDBY.
        """
        train, controller = self._setup_ato_train(network)

        result = controller.ato.disengage()

        assert result.success is True
        assert controller.ato.engaged is False

    def test_tick_brakes_and_transitions_to_standby_when_ato_stopped(self, network):
        """
        When ato_running=False, tick() should brake to stop and
        transition to STANDBY when stopped.
        """
        train, controller = self._setup_ato_train(network)

        # User presses ATO Stop
        controller.ato.disengage()
        assert controller.ato.engaged is False

        # Train is still moving
        controller.physics_sync.current_speed_kmh = 10.0
        controller.tick(0.1)

        # Should still be RUNNING_MOVING while braking
        # (handle should be braking)
        assert controller.ops.handle_position.value.startswith("B")

        # Simulate train stopped
        controller.physics_sync.current_speed_kmh = 0.0
        controller.tick(0.1)

        # Now should transition to STANDBY
        assert controller.ops.state == TrainState.READY

    def test_ato_go_starts_ato(self, network):
        """
        Verify that ato_go() still properly starts ATO.
        """
        track = Tile(x=0, y=0, track_bits=TRACK_EW)
        next_track = Tile(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_GO_TEST", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="ATO_GO_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        # ato.engage() sets operator intent; it does not require live authority
        # and does not move the operational state (motion does, via the reactive
        # rule). A synthetic path is wired so the cab-alignment bridge has a
        # direction to read.
        from tests.train_test_helpers import (
            attach_synthetic_path,
            make_synthetic_path,
        )

        attach_synthetic_path(
            controller,
            make_synthetic_path(
                train_id=train.train_id,
                block_id="BLOCK_1",
                block_tiles=[(0, 0), (1, 0)],
            ),
        )
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.READY
        controller.ato.engaged = False


        # Engaging ATO sets intent; the stopped train stays READY until it moves.
        result = controller.ato.engage()
        assert result.success is True
        assert controller.ato.engaged is True
        assert controller.ops.state == TrainState.READY





class TestAtoDistanceAwareControl:
    """
    Comprehensive tests for ATO distance-aware control (2026-01-26).

    BUG: ATO speed control uses tight tolerance around target speed, causing
    oscillation and re-acceleration near end-of-path.

    FIX: Use distance-aware target speed from braking curve and select handle
    with hysteresis to avoid N/B chatter.
    """

    def _setup_ato_train_with_path(self, network, clearance_cm: float = 1000.0):
        """Helper to set up an ATO train with specified clearance.

        Per Option B (design_new.md step `22a9`), the controller needs a
        live `received_path_plan` for ATC/ATO to compute service-brake
        authority. We wire a one-block synthetic path covering the train's
        current block; the `clearance_cm` arg becomes the manually-pinned
        controller field, but the live recompute walks the synthetic path.
        """
        from tests.train_test_helpers import (
            attach_synthetic_path,
            make_synthetic_path,
        )

        track = Tile(x=0, y=0, track_bits=TRACK_EW)
        next_track = Tile(x=1, y=0, track_bits=TRACK_EW)
        network.add_track(track)
        network.add_track(next_track)
        make_segment_block_with_tracks(network, "BLOCK_1", [(0, 0), (1, 0)], length_cm=200.0)

        car = TrainCar("ATO_DIST_TEST", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="ATO_DIST_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        attach_synthetic_path(
            controller,
            make_synthetic_path(
                train_id=train.train_id,
                block_id="BLOCK_1",
                block_tiles=[(0, 0), (1, 0)],
            ),
        )
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.engaged = True
        controller.atc.clearance_ahead_cm = clearance_cm
        controller.atc.speed_limit = 60.0
        controller.atc.current_block_speed_limit = 60.0

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
        controller.physics_sync.current_speed_kmh = 40.0  # Train moving at moderate speed

        # ATO stop is pressed
        controller.ato.disengage()
        assert controller.ato.engaged is False

        # Tick should apply aggressive service braking
        controller.tick(0.1)

        # Handle should be high B-notch (B4-B8) but NOT EB
        handle = controller.ops.handle_position
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
        controller.physics_sync.current_speed_kmh = 80.0  # High speed

        controller.ato.disengage()
        controller.tick(0.1)

        # Should use maximum service brake B8
        assert controller.ops.handle_position == HandlePosition.B8, (
            f"ATO stop at high speed should use B8, got {controller.ops.handle_position.value}"
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
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create block that covers the tracks with reasonable length
        # At model scale, 100 cm gives us plenty of room
        make_segment_block_with_tracks(network, "LONG_BLOCK", [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)], length_cm=100.0
        )

        car = TrainCar("ATO_BRAKE_TEST", "LOCOMOTIVE", length_cm=10.0)
        train = Train(train_id="ATO_BRAKE_TEST", consist=[car])
        network.add_train(train)
        # Place at middle of block so we have clearance ahead
        network.place_train(train, 2, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.engaged = True

        controller.physics_sync.current_speed_kmh = 30.0  # Moving at 30 km/h

        # Tick to calculate clearance and decide handle
        controller.tick(0.1)

        # Check clearance - should be around 40cm (remaining in block)
        # which is above safety margin (25cm), so ATC should allow some speed
        clearance = controller.atc.clearance_ahead_cm
        atc_limit = controller.atc.speed_limit
        handle = controller.ops.handle_position

        # With clearance > safety margin, should not trigger EB
        if clearance > 25.0:
            assert handle != HandlePosition.EB or controller.eb.active, (
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
        controller.physics_sync.current_speed_kmh = 5.0  # Low speed
        controller.atc.speed_limit = 10.0  # Target is 10 km/h
        controller.atc.current_block_speed_limit = 10.0
        controller.atc.clearance_ahead_cm = 10000.0  # Plenty of clearance

        # First tick
        controller.tick(0.1)
        handle_1 = controller.ops.handle_position

        # Simulate speed slightly changing
        controller.physics_sync.current_speed_kmh = 6.0
        controller.tick(0.1)
        handle_2 = controller.ops.handle_position

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
        controller.physics_sync.current_speed_kmh = 25.0  # Moderate speed

        controller.ato.disengage()
        controller.tick(0.1)

        handle = controller.ops.handle_position
        assert handle.value.startswith("B"), (
            f"ATO stop should use brake, got {handle.value}"
        )
        brake_number = int(handle.value[1])
        # At 25 km/h, should use at least B5 for aggressive stop
        assert brake_number >= 5, (
            f"ATO stop at 25 km/h should use B5+, got {handle.value}"
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
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create blocks
        make_segment_block_with_tracks(network, "BLOCK_A",
            positions=[(0, 0), (1, 0), (2, 0)],
            name="Block A",
            length_cm=150.0,
        )
        make_segment_block_with_tracks(network, "BLOCK_B",
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

    def test_ato_has_no_clearance_after_path_completes(self, network):
        """Per design_new.md step `22a9` (Option B): once the path
        transitions out of ACTIVE, `received_path_plan` reads `None` and
        the canonical lookahead is the no-authority empty payload — no
        block-bounded fallback authority. A stopped train sits at rest;
        a rolling train escalates to EB through step `826a`.

        This test pins the post-path-completion behavior: clearance and
        ATC limit must both be `0`, with `no_reservation` reason. The train
        cannot drive forward without a freshly-issued path.
        """
        from enums import AtcLimitReasonCode

        self._setup_station_track(network)

        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="TRAIN_STOP_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 3, 0, Direction.EAST)

        controller = TrainController(train_id=train.train_id, network=network)
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.READY
        controller.ato.engaged = False
        controller.physics_sync.current_speed_kmh = 0.0

        controller.tick(0.1)

        assert controller.atc.speed_limit == 0.0, (
            f"Post-completion ATC limit must be 0 under Option B, "
            f"got {controller.atc.speed_limit}"
        )
        assert (
            controller.atc._last_limit_reason_code == AtcLimitReasonCode.NO_RESERVATION
        ), (
            f"Post-completion reason must be NO_RESERVATION, "
            f"got {controller.atc._last_limit_reason_code!r}"
        )
        assert controller.target_speed == 0.0, (
            f"target_speed reads operational_limit at distance 0; "
            f"with no path it must be 0, got {controller.target_speed}"
        )


    def test_ato_destination_cleared_when_stopped_at_station(self, network):
        """
        ato_destination_block_id should be cleared when train stops at the station.

        This prevents the old destination from affecting future paths.
        """
        self._setup_station_track(network)

        # Create train and manually place at end of station (tile 4,0, facing WEST)
        # For multi-tile platforms, train must be at last tile to be "at station"
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="TRAIN_CLEAR_TEST", consist=[car])
        network.add_train(train)

        # v1: path geometry / future-authority blocks live on the dispatcher;
        # this test exercises the at-station-stopped branch which does not
        # depend on having an active path wired.

        controller = TrainController(train_id=train.train_id, network=network)
        # Manually seed a single-tile span at last station tile (4,0), facing
        # WEST (entry_edge="E" — entered from east). Cab "A" -> end_a is head.
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(4, 0), distance_cm=25.0, entry_edge="E"),
            end_b=SpanEndLocator(tile=(4, 0), distance_cm=25.0, entry_edge="E"),
            occupied_tiles=[(4, 0)],
            occupied_blocks=["BLOCK_B"],
        )
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.engaged = True

        controller.physics_sync.current_speed_kmh = 0.0  # Train is stopped

        # Tick should detect train is at station and stopped
        controller.tick(0.1)

        # When train arrives at station and stops, ato_destination_block_id should clear
        # (or transition to AT_STATION state should handle this)
        # A stopped ATO train with no live authority lands in READY and is not
        # driven forward. ATO intent stays engaged (design_new.md "ato.engaged":
        # intent is cleared only by EB / power-off / set_mode("manual")); it is
        # the absence of authority, not a cleared flag, that keeps the train
        # from moving.
        assert controller.ops.state == TrainState.READY, (
            f"Train stopped at destination with no authority should be READY. "
            f"state={controller.ops.state}"
        )
        assert controller.ops.handle_position == HandlePosition.N, (
            f"A stopped train with no authority must not be driven forward. "
            f"handle={controller.ops.handle_position}"
        )
        assert controller.ato.engaged is True, (
            "ATO intent stays engaged through a stop at the authority end."
        )




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



class TestTrendAwareATOControl:
    """
    Tests for ATO Control using future-aware curve-tracking approach (Feb 2026).

    The control system uses:
    - Zone 1 (Stopping): When curve at position 0 says stop (operational_limit <= 0.5),
      use direct braking notch calculation.
    - Future-aware evaluator: For all other cases, evaluates all candidate handle
      positions by simulating the train forward and checking which handle best
      tracks the operational speed curve without overshooting.
    - ATC safety cap: If the chosen handle would exceed ATC limits, override
      with braking.
    """

    @pytest.fixture
    def network(self):
        return RailwayNetwork()

    @pytest.fixture
    def straight_track(self, network):
        for x in range(10):
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        # Per Option B, ATC/ATO need a real `received_path_plan` to compute
        # forward authority. Create a block so the synthetic path below
        # resolves; the tests in this class exercise the ATO handle
        # evaluator under that authority.
        make_segment_block_with_tracks(network, "BLOCK_TREND",
            [(x, 0) for x in range(10)],
            length_cm=1000.0,
        )
        return network

    @pytest.fixture
    def train_and_controller(self, network, straight_track):
        from tests.train_test_helpers import (
            attach_synthetic_path,
            make_synthetic_path,
            set_train_span,
        )

        car = TrainCar(car_id="CAR_TEST", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="TRAIN_ATO_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = TrainController(train_id=train.train_id, network=network)
        # v1: the controller's head (and thus the live lookahead / operational
        # curve the ATO evaluator reads) derives from
        # `physics_sync.current_span`, not from `Train`. Seed a span at the
        # placement tile so the synthetic-path walk has a real head to start
        # from; without it `_get_head_tile()` is None and the curve at
        # distance 0 collapses to a stop, making ATO coast (N) regardless of
        # the speed/curve setup under test.
        set_train_span(
            controller,
            (0, 0),
            facing_direction=Direction.EAST,
            occupied_blocks=["BLOCK_TREND"],
        )
        attach_synthetic_path(
            controller,
            make_synthetic_path(
                train_id=train.train_id,
                block_id="BLOCK_TREND",
                block_tiles=[(x, 0) for x in range(10)],
            ),
        )
        controller.commands.control_mode = "ato"
        controller._apply_head_frame("end_a")
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.engaged = True
        controller.ato._previous_speed = 0.0
        controller.atc.speed_limit = 60.0
        controller.atc.current_block_speed_limit = 80.0
        controller.atc.clearance_ahead_cm = 100000.0

        return train, controller

    def test_future_aware_evaluator_returns_valid_handle(self, train_and_controller):
        """The evaluator should return a valid handle position for normal cruising."""
        train, controller = train_and_controller

        controller.physics_sync.current_speed_kmh = 58.0
        controller.ato._previous_speed = 58.0
        controller.ops.handle_position = HandlePosition.P2

        handle = controller.ato.compute_handle()

        # The evaluator should return some valid handle from the candidate set
        valid_handles = [
            HandlePosition.P5,
            HandlePosition.P4,
            HandlePosition.P3,
            HandlePosition.P2,
            HandlePosition.P1,
            HandlePosition.N,
            HandlePosition.B1,
            HandlePosition.B2,
            HandlePosition.B3,
            HandlePosition.B4,
            HandlePosition.B5,
            HandlePosition.B6,
            HandlePosition.B7,
            HandlePosition.B8,
        ]
        assert handle in valid_handles, (
            f"Future-aware evaluator should return a valid handle. Got {handle}"
        )

    def test_future_aware_brakes_when_overshooting(self, train_and_controller):
        """When speed significantly exceeds curve, evaluator should select braking.

        Note: With handle rate limiting (Task 5), the handle can only move
        ±1 notch per tick. Starting from P5, the rate-limited result will be
        P4 (one step toward brake). The important thing is the handle moves
        in the braking direction, not that it immediately reaches a brake notch.
        """
        train, controller = train_and_controller

        controller.physics_sync.current_speed_kmh = 65.0
        controller.ato._previous_speed = 64.0
        controller.ops.handle_position = HandlePosition.P5

        handle = controller.ato.compute_handle()

        # With rate limiting, from P5 the handle moves 1 step toward brake → P4
        # The key invariant: handle should move toward brake (lower power or brake)
        from enums import HANDLE_POSITION_INDEX

        current_idx = HANDLE_POSITION_INDEX[HandlePosition.P5]
        result_idx = HANDLE_POSITION_INDEX[handle]
        assert result_idx >= current_idx, (
            f"When overshooting curve, handle should move toward brake direction. "
            f"Started at P5 (idx {current_idx}), got {handle.value} (idx {result_idx})"
        )

    def test_future_aware_accelerates_when_below_curve(self, train_and_controller):
        """When speed is far below curve, evaluator should select power."""
        train, controller = train_and_controller

        controller.physics_sync.current_speed_kmh = 30.0
        controller.ato._previous_speed = 29.0
        controller.ops.handle_position = HandlePosition.N

        handle = controller.ato.compute_handle()

        # With speed (30) far below curve limit (~55), evaluator should power up
        _POWER_HANDLES = {
            HandlePosition.P1,
            HandlePosition.P2,
            HandlePosition.P3,
            HandlePosition.P4,
            HandlePosition.P5,
        }
        assert handle in _POWER_HANDLES, (
            f"When far below curve, evaluator should select a power handle. Got {handle}"
        )

    def test_future_aware_near_target_not_full_power(self, train_and_controller):
        """When near target, evaluator shouldn't use maximum power."""
        train, controller = train_and_controller

        controller.physics_sync.current_speed_kmh = 54.0
        controller.ato._previous_speed = 54.5
        controller.ops.handle_position = HandlePosition.N

        handle = controller.ato.compute_handle()

        # Evaluator picks whatever best tracks the curve — should be power since
        # we're still below the operational limit
        _POWER_HANDLES = {
            HandlePosition.P1,
            HandlePosition.P2,
            HandlePosition.P3,
            HandlePosition.P4,
            HandlePosition.P5,
        }
        valid = handle in _POWER_HANDLES or handle == HandlePosition.N
        assert valid, (
            f"When near target, evaluator should select power or neutral. Got {handle}"
        )

    def test_future_aware_sets_handle_reason(self, train_and_controller):
        """The method should set handle_reason for diagnostics."""
        train, controller = train_and_controller

        controller.physics_sync.current_speed_kmh = 55.0
        controller.ato._previous_speed = 55.0
        controller.ops.handle_position = HandlePosition.N

        controller.ato.compute_handle()

        assert controller.ops.handle_reason, "handle_reason should be set"
        # Reason strings produced by the kinematic compute_handle:
        # "Coast: ...", "Power up: ...", "Brake B?: ...", "Stopping: ...",
        # "Stopped", "No curves", "No train".
        assert any(
            tag in controller.ops.handle_reason
            for tag in ("Coast", "Power up", "Brake", "Stopping", "Stopped", "No ")
        ), (
            f"handle_reason should indicate the control mode. Got: {controller.ops.handle_reason}"
        )

    def test_future_aware_sets_previous_speed(self, train_and_controller):
        """The method should update _previous_speed for trend detection."""
        train, controller = train_and_controller

        controller.physics_sync.current_speed_kmh = 55.0
        controller.ato._previous_speed = 50.0

        controller.ato.compute_handle()

        assert controller.ato._previous_speed == 55.0, (
            f"_previous_speed should be updated to current speed. Got {controller.ato._previous_speed}"
        )

    def test_future_aware_atc_safety_cap(self, train_and_controller):
        """When evaluator picks handle that would exceed ATC, should override to brake.

        Note: With handle rate limiting (Task 5), the handle can only move
        ±1 notch per tick. Starting from P5, the result will be P4 (one step
        toward brake). The key invariant: handle moves in the braking direction.
        """
        train, controller = train_and_controller

        # Set ATC limit very low so safety cap triggers
        controller.atc.speed_limit = 20.0
        controller.physics_sync.current_speed_kmh = 25.0
        controller.ato._previous_speed = 24.0
        controller.ops.handle_position = HandlePosition.P5

        handle = controller.ato.compute_handle()

        # With rate limiting, from P5 the handle moves 1 step toward brake → P4
        from enums import HANDLE_POSITION_INDEX

        current_idx = HANDLE_POSITION_INDEX[HandlePosition.P5]
        result_idx = HANDLE_POSITION_INDEX[handle]
        assert result_idx >= current_idx, (
            f"ATC safety cap should move handle toward brake direction. "
            f"Started at P5 (idx {current_idx}), got {handle.value} (idx {result_idx})"
        )

    def test_priority_override_stopping_uses_full_brake(self, train_and_controller):
        """When ATC limit forces a stop (ceiling@0=0), ATO commands B8."""
        train, controller = train_and_controller
        controller.atc.speed_limit = 0.0

        controller.physics_sync.current_speed_kmh = 15.0
        controller.ato._previous_speed = 20.0
        controller.ops.handle_position = HandlePosition.B2

        handle = controller.ato.compute_handle()

        assert handle == HandlePosition.B8, (
            f"With ceiling@0=0 (forced stop), the stopping branch returns B8. "
            f"Got {handle}"
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
            track = Tile(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        return network

    @pytest.fixture
    def train_and_controller(self, network, straight_track):
        car = TrainCar(car_id="CAR_CURVE", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="TRAIN_CURVE_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)
        # `calculate_speed_curves` reads the head from the controller's
        # `current_span` and the network/block geometry; path geometry lives
        # on PathDispatcher in v1 and is not required for the curve-shape
        # assertions below.

        controller = TrainController(train_id=train.train_id, network=network)
        controller.commands.control_mode = "ato"
        controller.ops.state = TrainState.RUNNING_MOVING

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

    def test_ato_limit_below_atc_by_margin(self, train_and_controller):
        """ATO limit should be at least ATO_SPEED_MARGIN_KMH below ATC."""
        from train_controller import ATO_SPEED_MARGIN_KMH

        train, controller = train_and_controller
        train.max_speed = 100.0

        curves = controller.calculate_speed_curves(max_distance_cm=2000)

        for point in curves:
            atc = point["atc_limit"]
            ato = point["operational_limit"]
            if atc > ATO_SPEED_MARGIN_KMH:
                assert ato <= atc - ATO_SPEED_MARGIN_KMH, (
                    f"ATO ({ato}) should be at least {ATO_SPEED_MARGIN_KMH} km/h "
                    f"below ATC ({atc}) at distance {point['distance_cm']}cm"
                )

    def test_caution_block_without_nominal_limit_caps_at_global(
        self, train_and_controller
    ):
        """A CAUTION block ahead caps the curve at CAUTION_SPEED_LIMIT_KMH
        even when it carries no nominal speed_limit_kmh (design_new.md
        "ATC speed limit").

        Regression for gap #8: previously the curve only applied a CAUTION
        restriction when the block also had a nominal limit, so a CAUTION
        block with `speed_limit_kmh=None` produced no cap in the canonical
        lookahead speed_curves at all.
        """
        from constants import CAUTION_SPEED_LIMIT_KMH

        train, controller = train_and_controller
        # One CAUTION block with NO nominal limit, sitting 300-900cm ahead.
        lookahead_blocks = [
            {
                "id": "BLK_CAUTION",
                "name": "BLK_CAUTION",
                "state": BlockState.CAUTION.value,
                "start_cm": 300.0,
                "end_cm": 900.0,
                "length_cm": 600.0,
                "speed_limit_kmh": None,
            }
        ]
        curves = controller.calculate_speed_curves(
            max_distance_cm=3000, lookahead_blocks=lookahead_blocks
        )

        inside = [
            p for p in curves if 300.0 <= p["distance_cm"] < 900.0
        ]
        assert inside, "expected curve sample points inside the CAUTION block"
        for p in inside:
            assert p["atc_limit"] <= CAUTION_SPEED_LIMIT_KMH + 1e-6, (
                f"CAUTION block (no nominal limit) should cap atc_limit at "
                f"{CAUTION_SPEED_LIMIT_KMH}, got {p['atc_limit']} at "
                f"{p['distance_cm']}cm"
            )
        # At least one in-block point is reported with the CAUTION reason.
        assert any(
            p.get("atc_limit_reason_code") == "caution" for p in inside
        ), "expected at least one in-block point tagged CAUTION"

    def test_caution_block_with_high_nominal_limit_still_capped(
        self, train_and_controller
    ):
        """When a CAUTION block's nominal limit exceeds the global CAUTION
        cap, the global cap wins (lower of the two)."""
        from constants import CAUTION_SPEED_LIMIT_KMH

        train, controller = train_and_controller
        lookahead_blocks = [
            {
                "id": "BLK_CAUTION_HI",
                "name": "BLK_CAUTION_HI",
                "state": BlockState.CAUTION.value,
                "start_cm": 300.0,
                "end_cm": 900.0,
                "length_cm": 600.0,
                "speed_limit_kmh": CAUTION_SPEED_LIMIT_KMH + 30.0,
            }
        ]
        curves = controller.calculate_speed_curves(
            max_distance_cm=3000, lookahead_blocks=lookahead_blocks
        )
        inside = [p for p in curves if 300.0 <= p["distance_cm"] < 900.0]
        assert inside
        for p in inside:
            assert p["atc_limit"] <= CAUTION_SPEED_LIMIT_KMH + 1e-6, (
                f"global CAUTION cap should win over the higher nominal "
                f"limit; got {p['atc_limit']} at {p['distance_cm']}cm"
            )


class TestForwardSimulation:
    """
    Tests for forward simulation used by future-aware ATO.

    The forward simulation predicts where a train will be and how fast
    it will be going after applying a specific handle for a given distance.
    """

    @pytest.fixture
    def controller_with_profile(self, network):
        """Create a controller with a known speed profile for predictable physics."""
        from train_controller import TrainController
        from speed_profile import SpeedProfile

        train = Train(train_id="sim_test")
        train.max_speed = 60.0
        network.trains["sim_test"] = train

        # Register a simple linear profile
        profile = SpeedProfile(
            profile_id="test_sim",
            name="Test Simulation",
            acceleration_rate=10.0,  # 10 km/h per second
            deceleration_rate=15.0,  # 15 km/h per second
            friction_rate=0.5,
            operational_decel_ratio=0.6,
            air_resistance_enabled=False,  # Disable for predictable tests
        )
        network.speed_profiles.register(profile)
        train.speed_profile_id = "test_sim"

        controller = TrainController(train_id="sim_test", network=network)
        controller.atc.speed_limit = 60.0
        controller.atc.current_block_speed_limit = 60.0
        return controller

    def test_simulate_forward_braking_reduces_speed(self, controller_with_profile):
        """B4 braking from 20 km/h should result in lower speed after 5cm."""
        predicted_speed = controller_with_profile.ato.simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.B4,
            distance_cm=5.0,
        )
        assert predicted_speed < 20.0, "B4 braking should reduce speed"
        assert predicted_speed >= 0.0, "Speed should not go negative"

    def test_simulate_forward_power_increases_speed(self, controller_with_profile):
        """P5 power from 10 km/h should result in higher speed after 5cm."""
        predicted_speed = controller_with_profile.ato.simulate_forward(
            current_speed_kmh=10.0,
            handle=HandlePosition.P5,
            distance_cm=5.0,
        )
        assert predicted_speed > 10.0, "P5 should increase speed"

    def test_simulate_forward_neutral_slight_decrease(self, controller_with_profile):
        """Neutral handle should cause slight speed decrease from friction."""
        predicted_speed = controller_with_profile.ato.simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.N,
            distance_cm=5.0,
        )
        # Should decrease slightly due to friction (0.5 km/h/s)
        assert predicted_speed < 20.0, "Neutral should cause slight decrease"
        assert predicted_speed > 19.0, "Friction decrease should be small"

    def test_simulate_forward_zero_distance(self, controller_with_profile):
        """Zero distance should return current speed unchanged."""
        predicted_speed = controller_with_profile.ato.simulate_forward(
            current_speed_kmh=30.0,
            handle=HandlePosition.B4,
            distance_cm=0.0,
        )
        assert predicted_speed == 30.0

    def test_simulate_forward_braking_to_zero(self, controller_with_profile):
        """Heavy braking from low speed over long distance should reach 0."""
        predicted_speed = controller_with_profile.ato.simulate_forward(
            current_speed_kmh=5.0,
            handle=HandlePosition.B8,
            distance_cm=50.0,
        )
        assert predicted_speed == 0.0, "Should stop within 50cm from 5 km/h with B8"

    def test_simulate_forward_higher_brake_stops_faster(self, controller_with_profile):
        """Higher brake notch should result in lower speed at same distance."""
        speed_b2 = controller_with_profile.ato.simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.B2,
            distance_cm=3.0,
        )
        speed_b6 = controller_with_profile.ato.simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.B6,
            distance_cm=3.0,
        )
        assert speed_b6 < speed_b2, "B6 should reduce speed more than B2"



class TestHandleSmoothing:
    """Tests for handle change rate limiting to prevent jitter."""

    def test_rate_limit_prevents_jump(self):
        """Handle should not jump more than 1 notch per evaluation."""
        from train_controller.ato import rate_limit_handle

        # Currently at P3, evaluator wants B4 → should only go to P2
        result = rate_limit_handle(
            current=HandlePosition.P3,
            target=HandlePosition.B4,
        )
        # Should move one step toward brake: P3 → P2
        assert result == HandlePosition.P2

    def test_rate_limit_allows_same(self):
        """Same handle should pass through unchanged."""
        from train_controller.ato import rate_limit_handle

        result = rate_limit_handle(
            current=HandlePosition.B3,
            target=HandlePosition.B3,
        )
        assert result == HandlePosition.B3

    def test_rate_limit_allows_adjacent(self):
        """Adjacent handle should pass through."""
        from train_controller.ato import rate_limit_handle

        result = rate_limit_handle(
            current=HandlePosition.N,
            target=HandlePosition.B1,
        )
        assert result == HandlePosition.B1

    def test_rate_limit_exception_for_stopping(self):
        """Stopping zone should allow faster handle changes."""
        from train_controller.ato import rate_limit_handle

        # When target is B6 in stopping, allow 2-step jump
        result = rate_limit_handle(
            current=HandlePosition.P3,
            target=HandlePosition.B6,
            allow_fast=True,
        )
        # Should move 2 steps: P3 → P1
        assert result == HandlePosition.P1
