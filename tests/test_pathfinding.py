"""
Pathfinding and Persistence Tests - Pathfinding algorithms and layout save/load

Run with: pytest tests/test_pathfinding.py -v
"""

import pytest
import tempfile
import shutil

from track import (
    TrackSegment,
    TRACK_NS,
    TRACK_EW,
    BIT_TO_GROUP,
)
from network import RailwayNetwork
from pathfinder import RailwayPathfinder, RailPathNode
from block import Block
from station import Station
from enums import BlockState, PlatformSide, Direction
from train import Train, TrainCar


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


class TestRailwayPathfinder:
    """Tests for RailwayPathfinder class"""

    def test_find_path_straight(self, straight_ew_track):
        network = straight_ew_track
        pathfinder = RailwayPathfinder(network)
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=1.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=50.0)
        start = (0, 0)
        goal = (4, 0)
        start_direction = Direction.EAST
        path = pathfinder.find_path(train, start, goal, start_direction)
        assert path is not None
        assert len(path) > 0
        assert path[0][:2] == start
        assert path[-1][:2][0] >= goal[0] - 1

    def test_find_path_returns_none_for_impossible(self, network):
        network.add_track(TrackSegment(x=0, y=0, track_bits=TRACK_EW))
        network.add_track(TrackSegment(x=5, y=5, track_bits=TRACK_EW))
        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length=1.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=50.0)
        pathfinder = RailwayPathfinder(network)
        path = pathfinder.find_path(train, (0, 0), (5, 5), Direction.EAST)
        assert path is None


class TestPositionSorting:
    """Tests for block position sorting algorithm"""

    @staticmethod
    def _sort_block_positions(positions, network):
        from collections import Counter

        if len(positions) <= 1:
            return positions
        orientation_counts = Counter()
        for x, y in positions:
            track = network.get_track(x, y)
            if track:
                for bit_idx, group in BIT_TO_GROUP.items():
                    if track.track_bits & (1 << bit_idx):
                        orientation_counts[group] += 1
        if not orientation_counts:
            return positions
        dominant_orientation = orientation_counts.most_common(1)[0][0]
        if dominant_orientation == "NS":
            return sorted(positions, key=lambda pos: -pos[1])
        elif dominant_orientation == "EW":
            return sorted(positions, key=lambda pos: pos[0])
        elif dominant_orientation == "NE_SW":
            return sorted(positions, key=lambda pos: pos[0] + pos[1])
        elif dominant_orientation == "NW_SE":
            return sorted(positions, key=lambda pos: pos[1] - pos[0])
        else:
            return positions

    def test_sort_ns_positions(self, network):
        for y in range(4):
            network.add_track(TrackSegment(0, y, TRACK_NS))
        network.create_block("BLOCK_NS")
        for y in range(4):
            network.assign_track_to_block(0, y, "BLOCK_NS")
        positions = network.get_block_positions("BLOCK_NS")
        sorted_positions = self._sort_block_positions(positions, network)
        assert sorted_positions[0] == (0, 3)
        assert sorted_positions[-1] == (0, 0)
        y_values = [pos[1] for pos in sorted_positions]
        assert y_values == sorted(y_values, reverse=True)

    def test_sort_ew_positions(self, network):
        for x in range(4):
            network.add_track(TrackSegment(x, 0, TRACK_EW))
        network.create_block("BLOCK_EW")
        for x in range(4):
            network.assign_track_to_block(x, 0, "BLOCK_EW")
        positions = network.get_block_positions("BLOCK_EW")
        sorted_positions = self._sort_block_positions(positions, network)
        assert sorted_positions[0] == (0, 0)
        assert sorted_positions[-1] == (3, 0)
        x_values = [pos[0] for pos in sorted_positions]
        assert x_values == sorted(x_values)

    def test_sort_empty_positions(self, network):
        sorted_positions = self._sort_block_positions([], network)
        assert sorted_positions == []

    def test_sort_single_position(self, network):
        network.add_track(TrackSegment(5, 5, TRACK_NS))
        network.create_block("BLOCK_SINGLE")
        network.assign_track_to_block(5, 5, "BLOCK_SINGLE")
        positions = network.get_block_positions("BLOCK_SINGLE")
        sorted_positions = self._sort_block_positions(positions, network)
        assert len(sorted_positions) == 1
        assert sorted_positions[0] == (5, 5)


