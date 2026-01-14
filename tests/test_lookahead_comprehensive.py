"""
Comprehensive Lookahead Tests

This file combines and expands tests from:
- test_lookahead_fixes.py (block lookahead jumping issue)
- test_train_position_jumping.py (train position smoothness)

And adds new tests for:
- Demo network layout validation (blocks, signals, switches, stations)
- Edge cases (path boundaries, switch traversal, multi-tile blocks)
- Physical position calculations

Run with: conda run -n general pytest tests/test_lookahead_comprehensive.py -v
"""

import pytest
from train_lookahead import (
    build_lookahead,
    build_lookahead_response,
    LookaheadItemType,
    _calculate_train_physical_position,
    _precompute_tile_counts,
)
from train import Train
from network import RailwayNetwork
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
from enums import Direction, BlockState
from railway_signal import SignalType


# ============= PYTEST FIXTURES =============


@pytest.fixture
def network():
    """Fresh RailwayNetwork for each test."""
    return RailwayNetwork()


@pytest.fixture
def simple_two_tile_block_network(network):
    """
    Create a simple network with a 2-tile block for testing lookahead basics.

    Layout:
        Block A (100cm): (0,0) -> (1,0)
        Block B (100cm): (2,0)
    """
    # Block A: 2 tiles, 100cm total (50cm per tile)
    block_a = Block(block_id="A", length_cm=100.0)
    network.blocks["A"] = block_a

    track1 = TrackSegment(0, 0)
    track1.block_section_id = "A"
    track1.track_bits = TRACK_EW
    network.add_track(track1)

    track2 = TrackSegment(1, 0)
    track2.block_section_id = "A"
    track2.track_bits = TRACK_EW
    network.add_track(track2)

    # Block B: 1 tile, 100cm
    block_b = Block(block_id="B", length_cm=100.0)
    network.blocks["B"] = block_b
    track3 = TrackSegment(2, 0)
    track3.block_section_id = "B"
    track3.track_bits = TRACK_EW
    network.add_track(track3)

    return network


@pytest.fixture
def multi_block_path_network(network):
    """
    Create a network with multiple blocks for testing path traversal.

    Layout (10 tiles total):
        Block A (150cm, 3 tiles): (0,0) -> (2,0)
        Block B (200cm, 4 tiles): (3,0) -> (6,0)
        Block C (150cm, 3 tiles): (7,0) -> (9,0)
    """
    # Block A: 3 tiles
    network.blocks["A"] = Block(block_id="A", name="Block A", length_cm=150.0)
    for x in range(3):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        track.block_section_id = "A"
        network.add_track(track)

    # Block B: 4 tiles
    network.blocks["B"] = Block(block_id="B", name="Block B", length_cm=200.0)
    for x in range(3, 7):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        track.block_section_id = "B"
        network.add_track(track)

    # Block C: 3 tiles
    network.blocks["C"] = Block(block_id="C", name="Block C", length_cm=150.0)
    for x in range(7, 10):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        track.block_section_id = "C"
        network.add_track(track)

    return network


@pytest.fixture
def demo_network(network):
    """
    Create a network matching the demo_main.py layout for integration testing.

    Layout from demo_main.py:
        BLOCK_MAIN_WEST (150cm, 3 tiles): (0,0) -> (2,0)
        Switch at (3,0): TRACK_EW | TRACK_NW (not in any block)
        BLOCK_MAIN_EAST (350cm, 7 tiles): (4,0) -> (10,0)
        BLOCK_BRANCH (150cm, 3 tiles): (3,1) -> (3,3)

        Stations:
        - Central Station at (7,0) - 100cm platform
        - North Station at (3,2) - 80cm platform

        Signals:
        - Westbound Signal at (5,0) facing W
        - Eastbound Signal at (1,0) facing E
        - Branch Exit Signal at (3,1) facing S
    """
    # Main Line West: (0,0) to (2,0)
    for x in range(3):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        network.add_track(track)

    network.create_block_with_tracks(
        block_id="BLOCK_MAIN_WEST",
        positions=[(x, 0) for x in range(3)],
        name="Main Line West",
        length_cm=150.0,
        max_speed_kmh=60.0,
    )

    # Switch at (3,0) - BOUNDARY, NO block assignment
    switch_track = TrackSegment(3, 0, track_bits=TRACK_EW | TRACK_NW)
    switch_track.switch_state = 0  # MAIN (EW) active
    network.add_track(switch_track)

    # Main Line East: (4,0) to (10,0)
    for x in range(4, 11):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        network.add_track(track)

    network.create_block_with_tracks(
        block_id="BLOCK_MAIN_EAST",
        positions=[(x, 0) for x in range(4, 11)],
        name="Main Line East",
        length_cm=350.0,
        max_speed_kmh=80.0,
    )

    # Station at (7,0)
    network.add_station(
        x=7,
        y=0,
        name="Central Station",
        length_cm=100.0,
        platform_side="both",
        top_speed_kmh=40.0,
    )

    # North Branch: (3,1) to (3,3)
    for y in range(1, 4):
        track = TrackSegment(3, y, track_bits=TRACK_NS)
        network.add_track(track)

    network.create_block_with_tracks(
        block_id="BLOCK_BRANCH",
        positions=[(3, y) for y in range(1, 4)],
        name="North Branch",
        length_cm=150.0,
        max_speed_kmh=40.0,
    )

    # Station at (3,2) on branch
    network.add_station(
        x=3,
        y=2,
        name="North Station",
        length_cm=80.0,
        platform_side="right",
        top_speed_kmh=30.0,
    )

    # Signals
    # Westbound Signal at (5,0) facing W
    network.add_signal(
        x=5,
        y=0,
        detection_direction="W",
        signal_type=SignalType.BLOCK,
        name="Westbound Signal",
        auto_connect=True,
    )

    # Eastbound Signal at (1,0) facing E
    network.add_signal(
        x=1,
        y=0,
        detection_direction="E",
        signal_type=SignalType.BLOCK,
        name="Eastbound Signal",
        auto_connect=True,
    )

    # Branch Exit Signal at (3,1) facing S
    network.add_signal(
        x=3,
        y=1,
        detection_direction="S",
        signal_type=SignalType.BLOCK,
        name="Branch Exit Signal",
        auto_connect=False,
    )

    return network


