"""
Signal Tests - Signal creation, state calculation, and validation

Run with: pytest tests/test_signals.py -v
"""

import pytest

from track import (
    TrackSegment,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
)
from network import RailwayNetwork
from block import Block
from railway_signal import Signal, SignalType, SignalState
from enums import BlockState
from tests.train_test_helpers import make_train_position


@pytest.fixture
def network():
    return RailwayNetwork()


class TestSignal:
    """Tests for Signal class"""

    def test_signal_creation(self):
        signal = Signal(
            signal_id="SIG_001",
            position=(5, 5),
            detection_direction="N",
            signal_type=SignalType.BLOCK,
        )
        assert signal.signal_id == "SIG_001"
        assert signal.position == (5, 5)
        assert signal.detection_direction == "N"
        assert signal.signal_type == SignalType.BLOCK

    def test_serialization(self):
        signal = Signal(
            signal_id="SIG_001",
            position=(5, 5),
            detection_direction="N",
            signal_type=SignalType.BLOCK,
            name="Test Signal",
        )
        signal_dict = signal.to_dict()
        assert signal_dict["signal_id"] == "SIG_001"
        assert signal_dict["x"] == 5
        assert signal_dict["y"] == 5
        assert signal_dict["detection_direction"] == "N"
        restored = Signal.from_dict(signal_dict)
        assert restored.signal_id == signal.signal_id
        assert restored.position == signal.position

    def test_signal_switch_routing_check(self):
        network = RailwayNetwork()
        network.add_track(TrackSegment(3, 0, TRACK_NW + TRACK_EW))
        network.add_track(TrackSegment(3, 1, TRACK_NS))
        network.add_track(TrackSegment(3, 2, TRACK_NS))
        network.create_block("BLOCK_BRANCH")
        network.assign_track_to_block(3, 2, "BLOCK_BRANCH")
        signal = network.add_signal(3, 1, "N")
        switch = network.get_track(3, 0)
        switch.switch_state = 0
        network.update_signals_from_blocks()
        assert signal.state == SignalState.GREEN
        switch.switch_state = 1
        network.update_signals_from_blocks()
        assert signal.state == SignalState.GREEN


class TestSignalInBlock:
    """Tests for signal placement and monitoring within blocks."""

    def test_signal_tile_keeps_block_section_id(self):
        network = RailwayNetwork()
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            block_id="BLOCK_A", positions=[(0, 0), (1, 0), (2, 0)], name="Block A"
        )
        signal = network.add_signal(1, 0, "E", auto_connect=False)
        track = network.get_track(1, 0)
        assert track.block_section_id == "BLOCK_A"

    def test_is_signal_on_entry_tile_eastbound(self):
        network = RailwayNetwork()
        for x in range(4):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_A", [(0, 0)], name="A")
        network.create_block_with_tracks("BLOCK_B", [(1, 0), (2, 0), (3, 0)], name="B")
        signal = network.add_signal(1, 0, "E", auto_connect=False)
        assert network.is_signal_on_entry_tile(signal) == True
        signal2 = network.add_signal(2, 0, "E", auto_connect=False)
        assert network.is_signal_on_entry_tile(signal2) == False

    def test_is_signal_on_entry_tile_westbound(self):
        network = RailwayNetwork()
        for x in range(4):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_A", [(0, 0), (1, 0), (2, 0)], name="A")
        network.create_block_with_tracks("BLOCK_B", [(3, 0)], name="B")
        signal = network.add_signal(2, 0, "W", auto_connect=False)
        assert network.is_signal_on_entry_tile(signal) == True

    def test_signal_in_middle_cannot_monitor_own_block(self):
        network = RailwayNetwork()
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_A", [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)], name="A"
        )
        signal = network.add_signal(2, 0, "E", auto_connect=False)
        with pytest.raises(ValueError):
            network.connect_signal_to_block(signal.signal_id, "BLOCK_A")

    def test_signal_on_entry_can_monitor_own_block(self):
        network = RailwayNetwork()
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            "BLOCK_A", [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)], name="A"
        )
        signal = network.add_signal(0, 0, "E", auto_connect=False)
        network.connect_signal_to_block(signal.signal_id, "BLOCK_A")
        assert signal.monitored_block_id == "BLOCK_A"


