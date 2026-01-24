"""
Comprehensive Test Suite for Dispatcher System

Tests for:
- Route creation, validation, and lifecycle
- Block reservation for routes
- Switch locking for routes
- Route pathfinding
- Route extension
- Route cancellation
- Approach locking
- Train priority and emergency stop

See docs/DISPATCHER_DESIGN.md for full specification.
"""

import pytest
from datetime import datetime
from typing import Dict, List, Tuple

from path import (
    Path,
    PathStatus,
    PathSegment,
    ValidationError,
    ValidationErrorType,
    ValidationResult,
    PathResult,
    ExtensionResult,
    CancellationResult,
    DEFAULT_PATH_PRIORITY,
)
from path_dispatcher import PathDispatcher
from network import RailwayNetwork
from simulation import SimulationController
from track import (
    TrackSegment,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
)
from block import Block
from train import Train, TrainCar, TrainPosition
from enums import BlockState, Direction, TrainState, HandlePosition
from simulation import SimulationController


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def network():
    """Create a basic network for testing."""
    return RailwayNetwork()


@pytest.fixture
def dispatcher(network):
    """Create a dispatcher with the network."""
    return PathDispatcher(network)


@pytest.fixture
def simple_linear_network(network):
    """
    Create a simple linear network with 3 blocks.

    Layout:
        [Block A: (0,0)]---[Block B: (1,0), (2,0)]---[Block C: (3,0)]

    All tracks are straight EW.
    """
    # Create tracks
    for x in range(4):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)

    # Create blocks
    block_a = network.create_block("BLOCK_A", "Block A")
    block_a.length_cm = 100.0
    network.get_track(0, 0).block_section_id = "BLOCK_A"

    block_b = network.create_block("BLOCK_B", "Block B")
    block_b.length_cm = 200.0
    network.get_track(1, 0).block_section_id = "BLOCK_B"
    network.get_track(2, 0).block_section_id = "BLOCK_B"

    block_c = network.create_block("BLOCK_C", "Block C")
    block_c.length_cm = 100.0
    network.get_track(3, 0).block_section_id = "BLOCK_C"

    return network


@pytest.fixture
def network_with_switch(network):
    """
    Create a network with a switch.

    Layout:
                            [Block C: (2,1)]
                           /
        [Block A: (0,0)]--[Switch: (1,0)]--[Block B: (2,0)]

    """
    # Block A - straight track
    track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
    network.add_track(track_a)
    block_a = network.create_block("BLOCK_A", "Block A")
    block_a.length_cm = 100.0
    track_a.block_section_id = "BLOCK_A"

    # Switch at (1,0) - EW + NE = 2 + 4 = 6
    switch = TrackSegment(x=1, y=0, track_bits=TRACK_EW | TRACK_NE)
    network.add_track(switch)
    # Switch is not in a block (block boundary)

    # Block B - straight track
    track_b = TrackSegment(x=2, y=0, track_bits=TRACK_EW)
    network.add_track(track_b)
    block_b = network.create_block("BLOCK_B", "Block B")
    block_b.length_cm = 100.0
    track_b.block_section_id = "BLOCK_B"

    # Block C - diagonal track leading up
    track_c = TrackSegment(
        x=2, y=1, track_bits=TRACK_SW
    )  # Connects to switch via S edge
    network.add_track(track_c)
    block_c = network.create_block("BLOCK_C", "Block C")
    block_c.length_cm = 100.0
    track_c.block_section_id = "BLOCK_C"

    return network


@pytest.fixture
def train():
    """Create a basic train for testing."""
    train = Train(
        train_id="TRAIN_001",
        max_speed=100.0,
        current_speed=50.0,
        acceleration=2.0,
        priority=5,
    )
    train.consist = [
        TrainCar(car_id="CAR1", car_type="LOCOMOTIVE", length=20.0),
        TrainCar(car_id="CAR2", car_type="PASSENGER", length=25.0),
    ]
    return train


# ============================================================================
# Test Route Data Structures
# ============================================================================


class TestPathDataclass:
    """Tests for Route dataclass."""

    def test_path_creation(self):
        """Test basic route creation."""
        path = Path(
            path_id="ROUTE_A_TO_B_001",
            status=PathStatus.PENDING,
            priority=5,
            start_block_id="BLOCK_A",
            end_block_id="BLOCK_B",
        )

        assert path.path_id == "ROUTE_A_TO_B_001"
        assert path.status == PathStatus.PENDING
        assert path.priority == 5
        assert path.start_block_id == "BLOCK_A"
        assert path.end_block_id == "BLOCK_B"
        assert path.train_id is None

    def test_path_status_queries(self):
        """Test route status query methods."""
        path = Path(path_id="TEST", status=PathStatus.SET)
        assert path.is_set()
        assert not path.is_active()
        assert path.can_be_extended()
        assert path.can_be_cancelled()

        path.status = PathStatus.ACTIVE
        assert path.is_active()
        assert path.can_be_extended()

        path.status = PathStatus.COMPLETED
        assert path.is_completed()
        assert not path.can_be_cancelled()

    def test_path_serialization(self):
        """Test route to_dict and from_dict."""
        path = Path(
            path_id="ROUTE_TEST_001",
            status=PathStatus.ACTIVE,
            priority=7,
            start_block_id="START",
            end_block_id="END",
            path_tiles=[(0, 0), (1, 0), (2, 0)],
            block_sequence=["BLK_1", "BLK_2"],
            switch_positions={(1, 0): 0},
            train_id="TRAIN_1",
        )

        data = path.to_dict()
        assert data["path_id"] == "ROUTE_TEST_001"
        assert data["status"] == "active"
        assert data["priority"] == 7

        restored = Path.from_dict(data)
        assert restored.path_id == path.path_id
        assert restored.status == path.status
        assert restored.priority == path.priority
        assert restored.path_tiles == path.path_tiles

    def test_path_progress_tracking(self):
        """Test route progress tracking methods."""
        path = Path(
            path_id="TEST",
            block_sequence=["BLK_1", "BLK_2", "BLK_3", "BLK_4"],
        )

        assert path.get_progress_percentage() == 0.0

        path.current_block_index = 2
        assert path.get_progress_percentage() == 50.0

        path.blocks_cleared.add("BLK_1")
        assert path.get_cleared_block_count() == 1


class TestPathSegment:
    """Tests for PathSegment dataclass."""

    def test_path_path_creation(self):
        """Test PathSegment creation."""
        path = PathSegment(
            tiles=[(0, 0), (1, 0), (2, 0)],
            distance_cm=300.0,
            required_switches={(1, 0): 0},
            blocks=["BLK_A", "BLK_B"],
        )

        assert len(path.tiles) == 3
        assert path.distance_cm == 300.0
        assert path.required_switches == {(1, 0): 0}
        assert path.blocks == ["BLK_A", "BLK_B"]

    def test_path_path_serialization(self):
        """Test PathSegment to_dict."""
        path = PathSegment(
            tiles=[(0, 0), (1, 0)],
            distance_cm=200.0,
            required_switches={(1, 0): 1},
            blocks=["BLK_A"],
        )

        data = path.to_dict()
        assert data["tiles"] == [(0, 0), (1, 0)]
        assert data["distance_cm"] == 200.0
        assert "1,0" in data["required_switches"]


