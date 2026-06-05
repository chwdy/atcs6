"""
Demo 6: Baked yard layout (geometry + three yard stations).

This scene bakes the hand-drawn layout from layouts/demo_6.json directly into
Python (same style as demo_4 / demo_5): 474 track tiles and 44 switches form
one connected running network — a north-south mainline, an east corridor, and
west/south running lines that gather into three yards. On top of the geometry
it builds three yard STATIONS (NW Yard, Yard A, Yard B) with 20 platforms
total — each 3 tiles long, sitting at the buffer end of a yard road;
add_station auto-creates one block per platform.

Blocks (54 total): one {platform_id}_BLOCK per platform (20, auto-created by
add_station), one {platform_id}_APPROACH per yard road (19, the tiles between a
platform and its throat switch), and 15 running-line blocks drawn along the
mainline and yard corridors. There are no tunnels, signals, routes, or trains
yet — setup_demo_train() is a no-op; trains and routes get added on top as the
layout is built out.

The editor renders the grid rotated 45 degrees, so the long diagonal tile runs
read as straight horizontal running lines on screen.
"""

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
from path_dispatcher import PathDispatcher
from switch import Switch


# Identifier read by bootstrap.py and by scene-gated tests to recognise this
# scene. Each scene module declares its own ID; tests compare against the
# active scene (see bootstrap.get_current_scene_id) to decide whether to run.
SCENE_ID = "demo_6"


def test_block_validation():
    """Test that block validation rules are working correctly.

    Same checks as demo_main.test_block_validation — the server's startup
    block-validation gate calls this on whichever scene module is active.
    """
    print("\n" + "=" * 70)
    print("Testing Block Validation Rules...")
    print("=" * 70)

    test_network = RailwayNetwork()

    for x in range(8):
        test_network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

    print("\n[TEST 1] Verifying disconnected tracks are rejected...")
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_DISCONNECTED",
            positions=[(0, 0), (4, 0)],
            name="Test",
        )
        assert False, "Should have raised ValueError for disconnected tracks"
    except ValueError as e:
        assert "not continuous" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    print("\n[TEST 2] Verifying switches cannot be in blocks...")
    switch_track = TrackSegment(5, 0, track_bits=TRACK_EW | TRACK_NE)
    test_network.add_track(switch_track)
    switch = Switch(switch_id="SW_5_0", x=5, y=0)
    test_network.add_switch(switch)
    switch_track.switch_id = switch.switch_id
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_WITH_SWITCH",
            positions=[(3, 0), (4, 0), (5, 0)],
            name="Test",
        )
        assert False, "Should have raised ValueError for switch in block"
    except ValueError as e:
        assert "switch" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    print("\n[TEST 3] Verifying duplicate block IDs are rejected...")
    test_network.create_block_with_tracks(
        block_id="TEST_BLOCK_1", positions=[(0, 0), (1, 0)], name="Test Block 1"
    )
    try:
        test_network.create_block_with_tracks(
            block_id="TEST_BLOCK_1",
            positions=[(6, 0)],
            name="Test",
        )
        assert False, "Should have raised ValueError for duplicate block ID"
    except ValueError as e:
        assert "already exists" in str(e).lower(), f"Wrong error message: {e}"
        print(f"  ✓ Correctly rejected: {e}")

    print("\n" + "=" * 70)
    print("✓ Block Validation Tests Passed")
    print("=" * 70)


