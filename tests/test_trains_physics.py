"""
Train physics tests - Movement, block occupancy, transitions

Run with: pytest tests/test_trains_physics.py -v
"""

import logging
import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition
from train import Train, TrainCar
from tests.train_test_helpers import make_train_span
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
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
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
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0)
        train = Train(train_id="OCCUPIED_TILE_ORDER", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        return train, span

    def test_occupied_tiles_order_leading_end_a(self, network):
        from train_physics import TrainPhysics, _work_from_span

        train, span = self._setup_two_tile_train(network)
        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._work = _work_from_span(span, head_is_a=True)
        physics._move_train(train, 1.0)

        work = physics._work
        assert work.head_is_a is True
        assert work.occupied_tiles[0] == work.end_a_tile
        assert work.occupied_tiles[-1] == work.end_b_tile




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
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        network.create_segment_with_tracks(
            "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
        )
        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 0, 0, Direction.EAST)

        from train_physics import TrainPhysics, _work_from_span

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._work = _work_from_span(span, head_is_a=True)
        physics._move_train(train, 1.0)

        work = physics._work
        assert work.end_a_tile == (1, 0)  # head
        assert work.end_b_tile == (0, 0)  # tail
        assert work.end_b_entry_edge == "W"
        assert work.end_b_distance_cm == pytest.approx(11.0)



class TestZeroLengthSwitchTraversal:
    """Tests for moving across zero-length switch tiles."""

    def test_train_advances_across_zero_length_switch(self, network):
        network.add_track(Tile(0, 0, track_bits=TRACK_EW))
        switch_track = Tile(1, 0, track_bits=TRACK_EW | TRACK_NW)
        switch_track.switch_state = 0
        network.add_track(switch_track)
        network.add_track(Tile(2, 0, track_bits=TRACK_EW))
        switch = Switch(switch_id="SW_1_0", x=1, y=0)
        network.add_switch(switch)
        switch_track.switch_id = switch.switch_id

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="T1", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 0, 0, Direction.EAST)

        from train_physics import TrainPhysics, _work_from_span

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._work = _work_from_span(span, head_is_a=True)
        physics._move_train(train, 25.0)

        work = physics._work
        assert work.end_a_tile == (2, 0)  # head
        assert work.end_a_distance_cm == pytest.approx(25.0)
        assert work.end_b_tile == (0, 0)  # tail
        assert work.end_b_entry_edge == "W"
        assert work.end_b_distance_cm == pytest.approx(35.0)



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
        track_a = Tile(x=0, y=0, track_bits=TRACK_EW)
        track_b = Tile(x=1, y=0, track_bits=TRACK_EW)
        track_a.segment_id = "B1"
        track_b.segment_id = "B2"
        network.add_track(track_a)
        network.add_track(track_b)

        network.create_segment("B1", "Block 1")
        network.create_segment("B2", "Block 2")

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=60.0)
        network.add_train(train)

        network.block_manager.occupy_block("B1", train.train_id)

        from train_physics import TrainPhysics, _SpanWorkState

        physics = TrainPhysics(train_id=train.train_id, network=network)
        physics._last_occupied_blocks = {"B1"}
        physics._work = _SpanWorkState(
            head_is_a=True,
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            occupied_tiles=[(1, 0)],
            occupied_blocks=["B2"],
        )

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
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_PHYS_LOG", consist=[car], max_speed=60.0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)

        # Per design_new.md: power_on is a read-only derived property; use
        # controller.ops.state to power on.
        controller.ops.state = TrainState.READY
        controller.ops.handle_position = HandlePosition.P2
        # v1: write canonical speed onto the controller; controller.physics_sync.current_speed_kmh mirrors.
        controller.physics_sync.current_speed_kmh = 12.0
        controller.physics_sync.current_speed_kmh = 12.0

        initial_speed = controller.physics_sync.current_speed_kmh
        physics.tick(0.1)

        # Speed should increase with P2 handle (power)
        assert controller.physics_sync.current_speed_kmh > initial_speed


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
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        car = TrainCar(car_id="CAR_PCT", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_PCT", consist=[car], max_speed=120.0)
        network.add_train(train)
        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        physics = sim._get_or_create_physics(train)
        controller.physics_sync.current_span = make_train_span((1, 0), Direction.EAST)
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.commands.control_mode = "manual"
        controller.manual.manual_mode_type = "power_percent"
        controller.atc.speed_limit = 120.0
        controller.physics_sync.current_speed_kmh = 0.0
        controller.physics_sync.current_speed_kmh = 0.0
        return network, train, controller, physics

    def test_power_percent_50_immediate_curve_speed(self):
        """At 50% with the per-train default curve and no braking, speed jumps to
        60 km/h (50% of this train's 120 max_speed)."""
        network, train, controller, physics = self._build_train()
        controller.manual.manual_power_percent = 50.0
        physics.tick(0.1)
        # The profile-less train resolves to a default whose power_speed_curve is
        # anchored to its own max_speed (120): [(0,0),(100,120)] -> 50% -> 60 km/h
        # (design_new.md "SpeedProfile"), not the shared 100-anchored default.
        assert abs(controller.physics_sync.current_speed_kmh - 60.0) < 0.5

    def test_power_percent_eb_overrides_to_decel(self):
        """EB latched: percent ignored; emergency-brake decel applies."""
        network, train, controller, physics = self._build_train()
        controller.manual.manual_power_percent = 80.0
        controller.physics_sync.current_speed_kmh = 60.0
        controller.physics_sync.current_speed_kmh = 60.0
        controller.eb.active = True
        physics.tick(0.1)
        # Default emergency_brake_rate is 30 km/h/s, so 0.1s -> 60 - 3 = 57.
        assert controller.physics_sync.current_speed_kmh < 60.0
        assert controller.physics_sync.current_speed_kmh >= 0.0

    def test_power_percent_atc_clamps_to_limit(self):
        """ATC ceiling caps the calibrated curve target."""
        network, train, controller, physics = self._build_train()
        controller.manual.manual_power_percent = 80.0
        controller.atc.speed_limit = 30.0
        physics.tick(0.1)
        # Curve says 80, ATC says 30 -> speed clamped at 30.
        assert abs(controller.physics_sync.current_speed_kmh - 30.0) < 0.5

