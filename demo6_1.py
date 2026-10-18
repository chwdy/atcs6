"""
Demo 6.1: demo_6 with holding points, for exercising reservation chunking.

This is an exact copy of the demo_6 layout — same tiles, switches, stations,
blocks, tunnels, parked stock and the two circular routes — with ONE change:
six main-line running blocks are marked as holding points
(`Block.is_holding_point = True`). A holding point is a block a train may wait
on without fouling a switch throat or blocking a platform; under the
reserve-and-lock model a train's reservation stops (chunks) at the next holding
point instead of extending through the contested junction ahead. The six sit on
both main-line approaches to each of the three diamond crossings (West, Center,
East) — see HOLDING_POINTS below. Nothing else differs from demo_6.

This scene bakes the hand-drawn layout from layouts/demo_6.json directly into
Python (same style as demo_4 / demo_5): 474 track tiles and 44 switches form
two double-track running lines that each gather into yards — a longer "main
line" carrying Yard A and Yard B, and a "branch line" through NW Yard.

STATIONS (54 platforms): three yard stations (NW Yard, Yard A, Yard B; 20
platforms, 3 tiles each / 60 cm, at a yard-road buffer end); ten Mainline
stations (STN_ML_1..10) along the double-track section from the NW buffer
terminus to the Yard A throat; and six Branch stations (STN_BR_1..6) on the
x=7 / x=9 corridor through NW Yard. Each line station is a pair of platforms
(one per running track, A/B) sitting directly across from each other, 2 or 3
tiles long (60 cm), inline on the running line; two running blocks fall between
adjacent stations. Two stations are special: Mainline 9 is the M6 four-track
INTERCHANGE — ONE station of four platforms (two outer through-tracks = main
line, two inner stub tracks = branch, ids PLT_BR_7_*; tunnelled to the branch),
sides facing inward for cross-platform transfer; and Mainline 10 is the eastern
TERMINAL beside the Yard A crossover where main-line services turn back.
add_station auto-creates one {platform_id}_BLOCK per platform (length copied
from the platform: 60 cm throughout, longer than the 45 cm stock).

Blocks (160): one {platform_id}_BLOCK per platform (54), the
{platform_id}_APPROACH yard-road blocks (19), the inter-station running blocks
(BLK_ML_* / BLK_BR7_* / BLK_BR9_*), the remaining hand-drawn line blocks, and
BLK_FILL_1..34 covering every other bare running-line tile (the parallel
B-tracks, yard-throat connectors, interchange stubs) so the whole network is
blocked end to end.

TUNNELS (2): the M6 interchange's two inner stub tracks tunnel from their
dead-ends to the branch's north buffer, so branch trains terminate at M6 and
the two running lines become ONE connected network (the interchange keeps its
main-line switch links too, so it is also a junction).

ROUTES (2) + TRAINS (17): setup_demo_train() places one 45 cm locomotive on
every yard road except one per yard (so each yard has N-1 trains), then builds
two CIRCULAR routes and assigns + powers every train. MAIN_ROUTE runs the whole
main line M1<->M10 (all stops, out on A platforms / back on B); BRANCH_ROUTE
runs from the M10 terminal through the M9 interchange down the branch to B6 and
back. The 4 NW-Yard + 3 Yard-A trains run the Branch; the other 10 run the Main.
Because the stock starts packed in dead-end yard sidings, departures are
throat-limited — trains leave one at a time, so circulation builds up gradually
rather than all at once. No signals.

The editor renders the grid rotated 45 degrees, so the long diagonal tile runs
read as straight horizontal running lines on screen.
"""

from network import RailwayNetwork
from track import (
    Tile,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
)
from path_dispatcher import PathDispatcher
from switch import Switch
from entity_ids import segment_id_for_block


# Identifier read by bootstrap.py and by scene-gated tests to recognise this
# scene. Each scene module declares its own ID; tests compare against the
# active scene (see bootstrap.get_current_scene_id) to decide whether to run.
SCENE_ID = "demo6_1"