# Baked track geometry from layouts/demo_6.json. Each entry: (x, y, bits).
# dead_end_ports (buffer stops) are NOT listed here — the network recomputes
# them from connectivity when tracks are added (see
# TrackSegment._update_dead_ends). Switch tiles carry their geometry here and
# get their Switch entity from SWITCHES below.
TILES = [
    (-22, 2, TRACK_NE),
    (-22, 3, TRACK_SW),
    (-21, 1, TRACK_NE),
    (-21, 2, TRACK_SW),
    (-21, 4, TRACK_NE),
    (-20, 0, TRACK_NE),
    (-20, 1, TRACK_EW | TRACK_SW),  # SW_-20_1
    (-20, 3, TRACK_NE),
    (-20, 4, TRACK_SW),
    (-19, -1, TRACK_NE),
    (-19, 0, TRACK_NS | TRACK_SW),  # SW_-19_0
    (-19, 1, TRACK_NS | TRACK_EW),
    (-19, 2, TRACK_NS | TRACK_NE),  # SW_-19_2
    (-19, 3, TRACK_SW),
    (-18, -2, TRACK_NE),
    (-18, -1, TRACK_SW),
    (-18, 1, TRACK_EW | TRACK_NE),  # SW_-18_1
    (-18, 2, TRACK_SW),
    (-17, -3, TRACK_NE),
    (-17, -2, TRACK_SW),
    (-17, 0, TRACK_NE),
    (-17, 1, TRACK_SW),
    (-16, -4, TRACK_NE),
    (-16, -3, TRACK_SW),
    (-16, -1, TRACK_NE),
    (-16, 0, TRACK_SW),
    (-15, -5, TRACK_NE),
    (-15, -4, TRACK_SW),
    (-15, -2, TRACK_NE),
    (-15, -1, TRACK_SW),
    (-14, -6, TRACK_NE),
    (-14, -5, TRACK_SW),
    (-14, -3, TRACK_NE),
    (-14, -2, TRACK_SW),
    (-13, -7, TRACK_NE),
    (-13, -6, TRACK_SW),
    (-13, -4, TRACK_NE),
    (-13, -3, TRACK_SW),
    (-12, -8, TRACK_NE),
    (-12, -7, TRACK_SW),
    (-12, -5, TRACK_NE),
    (-12, -4, TRACK_SW),
    (-11, -9, TRACK_NE),
    (-11, -8, TRACK_SW),
    (-11, -6, TRACK_NE),
    (-11, -5, TRACK_SW),
    (-10, -10, TRACK_NE),
    (-10, -9, TRACK_SW),
    (-10, -7, TRACK_NE),
    (-10, -6, TRACK_SW),
    (-9, -10, TRACK_EW),
    (-9, -8, TRACK_NE),
    (-9, -7, TRACK_SW),
    (-8, -10, TRACK_EW),
    (-8, -8, TRACK_EW),
    (-7, -10, TRACK_EW),
    (-7, -8, TRACK_EW),
    (-6, -10, TRACK_EW | TRACK_NW),  # SW_-6_-10
    (-6, -9, TRACK_SE),
    (-6, -8, TRACK_EW),
    (-5, -27, TRACK_SE),
    (-5, -10, TRACK_EW),
    (-5, -9, TRACK_NW),
    (-5, -8, TRACK_EW | TRACK_SE),  # SW_-5_-8
    (-4, -28, TRACK_SE),
    (-4, -27, TRACK_NW),
    (-4, -26, TRACK_SE),
    (-4, -10, TRACK_EW),
    (-4, -8, TRACK_NW),
    (-4, -7, TRACK_SE),
    (-3, -29, TRACK_SE),
    (-3, -28, TRACK_NW),
    (-3, -27, TRACK_SE),
    (-3, -26, TRACK_NW),
    (-3, -25, TRACK_SE),
    (-3, -10, TRACK_NW),
    (-3, -9, TRACK_SE),
    (-3, -7, TRACK_NW),
    (-3, -6, TRACK_SE),
    (-2, -30, TRACK_SE),
    (-2, -29, TRACK_NW),
    (-2, -28, TRACK_SE),
    (-2, -27, TRACK_NW),
    (-2, -26, TRACK_SE),
    (-2, -25, TRACK_NW),
    (-2, -24, TRACK_SE),
    (-2, -9, TRACK_NW),
    (-2, -8, TRACK_SE),
    (-2, -6, TRACK_NW),
    (-2, -5, TRACK_SE),
    (-1, -31, TRACK_SE),
    (-1, -30, TRACK_NW),
    (-1, -29, TRACK_SE),
    (-1, -28, TRACK_NW),
    (-1, -27, TRACK_SE),
    (-1, -26, TRACK_NW),
    (-1, -25, TRACK_SE),
    (-1, -24, TRACK_EW),
    (-1, -8, TRACK_NW),
    (-1, -7, TRACK_SE),
    (-1, -5, TRACK_NW),
    (-1, -4, TRACK_SE),
    (0, -31, TRACK_NW),
    (0, -30, TRACK_SE),
    (0, -29, TRACK_NW),
    (0, -28, TRACK_SE),
    (0, -27, TRACK_NW),
    (0, -26, TRACK_SE),
    (0, -25, TRACK_NW),
    (0, -24, TRACK_EW | TRACK_SE),  # SW_0_-24
    (0, -7, TRACK_NW),
    (0, -6, TRACK_SE),
    (0, -4, TRACK_NW),
    (0, -3, TRACK_SE),
    (1, -30, TRACK_NW),
    (1, -29, TRACK_SE),
    (1, -28, TRACK_NW),
    (1, -27, TRACK_SE),
    (1, -26, TRACK_NW),
    (1, -25, TRACK_SE),
    (1, -24, TRACK_EW | TRACK_NW),  # SW_1_-24
    (1, -23, TRACK_SE),
    (1, -6, TRACK_NW),
    (1, -5, TRACK_SE),
    (1, -3, TRACK_NW),
    (1, -2, TRACK_SE),
    (2, -29, TRACK_NW),
    (2, -28, TRACK_NS),
    (2, -27, TRACK_NS | TRACK_NW),  # SW_2_-27
    (2, -26, TRACK_NS),
    (2, -25, TRACK_NS | TRACK_NW),  # SW_2_-25
    (2, -24, TRACK_EW | TRACK_SE),  # SW_2_-24
    (2, -23, TRACK_NW),
    (2, -22, TRACK_SE),
    (2, -5, TRACK_NW),
    (2, -4, TRACK_SE),
    (2, -2, TRACK_NW),
    (2, -1, TRACK_SE),
    (3, -24, TRACK_NW),
    (3, -23, TRACK_NS | TRACK_SE),  # SW_3_-23
    (3, -22, TRACK_NS | TRACK_NW),  # SW_3_-22
    (3, -21, TRACK_SE),
    (3, -4, TRACK_NW),
    (3, -3, TRACK_SE),
    (3, -1, TRACK_EW | TRACK_NW),  # SW_3_-1
    (3, 0, TRACK_SE),
    (4, -23, TRACK_NW),
    (4, -22, TRACK_SE),
    (4, -21, TRACK_NW),
    (4, -20, TRACK_SE),
    (4, -3, TRACK_NW),
    (4, -2, TRACK_NS | TRACK_SE),  # SW_4_-2
    (4, -1, TRACK_NS | TRACK_EW),
    (4, 0, TRACK_NS | TRACK_NW),  # SW_4_0
    (4, 1, TRACK_SE),
    (5, -22, TRACK_NW),
    (5, -21, TRACK_SE),
    (5, -20, TRACK_NW),
    (5, -19, TRACK_SE),
    (5, -2, TRACK_NW),
    (5, -1, TRACK_EW | TRACK_SE),  # SW_5_-1
    (5, 1, TRACK_NW),
    (5, 2, TRACK_SE),
    (6, -21, TRACK_NW),
    (6, -20, TRACK_SE),
    (6, -19, TRACK_NW),
    (6, -18, TRACK_SE),
    (6, -1, TRACK_NW),
    (6, 0, TRACK_SE),
    (6, 2, TRACK_NW),
    (6, 3, TRACK_SE),
    (7, -29, TRACK_NS),
    (7, -28, TRACK_NS),
    (7, -27, TRACK_NS),
    (7, -26, TRACK_NS),
    (7, -25, TRACK_NS | TRACK_SE),  # SW_7_-25
    (7, -24, TRACK_NS),
    (7, -23, TRACK_NS),
    (7, -22, TRACK_NS),
    (7, -21, TRACK_NS),
    (7, -20, TRACK_NS | TRACK_NW),  # SW_7_-20
    (7, -19, TRACK_NS | TRACK_SE),  # SW_7_-19
    (7, -18, TRACK_NS | TRACK_NW),  # SW_7_-18
    (7, -17, TRACK_NS),
    (7, -16, TRACK_NS),
    (7, -15, TRACK_NS),
    (7, -14, TRACK_NS),
    (7, -13, TRACK_NS),
    (7, -12, TRACK_NS),
    (7, -11, TRACK_NS),
    (7, -10, TRACK_NS),
    (7, -9, TRACK_NS),
    (7, -8, TRACK_NS),
    (7, -7, TRACK_NS),
    (7, -6, TRACK_NS),
    (7, -5, TRACK_NS),
    (7, -4, TRACK_NS),
    (7, -3, TRACK_NS),
    (7, -2, TRACK_NS),
    (7, 0, TRACK_NW),
    (7, 1, TRACK_SE),
    (7, 3, TRACK_NW),
    (7, 4, TRACK_NS),
    (7, 5, TRACK_SE),
    (8, -25, TRACK_NW),
    (8, -24, TRACK_SE),
    (8, -19, TRACK_NW),
    (8, -18, TRACK_SE),
    (8, 1, TRACK_EW),
    (8, 5, TRACK_NW),
    (8, 6, TRACK_SE),
    (9, -29, TRACK_NS),
    (9, -28, TRACK_NS),
    (9, -27, TRACK_NS),
    (9, -26, TRACK_NS),
    (9, -25, TRACK_NS),
    (9, -24, TRACK_NS | TRACK_NW),  # SW_9_-24
    (9, -23, TRACK_NS),
    (9, -22, TRACK_NS),
    (9, -21, TRACK_NS),
    (9, -20, TRACK_NS),
    (9, -19, TRACK_NS),
    (9, -18, TRACK_NS | TRACK_NW),  # SW_9_-18
    (9, -17, TRACK_NS),
    (9, -16, TRACK_NS),
    (9, -15, TRACK_NS),
    (9, -14, TRACK_NS),
    (9, -13, TRACK_NS),
    (9, -12, TRACK_NS),
    (9, -11, TRACK_NS),
    (9, -10, TRACK_NS),
    (9, -9, TRACK_NS),
    (9, -8, TRACK_NS),
    (9, -7, TRACK_NS),
    (9, -6, TRACK_NS),
    (9, -5, TRACK_NS),
    (9, -4, TRACK_NS),
    (9, -3, TRACK_NS),
    (9, -2, TRACK_NS),
    (9, 1, TRACK_EW),
    (9, 4, TRACK_NW),
    (9, 5, TRACK_SE),
    (9, 6, TRACK_NW),
    (9, 7, TRACK_SE),
    (10, 1, TRACK_EW),
    (10, 3, TRACK_SE),
    (10, 5, TRACK_NW),
    (10, 6, TRACK_SE),
    (10, 7, TRACK_NW),
    (10, 8, TRACK_SE),
    (11, 1, TRACK_NW),
    (11, 2, TRACK_SE),
    (11, 3, TRACK_NW),
    (11, 4, TRACK_SE),
    (11, 6, TRACK_NW),
    (11, 7, TRACK_SE),
    (11, 8, TRACK_NW),
    (11, 9, TRACK_SE),
    (12, 2, TRACK_NW),
    (12, 3, TRACK_SE),
    (12, 4, TRACK_NW),
    (12, 5, TRACK_SE),
    (12, 7, TRACK_NW),
    (12, 8, TRACK_SE),
    (12, 9, TRACK_EW),
    (13, 3, TRACK_NW),
    (13, 4, TRACK_SE),
    (13, 5, TRACK_NW),
    (13, 6, TRACK_SE),
    (13, 8, TRACK_NW),
    (13, 9, TRACK_EW | TRACK_SE),  # SW_13_9
    (14, 4, TRACK_NW),
    (14, 5, TRACK_SE),
    (14, 6, TRACK_NW),
    (14, 7, TRACK_SE),
    (14, 9, TRACK_NW),
    (14, 10, TRACK_SE),
    (15, 5, TRACK_NW),
    (15, 6, TRACK_NS),
    (15, 7, TRACK_NS | TRACK_NW),  # SW_15_7
    (15, 8, TRACK_SE),
    (15, 10, TRACK_EW | TRACK_NW),  # SW_15_10
    (15, 11, TRACK_SE),
    (16, 8, TRACK_NW),
    (16, 9, TRACK_NS | TRACK_SE),  # SW_16_9
    (16, 10, TRACK_NS | TRACK_EW),
    (16, 11, TRACK_NS | TRACK_NW),  # SW_16_11
    (16, 12, TRACK_SE),
    (17, 9, TRACK_NW),
    (17, 10, TRACK_EW | TRACK_SE),  # SW_17_10
    (17, 12, TRACK_NW),
    (17, 13, TRACK_SE),
    (18, 10, TRACK_NW),
    (18, 11, TRACK_SE),
    (18, 13, TRACK_NW),
    (18, 14, TRACK_SE),
    (19, 11, TRACK_NW),
    (19, 12, TRACK_NS | TRACK_SE),  # SW_19_12
    (19, 13, TRACK_NS),
    (19, 14, TRACK_NS | TRACK_NW),  # SW_19_14
    (19, 15, TRACK_SE),
    (20, 12, TRACK_NW),
    (20, 13, TRACK_SE),
    (20, 15, TRACK_EW | TRACK_NW),  # SW_20_15
    (20, 16, TRACK_NS),
    (20, 17, TRACK_NS | TRACK_SE),  # SW_20_17
    (20, 18, TRACK_NS),
    (20, 19, TRACK_NS | TRACK_SE),  # SW_20_19
    (20, 20, TRACK_NS),
    (20, 21, TRACK_NS),
    (20, 22, TRACK_NS | TRACK_SE),  # SW_20_22
    (20, 23, TRACK_NS),
    (20, 24, TRACK_NS | TRACK_SE),  # SW_20_24
    (20, 25, TRACK_NS),
    (20, 26, TRACK_SE),
    (21, 13, TRACK_EW | TRACK_NW),  # SW_21_13
    (21, 14, TRACK_SE),
    (21, 15, TRACK_NW),
    (21, 16, TRACK_SE),
    (21, 17, TRACK_NW),
    (21, 18, TRACK_SE),
    (21, 19, TRACK_NW),
    (21, 20, TRACK_SE),
    (21, 22, TRACK_NW),
    (21, 23, TRACK_SE),
    (21, 24, TRACK_NW),
    (21, 25, TRACK_SE),
    (21, 26, TRACK_NW),
    (21, 27, TRACK_SE),
    (22, 13, TRACK_EW),
    (22, 14, TRACK_NW),
    (22, 15, TRACK_SE),
    (22, 16, TRACK_NW),
    (22, 17, TRACK_SE),
    (22, 18, TRACK_NW),
    (22, 19, TRACK_SE),
    (22, 20, TRACK_NW),
    (22, 21, TRACK_SE),
    (22, 23, TRACK_EW | TRACK_NW),  # SW_22_23
    (22, 24, TRACK_SE),
    (22, 25, TRACK_NW),
    (22, 26, TRACK_SE),
    (22, 27, TRACK_NW),
    (22, 28, TRACK_SE),
    (23, 13, TRACK_EW | TRACK_NW),  # SW_23_13
    (23, 14, TRACK_SE),
    (23, 15, TRACK_NW),
    (23, 16, TRACK_SE),
    (23, 17, TRACK_NW),
    (23, 18, TRACK_SE),
    (23, 19, TRACK_NW),
    (23, 20, TRACK_SE),
    (23, 21, TRACK_NW),
    (23, 22, TRACK_SE),
    (23, 23, TRACK_EW),
    (23, 24, TRACK_NW),
    (23, 25, TRACK_SE),
    (23, 26, TRACK_NW),
    (23, 27, TRACK_SE),
    (23, 28, TRACK_NW),
    (23, 29, TRACK_SE),
    (24, 13, TRACK_EW),
    (24, 14, TRACK_NW),
    (24, 15, TRACK_SE),
    (24, 16, TRACK_NW),
    (24, 17, TRACK_SE),
    (24, 18, TRACK_NW),
    (24, 19, TRACK_SE),
    (24, 20, TRACK_NW),
    (24, 21, TRACK_SE),
    (24, 22, TRACK_NW),
    (24, 23, TRACK_NW),
    (24, 24, TRACK_SE),
    (24, 25, TRACK_NW),
    (24, 26, TRACK_SE),
    (24, 27, TRACK_NW),
    (24, 28, TRACK_SE),
    (25, 13, TRACK_EW | TRACK_NW),  # SW_25_13
    (25, 14, TRACK_SE),
    (25, 15, TRACK_NW),
    (25, 16, TRACK_SE),
    (25, 17, TRACK_NW),
    (25, 18, TRACK_SE),
    (25, 19, TRACK_NW),
    (25, 20, TRACK_SE),
    (25, 21, TRACK_NW),
    (25, 24, TRACK_NW),
    (25, 25, TRACK_SE),
    (25, 26, TRACK_NW),
    (25, 27, TRACK_SE),
    (26, 13, TRACK_EW),
    (26, 14, TRACK_NW),
    (26, 15, TRACK_SE),
    (26, 16, TRACK_NW),
    (26, 17, TRACK_SE),
    (26, 18, TRACK_NW),
    (26, 19, TRACK_SE),
    (26, 20, TRACK_NW),
    (26, 25, TRACK_NW),
    (26, 26, TRACK_SE),
    (27, 13, TRACK_EW),
    (27, 15, TRACK_NW),
    (27, 16, TRACK_SE),
    (27, 17, TRACK_NW),
    (27, 18, TRACK_SE),
    (27, 19, TRACK_NW),
    (28, 13, TRACK_NW),
    (28, 14, TRACK_SE),
    (28, 16, TRACK_NW),
    (28, 17, TRACK_SE),
    (28, 18, TRACK_NW),
    (29, 14, TRACK_NW),
    (29, 15, TRACK_SE),
    (29, 17, TRACK_NW),
    (30, 15, TRACK_NW),
    (30, 16, TRACK_SE),
    (31, 16, TRACK_NW),
    (31, 17, TRACK_SE),
    (32, 17, TRACK_NW),
    (32, 18, TRACK_SE),
    (33, 18, TRACK_NW),
    (33, 19, TRACK_SE),
    (34, 19, TRACK_NW),
    (34, 20, TRACK_SE),
    (35, 20, TRACK_NW),
    (35, 21, TRACK_SE),
    (36, 21, TRACK_EW | TRACK_NW),  # SW_36_21
    (36, 22, TRACK_NS | TRACK_SE),  # SW_36_22
    (36, 23, TRACK_NS | TRACK_SE),  # SW_36_23
    (36, 24, TRACK_NS),
    (36, 25, TRACK_NS),
    (36, 26, TRACK_NS | TRACK_SE),  # SW_36_26
    (36, 27, TRACK_NS),
    (36, 28, TRACK_SE),
    (37, 21, TRACK_EW),
    (37, 22, TRACK_EW),
    (37, 23, TRACK_NW),
    (37, 24, TRACK_SE),
    (37, 26, TRACK_NW),
    (37, 27, TRACK_SE),
    (37, 28, TRACK_NW),
    (37, 29, TRACK_SE),
    (38, 21, TRACK_EW),
    (38, 22, TRACK_NW),
    (38, 23, TRACK_SE),
    (38, 24, TRACK_NW),
    (38, 25, TRACK_SE),
    (38, 27, TRACK_NW),
    (38, 28, TRACK_SE),
    (38, 29, TRACK_NW),
    (38, 30, TRACK_SE),
    (39, 21, TRACK_NW),
    (39, 22, TRACK_SE),
    (39, 23, TRACK_NW),
    (39, 24, TRACK_SE),
    (39, 25, TRACK_NW),
    (39, 26, TRACK_SE),
    (39, 28, TRACK_NW),
    (39, 29, TRACK_SE),
    (40, 22, TRACK_NW),
    (40, 23, TRACK_SE),
    (40, 24, TRACK_NW),
    (40, 25, TRACK_SE),
    (40, 26, TRACK_NW),
    (40, 27, TRACK_SE),
    (41, 23, TRACK_NW),
    (41, 24, TRACK_SE),
    (41, 25, TRACK_NW),
    (41, 26, TRACK_SE),
    (41, 27, TRACK_NW),
    (42, 24, TRACK_NW),
    (42, 25, TRACK_SE),
    (42, 26, TRACK_NW),
    (43, 25, TRACK_NW),
]


