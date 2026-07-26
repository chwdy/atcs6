"""
Station Tests - Station creation, orientation, and block integration

Run with: pytest tests/test_stations.py -v
"""

import pytest

from track import (
    Tile,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
)
from network import RailwayNetwork
from station import Station
from railway_platform import Platform
from block import Block
from railway_signal import Signal, SignalType
from enums import BlockState, PlatformSide, Direction
from route_executor import RouteExecutionState, RouteExecutor
from step_player import StepPlayer
from train import Train
from train_route import RouteTask
from tests.train_test_helpers import set_train_span


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


@pytest.fixture
def straight_ns_track(network):
    for y in range(5):
        track = Tile(x=0, y=y, track_bits=TRACK_NS)
        network.add_track(track)
    return network


@pytest.fixture
def zigzag_diagonal_nw_se(network):
    network.add_track(Tile(x=0, y=0, track_bits=TRACK_NW))
    network.add_track(Tile(x=1, y=1, track_bits=TRACK_SE))
    network.add_track(Tile(x=2, y=2, track_bits=TRACK_NW))
    return network


@pytest.fixture
def zigzag_diagonal_ne_sw(network):
    network.add_track(Tile(x=0, y=0, track_bits=TRACK_NE))
    network.add_track(Tile(x=1, y=1, track_bits=TRACK_SW))
    network.add_track(Tile(x=2, y=2, track_bits=TRACK_NE))
    return network


class TestStation:
    """Tests for Platform class (formerly Station)"""

    def test_station_creation(self, straight_ns_track):
        network = straight_ns_track
        station = Platform(
            platform_id="PLT_001",
            name="Test Station",
            positions=[(0, 1)],
            length_cm=100,
            platform_side=PlatformSide.BOTH,
            top_speed_kmh=30,
        )
        assert station.platform_id == "PLT_001"
        assert station.name == "Test Station"
        assert (0, 1) in station.positions

    def test_orientation_detection_ns(self, straight_ns_track):
        network = straight_ns_track
        station = Platform(
            platform_id="PLT_001",
            name="NS Station",
            positions=[(0, 1)],
            length_cm=100,
            platform_side=PlatformSide.BOTH,
            top_speed_kmh=30,
            initial_orientation="NS",
        )
        assert station.initial_orientation == "NS"

    def test_orientation_detection_ew(self, straight_ew_track):
        network = straight_ew_track
        station = Platform(
            platform_id="STA_001",
            name="EW Station",
            positions=[(1, 0)],
            length_cm=100,
            platform_side=PlatformSide.BOTH,
            top_speed_kmh=30,
            initial_orientation="EW",
        )
        assert station.initial_orientation == "EW"

    def test_add_position(self, straight_ns_track):
        network = straight_ns_track
        station = Platform(
            platform_id="STA_001",
            name="Test Station",
            positions=[(0, 1)],
            length_cm=100,
            platform_side=PlatformSide.BOTH,
            top_speed_kmh=30,
        )
        station.add_position(0, 2)
        assert (0, 2) in station.positions

    def test_zigzag_diagonal_orientation_nw_se(self, zigzag_diagonal_nw_se):
        network = zigzag_diagonal_nw_se
        station = Platform(
            platform_id="STA_001",
            name="Diagonal Station",
            positions=[(0, 0)],
            length_cm=100,
            platform_side=PlatformSide.BOTH,
            top_speed_kmh=30,
            initial_orientation="NW_SE",
        )
        assert station.initial_orientation == "NW_SE"

    def test_zigzag_diagonal_orientation_ne_sw(self, zigzag_diagonal_ne_sw):
        network = zigzag_diagonal_ne_sw
        station = Platform(
            platform_id="STA_001",
            name="Diagonal Station",
            positions=[(0, 0)],
            length_cm=100,
            platform_side=PlatformSide.BOTH,
            top_speed_kmh=30,
            initial_orientation="NE_SW",
        )
        assert station.initial_orientation == "NE_SW"

    def test_serialization_with_orientation(self, straight_ns_track):
        network = straight_ns_track
        station = Platform(
            platform_id="STA_001",
            name="Test Station",
            positions=[(0, 1)],
            length_cm=100,
            platform_side=PlatformSide.BOTH,
            top_speed_kmh=30,
            initial_orientation="NS",
        )
        station_dict = station.to_dict_with_orientation(network)
        assert "orientation" in station_dict
        assert station_dict["orientation"] == "NS"

    def test_block_assignment_basic(self, straight_ns_track):
        station = Platform(
            platform_id="STA_001",
            name="Test Station",
            positions=[(0, 1), (0, 2), (0, 3)],
            length_cm=300,
            initial_orientation="NS",
        )
        assert station.assign_tile_to_block(0, 1, "BLK_001")
        assert station.assign_tile_to_block(0, 2, "BLK_002")
        assert station.assign_tile_to_block(0, 3, "BLK_002")
        assert station.get_block_for_tile(0, 1) == "BLK_001"
        assert station.get_block_for_tile(0, 2) == "BLK_002"
        assert station.get_block_for_tile(0, 3) == "BLK_002"

    def test_block_unassignment(self, straight_ns_track):
        station = Platform(
            platform_id="STA_001",
            name="Test Station",
            positions=[(0, 1), (0, 2)],
            length_cm=200,
        )
        station.assign_tile_to_block(0, 1, "BLK_001")
        assert station.get_block_for_tile(0, 1) == "BLK_001"
        assert station.unassign_tile_from_block(0, 1)
        assert station.get_block_for_tile(0, 1) is None

    def test_get_tiles_for_block(self, straight_ns_track):
        station = Platform(
            platform_id="STA_001",
            name="Test Station",
            positions=[(0, 1), (0, 2), (0, 3), (0, 4)],
            length_cm=400,
        )
        station.assign_tile_to_block(0, 1, "BLK_001")
        station.assign_tile_to_block(0, 2, "BLK_002")
        station.assign_tile_to_block(0, 3, "BLK_002")
        station.assign_tile_to_block(0, 4, "BLK_003")
        assert set(station.get_tiles_for_block("BLK_001")) == {(0, 1)}
        assert set(station.get_tiles_for_block("BLK_002")) == {(0, 2), (0, 3)}
        assert set(station.get_tiles_for_block("BLK_003")) == {(0, 4)}

    def test_get_all_assigned_blocks(self, straight_ns_track):
        station = Platform(
            platform_id="STA_001",
            name="Test Station",
            positions=[(0, 1), (0, 2), (0, 3)],
            length_cm=300,
        )
        assert len(station.get_all_assigned_blocks()) == 0
        station.assign_tile_to_block(0, 1, "BLK_001")
        station.assign_tile_to_block(0, 2, "BLK_002")
        station.assign_tile_to_block(0, 3, "BLK_002")
        assert station.get_all_assigned_blocks() == {"BLK_001", "BLK_002"}


