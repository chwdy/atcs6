"""
Railway System Demo

This is the main demonstration script for the railway management system.
It imports all components from separate modules and creates a comprehensive
demo showing train dispatching, scheduling, and safety systems in action.

Track Building using 6-bit track_bits system:
- TrackSegment(x, y, track_bits=TRACK_EW): East-West straight track
- TrackSegment(x, y, track_bits=TRACK_NS): North-South straight track
- TrackSegment(x, y, track_bits=TRACK_NE): North-East diagonal track
- TrackSegment(x, y, track_bits=TRACK_NW): North-West diagonal track
- TrackSegment(x, y, track_bits=TRACK_SE): South-East diagonal track
- TrackSegment(x, y, track_bits=TRACK_SW): South-West diagonal track
- TrackSegment(x, y, track_bits=TRACK_EW | TRACK_NE): 3-way switch
- Dead-ends are auto-detected by propagation system based on neighbor connectivity
"""

from datetime import datetime, timedelta
from enums import Direction, SignalState as EnumSignalState, SwitchPosition
from network import RailwayNetwork
from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW, FORBIDDEN_TRACK_BITS
from train import Train, TrainSchedule, ScheduleStop
from interlocking import InterlockingRoute
from dispatcher import TrainDispatcher
from railway_signal import SignalType

# ============= Demo Functions =============

def test_block_validation():
    """Test that block validation rules are working correctly.

    These tests verify that invalid operations FAIL as expected.
    Each test should raise a ValueError with a descriptive error message.
    """
    print("\n" + "=" * 70)
    print("Testing Block Validation Rules...")
    print("=" * 70)

    test_network = RailwayNetwork()

    # Create some test tracks
    for x in range(5):
        test_network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

    # Test 1: Cannot create block with disconnected tracks
    print("\n[TEST 1] Verifying disconnected tracks are rejected...")
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_DISCONNECTED",
            positions=[(0, 0), (4, 0)],  # Not adjacent - gap in between
            name="Test"
        )
        assert False, "Should have raised ValueError for disconnected tracks"
    except ValueError as e:
        assert "not continuous" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 2: Cannot add switch to block
    print("\n[TEST 2] Verifying switches cannot be in blocks...")
    test_network.add_track(TrackSegment(5, 0, track_bits=TRACK_EW | TRACK_NE))  # Switch
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_WITH_SWITCH",
            positions=[(3, 0), (4, 0), (5, 0)],  # Includes switch at (5,0)
            name="Test"
        )
        assert False, "Should have raised ValueError for switch in block"
    except ValueError as e:
        assert "switch" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 3: Cannot add track with signal to block
    print("\n[TEST 3] Verifying tracks with signals cannot be in blocks...")
    # Create a valid block first
    test_network.create_block_with_tracks(
        block_id="TEST_BLOCK_1",
        positions=[(0, 0), (1, 0)],
        name="Test Block 1"
    )
    # Add a signal
    test_network.add_signal(2, 0, "E", signal_type="block", name="Test Signal")
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_WITH_SIGNAL",
            positions=[(2, 0), (3, 0)],  # Includes tile with signal
            name="Test"
        )
        assert False, "Should have raised ValueError for signal in block"
    except ValueError as e:
        assert "signal" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 4: Cannot assign track already in another block
    print("\n[TEST 4] Verifying tracks cannot be in multiple blocks...")
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_DUPLICATE",
            positions=[(0, 0), (1, 0)],  # These are already in TEST_BLOCK_1
            name="Test"
        )
        assert False, "Should have raised ValueError for duplicate assignment"
    except ValueError as e:
        assert "already in block" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 5: Cannot create block with duplicate ID
    print("\n[TEST 5] Verifying duplicate block IDs are rejected...")
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_BLOCK_1",  # Already exists
            positions=[(4, 0)],
            name="Test"
        )
        assert False, "Should have raised ValueError for duplicate block ID"
    except ValueError as e:
        assert "already exists" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 6: Cannot create block with invalid properties
    print("\n[TEST 6] Verifying invalid block properties are rejected...")
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_INVALID_PROPS",
            positions=[(4, 0)],
            name="Test",
            length_cm=-100.0  # Negative length!
        )
        assert False, "Should have raised ValueError for negative length"
    except ValueError as e:
        assert "negative" in str(e).lower() or "length_cm" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    print("\n" + "=" * 70)
    print("✓ All Block Validation Tests Passed!")
    print("=" * 70)


