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
from types import SimpleNamespace

from path import Path
from runtime_payloads import (
    PathSegment,
    ValidationError,
    ValidationErrorType,
    ValidationResult,
    CancellationResult,
    DEFAULT_PATH_PRIORITY,
    AcquisitionMode,
    PathAcquisitionResult,
    PathAcquisitionFailureKind,
    SwitchLockClaim,
)
from enums import PathStatus
from path_dispatcher import PathDispatcher
from network import RailwayNetwork
from switch import Switch
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
from railway_platform import Platform
from train import Train, TrainCar, TrainPosition
from tests.train_test_helpers import make_train_position
from train_route import Route, RouteMode, RouteTask
from enums import BlockState, Direction, LeadingEnd, TrainState, HandlePosition
from simulation import SimulationController
from route_executor import RouteExecutionState
from datetime import datetime


# ============================================================================
# Test Helpers (replace removed on_train_enters_path/on_train_advances)
# ============================================================================


def activate_path_for_train(dispatcher, path, train_id, block_id):
    """Simulate a train entering a path's first block.

    Replaces the removed PathDispatcher.on_train_enters_path() for test setup.
    Manually sets path state and occupies the block.
    """
    if path.train_id is None:
        path.train_id = train_id
    path.status = PathStatus.ACTIVE
    path.activated_at = datetime.now()

    if block_id in path.block_sequence:
        path.current_block_index = path.block_sequence.index(block_id)

    network = dispatcher.network
    if network.block_manager:
        network.block_manager.occupy_block(block_id, train_id)
    else:
        block = network.blocks.get(block_id)
        if block:
            block.occupy(train_id)
    return True


def advance_path_to_block(dispatcher, path, entered_block_id):
    """Simulate a train advancing to a new block along the path.

    Replaces the removed PathDispatcher.on_train_advances() for test setup.
    Manually updates path position and occupies the block.
    """
    if entered_block_id in path.block_sequence:
        path.current_block_index = path.block_sequence.index(entered_block_id)

    network = dispatcher.network
    if network.block_manager:
        network.block_manager.occupy_block(entered_block_id, path.train_id)
    else:
        block = network.blocks.get(entered_block_id)
        if block:
            block.occupy(path.train_id)

    dispatcher._release_blocks_behind_train(path)

    if entered_block_id == path.block_sequence[-1]:
        path.status = PathStatus.CLEARING

    return True


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
        acceleration=2.0,
        priority=5,
    )
    train.consist = [
        TrainCar(car_id="CAR1", car_type="LOCOMOTIVE", length_cm=20.0),
        TrainCar(car_id="CAR2", car_type="PASSENGER", length_cm=25.0),
    ]
    return train


# ============================================================================
# Test Route Data Structures
# ============================================================================




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



# ============================================================================
# Test Block Route Reservation
# ============================================================================




# ============================================================================
# Test Switch Locking
# ============================================================================


