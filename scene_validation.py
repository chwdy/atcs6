"""Scene-independent block-validation gate.

The server refuses to boot unless these checks pass (see
`server.run_startup_tests`). They build their own throwaway
`RailwayNetwork`, so they say nothing about the active scene — they assert
that the network's *validation rules* still reject the invalid operations
they are supposed to reject.

This used to be copy-pasted into every scene module as
`test_block_validation()`, which meant a scene baked by `json_to_scene.py`
(demo7, demo7_1) had no copy at all and the boot gate died with
`module 'demo7' has no attribute 'test_block_validation'`. There is one copy
now and the gate calls it directly.
"""

from network import RailwayNetwork
from switch import Switch
from track import Tile, TRACK_EW, TRACK_NE


def validate_block_rules():
    """Check that block validation rules are working correctly.

    These tests verify that invalid operations FAIL as expected.
    Each test should raise a ValueError with a descriptive error message.
    """
    print("\n" + "=" * 70)
    print("Testing Block Validation Rules...")
    print("=" * 70)

    test_network = RailwayNetwork()

    # Create some test tracks (0,0) to (7,0) - extra tracks at (6,0) and (7,0) for Test 5/6
    for x in range(8):
        test_network.add_track(Tile(x, 0, track_bits=TRACK_EW))

    # Test 1: Cannot create block with disconnected tracks
    print("\n[TEST 1] Verifying disconnected tracks are rejected...")
    try:
        test_network.create_segment_with_tracks(
            segment_id="TEST_DISCONNECTED",
            positions=[(0, 0), (4, 0)],  # Not adjacent - gap in between
            name="Test",
        )
        assert False, "Should have raised ValueError for disconnected tracks"
    except ValueError as e:
        assert "not continuous" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 2: Cannot add switch to block
    print("\n[TEST 2] Verifying switches cannot be in blocks...")
    switch_track = Tile(5, 0, track_bits=TRACK_EW | TRACK_NE)  # Switch
    test_network.add_track(switch_track)
    # Create switch entity
    switch = Switch(switch_id="SW_5_0", x=5, y=0)
    test_network.add_switch(switch)
    switch_track.switch_id = switch.switch_id
    try:
        test_network.create_segment_with_tracks(
            segment_id="TEST_WITH_SWITCH",
            positions=[(3, 0), (4, 0), (5, 0)],  # Includes switch at (5,0)
            name="Test",
        )
        assert False, "Should have raised ValueError for switch in block"
    except ValueError as e:
        assert "switch" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 3: Signals can be in blocks, but cannot monitor own block from middle
    print(
        "\n[TEST 3] Verifying signals in blocks can only monitor own block from entry tile..."
    )

    # First create TEST_BLOCK_1 at (0,0), (1,0) which is also used by Test 4 and Test 5
    test_network.create_segment_with_tracks(
        segment_id="TEST_BLOCK_1", positions=[(0, 0), (1, 0)], name="Test Block 1"
    )

    # Create a 3-tile block for testing
    test_network.create_segment_with_tracks(
        segment_id="TEST_BLOCK_SIGNAL",
        positions=[(2, 0), (3, 0), (4, 0)],
        name="Test Block for Signal",
    )

    # Add a signal in the MIDDLE of the block (at tile 3,0)
    # This signal CAN exist in the block, but CANNOT monitor TEST_BLOCK_SIGNAL
    middle_signal = test_network.add_signal(
        3, 0, "E", signal_type="block", name="Middle Signal", auto_connect=False
    )

    # Verify the track still has its block_id
    middle_track = test_network.get_track(3, 0)
    assert middle_track.block_id == "TEST_BLOCK_SIGNAL", (
        "Signal tile should remain in block"
    )
    print(
        f"  ✓ Signal placed in middle of block, tile remains in block: {middle_track.block_id}"
    )

    # Try to make the signal monitor its own block (should fail - not on entry tile)
    try:
        test_network.connect_signal_to_block(
            middle_signal.signal_id, "TEST_BLOCK_SIGNAL"
        )
        assert False, (
            "Should have raised ValueError for monitoring own block from middle"
        )
    except ValueError as e:
        assert "entry tile" in str(e).lower() or "middle" in str(e).lower(), (
            f"Wrong error: {e}"
        )
        print(f"  ✓ Correctly rejected monitoring own block from middle: {e}")

    # Add a signal at the ENTRY tile (tile 2,0 for eastbound)
    # Eastbound trains approach from west, so (2,0) is the entry tile
    entry_signal = test_network.add_signal(
        2, 0, "E", signal_type="block", name="Entry Signal", auto_connect=False
    )

    # This signal CAN monitor its own block (TEST_BLOCK_SIGNAL) because it's on entry tile
    reachable = test_network.get_reachable_blocks(entry_signal.signal_id)
    assert "TEST_BLOCK_SIGNAL" in reachable, (
        f"Entry tile signal should be able to monitor own block, got: {reachable}"
    )
    print(f"  ✓ Entry tile signal can monitor own block, reachable: {reachable}")

    # Test 4: Cannot assign track already in another block
    print("\n[TEST 4] Verifying tracks cannot be in multiple blocks...")
    try:
        test_network.create_segment_with_tracks(
            segment_id="TEST_DUPLICATE",
            positions=[(0, 0), (1, 0)],  # These are already in TEST_BLOCK_1
            name="Test",
        )
        assert False, "Should have raised ValueError for duplicate assignment"
    except ValueError as e:
        assert "already in block" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 5: Cannot create block with duplicate ID
    print("\n[TEST 5] Verifying duplicate block IDs are rejected...")
    try:
        test_network.create_segment_with_tracks(
            segment_id="TEST_BLOCK_1",  # Already exists
            positions=[(6, 0)],  # Use position not in any block
            name="Test",
        )
        assert False, "Should have raised ValueError for duplicate block ID"
    except ValueError as e:
        assert "already exists" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    # Test 6: Cannot create block with invalid properties
    print("\n[TEST 6] Verifying invalid block properties are rejected...")
    try:
        test_network.create_segment_with_tracks(
            segment_id="TEST_INVALID_PROPS",
            positions=[(7, 0)],  # Use position not in any block
            name="Test",
            length_cm=-100.0,  # Negative length!
        )
        assert False, "Should have raised ValueError for negative length"
    except ValueError as e:
        assert "negative" in str(e).lower() or "length_cm" in str(e).lower(), (
            f"Wrong error message: {e}"
        )
        print(f"  ✓ Correctly rejected: {e}")

    print("\n" + "=" * 70)
    print("✓ All Block Validation Tests Passed!")
    print("=" * 70)