class TestSignalDirectionAwareness:
    """Tests for directional signal behavior with routes."""

    def test_signal_shows_red_for_opposite_direction_route(self):
        network = RailwayNetwork()
        for x in range(4):
            t = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(t)
        network.create_block_with_tracks("BLOCK_A", [(0, 0), (1, 0)])
        network.create_block_with_tracks("BLOCK_B", [(2, 0), (3, 0)])
        signal_east = network.add_signal(1, 0, "E", auto_connect=False)
        network.connect_signal_to_block(signal_east.signal_id, "BLOCK_B")
        network.update_signals_from_blocks()
        assert signal_east.state == SignalState.GREEN
        block_b = network.get_block("BLOCK_B")
        block_b.reserve_for_path("WESTBOUND_ROUTE", None, "W")
        network.update_signals_from_blocks()
        assert signal_east.state == SignalState.RED

    def test_signal_shows_green_for_same_direction_route(self):
        network = RailwayNetwork()
        for x in range(4):
            t = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(t)
        network.create_block_with_tracks("BLOCK_A", [(0, 0), (1, 0)])
        network.create_block_with_tracks("BLOCK_B", [(2, 0), (3, 0)])
        signal_east = network.add_signal(1, 0, "E", auto_connect=False)
        signal_east.approaching_train_id = "TRAIN_001"
        network.connect_signal_to_block(signal_east.signal_id, "BLOCK_B")
        block_b = network.get_block("BLOCK_B")
        block_b.reserve_for_path("EASTBOUND_ROUTE", "TRAIN_001", "E")
        network.update_signals_from_blocks()
        assert signal_east.state == SignalState.GREEN


class TestSwitchSignalBugFixes:
    """Tests for switch and signal routing bug fixes."""

    def test_diverge_only_switch_blocks_inactive_entry_bits_12(self):
        track = TrackSegment(0, 0, 12)
        track.switch_state = 0
        assert track.get_exits("N", use_switch_state=True) == ["E"]
        assert track.get_exits("E", use_switch_state=True) == ["N"]
        assert track.get_exits("W", use_switch_state=True) == []
        track.switch_state = 1
        assert track.get_exits("N", use_switch_state=True) == ["W"]
        assert track.get_exits("W", use_switch_state=True) == ["N"]
        assert track.get_exits("E", use_switch_state=True) == []

    def test_diverge_only_switch_blocks_inactive_entry_bits_20(self):
        track = TrackSegment(0, 0, 20)
        track.switch_state = 0
        assert track.get_exits("E", use_switch_state=True) == ["N"]
        assert track.get_exits("N", use_switch_state=True) == ["E"]
        assert track.get_exits("S", use_switch_state=True) == []
        track.switch_state = 1
        assert track.get_exits("E", use_switch_state=True) == ["S"]
        assert track.get_exits("S", use_switch_state=True) == ["E"]
        assert track.get_exits("N", use_switch_state=True) == []

    def test_standard_switch_blocks_diverge_entry_in_main_state(self):
        configs = [
            (5, "NS+NE"),
            (9, "NS+NW"),
            (17, "NS+SE"),
            (33, "NS+SW"),
            (6, "EW+NE"),
            (10, "EW+NW"),
            (18, "EW+SE"),
            (34, "EW+SW"),
        ]
        for bits, name in configs:
            track = TrackSegment(0, 0, bits)
            assert track.is_switch, f"{name} should be a switch"
            track.switch_state = 0
            main_edges = {"N", "S"} if bits & 1 else {"E", "W"}
            diverge_edges = set(track.edges) - main_edges
            for diverge_edge in diverge_edges:
                exits = track.get_exits(diverge_edge, use_switch_state=True)
                assert exits == [], (
                    f"{name}: Entry from diverge edge {diverge_edge} should be BLOCKED"
                )

    def test_signal_ignores_approach_switch_north_facing(self):
        network = RailwayNetwork()
        network.add_track(TrackSegment(0, 0, TRACK_NW + TRACK_EW))
        network.add_track(TrackSegment(0, 1, TRACK_NS))
        network.add_track(TrackSegment(0, 2, TRACK_NS))
        network.create_block("BLOCK_NORTH")
        network.assign_track_to_block(0, 2, "BLOCK_NORTH")
        signal = network.add_signal(0, 1, "N")
        switch = network.get_track(0, 0)
        for state in [0, 1]:
            switch.switch_state = state
            network.update_signals_from_blocks()
            assert signal.state == SignalState.GREEN


