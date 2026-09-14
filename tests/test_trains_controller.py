"""
Train controller tests - Controller basics, ATC, braking

Run with: pytest tests/test_trains_controller.py -v
"""

import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainStatus, HandlePosition
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from tests.train_test_helpers import set_train_span
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode, RouteTask
from switch import Switch
from tests.segment_test_helpers import make_segment_block, make_segment_block_with_tracks
from entity_ids import segment_id_for_block


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network



class TestRaceConditionFix:
    """Tests for race condition fixes."""

    def test_block_manager_prevents_premature_release(self, network):
        block = make_segment_block(network, "B1", "Block 1")
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

    def test_atc_triggers_eb_on_10kmh_overspeed(self):
        """
        Test that ATC triggers emergency brake when overspeed by > 10 km/h.

        Per new tick() workflow:
        - current_speed > atc_speed + 10 → eb_active = True
        """
        network = RailwayNetwork()

        # Create track and block with speed limit
        track = Tile(x=0, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block("B1")
        network.add_track(track)

        block = make_segment_block(network, "B1", "Block 1")
        block.length_cm = 1000.0
        block.speed_limit_kmh = 30.0  # 30 km/h limit

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_EB_TEST", consist=[car], max_speed=100.0)
        network.add_train(train)

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)
        set_train_span(
            controller, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["B1"]
        )
        controller.powered = True
        controller._apply_head_frame("end_a")
        controller.physics_sync.current_speed_kmh = 42.0  # 12 km/h over limit (EB zone)

        # Tick controller
        sim.update(0.1)

        # Should trigger EB
        assert controller.eb.active == True, "EB should trigger at 12 km/h overspeed"
        # Per design_new.md "canonical lookahead payload", the "speed-limit
        # reason" comes from the
        # canonical lookahead at distance 0; for EB-active state that reads
        # "Emergency brake active". The trigger context (overspeed) is
        # carried by NotificationCenter via the controller's pending
        # notifications.
        assert (controller.speed_limit_reason or "").lower() == "emergency brake active", (
            f"Lookahead should report EB active, got: {controller.speed_limit_reason}"
        )
        _notes = [
            n.message
            for n in controller.simulation_controller.notification_center.peek_recent(200)
        ]
        assert any("overspeed" in note.lower() for note in _notes), (
            f"Should have an overspeed EB event, got: {_notes}"
        )