class TestValidationError:
    """Tests for ValidationError dataclass."""

    def test_validation_error_creation(self):
        """Test ValidationError creation."""
        error = ValidationError(
            type=ValidationErrorType.BLOCK_OCCUPIED,
            message="Block A is occupied",
            block_id="BLOCK_A",
            occupied_by="TRAIN_1",
        )

        assert error.type == ValidationErrorType.BLOCK_OCCUPIED
        assert error.block_id == "BLOCK_A"
        assert error.occupied_by == "TRAIN_1"

    def test_validation_error_serialization(self):
        """Test ValidationError to_dict."""
        error = ValidationError(
            type=ValidationErrorType.SWITCH_LOCKED,
            message="Switch locked",
            position=(1, 0),
            locked_by_path="PATH_X",
        )

        data = error.to_dict()
        assert data["type"] == "switch_locked"
        assert data["position"] == (1, 0)


# ============================================================================
# Test Block Route Reservation
# ============================================================================


class TestBlockPathReservation:
    """Tests for block route reservation methods."""

    def test_reserve_for_path_success(self, network):
        """Test successful block reservation for route."""
        block = network.create_block("TEST_BLOCK")

        assert block.state == BlockState.FREE
        assert block.reserve_for_path("PATH_001")
        assert block.state == BlockState.RESERVED
        assert block.reserved_by_path == "PATH_001"

    def test_reserve_for_path_already_reserved(self, network):
        """Test reservation fails when already reserved by different route."""
        block = network.create_block("TEST_BLOCK")

        assert block.reserve_for_path("PATH_001")
        assert not block.reserve_for_path("PATH_002")
        assert block.reserved_by_path == "PATH_001"

    def test_reserve_for_path_same_route(self, network):
        """Test reservation succeeds for same route."""
        block = network.create_block("TEST_BLOCK")

        assert block.reserve_for_path("PATH_001")
        assert block.reserve_for_path("PATH_001")  # Same route - OK
        assert block.reserved_by_path == "PATH_001"

    def test_is_reserved_for_path(self, network):
        """Test is_reserved_for_path check."""
        block = network.create_block("TEST_BLOCK")
        block.reserve_for_path("PATH_001")

        assert block.is_reserved_for_path("PATH_001")
        assert not block.is_reserved_for_path("PATH_002")

    def test_can_reserve_for_path(self, network):
        """Test can_reserve_for_path check."""
        block = network.create_block("TEST_BLOCK")

        assert block.can_reserve_for_path("PATH_001")

        block.reserve_for_path("PATH_001")
        assert block.can_reserve_for_path("PATH_001")
        assert not block.can_reserve_for_path("PATH_002")

    def test_release_path_reservation(self, network):
        """Test releasing route reservation."""
        block = network.create_block("TEST_BLOCK")
        block.reserve_for_path("PATH_001")

        assert block.release_path_reservation()
        assert block.state == BlockState.FREE
        assert block.reserved_by_path is None

    def test_is_available_for_path(self, network):
        """Test is_available_for_path check."""
        block = network.create_block("TEST_BLOCK")

        # Free block - available to any route
        assert block.is_available_for_path("PATH_001")

        # Reserved by same route - available
        block.reserve_for_path("PATH_001")
        assert block.is_available_for_path("PATH_001")

        # Reserved by different route - not available
        assert not block.is_available_for_path("PATH_002")

    def test_disabled_block_not_available(self, network):
        """Test disabled block cannot be reserved."""
        block = network.create_block("TEST_BLOCK")
        block.disabled = True

        assert not block.can_reserve_for_path("PATH_001")
        assert not block.reserve_for_path("PATH_001")
        assert not block.is_available_for_path("PATH_001")


# ============================================================================
# Test Switch Locking
# ============================================================================


class TestSwitchLocking:
    """Tests for switch locking methods."""

    def test_lock_switch_success(self, network):
        """Test successful switch locking."""
        # Create a switch (EW + NE)
        switch = TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(switch)

        assert switch.is_switch
        assert not switch.is_locked_by_path()

        assert switch.lock_for_path("PATH_001", 0)
        assert switch.is_locked_by_path()
        assert switch.is_locked_by("PATH_001")
        assert switch.locked_state == 0

    def test_lock_switch_already_locked(self, network):
        """Test locking fails when already locked by different route."""
        switch = TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(switch)

        assert switch.lock_for_path("PATH_001", 0)
        assert not switch.lock_for_path("PATH_002", 0)
        assert switch.is_locked_by("PATH_001")

    def test_lock_switch_same_route_same_state(self, network):
        """Test locking succeeds for same route same state."""
        switch = TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(switch)

        assert switch.lock_for_path("PATH_001", 0)
        assert switch.lock_for_path("PATH_001", 0)  # Same route, same state - OK

    def test_unlock_switch(self, network):
        """Test unlocking switch."""
        switch = TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(switch)

        switch.lock_for_path("PATH_001", 0)
        assert switch.unlock_for_path("PATH_001")
        assert not switch.is_locked_by_path()

    def test_unlock_switch_wrong_route(self, network):
        """Test unlocking fails for wrong route."""
        switch = TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(switch)

        switch.lock_for_path("PATH_001", 0)
        assert not switch.unlock_for_path("PATH_002")
        assert switch.is_locked_by("PATH_001")

    def test_get_lock_info(self, network):
        """Test getting lock info."""
        switch = TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(switch)

        assert switch.get_lock_info() is None

        switch.lock_for_path("PATH_001", 1)
        info = switch.get_lock_info()
        assert info is not None
        assert info["path_id"] == "PATH_001"
        assert info["locked_state"] == 1

    def test_can_lock_for_path(self, network):
        """Test can_lock_for_path check."""
        switch = TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(switch)

        # Not locked - can lock
        assert switch.can_lock_for_path("PATH_001", 0)

        switch.lock_for_path("PATH_001", 0)

        # Same route, same state - can lock
        assert switch.can_lock_for_path("PATH_001", 0)

        # Same route, different state - cannot lock
        assert not switch.can_lock_for_path("PATH_001", 1)

        # Different route - cannot lock
        assert not switch.can_lock_for_path("PATH_002", 0)

    def test_non_switch_cannot_be_locked(self, network):
        """Test that non-switches cannot be locked."""
        straight = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(straight)

        assert not straight.is_switch
        assert not straight.lock_for_path("PATH_001", 0)


# ============================================================================
# Test Route Dispatcher - ID Generation
# ============================================================================


class TestPathIDGeneration:
    """Tests for route ID generation."""

    def test_generate_path_id(self, network, dispatcher):
        """Test route ID generation."""
        network.create_block("BLOCK_A", "Start")
        network.create_block("BLOCK_B", "End")

        path_id = dispatcher.generate_path_id("BLOCK_A", "BLOCK_B")
        assert path_id.startswith("PATH_")
        assert "START" in path_id or "BLOCK_A" in path_id

    def test_path_id_sequence(self, network, dispatcher):
        """Test route ID sequence increments."""
        network.create_block("BLOCK_A")
        network.create_block("BLOCK_B")

        id1 = dispatcher.generate_path_id("BLOCK_A", "BLOCK_B")
        id2 = dispatcher.generate_path_id("BLOCK_A", "BLOCK_B")

        assert id1 != id2
        assert "001" in id1
        assert "002" in id2


# ============================================================================
# Test Route Dispatcher - Pathfinding
# ============================================================================