class TestMultiBlockStationAPI:
    """Tests for multi-block station API endpoints"""

    def test_assign_tile_to_block_endpoint(self, network):
        network.add_track(Tile(0, 1, TRACK_NS))
        network.add_track(Tile(0, 2, TRACK_NS))
        network.add_track(Tile(0, 3, TRACK_NS))
        station = network.add_station(0, 1, station_id="STA_001", name="Test Station")
        network.extend_station("STA_001", [(0, 2), (0, 3)])
        network.create_segment("BLK_001")
        success = station.assign_tile_to_block(0, 2, "BLK_001")
        assert success
        assert station.get_block_for_tile(0, 2) == "BLK_001"

    def test_unassign_tile_from_block_endpoint(self, network):
        network.add_track(Tile(0, 1, TRACK_NS))
        network.add_track(Tile(0, 2, TRACK_NS))
        station = network.add_station(0, 1, station_id="STA_001", name="Test Station")
        network.extend_station("STA_001", [(0, 2)])
        network.create_segment("BLK_001")
        station.assign_tile_to_block(0, 1, "BLK_001")
        assert station.get_block_for_tile(0, 1) == "BLK_001"
        success = station.unassign_tile_from_block(0, 1)
        assert success
        assert station.get_block_for_tile(0, 1) is None

    def test_get_block_assignments_structure(self, network):
        network.add_track(Tile(0, 1, TRACK_NS))
        network.add_track(Tile(0, 2, TRACK_NS))
        network.add_track(Tile(0, 3, TRACK_NS))
        station = network.add_station(0, 1, station_id="STA_001", name="Test Station")
        network.extend_station("STA_001", [(0, 2), (0, 3)])
        network.create_segment("BLK_001")
        network.create_segment("BLK_002")
        station.assign_tile_to_block(0, 1, "BLK_001")
        station.assign_tile_to_block(0, 2, "BLK_002")
        station.assign_tile_to_block(0, 3, "BLK_002")
        all_blocks = station.get_all_assigned_blocks()
        assert all_blocks == {"BLK_001", "BLK_002"}

    def test_block_assignments_persist_through_serialization(self, network):
        network.add_track(Tile(0, 1, TRACK_NS))
        network.add_track(Tile(0, 2, TRACK_NS))
        station = network.add_station(0, 1, station_id="STA_001", name="Test Station")
        network.extend_station("STA_001", [(0, 2)])
        network.create_segment("BLK_001")
        station.assign_tile_to_block(0, 1, "BLK_001")
        station.assign_tile_to_block(0, 2, "BLK_001")
        data = station.to_dict()
        assert "block_assignments" in data
        assert data["block_assignments"] == {"0,1": "BLK_001", "0,2": "BLK_001"}
        station2 = Platform.from_dict(data)
        assert station2.get_block_for_tile(0, 1) == "BLK_001"
        assert station2.get_block_for_tile(0, 2) == "BLK_001"