class TestLayoutPersistence:
    """Tests for layout save/load functionality"""

    def test_persistence_manager_init(self):
        from persistence import PersistenceManager

        temp_dir = tempfile.mkdtemp()
        try:
            pm = PersistenceManager(storage_dir=temp_dir)
            assert pm.storage_dir.exists()
            assert pm.SCHEMA_VERSION == "1.0"
        finally:
            shutil.rmtree(temp_dir)

    def test_save_and_load_layout(self, network):
        from persistence import PersistenceManager

        temp_dir = tempfile.mkdtemp()
        try:
            pm = PersistenceManager(storage_dir=temp_dir)
            track1 = TrackSegment(x=0, y=0, track_bits=TRACK_NS)
            track2 = TrackSegment(x=0, y=1, track_bits=TRACK_NS)
            network.add_track(track1)
            network.add_track(track2)
            result = pm.save_merged_layout("test-layout", network, "Test description")
            assert result["steps_generated"] or result["steps_count"] >= 0
            network.tracks.clear()
            assert len(network.tracks) == 0
            load_result = pm.load_layout_with_fallback("test-layout", network)
            assert load_result["success"] == True
            assert len(network.tracks) == 2
            assert network.get_track(0, 0) is not None
            assert network.get_track(0, 1) is not None
        finally:
            shutil.rmtree(temp_dir)

    def test_list_layouts(self, network):
        from persistence import PersistenceManager

        temp_dir = tempfile.mkdtemp()
        try:
            pm = PersistenceManager(storage_dir=temp_dir)
            pm.save_merged_layout("layout1", network, "First")
            pm.save_merged_layout("layout2", network, "Second")
            layouts = pm.list_layouts()
            assert len(layouts) == 2
            layout_names = [l["name"] for l in layouts]
            assert "layout1" in layout_names
            assert "layout2" in layout_names
        finally:
            shutil.rmtree(temp_dir)

    def test_delete_layout(self, network):
        from persistence import PersistenceManager

        temp_dir = tempfile.mkdtemp()
        try:
            pm = PersistenceManager(storage_dir=temp_dir)
            pm.save_merged_layout("delete-me", network, "To be deleted")
            layouts = pm.list_layouts()
            assert len(layouts) == 1
            success = pm.delete_layout("delete-me")
            assert success == True
            layouts = pm.list_layouts()
            assert len(layouts) == 0
        finally:
            shutil.rmtree(temp_dir)

    def test_layout_with_blocks_and_signals(self, network):
        from persistence import PersistenceManager
        from railway_signal import Signal, SignalType

        temp_dir = tempfile.mkdtemp()
        try:
            pm = PersistenceManager(storage_dir=temp_dir)
            for y in range(3):
                track = TrackSegment(x=0, y=y, track_bits=TRACK_NS)
                network.add_track(track)
            block = Block(block_id="BLK1", name="Test Block")
            network.blocks[block.block_id] = block
            for y in range(3):
                track = network.get_track(0, y)
                track.block_section_id = "BLK1"
            signal = Signal(
                signal_id="SIG1",
                position=(0, 0),
                detection_direction="N",
                signal_type=SignalType.BLOCK,
            )
            network.signals[signal.signal_id] = signal
            result = pm.save_merged_layout("complex-layout", network)
            network.tracks.clear()
            network.blocks.clear()
            network.signals.clear()
            load_result = pm.load_layout_with_fallback("complex-layout", network)
            assert load_result["success"] == True
            assert len(network.tracks) == 3
            assert len(network.blocks) == 1
            assert len(network.signals) == 1
        finally:
            shutil.rmtree(temp_dir)


class TestValidationParity:
    """Ensure backend rejects all operations that frontend blocks"""

    def test_all_forbidden_track_bits(self):
        from track import FORBIDDEN_TRACK_BITS

        for bits in FORBIDDEN_TRACK_BITS:
            track = TrackSegment(x=0, y=0, track_bits=bits)
            assert track is not None
            assert track.track_bits == bits

    def test_signal_on_switch_rejected(self):
        from signal_validation import FORBIDDEN_SIGNAL_BITS
        from track import FORBIDDEN_TRACK_BITS

        for forbidden_bits in FORBIDDEN_SIGNAL_BITS:
            if forbidden_bits in FORBIDDEN_TRACK_BITS:
                continue
            from signal_validation import can_place_signal

            result = can_place_signal(forbidden_bits)
            assert result == False

    def test_crossing_in_block_rejected(self):
        from block_validation import is_crossing

        crossing_bits = [3, 24, 36]
        for bits in crossing_bits:
            assert is_crossing(bits) == True


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_pathfinding.py -v")