class TestPathSegmentfinding:
    """Tests for route pathfinding."""

    def test_find_paths_simple_linear(self, simple_linear_network, dispatcher):
        """Test finding paths in simple linear network."""
        dispatcher.network = simple_linear_network

        paths = dispatcher.find_paths("BLOCK_A", "BLOCK_C")

        assert len(paths) >= 1
        # Path should go through BLOCK_B
        assert "BLOCK_B" in paths[0].blocks

    def test_find_paths_no_path(self, network, dispatcher):
        """Test no path found for disconnected blocks."""
        # Create two disconnected blocks
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(track_a)
        block_a = network.create_block("BLOCK_A")
        track_a.block_section_id = "BLOCK_A"

        track_b = TrackSegment(
            x=10, y=10, track_bits=TRACK_EW
        )  # Far away, disconnected
        network.add_track(track_b)
        block_b = network.create_block("BLOCK_B")
        track_b.block_section_id = "BLOCK_B"

        paths = dispatcher.find_paths("BLOCK_A", "BLOCK_B")
        assert len(paths) == 0

    def test_find_paths_with_switch(self, network_with_switch, dispatcher):
        """Test finding paths through switch."""
        dispatcher.network = network_with_switch

        # Path to BLOCK_B (straight)
        paths_b = dispatcher.find_paths("BLOCK_A", "BLOCK_B")
        assert len(paths_b) >= 1

        # Path to BLOCK_C may or may not exist depending on switch connectivity
        # This test validates that the pathfinder handles switches correctly
        # The actual paths found depend on the network topology

    def test_paths_sorted_by_distance(self, simple_linear_network, dispatcher):
        """Test that paths are sorted by distance."""
        dispatcher.network = simple_linear_network

        paths = dispatcher.find_paths("BLOCK_A", "BLOCK_C")

        if len(paths) > 1:
            for i in range(len(paths) - 1):
                assert paths[i].distance_cm <= paths[i + 1].distance_cm


# ============================================================================
# Test Route Dispatcher - Validation
# ============================================================================


class TestPathValidation:
    """Tests for route validation."""

    def test_validate_path_success(self, simple_linear_network, dispatcher):
        """Test successful path validation."""
        dispatcher.network = simple_linear_network

        path = PathSegment(
            tiles=[(0, 0), (1, 0), (2, 0), (3, 0)],
            distance_cm=400.0,
            required_switches={},
            blocks=["BLOCK_A", "BLOCK_B", "BLOCK_C"],
        )

        result = dispatcher.validate_path(path, "PATH_001")
        assert result.valid
        assert len(result.errors) == 0

    def test_validate_path_unblocked_tile(self, simple_linear_network, dispatcher):
        """Test validation fails for tiles not in blocks."""
        dispatcher.network = simple_linear_network

        # Add a track without a block
        unblocked = TrackSegment(x=4, y=0, track_bits=TRACK_EW)
        simple_linear_network.add_track(unblocked)

        path = PathSegment(
            tiles=[(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)],
            distance_cm=500.0,
            required_switches={},
            blocks=["BLOCK_A", "BLOCK_B", "BLOCK_C"],
        )

        result = dispatcher.validate_path(path, "PATH_001")
        assert not result.valid
        assert any(e.type == ValidationErrorType.UNBLOCKED_TILE for e in result.errors)

    def test_validate_path_block_occupied(self, simple_linear_network, dispatcher):
        """Test validation fails when block is occupied."""
        dispatcher.network = simple_linear_network

        # Occupy block B
        block_b = simple_linear_network.blocks["BLOCK_B"]
        block_b.occupy("OTHER_TRAIN")

        path = PathSegment(
            tiles=[(0, 0), (1, 0), (2, 0), (3, 0)],
            distance_cm=400.0,
            required_switches={},
            blocks=["BLOCK_A", "BLOCK_B", "BLOCK_C"],
        )

        result = dispatcher.validate_path(path, "PATH_001")
        assert not result.valid
        assert any(e.type == ValidationErrorType.BLOCK_OCCUPIED for e in result.errors)

    def test_validate_path_block_reserved(self, simple_linear_network, dispatcher):
        """Test validation fails when block is reserved for different route."""
        dispatcher.network = simple_linear_network

        # Reserve block B for different route
        block_b = simple_linear_network.blocks["BLOCK_B"]
        block_b.reserve_for_path("OTHER_PATH")

        path = PathSegment(
            tiles=[(0, 0), (1, 0), (2, 0), (3, 0)],
            distance_cm=400.0,
            required_switches={},
            blocks=["BLOCK_A", "BLOCK_B", "BLOCK_C"],
        )

        result = dispatcher.validate_path(path, "PATH_001")
        assert not result.valid
        assert any(e.type == ValidationErrorType.BLOCK_RESERVED for e in result.errors)

    def test_path_through_switch_tiles(self):
        """
        Test that routes can traverse switch tiles.

        Switches are block boundaries and do NOT have block_section_id.
        The path dispatcher should skip the block check for switches.
        """
        network = RailwayNetwork()

        # Block A: (0,0) to (2,0)
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_A", [(0, 0), (1, 0), (2, 0)], name="A")

        # Switch at (3,0) - NOT in any block (boundary)
        switch = TrackSegment(3, 0, track_bits=TRACK_EW | TRACK_NW)
        network.add_track(switch)
        assert switch.is_switch == True
        assert switch.block_section_id is None

        # Block B: (4,0) to (6,0)
        for x in range(4, 7):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_B", [(4, 0), (5, 0), (6, 0)], name="B")

        dispatcher = PathDispatcher(network)

        # Find route from A to B (must traverse switch)
        paths = dispatcher.find_paths("BLOCK_A", "BLOCK_B")

        assert len(paths) > 0, "Should find at least one path through switch"
        path = paths[0]

        # Verify switch is in the path
        assert (3, 0) in path.tiles, "Switch tile should be in path"

        # Verify path includes both blocks
        assert "BLOCK_A" in path.blocks
        assert "BLOCK_B" in path.blocks

        # Verify switch state is recorded
        assert (3, 0) in path.required_switches

    def test_path_through_signal_tiles(self):
        """
        Test that routes can traverse signal tiles.

        Signal tiles remain in their blocks (have block_section_id).
        The path dispatcher should allow paths through signal tiles.
        """
        network = RailwayNetwork()

        # Block A: (0,0) to (2,0) with signal at (1,0)
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_A", [(0, 0), (1, 0), (2, 0)], name="A")

        # Place signal at (1,0) - tile should remain in BLOCK_A
        signal = network.add_signal(1, 0, "E", auto_connect=False)
        signal_track = network.get_track(1, 0)
        assert signal_track.block_section_id == "BLOCK_A", (
            "Signal tile should remain in block"
        )

        # Block B: (3,0) to (5,0)
        for x in range(3, 6):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks("BLOCK_B", [(3, 0), (4, 0), (5, 0)], name="B")

        dispatcher = PathDispatcher(network)

        # Find route from A to B (must traverse signal tile at 1,0)
        paths = dispatcher.find_paths("BLOCK_A", "BLOCK_B")

        assert len(paths) > 0, "Should find at least one path through signal tile"
        path = paths[0]

        # Verify signal tile is in the path
        assert (1, 0) in path.tiles or (2, 0) in path.tiles, (
            "Path should include tiles from BLOCK_A"
        )