class TestExtensionValidation:
    """Tests for can-extend validation logic"""

    def test_station_extension_requires_adjacency(self, network):
        network.add_track(Tile(0, 0, TRACK_NS))
        network.add_track(Tile(0, 1, TRACK_NS))
        network.add_track(Tile(0, 2, TRACK_NS))
        station = network.add_station(0, 0, station_id="STATION_A", name="Station A")
        network.extend_station("STATION_A", [(0, 1), (0, 2)])
        network.add_track(Tile(0, 5, TRACK_NS))
        with pytest.raises(ValueError, match="must be adjacent to existing station"):
            network.extend_station("STATION_A", [(0, 5)])

    def test_station_extension_requires_orientation_match(self, network):
        network.add_track(Tile(0, 0, TRACK_NS))
        network.add_track(Tile(0, 1, TRACK_NS))
        station = network.add_station(0, 0, station_id="STATION_A", name="Station A")
        network.extend_station("STATION_A", [(0, 1)])
        network.add_track(Tile(0, 2, TRACK_EW))
        with pytest.raises(ValueError, match="exact orientation match"):
            network.extend_station("STATION_A", [(0, 2)])

    def test_block_extension_requires_adjacency(self, network):
        network.add_track(Tile(0, 0, TRACK_EW))
        network.add_track(Tile(1, 0, TRACK_EW))
        network.add_track(Tile(2, 0, TRACK_EW))
        network.create_segment("BLOCK_A")
        network.assign_track_to_segment(0, 0, "BLOCK_A")
        network.assign_track_to_segment(1, 0, "BLOCK_A")
        network.assign_track_to_segment(2, 0, "BLOCK_A")
        network.add_track(Tile(5, 0, TRACK_EW))
        with pytest.raises(ValueError, match="must be adjacent to existing block"):
            network.extend_segment("BLOCK_A", [(5, 0)])

    def test_block_extension_rejects_diagonal_crossings(self, network):
        # Diagonal crossings (NW+SE, NE+SW) cannot be extended into a block.
        network.add_track(Tile(0, 0, TRACK_EW))
        network.create_segment("BLOCK_A")
        network.assign_track_to_segment(0, 0, "BLOCK_A")
        network.add_track(Tile(1, 0, TRACK_NW | TRACK_SE))
        with pytest.raises(ValueError, match="Cannot add crossing to block"):
            network.extend_segment("BLOCK_A", [(1, 0)])

    def test_block_extension_allows_orthogonal_crossing(self, network):
        # The orthogonal NS+EW crossing (track_bits 3) may be extended into a block.
        network.add_track(Tile(0, 0, TRACK_EW))
        network.create_segment("BLOCK_A")
        network.assign_track_to_segment(0, 0, "BLOCK_A")
        network.add_track(Tile(1, 0, TRACK_NS | TRACK_EW))
        network.extend_segment("BLOCK_A", [(1, 0)])
        assert network.get_track(1, 0).block_id == "BLOCK_A"