class TestControllerSignalDirectionFiltering:
    """
    Comprehensive tests for signal direction filtering in TrainController.

    These tests ensure the following architectural improvements don't regress:

    ISSUE 1: _is_signal_red_for_block() checked ALL signals monitoring a block
    regardless of direction. An eastbound RED signal would incorrectly block
    a westbound train.

    FIX: Added direction filtering — only signals whose detection_direction
    matches the train's facing_direction are considered.

    ISSUE 2: _is_approaching_yellow_signal() had the same problem.

    FIX: Same direction filtering applied.
    """

    def _build_bidirectional_block_network(self):
        """Build a network with blocks and signals in both directions."""
        from train import Train
        from train_controller import TrainController
        from enums import Direction
        from tests.train_test_helpers import make_train_position

        network = RailwayNetwork()
        # 6-tile east-west track: (0,0) to (5,0)
        for x in range(6):
            t = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(t)

        # Create 3 blocks
        network.create_block_with_tracks("BLOCK_WEST", [(0, 0), (1, 0)])
        network.create_block_with_tracks("BLOCK_MID", [(2, 0), (3, 0)])
        network.create_block_with_tracks("BLOCK_EAST", [(4, 0), (5, 0)])

        # Add signals in BOTH directions on the boundary between WEST and MID
        # Signal monitoring eastbound traffic into BLOCK_MID
        sig_east = network.add_signal(1, 0, "E", auto_connect=False)
        network.connect_signal_to_block(sig_east.signal_id, "BLOCK_MID")

        # Signal monitoring westbound traffic into BLOCK_WEST
        sig_west = network.add_signal(2, 0, "W", auto_connect=False)
        network.connect_signal_to_block(sig_west.signal_id, "BLOCK_WEST")

        # Place a train at (3, 0)
        train = Train(train_id="TEST_TRAIN")
        train.position = make_train_position(
            head_tile=(3, 0),
            facing_direction=Direction.EAST,
            head_distance_cm=20.0,
        )
        network.add_train(train)

        controller = TrainController(train_id="TEST_TRAIN", network=network)

        return network, train, controller, sig_east, sig_west

    def test_red_signal_blocks_same_direction_train(self):
        """A RED signal should block a train traveling in the same direction."""
        from enums import Direction

        network, train, controller, sig_east, sig_west = (
            self._build_bidirectional_block_network()
        )

        # Train facing EAST, eastbound signal is RED
        # Direction set via position (refactored): train.set_cab("A")  # was: Direction.EAST
        sig_east.state = SignalState.RED

        assert controller._is_signal_red_for_block("BLOCK_MID") is True

    def test_red_signal_ignored_for_opposite_direction_train(self):
        """A RED signal for eastbound traffic should NOT block a westbound train."""
        from enums import Direction

        network, train, controller, sig_east, sig_west = (
            self._build_bidirectional_block_network()
        )

        # Train facing WEST, but only eastbound signal is RED
        # Update position to face WEST instead of EAST
        train.position = make_train_position(
            head_tile=(3, 0),
            facing_direction=Direction.WEST,
            head_distance_cm=20.0,
        )
        sig_east.state = SignalState.RED
        sig_west.state = SignalState.GREEN

        # Eastbound RED should be ignored for westbound train
        assert controller._is_signal_red_for_block("BLOCK_MID") is False

    def test_red_signal_both_directions_blocks_any_train(self):
        """If signals in both directions are RED, train is blocked regardless."""
        from enums import Direction

        network, train, controller, sig_east, sig_west = (
            self._build_bidirectional_block_network()
        )

        sig_east.state = SignalState.RED
        sig_west.state = SignalState.RED

        # Eastbound train blocked by eastbound red signal
        # Train already facing EAST from setup
        assert controller._is_signal_red_for_block("BLOCK_MID") is True

        # Westbound train blocked by westbound red signal on BLOCK_WEST
        # Update position to face WEST
        train.position = make_train_position(
            head_tile=(3, 0),
            facing_direction=Direction.WEST,
            head_distance_cm=20.0,
        )
        assert controller._is_signal_red_for_block("BLOCK_WEST") is True


    def test_yellow_signal_ignored_for_opposite_direction(self):
        """A YELLOW signal for eastbound traffic should NOT affect westbound train."""
        from train_controller import TrainController
        from enums import Direction

        network, train, controller, sig_east, sig_west = (
            self._build_bidirectional_block_network()
        )

        # Train facing WEST at (3,0) in BLOCK_MID
        # Update position to face WEST
        train.position = make_train_position(
            head_tile=(3, 0),
            facing_direction=Direction.WEST,
            head_distance_cm=20.0,
        )

        # Eastbound signal on BLOCK_WEST is YELLOW
        sig_east_on_west = network.add_signal(0, 0, "E", auto_connect=False)
        network.connect_signal_to_block(sig_east_on_west.signal_id, "BLOCK_WEST")
        sig_east_on_west.state = SignalState.YELLOW

        # Set up westbound path
        train.reserved_blocks = ["BLOCK_MID", "BLOCK_WEST"]
        train.current_path = [
            (3, 0, "E", "W"),
            (2, 0, "E", "W"),
            (1, 0, "E", "W"),
            (0, 0, "E", "W"),
        ]
        train.path_index = 0

        # The eastbound yellow signal should NOT affect westbound train
        assert controller._is_approaching_yellow_signal() is False

    def test_no_facing_direction_falls_back_to_any_signal(self):
        """If train has no facing_direction, all signals should be considered (safe fallback)."""
        from enums import Direction

        network, train, controller, sig_east, sig_west = (
            self._build_bidirectional_block_network()
        )

        # Train with no facing direction
        train.position = None  # Clear position to have no facing direction
        sig_east.state = SignalState.RED

        # Should still detect RED since we can't determine direction
        assert controller._is_signal_red_for_block("BLOCK_MID") is True

    def test_block_with_no_signal_returns_false(self):
        """Block with no monitoring signal should not be flagged as red."""
        from enums import Direction

        network, train, controller, sig_east, sig_west = (
            self._build_bidirectional_block_network()
        )

        # Direction set via position (refactored): train.set_cab("A")  # was: Direction.EAST

        # BLOCK_EAST has no signals monitoring it
        assert controller._is_signal_red_for_block("BLOCK_EAST") is False

    def test_green_signal_does_not_block(self):
        """GREEN signals should never block regardless of direction."""
        from enums import Direction

        network, train, controller, sig_east, sig_west = (
            self._build_bidirectional_block_network()
        )

        # Direction set via position (refactored): train.set_cab("A")  # was: Direction.EAST
        sig_east.state = SignalState.GREEN

        assert controller._is_signal_red_for_block("BLOCK_MID") is False