# Switch entities (switch_id, x, y) for the switch tiles above. All are
# standard turnouts at default MAIN alignment.
SWITCHES = [
    ("SW_-20_1", -20, 1),
    ("SW_-19_0", -19, 0),
    ("SW_-19_2", -19, 2),
    ("SW_-18_1", -18, 1),
    ("SW_-6_-10", -6, -10),
    ("SW_-5_-8", -5, -8),
    ("SW_0_-24", 0, -24),
    ("SW_1_-24", 1, -24),
    ("SW_2_-27", 2, -27),
    ("SW_2_-25", 2, -25),
    ("SW_2_-24", 2, -24),
    ("SW_3_-23", 3, -23),
    ("SW_3_-22", 3, -22),
    ("SW_3_-1", 3, -1),
    ("SW_4_-2", 4, -2),
    ("SW_4_0", 4, 0),
    ("SW_5_-1", 5, -1),
    ("SW_7_-25", 7, -25),
    ("SW_7_-20", 7, -20),
    ("SW_7_-19", 7, -19),
    ("SW_7_-18", 7, -18),
    ("SW_9_-24", 9, -24),
    ("SW_9_-18", 9, -18),
    ("SW_13_9", 13, 9),
    ("SW_15_7", 15, 7),
    ("SW_15_10", 15, 10),
    ("SW_16_9", 16, 9),
    ("SW_16_11", 16, 11),
    ("SW_17_10", 17, 10),
    ("SW_19_12", 19, 12),
    ("SW_19_14", 19, 14),
    ("SW_20_15", 20, 15),
    ("SW_20_17", 20, 17),
    ("SW_20_19", 20, 19),
    ("SW_20_22", 20, 22),
    ("SW_20_24", 20, 24),
    ("SW_21_13", 21, 13),
    ("SW_22_23", 22, 23),
    ("SW_23_13", 23, 13),
    ("SW_25_13", 25, 13),
    ("SW_36_21", 36, 21),
    ("SW_36_22", 36, 22),
    ("SW_36_23", 36, 23),
    ("SW_36_26", 36, 26),
]