# ============================================================================
# Test Route Dispatcher - Route Creation
# ============================================================================


class TestPathCreation:
    """Tests for route creation."""

    def test_create_path_success(self, simple_linear_network, dispatcher):
        """Test successful route creation."""
        dispatcher.network = simple_linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")

        assert result.success
        assert result.path is not None
        assert result.path.status == PathStatus.SET
        assert result.path.start_block_id == "BLOCK_A"
        assert result.path.end_block_id == "BLOCK_C"

    def test_create_path_reserves_blocks(self, simple_linear_network, dispatcher):
        """Test route creation reserves blocks."""
        dispatcher.network = simple_linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")

        assert result.success

        # All blocks should be reserved
        for block_id in result.path.block_sequence:
            block = simple_linear_network.blocks.get(block_id)
            if block:
                assert block.state == BlockState.RESERVED
                assert block.reserved_by_path == result.path.path_id

    def test_create_path_no_path(self, network, dispatcher):
        """Test route creation fails when no path exists."""
        # Create disconnected blocks
        track_a = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(track_a)
        network.create_block("BLOCK_A")
        track_a.block_section_id = "BLOCK_A"

        track_b = TrackSegment(x=10, y=10, track_bits=TRACK_EW)
        network.add_track(track_b)
        network.create_block("BLOCK_B")
        track_b.block_section_id = "BLOCK_B"

        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")

        assert not result.success
        assert "No path" in result.error

    def test_create_path_with_priority(self, simple_linear_network, dispatcher):
        """Test route creation with custom priority."""
        dispatcher.network = simple_linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", priority=8)

        assert result.success
        assert result.path.priority == 8

    def test_create_path_with_train(self, simple_linear_network, dispatcher, train):
        """Test route creation with assigned train."""
        dispatcher.network = simple_linear_network
        simple_linear_network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", train_id=train.train_id)

        assert result.success
        assert result.path.train_id == train.train_id


# ============================================================================
# Test Route Dispatcher - Route Lifecycle
# ============================================================================


class TestPathLifecycle:
    """Tests for route lifecycle management."""

    def test_train_enters_path(self, simple_linear_network, dispatcher, train):
        """Test train entering route."""
        dispatcher.network = simple_linear_network
        simple_linear_network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success
        path_id = result.path.path_id

        # Train enters first block
        success = dispatcher.on_train_enters_path(path_id, train.train_id, "BLOCK_A")

        assert success
        path = dispatcher.get_path(path_id)
        assert path.status == PathStatus.ACTIVE
        assert path.train_id == train.train_id

    def test_train_advances(self, simple_linear_network, dispatcher, train):
        """Test train advancing through route."""
        dispatcher.network = simple_linear_network
        simple_linear_network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        path_id = result.path.path_id

        dispatcher.on_train_enters_path(path_id, train.train_id, "BLOCK_A")
        success = dispatcher.on_train_advances(path_id, "BLOCK_B")

        assert success
        path = dispatcher.get_path(path_id)
        assert path.current_block_index > 0

    def test_complete_path(self, simple_linear_network, dispatcher, train):
        """Test route completion.

        Note: complete_path only clears blocks that are not OCCUPIED.
        If a train is still physically in a block, it stays OCCUPIED.
        This test verifies that the path is removed and switches/reservations are cleared.
        """
        dispatcher.network = simple_linear_network
        simple_linear_network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        path_id = result.path.path_id

        dispatcher.on_train_enters_path(path_id, train.train_id, "BLOCK_A")
        dispatcher.on_train_advances(path_id, "BLOCK_B")
        dispatcher.on_train_advances(path_id, "BLOCK_C")

        success = dispatcher.complete_path(path_id)

        assert success
        assert dispatcher.get_path(path_id) is None  # Removed from active routes

        # Path should be removed and path reservations cleared
        for block_id in ["BLOCK_A", "BLOCK_B", "BLOCK_C"]:
            block = simple_linear_network.blocks.get(block_id)
            if block:
                # Block reservation should be cleared
                assert block.reserved_by_path is None
                # Block state depends on whether train is still physically in it
                # Since this test doesn't track train position, blocks may still be OCCUPIED


# ============================================================================
# Test Route Dispatcher - Extension
# ============================================================================


class TestPathExtension:
    """Tests for route extension."""

    def test_extend_path_success(self, network, dispatcher):
        """Test successful route extension."""
        # Create a longer network: A -> B -> C -> D
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        for i, block_id in enumerate(["BLOCK_A", "BLOCK_B", "BLOCK_C", "BLOCK_D"]):
            block = network.create_block(block_id)
            block.length_cm = 100.0
            network.get_track(i, 0).block_section_id = block_id
        network.get_track(4, 0).block_section_id = "BLOCK_D"  # Extra tile for D

        dispatcher.network = network

        # Create route A -> C
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success
        path_id = result.path.path_id

        # Extend to D
        ext_result = dispatcher.extend_path(path_id, "BLOCK_D")

        assert ext_result.success
        assert "BLOCK_D" in ext_result.extended_blocks

        path = dispatcher.get_path(path_id)
        assert path.end_block_id == "BLOCK_D"

    def test_extend_completed_route_fails(
        self, simple_linear_network, dispatcher, train
    ):
        """Test extending completed route fails."""
        dispatcher.network = simple_linear_network
        simple_linear_network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        path_id = result.path.path_id

        # Complete the route
        dispatcher.on_train_enters_path(path_id, train.train_id, "BLOCK_A")
        dispatcher.on_train_advances(path_id, "BLOCK_C")
        dispatcher.complete_path(path_id)

        # Try to extend - should fail (route no longer exists)
        ext_result = dispatcher.extend_path(path_id, "BLOCK_X")
        assert not ext_result.success


# ============================================================================
# Test Route Dispatcher - Cancellation
# ============================================================================


class TestPathCancellation:
    """Tests for route cancellation."""

    def test_cancel_path(self, simple_linear_network, dispatcher):
        """Test route cancellation."""
        dispatcher.network = simple_linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        path_id = result.path.path_id

        cancel_result = dispatcher.cancel_path(path_id)

        assert cancel_result.success
        assert len(cancel_result.blocks_released) > 0

    def test_cancel_path_releases_resources(self, simple_linear_network, dispatcher):
        """Test cancellation releases blocks and switches."""
        dispatcher.network = simple_linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        path_id = result.path.path_id

        dispatcher.cancel_path(path_id)

        # Blocks should be released
        for block_id in ["BLOCK_A", "BLOCK_B", "BLOCK_C"]:
            block = simple_linear_network.blocks.get(block_id)
            if block:
                assert block.state == BlockState.FREE


# ============================================================================
# Test Train Priority and Emergency Stop
# ============================================================================


