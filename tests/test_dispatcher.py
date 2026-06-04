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
    PathAcquisitionResultKind,
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
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from tests.train_test_helpers import set_train_span
from train_route import Route, RouteMode, RouteTask
from enums import BlockState, Direction, TrainState, HandlePosition
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


def _make_seeded_controller(sim, train):
    """Create (via the sim) the train's controller and seed its single-tile
    span from the placement stashed by `_create_stopped_train`.

    v1: head tile and facing derive from (cab, `physics_sync.current_span`),
    so a controller built straight from `place_train`/`add_train` has no head
    until its span is seeded. This mirrors the old `train.position` placement
    for the unit tests that drive controller/ATO behaviour from the head.
    """
    controller = sim._get_or_create_controller(train)
    set_train_span(
        controller,
        train._seed_head_tile,
        facing_direction=train._seed_facing,
        occupied_blocks=[train._seed_block_id],
    )
    return controller


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
    `Switch.release_lock`. `TrackSegment` exposes no lock surface in v1; all
    lock state lives on the owning `Switch` entity (`Switch.locks`,
    `Switch.is_locked()`) per design_new.md:651."""

    def _make_switch_at(self, network, x: int, y: int):
        track = TrackSegment(x=x, y=y, track_bits=TRACK_EW | TRACK_NE)
        network.add_track(track)
        switch = Switch(switch_id=f"SW_{x}_{y}", x=x, y=y)
        network.add_switch(switch)
        track.switch_id = switch.switch_id
        return track, switch

    def test_lock_switch_success(self, network):
        """Successful future-authority lock acquisition is recorded on the
        owning `Switch` entity."""
        track, switch = self._make_switch_at(network, 0, 0)

        assert track.is_switch
        assert not switch.is_locked()

        claim = SwitchLockClaim(
            path_id="PATH_001", train_id="TRAIN_X", locked_state=0
        )
        assert switch.acquire_lock(claim)
        assert switch.is_locked()
        assert ("PATH_001", None) in switch.locks
        assert switch.locks[("PATH_001", None)][1] == 0

    def test_lock_switch_conflicting_state(self, network):
        """Lock compatibility rules per design_new.md:617, :624, :633:

        - same-train multi-path same-state co-locking is allowed
          (e.g. extend_path reuses the live path's lock);
        - cross-train primary same-state co-locking is rejected — two
          trains' primary claims would both authorize physical traversal
          but `Switch.occupied_by` is single-owner, so the second
          arriver would hit the fatal `SWITCH_OCCUPIED_BY_OTHER_TRAIN`
          conflict;
        - different-state claims are rejected regardless of train.
        """
        _, switch = self._make_switch_at(network, 0, 0)

        first = SwitchLockClaim(
            path_id="PATH_001", train_id="TRAIN_X", locked_state=0
        )
        assert switch.acquire_lock(first)

        # Same train, different path, same state: allowed (e.g. extend).
        same_train = SwitchLockClaim(
            path_id="PATH_001b", train_id="TRAIN_X", locked_state=0
        )
        assert switch.acquire_lock(same_train)

        # Different train, same state primary: rejected.
        cross_train = SwitchLockClaim(
            path_id="PATH_002", train_id="TRAIN_Y", locked_state=0
        )
        assert not switch.acquire_lock(cross_train), (
            "Cross-train primary same-state co-locking is rejected per "
            "design_new.md:624 — the second arriving train would crash "
            "on Switch.occupied_by single-owner check."
        )

        # Different state is rejected regardless of train.
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

    def test_cross_train_non_conflicting_routes_coexist(self, network):
        """Two trains with geometrically non-conflicting routes (NW + SE on
        a scissors crossover) may both hold primary locks at the same
        ``locked_state``, per design_new.md:617/:624/:649. This is the
        case the bare cross-train-primary rejection used to block — with
        route info the lock layer is gated by geometry instead.
        """
        from switch import routes_conflict, make_route_key
        from track import TRACK_NS, TRACK_EW, TRACK_NW, TRACK_SE
        # Place a scissors crossover (NS+EW+NW+SE) so DIVERGE alignment
        # exposes the NW and SE routes simultaneously.
        track = TrackSegment(
            x=0, y=0,
            track_bits=TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE,
        )
        network.add_track(track)
        switch = Switch(switch_id="SW_SCISSOR", x=0, y=0)
        network.add_switch(switch)
        track.switch_id = switch.switch_id
        switch.position = 1  # DIVERGE — NW + SE routes active

        nw = make_route_key("N", "W")
        se = make_route_key("S", "E")
        assert not routes_conflict(nw, se), (
            "Geometry precondition: NW and SE arcs are in opposite "
            "quadrants and must not conflict."
        )

        # T1 locks NW; T2 (different train) locks SE at the same state.
        t1 = SwitchLockClaim(
            path_id="PATH_T1", train_id="T1",
            locked_state=1, route=nw,
        )
        t2 = SwitchLockClaim(
            path_id="PATH_T2", train_id="T2",
            locked_state=1, route=se,
        )
        assert switch.acquire_lock(t1)
        assert switch.acquire_lock(t2), (
            "Cross-train primary co-locking must be allowed when the "
            "routes are geometrically non-conflicting per "
            "design_new.md:624."
        )
        # A third train trying NS (which crosses the tile center) must
        # conflict with both — NS shares N with NW and S with SE.
        ns = make_route_key("N", "S")
        t3 = SwitchLockClaim(
            path_id="PATH_T3", train_id="T3",
            locked_state=1, route=ns,
        )
        assert not switch.acquire_lock(t3), (
            "NS shares the N edge with NW and the S edge with SE; must "
            "reject regardless of train identity."
        )

    def test_cross_train_conflicting_routes_rejected(self, network):
        """NW + NE share the N edge midpoint — even on a tile where both
        bits are present, cross-train primary co-locking with conflicting
        routes is rejected. Same train would also be rejected, since
        route_owners can't hold two trains on overlapping geometry.
        """
        from switch import make_route_key
        from track import TRACK_NW, TRACK_NE
        track = TrackSegment(
            x=0, y=0,
            track_bits=TRACK_NW | TRACK_NE,
        )
        network.add_track(track)
        switch = Switch(switch_id="SW_T", x=0, y=0)
        network.add_switch(switch)
        track.switch_id = switch.switch_id

        nw = make_route_key("N", "W")
        ne = make_route_key("N", "E")
        assert switch.acquire_lock(SwitchLockClaim(
            path_id="P1", train_id="T1", locked_state=0, route=nw,
        ))
        # Different train, conflicting route (shares N edge): rejected.
        assert not switch.acquire_lock(SwitchLockClaim(
            path_id="P2", train_id="T2", locked_state=0, route=ne,
        ))

    def test_unlock_switch(self, network):
        """`release_lock` removes the future-authority claim from the owning
        `Switch`."""
        _, switch = self._make_switch_at(network, 0, 0)

        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_001", train_id="TRAIN_X", locked_state=0)
        )
        assert switch.release_lock("PATH_001", None)
        assert not switch.is_locked()

    def test_unlock_switch_wrong_route(self, network):
        """Releasing a non-existent claim returns False; existing claim
        remains."""
        _, switch = self._make_switch_at(network, 0, 0)

        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_001", train_id="TRAIN_X", locked_state=0)
        )
        assert not switch.release_lock("PATH_002", None)
        assert ("PATH_001", None) in switch.locks

    def test_lock_record_shape(self, network):
        """A recorded lock is keyed by `(path_id, source_switch_id)` and
        stores `(train_id, locked_state)` on the owning `Switch` per
        design_new.md:651."""
        _, switch = self._make_switch_at(network, 0, 0)

        assert switch.locks == {}

        switch.acquire_lock(
            SwitchLockClaim(path_id="PATH_001", train_id="TRAIN_X", locked_state=1)
        )
        assert ("PATH_001", None) in switch.locks
        train_id, locked_state = switch.locks[("PATH_001", None)]
        assert train_id == "TRAIN_X"
        assert locked_state == 1

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

    def test_non_switch_has_no_owning_switch(self, network):
        """Non-switch track has no `switch_id` and no owning `Switch`
        entity, so it has no lock surface at all."""
        straight = TrackSegment(x=0, y=0, track_bits=TRACK_EW)
        network.add_track(straight)

        assert not straight.is_switch
        assert straight.switch_id is None
        assert network.switches.get(straight.switch_id) is None


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
        """Create a stopped train at the given tile with facing direction.

        v1: the live position is the controller's `physics_sync.current_span`,
        not `Train.position`. We stash the intended head tile / facing on the
        train so `_make_seeded_controller` can seed the controller span once it
        is created (the controller registry is owned by the sim the caller
        builds next).
        """
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="TEST_TRAIN", consist=[car], max_speed=80.0)
        train._seed_head_tile = tile
        train._seed_facing = facing
        train._seed_block_id = block_id
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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)])

        result = controller._get_path_direction_at_head(train)
        assert result == Direction.EAST

    def test_path_direction_at_head_backward(self):
        """Path direction at head returns west when path goes backward."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

        self._attach_live_path(controller, train, [(4, 0), (3, 0), (2, 0)])

        result = controller._get_path_direction_at_head(train)
        assert result == Direction.WEST

    def test_path_direction_at_head_fallback_to_first_tiles(self):
        """When head tile not in path, falls back to first two tiles."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (5, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)])

        result = controller._get_path_direction_at_head(train)
        assert result == Direction.EAST

    def test_path_direction_at_head_last_tile_backward_infer(self):
        """When head is the last tile in path, infers from previous tile."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "B"

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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

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
        controller = _make_seeded_controller(sim, train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

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
        controller = _make_seeded_controller(sim, train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

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
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

        # Return path: (1,4) -> (1,3) = SOUTH
        self._attach_live_path(controller, train, [(1, 4), (1, 3), (0, 3)], "test_return", end_block_id="BLK_SOUTH")

        info = controller.ato.compute_info()
        assert info is not None
        # Overall vector (1,4)->(0,3): dx=-1 -> WEST, opposite of EAST -> switch cab
        assert info.required_cab == "B"

    def test_ato_info_tunnel_loop_no_spurious_flip(self):
        """Train traversing a circular loop through a tunnel: cab must NOT flip.

        Pins the fix for the tunnel-loop "Cab points away from authority" bug.

        Pre-fix, _required_cab_for_path() relied on a cardinal-direction
        fallback (`_dominant_direction_from_tiles`) which treated the
        non-grid tile pair (E end of west block ↔ W end of east block)
        as a straight-line displacement, decided the overall path "went
        backward," and required a cab flip. With the fix, the connectivity
        check (which now consults `network.get_connection_edges`, so it
        understands tunnel adjacency) returns "no flip," and the fallback
        is skipped because the path crosses a tunnel hop.
        """
        from network import RailwayNetwork
        from track import TrackSegment, TRACK_EW

        network = RailwayNetwork()
        # Two short EW segments far apart, joined by a tunnel between
        # (2,0).E and (10,0).W — same shape as TunnelScene.
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        for x in range(10, 13):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        network.create_block_with_tracks(
            block_id="BLK_WEST", positions=[(0, 0), (1, 0), (2, 0)], name="W"
        )
        network.create_block_with_tracks(
            block_id="BLK_EAST", positions=[(10, 0), (11, 0), (12, 0)], name="E"
        )
        network.add_tunnel((2, 0, "E"), (10, 0, "W"))

        # Train at (2, 0) facing EAST: entered through W, would tunnel to (10, 0).
        train = self._create_stopped_train(
            network, (2, 0), Direction.EAST, "BLK_WEST"
        )

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

        # Path: (2, 0) -> (10, 0) (tunnel hop) -> (11, 0) -> (12, 0).
        # Note (2,0)->(10,0) has dx=8, dy=0 — not grid-adjacent, but
        # tunnel-adjacent via get_connection_edges.
        self._attach_live_path(
            controller, train,
            [(2, 0), (10, 0), (11, 0), (12, 0)],
            "test_tunnel_loop",
            end_block_id="BLK_EAST",
        )

        # The path goes forward through the tunnel — train continues facing
        # EAST after the tunnel hop, no cab flip needed.
        info = controller.ato.compute_info()
        assert info is not None
        assert info.required_cab == "A", (
            "Tunnel hop should not be mistaken for a backward move; cab A "
            "remains correct because the train keeps moving forward through "
            "the tunnel."
        )

    def test_path_assignment_turn_around_opposite(self):
        """ato_go() should turn train around when path is opposite direction.

        Previously path assignment handled turn-around directly, but now
        ato_go() is the single point for cab auto-correction.
        """
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
        # Seed a real two-tile span (head (4,0) facing EAST, tail one tile
        # behind at (3,0)) so the cab flip genuinely re-derives facing. A
        # degenerate single-tile span (end_a == end_b) cannot express a
        # cab-driven orientation flip.
        controller.physics_sync.current_span = TrainSpan(
            end_a=SpanEndLocator(tile=(4, 0), distance_cm=20.0, entry_edge="W"),
            end_b=SpanEndLocator(tile=(3, 0), distance_cm=0.0, entry_edge="W"),
            occupied_tiles=[(4, 0), (3, 0)],
            occupied_blocks=["BLK_EAST", "BLK_MID"],
        )
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")
        controller.commands.control_mode = "ato"

        assert controller.commands.cab == "A"
        assert controller._facing_direction() == Direction.EAST

        # Assign a return path going WEST (no turn-around happens here anymore)
        self._attach_live_path(controller, train, [(4, 0), (3, 0), (2, 0)], end_block_id="BLK_WEST")

        # Cab should NOT have changed yet — path assignment no longer turns
        assert controller.commands.cab == "A"

        # ato_go() should auto-correct the cab
        result = controller.ato.go()
        assert result.success, f"ato_go failed: {result.message}"
        assert controller.commands.cab == "B"
        # Cab B makes end_b (the (3,0) end) the head -> train now faces WEST.
        assert controller._facing_direction() == Direction.WEST

    def test_path_assignment_no_turn_when_same_direction(self):
        """ato_go() should NOT switch cab when path matches facing direction."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")
        controller.commands.control_mode = "ato"

        assert controller.commands.cab == "A"

        # Assign forward path going EAST
        self._attach_live_path(controller, train, [(2, 0), (3, 0), (4, 0)], end_block_id="BLK_EAST")

        # ato_go() should NOT switch cab
        result = controller.ato.go()
        assert result.success
        assert controller.commands.cab == "A"
        # Facing derives from (cab, current_span); cab unchanged -> still EAST.
        assert controller._facing_direction() == Direction.EAST

    def test_vertical_train_return_path_south(self):
        """Train facing NORTH on vertical track, assigned return path SOUTH.

        Tests exact-opposite on vertical track.
        """
        network = self._build_vertical_ns_network()
        train = self._create_stopped_train(
            network, (0, 4), Direction.NORTH, "BLK_NORTH"
        )

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
        controller.commands.cab = "A"

        self._attach_live_path(controller, train, [(0, 4), (0, 3), (0, 2)], end_block_id="BLK_SOUTH")

        info = controller.ato.compute_info()
        assert info is not None
        assert info.required_cab == "B"  # NORTH -> SOUTH = opposite

    def test_calculate_ato_info_no_path(self):
        """_calculate_ato_info returns None when no path or destination."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = _make_seeded_controller(sim, train)
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


class TestBestEffortAcquisitionContract:
    """create_path / extend_path BEST_EFFORT contract per design_new.md.

    Cites:
      * :884-:911 — BEST_EFFORT must truncate to the longest acquirable
        block-terminated prefix and return `result_kind=TRUNCATED_GREW`.
      * :890     — structural failures (disabled blocks, missing tracks)
        still hard-reject.
      * :938     — the first failing block/switch is the truncation point.
      * :894     — `head_on_switch_no_block_authority` exception case.
    """

    def _make_probe_train(self, network, dispatcher):
        """Place the probe train at BLOCK_A (0,0, facing EAST) and seed a
        controller span so the dispatcher can resolve its current block.

        v1: the head/current block derives from
        `TrainController.physics_sync.current_span`, which the dispatcher
        reads through its `simulation_controller` controller registry. Wire
        the dispatcher to a `SimulationController` over the same network and
        create+seed the controller via that sim.
        """
        car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="T_PROBE", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        sim = SimulationController(network, dispatcher)
        controller = sim._get_or_create_controller(train)
        set_train_span(
            controller,
            (0, 0),
            facing_direction=Direction.EAST,
            occupied_blocks=["BLOCK_A"],
        )
        return train

    def test_create_path_best_effort_truncates_on_occupied_block(
        self, simple_linear_network, dispatcher,
    ):
        """An OCCUPIED future block in BEST_EFFORT mode is a truncation
        point — create_path must succeed with TRUNCATED_GREW. Without the
        fix, this asserts success=False with BLOCKED_BEFORE_TARGET.
        """
        dispatcher.network = simple_linear_network
        self._make_probe_train(simple_linear_network, dispatcher)
        simple_linear_network.blocks["BLOCK_C"].occupy("T_OTHER")

        result = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert result.success, (
            f"BEST_EFFORT create_path must truncate on an occupied future "
            f"block per design_new.md:884-911. Got failure_kind="
            f"{result.failure_kind!r}, reason={result.reason!r}"
        )
        assert result.result_kind == PathAcquisitionResultKind.TRUNCATED_GREW
        assert result.authority_end_block_id == "BLOCK_B", (
            f"Expected truncation to BLOCK_B (longest acquirable prefix "
            f"toward occupied BLOCK_C). Got: {result.authority_end_block_id!r}"
        )
        assert result.path_id is not None
        assert result.resource_sequence is not None
        assert len(result.resource_sequence) >= 1

    def test_create_path_all_or_nothing_still_hard_rejects_on_occupied(
        self, simple_linear_network, dispatcher,
    ):
        """The fix must NOT change ALL_OR_NOTHING semantics — an occupied
        future block is still a hard reject in that mode.
        """
        dispatcher.network = simple_linear_network
        self._make_probe_train(simple_linear_network, dispatcher)
        simple_linear_network.blocks["BLOCK_C"].occupy("T_OTHER")

        result = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.ALL_OR_NOTHING,
        )

        assert not result.success
        assert result.failure_kind == PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET

    def test_create_path_best_effort_hard_rejects_on_disabled_block(
        self, simple_linear_network, dispatcher,
    ):
        """Structural failures (BLOCK_DISABLED) must still hard-reject
        even in BEST_EFFORT mode, per design_new.md:890.
        """
        dispatcher.network = simple_linear_network
        self._make_probe_train(simple_linear_network, dispatcher)
        simple_linear_network.blocks["BLOCK_B"].disabled = True

        result = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert not result.success, (
            "A disabled future block is structural and must hard-reject "
            "regardless of acquisition_mode (design_new.md:890)."
        )
        # The disabled block can be filtered by the pathfinder upstream
        # (NO_VIABLE_PATH) or caught by validate_path (BLOCKED_BEFORE_TARGET).
        # Either is acceptable per the design.
        assert result.failure_kind in (
            PathAcquisitionFailureKind.NO_VIABLE_PATH,
            PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET,
        )

    def test_extend_path_best_effort_truncates_on_occupied_future_block(
        self, simple_linear_network, dispatcher,
    ):
        """extend_path BEST_EFFORT must also truncate (symmetric with
        create_path) per design_new.md:994. First create a successful
        path to BLOCK_B, then try to extend to BLOCK_C with BLOCK_C
        occupied — the extension cannot grow but the existing path
        remains intact.
        """
        dispatcher.network = simple_linear_network
        self._make_probe_train(simple_linear_network, dispatcher)

        first = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_B",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )
        assert first.success, f"setup failed: {first.reason}"
        path_id = first.path_id

        # Occupy BLOCK_C — extension must hit it as a conflict.
        simple_linear_network.blocks["BLOCK_C"].occupy("T_OTHER")

        result = dispatcher.extend_path(
            path_id=path_id,
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        # With BLOCK_C the only new block in the extension segment and it
        # occupied by another train, no new resource can be appended. Per
        # design_new.md ":994" / ":997", BEST_EFFORT extend in this case
        # must return `success=True, result_kind=BLOCKED_NO_GROWTH` — the
        # existing path is still a legal prefix toward BLOCK_C; the
        # obstruction is purely live occupancy. Route executors use this
        # to distinguish "temporarily blocked, retry" from cancellation.
        assert result.success, (
            f"extend_path should report blocked_no_growth as success per "
            f"design_new.md:994; got failure_kind={result.failure_kind!r}"
        )
        assert result.result_kind == PathAcquisitionResultKind.BLOCKED_NO_GROWTH
        # Existing path must still be ACTIVE and unchanged.
        live_path = dispatcher.paths.get(path_id)
        assert live_path is not None
        assert live_path.status == PathStatus.ACTIVE
        assert live_path.get_authority_end_block_id() == "BLOCK_B"

    def test_create_path_best_effort_truncates_before_locked_switch(
        self, network, dispatcher,
    ):
        """A boundary switch whose lock is held in a conflicting state must
        NOT force the truncated prefix to keep that switch.

        Layout (all EW; switch at (2,0) supports EW MAIN or NE DIVERGE):

            [A:(0,0)]--[B:(1,0)]--[SW:(2,0)]--[C:(3,0)]

        With SW pre-locked by another path in DIVERGE (state=1), the
        candidate A->B->SW->C requires state=0 and cannot acquire SW.
        Per design_new.md ":891" / ":938", BEST_EFFORT must truncate the
        live authority to land BEFORE the contested switch (authority
        ends at BLOCK_B). Without the fix `_best_effort_truncate` keeps
        the switch tile in `cutoff_tiles`, propagates its lock request
        into `cutoff_switches`, fails the acquire, and returns None — so
        `create_path` falls through to BLOCKED_BEFORE_TARGET.
        """
        # Build A--B--SW--C inline.
        for x in range(4):
            bits = (TRACK_EW | TRACK_NE) if x == 2 else TRACK_EW
            network.add_track(TrackSegment(x=x, y=0, track_bits=bits))

        block_a = network.create_block("BLOCK_A", "Block A")
        block_a.length_cm = 100.0
        network.get_track(0, 0).block_section_id = "BLOCK_A"

        block_b = network.create_block("BLOCK_B", "Block B")
        block_b.length_cm = 100.0
        network.get_track(1, 0).block_section_id = "BLOCK_B"

        switch = Switch(switch_id="SW_X", x=2, y=0)
        network.add_switch(switch)
        network.get_track(2, 0).switch_id = "SW_X"

        block_c = network.create_block("BLOCK_C", "Block C")
        block_c.length_cm = 100.0
        network.get_track(3, 0).block_section_id = "BLOCK_C"

        self._make_probe_train(network, dispatcher)

        # Pre-lock the switch in DIVERGE (state=1) on behalf of another
        # path so the candidate's required MAIN (state=0) is unavailable.
        held = SwitchLockClaim(
            path_id="PATH_OTHER", train_id="T_OTHER", locked_state=1
        )
        assert switch.acquire_lock(held)

        result = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert result.success, (
            f"BEST_EFFORT create_path must truncate before a locked "
            f"boundary switch per design_new.md:891/:938. Got "
            f"failure_kind={result.failure_kind!r}, reason={result.reason!r}"
        )
        assert result.result_kind == PathAcquisitionResultKind.TRUNCATED_GREW
        assert result.authority_end_block_id == "BLOCK_B", (
            f"Truncated authority must land at BLOCK_B (before the "
            f"contested switch). Got: {result.authority_end_block_id!r}"
        )


class TestSameBlockSyntheticAuthority:
    """`create_path(...)` same-block / same-tile synthetic authority per
    design_new.md:958-:971 (M8).

    When the requested destination block is already inside the train's
    current occupied span, the pathfinder cannot walk a block to itself, so
    dispatcher must instead build a synthetic local path with one block
    resource whose `path_tiles` slice covers the local movement, derived
    from the train's occupied span plus `requested_destination_direction`.
    For a movement that stays inside a single tile, dispatcher must also
    have a selected cab whose direction agrees with
    `requested_destination_direction`, else reject with a defined reason
    (design_new.md:961).
    """

    def _rebuild_geometry(self, network):
        """Populate `Block.ordered_tiles` the way a real edited network
        does, so block-relative `forward`/`reverse` resolve to real compass
        edges (the bare fixtures only set `block_section_id`)."""
        gm = network._get_block_geometry_manager()
        for block_id in list(network.blocks.keys()):
            gm.rebuild_block_ordered_tiles(block_id)

    def _seed_train_in_block(
        self,
        network,
        dispatcher,
        *,
        head_tile,
        occupied_tiles,
        block_id,
        facing=Direction.EAST,
        cab=None,
    ):
        """Place a train whose occupied span covers `block_id` and seed a
        controller span so the dispatcher reads it as the current block.

        Mirrors `TestBestEffortAcquisitionContract._make_probe_train` but
        lets the caller span a multi-tile block and pick a cab.
        """
        car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
        train = Train(train_id="T_SAME", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, head_tile[0], head_tile[1], facing)

        sim = SimulationController(network, dispatcher)
        controller = sim._get_or_create_controller(train)
        if cab is not None:
            controller.commands.set_cab(cab)
        set_train_span(
            controller,
            head_tile,
            facing_direction=facing,
            occupied_tiles=list(occupied_tiles),
            occupied_blocks=[block_id],
        )
        return train, controller

    def _cab_block_direction(self, controller, network):
        """The block-relative ("forward"/"reverse") direction the selected
        cab faces, mapped from the head end's outward compass edge through
        the head block's `block_direction_for_compass_exit`. This is the
        same disambiguation signal the dispatcher must use for a same-tile
        move (design_new.md:961)."""
        head_end = controller._head_end_label()
        facing = controller._orientation_for_head_end(head_end)
        edge = controller._direction_to_edge(facing) if facing else None
        head_tile = controller._get_head_tile()
        track = network.get_track(*head_tile)
        block = network.blocks[track.block_section_id]
        return block.block_direction_for_compass_exit(edge)

    def _opposite_of_cab_block_direction(self, controller, network):
        d = self._cab_block_direction(controller, network)
        return "reverse" if d == "forward" else "forward"

    def test_same_block_forward_builds_one_block_synthetic_path(
        self, simple_linear_network, dispatcher,
    ):
        """A train spanning the multi-tile BLOCK_B that requests BLOCK_B as
        its own destination must NOT get NO_VIABLE_PATH; dispatcher builds a
        synthetic one-block authority over the local movement
        (design_new.md:960-:961).
        """
        dispatcher.network = simple_linear_network
        self._rebuild_geometry(simple_linear_network)
        # Train body covers both BLOCK_B tiles; head at (2,0) facing EAST.
        self._seed_train_in_block(
            simple_linear_network,
            dispatcher,
            head_tile=(2, 0),
            occupied_tiles=[(1, 0), (2, 0)],
            block_id="BLOCK_B",
        )

        result = dispatcher.create_path(
            train_id="T_SAME",
            requested_destination_block_id="BLOCK_B",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert result.success, (
            f"Same-block create_path must build synthetic local authority "
            f"per design_new.md:960-:961, not NO_VIABLE_PATH. Got "
            f"failure_kind={result.failure_kind!r}, reason={result.reason!r}"
        )
        assert result.authority_end_block_id == "BLOCK_B"
        assert result.requested_destination_block_id == "BLOCK_B"
        # Exactly one block resource in the live sequence.
        assert result.resource_sequence is not None
        assert len(result.resource_sequence) == 1
        entry = result.resource_sequence[0]
        assert entry.resource_type == "block"
        assert entry.resource_id == "BLOCK_B"
        # Forward traversal of BLOCK_B exits EAST and enters from WEST.
        assert entry.exit_edge == "E", (
            f"forward exit edge must be E (BLOCK_B increasing direction); "
            f"got {entry.exit_edge!r}"
        )
        assert entry.entry_edge == "W"
        # path_tiles cover the local block tiles in forward (ordered) order.
        assert [(t.x, t.y) for t in entry.path_tiles] == [(1, 0), (2, 0)]
        # A live ACTIVE path record exists keyed by the returned id.
        live = dispatcher.paths.get(result.path_id)
        assert live is not None and live.status == PathStatus.ACTIVE

    def test_same_block_reverse_builds_reversed_tile_slice(
        self, simple_linear_network, dispatcher,
    ):
        """A reverse same-block request must build the local frame in the
        decreasing direction of the block's `ordered_tiles`
        (design_new.md:958 direction interpretation)."""
        dispatcher.network = simple_linear_network
        self._rebuild_geometry(simple_linear_network)
        self._seed_train_in_block(
            simple_linear_network,
            dispatcher,
            head_tile=(2, 0),
            occupied_tiles=[(1, 0), (2, 0)],
            block_id="BLOCK_B",
        )

        result = dispatcher.create_path(
            train_id="T_SAME",
            requested_destination_block_id="BLOCK_B",
            requested_destination_direction="reverse",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert result.success, (
            f"reverse same-block create_path must succeed; got "
            f"failure_kind={result.failure_kind!r}, reason={result.reason!r}"
        )
        assert len(result.resource_sequence) == 1
        entry = result.resource_sequence[0]
        assert entry.resource_id == "BLOCK_B"
        # Reverse traversal exits WEST and enters from EAST.
        assert entry.exit_edge == "W", (
            f"reverse exit edge must be W; got {entry.exit_edge!r}"
        )
        assert entry.entry_edge == "E"
        # Tiles in decreasing (reversed) order.
        assert [(t.x, t.y) for t in entry.path_tiles] == [(2, 0), (1, 0)]

    def test_same_tile_no_cab_is_rejected(self, network, dispatcher):
        """When the local movement stays inside a SINGLE tile and no cab is
        selected, dispatcher must reject with the documented reason rather
        than guessing the frame (design_new.md:961)."""
        # Single-tile block A at (0,0).
        network.add_track(TrackSegment(x=0, y=0, track_bits=TRACK_EW))
        network.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_EW))
        network.create_block("BLOCK_A", "Block A")
        network.get_track(0, 0).block_section_id = "BLOCK_A"
        network.create_block("BLOCK_B", "Block B")
        network.get_track(1, 0).block_section_id = "BLOCK_B"
        self._rebuild_geometry(network)

        # Train occupies only the single-tile BLOCK_A; cab=None (no cab).
        self._seed_train_in_block(
            network,
            dispatcher,
            head_tile=(0, 0),
            occupied_tiles=[(0, 0)],
            block_id="BLOCK_A",
            cab=None,
        )

        result = dispatcher.create_path(
            train_id="T_SAME",
            requested_destination_block_id="BLOCK_A",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert not result.success, (
            "same-tile synthetic movement with no selected cab must be "
            "rejected per design_new.md:961"
        )
        assert "cab" in (result.reason or "").lower(), (
            f"rejection reason must name the missing cab; got "
            f"{result.reason!r}"
        )

    def test_same_tile_cab_disagrees_is_rejected(self, network, dispatcher):
        """Single-tile move with a selected cab whose direction disagrees
        with `requested_destination_direction` must be rejected
        (design_new.md:961)."""
        network.add_track(TrackSegment(x=0, y=0, track_bits=TRACK_EW))
        network.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_EW))
        network.create_block("BLOCK_A", "Block A")
        network.get_track(0, 0).block_section_id = "BLOCK_A"
        network.create_block("BLOCK_B", "Block B")
        network.get_track(1, 0).block_section_id = "BLOCK_B"
        self._rebuild_geometry(network)

        _, controller = self._seed_train_in_block(
            network,
            dispatcher,
            head_tile=(0, 0),
            occupied_tiles=[(0, 0)],
            block_id="BLOCK_A",
            facing=Direction.EAST,
            cab="A",
        )
        # The cab's block-relative direction is fixed by the block geometry
        # (single-tile `ordered_tiles` anchor) — request the OPPOSITE so the
        # cab and requested direction disagree, whatever the anchor resolved to.
        disagreeing = self._opposite_of_cab_block_direction(controller, network)

        result = dispatcher.create_path(
            train_id="T_SAME",
            requested_destination_block_id="BLOCK_A",
            requested_destination_direction=disagreeing,
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert not result.success, (
            "same-tile move whose cab direction disagrees with the "
            "requested direction must be rejected per design_new.md:961"
        )

    def test_same_tile_cab_agrees_succeeds(self, network, dispatcher):
        """Single-tile move with a selected cab whose direction agrees with
        the requested direction builds a synthetic one-tile authority."""
        network.add_track(TrackSegment(x=0, y=0, track_bits=TRACK_EW))
        network.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_EW))
        network.create_block("BLOCK_A", "Block A")
        network.get_track(0, 0).block_section_id = "BLOCK_A"
        network.create_block("BLOCK_B", "Block B")
        network.get_track(1, 0).block_section_id = "BLOCK_B"
        self._rebuild_geometry(network)

        _, controller = self._seed_train_in_block(
            network,
            dispatcher,
            head_tile=(0, 0),
            occupied_tiles=[(0, 0)],
            block_id="BLOCK_A",
            facing=Direction.EAST,
            cab="A",
        )
        # Request exactly the cab's block-relative direction so they agree.
        agreeing = self._cab_block_direction(controller, network)

        result = dispatcher.create_path(
            train_id="T_SAME",
            requested_destination_block_id="BLOCK_A",
            requested_destination_direction=agreeing,
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert result.success, (
            f"same-tile move with an agreeing cab ({agreeing!r}) must "
            f"succeed; got failure_kind={result.failure_kind!r}, "
            f"reason={result.reason!r}"
        )
        assert len(result.resource_sequence) == 1
        assert result.resource_sequence[0].resource_id == "BLOCK_A"
        assert result.authority_end_block_id == "BLOCK_A"

    def test_normal_different_block_still_works(
        self, simple_linear_network, dispatcher,
    ):
        """The same-block branch must NOT disturb the ordinary different-block
        path build: a train in BLOCK_A routing to BLOCK_C still walks the
        network normally."""
        dispatcher.network = simple_linear_network
        self._rebuild_geometry(simple_linear_network)
        self._seed_train_in_block(
            simple_linear_network,
            dispatcher,
            head_tile=(0, 0),
            occupied_tiles=[(0, 0)],
            block_id="BLOCK_A",
        )

        result = dispatcher.create_path(
            train_id="T_SAME",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )

        assert result.success, (
            f"ordinary different-block create_path must still work; got "
            f"failure_kind={result.failure_kind!r}, reason={result.reason!r}"
        )
        assert result.authority_end_block_id == "BLOCK_C"
        block_ids = [
            e.resource_id for e in result.resource_sequence
            if e.resource_type == "block"
        ]
        assert block_ids[-1] == "BLOCK_C"
        # More than one block resource -> this went through the normal walk,
        # not the single-block synthetic branch.
        assert len(block_ids) >= 2