# Blocks created on top of the platform blocks. Each entry:
# (block_id, name, [positions], length_cm, speed_limit_kmh). create_block_with_tracks
# assigns the tiles, validates continuity, and rebuilds the block's ordered_tiles.
BLOCKS = [
    # Yard approach blocks: the road between each platform and its
    # throat switch (30 cm / 45 km/h).
    ("PLT_NWY_1_APPROACH", "NW Yard 1 approach", [(-3, -26), (-3, -25), (-2, -25), (-2, -24), (-1, -24)], 30.0, 45.0),
    ("PLT_NWY_2_APPROACH", "NW Yard 2 approach", [(-2, -27), (-2, -26), (-1, -26), (-1, -25), (0, -25)], 30.0, 45.0),
    ("PLT_NWY_3_APPROACH", "NW Yard 3 approach", [(-1, -28), (-1, -27), (0, -27), (0, -26), (1, -26), (1, -25)], 30.0, 45.0),
    ("PLT_NWY_4_APPROACH", "NW Yard 4 approach", [(0, -29), (0, -28), (1, -28), (1, -27)], 30.0, 45.0),
    ("PLT_NWY_5_APPROACH", "NW Yard 5 approach", [(1, -30), (1, -29), (2, -29), (2, -28)], 30.0, 45.0),
    ("PLT_YA_10_APPROACH", "Yard A 10 (siding) approach", [(20, 25), (20, 26), (21, 26), (21, 27), (22, 27)], 30.0, 45.0),
    ("PLT_YA_1_APPROACH", "Yard A 1 approach", [(21, 19), (21, 20), (22, 20), (22, 21)], 30.0, 45.0),
    ("PLT_YA_2_APPROACH", "Yard A 2 approach", [(21, 17), (21, 18), (22, 18), (22, 19), (23, 19), (23, 20)], 30.0, 45.0),
    ("PLT_YA_3_APPROACH", "Yard A 3 approach", [(21, 15), (21, 16), (22, 16), (22, 17), (23, 17), (23, 18), (24, 18), (24, 19)], 30.0, 45.0),
    ("PLT_YA_4_APPROACH", "Yard A 4 approach", [(21, 14), (22, 14), (22, 15), (23, 15), (23, 16), (24, 16), (24, 17), (25, 17), (25, 18)], 30.0, 45.0),
    ("PLT_YA_5_APPROACH", "Yard A 5 approach", [(23, 14), (24, 14), (24, 15), (25, 15), (25, 16), (26, 16), (26, 17)], 30.0, 45.0),
    ("PLT_YA_6_APPROACH", "Yard A 6 approach", [(25, 14), (26, 14), (26, 15), (27, 15), (27, 16)], 30.0, 45.0),
    ("PLT_YA_7_APPROACH", "Yard A 7 approach", [(22, 24), (23, 24), (23, 25), (24, 25)], 30.0, 45.0),
    ("PLT_YA_8_APPROACH", "Yard A 8 approach", [(23, 23), (24, 23), (24, 24), (25, 24)], 30.0, 45.0),
    ("PLT_YA_9_APPROACH", "Yard A 9 (siding) approach", [(21, 24), (21, 25), (22, 25), (22, 26), (23, 26)], 30.0, 45.0),
    ("PLT_YB_1_APPROACH", "Yard B 1 approach", [(36, 27), (36, 28), (37, 28)], 30.0, 45.0),
    ("PLT_YB_2_APPROACH", "Yard B 2 approach", [(37, 23), (37, 24), (38, 24), (38, 25), (39, 25), (39, 26)], 30.0, 45.0),
    ("PLT_YB_3_APPROACH", "Yard B 3 approach", [(37, 22), (38, 22), (38, 23), (39, 23), (39, 24), (40, 24), (40, 25)], 30.0, 45.0),
    ("PLT_YB_4_APPROACH", "Yard B 4 approach", [(37, 21), (38, 21), (39, 21), (39, 22), (40, 22), (40, 23), (41, 23), (41, 24)], 30.0, 45.0),
    # Running-line blocks drawn on the mainline and yard corridors
    # (unnamed, no posted speed — defaults like every editor-drawn block).
    ("BLK_588D28", "", [(37, 26), (37, 27), (38, 27)], 0.0, None),
    ("BLK_589634", "", [(36, 24), (36, 25)], 0.0, None),
    ("BLK_58B277", "", [(31, 17), (32, 17), (32, 18), (33, 18), (33, 19), (34, 19), (34, 20), (35, 20), (35, 21)], 0.0, None),
    ("BLK_58BC8E", "", [(26, 13), (27, 13), (28, 13), (28, 14), (29, 14), (29, 15), (30, 15), (30, 16), (31, 16)], 0.0, None),
    ("BLK_58C972", "", [(24, 13)], 0.0, None),
    ("BLK_58D3CB", "", [(22, 13)], 0.0, None),
    ("BLK_58DCB7", "", [(20, 16)], 0.0, None),
    ("BLK_58E4DA", "", [(20, 18)], 0.0, None),
    ("BLK_58ED93", "", [(20, 20), (20, 21)], 0.0, None),
    ("BLK_5908BA", "", [(21, 22), (21, 23)], 0.0, None),
    ("BLK_591162", "", [(20, 23)], 0.0, None),
    ("BLK_9C013F", "", [(10, 5), (10, 6), (11, 6), (11, 7), (12, 7)], 0.0, None),
    ("BLK_9C155C", "", [(11, 4), (12, 4), (12, 5), (13, 5), (13, 6)], 0.0, None),
    ("BLK_9C2E19", "", [(12, 3), (13, 3), (13, 4), (14, 4), (14, 5)], 0.0, None),
    ("BLK_9C50CD", "", [(9, 6), (9, 7), (10, 7), (10, 8), (11, 8)], 0.0, None),
]