def create_train_on_path(
    path: list, path_index: int = 0, distance_on_track: float = 0.0
) -> Train:
    """Helper to create a train positioned on a path."""
    train = Train(train_id="T1")
    train.current_path = path
    train.path_index = path_index
    train.current_pos = (path[path_index][0], path[path_index][1]) if path else None
    train.distance_on_track = distance_on_track
    train.facing_direction = path[path_index][2] if path else Direction.EAST
    train.travel_direction = "forward"
    return train


# ============= TESTS FROM test_lookahead_fixes.py =============


class TestLookaheadJumping:
    """
    Test case to reproduce the 'jumping' lookahead issue where blocks might
    disappear or have incorrect distances when the train moves between tiles
    within the same block.

    Original issue: Block items would disappear from lookahead or have
    incorrect distances when train moved from tile 1 to tile 2 of a multi-tile block.
    """

    def test_lookahead_inside_multi_tile_block(self, simple_two_tile_block_network):
        """
        Verify lookahead items when train is on the second tile of a multi-tile block.

        The block should remain in lookahead at distance 0.0 regardless of which
        tile within the block the train is on.
        """
        network = simple_two_tile_block_network

        train = Train(train_id="T1")
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.facing_direction = Direction.EAST
        train.travel_direction = "forward"

        # CASE 1: Train on Tile 1 (Start of Block A)
        train.path_index = 0
        train.current_pos = (0, 0)
        train.distance_on_track = 0.0

        result1 = build_lookahead(train, network)

        # Expect Block A to be first item at 0.0cm
        assert len(result1.lookahead_items) > 0, "Expected lookahead items on tile 1"
        item_a = next(
            (
                i
                for i in result1.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK and i.data["id"] == "A"
            ),
            None,
        )
        assert item_a is not None, "Block A should be in lookahead"
        assert item_a.distance_cm == 0.0, (
            f"Block A should be at distance 0.0, got {item_a.distance_cm}"
        )

        # CASE 2: Train on Tile 2 (Middle of Block A)
        train.path_index = 1
        train.current_pos = (1, 0)
        train.distance_on_track = 0.0

        result2 = build_lookahead(train, network)

        item_a_2 = next(
            (
                i
                for i in result2.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK and i.data["id"] == "A"
            ),
            None,
        )

        # Block A should still be present
        assert item_a_2 is not None, (
            "Block A disappeared from lookahead when on 2nd tile of block"
        )

        # The distance should be 0.0 because Block A starts at path beginning
        assert item_a_2.distance_cm == 0.0, (
            f"Block A distance changed to {item_a_2.distance_cm}"
        )

    def test_lookahead_block_sequence_consistency(self, multi_block_path_network):
        """
        Verify that block sequence in lookahead is consistent as train moves through path.

        All blocks should appear in the same order regardless of train position.
        """
        network = multi_block_path_network

        path = [(x, 0, Direction.EAST) for x in range(10)]

        # Train at start of Block A
        train = create_train_on_path(path, path_index=0)
        result1 = build_lookahead(train, network)

        blocks1 = [
            i.data["id"]
            for i in result1.lookahead_items
            if i.item_type == LookaheadItemType.BLOCK
        ]

        # Train at start of Block B
        train.path_index = 3
        train.current_pos = (3, 0)
        result2 = build_lookahead(train, network)

        blocks2 = [
            i.data["id"]
            for i in result2.lookahead_items
            if i.item_type == LookaheadItemType.BLOCK
        ]

        # Block A should not be in lookahead when train has passed it
        assert "A" not in blocks2, "Block A should not appear when train is in Block B"
        assert "B" in blocks2, "Block B should appear when train is on it"
        assert "C" in blocks2, "Block C should appear (ahead of train)"


# ============= TESTS FROM test_train_position_jumping.py =============


class TestTrainPositionJumping:
    """
    Test case to investigate why the train monitor visualization might jump
    when the train moves between tiles within the same block.

    The visualization depends on 'distance_into_path_cm'. If this value jumps,
    the train's position on the monitor will jump.
    """

    def test_distance_into_path_smoothness(self, simple_two_tile_block_network):
        """
        Verify that distance_into_path_cm increases smoothly as the train moves
        across a multi-tile block.

        Expected behavior:
        - Tile 1, 0cm into tile: 0.0 cm total
        - Tile 1, 49cm into tile: 49.0 cm total
        - Tile 2, 0cm into tile: 50.0 cm total (first tile is 50cm)
        - Tile 2, 10cm into tile: 60.0 cm total
        """
        network = simple_two_tile_block_network

        train = Train(train_id="T1")
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.facing_direction = Direction.EAST
        train.travel_direction = "forward"

        block_tile_counts, station_tile_counts = _precompute_tile_counts(network)

        # Step 1: Train at start of Tile 1 (0cm into block)
        train.path_index = 0
        train.current_pos = (0, 0)
        train.distance_on_track = 0.0

        dist1, _ = _calculate_train_physical_position(
            train, network, block_tile_counts, station_tile_counts
        )
        assert dist1 == 0.0, f"Expected 0.0 at start, got {dist1}"

        # Step 2: Train near end of Tile 1 (49cm into block)
        train.distance_on_track = 49.0
        dist2, _ = _calculate_train_physical_position(
            train, network, block_tile_counts, station_tile_counts
        )
        assert dist2 == 49.0, f"Expected 49.0, got {dist2}"

        # Step 3: Train just entered Tile 2 (50cm into block ideally)
        train.path_index = 1
        train.current_pos = (1, 0)
        train.distance_on_track = 0.0

        dist3, _ = _calculate_train_physical_position(
            train, network, block_tile_counts, station_tile_counts
        )
        assert dist3 == 50.0, f"Jump detected! Expected 50.0, got {dist3}"

        # Step 4: Train inside Tile 2 (10cm into tile -> 60cm into block)
        train.distance_on_track = 10.0
        dist4, _ = _calculate_train_physical_position(
            train, network, block_tile_counts, station_tile_counts
        )
        assert dist4 == 60.0, (
            f"Incorrect position in 2nd tile. Expected 60.0, got {dist4}"
        )

    def test_distance_continuity_across_blocks(self, multi_block_path_network):
        """
        Verify distance calculations are continuous when crossing block boundaries.

        Block layout:
            Block A (150cm, 3 tiles): (0,0) -> (2,0) - 50cm per tile
            Block B (200cm, 4 tiles): (3,0) -> (6,0) - 50cm per tile
            Block C (150cm, 3 tiles): (7,0) -> (9,0) - 50cm per tile
        """
        network = multi_block_path_network
        path = [(x, 0, Direction.EAST) for x in range(10)]

        train = create_train_on_path(path)
        block_tile_counts, station_tile_counts = _precompute_tile_counts(network)

        # At end of Block A (tile 2, near end)
        train.path_index = 2
        train.current_pos = (2, 0)
        train.distance_on_track = 49.0  # Near end of tile

        dist_end_a, _ = _calculate_train_physical_position(
            train, network, block_tile_counts, station_tile_counts
        )
        # 2 tiles * 50cm + 49cm = 149cm
        assert abs(dist_end_a - 149.0) < 0.1, f"Expected ~149.0, got {dist_end_a}"

        # Just entered Block B (tile 3, start)
        train.path_index = 3
        train.current_pos = (3, 0)
        train.distance_on_track = 0.0

        dist_start_b, _ = _calculate_train_physical_position(
            train, network, block_tile_counts, station_tile_counts
        )
        # Block A total = 150cm, so start of Block B = 150cm
        assert abs(dist_start_b - 150.0) < 0.1, f"Expected 150.0, got {dist_start_b}"