# Blocks marked as holding points in this variant — the ONE thing that differs
# from demo_6 (applied by _mark_holding_points after the blocks are built). A
# holding point is a block a train may wait on without fouling a switch throat
# or blocking a platform, so a reservation chunks (stops) at the next holding
# point rather than running on through the junction ahead. These six are the
# main-line running blocks flanking the three diamond crossings — one on each
# approach to each crossing — so a train heading for a contested crossing holds
# on the last block before it:
#   BLK_ML_1  / BLK_ML_2   -> West crossing   (BLK_XING_W)
#   BLK_ML_13 / BLK_ML_14  -> Center crossing (BLK_XING_C)
#   BLK_ML_17 / BLK_ML_18  -> East crossing   (BLK_XING_E)
HOLDING_POINTS = [
    "BLK_ML_1", "BLK_ML_2",
    "BLK_ML_13", "BLK_ML_14",
    "BLK_ML_17", "BLK_ML_18",
]


# Baked track geometry from layouts/demo_6.json. Each entry: (x, y, bits).
# dead_end_ports (buffer stops) are NOT listed here — the network recomputes
# them from connectivity when tracks are added (see
# Tile._update_dead_ends). Switch tiles carry their geometry here and
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
# (block_id, name, [positions], length_cm, speed_limit_kmh). create_segment_with_tracks
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
    ("BLK_588D28", "", [(37, 26), (37, 27), (38, 27)], 30.0, None),
    ("BLK_589634", "", [(36, 24), (36, 25)], 20.0, None),
    ("BLK_58B277", "", [(31, 17), (32, 17), (32, 18), (33, 18), (33, 19), (34, 19), (34, 20), (35, 20), (35, 21)], 30.0, None),
    ("BLK_58BC8E", "", [(26, 13), (27, 13), (28, 13), (28, 14), (29, 14), (29, 15), (30, 15), (30, 16), (31, 16)], 30.0, None),
    ("BLK_58C972", "", [(24, 13)], 20.0, None),
    ("BLK_58D3CB", "", [(22, 13)], 20.0, None),
    ("BLK_58DCB7", "", [(20, 16)], 20.0, None),
    ("BLK_58E4DA", "", [(20, 18)], 20.0, None),
    ("BLK_58ED93", "", [(20, 20), (20, 21)], 20.0, None),
    ("BLK_5908BA", "", [(21, 22), (21, 23)], 20.0, None),
    ("BLK_591162", "", [(20, 23)], 20.0, None),
    # (The three BLK_9C* stub blocks that used to sit here are gone — those
    # tiles are now the M6 interchange's inner branch platforms / approaches.)
    # Main-line running blocks: the gaps between the Mainline stations, two
    # blocks per gap (a junction switch/crossing makes the odd gap three).
    ("BLK_ML_1", "", [(-21, 2), (-21, 1)], 20.0, None),
    ("BLK_ML_2", "", [(-17, 1), (-17, 0), (-16, 0), (-16, -1)], 30.0, None),
    ("BLK_ML_3", "", [(-14, -3)], 20.0, None),
    ("BLK_ML_4", "", [(-13, -3), (-13, -4)], 20.0, None),
    ("BLK_ML_5", "", [(-11, -5)], 20.0, None),
    ("BLK_ML_6", "", [(-11, -6)], 20.0, None),
    ("BLK_ML_7", "", [(-9, -7)], 20.0, None),
    ("BLK_ML_8", "", [(-9, -8)], 20.0, None),
    ("BLK_ML_9", "", [(-6, -8)], 20.0, None),
    ("BLK_ML_10", "", [(-4, -8), (-4, -7), (-3, -7), (-3, -6)], 30.0, None),
    ("BLK_ML_11", "", [(-1, -5)], 20.0, None),
    ("BLK_ML_12", "", [(-1, -4), (0, -4)], 20.0, None),
    ("BLK_ML_13", "", [(1, -2), (2, -2), (2, -1)], 30.0, None),
    ("BLK_ML_14", "", [(6, -1), (6, 0)], 20.0, None),
    ("BLK_ML_15", "", [(8, 1), (9, 1), (10, 1), (11, 1), (11, 2)], 30.0, None),
    ("BLK_ML_16", "", [(12, 2), (12, 3), (13, 3), (13, 4), (14, 4)], 30.0, None),
    ("BLK_ML_17", "", [(15, 6)], 20.0, None),
    ("BLK_ML_18", "", [(15, 8), (16, 8)], 20.0, None),
    ("BLK_ML_19", "", [(19, 11)], 20.0, None),
    ("BLK_ML_20", "", [(20, 12), (20, 13)], 20.0, None),
    # Branch-line running blocks on the x=7 (BR7) and x=9 (BR9) tracks: the
    # gaps between the 6 Branch stations, split two-per-gap on the clear run.
    ("BLK_BR7_1", "", [(7, -4)], 20.0, None),
    ("BLK_BR7_2", "", [(7, -5)], 20.0, None),
    ("BLK_BR7_3", "", [(7, -8)], 20.0, None),
    ("BLK_BR7_4", "", [(7, -9)], 20.0, None),
    ("BLK_BR7_5", "", [(7, -12)], 20.0, None),
    ("BLK_BR7_6", "", [(7, -13)], 20.0, None),
    ("BLK_BR7_7", "", [(7, -16)], 20.0, None),
    ("BLK_BR7_8", "", [(7, -17)], 20.0, None),
    ("BLK_BR7_9", "", [(7, -24)], 20.0, None),
    ("BLK_BR7_10", "", [(7, -26)], 20.0, None),
    ("BLK_BR9_1", "", [(9, -4)], 20.0, None),
    ("BLK_BR9_2", "", [(9, -5)], 20.0, None),
    ("BLK_BR9_3", "", [(9, -8)], 20.0, None),
    ("BLK_BR9_4", "", [(9, -9)], 20.0, None),
    ("BLK_BR9_5", "", [(9, -12)], 20.0, None),
    ("BLK_BR9_6", "", [(9, -13)], 20.0, None),
    ("BLK_BR9_7", "", [(9, -16)], 20.0, None),
    ("BLK_BR9_8", "", [(9, -17)], 20.0, None),
    ("BLK_BR9_9", "", [(9, -19)], 20.0, None),
    ("BLK_BR9_10", "", [(9, -20)], 20.0, None),
    ("BLK_BR9_11", "", [(9, -25)], 20.0, None),
    ("BLK_BR9_12", "", [(9, -26)], 20.0, None),
    # Fill blocks: every remaining bare running-line tile (the parallel B-track
    # running lines, yard-throat connectors, and interchange stubs) put into a
    # block so the whole network is blocked end to end. One block per contiguous
    # switch-free run; length 0 / no posted speed like the other running blocks.
    ("BLK_FILL_1", "", [(-21, 4)], 20.0, None),
    ("BLK_FILL_2", "", [(-20, 0)], 20.0, None),
    ("BLK_FILL_3", "", [(-19, -1), (-18, -2), (-18, -1)], 30.0, None),
    ("BLK_FILL_4", "", [(-19, 3)], 20.0, None),
    ("BLK_FILL_5", "", [(-18, 2)], 20.0, None),
    ("BLK_FILL_6", "", [(-16, -4), (-15, -5), (-15, -4)], 30.0, None),
    ("BLK_FILL_7", "", [(-13, -7), (-13, -6)], 20.0, None),
    ("BLK_FILL_8", "", [(-11, -9), (-11, -8), (-10, -10), (-10, -9), (-9, -10)], 30.0, None),
    ("BLK_FILL_9", "", [(-6, -9), (-5, -9)], 20.0, None),
    ("BLK_FILL_10", "", [(-5, -10), (-4, -10), (-3, -10), (-3, -9), (-2, -9), (-2, -8)], 30.0, None),
    ("BLK_FILL_11", "", [(0, -7), (0, -6), (1, -6)], 30.0, None),
    ("BLK_FILL_12", "", [(1, -23), (2, -23), (2, -22)], 30.0, None),
    ("BLK_FILL_13", "", [(2, -26)], 20.0, None),
    ("BLK_FILL_14", "", [(2, -4), (3, -4), (3, -3), (4, -3)], 30.0, None),
    ("BLK_FILL_15", "", [(3, -24)], 20.0, None),
    ("BLK_FILL_16", "", [(3, -21), (4, -21), (4, -20), (5, -20), (5, -19), (6, -19), (6, -18)], 30.0, None),
    ("BLK_FILL_17", "", [(3, 0)], 20.0, None),
    ("BLK_FILL_18", "", [(4, -23), (4, -22), (5, -22), (5, -21), (6, -21), (6, -20)], 30.0, None),
    ("BLK_FILL_19", "", [(4, 1)], 20.0, None),
    ("BLK_FILL_20", "", [(5, -2)], 20.0, None),
    ("BLK_FILL_21", "", [(6, 2), (6, 3), (7, 3), (7, 4), (7, 5), (8, 5), (8, 6), (9, 6), (9, 7), (10, 7), (10, 8)], 30.0, None),
    ("BLK_FILL_22", "", [(8, -25), (8, -24)], 20.0, None),
    ("BLK_FILL_23", "", [(8, -19), (8, -18)], 20.0, None),
    ("BLK_FILL_24", "", [(9, 4), (9, 5), (10, 5), (10, 6), (11, 6), (11, 7)], 30.0, None),
    ("BLK_FILL_25", "", [(10, 3), (11, 3), (11, 4), (12, 4), (12, 5), (13, 5)], 30.0, None),
    ("BLK_FILL_26", "", [(12, 9)], 20.0, None),
    ("BLK_FILL_27", "", [(13, 8)], 20.0, None),
    ("BLK_FILL_28", "", [(14, 7)], 20.0, None),
    ("BLK_FILL_29", "", [(14, 9), (14, 10)], 20.0, None),
    ("BLK_FILL_30", "", [(15, 11)], 20.0, None),
    ("BLK_FILL_31", "", [(17, 9)], 20.0, None),
    ("BLK_FILL_32", "", [(17, 13), (18, 13), (18, 14)], 30.0, None),
    ("BLK_FILL_33", "", [(19, 13)], 20.0, None),
    ("BLK_FILL_34", "", [(19, 15)], 20.0, None),
    # Diamond crossings (NS+EW, track_bits 3). Each sits between four switches,
    # so each is its own single-tile block.
    ("BLK_XING_W", "West crossing", [(-19, 1)], 20.0, None),
    ("BLK_XING_C", "Center crossing", [(4, -1)], 20.0, None),
    ("BLK_XING_E", "East crossing", [(16, 10)], 20.0, None),
]


