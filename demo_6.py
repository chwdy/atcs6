"""
Demo 6: Baked track skeleton (geometry-only scaffold).

This scene bakes the hand-drawn track skeleton from layouts/demo_6.json
directly into Python (same style as demo_4 / demo_5): 434 track tiles and
25 switches from the JSON, 2 baked tunnels, plus 2 turnouts added here to
splice the two disconnected Yard A sidings into the running network. On top of
the geometry it builds three yard STATIONS (NW Yard, Yard A, Yard B) with 19
platforms total — each 3 tiles long, sitting at the buffer end of a yard road;
add_station auto-creates one block per platform. There are still no signals,
routes, or trains — setup_demo_train() is a no-op; trains and routes get added
on top as the layout is built out.

Network shape (tile coordinates; the editor renders this rotated 45 degrees,
so the long diagonal runs read as straight running lines on screen):

  - North-south double-track mainline on columns x=7 and x=8, from the north
    buffer stops (7,-29)/(8,-29) down to (7,-2)/(8,-2), with a mid-line
    crossover at SW_7_-19 / SW_7_-20 / SW_8_-19.
  - Tunnel portal: both mainline tracks tunnel from (7,-2)N and (8,-2)N
    through to (9,3) — the only link between the north mainline and the
    south/east network (the two are not grid-adjacent).
  - SE diagonal corridor from (9,3) up to the east throat at
    SW_19_13 / SW_20_13..15 / SW_21_13, with a triangle junction at
    SW_14_9 / SW_15_8 / SW_16_10.
  - West side: junction SW_-19_0, a wye at SW_-7_-10 / SW_-7_-11, and a
    far-west buffer stub at (-22,2)/(-21,3).
  - NW yard (station "NW Yard", 5 platforms): a 5-track fan ending at
    (-1,-31)..(-5,-27), gathered by the ladder throat SW_0_-24 / SW_2_-24 /
    SW_2_-25 / SW_2_-27.
  - Center-right yard "A" (station "Yard A", 10 platforms): fan ending at
    (24,22)..(29,17) and (25,27)/(26,26), fed via SW_22_23 and the east
    throat, plus the two former stub sidings — now spliced in at SW_20_22 /
    SW_20_24 — ending at (24,28) and (23,29).
  - Far-east yard "B" (station "Yard B", 4 platforms): a 5-switch ladder at
    x=36 (SW_36_21..26) feeding a fan ending at (38,30)..(43,25). Two 1-tile
    stub spurs off the (39,28) crossing are too short for a 3-tile platform
    and carry none.

Dropped from the source JSON: 13 stale switch definitions that sat at
coordinates with no track tile (leftovers from an earlier depot/ladder design):
  SW_7_10, SW_8_10, SW_LAD0, SW_LAD1, SW_LAD2, SW_LAD3, SW_LAD4, SW_LAD5, SW_LEAD, SW_X1, SW_X2, SW_XOVER_N, SW_XOVER_S
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


# Track-tile layout. Each entry: (x, y, bits, role).
# S track runs SE-side of the corridor; N track runs NW-side.
# Both tracks zig-zag NE overall (the y=x line — appears horizontal on the
# rotated isometric screen).
#
# The three throat tiles (7,7), (7,8), (7,9) are NOT listed here — they are
# placed as switches by `_add_throat`. The corridor blocks therefore split
# around them, exactly the way the X1/X2 scissors tiles split their blocks.



# Baked track geometry from layouts/demo_6.json. Each entry: (x, y, bits).
# dead_end_ports (buffer stops) are NOT listed here — the network recomputes
# them from connectivity when tracks and tunnels are added (see
# TrackSegment._update_dead_ends). Switch tiles carry their geometry here and
# get their Switch entity from SWITCHES below.
TILES = [
    (-22, 2, TRACK_NE),
    (-21, 1, TRACK_NE),
    (-21, 2, TRACK_NE | TRACK_SW),
    (-21, 3, TRACK_SW),
    (-20, 0, TRACK_NE),
    (-20, 1, TRACK_NE | TRACK_SW),
    (-20, 2, TRACK_SW),
    (-19, -1, TRACK_NE),
    (-19, 0, TRACK_NS | TRACK_EW | TRACK_NE | TRACK_SW),  # SW_-19_0
    (-19, 1, TRACK_SW),
    (-18, -2, TRACK_NE),
    (-18, -1, TRACK_NE | TRACK_SW),
    (-18, 0, TRACK_SW),
    (-17, -3, TRACK_NE),
    (-17, -2, TRACK_NE | TRACK_SW),
    (-17, -1, TRACK_SW),
    (-16, -4, TRACK_NE),
    (-16, -3, TRACK_NE | TRACK_SW),
    (-16, -2, TRACK_SW),
    (-15, -5, TRACK_NE),
    (-15, -4, TRACK_NE | TRACK_SW),
    (-15, -3, TRACK_SW),
    (-14, -6, TRACK_NE),
    (-14, -5, TRACK_NE | TRACK_SW),
    (-14, -4, TRACK_SW),
    (-13, -7, TRACK_NE),
    (-13, -6, TRACK_NE | TRACK_SW),
    (-13, -5, TRACK_SW),
    (-12, -8, TRACK_NE),
    (-12, -7, TRACK_NE | TRACK_SW),
    (-12, -6, TRACK_SW),
    (-11, -9, TRACK_NE),
    (-11, -8, TRACK_NE | TRACK_SW),
    (-11, -7, TRACK_SW),
    (-10, -10, TRACK_NE),
    (-10, -9, TRACK_NE | TRACK_SW),
    (-10, -8, TRACK_SW),
    (-9, -11, TRACK_NE),
    (-9, -10, TRACK_NE | TRACK_SW),
    (-9, -9, TRACK_SW),
    (-8, -11, TRACK_EW),
    (-8, -10, TRACK_EW),
    (-7, -11, TRACK_EW | TRACK_NE),  # SW_-7_-11
    (-7, -10, TRACK_EW | TRACK_SW),  # SW_-7_-10
    (-6, -11, TRACK_EW),
    (-6, -10, TRACK_EW),
    (-5, -27, TRACK_SE),
    (-5, -11, TRACK_EW),
    (-5, -10, TRACK_EW),
    (-4, -28, TRACK_SE),
    (-4, -27, TRACK_NW),
    (-4, -26, TRACK_SE),
    (-4, -11, TRACK_NW),
    (-4, -10, TRACK_NW | TRACK_SE),
    (-4, -9, TRACK_SE),
    (-3, -29, TRACK_SE),
    (-3, -28, TRACK_NW),
    (-3, -27, TRACK_SE),
    (-3, -26, TRACK_NW),
    (-3, -25, TRACK_SE),
    (-3, -10, TRACK_NW),
    (-3, -9, TRACK_NW | TRACK_SE),
    (-3, -8, TRACK_SE),
    (-2, -30, TRACK_SE),
    (-2, -29, TRACK_NW),
    (-2, -28, TRACK_SE),
    (-2, -27, TRACK_NW),
    (-2, -26, TRACK_SE),
    (-2, -25, TRACK_NW),
    (-2, -24, TRACK_SE),
    (-2, -9, TRACK_NW),
    (-2, -8, TRACK_NW | TRACK_SE),
    (-2, -7, TRACK_SE),
    (-1, -31, TRACK_SE),
    (-1, -30, TRACK_NW),
    (-1, -29, TRACK_SE),
    (-1, -28, TRACK_NW),
    (-1, -27, TRACK_SE),
    (-1, -26, TRACK_NW),
    (-1, -25, TRACK_SE),
    (-1, -24, TRACK_EW),
    (-1, -8, TRACK_NW),
    (-1, -7, TRACK_NW | TRACK_SE),
    (-1, -6, TRACK_SE),
    (0, -31, TRACK_NW),
    (0, -30, TRACK_SE),
    (0, -29, TRACK_NW),
    (0, -28, TRACK_SE),
    (0, -27, TRACK_NW),
    (0, -26, TRACK_SE),
    (0, -25, TRACK_NW),
    (0, -24, TRACK_EW | TRACK_SE),  # SW_0_-24
    (0, -7, TRACK_NW),
    (0, -6, TRACK_NW | TRACK_SE),
    (0, -5, TRACK_SE),
    (1, -30, TRACK_NW),
    (1, -29, TRACK_SE),
    (1, -28, TRACK_NW),
    (1, -27, TRACK_SE),
    (1, -26, TRACK_NW),
    (1, -25, TRACK_SE),
    (1, -24, TRACK_EW),
    (1, -6, TRACK_NW),
    (1, -5, TRACK_NW | TRACK_SE),
    (1, -4, TRACK_SE),
    (2, -29, TRACK_NW),
    (2, -28, TRACK_NS),
    (2, -27, TRACK_NS | TRACK_NW),  # SW_2_-27
    (2, -26, TRACK_NS),
    (2, -25, TRACK_NS | TRACK_NW),  # SW_2_-25
    (2, -24, TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE),  # SW_2_-24
    (2, -23, TRACK_SE),
    (2, -5, TRACK_NW),
    (2, -4, TRACK_NW | TRACK_SE),
    (2, -3, TRACK_SE),
    (3, -24, TRACK_NW),
    (3, -23, TRACK_NW | TRACK_SE),
    (3, -22, TRACK_SE),
    (3, -4, TRACK_NW),
    (3, -3, TRACK_NW | TRACK_SE),
    (3, -2, TRACK_SE),
    (4, -23, TRACK_NW),
    (4, -22, TRACK_NW | TRACK_SE),
    (4, -21, TRACK_SE),
    (4, -3, TRACK_NW),
    (4, -2, TRACK_NW | TRACK_SE),
    (4, -1, TRACK_SE),
    (5, -22, TRACK_NW),
    (5, -21, TRACK_NW | TRACK_SE),
    (5, -20, TRACK_SE),
    (5, -2, TRACK_NW),
    (5, -1, TRACK_NW | TRACK_SE),
    (5, 0, TRACK_SE),
    (6, -21, TRACK_NW),
    (6, -20, TRACK_NW | TRACK_SE),
    (6, -19, TRACK_SE),
    (6, -1, TRACK_NW),
    (6, 0, TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE),  # SW_6_0
    (6, 1, TRACK_SE),
    (7, -29, TRACK_NS),
    (7, -28, TRACK_NS),
    (7, -27, TRACK_NS),
    (7, -26, TRACK_NS),
    (7, -25, TRACK_NS),
    (7, -24, TRACK_NS),
    (7, -23, TRACK_NS),
    (7, -22, TRACK_NS),
    (7, -21, TRACK_NS),
    (7, -20, TRACK_NS | TRACK_NW),  # SW_7_-20
    (7, -19, TRACK_NS | TRACK_NW | TRACK_SE),  # SW_7_-19
    (7, -18, TRACK_NS),
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
    (7, 1, TRACK_NW | TRACK_SE),
    (7, 2, TRACK_NS),
    (7, 3, TRACK_NS),
    (7, 4, TRACK_NS),
    (7, 5, TRACK_SE),
    (8, -29, TRACK_NS),
    (8, -28, TRACK_NS),
    (8, -27, TRACK_NS),
    (8, -26, TRACK_NS),
    (8, -25, TRACK_NS),
    (8, -24, TRACK_NS),
    (8, -23, TRACK_NS),
    (8, -22, TRACK_NS),
    (8, -21, TRACK_NS),
    (8, -20, TRACK_NS),
    (8, -19, TRACK_NS | TRACK_NW),  # SW_8_-19
    (8, -18, TRACK_NS),
    (8, -17, TRACK_NS),
    (8, -16, TRACK_NS),
    (8, -15, TRACK_NS),
    (8, -14, TRACK_NS),
    (8, -13, TRACK_NS),
    (8, -12, TRACK_NS),
    (8, -11, TRACK_NS),
    (8, -10, TRACK_NS),
    (8, -9, TRACK_NS),
    (8, -8, TRACK_NS),
    (8, -7, TRACK_NS),
    (8, -6, TRACK_NS),
    (8, -5, TRACK_NS),
    (8, -4, TRACK_NS),
    (8, -3, TRACK_NS),
    (8, -2, TRACK_NS),
    (8, 1, TRACK_EW),
    (8, 5, TRACK_NW),
    (8, 6, TRACK_SE),
    (9, 1, TRACK_EW),
    (9, 3, TRACK_NW | TRACK_SE),
    (9, 4, TRACK_SE),
    (9, 6, TRACK_NW),
    (9, 7, TRACK_SE),
    (10, 1, TRACK_EW),
    (10, 3, TRACK_NW),
    (10, 4, TRACK_NW | TRACK_SE),
    (10, 5, TRACK_SE),
    (10, 7, TRACK_NW),
    (10, 8, TRACK_SE),
    (11, 1, TRACK_NW),
    (11, 2, TRACK_SE),
    (11, 4, TRACK_NW),
    (11, 5, TRACK_NW | TRACK_SE),
    (11, 6, TRACK_SE),
    (11, 8, TRACK_NW),
    (11, 9, TRACK_SE),
    (12, 2, TRACK_NW),
    (12, 3, TRACK_SE),
    (12, 5, TRACK_NW),
    (12, 6, TRACK_NW | TRACK_SE),
    (12, 7, TRACK_SE),
    (12, 9, TRACK_EW),
    (13, 3, TRACK_NW),
    (13, 4, TRACK_SE),
    (13, 6, TRACK_NW),
    (13, 7, TRACK_NW | TRACK_SE),
    (13, 8, TRACK_SE),
    (13, 9, TRACK_EW),
    (14, 4, TRACK_NW),
    (14, 5, TRACK_SE),
    (14, 7, TRACK_NW),
    (14, 8, TRACK_NW | TRACK_SE),
    (14, 9, TRACK_EW | TRACK_SE),  # SW_14_9
    (15, 5, TRACK_NW),
    (15, 6, TRACK_NS),
    (15, 7, TRACK_NS),
    (15, 8, TRACK_NS | TRACK_NW),  # SW_15_8
    (15, 9, TRACK_NW | TRACK_SE),
    (15, 10, TRACK_SE),
    (16, 9, TRACK_NW),
    (16, 10, TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE),  # SW_16_10
    (16, 11, TRACK_SE),
    (17, 10, TRACK_NW),
    (17, 11, TRACK_NW | TRACK_SE),
    (17, 12, TRACK_SE),
    (18, 11, TRACK_NW),
    (18, 12, TRACK_NW | TRACK_SE),
    (18, 13, TRACK_SE),
    (19, 12, TRACK_NW),
    (19, 13, TRACK_EW | TRACK_NW | TRACK_SE),  # SW_19_13
    (19, 14, TRACK_SE),
    (20, 13, TRACK_EW | TRACK_NW),  # SW_20_13
    (20, 14, TRACK_NS | TRACK_NW),  # SW_20_14
    (20, 15, TRACK_NS | TRACK_SE),  # SW_20_15
    (20, 16, TRACK_NS),
    (20, 17, TRACK_NS | TRACK_EW),
    (20, 18, TRACK_NS),
    (20, 19, TRACK_NS | TRACK_EW),
    (20, 20, TRACK_NS),
    (20, 21, TRACK_NS),
    (20, 22, TRACK_SE),
    (20, 23, TRACK_NS),
    (20, 24, TRACK_SE),
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
    (23, 13, TRACK_NS | TRACK_EW),
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
    (25, 13, TRACK_NS | TRACK_EW),
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
    (36, 25, TRACK_NS | TRACK_SE),  # SW_36_25
    (36, 26, TRACK_NS | TRACK_SE),  # SW_36_26
    (36, 27, TRACK_NS),
    (36, 28, TRACK_SE),
    (37, 21, TRACK_EW),
    (37, 22, TRACK_EW),
    (37, 23, TRACK_NW),
    (37, 24, TRACK_SE),
    (37, 25, TRACK_NW),
    (37, 26, TRACK_NW | TRACK_SE),
    (37, 27, TRACK_SE),
    (37, 28, TRACK_NW),
    (37, 29, TRACK_SE),
    (38, 21, TRACK_EW),
    (38, 22, TRACK_NW),
    (38, 23, TRACK_SE),
    (38, 24, TRACK_NW),
    (38, 25, TRACK_SE),
    (38, 26, TRACK_NW),
    (38, 27, TRACK_NW | TRACK_SE),
    (38, 28, TRACK_SE),
    (38, 29, TRACK_NW),
    (38, 30, TRACK_SE),
    (39, 21, TRACK_NW),
    (39, 22, TRACK_SE),
    (39, 23, TRACK_NW),
    (39, 24, TRACK_SE),
    (39, 25, TRACK_NW),
    (39, 26, TRACK_SE),
    (39, 27, TRACK_NW),
    (39, 28, TRACK_NW | TRACK_SE),
    (39, 29, TRACK_SE),
    (40, 22, TRACK_NW),
    (40, 23, TRACK_SE),
    (40, 24, TRACK_NW),
    (40, 25, TRACK_SE),
    (40, 26, TRACK_NW),
    (40, 27, TRACK_SE),
    (40, 28, TRACK_NW),
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


# Switch entities (switch_id, x, y) for the 25 switch tiles above. All are
# standard turnouts at default MAIN alignment.
SWITCHES = [
    ("SW_-19_0", -19, 0),
    ("SW_-7_-11", -7, -11),
    ("SW_-7_-10", -7, -10),
    ("SW_0_-24", 0, -24),
    ("SW_2_-27", 2, -27),
    ("SW_2_-25", 2, -25),
    ("SW_2_-24", 2, -24),
    ("SW_6_0", 6, 0),
    ("SW_7_-20", 7, -20),
    ("SW_7_-19", 7, -19),
    ("SW_8_-19", 8, -19),
    ("SW_14_9", 14, 9),
    ("SW_15_8", 15, 8),
    ("SW_16_10", 16, 10),
    ("SW_19_13", 19, 13),
    ("SW_20_13", 20, 13),
    ("SW_20_14", 20, 14),
    ("SW_20_15", 20, 15),
    ("SW_21_13", 21, 13),
    ("SW_22_23", 22, 23),
    ("SW_36_21", 36, 21),
    ("SW_36_22", 36, 22),
    ("SW_36_23", 36, 23),
    ("SW_36_25", 36, 25),
    ("SW_36_26", 36, 26),
]


# Tunnel pairs: ((x, y, edge), (x, y, edge)). They link the south end of the
# x=7/8 mainline to the (9,3) corridor head across a one-tile gap.
TUNNELS = [
    ((9, 3, "S"), (8, -2, "N")),
    ((9, 3, "W"), (7, -2, "N")),
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


def _add_tunnels(network: RailwayNetwork) -> None:
    """Pair the mainline south end with the (9,3) corridor head."""
    for (a_ep, b_ep) in TUNNELS:
        network.add_tunnel(a_ep, b_ep)


# Splice the two disconnected Yard A sidings into the running network.
# Each siding's SW foot is an NS dead-end sitting directly north of a plain SE
# tile; converting that tile to SE|NS adds the N<->S segment (a 45-degree-eased
# turnout, NOT a square corner — it carries the NS diagonal bit) so the foot
# connects through. SW_20_22 links siding 1 to the fan; SW_20_24 carries on to
# siding 2. With both in place the whole layout is one connected component.
SIDING_TURNOUTS = [
    ("SW_20_22", 20, 22),
    ("SW_20_24", 20, 24),
]


def _connect_yard_a_sidings(network: RailwayNetwork) -> None:
    """Turn the two splice tiles into SE|NS turnouts and give each a Switch."""
    for (switch_id, x, y) in SIDING_TURNOUTS:
        network.get_track(x, y).set_track_bits(TRACK_SE | TRACK_NS)
        network.add_switch(Switch(switch_id=switch_id, x=x, y=y))
        network.get_track(x, y).switch_id = switch_id


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
    ]),
]


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
                top_speed_kmh=40.0,
            )
            for extra in extra_xys:
                network.extend_station(platform.platform_id, [extra])
            platform_ids.append(platform.platform_id)
        network.create_station_group(
            station_id=group_id,
            name=group_name,
            platform_ids=platform_ids,
        )


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the Demo 6 network from scratch and return it.

    Mirrors the name used by the other scene modules so bootstrap.py can load
    it through the standard scene interface. Order: plain tracks, then the
    baked switches, then the two siding-splice turnouts, then tunnels
    (add_tunnel requires both endpoints to be dead-ends, which only holds once
    all plain tiles are placed), then the yard stations.
    """
    print("\n" + "=" * 70)
    print("Demo 6: Baked skeleton + three yard stations")
    print("=" * 70)

    network = RailwayNetwork()
    _add_tracks(network)
    _add_switches(network)
    _connect_yard_a_sidings(network)
    _add_tunnels(network)
    _add_stations(network)

    print(
        f"  tracks={len(network.tracks)}  "
        f"switches={len(network.switches)}  "
        f"tunnels={len(network.tunnels) // 2}  "
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