class TestSwitchLocking:
    """Tests for v1 multi-owner switch locking via `Switch.acquire_lock` /
    `Switch.release_lock`. `Track.locked_by_path` / `Track.locked_state` /
    `Track.is_locked_by_path()` are derived projections that resolve through
    `network.switches[switch_id].locks` per design_new.md."""

    def _make_switch_at(self, network, x: int, y: int):
        track = TrackSegment(x=x, y=y, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(track)
        switch = Switch(switch_id=f"SW_{x}_{y}", x=x, y=y)
        network.add_switch(switch)
        track.switch_id = switch.switch_id
        return track, switch

    def test_lock_switch_success(self, network):
        """Successful future-authority lock acquisition surfaces on the
        derived Track projection."""
        track, switch = self._make_switch_at(network, 0, 0)

        assert track.is_switch
        assert not track.is_locked_by_path()

        claim = SwitchLockClaim(
            path_id="PATH_001", train_id="TRAIN_X", locked_state=0
        )
        assert switch.acquire_lock(claim)
        assert track.is_locked_by_path()
        assert track.is_locked_by("PATH_001")
        assert track.locked_state == 0

    def test_lock_switch_conflicting_state(self, network):
        """Acquiring a lock with a different state than an existing lock fails
        (v1 multi-owner compatibility rule)."""
        _, switch = self._make_switch_at(network, 0, 0)

        first = SwitchLockClaim(
            path_id="PATH_001", train_id="TRAIN_X", locked_state=0
        )
        assert switch.acquire_lock(first)

        # Same state from a different path is allowed in v1 (compatible).
        compat = SwitchLockClaim(
            path_id="PATH_002", train_id="TRAIN_Y", locked_state=0
        )
        assert switch.acquire_lock(compat)

        # Different state is rejected.
        conflict = SwitchLockClaim(
            path_id="PATH_003", train_id="TRAIN_Z", locked_state=1
        )
        assert not switch.acquire_lock(conflict)

    def test_lock_switch_same_route_same_state_idempotent(self, network):
        """Same canonical claim with the same state is idempotently accepted."""
        _, switch = self._make_switch_at(network, 0, 0)

        claim = SwitchLockClaim(
            path_id="PATH_001", train_id="TRAIN_X", locked_state=0
        )
        assert switch.acquire_lock(claim)
        assert switch.acquire_lock(claim)

    def test_unlock_switch(self, network):
        """`release_lock` removes the future-authority claim; derived Track
        projection clears."""
        track, switch = self._make_switch_at(network, 0, 0)

        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_001", train_id="TRAIN_X", locked_state=0)
        )
        assert switch.release_lock("PATH_001", None)
        assert not track.is_locked_by_path()

    def test_unlock_switch_wrong_route(self, network):
        """Releasing a non-existent claim returns False; existing claim
        remains."""
        track, switch = self._make_switch_at(network, 0, 0)

        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_001", train_id="TRAIN_X", locked_state=0)
        )
        assert not switch.release_lock("PATH_002", None)
        assert track.is_locked_by("PATH_001")

    def test_get_lock_info(self, network):
        """`Track.get_lock_info()` projects from `Switch.locks`."""
        track, switch = self._make_switch_at(network, 0, 0)

        assert track.get_lock_info() is None

        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_001", train_id="TRAIN_X", locked_state=1)
        )
        info = track.get_lock_info()
        assert info is not None
        assert info["path_id"] == "PATH_001"
        assert info["locked_state"] == 1

    def test_can_lock_predicate(self, network):
        """`Switch.can_lock(state)` is the canonical multi-owner predicate."""
        _, switch = self._make_switch_at(network, 0, 0)

        # Empty switch — can lock either state.
        assert switch.can_lock(0)
        assert switch.can_lock(1)

        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_001", train_id="TRAIN_X", locked_state=0)
        )

        # Same state still acquirable by other compatible owners.
        assert switch.can_lock(0)
        # Different state rejected while existing lock holds state=0.
        assert not switch.can_lock(1)

    def test_non_switch_cannot_be_locked(self, network):
        """Non-switch track has no owning `Switch` entity; the derived
        projection always reports unlocked."""
        straight = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(straight)

        assert not straight.is_switch
        assert not straight.is_locked_by_path()


# ============================================================================
# Test Route Dispatcher - ID Generation
# ============================================================================


class TestPathIDGeneration:
    """Tests for route ID generation."""

    def test_generate_path_id(self, network, dispatcher):
        """Test route ID generation."""
        network.create_block("BLOCK_A", "Start")
        network.create_block("BLOCK_B", "End")

        path_id = dispatcher._generate_path_id("TRAIN_X", "BLOCK_B")
        assert path_id.startswith("PATH_")
        assert "TRAIN_X" in path_id
        assert "END" in path_id or "BLOCK_B" in path_id

    def test_path_id_sequence(self, network, dispatcher):
        """Test route ID sequence increments."""
        network.create_block("BLOCK_A")
        network.create_block("BLOCK_B")

        id1 = dispatcher._generate_path_id("TRAIN_X", "BLOCK_B")
        id2 = dispatcher._generate_path_id("TRAIN_X", "BLOCK_B")

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

        # Reserve block B for a different route via the canonical API.
        # `reserved_by_path` / `reserved_direction` / `reserved_for_train`
        # are derived read-only properties of `block.reservation`. Use a
        # compass direction here because this fixture's blocks do not
        # populate `ordered_tiles`, so block-relative `forward` cannot be
        # resolved.
        block_b = simple_linear_network.blocks["BLOCK_B"]
        block_b.reserve_for_path(
            path_id="OTHER_PATH",
            train_id="T_OTHER",
            direction="E",
        )

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




# ============================================================================
# Test Route Dispatcher - Route Lifecycle
# ============================================================================


class TestPathLifecycle:
    """Tests for route lifecycle management."""




# ============================================================================
# Test Route Dispatcher - Extension
# ============================================================================




# ============================================================================
# Test Route Dispatcher - Cancellation
# ============================================================================




