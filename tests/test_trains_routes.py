"""
Route execution tests - Routes, path assignment, stations

Run with: pytest tests/test_trains_routes.py -v
"""

import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from train_controller import TrainController
from path_dispatcher import PathDispatcher


@pytest.fixture
def network():
    return RailwayNetwork()


class TestBlockTileSyncValidation:
    """
    Tests for block-tile position sync validation.

    These tests verify that the sync validation detects and corrects
    desyncs between tiles and blocks.
    """

    @pytest.fixture
    def physics_setup(self):
        """Create network, train, and physics."""
        from network import RailwayNetwork
        from train import Train
        from train_physics import TrainPhysics
        from train_controller import TrainController
        from block import Block
        from enums import Direction, BlockState

        network = RailwayNetwork()

        # Create track with blocks
        from track import TrackSegment, TRACK_EW

        # v1: Block.state is read-only; new blocks are FREE by default.
        block = Block(block_id="B1", length_cm=300.0)
        network.blocks["B1"] = block

        for x in range(6):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "B1"
            network.add_track(track)

        train = Train(train_id="T1", max_speed=60.0)
        network.add_train(train)

        controller = TrainController(train_id="T1", network=network)
        physics = TrainPhysics(train_id="T1", network=network, controller=controller)
        # v1: head/tail and tile occupancy live on the controller span, not
        # train.position. Seed the physics movement scratchpad so
        # _validate_position_sync / _get_blocks_for_train derive tile occupancy
        # from it.
        from train_physics import _SpanWorkState

        physics._work = _SpanWorkState(
            head_is_a=True,
            end_a_tile=(2, 0),
            end_a_distance_cm=25.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
            occupied_blocks=["B1"],
        )

        return network, train, physics

    def test_sync_validation_returns_true_when_synced(self, physics_setup):
        """Validation should return True when tiles and the local tracker match.

        v1: physics._validate_position_sync now compares tile-derived blocks
        against the physics-local last-occupied-blocks tracker (not against
        train.position.occupied_blocks).
        """
        _, train, physics = physics_setup
        # Seed the physics internal tracker so tiles match the tracker.
        physics._last_occupied_blocks = {"B1"}
        result = physics._validate_position_sync(train)
        assert result is True, "Should return True when synced"

    def test_sync_validation_detects_desync(self, physics_setup):
        """Validation should detect when local tracker doesn't match tiles."""
        _, train, physics = physics_setup
        # Force a tracker mismatch.
        physics._last_occupied_blocks = set()
        result = physics._validate_position_sync(train)
        assert result is False, "Should return False when desynced"

    # v1: removed test_sync_validation_auto_corrects — auto-correction was
    # removed; corrections live in the dispatcher progress flow now.