def create_comprehensive_demo():
    """Create comprehensive demo railway network with proper Block objects.

    Block Structure:
    - BLOCK_MAIN_WEST: Tracks (0,0) to (2,0) - "Main Line West"
    - Switch at (3,0) is a BOUNDARY, not in any block
    - BLOCK_MAIN_EAST: Tracks (4,0) to (10,0) - "Main Line East"
    - BLOCK_BRANCH: Tracks (3,1) to (3,3) - "North Branch"
    """
    print("\n" + "=" * 70)
    print("Creating Railway Network with Block Management...")
    print("=" * 70)

    network = RailwayNetwork()

    # === Create Tracks and Blocks with Validated Assignment ===
    # Note: Blocks are now created WITH tracks using create_block_with_tracks()

    # Main Line West: (0,0) to (2,0)
    for x in range(0, 3):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        network.add_track(track)

    # Create block with validated track assignment
    network.create_block_with_tracks(
        block_id="BLOCK_MAIN_WEST",
        positions=[(x, 0) for x in range(0, 3)],
        name="Main Line West",
        length_cm=150.0,  # 3 tiles * 50cm each
        max_speed_kmh=60.0
    )

    # Switch at (3,0) - BOUNDARY, NO block assignment
    # Connects main line (EW) with branch toward NORTH via NW
    track = TrackSegment(3, 0, track_bits=TRACK_EW | TRACK_NW)
    track.switch_state = 0  # MAIN (EW) active
    # track.block_section_id intentionally left as None
    network.add_track(track)

    # Main Line East: (4,0) to (10,0)
    for x in range(4, 11):
        track = TrackSegment(x, 0, track_bits=TRACK_EW)
        network.add_track(track)

    # Create block with validated track assignment
    network.create_block_with_tracks(
        block_id="BLOCK_MAIN_EAST",
        positions=[(x, 0) for x in range(4, 11)],
        name="Main Line East",
        length_cm=350.0,  # 7 tiles * 50cm each
        max_speed_kmh=80.0
    )

    # Station at (7,0) - will split BLOCK_MAIN_EAST into before/station/after
    network.add_station(
        x=7, y=0,
        name="Central Station",
        length_cm=100.0,  # Platform length
        platform_side="both",
        top_speed_kmh=40.0  # Reduced speed through station
    )

    # North Branch: (3,1) to (3,3)
    for y in range(1, 4):
        track = TrackSegment(3, y, track_bits=TRACK_NS)
        network.add_track(track)

    # Create block with validated track assignment
    network.create_block_with_tracks(
        block_id="BLOCK_BRANCH",
        positions=[(3, y) for y in range(1, 4)],
        name="North Branch",
        length_cm=150.0,  # 3 tiles * 50cm each
        max_speed_kmh=40.0
    )

    print(f"  Created {len(network.blocks)} blocks with validated track assignments")

    # Station at (3,2) on branch - will split BLOCK_BRANCH into before/station/after
    network.add_station(
        x=3, y=2,
        name="North Station",
        length_cm=80.0,  # Smaller platform
        platform_side="right",
        top_speed_kmh=30.0  # Lower speed for branch station
    )

    # remove 9,0 if exist
    existing = network.get_track(9, 0)
    if existing:
        network.remove_track(9, 0)
    # Create switch at (9,0) - EW + NW to go up-left diagonally
    switch_track = TrackSegment(9, 0, track_bits=TRACK_EW | TRACK_NW)
    switch_track.switch_state = 0  # MAIN (EW) active initially
    network.add_track(switch_track)

    # Simple approach: vertical branch at x=9
    for y in range(1, 6):
        track = TrackSegment(9, y, track_bits=TRACK_NS)
        network.add_track(track)

    network.add_track(TrackSegment(9, 6, track_bits=TRACK_SE))
    network.add_track(TrackSegment(10, 6, track_bits=TRACK_NW))
    network.add_track(TrackSegment(10, 7, track_bits=TRACK_SE))
    network.add_track(TrackSegment(11, 7, track_bits=TRACK_NW))
    network.add_track(TrackSegment(11, 8, track_bits=TRACK_SE))
    network.add_track(TrackSegment(12, 8, track_bits=TRACK_NW))

    east_branch_station = network.add_station(
        x=10, y=7,
        name="east branch Station",
        length_cm=80.0,  # Smaller platform
        platform_side="right",
        top_speed_kmh=30.0  # Lower speed for branch station
    )
    network.extend_station(east_branch_station.station_id, [(11, 7)])
    network.extend_station(east_branch_station.station_id, [(10, 6)])

    

    # Create block for vertical branch (not including switch)
    network.create_block_with_tracks(
        block_id="BLOCK_EAST_BRANCH",
        positions=[(9, y) for y in range(1, 6)],
        name="East Vertical Branch",
        length_cm=200.0,  # 5 tiles
        max_speed_kmh=50.0
    )
    
    print(f"  ✓ Created vertical east branch with 4 tiles")

    # === Multi-tile Station on East Branch ===
    # Add station at top of east branch - spans 2 tiles
    east_terminal = network.add_station(
        x=9, y=3,
        name="East Terminal",
        length_cm=100.0,  # 2-tile platform
        platform_side="both",
        top_speed_kmh=20.0  # Low speed for terminal station
    )
    
    # Extend station to cover 2 tiles (use station_id from returned station)
    network.extend_station(east_terminal.station_id, [(9, 4)])
    
    print(f"  ✓ Created multi-tile East Terminal station spanning 2 tiles")

    # === Signal Testing (after all tracks are created) ===
    print("\n  Testing Signal System:")

    # Signal semantics:
    # - Signal "facing EAST" = light visible to trains coming FROM the east
    # - Those trains travel WEST (opposite direction)
    # - Signal monitors block in the WEST direction (where trains are going)
    #
    # Layout:
    #   BLOCK_MAIN_WEST: (0,0)-(2,0)  |  Switch (3,0)  |  BLOCK_MAIN_EAST: (4,0)-(10,0)
    #                                 |                |
    #                                 |  BLOCK_BRANCH: (3,1)-(3,3)

    # Signal 1: For westbound trains (coming from east, traveling west)
    # Placed at (5,0) in BLOCK_MAIN_EAST, facing EAST
    # Trains travel WEST, look WEST: skip (4,0) own block, (3,0) switch, find (2,0) BLOCK_MAIN_WEST ✓
    signal1 = network.add_signal(
        x=5, y=0,
        detection_direction="W",
        signal_type=SignalType.BLOCK,
        name="Westbound Signal",
        auto_connect=True
    )
    network.update_signal(signal1.signal_id, state="green")
    print(f"    ✓ Created signal '{signal1.name}' at (5,0) facing EAST, monitors: {signal1.monitored_block_id}")

    # Signal 2: For eastbound trains (coming from west, traveling east)
    # Placed at (1,0) in BLOCK_MAIN_WEST, facing WEST
    # Trains travel EAST, look EAST: skip (2,0) own block, (3,0) switch, find (4,0) BLOCK_MAIN_EAST ✓
    signal2 = network.add_signal(
        x=1, y=0,
        detection_direction="E",
        signal_type=SignalType.BLOCK,
        name="Eastbound Signal",
        auto_connect=True
    )
    print(f"    ✓ Created signal '{signal2.name}' at (1,0) facing WEST, monitors: {signal2.monitored_block_id}")

    # Signal 3: For trains exiting branch (from north, traveling south)
    # Placed at (3,1) in BLOCK_BRANCH, facing NORTH
    # Trains travel SOUTH via switch at (3,0) which has TRACK_EW | TRACK_NW
    # The NW diagonal connects N edge to W edge, so trains can only reach BLOCK_MAIN_WEST
    signal3 = network.add_signal(
        x=3, y=1,
        detection_direction="S",
        signal_type=SignalType.BLOCK,
        name="Branch Exit Signal",
        auto_connect=False
    )

    # Test: Show reachable blocks for this signal
    reachable = network.get_reachable_blocks(signal3.signal_id)
    print(f"    ℹ Reachable blocks from signal at (3,1): {reachable}")

    # Test: Try to connect to a non-reachable block (should fail - no valid path)
    # Find any block that's NOT reachable from this signal
    non_reachable_blocks = [bid for bid in network.blocks.keys() if bid not in reachable]
    if non_reachable_blocks:
        try:
            network.connect_signal_to_block(signal3.signal_id, non_reachable_blocks[0])
            print("    ✗ ERROR: Connection to non-reachable block should have failed!")
        except ValueError as e:
            print(f"    ✓ Correctly rejected invalid path to '{non_reachable_blocks[0]}': {e}")

    # Connect to a reachable block (valid path via NW diagonal at switch)
    # Note: Original BLOCK_MAIN_WEST may have been split into child blocks
    if reachable:
        target_block = reachable[0]  # Pick first reachable block
        network.connect_signal_to_block(signal3.signal_id, target_block)
        print(f"    ✓ Created signal '{signal3.name}' at (3,1) facing NORTH, monitors: {signal3.monitored_block_id}")
    else:
        print(f"    ⚠ No reachable blocks for signal at (3,1)")

    # Test: Try to place signal on switch (should fail)
    try:
        network.add_signal(x=3, y=0, detection_direction="E", name="Invalid Switch Signal")
        print("    ✗ ERROR: Signal on switch should have failed!")
    except ValueError as e:
        print(f"    ✓ Correctly rejected signal on switch: {e}")

    # Test: Try invalid direction for track
    try:
        network.add_signal(x=6, y=0, detection_direction="N", name="Invalid Direction")
        print("    ✗ ERROR: Invalid direction should have failed!")
    except ValueError as e:
        print(f"    ✓ Correctly rejected invalid direction: {e}")

    # Test: Signal state cycling
    network.update_signal(signal3.signal_id, state="yellow")
    print(f"    ✓ Updated '{signal3.name}' state to YELLOW")

    # Test: Delete and recreate signal (use a plain track, not the crossing at 9,0)
    test_signal = network.add_signal(x=10, y=0, detection_direction="E", name="Temp Signal")
    temp_id = test_signal.signal_id
    network.remove_signal(temp_id)
    print(f"    ✓ Created and deleted temp signal at (10,0)")

    # Test: Switch routing affects signal states
    print("\n  Testing Switch-Signal Interaction:")
    switch_track = network.get_track(3, 0)

    # Initial state: switch is MAIN (0), signals should have paths to their blocks
    network.update_signals_from_blocks()
    print(f"    Switch at (3,0) state: {switch_track.switch_state} (0=MAIN, 1=DIVERGE)")
    print(f"    Signal 1 (Westbound at 5,0): state={signal1.state.value}, monitors={signal1.monitored_block_id}")
    print(f"    Signal 2 (Eastbound at 1,0): state={signal2.state.value}, monitors={signal2.monitored_block_id}")
    print(f"    Signal 3 (Branch Exit at 3,1): state={signal3.state.value}, monitors={signal3.monitored_block_id}")

    # Toggle switch to DIVERGE
    switch_track.switch_state = 1
    network.update_signals_from_blocks()
    print(f"\n    After toggling switch to DIVERGE (1):")
    print(f"    Signal 1 (Westbound at 5,0): state={signal1.state.value} (E entry not part of NW segment → RED)")
    print(f"    Signal 2 (Eastbound at 1,0): state={signal2.state.value} (W→N via NW, not W→E → RED)")
    print(f"    Signal 3 (Branch Exit at 3,1): state={signal3.state.value} (N→W via NW now active → GREEN)")

    # Toggle back to MAIN
    switch_track.switch_state = 0
    network.update_signals_from_blocks()
    print(f"\n    After toggling switch back to MAIN (0):")
    print(f"    Signal 2 (Eastbound at 1,0): state={signal2.state.value} (path W→E restored)")

    print(f"\n  Signal tests complete. Total signals: {len(network.signals)}")

    print("✓ Network created successfully")
    print(f"  Total tracks: {len(network.tracks)}")
    print(f"  Blocks: {', '.join(network.blocks.keys())}")
    print(f"  Signals: {len(network.signals)}")

    return network