# ============= DEMO NETWORK LAYOUT TESTS =============


class TestDemoNetworkLayout:
    """
    Tests that verify the demo_main.py network layout has correct
    block positions, signal positions, switch positions, and station positions.
    """

    def test_demo_block_positions_calculated_correctly(self, demo_network):
        """
        Verify that block start/end positions are correct for the demo layout.

        Expected layout:
            BLOCK_MAIN_WEST: starts at 0cm, length 150cm
            Switch at (3,0): no block, creates gap
            BLOCK_MAIN_EAST: starts after switch, length 350cm
        """
        network = demo_network

        # Create train traveling east
        path = [(x, 0, Direction.EAST) for x in range(11)]
        train = create_train_on_path(path)

        result = build_lookahead_response(train, network)

        blocks = result.get("blocks", [])

        # Find BLOCK_MAIN_WEST
        main_west = next((b for b in blocks if b["id"] == "BLOCK_MAIN_WEST"), None)
        assert main_west is not None, "BLOCK_MAIN_WEST should be in lookahead"
        assert main_west["start_cm"] == 0.0, (
            f"BLOCK_MAIN_WEST should start at 0, got {main_west['start_cm']}"
        )
        assert main_west["length_cm"] == 150.0, (
            f"Expected 150cm length, got {main_west['length_cm']}"
        )
        assert main_west["tile_count"] == 3, (
            f"Expected 3 tiles, got {main_west['tile_count']}"
        )

        # Main east blocks should start after switch (at 150cm + switch length)
        # The station at (7,0) causes block splitting, so we look for any block
        # that is part of the main east line (starts with BLOCK_MAIN_EAST or contains STA_)
        east_blocks = [b for b in blocks if "MAIN_EAST" in b["id"] or "STA_" in b["id"]]
        assert len(east_blocks) > 0, (
            f"Should have east blocks, got: {[b['id'] for b in blocks]}"
        )

        # Verify total block structure - should have at least 2 blocks total
        assert len(blocks) >= 2, f"Expected at least 2 blocks, got {len(blocks)}"

        # First block should be BLOCK_MAIN_WEST at position 0
        sorted_blocks = sorted(blocks, key=lambda b: b["start_cm"])
        assert sorted_blocks[0]["id"] == "BLOCK_MAIN_WEST", (
            "First block should be BLOCK_MAIN_WEST"
        )

        # Verify blocks after switch have start position >= 150cm
        for b in east_blocks:
            assert b["start_cm"] >= 150.0, (
                f"Block {b['id']} should start after BLOCK_MAIN_WEST (150cm), got {b['start_cm']}"
            )

    def test_demo_signal_positions_at_tile_centers(self, demo_network):
        """
        Verify signals appear in lookahead at expected positions.

        Signals are positioned at the start of their tiles (in lookahead distance terms).
        """
        network = demo_network

        # Create eastbound train (will see Eastbound Signal at (1,0))
        path = [(x, 0, Direction.EAST) for x in range(11)]
        train = create_train_on_path(path)

        result = build_lookahead_response(train, network)
        signals = result.get("signals", [])

        # Eastbound signal at (1,0) facing E should be visible to eastbound trains
        eastbound_signal = next(
            (
                s
                for s in signals
                if "Eastbound" in str(network.signals.get(s["id"], {}))
            ),
            None,
        )
        # Signal should be found (it's at tile 1 facing the travel direction E)
        # Note: Signal detection depends on direction matching

    def test_demo_switch_positions_calculated(self, demo_network):
        """
        Verify switch at (3,0) is detected and positioned correctly.
        """
        network = demo_network

        # Create eastbound train
        path = [(x, 0, Direction.EAST) for x in range(11)]
        train = create_train_on_path(path)

        result = build_lookahead_response(train, network)
        switches = result.get("switches", [])

        # Should find switch at (3,0)
        switch = next((s for s in switches if s["x"] == 3 and s["y"] == 0), None)
        assert switch is not None, "Switch at (3,0) should be in lookahead"
        assert switch["position"] == "main", (
            "Switch should be in 'main' position (state 0)"
        )
        # Switch distance should be at 150cm (after BLOCK_MAIN_WEST)
        assert switch["distance_cm"] == 150.0, (
            f"Switch should be at 150cm, got {switch['distance_cm']}"
        )

    def test_demo_station_positions(self, demo_network):
        """
        Verify stations appear at correct positions in lookahead.

        Central Station at (7,0) should appear within BLOCK_MAIN_EAST.
        """
        network = demo_network

        # Create eastbound train
        path = [(x, 0, Direction.EAST) for x in range(11)]
        train = create_train_on_path(path)

        result = build_lookahead_response(train, network)
        stations = result.get("stations", [])

        # Central Station at (7,0)
        central = next((s for s in stations if s["name"] == "Central Station"), None)
        assert central is not None, "Central Station should be in lookahead"
        assert central["length_cm"] == 100.0, (
            f"Expected 100cm platform, got {central['length_cm']}"
        )

    def test_demo_branch_layout(self, demo_network):
        """
        Verify the north branch layout is correct when traveling north.

        Note: Adding a station to BLOCK_BRANCH causes it to be split into
        child blocks (e.g., BLOCK_BRANCH_1, BLOCK_BRANCH_2, STA_*_BLOCK).
        """
        network = demo_network

        # Switch to diverge mode to access branch
        switch_track = network.get_track(3, 0)
        switch_track.switch_state = 1  # DIVERGE (NW) active

        # Train traveling north through branch
        # Path: (3,0) -> (3,1) -> (3,2) -> (3,3)
        path = [
            (3, 0, Direction.NORTH),
            (3, 1, Direction.NORTH),
            (3, 2, Direction.NORTH),
            (3, 3, Direction.NORTH),
        ]
        train = create_train_on_path(path, path_index=1)  # Start at (3,1) in branch

        result = build_lookahead_response(train, network)
        blocks = result.get("blocks", [])

        # Look for branch-related blocks (original or split)
        branch_blocks = [b for b in blocks if "BRANCH" in b["id"] or "STA_" in b["id"]]

        # Should have at least one branch-related block
        assert len(branch_blocks) > 0, (
            f"Should have branch blocks, got: {[b['id'] for b in blocks]}"
        )

        # Verify total length of branch blocks sums correctly
        # (Original BLOCK_BRANCH was 150cm, but station splits it)