# Tunnels (pairs of dead-end edges joined as one tile-to-tile link). The M6
# interchange's two inner stub tracks dead-end at (9,4) and (10,3); each is
# tunnelled to the branch line's north buffer so branch trains run up the
# x=7/x=9 corridor, through the tunnel, and terminate at the M6 interchange
# beside the main line. Paired A-to-A / B-to-B so the branch double-track
# continues uncrossed: branch-A (x=7, (7,-2)) -> interchange Track2 (9,4);
# branch-B (x=9, (9,-2)) -> interchange Track3 (10,3). This also joins the two
# running lines into one connected network. Entry: ((x, y, edge), (x, y, edge)).
TUNNELS = [
    ((9, 4, "W"), (7, -2, "N")),
    ((10, 3, "S"), (9, -2, "N")),
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
    # --- Main line: stations spaced along the double-track section from the NW
    # buffer terminus to the Yard A throat. Each is a pair of platforms, one on
    # each running track (A / B) sitting directly across from each other,
    # 2 or 3 tiles each, inline on the running line; two running blocks
    # (BLK_ML_* below) fall between adjacent stations. Mainline 9 is the M6
    # INTERCHANGE: ONE station of four platforms — the two outer through-tracks
    # are the main line, the two inner stub tracks are the branch (tunnelled to
    # the branch via TUNNELS); sides face inward so each main platform pairs
    # across with a branch platform for cross-platform transfer. Mainline 10 is
    # the eastern TERMINAL by the Yard A crossover, where services turn back.
    ("STN_ML_1", "Mainline 1", [
        ("PLT_ML_1_A", "Mainline 1 A", (-22, 3), [(-22, 2)], "right"),
        ("PLT_ML_1_B", "Mainline 1 B", (-20, 3), [(-20, 4)], "left"),
    ]),
    ("STN_ML_2", "Mainline 2", [
        ("PLT_ML_2_A", "Mainline 2 A", (-15, -1), [(-15, -2), (-14, -2)], "right"),
        ("PLT_ML_2_B", "Mainline 2 B", (-17, -2), [(-17, -3), (-16, -3)], "left"),
    ]),
    ("STN_ML_3", "Mainline 3", [
        ("PLT_ML_3_A", "Mainline 3 A", (-12, -4), [(-12, -5)], "right"),
        ("PLT_ML_3_B", "Mainline 3 B", (-14, -5), [(-14, -6)], "left"),
    ]),
    ("STN_ML_4", "Mainline 4", [
        ("PLT_ML_4_A", "Mainline 4 A", (-10, -6), [(-10, -7)], "right"),
        ("PLT_ML_4_B", "Mainline 4 B", (-12, -7), [(-12, -8)], "left"),
    ]),
    ("STN_ML_5", "Mainline 5", [
        ("PLT_ML_5_A", "Mainline 5 A", (-8, -8), [(-7, -8)], "right"),
        ("PLT_ML_5_B", "Mainline 5 B", (-7, -10), [(-8, -10)], "left"),
    ]),
    ("STN_ML_6", "Mainline 6", [
        ("PLT_ML_6_A", "Mainline 6 A", (-2, -6), [(-2, -5)], "right"),
        ("PLT_ML_6_B", "Mainline 6 B", (-1, -8), [(-1, -7)], "left"),
    ]),
    ("STN_ML_7", "Mainline 7", [
        ("PLT_ML_7_A", "Mainline 7 A", (0, -3), [(1, -3)], "right"),
        ("PLT_ML_7_B", "Mainline 7 B", (1, -5), [(2, -5)], "left"),
    ]),
    ("STN_ML_8", "Mainline 8", [
        ("PLT_ML_8_A", "Mainline 8 A", (7, 0), [(7, 1)], "right"),
        ("PLT_ML_8_B", "Mainline 8 B", (5, 1), [(5, 2)], "left"),
    ]),
    # M6 four-track interchange — ONE station, four platforms (top→bottom):
    # main-up (Track1), branch-A (Track2), branch-B (Track3), main-down (Track4).
    # Inner-facing sides pair main<->branch across for transfer.
    ("STN_ML_9", "Mainline 9 (interchange)", [
        ("PLT_ML_9_B", "Mainline 9 main B", (11, 8), [(11, 9)], "right"),
        ("PLT_BR_7_A", "Mainline 9 branch A", (12, 7), [(12, 8)], "left"),
        ("PLT_BR_7_B", "Mainline 9 branch B", (13, 6), [(14, 6)], "right"),
        ("PLT_ML_9_A", "Mainline 9 main A", (14, 5), [(15, 5)], "left"),
    ]),
    ("STN_ML_10", "Mainline 10 (terminal)", [
        ("PLT_ML_10_A", "Mainline 10 (terminal) A", (18, 10), [(18, 11)], "right"),
        ("PLT_ML_10_B", "Mainline 10 (terminal) B", (17, 12), [(16, 12)], "left"),
    ]),
    # --- Branch line (the line through NW Yard): 6 stations along the x=7 / x=9
    # double-track corridor, north buffer terminus (7,-2)/(9,-2) to the south
    # stub terminus (7,-29)/(9,-29). Four sit on the clear run north of the
    # yard throat; the last two use the short segments between the throat
    # switches, so a couple of gaps carry a switch instead of two clean blocks.
    ("STN_BR_1", "Branch 1", [
        ("PLT_BR_1_A", "Branch 1 A", (7, -2), [(7, -3)], "right"),
        ("PLT_BR_1_B", "Branch 1 B", (9, -2), [(9, -3)], "right"),
    ]),
    ("STN_BR_2", "Branch 2", [
        ("PLT_BR_2_A", "Branch 2 A", (7, -6), [(7, -7)], "right"),
        ("PLT_BR_2_B", "Branch 2 B", (9, -6), [(9, -7)], "right"),
    ]),
    ("STN_BR_3", "Branch 3", [
        ("PLT_BR_3_A", "Branch 3 A", (7, -10), [(7, -11)], "right"),
        ("PLT_BR_3_B", "Branch 3 B", (9, -10), [(9, -11)], "right"),
    ]),
    ("STN_BR_4", "Branch 4", [
        ("PLT_BR_4_A", "Branch 4 A", (7, -14), [(7, -15)], "right"),
        ("PLT_BR_4_B", "Branch 4 B", (9, -14), [(9, -15)], "right"),
    ]),
    ("STN_BR_5", "Branch 5", [
        ("PLT_BR_5_A", "Branch 5 A", (7, -21), [(7, -22), (7, -23)], "right"),
        ("PLT_BR_5_B", "Branch 5 B", (9, -21), [(9, -22), (9, -23)], "right"),
    ]),
    ("STN_BR_6", "Branch 6", [
        ("PLT_BR_6_A", "Branch 6 A", (7, -27), [(7, -28), (7, -29)], "right"),
        ("PLT_BR_6_B", "Branch 6 B", (9, -27), [(9, -28), (9, -29)], "right"),
    ]),
]