class TestSimulationSignalUpdates:
    """
    Tests for signal state updates within the simulation loop.

    ISSUE: update_signals_from_blocks() was only called at path
    creation/cancellation, not during simulation ticks. Signals became stale
    during runtime, showing outdated states as trains moved through blocks.

    FIX: Added network.update_signals_from_blocks() call to
    SimulationController.update() after all trains are updated.
    """

    def test_signals_update_during_simulation_tick(self):
        """Signal states should update after each simulation tick."""
        from train import Train
        from train_controller import TrainController
        from train_physics import TrainPhysics
        from simulation import SimulationController
        from enums import Direction, BlockState

        network = RailwayNetwork()
        for x in range(4):
            t = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(t)

        network.create_block_with_tracks("BLOCK_A", [(0, 0), (1, 0)])
        network.create_block_with_tracks("BLOCK_B", [(2, 0), (3, 0)])

        sig = network.add_signal(1, 0, "E", auto_connect=False)
        network.connect_signal_to_block(sig.signal_id, "BLOCK_B")

        # Signal should start GREEN (BLOCK_B is free)
        network.update_signals_from_blocks()
        assert sig.state == SignalState.GREEN

        # Manually occupy BLOCK_B. Block.state is a derived `@property` in v1
        # (computed from `occupied_by` / `_is_caution` / `reservation`), so
        # assigning `state` directly is forbidden — we set the underlying
        # primitive instead.
        block_b = network.get_block("BLOCK_B")
        block_b.occupied_by = "SOME_TRAIN"

        # Before simulation tick, signal is still GREEN (stale)
        assert sig.state == SignalState.GREEN

        # Create simulation controller and run a tick
        sim = SimulationController(network)
        sim.update(0.05)

        # After tick, signal should now be RED (updated by simulation loop)
        assert sig.state == SignalState.RED

    def test_signal_transitions_through_block_states(self):
        """Signal should track block state changes across multiple ticks."""
        from enums import Direction, BlockState
        from simulation import SimulationController

        network = RailwayNetwork()
        for x in range(6):
            t = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(t)

        network.create_block_with_tracks("BLOCK_A", [(0, 0), (1, 0)])
        network.create_block_with_tracks("BLOCK_B", [(2, 0), (3, 0)])
        network.create_block_with_tracks("BLOCK_C", [(4, 0), (5, 0)])

        sig = network.add_signal(1, 0, "E", auto_connect=False)
        network.connect_signal_to_block(sig.signal_id, "BLOCK_B")
        network.update_signals_from_blocks()

        sim = SimulationController(network)

        # Block B free -> signal GREEN
        assert sig.state == SignalState.GREEN

        # Block B occupied -> signal RED. Block.state is derived from
        # primitives in v1; setting `occupied_by` is sufficient.
        block_b = network.get_block("BLOCK_B")
        block_b.occupied_by = "TRAIN_1"
        sim.update(0.05)
        assert sig.state == SignalState.RED

        # Block B free again -> signal GREEN.
        block_b.occupied_by = None
        sim.update(0.05)
        assert sig.state == SignalState.GREEN


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_signals.py -v")