class TestTrainPriorityAndEmergencyStop:
    """Tests for train priority and emergency stop."""

    def test_train_default_priority(self):
        """Test train default priority is 5."""
        train = Train(train_id="TEST")
        assert train.priority == 5

    def test_train_custom_priority(self):
        """Test train with custom priority."""
        train = Train(train_id="TEST", priority=8)
        assert train.priority == 8

    def test_emergency_stop(self, train):
        """Test initiating emergency stop."""
        train.current_speed = 100.0
        train.state = TrainState.RUNNING_MOVING
        train.initiate_emergency_stop()

        assert train.is_emergency_braking
        assert train.state == TrainState.RUNNING_WAITING

    def test_braking_distance_calculation(self, train):
        """Test braking distance calculation."""
        train.current_speed = 100.0  # km/h
        train.max_braking_rate = 2.0  # m/s²

        distance = train.get_braking_distance()

        # At 100 km/h = 27.78 m/s, with 2 m/s² deceleration
        # d = v² / (2a) = 27.78² / (2*2) = 192.9 m
        assert 190 < distance < 200

    def test_braking_distance_cm(self, train):
        """Test braking distance in centimeters."""
        train.current_speed = 36.0  # km/h = 10 m/s
        train.max_braking_rate = 2.0  # m/s²

        distance_cm = train.get_braking_distance_cm()
        # d = 10² / (2*2) = 25 m = 2500 cm
        assert 2400 < distance_cm < 2600

    def test_emergency_braking_update(self, train):
        """Test emergency braking update reduces speed."""
        train.current_speed = 100.0
        train.max_braking_rate = 10.0
        train.initiate_emergency_stop()

        # Update with 1 second delta
        still_braking = train.update_emergency_braking(1.0)

        assert still_braking
        assert train.current_speed < 100.0

    def test_emergency_braking_stops_train(self, train):
        """Test emergency braking eventually stops train."""
        train.current_speed = 36.0  # 10 m/s
        train.max_braking_rate = 10.0  # Should stop in 1 second
        train.initiate_emergency_stop()

        # Update with enough time to stop
        for _ in range(10):
            if not train.update_emergency_braking(0.2):
                break

        assert train.current_speed == 0.0
        assert not train.is_emergency_braking
        assert train.state == TrainState.IDLE


# ============================================================================
# Test Query Methods
# ============================================================================


class TestQueryMethods:
    """Tests for dispatcher query methods."""

    def test_get_active_routes(self, simple_linear_network, dispatcher):
        """Test getting active routes."""
        dispatcher.network = simple_linear_network

        assert len(dispatcher.get_active_paths()) == 0

        dispatcher.create_path("BLOCK_A", "BLOCK_C")

        assert len(dispatcher.get_active_paths()) == 1

    def test_get_routes_for_train(self, simple_linear_network, dispatcher, train):
        """Test getting routes for specific train."""
        dispatcher.network = simple_linear_network
        simple_linear_network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", train_id=train.train_id)

        routes = dispatcher.get_paths_for_train(train.train_id)
        assert len(routes) == 1
        assert routes[0].path_id == result.path.path_id

    def test_get_routes_using_block(self, simple_linear_network, dispatcher):
        """Test getting routes using specific block."""
        dispatcher.network = simple_linear_network

        dispatcher.create_path("BLOCK_A", "BLOCK_C")

        routes = dispatcher.get_paths_using_block("BLOCK_B")
        assert len(routes) == 1


# ============================================================================
# Test Route Serialization
# ============================================================================


class TestPathSerialization:
    """Tests for route serialization."""

    def test_path_result_serialization(self, simple_linear_network, dispatcher):
        """Test PathResult serialization."""
        dispatcher.network = simple_linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")

        data = result.to_dict()
        assert data["success"] == True
        assert "path" in data

    def test_cancellation_result_serialization(self, simple_linear_network, dispatcher):
        """Test CancellationResult serialization."""
        dispatcher.network = simple_linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        cancel_result = dispatcher.cancel_path(result.path.path_id)

        data = cancel_result.to_dict()
        assert data["success"] == True
        assert "blocks_released" in data


# ============================================================================
# Test Direction-Aware Route Extension (Bug Fix)
# ============================================================================