# Parked yard stock: one train on every yard platform road EXCEPT one per yard
# (left free for run-rounds). Each entry is the platform_id of an occupied yard
# road; the train sits on {platform_id}_BLOCK, powered off, with no route.
# setup_demo_train() resolves each train's facing + head tile from the layout
# (see _yard_train_placement) so it sits fully inside the platform — head at the
# throat, body tucked into the dead end, ready to depart forward, the way demo_5
# parks its stock. NW Yard, Yard A, Yard B each keep their last/siding road
# (PLT_NWY_5 / PLT_YA_10 / PLT_YB_5) empty -> 4 + 9 + 4 = 17 trains.
TRAINS = [
    "PLT_NWY_1",
    "PLT_NWY_2",
    "PLT_NWY_3",
    "PLT_NWY_4",
    "PLT_YA_1",
    "PLT_YA_2",
    "PLT_YA_3",
    "PLT_YA_4",
    "PLT_YA_5",
    "PLT_YA_6",
    "PLT_YA_7",
    "PLT_YA_8",
    "PLT_YA_9",
    "PLT_YB_1",
    "PLT_YB_2",
    "PLT_YB_3",
    "PLT_YB_4",
]


# Two CIRCULAR routes the parked stock is assigned to (built in
# setup_demo_train). Each is an out-and-back loop: out on the A platforms,
# back on the B platforms. MAIN runs the whole main line M1<->M10; BRANCH runs
# from the M10 terminal through the M9 interchange down the branch to B6.
# Stops are platform_ids in travel order (the route loops from the last back to
# the first).
MAIN_ROUTE_STOPS = (
    ["STN_ML_1"]+[f"PLT_ML_{i}_B" for i in range(2, 8)]+ ["PLT_ML_8_A"] + ["PLT_ML_9_A"]
    + ["PLT_ML_10_B", "PLT_ML_9_B", "PLT_ML_8_B"] + [f"PLT_ML_{i}_A" for i in range(7, 1, -1)]
)
BRANCH_ROUTE_STOPS = (
    ["PLT_ML_10_A", "PLT_BR_7_A"] + [f"PLT_BR_{i}_A" for i in range(1, 7)]
    + [f"PLT_BR_{i}_B" for i in range(5, 0, -1)] + ["PLT_BR_7_B"]
)
# Train -> route split. Branch: the 4 NW-Yard trains + 3 Yard-A trains.
# Main: the remaining 10 (6 Yard-A + 4 Yard-B). Ids are T_<platform_id[4:]>.
BRANCH_TRAIN_IDS = [
    "T_NWY_1", "T_NWY_2", "T_NWY_3", "T_NWY_4",
    "T_YA_1", "T_YA_2", "T_YA_3",
]