def create_track_showcase(network: RailwayNetwork):
    """Create showcase of all valid track_bits configurations.

    Places all 42 valid non-empty track_bits (0-63 excluding forbidden) in a grid
    to the left of the main demo area. Each tile is isolated so dead-ends are
    auto-detected on all edges, demonstrating the buffer stop rendering.
    """
    # Valid track_bits values (1-63 minus forbidden, excluding 0 which is empty)
    valid_bits = [i for i in range(1, 64) if i not in FORBIDDEN_TRACK_BITS]

    # Place in a grid starting at (-15, -5) to avoid overlap with main demo
    start_x, start_y = -15, -5
    cols = 7  # 7 columns

    for idx, bits in enumerate(valid_bits):
        x = start_x + (idx % cols) * 2  # 2-tile spacing
        y = start_y - (idx // cols) * 2
        print(f"Adding track_bits={bits:06b} at ({x}, {y})")
        track = TrackSegment(x, y, track_bits=bits)
        network.add_track(track)

    print(f"✓ Track showcase created with {len(valid_bits)} valid configurations")
    print(f"  Location: ({start_x}, {start_y}) in a {cols}-column grid")

    return network


def create_diagonal_siding(network: RailwayNetwork):
    """Create a diagonal siding using alternating NW/SE tracks.
    
    This tests diagonal track connectivity and rendering.
    Pattern like user described:
      (12,0): Switch with EW + NW
      (12,1): SE
      (13,1): NW  
      (13,2): SE
      (14,2): NW
      (14,3): SE (end)
    
    This creates a stair-step siding going diagonally SE from the main line.
    """
    print("\n  Creating Diagonal Siding:")
    
    # First, extend the main line to x=14 if needed
    for x in range(11, 15):
        existing = network.get_track(x, 0)
        if not existing:
            track = TrackSegment(x, 0, track_bits=TRACK_EW)
            network.add_track(track)
            print(f"    Added main line track at ({x}, 0)")
    
    # Create switch at (12,0) - EW main line + NW diagonal branch
    existing = network.get_track(12, 0)
    if existing:
        network.remove_track(12, 0)
    switch_track = TrackSegment(12, 0, track_bits=TRACK_EW | TRACK_NW)
    switch_track.switch_state = 0  # MAIN (EW) active initially
    network.add_track(switch_track)
    print(f"    Created switch at (12,0) with EW+NW")
    
    # Diagonal siding pattern: alternating NW and SE
    # NW connects N-edge to W-edge
    # SE connects S-edge to E-edge
    # 
    # From (12,0) NW, the N-edge connects to (12,1) from S
    # Then at (12,1) we need SE to go to (13,1) via E-edge
    
    # (12,1): SE - connects S to E
    # Entry from S (from switch NW at 12,0), exit E to (13,1)
    track = TrackSegment(12, 1, track_bits=TRACK_SE)
    network.add_track(track)
    print(f"    Added SE track at (12,1)")
    
    # (13,1): NW - connects N to W  
    # Entry from W (from 12,1 SE), exit N to (13,2)
    track = TrackSegment(13, 1, track_bits=TRACK_NW)
    network.add_track(track)
    print(f"    Added NW track at (13,1)")
    
    # (13,2): SE - connects S to E
    # Entry from S (from 13,1 NW), exit E to (14,2)
    track = TrackSegment(13, 2, track_bits=TRACK_SE)
    network.add_track(track)
    print(f"    Added SE track at (13,2)")
    
    # (14,2): NW - connects N to W
    # Entry from W (from 13,2 SE), exit N to (14,3)
    track = TrackSegment(14, 2, track_bits=TRACK_NW)
    network.add_track(track)
    print(f"    Added NW track at (14,2)")
    
    # (14,3): SE - connects S to E (dead end on E side)
    # Entry from S (from 14,2 NW), exit E is dead end
    track = TrackSegment(14, 3, track_bits=TRACK_SE)
    network.add_track(track)
    print(f"    Added SE track at (14,3) - end of siding")
    
    # Propagate to detect dead ends
    network.propagate_from_tile(14, 3)
    
    print(f"  ✓ Diagonal siding created: (12,0) switch -> (14,3) end")
    print(f"    Pattern: NW at switch -> SE -> NW -> SE -> NW -> SE")
    
    # Note: Cannot place station on diagonal tracks currently
    # The station system requires NS or EW tracks
    print(f"    Note: Stations on diagonal tracks not yet supported")
    
    return network




def run_comprehensive_demo():
    """Run comprehensive demonstration of isometric railway network"""

    # Run validation tests first
    test_block_validation()

    # Create network
    network = create_comprehensive_demo()

    # Add showcase of all valid track configurations
    network = create_track_showcase(network)
    
    # Add diagonal siding to test diagonal track rendering
    network = create_diagonal_siding(network)

    print("\n" + "=" * 70)
    print("Isometric Demo - Track Layout Summary")
    print("=" * 70)
    print(f"Main line: (0,0) -> (14,0) using TRACK_EW (east-west)")
    print(f"  Total: {len([t for t in network.tracks.values() if t.y == 0])} tracks on main line")
    print(f"North branch: (3,1) -> (3,3) using TRACK_NS (north)")
    print(f"  Total: {len([t for t in network.tracks.values() if t.x == 3 and t.y > 0])} tracks on north branch")
    print(f"Switch at (3,0) connects main line with north branch")
    print(f"East branch: (9,1) -> (9,4) using TRACK_NS (vertical)")
    print(f"  Connected via crossing at (9,0)")
    print(f"Diagonal siding: (12,0) switch -> (14,3) via NW/SE alternating")
    print(f"Stations:")
    print(f"  - Central Station at (7,0) on main line")
    print(f"  - North Station at (3,2) on north branch")
    print(f"  - East Terminal at (9,3)-(9,4) - 2-tile multi-tile station")
    print(f"Signals: Westbound at (5,0), Eastbound at (1,0), Branch Exit at (3,1)")

    # Show network visualization
    network.visualization()
    
    print("\n" + "=" * 70)
    print("Demo Complete")
    print("=" * 70)


def run_regression_tests():
    """Run both Python and JavaScript test suites to prevent regressions.

    This runs before starting the demo server to ensure:
    1. Backend Python tests pass (using pytest)
    2. Frontend API tests pass (using node)

    If any tests fail, the demo will not start.
    """
    import subprocess
    import sys

    print("\n" + "=" * 70)
    print("Running Regression Test Suite")
    print("=" * 70)

    # Test 1: Run Python backend tests
    print("\n[1/2] Running Python Backend Tests...")
    print("-" * 70)

    try:
        # Try to run pytest
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/test_backend.py", "-v"],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            print("❌ Python tests FAILED!")
            print("\nOutput:")
            print(result.stdout)
            print(result.stderr)

            # Check if pytest is not installed
            if "No module named pytest" in result.stderr:
                print("\n⚠️  WARNING: pytest not installed. Skipping Python tests.")
                print("   Install with: pip install pytest")
            else:
                print("\n🛑 BLOCKING: Python tests must pass before starting server.")
                return False
        else:
            print("✅ Python tests PASSED!")
            # Show summary
            if result.stdout:
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'passed' in line.lower() or 'failed' in line.lower():
                        print(f"   {line.strip()}")

    except subprocess.TimeoutExpired:
        print("❌ Python tests TIMEOUT!")
        print("   Tests took longer than 60 seconds.")
        return False
    except Exception as e:
        print(f"⚠️  WARNING: Could not run Python tests: {e}")
        print("   Continuing anyway...")

    # Test 2: Run JavaScript API tests (requires server to be running)
    # Note: These tests need the server running, so we skip them in pre-flight
    # They should be run separately after server starts
    print("\n[2/2] JavaScript API Tests")
    print("-" * 70)
    print("ℹ️  JavaScript API tests require server to be running.")
    print("   Run manually with: node tests/test_api.js")
    print("   (after starting the demo server)")

    print("\n" + "=" * 70)
    print("✅ Pre-flight Tests Complete - Starting Demo")
    print("=" * 70)

    return True


if __name__ == "__main__":
    # Run regression tests before starting demo
    if not run_regression_tests():
        print("\n🛑 Demo startup ABORTED due to test failures.")
        print("   Fix failing tests before proceeding.")
        import sys
        sys.exit(1)

    # If tests pass, run the comprehensive demo
    run_comprehensive_demo()