# Yard stations. Each entry: (group_id, group_name, [(platform_id, name,
# first_tile, extra_tiles, platform_side), ...]). Every platform is 3 tiles
# (60 cm) anchored at the yard road's buffer end and extending inward. side is
# "right" throughout (cosmetic — it only picks which side renders the platform
# graphic) and is tunable. add_station auto-creates one `{platform_id}_BLOCK`
# per platform, so no blocks are pre-declared here.
STATIONS = [
    ("STN_NW_YARD", "NW Yard", [
        ("PLT_NWY_1", "NW Yard 1", (-5, -27), [(-4, -27), (-4, -26)], "right"),
        ("PLT_NWY_2", "NW Yard 2", (-4, -28), [(-3, -28), (-3, -27)], "right"),
        ("PLT_NWY_3", "NW Yard 3", (-3, -29), [(-2, -29), (-2, -28)], "right"),
        ("PLT_NWY_4", "NW Yard 4", (-2, -30), [(-1, -30), (-1, -29)], "right"),
        ("PLT_NWY_5", "NW Yard 5", (-1, -31), [(0, -31), (0, -30)], "right"),
    ]),
    ("STN_YARD_A", "Yard A", [
        ("PLT_YA_1", "Yard A 1", (24, 22), [(23, 22), (23, 21)], "right"),
        ("PLT_YA_2", "Yard A 2", (25, 21), [(24, 21), (24, 20)], "right"),
        ("PLT_YA_3", "Yard A 3", (26, 20), [(25, 20), (25, 19)], "right"),
        ("PLT_YA_4", "Yard A 4", (27, 19), [(26, 19), (26, 18)], "right"),
        ("PLT_YA_5", "Yard A 5", (28, 18), [(27, 18), (27, 17)], "right"),
        ("PLT_YA_6", "Yard A 6", (29, 17), [(28, 17), (28, 16)], "right"),
        ("PLT_YA_7", "Yard A 7", (25, 27), [(25, 26), (24, 26)], "right"),
        ("PLT_YA_8", "Yard A 8", (26, 26), [(26, 25), (25, 25)], "right"),
        ("PLT_YA_9", "Yard A 9 (siding)", (24, 28), [(24, 27), (23, 27)], "right"),
        ("PLT_YA_10", "Yard A 10 (siding)", (23, 29), [(23, 28), (22, 28)], "right"),
    ]),
    ("STN_YARD_B", "Yard B", [
        ("PLT_YB_1", "Yard B 1", (38, 30), [(38, 29), (37, 29)], "right"),
        ("PLT_YB_2", "Yard B 2", (41, 27), [(40, 27), (40, 26)], "right"),
        ("PLT_YB_3", "Yard B 3", (42, 26), [(41, 26), (41, 25)], "right"),
        ("PLT_YB_4", "Yard B 4", (43, 25), [(42, 25), (42, 24)], "right"),
        ("PLT_YB_5", "Yard B 5", (39, 29), [(39, 28), (38, 28)], "right"),
    ]),
]