def _add_tracks(network: RailwayNetwork) -> None:
    """Place every baked track tile."""
    for (x, y, bits) in TILES:
        network.add_track(Tile(x, y, track_bits=bits))


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
                # Every platform is 60 cm — longer than the 45 cm stock so any
                # train berths fully within the platform. add_station copies this
                # length to the platform's {id}_BLOCK (the tile is bare at this
                # point), so the platform and its block share the length.
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
        network.create_segment_with_tracks(
            # The tuple's id is this scene's BLOCK id: saved routes, tests and
            # hardware bindings already name it, so it is pinned and the
            # segment takes the matching geometry-axis id (see entity_ids).
            block_id=block_id,
            segment_id=segment_id_for_block(block_id),
            positions=positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
        )


def _add_tunnels(network: RailwayNetwork) -> None:
    """Join the baked tunnel endpoint pairs (M6 interchange <-> branch)."""
    for (a, b) in TUNNELS:
        network.add_tunnel(a, b)


def _mark_holding_points(network: RailwayNetwork) -> None:
    """Set `is_holding_point = True` on this variant's HOLDING_POINTS blocks."""
    for block_id in HOLDING_POINTS:
        block = network.get_block(block_id)
        if block is None:
            raise ValueError(
                f"demo6_1 holding point {block_id!r} is not a block in this "
                f"scene; HOLDING_POINTS is out of sync with BLOCKS."
            )
        block.is_holding_point = True


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the Demo 6 network from scratch and return it.

    Mirrors the name used by the other scene modules so bootstrap.py can load
    it through the standard scene interface. Order: plain tracks, then the
    switches, then the yard stations (each auto-creates its platform block),
    then the approach and running-line blocks on top.
    """
    print("\n" + "=" * 70)
    print("Demo 6.1: demo_6 layout + six main-line holding points")
    print("=" * 70)

    network = RailwayNetwork()
    _add_tracks(network)
    _add_switches(network)
    _add_stations(network)
    _add_blocks(network)
    _add_tunnels(network)
    _mark_holding_points(network)

    print(
        f"  tracks={len(network.tracks)}  "
        f"switches={len(network.switches)}  "
        f"platforms={len(network.platforms)}  "
        f"blocks={len(network.blocks)}  "
        f"tunnels={len(network.tunnels) // 2}"
    )
    return network


def _yard_train_placement(network: RailwayNetwork, platform_id: str):
    """Resolve ``(initial_direction, head_tile)`` for a loco parked on a yard road.

    Each yard road is a dead-end stub: exactly one of its tiles (the throat)
    borders the road's own ``{platform_id}_APPROACH`` block; the others run back
    to the buffer. Placing the head on that throat tile facing OUT toward the
    approach makes the body extend back into the stub, so the whole train sits
    inside ``{platform_id}_BLOCK`` and can depart forward — the same shape demo_5
    parks its stock in (head at the open end, body into the dead end).

    Raises ValueError if no tile borders the road's approach.
    """
    block_id = f"{platform_id}_BLOCK"
    approach_id = f"{platform_id}_APPROACH"
    positions = network.get_block_positions(block_id)
    tile_set = {tuple(p) for p in positions}
    edge_to_dir = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST"}
    for (x, y) in positions:
        track = network.get_track(x, y)
        for edge in sorted(track.edges):
            neighbor = network.get_neighbor(x, y, edge)
            if neighbor is None or (neighbor.x, neighbor.y) in tile_set:
                continue
            if neighbor.block_id == approach_id:
                return edge_to_dir[edge], (x, y)
    raise ValueError(
        f"No throat tile for {platform_id!r}: none of its tiles border "
        f"{approach_id!r}, so a yard placement cannot be resolved."
    )


def setup_demo_train(
    network: RailwayNetwork,
    path_dispatcher: PathDispatcher,
    sim_controller,
):
    """Place the yard stock, build the Main and Branch routes, and assign +
    power every train so the scene runs.

    The 17 trains are placed in the yards (one per road, minus one road per
    yard) exactly as before, then each is assigned to a CIRCULAR route and
    powered on: the 4 NW-Yard + 3 Yard-A trains run the BRANCH route, the
    other 10 run the MAIN route. Because the stock starts packed in dead-end
    yard sidings, departures are throat-limited — trains leave one at a time
    and circulation builds up slowly rather than all at once.
    """
    from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE
    from train_route import (
        Route, RouteTask, RouteMode, RouteStartMode, PlatformTarget, StationTarget,
    )

    # One shared profile for the stock (definition state only).
    profile = SpeedProfile(
        profile_id="yard_profile",
        name="Yard Stock",
        power_speed_curve=list(DEFAULT_SPEED_PROFILE.power_speed_curve),
        voltage_power_curve=list(DEFAULT_SPEED_PROFILE.voltage_power_curve),
        lighting_voltage=DEFAULT_SPEED_PROFILE.lighting_voltage,
        acceleration_rate=3.0,
        deceleration_rate=4.2,
        emergency_brake_rate=5,
        friction_rate=DEFAULT_SPEED_PROFILE.friction_rate,
        operational_decel_ratio=DEFAULT_SPEED_PROFILE.operational_decel_ratio,
        air_resistance_enabled=True,
        drag_coefficient=0.001,
        reference_speed_kmh=100.0,
    )
    network.speed_profiles.register(profile)

    # Place the yard stock. Facing + head tile are resolved from the layout so
    # each loco sits fully inside its road (head at the throat, body in the
    # stub), rather than poking out into the approach.
    for platform_id in TRAINS:
        facing, head_tile = _yard_train_placement(network, platform_id)
        train = sim_controller.add_train(
            train_id=f"T_{platform_id[4:]}",
            starting_block_id=f"{platform_id}_BLOCK",
            initial_direction=facing,
            max_speed_kmh=120.0,
            length_cm=45.0,
            train_type="LOCOMOTIVE",
            initial_cab="A",
            start_tile=head_tile,
        )
        train.speed_profile_id = profile.profile_id

    # Build + register the two CIRCULAR routes. start_mode=NEAREST means each
    # train heads for the stop nearest its yard rather than all racing to the
    # first stop. task_direction="either" lets a train reverse out of its road.
    def _build_route(route_id, name, stops):
        route = Route(route_id=route_id, name=name, mode=RouteMode.CIRCULAR,
                      start_mode=RouteStartMode.NEAREST)
        for stop_id in stops:
            # A stop id is either a platform (PLT_*) or a whole station group
            # (STN_*). Targeting the station group lets the resolver pick
            # whichever of its platforms is free and de-conflicts trains across
            # them; wrapping a station id as a PlatformTarget would point at a
            # platform that does not exist, which the runtime treats as a broken
            # task and silently skips. Dispatch on which registry the id is in.
            if stop_id in network.station_groups:
                location = StationTarget(station_id=stop_id)
            else:
                location = PlatformTarget(platform_id=stop_id)
            route.add_task(RouteTask(
                task_kind="stop",
                task_location=location,
                task_direction="either",
            ))
        sim_controller.route_executor.register_route(route)
        return route

    main_route = _build_route("ROUTE_MAIN", "Main Line", MAIN_ROUTE_STOPS)
    branch_route = _build_route("ROUTE_BRANCH", "Branch Line", BRANCH_ROUTE_STOPS)

    # Assign each train to its route and power it on.
    branch_set = set(BRANCH_TRAIN_IDS)
    n_branch = n_main = 0
    for platform_id in TRAINS:
        train_id = f"T_{platform_id[4:]}"
        route = branch_route if train_id in branch_set else main_route
        route.assign_train(train_id)
        if route is branch_route:
            n_branch += 1
        else:
            n_main += 1
        _ctrl = sim_controller.get_or_create_controller(train_id)
        _ctrl.commands.power_on_cmd()
        _ctrl.commands.set_mode("ato")

    print(f"  Demo 6: {len(TRAINS)} trains placed; assigned {n_branch} to the "
          f"Branch route and {n_main} to the Main route, powered on.")