# ============================================================================
# Test Path Cancellation Stops Train (Full Integration)
# ============================================================================




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

    # v1: emergency-stop and braking-distance tests removed.
    # Train.initiate_emergency_stop / update_emergency_braking /
    # get_braking_distance / get_braking_distance_cm / current_speed /
    # is_emergency_braking / state are all gone per design_new.md ~line
    # 1292; EB lifecycle and live speed live on TrainController. EB is
    # exercised in test_pending_cancel_latch.py and test_train_system.py.


# ============================================================================
# Test Query Methods
# ============================================================================


class TestQueryMethods:
    """Tests for dispatcher query methods."""



# ============================================================================
# Test Route Serialization
# ============================================================================


class TestPathSerialization:
    """Tests for route serialization."""




# ============================================================================
# Test Direction-Aware Route Extension (Bug Fix)
# ============================================================================




# ============================================================================
# Path-Based Block Release Tests
# ============================================================================




class TestSimulationPathAdvancement:
    """Tests for SimulationController path advancement integration."""










class TestPathCompletionAtStoppedDestination:
    """
    Comprehensive tests for destination-stop completion fixed in 2026-02-08.

    BUG:
    Path completion required train.head_tile == path.path_tiles[-1]. If ATO stopped
    in the destination block before the exact final tile, the path never completed
    and remained active/locked.

    FIX:
    When a path is in CLEARING and the train is stopped in the destination block,
    complete the path and release resources.
    """