def _add_tracks(network: RailwayNetwork) -> None:
    """Place every baked track tile."""
    for (x, y, bits) in TILES:
        network.add_track(TrackSegment(x, y, track_bits=bits))


def _add_switches(network: RailwayNetwork) -> None:
    """Create the Switch entity for each switch tile and link the tile to it."""
    for (switch_id, x, y) in SWITCHES:
        network.add_switch(Switch(switch_id=switch_id, x=x, y=y))
        network.get_track(x, y).switch_id = switch_id


def _add_stations(network: RailwayNetwork) -> None:
    """Build every yard platform (3 tiles each) and group each yard's platforms
    into one station."""
    for (group_id, group_name, platforms) in STATIONS:
        platform_ids = []
        for (pid, name, first_xy, extra_xys, side) in platforms:
            platform = network.add_station(
                x=first_xy[0],
                y=first_xy[1],
                station_id=pid,
                name=name,
                length_cm=60.0,
                platform_side=side,
                top_speed_kmh=45.0,
            )
            for extra in extra_xys:
                network.extend_station(platform.platform_id, [extra])
            platform_ids.append(platform.platform_id)
        network.create_station_group(
            station_id=group_id,
            name=group_name,
            platform_ids=platform_ids,
        )


def _add_blocks(network: RailwayNetwork) -> None:
    """Create the approach and running-line blocks from their baked tile lists."""
    for (block_id, name, positions, length_cm, speed_limit_kmh) in BLOCKS:
        network.create_block_with_tracks(
            block_id=block_id,
            positions=positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
        )


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the Demo 6 network from scratch and return it.

    Mirrors the name used by the other scene modules so bootstrap.py can load
    it through the standard scene interface. Order: plain tracks, then the
    switches, then the yard stations (each auto-creates its platform block),
    then the approach and running-line blocks on top.
    """
    print("\n" + "=" * 70)
    print("Demo 6: Baked yard layout + three yard stations")
    print("=" * 70)

    network = RailwayNetwork()
    _add_tracks(network)
    _add_switches(network)
    _add_stations(network)
    _add_blocks(network)

    print(
        f"  tracks={len(network.tracks)}  "
        f"switches={len(network.switches)}  "
        f"platforms={len(network.platforms)}  "
        f"blocks={len(network.blocks)}"
    )
    return network


def setup_demo_train(
    network: RailwayNetwork,
    path_dispatcher: PathDispatcher,
    sim_controller,
):
    """No-op: Demo 6 has track + yard stations but no trains or routes yet.

    Kept so the scene satisfies the bootstrap scene interface
    (bootstrap.setup_demo_train calls this). Routes and trains will be added
    here as the layout is built out.
    """
    print("  Demo 6: yard stations built; no trains/routes installed yet.")