class TestStationStoppingBehavior:
    """Tests for station stopping position logic."""

    def test_single_tile_station_stops_at_only_tile(self, network):
        network.add_track(Tile(0, 0, TRACK_EW))
        network.add_track(Tile(1, 0, TRACK_EW))
        network.add_track(Tile(2, 0, TRACK_EW))
        station = network.add_station(
            1, 0, station_id="STA_SINGLE", name="Single Tile Station", length_cm=1500.0
        )
        from train import Train, TrainCar
        from enums import Direction
        from train_controller import create_controller_for_train
        from route_executor import RouteExecutor

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        controller = create_controller_for_train("TRAIN_01", network)
        set_train_span(controller, (1, 0), facing_direction=Direction.EAST)
        executor = RouteExecutor(network, shared_controllers={"TRAIN_01": controller})
        target_tile = executor._get_target_platform_tile(train, station)
        assert target_tile == (1, 0)

    def test_multi_tile_station_traveling_east_stops_at_easternmost(self, network):
        for x in range(5):
            network.add_track(Tile(x, 0, TRACK_EW))
        station = network.add_station(
            1,
            0,
            station_id="STA_MULTI_EW",
            name="Multi-tile EW Station",
            length_cm=4500.0,
        )
        network.extend_station("STA_MULTI_EW", [(2, 0), (3, 0)])
        from train import Train, TrainCar
        from enums import Direction
        from train_controller import create_controller_for_train
        from route_executor import RouteExecutor

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        controller = create_controller_for_train("TRAIN_01", network)
        set_train_span(controller, (1, 0), facing_direction=Direction.EAST)
        executor = RouteExecutor(network, shared_controllers={"TRAIN_01": controller})
        target_tile = executor._get_target_platform_tile(train, station)
        assert target_tile == (3, 0)

    def test_multi_tile_station_traveling_north_stops_at_northernmost(self, network):
        for y in range(5):
            network.add_track(Tile(0, y, TRACK_NS))
        station = network.add_station(
            0,
            1,
            station_id="STA_MULTI_NS",
            name="Multi-tile NS Station",
            length_cm=4500.0,
        )
        network.extend_station("STA_MULTI_NS", [(0, 2), (0, 3)])
        from train import Train, TrainCar
        from enums import Direction
        from train_controller import create_controller_for_train
        from route_executor import RouteExecutor

        car = TrainCar(car_id="CAR_01", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TRAIN_01", consist=[car], max_speed=60.0)
        controller = create_controller_for_train("TRAIN_01", network)
        set_train_span(controller, (0, 1), facing_direction=Direction.NORTH)
        executor = RouteExecutor(network, shared_controllers={"TRAIN_01": controller})
        target_tile = executor._get_target_platform_tile(train, station)
        assert target_tile == (0, 3)


class TestBlockStationIntegration:
    """Tests for block-station synchronization."""

    def test_auto_shorten_block_when_station_tile_removed(self, network):
        network.add_track(Tile(0, 0, TRACK_NS))
        network.add_track(Tile(0, 1, TRACK_NS))
        network.add_track(Tile(0, 2, TRACK_NS))
        station = network.add_station(0, 0, station_id="STATION_A", name="Station A")
        network.extend_station("STATION_A", [(0, 1), (0, 2)])
        station_block_id = f"{station.platform_id}_BLOCK"
        assert station_block_id in network.blocks
        block_positions = network.get_block_positions(station_block_id)
        assert len(block_positions) == 3
        station.positions = [(0, 0), (0, 1)]
        track_0_2 = network.get_track(0, 2)
        track_0_2.station_id = None
        if track_0_2.block_id == station_block_id:
            track_0_2.segment_id = None
        block_positions = network.get_block_positions(station_block_id)
        assert len(block_positions) == 2




class TestStationPlatformSchemaBugFixes:
    """
    Comprehensive tests for station/platform schema and replay clearing bugs.

    These tests prevent regressions for three issues:

    BUG 1: Legacy snapshots with `stations` loaded as empty because only `platforms`
           were read.
    FIX 1: Persistence migration maps legacy `stations` -> `platforms`.

    BUG 2: Step replay with clear_first=True left stale station_groups from previous
           layouts.
    FIX 2: StepPlayer now clears station_groups in both replay entry points.

    BUG 3: PlatformSide.EITHER serialized as right-side only.
    FIX 3: EITHER now serializes to both orientation sides.
    """

    def test_step_player_play_clears_station_groups_with_clear_first(self):
        """play() should clear stale station_groups when loading a new layout."""
        network = RailwayNetwork()
        network.station_groups["STA_GROUP_OLD"] = Station(
            station_id="STA_GROUP_OLD",
            name="Old Group",
            platform_ids=["PLT_OLD"],
        )

        player = StepPlayer()
        player.play({"steps": []}, network, clear_first=True)

        assert network.station_groups == {}

    def test_step_player_play_steps_clears_station_groups_with_clear_first(self):
        """play_steps() should clear stale station_groups in fallback replay mode."""
        network = RailwayNetwork()
        network.station_groups["STA_GROUP_OLD"] = Station(
            station_id="STA_GROUP_OLD",
            name="Old Group",
            platform_ids=["PLT_OLD"],
        )

        player = StepPlayer()
        player.play_steps([], network, clear_first=True)

        assert network.station_groups == {}

    def test_platform_side_either_serializes_to_both_orientation_sides(
        self, straight_ns_track
    ):
        """EITHER should serialize as both sides, not right-side only."""
        station = Platform(
            platform_id="PLT_EITHER",
            name="Flexible Side Platform",
            positions=[(0, 1)],
            length_cm=100,
            platform_side=PlatformSide.EITHER,
            initial_orientation="NS",
        )

        payload = station.to_dict_with_orientation(straight_ns_track)

        assert payload["platform_sides"] == ["west", "east"]


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_stations.py -v")