class TestMultiTilePlatformStopping:
    """
    Tests for train stopping at multi-tile platforms.

    BUG: When a path ends at a multi-tile station platform, the train should stop
    at the END of the platform (last tile), not at the first tile. This test
    ensures the train controller correctly calculates clearance to the end of
    the platform regardless of block position ordering.

    Key issue: _calculate_clearance_to_end_of_current_block was checking if there
    were more station tiles after the current one in block_positions, but
    block_positions is in arbitrary insertion order, not travel direction order.
    """

    @pytest.fixture
    def multi_tile_station_network(self, network):
        """Create a network with a 2-tile station on the east branch."""
        from track import TRACK_NS, TRACK_EW, TRACK_NW

        # Create vertical track layout: (9,0) to (9,4)
        # Train will travel NORTH from (9,0) to station at (9,3)-(9,4)
        for y in range(5):
            track = TrackSegment(9, y, track_bits=TRACK_NS)
            network.add_track(track)

        # Create blocks
        network.create_block_with_tracks(
            block_id="BLOCK_START",
            positions=[(9, 0)],
            name="Start Block",
            length_cm=50.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_MID",
            positions=[(9, 1), (9, 2)],
            name="Middle Block",
            length_cm=100.0,
        )
        # Multi-tile station block spanning (9,3) and (9,4)
        # Add tracks in reverse order to test insertion order issue
        network.create_block_with_tracks(
            block_id="STATION_BLOCK",
            positions=[(9, 4), (9, 3)],  # Note: reversed order!
            name="East Terminal",
            length_cm=100.0,
        )

        # Create station platform spanning both tiles
        from railway_platform import Platform

        station = Platform(
            platform_id="EAST_TERMINAL",
            name="East Terminal",
            positions=[(9, 3), (9, 4)],
            length_cm=100.0,
            platform_side="both",
        )
        network.stations["EAST_TERMINAL"] = station

        # Mark tracks as having station
        network.get_track(9, 3).station_id = "EAST_TERMINAL"
        network.get_track(9, 4).station_id = "EAST_TERMINAL"

        return network

    def test_clearance_includes_full_multi_tile_station_length(
        self, multi_tile_station_network
    ):
        """
        _calculate_clearance_to_end_of_current_block() should calculate distance
        to the end of the platform, even when block_positions are in arbitrary order.

        This ensures ATO allows the train to travel the full platform length
        before stopping, rather than stopping at the first tile.
        """
        network = multi_tile_station_network

        # Create train at first station tile (9,3), traveling NORTH
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="CLEARANCE_TEST", consist=[car])
        network.add_train(train)

        controller = TrainController(train_id=train.train_id, network=network)
        # Place train at first station tile (9,3) with path to last tile (9,4).
        # leading_end was A -> default head end (end_a) with no cab set.
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(9, 3), distance_cm=0.0, entry_edge="S"),
            end_b=SpanEndLocator(tile=(9, 3), distance_cm=0.0, entry_edge="S"),
            occupied_tiles=[(9, 3)],
            occupied_blocks=["STATION_BLOCK"],
        )
        # `_calculate_clearance_to_end_of_current_block` reads the controller's
        # current_span and the network, not current_speed or current_path —
        # both of those live on TrainController / PathDispatcher in v1.

        # Calculate clearance - should include distance to end of platform
        clearance = controller._calculate_clearance_to_end_of_current_block()

        # Station block is 2 tiles (9,3) and (9,4), each 50cm = 100cm total
        # Train is at start of tile (9,3), so clearance should be ~100cm
        # (remaining in current tile + full next tile)
        assert clearance >= 75.0, (
            f"Clearance should include full platform length (~100cm), "
            f"but got {clearance}cm. "
            f"The train will stop prematurely at the first tile instead of "
            f"continuing to the end of the multi-tile platform. "
            f"Block positions may be in reverse insertion order: "
            f"{network.get_block_positions('STATION_BLOCK')}"
        )

    def test_clearance_calculation_with_reversed_block_positions(
        self, multi_tile_station_network
    ):
        """
        _calculate_clearance_to_end_of_current_block should work correctly
        even when block positions are in reverse insertion order.

        The bug was that the code checked for 'more station tiles after' using
        block_positions index order, but block_positions can be in any order
        depending on insertion sequence.
        """
        network = multi_tile_station_network

        # Create train already in the station block at first tile (9,3)
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="CLEARANCE_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 9, 3, Direction.NORTH)

        # Create controller
        controller = TrainController(train_id=train.train_id, network=network)

        # Manually set span to be at start of tile (9,3). leading_end was A ->
        # default head end (end_a) with no cab set.
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(9, 3), distance_cm=0.0, entry_edge="S"),
            end_b=SpanEndLocator(tile=(9, 3), distance_cm=0.0, entry_edge="S"),
            occupied_tiles=[(9, 3)],
            occupied_blocks=["STATION_BLOCK"],
        )

        # Calculate clearance to end of current block
        clearance = controller._calculate_clearance_to_end_of_current_block()

        # Clearance should be approximately 100cm (two 50cm tiles: (9,3) and (9,4))
        # Not just 50cm for the current tile
        assert clearance >= 75.0, (
            f"Clearance should include distance to end of platform (~100cm), "
            f"but got {clearance}cm. "
            f"Block positions were inserted in reverse order [(9,4), (9,3)], "
            f"which may cause the clearance calculation to stop early "
            f"if not properly sorting by travel direction."
        )