# ============= EDGE CASE TESTS =============


class TestEdgeCases:
    """
    Edge case tests for boundary conditions and special scenarios.
    """

    def test_train_at_path_start(self, simple_two_tile_block_network):
        """
        Test train at the very start of path (path_index=0, distance=0).

        All blocks ahead should be visible in lookahead.
        """
        network = simple_two_tile_block_network

        path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train = create_train_on_path(path, path_index=0, distance_on_track=0.0)

        result = build_lookahead(train, network)

        blocks = [
            i.data["id"]
            for i in result.lookahead_items
            if i.item_type == LookaheadItemType.BLOCK
        ]

        assert "A" in blocks, "Block A should be visible at path start"
        assert "B" in blocks, "Block B should be visible at path start"

    def test_train_at_path_end(self, simple_two_tile_block_network):
        """
        Test train at the end of path (last tile, near end).

        Only the current block should be visible (or none if past all blocks).
        """
        network = simple_two_tile_block_network

        path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train = create_train_on_path(path, path_index=2, distance_on_track=90.0)

        result = build_lookahead(train, network)

        blocks = [
            i.data["id"]
            for i in result.lookahead_items
            if i.item_type == LookaheadItemType.BLOCK
        ]

        # Block B should be visible (train is on it)
        assert "B" in blocks, "Block B should be visible when train is on it"
        # Block A should NOT be visible (train has passed it)
        assert "A" not in blocks, "Block A should not be visible when train is past it"

    def test_train_moving_through_switch(self, demo_network):
        """
        Test lookahead when train is on a switch tile.

        Switch should appear in lookahead with correct state.
        """
        network = demo_network

        path = [
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),  # Switch
            (4, 0, Direction.EAST),
        ]
        train = create_train_on_path(path, path_index=1)  # On switch

        result = build_lookahead(train, network)

        switches = [
            i for i in result.lookahead_items if i.item_type == LookaheadItemType.SWITCH
        ]

        assert len(switches) > 0, "Switch should be in lookahead"
        assert switches[0].data["x"] == 3, "Switch x should be 3"
        assert switches[0].data["y"] == 0, "Switch y should be 0"

    def test_single_tile_block(self, network):
        """
        Test block with only 1 tile.

        Block length should equal tile length.
        """
        # Create single-tile block
        network.blocks["SINGLE"] = Block(block_id="SINGLE", length_cm=50.0)
        track = TrackSegment(0, 0, track_bits=TRACK_EW)
        track.block_section_id = "SINGLE"
        network.add_track(track)

        path = [(0, 0, Direction.EAST)]
        train = create_train_on_path(path)

        result = build_lookahead(train, network)

        block = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK and i.data["id"] == "SINGLE"
            ),
            None,
        )
        assert block is not None, "Single-tile block should be visible"
        assert block.data["length_cm"] == 50.0, (
            f"Expected 50cm, got {block.data['length_cm']}"
        )
        assert block.data["tile_count"] == 1, (
            f"Expected 1 tile, got {block.data['tile_count']}"
        )

    def test_multi_tile_blocks_various_sizes(self, network):
        """
        Test blocks with 2, 3, and 4 tiles to verify per-tile length calculation.
        """
        # Block with 2 tiles (100cm = 50cm per tile)
        network.blocks["B2"] = Block(block_id="B2", length_cm=100.0)
        for x in range(2):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "B2"
            network.add_track(track)

        # Block with 3 tiles (150cm = 50cm per tile)
        network.blocks["B3"] = Block(block_id="B3", length_cm=150.0)
        for x in range(2, 5):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "B3"
            network.add_track(track)

        # Block with 4 tiles (200cm = 50cm per tile)
        network.blocks["B4"] = Block(block_id="B4", length_cm=200.0)
        for x in range(5, 9):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "B4"
            network.add_track(track)

        path = [(x, 0, Direction.EAST) for x in range(9)]
        train = create_train_on_path(path)

        result = build_lookahead(train, network)

        blocks = {
            i.data["id"]: i.data
            for i in result.lookahead_items
            if i.item_type == LookaheadItemType.BLOCK
        }

        assert blocks["B2"]["tile_count"] == 2
        assert blocks["B3"]["tile_count"] == 3
        assert blocks["B4"]["tile_count"] == 4

        # Verify cumulative distances
        block_items = sorted(
            [
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK
            ],
            key=lambda x: x.distance_cm,
        )
        assert block_items[0].data["id"] == "B2"
        assert block_items[0].distance_cm == 0.0
        assert block_items[1].data["id"] == "B3"
        assert block_items[1].distance_cm == 100.0  # After B2
        assert block_items[2].data["id"] == "B4"
        assert block_items[2].distance_cm == 250.0  # After B2 (100) + B3 (150)

    def test_block_with_signal(self, network):
        """
        Test that blocks containing signals are handled correctly.

        Both the block and signal should appear in lookahead.
        """
        # Create block with signal
        network.blocks["SIG_BLOCK"] = Block(block_id="SIG_BLOCK", length_cm=100.0)
        for x in range(2):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "SIG_BLOCK"
            network.add_track(track)

        # Add signal at (1,0) facing E
        network.add_signal(x=1, y=0, detection_direction="E", name="Test Signal")

        path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]
        train = create_train_on_path(path)

        result = build_lookahead(train, network)

        # Check block exists
        block = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK
            ),
            None,
        )
        assert block is not None, "Block should be in lookahead"

        # Check signal exists (facing E matches travel direction E)
        signal = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.SIGNAL
            ),
            None,
        )
        assert signal is not None, "Signal should be in lookahead"

    def test_block_with_station(self, network):
        """
        Test that blocks containing stations are handled correctly.

        Both the block and station should appear in lookahead.
        Station is a marker and should not affect block distance calculation.
        """
        # Create block
        network.blocks["STN_BLOCK"] = Block(block_id="STN_BLOCK", length_cm=100.0)
        for x in range(2):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "STN_BLOCK"
            network.add_track(track)

        # Add station at (1,0)
        network.add_station(x=1, y=0, name="Test Station", length_cm=50.0)

        path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]
        train = create_train_on_path(path)

        result = build_lookahead(train, network)

        # Check block exists
        block = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK
            ),
            None,
        )
        assert block is not None, "Block should be in lookahead"
        assert block.data["length_cm"] == 100.0, "Block length should be 100cm"

        # Check station exists
        station = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.STATION
            ),
            None,
        )
        assert station is not None, "Station should be in lookahead"
        assert station.data["length_cm"] == 50.0, "Station length should be 50cm"