class TestATODirectionDetection:
    """
    Comprehensive tests for ATO direction detection fixed in 2026-02.

    These tests ensure the following bug doesn't regress:

    BUG: When a train stopped at a station (e.g., east branch station) is assigned
    a return path back in the opposite direction, the ATO fails to flip the cab/facing
    direction. This happens because:
    1. _calculate_ato_info() was a stub that always returned required_cab="A"
    2. The turn-around logic in routes/paths.py only checked for EXACT opposite
       direction, missing 90-degree cases on diagonal track.

    FIX:
    1. _calculate_ato_info() now uses _get_path_direction_at_head() to determine
       the actual path direction and compute the correct required_cab.
    2. routes/paths.py now delegates to the controller's _calculate_ato_info()
       instead of a manual direction check with a narrow "exact opposite" condition.
    """

    def _build_straight_ew_network(self):
        """Build a simple east-west network: 6 tiles (0,0)-(5,0) with 3 blocks."""
        network = RailwayNetwork()
        for x in range(6):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            block_id="BLK_WEST", positions=[(0, 0), (1, 0)], name="West"
        )
        network.create_block_with_tracks(
            block_id="BLK_MID", positions=[(2, 0), (3, 0)], name="Middle"
        )
        network.create_block_with_tracks(
            block_id="BLK_EAST", positions=[(4, 0), (5, 0)], name="East"
        )
        return network

    def _build_vertical_ns_network(self):
        """Build a simple north-south network: 6 tiles (0,0)-(0,5) with 3 blocks."""
        network = RailwayNetwork()
        for y in range(6):
            network.add_track(TrackSegment(0, y, track_bits=TRACK_NS))
        network.create_block_with_tracks(
            block_id="BLK_SOUTH", positions=[(0, 0), (0, 1)], name="South"
        )
        network.create_block_with_tracks(
            block_id="BLK_MID", positions=[(0, 2), (0, 3)], name="Middle"
        )
        network.create_block_with_tracks(
            block_id="BLK_NORTH", positions=[(0, 4), (0, 5)], name="North"
        )
        return network

    def _build_diagonal_network(self):
        """Build a network with vertical + diagonal sections (like east branch).

        Layout:
            (0,0) NS - (0,1) NS - (0,2) NS - (0,3) SE - (1,3) NW - (1,4) SE - (2,4) NW

        This mimics the east branch: vertical track feeding into a diagonal staircase.
        """
        network = RailwayNetwork()
        # Vertical section
        for y in range(3):
            network.add_track(TrackSegment(0, y, track_bits=TRACK_NS))
        # Diagonal staircase
        network.add_track(TrackSegment(0, 3, track_bits=TRACK_SE))
        network.add_track(TrackSegment(1, 3, track_bits=TRACK_NW))
        network.add_track(TrackSegment(1, 4, track_bits=TRACK_SE))
        network.add_track(TrackSegment(2, 4, track_bits=TRACK_NW))

        # Blocks
        network.create_block_with_tracks(
            block_id="BLK_SOUTH",
            positions=[(0, 0), (0, 1)],
            name="South Block",
        )
        network.create_block_with_tracks(
            block_id="BLK_VERT",
            positions=[(0, 2), (0, 3)],
            name="Vertical Block",
        )
        network.create_block_with_tracks(
            block_id="BLK_DIAG",
            positions=[(1, 3), (1, 4)],
            name="Diagonal Block",
        )
        network.create_block_with_tracks(
            block_id="BLK_END",
            positions=[(2, 4)],
            name="End Block",
        )
        return network

    def _create_stopped_train(self, network, tile, facing, block_id):
        """Create a stopped train at the given tile with facing direction."""
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TEST_TRAIN", consist=[car], max_speed=80.0)
        train.position = make_train_position(
            tile, facing_direction=facing, occupied_blocks=[block_id]
        )
        train.current_speed = 0.0
        network.add_train(train)
        return train

    def _attach_live_path(self, controller, train, tiles, path_id="test_path", end_block_id=None):
        """Attach dispatcher-backed live path tiles to the controller.

        v1: controller.path_projection.received_path_plan reads from path_dispatcher.get_active_path(train_id).
        Per design_new.md "Path field schema", `Path.path_tiles` is exposed as
        the function `Path.get_path_tiles()`; the stub matches that contract.
        """
        class _StubPath:
            def __init__(self, path_tiles, path_id, end_block_id):
                self._path_tiles = path_tiles
                self.path_id = path_id
                self.end_block_id = end_block_id
                self.resource_sequence = []
                self.status = None

            def get_path_tiles(self):
                return self._path_tiles

            def get_authority_end_block_id(self):
                return self.end_block_id or ""

        live_paths = {path_id: _StubPath(tiles, path_id, end_block_id)}
        # Map train_id -> active path for v1 contract.
        active_paths_by_train = {train.train_id: live_paths[path_id]}

        class StubDispatcher:
            def get_path(self, requested_path_id):
                return live_paths.get(requested_path_id)

            def get_active_path(self, train_id):
                return active_paths_by_train.get(train_id)

            def active_path_id_by_train(self, train_id):
                p = active_paths_by_train.get(train_id)
                return p.path_id if p else None

        controller.path_dispatcher = StubDispatcher()
        train.assigned_path_id = path_id

    # ===== _direction_from_tiles tests =====

    def test_direction_from_tiles_east(self):
        """_direction_from_tiles: (0,0) -> (1,0) = EAST."""
        from train_controller import TrainController

        result = TrainController._direction_from_tiles((0, 0), (1, 0))
        assert result == Direction.EAST

    def test_direction_from_tiles_west(self):
        """_direction_from_tiles: (1,0) -> (0,0) = WEST."""
        from train_controller import TrainController

        result = TrainController._direction_from_tiles((1, 0), (0, 0))
        assert result == Direction.WEST

    def test_direction_from_tiles_north(self):
        """_direction_from_tiles: (0,0) -> (0,1) = NORTH."""
        from train_controller import TrainController

        result = TrainController._direction_from_tiles((0, 0), (0, 1))
        assert result == Direction.NORTH

    def test_direction_from_tiles_south(self):
        """_direction_from_tiles: (0,1) -> (0,0) = SOUTH."""
        from train_controller import TrainController

        result = TrainController._direction_from_tiles((0, 1), (0, 0))
        assert result == Direction.SOUTH

    def test_direction_from_tiles_same_tile(self):
        """_direction_from_tiles: same tile returns None."""
        from train_controller import TrainController

        result = TrainController._direction_from_tiles((0, 0), (0, 0))
        assert result is None

    def test_direction_from_tiles_diagonal_prefers_x(self):
        """_direction_from_tiles: diagonal delta prioritizes x over y."""
        from train_controller import TrainController

        # dx=1, dy=1 -> should return EAST (dx checked first)
        result = TrainController._direction_from_tiles((0, 0), (1, 1))
        assert result == Direction.EAST

    # ===== _get_path_direction_at_head tests =====

    def test_path_direction_at_head_forward(self):
        """Path direction at head returns forward direction when head is in path."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"

        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)])

        result = controller._get_path_direction_at_head(train)
        assert result == Direction.EAST

    def test_path_direction_at_head_backward(self):
        """Path direction at head returns west when path goes backward."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"

        self._attach_live_path(controller, train, [(4, 0), (3, 0), (2, 0)])

        result = controller._get_path_direction_at_head(train)
        assert result == Direction.WEST

    def test_path_direction_at_head_fallback_to_first_tiles(self):
        """When head tile not in path, falls back to first two tiles."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (5, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"

        # Path doesn't include head tile (5,0)
        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)])

        result = controller._get_path_direction_at_head(train)
        assert result == Direction.EAST  # from (2,0) -> (3,0)

    def test_path_direction_at_head_infers_from_next_tile(self):
        """When path entry has no direction, infers from next tile."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"

        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)])

        result = controller._get_path_direction_at_head(train)
        assert result == Direction.EAST

    def test_path_direction_at_head_last_tile_backward_infer(self):
        """When head is the last tile in path, infers from previous tile."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"

        # Head is the last tile — no next tile to compare
        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)])

        result = controller._get_path_direction_at_head(train)
        # Infers from (3,0) -> (4,0) = EAST
        assert result == Direction.EAST

    # ===== _calculate_ato_info required_cab tests =====

    def test_ato_info_same_direction_keeps_cab(self):
        """When path direction matches facing, required_cab stays the same."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"
        controller.power_on = True

        # Path goes EAST, train faces EAST -> same direction
        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)], end_block_id="BLK_EAST")

        info = controller.ato.compute_info()
        assert info is not None
        assert info.required_cab == "A"  # keep current cab

    def test_ato_info_opposite_direction_switches_cab(self):
        """When path direction is opposite to facing, required_cab flips."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"
        controller.power_on = True

        # Path goes WEST, train faces EAST -> opposite
        self._attach_live_path(controller, train, [(4, 0), (3, 0), (2, 0)], end_block_id="BLK_WEST")

        info = controller.ato.compute_info()
        assert info is not None
        assert info.required_cab == "B"  # flip to opposite cab

    def test_ato_info_opposite_direction_cab_b_to_a(self):
        """When active_cab is B and path is opposite, required_cab becomes A."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.WEST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "B"
        controller.power_on = True

        # Path goes EAST, train faces WEST (cab B) -> opposite
        self._attach_live_path(controller, train, [(4, 0), (5, 0)], end_block_id="BLK_EAST")

        info = controller.ato.compute_info()
        assert info is not None
        assert info.required_cab == "A"

    def test_ato_info_90_degree_keeps_cab(self):
        """When path direction is 90 degrees off, keeps current cab (ambiguous)."""
        network = self._build_straight_ew_network()
        # Also add some NS track for variety
        network.add_track(TrackSegment(2, 1, track_bits=TRACK_NS))
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"
        controller.power_on = True

        # Path goes NORTH, train faces EAST -> 90 degrees
        self._attach_live_path(controller, train, [(2, 0), (2, 1)], end_block_id="BLK_EAST")

        info = controller.ato.compute_info()
        assert info is not None
        assert info.required_cab == "A"  # keep current - ambiguous

    # ===== ato_go auto-correction tests =====

    def test_ato_go_auto_corrects_cab_for_opposite_path(self):
        """ato_go() should auto-switch cab when path goes opposite to facing."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_cab("A")

        assert controller.commands.cab == "A"
        assert controller.ops.state == TrainState.READY

        # Assign a path going WEST (opposite)
        self._attach_live_path(controller, train, [(4, 0), (3, 0), (2, 0)], end_block_id="BLK_WEST")
        controller.commands.control_mode = "ato"

        # Recalculate ato_info (normally done by tick)
        controller.ato.info = controller.ato.compute_info()
        assert controller.ato.info.required_cab == "B"

        # ato_go should auto-correct the cab
        result = controller.ato.go()
        assert result.success, f"ato_go failed: {result.message}"
        assert controller.commands.cab == "B"
        assert controller.ato.running is True

    def test_ato_go_no_correction_when_cab_matches(self):
        """ato_go() should NOT switch cab when path matches facing direction."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_cab("A")

        assert controller.commands.cab == "A"

        # Path goes EAST (same as facing)
        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)], end_block_id="BLK_EAST")
        controller.commands.control_mode = "ato"

        controller.ato.info = controller.ato.compute_info()
        assert controller.ato.info.required_cab == "A"

        result = controller.ato.go()
        assert result.success
        assert controller.commands.cab == "A"  # unchanged

    # ===== Diagonal / real-world scenario tests =====

    def test_ato_info_diagonal_return_path(self):
        """Test direction detection on a diagonal staircase (like east branch).

        Train faces NORTH at (1,4), path goes south-west back through diagonal.
        _calculate_ato_info should detect the path goes opposite to facing.
        """
        network = self._build_diagonal_network()
        train = self._create_stopped_train(network, (1, 4), Direction.NORTH, "BLK_DIAG")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"
        controller.power_on = True

        # Return path goes south through diagonal: (1,4) -> (1,3) -> (0,3) -> (0,2) -> (0,1)
        # Tile-coord direction from (1,4) -> (1,3) is dy=-1 = SOUTH
        self._attach_live_path(controller, train, [(1, 4), (1, 3), (0, 3), (0, 2), (0, 1)], "test_return", end_block_id="BLK_SOUTH")

        info = controller.ato.compute_info()
        assert info is not None
        # Train faces NORTH, path at head goes SOUTH -> opposite -> switch cab
        assert info.required_cab == "B"

    def test_ato_info_diagonal_facing_east_path_south(self):
        """Train on diagonal facing EAST, path goes SOUTH — 90 degrees at head.

        The first path tile direction (SOUTH) is 90 degrees from the train's
        facing direction (EAST).  However, the *overall* path vector from
        (1,4) to (0,3) points WEST (dx=-1), which is the opposite of EAST.
        So the cab SHOULD switch.

        This was previously kept as 'ambiguous', but the overall-vector
        resolution introduced to fix the diagonal staircase return-trip bug
        now correctly detects this as a turn-around.
        """
        network = self._build_diagonal_network()
        train = self._create_stopped_train(network, (1, 4), Direction.EAST, "BLK_DIAG")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"
        controller.power_on = True

        # Return path: (1,4) -> (1,3) = SOUTH
        self._attach_live_path(controller, train, [(1, 4), (1, 3), (0, 3)], "test_return", end_block_id="BLK_SOUTH")

        info = controller.ato.compute_info()
        assert info is not None
        # Overall vector (1,4)->(0,3): dx=-1 -> WEST, opposite of EAST -> switch cab
        assert info.required_cab == "B"

    def test_path_assignment_turn_around_opposite(self):
        """ato_go() should turn train around when path is opposite direction.

        Previously path assignment handled turn-around directly, but now
        ato_go() is the single point for cab auto-correction.
        """
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_cab("A")
        controller.commands.control_mode = "ato"

        assert controller.commands.cab == "A"

        # Assign a return path going WEST (no turn-around happens here anymore)
        self._attach_live_path(controller, train, [(4, 0), (3, 0), (2, 0)], end_block_id="BLK_WEST")

        # Cab should NOT have changed yet — path assignment no longer turns
        assert controller.commands.cab == "A"

        # ato_go() should auto-correct the cab
        result = controller.ato.go()
        assert result.success, f"ato_go failed: {result.message}"
        assert controller.commands.cab == "B"
        assert train.facing_direction == Direction.WEST

    def test_path_assignment_no_turn_when_same_direction(self):
        """ato_go() should NOT switch cab when path matches facing direction."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_cab("A")
        controller.commands.control_mode = "ato"

        assert controller.commands.cab == "A"

        # Assign forward path going EAST
        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)], end_block_id="BLK_EAST")

        # ato_go() should NOT switch cab
        result = controller.ato.go()
        assert result.success
        assert controller.commands.cab == "A"
        assert train.facing_direction == Direction.EAST

    def test_vertical_train_return_path_south(self):
        """Train facing NORTH on vertical track, assigned return path SOUTH.

        Tests exact-opposite on vertical track.
        """
        network = self._build_vertical_ns_network()
        train = self._create_stopped_train(
            network, (0, 4), Direction.NORTH, "BLK_NORTH"
        )

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"
        controller.power_on = True

        self._attach_live_path(controller, train, [(0, 4), (0, 3), (0, 2)], end_block_id="BLK_SOUTH")

        info = controller.ato.compute_info()
        assert info is not None
        assert info.required_cab == "B"  # NORTH -> SOUTH = opposite

    def test_calculate_ato_info_no_path(self):
        """_calculate_ato_info returns None when no path or destination."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.commands.cab = "A"

        # No path, no destination
        train.assigned_path_id = None

        info = controller.ato.compute_info()
        assert info is None

    # v1: removed test_calculate_ato_info_short_path which wrote
    # `train.current_path` / `train.assigned_path_id` to fake path data.
    # Per design_new.md ~line 1292 those fields are forbidden — the
    # dispatcher's Path is the source of truth and ato_info should be
    # tested via a real `PathDispatcher.create_path(...)` setup.