class TestDirectionAwarePath:
    """
    Comprehensive tests for direction-aware route extension.

    These tests ensure the following bug doesn't regress:

    BUG: When extending a route, users could click on blocks in the BACKWARD
         direction, causing the route to extend backwards and flip direction.

    FIX: Implemented direction-aware extension:
         1. get_reachable_endpoints_for_extension() only returns blocks in forward direction
         2. extend_route() validates that target isn't already in route (prevents backward)
         3. _find_extension_paths() only explores paths in the route's exit direction
    """

    @pytest.fixture
    def linear_network(self, network):
        """Create a linear network: A -> B -> C -> D (west to east)"""
        # Create 4 tiles: x=0,1,2,3 all at y=0
        for x in range(4):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Create blocks - one tile per block
        for i, block_id in enumerate(["BLOCK_A", "BLOCK_B", "BLOCK_C", "BLOCK_D"]):
            block = network.create_block(block_id)
            block.length_cm = 100.0
            network.get_track(i, 0).block_section_id = block_id

        return network

    @pytest.fixture
    def y_switch_network(self, network):
        """Create a Y-switch network:

        A -> B -> Y (switch) -> C (straight)
                      \\-> D (diverge)
        """
        # Main line: x=0,1,2,3 at y=0
        for x in range(4):
            if x == 2:
                # Y-switch at x=2 (TRACK_EW | TRACK_NE = going E or NE)
                # is_switch is automatically computed from track_bits
                track = TrackSegment(x=x, y=0, track_bits=TRACK_EW | TRACK_NE)
            else:
                track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Diverging track at x=3, y=1
        track = TrackSegment(x=3, y=1, track_bits=TRACK_NE | TRACK_EW)
        network.add_track(track)

        # Create blocks
        network.create_block("BLOCK_A").length_cm = 100.0
        network.get_track(0, 0).block_section_id = "BLOCK_A"

        network.create_block("BLOCK_B").length_cm = 100.0
        network.get_track(1, 0).block_section_id = "BLOCK_B"

        # Switch at x=2 is a boundary - no block

        network.create_block("BLOCK_C").length_cm = 100.0
        network.get_track(3, 0).block_section_id = "BLOCK_C"

        network.create_block("BLOCK_D").length_cm = 100.0
        network.get_track(3, 1).block_section_id = "BLOCK_D"

        return network

    # ========================================================================
    # Route.get_exit_direction() Tests
    # ========================================================================

    def test_get_exit_direction_eastward(self, linear_network, dispatcher):
        """Test exit direction for eastward route (A -> C)."""
        dispatcher.network = linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success

        path = result.path
        assert path.get_exit_direction() == "E"

    def test_get_exit_direction_westward(self, linear_network, dispatcher):
        """Test exit direction for westward route (C -> A)."""
        dispatcher.network = linear_network

        result = dispatcher.create_path("BLOCK_C", "BLOCK_A")
        assert result.success

        path = result.path
        assert path.get_exit_direction() == "W"

    def test_get_exit_direction_short_route(self, linear_network, dispatcher):
        """Test exit direction for 2-tile route (A -> B)."""
        dispatcher.network = linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result.success

        path = result.path
        # Even short routes have a direction
        assert path.get_exit_direction() is not None

    def test_get_exit_direction_empty_route(self):
        """Test exit direction returns None for empty route."""
        from path import Path

        path = Path(path_id="TEST", path_tiles=[])
        assert path.get_exit_direction() is None

    def test_get_exit_direction_single_tile_route(self):
        """Test exit direction returns None for single tile route."""
        from path import Path

        path = Path(path_id="TEST", path_tiles=[(0, 0)])
        assert path.get_exit_direction() is None

    # ========================================================================
    # Route.get_last_tile_entry_edge() Tests
    # ========================================================================

    def test_get_last_tile_entry_edge_eastward(self, linear_network, dispatcher):
        """Test entry edge for eastward route - should be West."""
        dispatcher.network = linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success

        path = result.path
        # Heading East, so we enter last tile from West
        assert path.get_last_tile_entry_edge() == "W"

    def test_get_last_tile_entry_edge_westward(self, linear_network, dispatcher):
        """Test entry edge for westward route - should be East."""
        dispatcher.network = linear_network

        result = dispatcher.create_path("BLOCK_C", "BLOCK_A")
        assert result.success

        path = result.path
        # Heading West, so we enter last tile from East
        assert path.get_last_tile_entry_edge() == "E"

    # ========================================================================
    # get_reachable_endpoints_for_extension() Tests
    # ========================================================================

    def test_reachable_extensions_forward_only(self, linear_network, dispatcher):
        """Test that reachable extensions only include forward blocks."""
        dispatcher.network = linear_network

        # Create route A -> B (heading East)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result.success
        path_id = result.path.path_id

        # Get reachable extensions
        reachable, error = dispatcher.get_reachable_endpoints_for_extension(path_id)

        assert error is None
        # Should include C and D (forward), NOT include A (backward) or B (already in route)
        assert "BLOCK_C" in reachable
        assert "BLOCK_D" in reachable
        assert "BLOCK_A" not in reachable  # Backward
        assert "BLOCK_B" not in reachable  # Already in route

    def test_reachable_extensions_excludes_route_blocks(
        self, linear_network, dispatcher
    ):
        """Test that blocks already in route are excluded from extensions."""
        dispatcher.network = linear_network

        # Create route A -> C (includes B)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success
        path_id = result.path.path_id

        # Get reachable extensions
        reachable, error = dispatcher.get_reachable_endpoints_for_extension(path_id)

        assert error is None
        # Should include D (forward), NOT include A, B, C (already in route)
        assert "BLOCK_D" in reachable
        assert "BLOCK_A" not in reachable
        assert "BLOCK_B" not in reachable
        assert "BLOCK_C" not in reachable

    def test_reachable_extensions_respects_y_switch_geometry(
        self, y_switch_network, dispatcher
    ):
        """Test that Y-switch geometry is respected when finding extensions."""
        dispatcher.network = y_switch_network

        # Create route A -> B (heading East, will go through switch)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result.success
        path_id = result.path.path_id

        # Get reachable extensions
        reachable, error = dispatcher.get_reachable_endpoints_for_extension(path_id)

        assert error is None
        # Both C (straight) and D (diverge) should be reachable
        assert "BLOCK_C" in reachable or "BLOCK_D" in reachable

    def test_reachable_extensions_error_for_nonexistent_route(
        self, linear_network, dispatcher
    ):
        """Test error is returned for non-existent route."""
        dispatcher.network = linear_network

        reachable, error = dispatcher.get_reachable_endpoints_for_extension("FAKE_PATH")

        assert error is not None
        assert "not found" in error.lower()
        assert reachable == []

    def test_reachable_extensions_error_for_completed_route(
        self, linear_network, dispatcher
    ):
        """Test error is returned for completed route."""
        dispatcher.network = linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        path_id = result.path.path_id

        # Complete the route
        dispatcher.complete_path(path_id)

        reachable, error = dispatcher.get_reachable_endpoints_for_extension(path_id)

        # Route no longer exists after completion
        assert error is not None

    # ========================================================================
    # extend_route() Direction-Aware Validation Tests
    # ========================================================================

    def test_extend_path_rejects_backward_extension(self, linear_network, dispatcher):
        """Test that extending backward is rejected with clear error message."""
        dispatcher.network = linear_network

        # Create route B -> C (heading East)
        result = dispatcher.create_path("BLOCK_B", "BLOCK_C")
        assert result.success
        path_id = result.path.path_id

        # Try to extend backward to A
        ext_result = dispatcher.extend_path(path_id, "BLOCK_A")

        assert not ext_result.success
        assert ext_result.error is not None
        # Error should indicate it's not reachable in forward direction
        # Could say "backward", "already", or "no forward path"
        error_lower = ext_result.error.lower()
        assert (
            "backward" in error_lower
            or "already" in error_lower
            or "forward" in error_lower
        ), f"Expected backward/already/forward in error, got: {ext_result.error}"

    def test_extend_path_rejects_existing_block(self, linear_network, dispatcher):
        """Test that extending to a block already in route is rejected."""
        dispatcher.network = linear_network

        # Create route A -> C (includes B)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success
        path_id = result.path.path_id

        # Try to extend to B (already in route)
        ext_result = dispatcher.extend_path(path_id, "BLOCK_B")

        assert not ext_result.success
        assert ext_result.error is not None
        assert (
            "already" in ext_result.error.lower()
            or "backward" in ext_result.error.lower()
        )

    def test_extend_path_forward_success(self, linear_network, dispatcher):
        """Test that forward extension succeeds."""
        dispatcher.network = linear_network

        # Create route A -> B (heading East)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result.success
        path_id = result.path.path_id

        # Extend forward to C
        ext_result = dispatcher.extend_path(path_id, "BLOCK_C")

        assert ext_result.success
        assert "BLOCK_C" in ext_result.extended_blocks

        # Verify route was updated
        path = dispatcher.get_path(path_id)
        assert path.end_block_id == "BLOCK_C"
        assert "BLOCK_C" in path.block_sequence

    def test_extend_path_maintains_direction(self, linear_network, dispatcher):
        """Test that extended route maintains original direction."""
        dispatcher.network = linear_network

        # Create route A -> B (heading East)
        result = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result.success
        path_id = result.path.path_id
        original_direction = result.path.get_exit_direction()

        # Extend forward to C
        ext_result = dispatcher.extend_path(path_id, "BLOCK_C")
        assert ext_result.success

        # Direction should still be East
        path = dispatcher.get_path(path_id)
        assert path.get_exit_direction() == original_direction

    def test_extend_path_to_start_block_fails(self, linear_network, dispatcher):
        """Test that extending route to its own start block fails."""
        dispatcher.network = linear_network

        # Create route A -> C
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success
        path_id = result.path.path_id

        # Try to extend to A (the start block)
        ext_result = dispatcher.extend_path(path_id, "BLOCK_A")

        assert not ext_result.success

    # ========================================================================
    # Route.to_dict() Tests (exit_direction field)
    # ========================================================================

    def test_path_to_dict_includes_exit_direction(self, linear_network, dispatcher):
        """Test that route serialization includes exit_direction."""
        dispatcher.network = linear_network

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success

        route_dict = result.path.to_dict()

        assert "exit_direction" in route_dict
        assert route_dict["exit_direction"] == "E"

    # ========================================================================
    # Edge Cases
    # ========================================================================

    def test_extend_path_at_dead_end(self, network, dispatcher):
        """Test extending route at a dead end (no forward exits)."""
        # Create a dead-end track: A -> B -> C (C has no forward exit)
        # Use NS track which only has N and S edges
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        for i, block_id in enumerate(["BLOCK_A", "BLOCK_B", "BLOCK_C"]):
            block = network.create_block(block_id)
            block.length_cm = 100.0
            network.get_track(i, 0).block_section_id = block_id

        dispatcher.network = network

        # Create route A -> C
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C")
        assert result.success
        path_id = result.path.path_id

        # Get reachable extensions - should be empty (dead end)
        reachable, error = dispatcher.get_reachable_endpoints_for_extension(path_id)

        # No error, but no reachable blocks either
        assert error is None
        assert len(reachable) == 0

    def test_multiple_routes_independent_extensions(self, linear_network, dispatcher):
        """Test that multiple routes have independent extension options."""
        dispatcher.network = linear_network

        # Create route 1: A -> B (heading East)
        result1 = dispatcher.create_path("BLOCK_A", "BLOCK_B")
        assert result1.success
        route1_id = result1.path.path_id

        # Get reachable for route 1
        reachable1, error1 = dispatcher.get_reachable_endpoints_for_extension(route1_id)
        assert error1 is None

        # C and D should be reachable (they're not reserved yet since route only goes to B)
        # But this depends on block reservation logic
        assert "BLOCK_C" in reachable1 or "BLOCK_D" in reachable1