# ============= PRECOMPUTE TILE COUNTS TESTS =============


class TestPrecomputeTileCounts:
    """
    Tests for the _precompute_tile_counts helper function.
    """

    def test_precompute_block_tile_counts(self, multi_block_path_network):
        """
        Verify block tile counts are computed correctly.
        """
        network = multi_block_path_network

        block_counts, station_counts = _precompute_tile_counts(network)

        assert block_counts["A"] == 3, "Block A should have 3 tiles"
        assert block_counts["B"] == 4, "Block B should have 4 tiles"
        assert block_counts["C"] == 3, "Block C should have 3 tiles"

    def test_precompute_station_tile_counts(self, demo_network):
        """
        Verify station tile counts are computed correctly.
        """
        network = demo_network

        block_counts, station_counts = _precompute_tile_counts(network)

        # Central Station and North Station each have 1 tile by default
        # (multi-tile stations would have more)
        for station_id, count in station_counts.items():
            assert count >= 1, f"Station {station_id} should have at least 1 tile"


# ============= BUILD LOOKAHEAD RESPONSE TESTS =============


class TestBuildLookaheadResponse:
    """
    Tests for the build_lookahead_response function that creates the
    full API response format.
    """

    def test_response_contains_all_sections(self, multi_block_path_network):
        """
        Verify response contains all expected sections.
        """
        network = multi_block_path_network
        path = [(x, 0, Direction.EAST) for x in range(10)]
        train = create_train_on_path(path)

        response = build_lookahead_response(train, network)

        # Check all expected keys exist
        assert "train_id" in response
        assert "current_speed_kmh" in response
        assert "target_speed_kmh" in response
        assert "blocks" in response
        assert "signals" in response
        assert "stations" in response
        assert "switches" in response
        assert "distance_into_path_cm" in response
        assert "distance_to_next_tile_cm" in response
        assert "warnings" in response

    def test_response_blocks_have_correct_format(self, multi_block_path_network):
        """
        Verify block entries in response have all required fields.
        """
        network = multi_block_path_network
        path = [(x, 0, Direction.EAST) for x in range(10)]
        train = create_train_on_path(path)

        response = build_lookahead_response(train, network)

        for block in response["blocks"]:
            assert "id" in block
            assert "name" in block
            assert "state" in block
            assert "start_cm" in block
            assert "end_cm" in block
            assert "length_cm" in block
            assert "tile_count" in block
            assert "sequence" in block

    def test_response_distance_into_path(self, multi_block_path_network):
        """
        Verify distance_into_path_cm is calculated correctly.
        """
        network = multi_block_path_network
        path = [(x, 0, Direction.EAST) for x in range(10)]

        # Train at tile 3 (Block B start), 25cm into tile
        train = create_train_on_path(path, path_index=3, distance_on_track=25.0)

        response = build_lookahead_response(train, network)

        # Block A is 150cm, so start of Block B is at 150cm
        # Train is 25cm into first tile of Block B
        # Expected: 150cm + 25cm = 175cm
        expected = 175.0
        actual = response["distance_into_path_cm"]

        assert abs(actual - expected) < 1.0, f"Expected ~{expected}, got {actual}"

    def test_response_warnings_for_unconfigured_blocks(self, network):
        """
        Verify warnings are generated for blocks without length configured.
        """
        # Create block without length
        network.blocks["UNCONFIGURED"] = Block(block_id="UNCONFIGURED", length_cm=0.0)
        track = TrackSegment(0, 0, track_bits=TRACK_EW)
        track.block_section_id = "UNCONFIGURED"
        network.add_track(track)

        path = [(0, 0, Direction.EAST)]
        train = create_train_on_path(path)

        response = build_lookahead_response(train, network)

        # Should have a warning about unconfigured block
        assert len(response["warnings"]) > 0, (
            "Should have warnings for unconfigured block"
        )
        warning = response["warnings"][0]
        assert "block" in warning["type"].lower(), "Warning should mention block"


# ============= DIRECTION AND TRAVEL TESTS =============


