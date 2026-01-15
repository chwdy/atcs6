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

    def test_update_from_block(self):
        signal = Signal(
            signal_id="SIG_001",
            position=(0, 0),
            detection_direction="N",
            signal_type=SignalType.BLOCK,
        )
        signal.update_from_block(block_occupied=False)
        assert signal.state == SignalState.GREEN
        signal.update_from_block(block_occupied=True)
        assert signal.state == SignalState.RED

    def test_signal_state_matrix(self):
        signal = Signal(
            signal_id="SIG_001",
            position=(0, 0),
            detection_direction="N",
            signal_type=SignalType.BLOCK,
        )
        signal.update_from_block(block_occupied=False)
        assert signal.state == SignalState.GREEN
        signal.update_from_block(block_occupied=True)
        assert signal.state == SignalState.RED

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


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_signals.py -v")