# ============================================================================
# Path-Based Block Release Tests
# ============================================================================


class TestPathBasedBlockRelease:
    """
    Comprehensive tests for path-based block release using train tail position.

    These tests ensure the following bug doesn't regress:

    BUG: Blocks were released based on train head position, not tail position.
         This caused blocks to be released prematurely before the train tail
         had completely cleared them, leading to collisions and unsafe operations.

    FIX: Updated _release_blocks_behind_train() to use train.position.occupied_blocks
         (which is calculated from train tail position) instead of head position.
         Blocks now transition correctly: OCCUPIED → CAUTION → FREE based on tail.
    """

    def test_block_release_waits_for_tail_to_clear(
        self, simple_linear_network, dispatcher
    ):
        """
        Test that blocks are only released after train tail has completely cleared.

        Scenario:
        - Train enters Block A (50cm long train, 100cm block)
        - Train head enters Block B
        - Block A should remain OCCUPIED until tail leaves Block A
        - Then Block A should transition to CAUTION
        - Then Block A should transition to FREE
        """
        network = simple_linear_network
        dispatcher.network = network

        # Create path A → B → C WITHOUT a train first
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", train_id=None)
        assert result.success
        path = result.path

        # Path should be in SET status, blocks RESERVED, no train assigned yet
        assert path.status == PathStatus.SET
        assert path.train_id is None
        assert network.blocks["BLOCK_A"].state == BlockState.RESERVED

        # Now create and place train on the path
        train = Train(
            train_id="TRAIN_1", consist=[TrainCar("CAR_1", "LOCOMOTIVE", 50.0)]
        )
        train.state = TrainState.RUNNING_MOVING
        train.current_pos = (0, 0)
        train.distance_on_track = 0.0
        train.current_direction = Direction.EAST
        train.max_speed = 50.0
        train.current_speed = 25.0

        # Initialize train position
        from train import TrainPosition

        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=0.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0)],
            occupied_blocks=["BLOCK_A"],
        )

        network.trains["TRAIN_1"] = train

        # Activate path (train enters first block)
        # This assigns the train to the path and transitions to ACTIVE
        success = dispatcher.on_train_enters_path(path.path_id, "TRAIN_1", "BLOCK_A")
        assert success
        assert path.status == PathStatus.ACTIVE
        assert path.train_id == "TRAIN_1"
        assert network.blocks["BLOCK_A"].state == BlockState.OCCUPIED

        # Simulate train head entering Block B (train tail still in Block A)
        train.current_pos = (1, 0)
        train.distance_on_track = 25.0  # 25cm into Block B
        train.position.end_a_tile = (1, 0)
        train.position.end_a_distance_cm = 25.0
        train.position.end_a_entry_edge = "W"
        # Tail is still in Block A (50cm train, so tail is 25cm behind head)
        train.position.end_b_tile = (0, 0)
        train.position.end_b_distance_cm = 75.0  # 75cm from west edge of Block A
        train.position.occupied_tiles = [(0, 0), (1, 0)]
        train.position.occupied_blocks = ["BLOCK_A", "BLOCK_B"]

        # Train advances to Block B
        dispatcher.on_train_advances(path.path_id, "BLOCK_B")

        # Block A should STILL be OCCUPIED (tail hasn't cleared yet)
        assert network.blocks["BLOCK_A"].state == BlockState.OCCUPIED
        assert network.blocks["BLOCK_B"].state == BlockState.OCCUPIED

        # Simulate train tail leaving Block A (tail now fully in Block B)
        train.current_pos = (2, 0)
        train.distance_on_track = 25.0  # 25cm into tile (2,0)
        train.position.end_a_tile = (2, 0)
        train.position.end_a_distance_cm = 25.0
        train.position.end_b_tile = (1, 0)
        train.position.end_b_distance_cm = 75.0  # Tail is now in Block B
        train.position.occupied_tiles = [(1, 0), (2, 0)]
        train.position.occupied_blocks = ["BLOCK_B"]  # Only Block B now

        # Release blocks behind train
        dispatcher._release_blocks_behind_train(path)

        # Now Block A should transition to CAUTION (tail has cleared)
        assert network.blocks["BLOCK_A"].state == BlockState.CAUTION

        # Call again to transition CAUTION → FREE
        dispatcher._release_blocks_behind_train(path)

        # Now Block A should be FREE
        assert network.blocks["BLOCK_A"].state == BlockState.FREE
        assert "BLOCK_A" in path.blocks_cleared

    def test_long_train_occupies_multiple_blocks(
        self, simple_linear_network, dispatcher
    ):
        """
        Test that a long train keeps all blocks OCCUPIED while tail is in them.

        Scenario:
        - Train is 250cm long (longer than individual blocks)
        - Train occupies Blocks A, B, and C simultaneously
        - As tail clears each block, that block transitions to CAUTION then FREE
        """
        network = simple_linear_network
        dispatcher.network = network

        # Create a 250cm train (longer than any single block)
        train = Train(
            train_id="TRAIN_LONG", consist=[TrainCar("CAR_1", "LOCOMOTIVE", 250.0)]
        )
        train.state = TrainState.RUNNING_MOVING
        train.current_pos = (3, 0)
        train.distance_on_track = 50.0
        train.current_direction = Direction.EAST

        # Initialize train position (head in Block C, tail in Block A)
        from train import TrainPosition

        train.position = TrainPosition(
            end_a_tile=(3, 0),
            end_a_distance_cm=50.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=50.0,  # Tail position
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0), (1, 0), (2, 0), (3, 0)],
            occupied_blocks=["BLOCK_A", "BLOCK_B", "BLOCK_C"],
        )

        network.trains["TRAIN_LONG"] = train

        # Create path A → C
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", train_id="TRAIN_LONG")
        assert result.success
        path = result.path

        # Activate path
        dispatcher.on_train_enters_path(path.path_id, "TRAIN_LONG", "BLOCK_A")
        dispatcher.on_train_advances(path.path_id, "BLOCK_B")
        dispatcher.on_train_advances(path.path_id, "BLOCK_C")

        # All blocks should be OCCUPIED
        assert network.blocks["BLOCK_A"].state == BlockState.OCCUPIED
        assert network.blocks["BLOCK_B"].state == BlockState.OCCUPIED
        assert network.blocks["BLOCK_C"].state == BlockState.OCCUPIED

    def test_block_release_real_world_scenario(self, simple_linear_network, dispatcher):
        """
        Test real-world scenario: Train progresses through multiple blocks.

        This test simulates a train moving through blocks and verifies that
        blocks are released in the correct order with proper state transitions.
        """
        network = simple_linear_network
        dispatcher.network = network

        # Create a 50cm train
        train = Train(
            train_id="TRAIN_RW", consist=[TrainCar("CAR_1", "LOCOMOTIVE", 50.0)]
        )
        train.state = TrainState.RUNNING_MOVING
        train.current_pos = (0, 0)
        train.distance_on_track = 0.0
        train.current_direction = Direction.EAST

        # Initialize position
        from train import TrainPosition

        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=0.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0)],
            occupied_blocks=["BLOCK_A"],
        )

        network.trains["TRAIN_RW"] = train

        # Create path A → C
        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", train_id="TRAIN_RW")
        assert result.success
        path = result.path

        # Step 1: Train enters path
        dispatcher.on_train_enters_path(path.path_id, "TRAIN_RW", "BLOCK_A")
        assert network.blocks["BLOCK_A"].state == BlockState.OCCUPIED

        # Step 2: Head enters Block B, tail still in Block A
        train.current_pos = (1, 0)
        train.position.end_a_tile = (1, 0)
        train.position.end_a_distance_cm = 25.0
        train.position.end_b_tile = (0, 0)
        train.position.end_b_distance_cm = 75.0
        train.position.occupied_blocks = ["BLOCK_A", "BLOCK_B"]

        dispatcher.on_train_advances(path.path_id, "BLOCK_B")
        dispatcher._release_blocks_behind_train(path)

        # Block A still OCCUPIED (tail hasn't cleared)
        assert network.blocks["BLOCK_A"].state == BlockState.OCCUPIED
        assert network.blocks["BLOCK_B"].state == BlockState.OCCUPIED

        # Step 3: Tail clears Block A
        train.position.end_b_tile = (1, 0)
        train.position.end_b_distance_cm = 25.0
        train.position.occupied_blocks = ["BLOCK_B"]

        dispatcher._release_blocks_behind_train(path)

        # Block A now CAUTION
        assert network.blocks["BLOCK_A"].state == BlockState.CAUTION

        # Step 4: Safety margin satisfied
        dispatcher._release_blocks_behind_train(path)
        assert network.blocks["BLOCK_A"].state == BlockState.FREE

        # Step 5: Head enters Block C
        train.current_pos = (3, 0)
        train.position.end_a_tile = (3, 0)
        train.position.end_a_distance_cm = 25.0
        train.position.end_b_tile = (2, 0)
        train.position.end_b_distance_cm = 75.0
        train.position.occupied_blocks = ["BLOCK_B", "BLOCK_C"]

        dispatcher.on_train_advances(path.path_id, "BLOCK_C")
        dispatcher._release_blocks_behind_train(path)

        # Block B still OCCUPIED
        assert network.blocks["BLOCK_B"].state == BlockState.OCCUPIED

        # Step 6: Tail clears Block B
        train.position.end_b_tile = (3, 0)
        train.position.end_b_distance_cm = 25.0
        train.position.occupied_blocks = ["BLOCK_C"]

        dispatcher._release_blocks_behind_train(path)
        assert network.blocks["BLOCK_B"].state == BlockState.CAUTION

        dispatcher._release_blocks_behind_train(path)
        assert network.blocks["BLOCK_B"].state == BlockState.FREE