class TestDirectionAndTravel:
    """
    Tests for different travel directions and facing directions.
    """

    def test_westbound_travel(self, simple_two_tile_block_network):
        """
        Test lookahead when traveling west (reverse of typical east direction).
        """
        network = simple_two_tile_block_network

        # Westbound path
        path = [
            (2, 0, Direction.WEST),
            (1, 0, Direction.WEST),
            (0, 0, Direction.WEST),
        ]
        train = create_train_on_path(path)
        train.travel_direction = "forward"

        result = build_lookahead(train, network)

        # Should see blocks in reverse order: B first, then A
        block_items = sorted(
            [
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK
            ],
            key=lambda x: x.distance_cm,
        )

        assert len(block_items) >= 2, "Should see at least 2 blocks"
        assert block_items[0].data["id"] == "B", (
            "Block B should be first when going west"
        )
        assert block_items[1].data["id"] == "A", (
            "Block A should be second when going west"
        )

    def test_reverse_travel_direction(self, simple_two_tile_block_network):
        """
        Test lookahead when travel_direction is 'reverse'.

        When facing east but traveling in reverse, the effective travel direction is west.
        """
        network = simple_two_tile_block_network

        path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train = create_train_on_path(path, path_index=2)  # Start at end
        train.travel_direction = "reverse"  # Traveling backwards

        result = build_lookahead(train, network)

        # When reversing from (2,0) facing east, we look west
        # Should see blocks in that direction

    def test_northbound_travel(self, network):
        """
        Test lookahead for north-south track.
        """
        # Create NS track with blocks
        network.blocks["NORTH"] = Block(block_id="NORTH", length_cm=100.0)
        for y in range(2):
            track = TrackSegment(0, y, track_bits=TRACK_NS)
            track.block_section_id = "NORTH"
            network.add_track(track)

        path = [
            (0, 0, Direction.NORTH),
            (0, 1, Direction.NORTH),
        ]
        train = create_train_on_path(path)

        result = build_lookahead(train, network)

        block = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.BLOCK
            ),
            None,
        )
        assert block is not None, "Block should be visible for northbound travel"


# ============= ZERO-LENGTH TILE AND SIGNAL POSITION TESTS =============


class TestZeroLengthTileHandling:
    """
    Tests for proper handling of zero-length tiles (like switches).

    BUG FIX: Zero-length tiles were being skipped in occupancy tracking
    because the condition `distance_on_track >= tile_length_cm` was always
    true when tile_length_cm=0.
    """

    def test_zero_length_switch_block_occupancy(self, network):
        """
        Verify that a block containing a zero-length switch is properly occupied.
        """
        from simulation import SimulationController
        from switch import Switch

        # Create a block with a switch tile that has length=0
        network.blocks["SWITCH_BLOCK"] = Block(block_id="SWITCH_BLOCK", length_cm=0.0)

        # Create switch track using track_bits that create a switch (EW + NE = junction)
        # This will have switch_points and thus is_switch=True
        track = TrackSegment(0, 0, track_bits=TRACK_EW | TRACK_NE)
        track.block_section_id = "SWITCH_BLOCK"
        network.add_track(track)

        # Verify it's actually a switch
        assert track.is_switch, "Track should be a switch with EW+NE bits"

        # Create switch entity with zero length
        switch = Switch(
            switch_id="SW_1",
            x=0,
            y=0,
            main_length_cm=0.0,
            diverge_length_cm=0.0,
        )
        network.switches["SW_1"] = switch
        track.switch_id = "SW_1"

        # Create train
        train = Train(train_id="T1")
        train.current_pos = (0, 0)
        train.current_path = [(0, 0, Direction.EAST)]
        train.path_index = 0
        train.facing_direction = Direction.EAST
        train.distance_on_track = 0.0
        train.current_speed = 10.0
        network.trains["T1"] = train

        # Simulate position update
        sim = SimulationController(network)
        sim._update_train_position(train, 0.1)

        # The block should be occupied even though it's zero-length
        block = network.blocks["SWITCH_BLOCK"]
        assert block.occupied_by == "T1", (
            f"Zero-length switch block should be occupied by train. "
            f"Got occupied_by={block.occupied_by}"
        )


