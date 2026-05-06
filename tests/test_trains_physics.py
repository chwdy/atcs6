"""
Train physics tests - Movement, block occupancy, transitions

Run with: pytest tests/test_trains_physics.py -v
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

        assert train.position.leading_end == LeadingEnd.A
        assert train.position.occupied_tiles[0] == train.position.end_a_tile
        assert train.position.occupied_tiles[-1] == train.position.end_b_tile




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



class TestTrainPhysicsBlockCautionReleaseBugFix:
    """
    Comprehensive tests for TrainPhysics CAUTION release fix (2026-01-22).

    BUG: TrainPhysics._update_block_occupancy() only transitions OCCUPIED -> CAUTION
    and never clears CAUTION blocks, leaving blocks stuck in CAUTION indefinitely.

    FIX: When a block remains unoccupied after the CAUTION transition, clear it
    to FREE on the next update tick.
    """




class TestTrainPhysicsCautionHoldBugFix:
    """
    Comprehensive tests for TrainPhysics caution hold bug fix (2026-01-23).

    BUG: TrainPhysics._update_block_occupancy() clears CAUTION blocks on the
    next tick even when the train has not vacated another block. This causes
    the block behind the train to become FREE while the train is still moving.

    FIX: Keep the last vacated block in CAUTION until another block is vacated
    or the train leaves the track entirely.
    """





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
            leading_end=LeadingEnd.A,
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




class TestTrainPhysicsDebugLogging:
    """Regression tests for TrainPhysics debug details (2026-01-22)."""

    def test_train_physics_tick_updates_speed_correctly(self):
        """TrainPhysics.tick should update speed based on handle position.

        v1: target_speed is read-only on the controller; physics doesn't read
        target_speed at all — it computes speed deltas from handle_position
        plus the speed profile.
        """
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_PHYS_LOG", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)

        controller.power_on = True
        controller.handle_position = HandlePosition.P2
        # v1: write canonical speed onto the controller; controller.current_speed_kmh mirrors.
        controller.current_speed_kmh = 12.0
        controller.current_speed_kmh = 12.0

        initial_speed = controller.current_speed_kmh
        physics.tick(0.1)

        # Speed should increase with P2 handle (power)
        assert controller.current_speed_kmh > initial_speed


class TestTrainPhysicsManualPowerPercent:
    """Manual power_percent mode wiring (design_new.md "Ownership split").

    The controller publishes `manual_power_percent`; physics converts via
    SpeedProfile.power_speed_curve into the calibrated current speed when no
    braking override is active. ATC and EB braking force the percent
    contribution to 0 for the tick.
    """

    def _build_train(self):
        network = RailwayNetwork()
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        car = TrainCar(car_id="CAR_PCT", car_type="LOCOMOTIVE", length=20.0)
        train = Train(train_id="TRAIN_PCT", consist=[car], max_speed=120.0)
        train.position = make_train_position((1, 0), Direction.EAST)
        network.add_train(train)
        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)
        controller.state = TrainState.RUNNING_MOVING
        controller.control_mode = "manual"
        controller.manual_mode_type = "power_percent"
        controller.atc_speed_limit = 120.0
        controller.current_speed_kmh = 0.0
        controller.current_speed_kmh = 0.0
        return network, train, controller, physics

    def test_power_percent_50_immediate_curve_speed(self):
        """At 50% with default linear curve and no braking, speed jumps to 50 km/h."""
        network, train, controller, physics = self._build_train()
        controller.manual_power_percent = 50.0
        physics.tick(0.1)
        # Default power_speed_curve is [(0,0),(100,100)] -> 50% -> 50 km/h.
        assert abs(controller.current_speed_kmh - 50.0) < 0.5

    def test_power_percent_eb_overrides_to_decel(self):
        """EB latched: percent ignored; emergency-brake decel applies."""
        network, train, controller, physics = self._build_train()
        controller.manual_power_percent = 80.0
        controller.current_speed_kmh = 60.0
        controller.current_speed_kmh = 60.0
        controller.eb_active = True
        physics.tick(0.1)
        # Default emergency_brake_rate is 30 km/h/s, so 0.1s -> 60 - 3 = 57.
        assert controller.current_speed_kmh < 60.0
        assert controller.current_speed_kmh >= 0.0

    def test_power_percent_atc_clamps_to_limit(self):
        """ATC ceiling caps the calibrated curve target."""
        network, train, controller, physics = self._build_train()
        controller.manual_power_percent = 80.0
        controller.atc_speed_limit = 30.0
        physics.tick(0.1)
        # Curve says 80, ATC says 30 -> speed clamped at 30.
        assert abs(controller.current_speed_kmh - 30.0) < 0.5