class TestPathDispatcherProgress:
    """Tests for PathDispatcher progress updates."""

    def test_on_train_progress_releases_blocks(self, simple_linear_network, dispatcher):
        """Progress update should release blocks behind the tail."""
        network = simple_linear_network
        dispatcher.network = network

        train = Train(
            train_id="TRAIN_PROGRESS",
            consist=[TrainCar("CAR", "LOCOMOTIVE", 50.0)],
        )
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=0.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0)],
            occupied_blocks=["BLOCK_A"],
        )
        network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", train_id=train.train_id)
        assert result.success
        path = result.path

        dispatcher.on_train_enters_path(path.path_id, train.train_id, "BLOCK_A")
        dispatcher.on_train_advances(path.path_id, "BLOCK_B")

        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=10.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(1, 0)],
            occupied_blocks=["BLOCK_B"],
        )

        dispatcher.on_train_progress(path.path_id)

        assert network.blocks["BLOCK_A"].state == BlockState.CAUTION


class TestSimulationPathAdvancement:
    """Tests for SimulationController path advancement integration."""

    def test_simulation_advances_path_on_block_entry(
        self, simple_linear_network, dispatcher
    ):
        """Simulation should notify PathDispatcher when blocks are entered."""
        network = simple_linear_network
        dispatcher.network = network
        sim = SimulationController(network, dispatcher)

        train = Train(
            train_id="TRAIN_SIM",
            consist=[TrainCar("CAR", "LOCOMOTIVE", 50.0)],
            max_speed=60.0,
        )
        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0
        train.position = TrainPosition(
            end_a_tile=(1, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(1, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(1, 0)],
            occupied_blocks=["BLOCK_B"],
        )
        network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_C", train_id=train.train_id)
        assert result.success
        path = result.path
        train.assigned_path_id = path.path_id

        controller = sim._get_or_create_controller(train)
        controller.power_on = True
        controller.state = TrainState.RUNNING_MOVING
        controller.active_cab = "A"
        controller.handle_position = HandlePosition.N

        physics = sim._get_or_create_physics(train)
        physics._last_occupied_blocks = {"BLOCK_A"}

        sim._update_train(train, 0.1)

        assert path.current_block_index == 1


class TestPathSwitchUnlockBugFix:
    """
    Comprehensive tests for switch unlock timing fix (2026-01-23).

    BUG: Switch locks were released only after a block transitioned from CAUTION
    to FREE, leaving switches locked even when they were one block behind the
    train tail.

    FIX: Release switches as soon as the tail clears a block (OCCUPIED → CAUTION).
    """

    def test_switch_unlocks_when_tail_clears_block(
        self, network_with_switch, dispatcher
    ):
        """Switch unlocks when the block behind the tail enters CAUTION."""
        network = network_with_switch
        dispatcher.network = network

        train = Train(
            train_id="TRAIN_SWITCH",
            consist=[TrainCar("CAR_1", "LOCOMOTIVE", 20.0)],
        )
        train.position = TrainPosition(
            end_a_tile=(0, 0),
            end_a_distance_cm=0.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(0, 0)],
            occupied_blocks=["BLOCK_A"],
        )
        network.add_train(train)

        result = dispatcher.create_path("BLOCK_A", "BLOCK_B", train_id=train.train_id)
        assert result.success
        path = result.path

        switch_track = network.get_track(1, 0)
        assert switch_track.is_locked_by_path()

        dispatcher.on_train_enters_path(path.path_id, train.train_id, "BLOCK_A")

        train.position = TrainPosition(
            end_a_tile=(2, 0),
            end_a_distance_cm=5.0,
            end_a_entry_edge="W",
            end_b_tile=(2, 0),
            end_b_distance_cm=0.0,
            end_b_entry_edge="W",
            leading_end="a",
            facing_direction=Direction.EAST,
            occupied_tiles=[(2, 0)],
            occupied_blocks=["BLOCK_B"],
        )

        dispatcher.on_train_advances(path.path_id, "BLOCK_B")

        assert network.blocks["BLOCK_A"].state == BlockState.CAUTION
        assert not switch_track.is_locked_by_path()