class TestTrainControllerBrakingNotches:
    """Tests for braking notch selection updates (2026-01-22)."""

    def test_braking_notch_for_speed_uses_b1_to_b8(self):
        """Speed-based braking should map to B1-B8 notches.

        Buckets (current - target): >25 B8, >18 B7, >12 B6, >8 B5, >5 B4,
        >3 B3, >1 B2, else B1. Notches now multiply op_decel.
        """
        network = RailwayNetwork()
        train = Train(train_id="TRAIN_BRAKE", max_speed=60.0)
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)

        assert (
            controller.ato.calculate_braking_notch_for_speed(60.0, 25.0)
            == HandlePosition.B8
        )
        assert (
            controller.ato.calculate_braking_notch_for_speed(40.0, 18.0)
            == HandlePosition.B7
        )
        assert (
            controller.ato.calculate_braking_notch_for_speed(30.0, 14.0)
            == HandlePosition.B6
        )
        assert (
            controller.ato.calculate_braking_notch_for_speed(22.0, 12.0)
            == HandlePosition.B5
        )
        assert (
            controller.ato.calculate_braking_notch_for_speed(18.0, 12.0)
            == HandlePosition.B4
        )
        assert (
            controller.ato.calculate_braking_notch_for_speed(12.0, 7.0)
            == HandlePosition.B3
        )
        assert (
            controller.ato.calculate_braking_notch_for_speed(9.0, 6.0) == HandlePosition.B2
        )
        assert (
            controller.ato.calculate_braking_notch_for_speed(7.0, 6.0) == HandlePosition.B1
        )

    def test_manual_mode_uses_atc_speed_braking(self, network, monkeypatch):
        """Manual mode should brake toward ATC speed, not full stop.

        Per design_new.md step `826a`, service-braking against an ATC limit
        requires live authority (a no-authority rolling train escalates to EB
        instead). Wire a one-block synthetic path so this overspeed scenario
        has authority, then force a positive ATC limit so the braking notch is
        deterministic.
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

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=40.0)
        train = Train(train_id="TRAIN_MANUAL", consist=[car], max_speed=60.0)
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
        controller.commands.control_mode = "manual"
        # Per design_new.md: power_on is a read-only derived property; use
        # controller.ops.state to power on.
        controller.powered = True
        controller._apply_head_frame("end_a")
        controller.physics_sync.current_speed_kmh = 16.0

        monkeypatch.setattr(controller.atc, "clearance_ahead", lambda: 200.0)
        monkeypatch.setattr(
            controller.atc,
            "speed_limit_now",
            lambda: (10.0, "test"),
        )

        controller.tick(0.1)

        # current=16, target=10, diff=6 → bucket >5 → B4 (new buckets).
        assert controller.ops.handle_position == HandlePosition.B4

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
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=100.0)
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



class TestPoweredOffControllerTick:
    """A powered-off controller's Phase-2 tick entry still runs, but is inert.

    design_new.md "SimulationController.update() calls": for a train that is
    `OFF` at transaction start, route Phase 1 is skipped while Phase 2 still
    runs, so `power_on_cmd()` can be accepted. This replaces an older test that
    asserted a `logger.debug("power off")` trace — component-local logging is
    forbidden by design ("NotificationCenter is the only runtime sink"), so the
    trace is gone and the behavior underneath it is what's worth pinning.
    """

    def _controller(self):
        network = RailwayNetwork()
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_LOG_1", consist=[car], max_speed=60.0)
        network.add_train(train)
        sim = SimulationController(network)
        return sim._get_or_create_controller(train)

    def test_tick_on_a_powered_off_controller_is_inert(self):
        controller = self._controller()
        assert controller.train_status == TrainStatus.OFF
        controller.tick(0.1)  # must not raise
        assert controller.train_status == TrainStatus.OFF
        assert controller.power_on is False

    def test_power_on_is_accepted_while_off(self):
        controller = self._controller()
        controller.tick(0.1)
        assert controller.commands.power_on_cmd().success is True
        assert controller.train_status != TrainStatus.OFF
        assert controller.power_on is True



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

        track_a = Tile(x=0, y=0, track_bits=TRACK_EW)
        track_b = Tile(x=1, y=0, track_bits=TRACK_EW)
        track_a.segment_id = segment_id_for_block("B1")
        track_b.segment_id = segment_id_for_block("B1")
        network.add_track(track_a)
        network.add_track(track_b)

        block = make_segment_block(network, "B1", "Block 1")
        block.length_cm = 40.0

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=10.0)
        train = Train(train_id="TRAIN_CLEARANCE", consist=[car], max_speed=60.0)
        train.current_path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]

        network.add_train(train)
        controller = TrainController(train_id=train.train_id, network=network)
        # Head is end_a=(1,0), 5cm in from the W edge; the default framed head
        # (end_a) is the head. The span runs head (1,0) -> tail (0,0).
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(1, 0), distance_cm=5.0, entry_edge="W"),
            end_b=SpanEndLocator(tile=(0, 0), distance_cm=5.0, entry_edge="W"),
            occupied_tiles=[(1, 0), (0, 0)],
        )

        remaining = controller._get_remaining_distance_in_current_block()

        assert remaining == pytest.approx(15.0), (
            f"Expected 15.0 cm remaining, got {remaining}"
        )





class TestATCOverspeedWarning:
    """`overspeed_warn_threshold_kmh` drives a real diagnostic warning.

    design_new.md "Runtime settings": the margin above the ATC speed limit "at
    which ATC emits a diagnostic overspeed warning". It is edge triggered on
    entry into the warn band, so one excursion emits one warning rather than one
    per tick.

    The branch sits below the EB escalation, so it is reachable only with live
    authority: a moving train with no authority is escalated to EB first.
    """

    def _sim_with_authority(self):
        network = RailwayNetwork()
        for x in range(6):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        for blk, (a, b) in [("B1", (0, 1)), ("B2", (2, 3)), ("B3", (4, 5))]:
            make_segment_block_with_tracks(network, blk, positions=[(a, 0), (b, 0)], name=blk, length_cm=200.0
            )
            network.blocks[blk].speed_limit_kmh = 30.0

        car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="T_WARN", consist=[car], max_speed=100.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)
        network.block_manager.occupy_block("B1", train.train_id)

        dispatcher = PathDispatcher(network)
        sim = SimulationController(network, dispatcher)
        controller = sim._get_or_create_controller(train)
        set_train_span(
            controller, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["B1"]
        )
        controller._apply_head_frame("end_a")
        controller.powered = True

        assert dispatcher.create_path(
            train_id=train.train_id, requested_destination_block_id="B3"
        ).success
        return sim, controller

    @staticmethod
    def _warnings(controller):
        # There is no per-controller toast queue: the events go straight to the
        # one stream (design_new.md "Runtime notifications").
        nc = controller.simulation_controller.notification_center
        return [
            n for n in nc.peek_recent(200)
            if "overspeed warning" in n.message
        ]

    def _tick_at(self, sim, controller, speed_kmh):
        # ATC's overspeed warning is decided in TrainController.tick (the Action
        # stage). Drive it directly so the injected speed is the speed ATC reads:
        # a full sim.update(...) runs Observe's movement integration first and
        # re-derives the speed from the prior tick's command before ATC sees it,
        # so a forced current_speed_kmh would not reach ATC intact.
        controller.physics_sync.current_speed_kmh = speed_kmh
        controller.tick(0.1)

    def test_sustained_overspeed_in_warn_band_emits_exactly_one_warning(self):
        sim, controller = self._sim_with_authority()
        for _ in range(3):
            self._tick_at(sim, controller, 36.0)  # 6 over a 30 limit

        assert controller.atc.speed_limit == 30.0
        assert controller.eb.active is False, "warn band must not escalate to EB"
        assert len(self._warnings(controller)) == 1, controller.notifications.pending

    def test_dropping_below_the_threshold_rearms_the_warning(self):
        sim, controller = self._sim_with_authority()
        self._tick_at(sim, controller, 36.0)
        assert len(self._warnings(controller)) == 1

        self._tick_at(sim, controller, 29.0)  # back inside the limit
        assert controller._overspeed_warned is False

        self._tick_at(sim, controller, 36.0)  # a second excursion
        assert len(self._warnings(controller)) == 2

    def test_no_warning_below_the_threshold(self):
        sim, controller = self._sim_with_authority()
        self._tick_at(sim, controller, 32.0)  # only 2 over: inside the margin
        assert self._warnings(controller) == []
        assert controller._overspeed_warned is False