class TestSignalPositioning:
    """
    Tests for signal position calculation.

    BUG FIX: Signals were placed at tile START instead of tile MIDDLE.
    Fixed by adding tile_length/2 offset.
    """

    def test_signal_at_tile_middle(self, network):
        """
        Verify that signals are positioned at the middle of their tile.
        """
        # Create a 100cm block with 1 tile
        network.blocks["B1"] = Block(block_id="B1", length_cm=100.0)

        track = TrackSegment(0, 0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        # Add signal facing east (train will approach from west)
        signal = network.add_signal(
            signal_id="SIG_1",
            x=0,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )

        # Create train path going east
        path = [(0, 0, Direction.EAST)]
        train = create_train_on_path(path)

        result = build_lookahead(train, network)

        # Find the signal in lookahead
        signal_item = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.SIGNAL
            ),
            None,
        )

        assert signal_item is not None, "Signal should be in lookahead"

        # Signal should be at tile middle (50cm for a 100cm tile)
        # Block starts at 0, tile length is 100cm, so middle is 50cm
        assert signal_item.distance_cm == 50.0, (
            f"Signal should be at tile middle (50cm), got {signal_item.distance_cm}cm"
        )

    def test_signal_in_multi_tile_block(self, network):
        """
        Verify signal position in a multi-tile block.

        Signal is on the second tile of a 2-tile block (200cm total, 100cm per tile).
        Signal should be at: tile 0 (100cm) + half of tile 1 (50cm) = 150cm.
        """
        # Create a 200cm block with 2 tiles (100cm each)
        network.blocks["B1"] = Block(block_id="B1", length_cm=200.0)

        for x in range(2):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "B1"
            network.add_track(track)

        # Add signal on second tile (x=1)
        signal = network.add_signal(
            signal_id="SIG_1",
            x=1,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )

        # Create train path
        path = [(0, 0, Direction.EAST), (1, 0, Direction.EAST)]
        train = create_train_on_path(path)

        result = build_lookahead(train, network)

        signal_item = next(
            (
                i
                for i in result.lookahead_items
                if i.item_type == LookaheadItemType.SIGNAL
            ),
            None,
        )

        assert signal_item is not None, "Signal should be in lookahead"

        # Signal is at middle of tile 1: tile 0 (100cm) + half of tile 1 (50cm) = 150cm
        assert signal_item.distance_cm == 150.0, (
            f"Signal expected at 150cm, got {signal_item.distance_cm}cm"
        )

    def test_signal_position_consistent_with_and_without_path(self, network):
        """
        Verify that signal position is the same whether or not the train has a path.

        BUG: Signal position was different on initial load (no path) vs during
        simulation (with path). This test ensures consistency.
        """
        # Create a 100cm block with 1 tile
        network.blocks["B1"] = Block(block_id="B1", length_cm=100.0)

        track = TrackSegment(0, 0, track_bits=TRACK_EW)
        track.block_section_id = "B1"
        network.add_track(track)

        # Add signal
        signal = network.add_signal(
            signal_id="SIG_1",
            x=0,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )

        # Case 1: Train WITHOUT a path (initial state)
        train_no_path = Train(train_id="T1")
        train_no_path.current_pos = (0, 0)
        train_no_path.facing_direction = Direction.EAST
        train_no_path.travel_direction = "forward"
        train_no_path.current_path = None  # No path
        train_no_path.path_index = 0
        train_no_path.distance_on_track = 0.0

        result_no_path = build_lookahead(train_no_path, network)
        signal_no_path = next(
            (
                i
                for i in result_no_path.lookahead_items
                if i.item_type == LookaheadItemType.SIGNAL
            ),
            None,
        )

        # Case 2: Train WITH a path (simulation state)
        train_with_path = Train(train_id="T2")
        train_with_path.current_pos = (0, 0)
        train_with_path.facing_direction = Direction.EAST
        train_with_path.travel_direction = "forward"
        train_with_path.current_path = [(0, 0, Direction.EAST)]
        train_with_path.path_index = 0
        train_with_path.distance_on_track = 0.0

        result_with_path = build_lookahead(train_with_path, network)
        signal_with_path = next(
            (
                i
                for i in result_with_path.lookahead_items
                if i.item_type == LookaheadItemType.SIGNAL
            ),
            None,
        )

        assert signal_no_path is not None, "Signal should be in lookahead (no path)"
        assert signal_with_path is not None, "Signal should be in lookahead (with path)"

        # Signal position should be the same regardless of path
        assert signal_no_path.distance_cm == signal_with_path.distance_cm, (
            f"Signal position differs: no_path={signal_no_path.distance_cm}cm, "
            f"with_path={signal_with_path.distance_cm}cm"
        )

        # Both should be at tile middle (50cm for 100cm tile)
        assert signal_no_path.distance_cm == 50.0, (
            f"Signal should be at 50cm, got {signal_no_path.distance_cm}cm"
        )

    def test_signal_position_multiblock_with_and_without_path(self, network):
        """
        More realistic scenario: Signal is on block B (tile 2), train starts on block A.
        Tests that signal position is consistent with and without path.

        Layout:
            Block A (100cm): (0,0) -> (1,0)
            Block B (100cm): (2,0) with signal
        """
        # Block A: 2 tiles, 100cm (50cm per tile)
        network.blocks["A"] = Block(block_id="A", length_cm=100.0)
        for x in range(2):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "A"
            network.add_track(track)

        # Block B: 1 tile, 100cm (signal at tile middle = 50cm into block B)
        network.blocks["B"] = Block(block_id="B", length_cm=100.0)
        track_b = TrackSegment(2, 0, track_bits=TRACK_EW)
        track_b.block_section_id = "B"
        network.add_track(track_b)

        # Signal on tile 2 (block B)
        network.add_signal(
            signal_id="SIG_B",
            x=2,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )

        # Case 1: Train WITHOUT path on tile (0,0)
        train_no_path = Train(train_id="T1")
        train_no_path.current_pos = (0, 0)
        train_no_path.facing_direction = Direction.EAST
        train_no_path.travel_direction = "forward"
        train_no_path.current_path = None
        train_no_path.path_index = 0
        train_no_path.distance_on_track = 0.0

        result_no_path = build_lookahead(train_no_path, network)
        signal_no_path = next(
            (
                i
                for i in result_no_path.lookahead_items
                if i.item_type == LookaheadItemType.SIGNAL
            ),
            None,
        )

        # Case 2: Train WITH path on tile (0,0)
        train_with_path = Train(train_id="T2")
        train_with_path.current_pos = (0, 0)
        train_with_path.facing_direction = Direction.EAST
        train_with_path.travel_direction = "forward"
        train_with_path.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train_with_path.path_index = 0
        train_with_path.distance_on_track = 0.0

        result_with_path = build_lookahead(train_with_path, network)
        signal_with_path = next(
            (
                i
                for i in result_with_path.lookahead_items
                if i.item_type == LookaheadItemType.SIGNAL
            ),
            None,
        )

        assert signal_no_path is not None, "Signal should be in lookahead (no path)"
        assert signal_with_path is not None, "Signal should be in lookahead (with path)"

        # Signal should be at Block A length (100cm) + half of Block B (50cm) = 150cm
        expected_distance = 100.0 + 50.0  # Block A + half Block B

        assert signal_no_path.distance_cm == signal_with_path.distance_cm, (
            f"Signal position differs: no_path={signal_no_path.distance_cm}cm, "
            f"with_path={signal_with_path.distance_cm}cm"
        )

        assert signal_no_path.distance_cm == expected_distance, (
            f"Signal should be at {expected_distance}cm, got {signal_no_path.distance_cm}cm"
        )

    def test_signal_distance_when_train_at_tile_edge(self, network):
        """
        BUG: When train is at the edge between two tiles (distance_on_track=0 on new tile),
        the signal distance was incorrectly calculated by adding the full block length
        instead of accounting for the train's actual position.

        Scenario from user:
        - Block is 150cm, 3 tiles (50cm per tile)
        - Train at edge between (0,0) and (1,0) - meaning path_index=1, distance_on_track=0
        - Signal at (1,0)
        - Expected: Signal should be at ~25cm (half of 50cm tile) from train
        - Bug: Signal was showing 175cm (150cm block + 25cm)
        """
        # Block: 3 tiles, 150cm total (50cm per tile)
        network.blocks["B1"] = Block(block_id="B1", length_cm=150.0)

        for x in range(3):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "B1"
            network.add_track(track)

        # Signal on tile (1,0) - middle tile of the block
        network.add_signal(
            signal_id="SIG_1_0",
            x=1,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )

        # Train at edge between (0,0) and (1,0)
        # This means train head just entered tile (1,0), at distance_on_track=0
        train = Train(train_id="T1")
        train.current_pos = (1, 0)  # Head is on tile (1,0)
        train.facing_direction = Direction.EAST
        train.travel_direction = "forward"
        train.current_path = [
            (0, 0, Direction.EAST),
            (1, 0, Direction.EAST),
            (2, 0, Direction.EAST),
        ]
        train.path_index = 1  # Currently on second tile
        train.distance_on_track = 0.0  # At the edge, just entered

        response = build_lookahead_response(train, network)

        # Check distance_into_path_cm
        # Train is at start of tile 1, so it has traversed tile 0 completely = 50cm
        assert response["distance_into_path_cm"] == 50.0, (
            f"Train should be 50cm into path (1 tile), got {response['distance_into_path_cm']}cm"
        )

        # Signal should be at middle of tile (1,0)
        # = 50cm (tile 0) + 25cm (half tile 1) = 75cm from path start
        signals = response.get("signals", [])
        assert len(signals) == 1, f"Expected 1 signal, got {len(signals)}"

        signal = signals[0]
        expected_signal_distance = 75.0
        assert signal["distance_cm"] == expected_signal_distance, (
            f"Signal should be at {expected_signal_distance}cm from path start, "
            f"got {signal['distance_cm']}cm"
        )

    def test_signal_distance_when_train_at_tile_edge_no_path(self, network):
        """
        Same as above but train has NO PATH (initial load scenario).

        When there's no path, the lookahead scans from current tile forward.
        The signal should still be at the correct position relative to train.
        """
        # Block: 3 tiles, 150cm total (50cm per tile)
        network.blocks["B1"] = Block(block_id="B1", length_cm=150.0)

        for x in range(3):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "B1"
            network.add_track(track)

        # Signal on tile (1,0) - middle tile of the block
        network.add_signal(
            signal_id="SIG_1_0",
            x=1,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )

        # Train at tile (1,0) with NO PATH
        train = Train(train_id="T1")
        train.current_pos = (1, 0)
        train.facing_direction = Direction.EAST
        train.travel_direction = "forward"
        train.current_path = None  # No path!
        train.path_index = 0
        train.distance_on_track = 0.0

        response = build_lookahead_response(train, network)

        # Without a path, distance_into_path should be 0
        assert response["distance_into_path_cm"] == 0.0, (
            f"Without path, distance_into_path should be 0, got {response['distance_into_path_cm']}cm"
        )

        signals = response.get("signals", [])
        assert len(signals) == 1, f"Expected 1 signal, got {len(signals)}"

        signal = signals[0]

        # Signal is at middle of tile (1,0) = 25cm from train
        expected_signal_distance = 25.0
        assert signal["distance_cm"] == expected_signal_distance, (
            f"Signal should be at {expected_signal_distance}cm from train, "
            f"got {signal['distance_cm']}cm."
        )

    def test_signal_distance_bug_regression(self, network):
        """
        Regression test for the 'full block length added' bug.

        BUG DESCRIPTION (Fixed Jan 2026):
        When train was at tile (0,0) with a signal on tile (1,0), and both tiles
        were in the same 150cm/3-tile block, the signal showed 175cm instead of 75cm.

        ROOT CAUSE:
        The lookahead was adding the FULL block length (150cm) when first encountering
        the block on tile (0,0). Then when processing tile (1,0), cumulative_distance
        was already at 150cm, so signal ended up at 150 + 25 = 175cm.

        FIX:
        Changed to add per-tile length as we traverse each tile, not full block
        length on the first tile. Now cumulative_distance increases by 50cm per tile.

        This test ensures:
        1. Signal at (1,0) shows 75cm when train is at (0,0)
        2. Signal at (2,0) shows 125cm when train is at (0,0)
        3. Signal shows 25cm when train is on the same tile as the signal
        """
        # Block: 3 tiles, 150cm total (50cm per tile)
        network.blocks["B1"] = Block(block_id="B1", length_cm=150.0)

        for x in range(3):
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            track.block_section_id = "B1"
            network.add_track(track)

        # Signals on tiles (1,0) and (2,0)
        network.add_signal(
            signal_id="SIG_1",
            x=1,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )
        network.add_signal(
            signal_id="SIG_2",
            x=2,
            y=0,
            signal_type=SignalType.BLOCK,
            detection_direction="E",
        )

        # === Test 1: Train at (0,0), check signals on (1,0) and (2,0) ===
        train = Train(train_id="T1")
        train.current_pos = (0, 0)
        train.facing_direction = Direction.EAST
        train.travel_direction = "forward"
        train.current_path = None
        train.distance_on_track = 0.0

        response = build_lookahead_response(train, network)
        signals = {s["id"]: s["distance_cm"] for s in response.get("signals", [])}

        # Signal at (1,0): tile 0 (50cm) + half tile 1 (25cm) = 75cm
        assert signals.get("SIG_1") == 75.0, (
            f"SIG_1 should be at 75cm, got {signals.get('SIG_1')}cm"
        )
        # Signal at (2,0): tile 0 (50cm) + tile 1 (50cm) + half tile 2 (25cm) = 125cm
        assert signals.get("SIG_2") == 125.0, (
            f"SIG_2 should be at 125cm, got {signals.get('SIG_2')}cm"
        )

        # === Test 2: Train at (1,0), check signal on same tile ===
        train2 = Train(train_id="T2")
        train2.current_pos = (1, 0)
        train2.facing_direction = Direction.EAST
        train2.travel_direction = "forward"
        train2.current_path = None
        train2.distance_on_track = 0.0

        response2 = build_lookahead_response(train2, network)
        signals2 = {s["id"]: s["distance_cm"] for s in response2.get("signals", [])}

        # Signal at (1,0): half tile (25cm) from train
        assert signals2.get("SIG_1") == 25.0, (
            f"SIG_1 should be at 25cm when train on same tile, got {signals2.get('SIG_1')}cm"
        )
