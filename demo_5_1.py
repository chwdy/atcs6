"""
Demo 5.1: Four-Station Loop + Depot, trains start stabled (best-effort)

Demo 5.1 is Demo 5 with one operational change: the fleet grows from four to
FIVE trains, and instead of seating each train on its loop platform, all five
start stabled in the depot (platforms 1-5) and must path out onto the loop on
the first tick. The track, depot, throat, and route are identical to Demo 5 —
only the train count and their initial placement differ (see the "Initial
placement" note below).

Demo 5 (and therefore 5.1) is Demo 4 with three changes:

  1. Stations 3 and 4 (plus the X2 scissors crossover and the NE tunnel ends)
     are shifted NE by grid (+4, +4). This stretches each track's S2↔S3
     corridor from 3 tiles to 11 tiles, split into two blocks (A and B) by
     the depot-throat switches.

  2. The S2↔S3 corridor stays plain double track and is run one-way per track
     by convention: the S track carries outbound traffic (NE, away from the
     origin) and the N track carries the return (toward 0,0). There is no
     scissors crossover here, so the ONLY way to move between the two tracks
     in this corridor is the single N→S crossover at the throat (below).

  3. A 6-platform depot is built above the corridor. Its platforms are
     perpendicular to the main line (NE/SW vertical stubs) and sit in ONE
     aligned horizontal row. They ladder off a single running line, real-
     depot style: the lead rises from SW_LEAD and the platforms merge onto it
     one by one through 45°-eased turnouts (a single-direction ladder — no
     central split, so no forced 90° corner). The lead enters at the ladder's
     right end (nearest the throat).

Throat (between S2 and S3). The N→S crossover is a single DIAGONAL: the swap
rides the 45° EW bit, so it draws as one slanted rail (like the X1/X2
scissors) rather than a square drop-and-turn. The two crossover switches are
offset by one tile — SW_XOVER_N on the N track, SW_XOVER_S one tile outbound
on the S track:
    SW_LEAD     (7, 9)  SE|NS : through S↔E keeps the N track; the depot lead
                                rises from here (N↔S onto the lead).
    SW_XOVER_N  (7, 8)  NW|EW : through W↔N keeps the N track; W↔E swaps the
                                train DOWN-RIGHT along the diagonal to the S.
    SW_XOVER_S  (8, 8)  SE|EW : through S↔E keeps the S track; W↔E receives
                                the diagonal swap and merges it outbound.
  A train swapping N→S runs as one diagonal: N track → SW_XOVER_N (W→E) →
  SW_XOVER_S (W→E) → S corridor. None of the throat switches is a square
  corner (each carries a 45° NS/EW bit), so they pass the no-square-corner
  switch rule. With every throat switch in its default (through) state the
  main-line loop still runs unchanged.

Lead crossover (one row above the throat, at y=10). A second diagonal
crossover links the depot lead to N-track block B so the depot can be reached
from the S3 end of the N track, not just through SW_LEAD's lower merge:
    SW_7_10  (7,10) NS|NE : MAIN N↔S keeps the depot lead straight; DIVERGE
                            N↔E swaps onto the crossover toward SW_8_10.
    SW_8_10  (8,10) SE|EW : DIVERGE S↔E keeps the N track straight (the loop
                            return leg); MAIN W↔E receives the swap.
  The swap rides the 45° NS/EW bit as one slanted rail, so neither tile is a
  square corner. In their default states (SW_7_10 MAIN, SW_8_10 DIVERGE) the
  depot lead and the loop both stay continuous. The crossover splits N-track
  block B into B_1 (before SW_8_10) and B_2 (after it).

Layout (rendered horizontally on the rotated screen):

      D1   D2   D3   D4   D5   D6        ← 6 aligned platforms (vertical stubs)
       ╲    ╲    ╲    ╲    ╲    ╲         ← each peels off at 45°
   ────╳────╳────╳────╳────╳────╳╮       ← single ladder (one running line)
                                 │ lead rises from SW_LEAD (enters right end)
  ┌── S1-N ──┐    ┌── S2-N ──┐ ·· corridor ··◇lead◇·· ┌── S3-N ──┐    ┌── S4-N ──┐
  ├──────────┤─X1─├──────────┤── plain ──────╲──────── ├──────────┤─X2─├──────────┤  ← N track
  │          │    │          │                ╲ diagonal           │    │          │
  ├──────────┤─X1─├──────────┤── plain ────────╲─────── ├──────────┤─X2─├──────────┤  ← S track
  └── S1-S ──┘    └── S2-S ──┘                          └── S3-S ──┘    └── S4-S ──┘

Route (CIRCULAR, all 5 trains share it):
  STN_1 → dwell → PLT_S2_S → dwell → PLT_S3_S → dwell → STN_4
    → dwell → PLT_S3_N → dwell → PLT_S2_N → dwell → loop.

Initial placement (the demo_5.1 change). All five trains start stabled in the
depot, one per platform, parked at the platform mouth (the base tile, where the
stub meets its ladder turnout) facing EAST — i.e. nose pointed out toward the
ladder, body trailing up the stub. On tick 0 each train plans a path down the
lead and out onto the loop.

The depot necks down to ONE lead that joins the N-track return leg PAST the
one-way throat, so a depot train can only reach an S-track (outbound) stop by
running to a terminus and reversing. With five trains that pressures the S1/X1
reversal pocket: a train crossing the X1 scissors to reverse through S1 can
foul the junction nose-to-nose with a train sitting at S1. Two things keep that
from deadlocking:

  1. The dispatcher's "stay back one switch" rule (the junction-fouling
     truncation exclusion in design_new.md item 11): a best-effort authority
     that cannot reach its target never stops on a TWO-WAY block straight off a
     switch — it holds one block back, before the switch, leaving the crossover
     clear. So a train bound for S1 waits before X1 instead of fouling it.
  2. One-way track policy (`_set_one_way_tracks`): the S2<->S3 corridor and the
     two N-track crossover approaches are marked one-way, which both lets trains
     pack tight there (no head-on is possible on a one-way block, so rule 1
     exempts it) and stops the pathfinder running a train the wrong way.

On top of that the trains pull out as a "parade" — each assigned consecutive
loop stops in the loop's travel direction from the depot exit, ordered so the
platform nearest the lead (D5) leaves first — so they follow each other onto the
line in a sensible order rather than all racing for the same first stop:
  T5 in depot platform 5 (base (5,14), facing E) → first stop PLT_S3_N  (pulls out first)
  T4 in depot platform 4 (base (4,13), facing E) → first stop PLT_S2_N
  T3 in depot platform 3 (base (3,12), facing E) → first stop STN_1
  T2 in depot platform 2 (base (2,11), facing E) → first stop PLT_S2_S
  T1 in depot platform 1 (base (1,10), facing E) → first stop PLT_S3_S  (pulls out last)
Depot platform 6 (nearest the throat) is left empty. Because the depot necks
down to a single lead, the five trains serialise through the throat under the
BEST_EFFORT daisy-chain — they leave the depot a few seconds apart, not all at
once.

Performance note (expected, accepted):
  The mid-corridor N->S crossover adds graph cycles, so the pathfinder's
  topology-only walk enumeration (`railway_pathfinder.query_graph_walks`,
  whose BFS visited-key includes the visited block-set) is ~20x costlier
  per query than on demo_4's graph. The shared loop route uses
  `task_direction="either"`, so `route_target_resolver.resolve_task_direction`
  fires two such walks per task each time a (blocked) train re-plans. Under
  the 5-train BEST_EFFORT daisy-chain — the same gridlock demo_4 has — this
  is ~30 walks/tick, giving ~150-200 ms/tick. demo_4 runs the identical
  route and gridlocks the same way; its queries are just ~0.3 ms each so it
  is fast. The behaviour here is correct (trains circulate, no deadlock),
  only slower. The clean fix would be to memoize topology-only walks (they
  are a pure function of the static graph), but that is a shared-engine
  change out of this scene's scope.
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
SCENE_ID = "demo_5.1"


# Scissors crossover bit pattern (NS + EW straights + NW + SE diagonals).
# Per design_new.md "Switch.position": 0 = MAIN, 1 = DIVERGE. The dispatcher
# derives the required switch state from track.enumerate_traversals
# (track.py): NS/EW straight bits → state 0 (MAIN), NW/SE diagonal bits →
# state 1 (DIVERGE).
#
#   State 0 (MAIN) — swap routes via straight bits:
#     S→N swap uses NS (bit 0)
#     N→S swap uses EW (bit 1)  (entry W, exit E)
#
#   State 1 (DIVERGE) — stay-on-track routes via diagonal bits:
#     S track  go S→E uses SE (bit 4) — stays on S track
#     N track  go W→N uses NW (bit 3) — stays on N track
SCISSORS_BITS = TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE  # = 27


# ============= Demo Functions =============


# Track-tile layout. Each entry: (x, y, bits, role).
# S track runs SE-side of the corridor; N track runs NW-side.
# Both tracks zig-zag NE overall (the y=x line — appears horizontal on the
# rotated isometric screen).
#
# The three throat tiles (7,7), (7,8), (7,9) are NOT listed here — they are
# placed as switches by `_add_throat`. The corridor blocks therefore split
# around them, exactly the way the X1/X2 scissors tiles split their blocks.

S_TRACK_TILES = [
    # --- S1-S (unchanged) ---
    (1, 0, TRACK_NW, "S1_S_t1"),
    (1, 1, TRACK_SE, "S1_S_t2"),
    (2, 1, TRACK_NW, "S1_S_t3"),
    (2, 2, TRACK_NS, "BLK_S1_S_TO_X1"),   # NS detour up to crossover X1
    # (2, 3) is crossover X1.
    (3, 3, TRACK_EW, "BLK_X1_TO_S2_S"),   # EW detour out of X1 toward S2-S
    # --- S2-S (unchanged) ---
    (4, 3, TRACK_NW, "S2_S_t1"),
    (4, 4, TRACK_SE, "S2_S_t2"),
    (5, 4, TRACK_NW, "S2_S_t3"),
    # --- S2↔S3 corridor, block A (4 tiles; ends before the crossover) ---
    (5, 5, TRACK_SE, "BLK_S2_S_TO_S3_S_A_t1"),
    (6, 5, TRACK_NW, "BLK_S2_S_TO_S3_S_A_t2"),
    (6, 6, TRACK_SE, "BLK_S2_S_TO_S3_S_A_t3"),
    (7, 6, TRACK_NW, "BLK_S2_S_TO_S3_S_A_t4"),
    (7, 7, TRACK_SE, "BLK_S2_S_TO_S3_S_A_t5"),
    (8, 7, TRACK_NW, "BLK_S2_S_TO_S3_S_A_t6"),
    # (8, 8) is throat switch SW_XOVER_S — the S-side landing of the diagonal
    # N→S crossover (the swap rides the 45° EW bit down from SW_XOVER_N).
    # --- S2↔S3 corridor, block B (4 tiles; starts after the crossover) ---
    (9, 8, TRACK_NW, "BLK_S2_S_TO_S3_S_B_t1"),
    (9, 9, TRACK_SE, "BLK_S2_S_TO_S3_S_B_t2"),
    (10, 9, TRACK_NW, "BLK_S2_S_TO_S3_S_B_t3"),
    (10, 10, TRACK_SE, "BLK_S2_S_TO_S3_S_B_t4"),
    # --- S3-S (demo_4 coords + (4,4)) ---
    (11, 10, TRACK_NW, "S3_S_t1"),
    (11, 11, TRACK_SE, "S3_S_t2"),
    (12, 11, TRACK_NW, "S3_S_t3"),
    (12, 12, TRACK_NS, "BLK_S3_S_TO_X2"),  # NS detour up to crossover X2
    # (12, 13) is crossover X2.
    (13, 13, TRACK_EW, "BLK_X2_TO_S4_S"),  # EW detour out of X2 toward S4-S
    # --- S4-S (demo_4 coords + (4,4)) ---
    (14, 13, TRACK_NW, "S4_S_t1"),
    (14, 14, TRACK_SE, "S4_S_t2"),
    (15, 14, TRACK_NW, "S4_S_t3"),
]

# N track is offset (-1, +1) from S track on every tile (NW direction).
N_TRACK_TILES = [
    # --- S1-N (unchanged) ---
    (0, 1, TRACK_NW, "S1_N_t1"),
    (0, 2, TRACK_SE, "S1_N_t2"),
    (1, 2, TRACK_NW, "S1_N_t3"),
    (1, 3, TRACK_SE, "BLK_S1_N_TO_X1"),   # natural SE zigzag — enters X1 from W
    # (2, 3) is crossover X1 — N track passes through via NW bit.
    (2, 4, TRACK_SE, "BLK_X1_TO_S2_N"),   # natural SE zigzag — exits X1 to N
    # --- S2-N (unchanged) ---
    (3, 4, TRACK_NW, "S2_N_t1"),
    (3, 5, TRACK_SE, "S2_N_t2"),
    (4, 5, TRACK_NW, "S2_N_t3"),
    # --- S2↔S3 corridor, block A (5 tiles; ends before the crossover) ---
    (4, 6, TRACK_SE, "BLK_S2_N_TO_S3_N_A_t1"),
    (5, 6, TRACK_NW, "BLK_S2_N_TO_S3_N_A_t2"),
    (5, 7, TRACK_SE, "BLK_S2_N_TO_S3_N_A_t3"),
    (6, 7, TRACK_NW, "BLK_S2_N_TO_S3_N_A_t4"),
    (6, 8, TRACK_SE, "BLK_S2_N_TO_S3_N_A_t5"),
    # (7, 8) is throat switch SW_XOVER_N (N side of the diagonal crossover);
    # (7, 9) is throat switch SW_LEAD (to the depot). The crossover's S-side
    # switch SW_XOVER_S lands one tile outbound on the S track at (8, 8).
    # --- S2↔S3 corridor, block B (split by the lead crossover SW_8_10) ---
    (8, 9, TRACK_NW, "BLK_S2_N_TO_S3_N_B_1"),     # block B_1 (1 tile)
    # (8, 10) is lead-crossover switch SW_8_10 — block B splits around it.
    (9, 10, TRACK_NW, "BLK_S2_N_TO_S3_N_B_2_t1"),  # block B_2 starts after SW_8_10
    (9, 11, TRACK_SE, "BLK_S2_N_TO_S3_N_B_2_t2"),
    # --- S3-N (demo_4 coords + (4,4)) ---
    (10, 11, TRACK_NW, "S3_N_t1"),
    (10, 12, TRACK_SE, "S3_N_t2"),
    (11, 12, TRACK_NW, "S3_N_t3"),
    (11, 13, TRACK_SE, "BLK_S3_N_TO_X2"),
    # (12, 13) is crossover X2.
    (12, 14, TRACK_SE, "BLK_X2_TO_S4_N"),
    # --- S4-N (demo_4 coords + (4,4)) ---
    (13, 14, TRACK_NW, "S4_N_t1"),
    (13, 15, TRACK_SE, "S4_N_t2"),
    (14, 15, TRACK_NW, "S4_N_t3"),
]


# --- Depot geometry (a real-depot LADDER throat above the corridor) ------
# Six platforms (vertical NE/SW stubs, 3 tiles / 60 cm each) hang UP off ONE
# continuous ladder, bases aligned on screen-row x-y = -9 so they read as a
# horizontal rank. Screen: col = x+y (right), row = x-y (down).
#
# Every ladder turnout is SE|EW: SE (horizontal) carries the ladder through,
# EW (the 45° "╲" diagonal) peels the platform off — so each merge eases at
# 45° and NONE is a square corner. The ladder is SINGLE-DIRECTION (no central
# 3-way split, which would force a 90°): the lead enters at the right end
# (nearest the throat) and the platforms ladder off one by one, funnelling
# left — exactly how a real depot throat merges tracks gradually.
#   turnout SW_LADk = (2+k, 10+k)   platform base = (1+k, 10+k)  (W of turnout)
DEPOT_PLATFORM_BASES = [(1 + k, 10 + k) for k in range(6)]


def _add_plain_tracks(network: RailwayNetwork) -> None:
    """Place all non-switch main-line tiles for both tracks."""
    for tiles in (S_TRACK_TILES, N_TRACK_TILES):
        for (x, y, bits, _role) in tiles:
            network.add_track(Tile(x, y, track_bits=bits))


def _add_crossovers(network: RailwayNetwork) -> None:
    """Place the two scissors crossover tiles plus their Switch entities.

    Each crossover is a single tile whose bits encode both tracks' natural
    routes through the tile, AND the swap routes (NS for S→N, EW for N→S).
    The crossover tile is a switch boundary — it does NOT belong to any
    block. X2 is at demo_4's coord + (4, 4).
    """
    from enums import SwitchState
    for (sx, sy, sid) in ((2, 3, "SW_X1"), (12, 13, "SW_X2")):
        tile = Tile(sx, sy, track_bits=SCISSORS_BITS)
        network.add_track(tile)
        switch = Switch(switch_id=sid, x=sx, y=sy)
        # DIVERGE (1) = stay-on-track via NW/SE diagonals — the demo's
        # nominal alignment. MAIN (0) = swap via NS/EW straights.
        switch.position = SwitchState.DIVERGE
        network.add_switch(switch)
        tile.switch_id = switch.switch_id


def _add_throat(network: RailwayNetwork) -> None:
    """Place the three depot-throat switch tiles between S2 and S3.

    The N→S crossover is a single DIAGONAL: the swap rides the 45° EW bit, so
    it draws as one slanted rail (like the X1/X2 scissors) instead of a square
    drop-and-turn, and the switches are offset one tile (SW_XOVER_N on the N
    track, SW_XOVER_S one tile outbound on the S track). Every switch keeps a
    main-line through-route as its default state (DIVERGE below) so the loop
    still runs; the other state serves the depot lead / the crossover. None of
    these is a "square corner" (each has a diagonal NS/EW bit), so they satisfy
    the no-square-corner switch rule.

      SW_LEAD     (7,9) SE|NS : DIVERGE S↔E = N through, MAIN N↔S = depot lead
      SW_XOVER_N  (7,8) NW|EW : DIVERGE W↔N = N through, MAIN W↔E = swap to S
      SW_XOVER_S  (8,8) SE|EW : DIVERGE S↔E = S through, MAIN W↔E = receive swap
    """
    from enums import SwitchState
    throat = [
        (7, 9, TRACK_SE | TRACK_NS, "SW_LEAD", SwitchState.DIVERGE),
        (7, 8, TRACK_NW | TRACK_EW, "SW_XOVER_N", SwitchState.DIVERGE),
        (8, 8, TRACK_SE | TRACK_EW, "SW_XOVER_S", SwitchState.DIVERGE),
    ]
    for (sx, sy, bits, sid, state) in throat:
        tile = Tile(sx, sy, track_bits=bits)
        network.add_track(tile)
        switch = Switch(switch_id=sid, x=sx, y=sy)
        switch.position = state
        network.add_switch(switch)
        tile.switch_id = switch.switch_id


def _add_lead_crossover(network: RailwayNetwork) -> None:
    """Place the second diagonal crossover, one row above the throat (y=10),
    linking the depot lead to N-track block B.

    SW_7_10 sits on the depot lead at (7,10); SW_8_10 sits on the N track at
    (8,10), one tile to its E. Like the throat crossover, the swap rides the
    45° NS/EW diagonal so it draws as a single slanted rail and neither tile is
    a square corner. Each switch keeps its straight route as the default state,
    so the depot lead and the loop both stay continuous; the other state opens
    the crossover that links them.

      SW_7_10 (7,10) NS|NE : MAIN N↔S = depot lead through,
                             DIVERGE N↔E = swap onto the crossover to SW_8_10
      SW_8_10 (8,10) SE|EW : DIVERGE S↔E = N-track through (loop return leg),
                             MAIN W↔E = receive the swap from SW_7_10

    The pair connects the depot lead (above SW_7_10) to N-track block B at
    (9,10), a depot↔N-track path that bypasses SW_LEAD's lower merge.
    """
    from enums import SwitchState
    crossover = [
        (7, 10, TRACK_NS | TRACK_NE, "SW_7_10", SwitchState.MAIN),
        (8, 10, TRACK_SE | TRACK_EW, "SW_8_10", SwitchState.DIVERGE),
    ]
    for (sx, sy, bits, sid, state) in crossover:
        tile = Tile(sx, sy, track_bits=bits)
        network.add_track(tile)
        switch = Switch(switch_id=sid, x=sx, y=sy)
        switch.position = state
        network.add_switch(switch)
        tile.switch_id = switch.switch_id


def _add_depot(network: RailwayNetwork) -> None:
    """Place the fan depot above the throat: a central lead rising from
    SW_LEAD to a junction that fans left/right into a six-turnout ladder,
    plus six perpendicular platform stubs in one aligned horizontal row.

    Platform tiles are placed here as plain track; they become station
    blocks in `_add_stations`. The lead and the four plain ladder connectors
    are blocked in `_add_blocks`; the ladder turnouts and the fan junction
    are switch boundaries (no block).
    """
    from enums import SwitchState

    # Lead (the depot "running line"): rise from SW_LEAD's north edge through
    # the lead-crossover switch SW_7_10 at (7,10) and thread up past the throat
    # to the ladder's near (right) end at (8,15). (7,10) is placed as a switch
    # by `_add_lead_crossover`, so the lead's plain tiles start at (7,11).
    for (x, y, bits) in [
        (7, 11, TRACK_NS),
        (7, 12, TRACK_NS),
        (7, 13, TRACK_SE),
        (8, 13, TRACK_NW),
        (8, 14, TRACK_NS),
        (8, 15, TRACK_SW),
    ]:
        network.add_track(Tile(x, y, track_bits=bits))

    # Plain ladder connectors (NW corners) between consecutive turnouts.
    for (x, y) in [(3, 10), (4, 11), (5, 12), (6, 13), (7, 14)]:
        network.add_track(Tile(x, y, track_bits=TRACK_NW))

    # Single-direction ladder: lead enters at the right end (SW_LAD5) and each
    # turnout peels a platform off going up-left. Every turnout is SE|EW — SE
    # carries the ladder through, EW (the 45° diagonal) eases the platform
    # branch — so none is a square corner. Default DIVERGE = ladder through.
    for k in range(6):
        ax, ay = 2 + k, 10 + k
        at = Tile(ax, ay, track_bits=TRACK_SE | TRACK_EW)
        network.add_track(at)
        sw = Switch(switch_id=f"SW_LAD{k}", x=ax, y=ay)
        sw.position = SwitchState.DIVERGE
        network.add_switch(sw)
        at.switch_id = sw.switch_id

    # Six platforms (vertical NE/SW stubs, 3 tiles each), bases aligned on
    # screen-row -9, each branching up-left off its ladder turnout.
    for (bx, by) in DEPOT_PLATFORM_BASES:
        network.add_track(Tile(bx, by, track_bits=TRACK_NE))         # BOT
        network.add_track(Tile(bx, by + 1, track_bits=TRACK_SW))     # MID
        network.add_track(Tile(bx - 1, by + 1, track_bits=TRACK_NE)) # TOP


# Each block: (block_id, positions, name, length_cm, speed_limit_kmh or None).
# Station blocks are created automatically by add_station (named
# `{platform_id}_BLOCK`) so they are NOT pre-created here.
#
# The S2↔S3 corridor is split into block A and block B per track, with the
# throat switches sitting between them (block B omits the switch tiles, just
# as the X1/X2 bracket blocks omit the scissors tile). The crossover now sits
# origin-side of the depot lead, so the two blocks no longer cover equal tile
# counts; length_cm is tunable and kept equal here for simplicity.
_CORRIDOR_BLOCK_CM = 110.0

# Uniform max speed (km/h) for demo_5.1: every train and every track block and
# platform runs at this cap. One constant so the whole line stays in sync.
_MAX_SPEED_KMH = 70.0

BLOCKS = [
    # Plain blocks bracketing X1
    ("BLK_S1_S_TO_X1", [(2, 2)], "S1-S → X1", 30.0, _MAX_SPEED_KMH),
    ("BLK_S1_N_TO_X1", [(1, 3)], "S1-N → X1", 30.0, _MAX_SPEED_KMH),
    ("BLK_X1_TO_S2_S", [(3, 3)], "X1 → S2-S", 30.0, _MAX_SPEED_KMH),
    ("BLK_X1_TO_S2_N", [(2, 4)], "X1 → S2-N", 30.0, _MAX_SPEED_KMH),
    # S2↔S3 corridor — equal-length split, throat switches are boundaries.
    ("BLK_S2_S_TO_S3_S_A", [(5, 5), (6, 5), (6, 6), (7, 6), (7, 7), (8, 7)],
     "S2-S → S3-S (A)", _CORRIDOR_BLOCK_CM, _MAX_SPEED_KMH),
    ("BLK_S2_S_TO_S3_S_B", [(9, 8), (9, 9), (10, 9), (10, 10)],
     "S2-S → S3-S (B)", _CORRIDOR_BLOCK_CM, _MAX_SPEED_KMH),
    ("BLK_S2_N_TO_S3_N_A", [(4, 6), (5, 6), (5, 7), (6, 7), (6, 8)],
     "S2-N → S3-N (A)", _CORRIDOR_BLOCK_CM, _MAX_SPEED_KMH),
    # Block B is split around the lead-crossover switch SW_8_10 at (8,10):
    # B_1 is the single tile before it, B_2 the two tiles after. The 110 cm
    # corridor length is split by tile count (1/3 + 2/3) so the total is kept.
    ("BLK_S2_N_TO_S3_N_B_1", [(8, 9)],
     "S2-N → S3-N (B1)", _CORRIDOR_BLOCK_CM / 3, _MAX_SPEED_KMH),
    ("BLK_S2_N_TO_S3_N_B_2", [(9, 10), (9, 11)],
     "S2-N → S3-N (B2)", _CORRIDOR_BLOCK_CM * 2 / 3, _MAX_SPEED_KMH),
    # Plain blocks bracketing X2 (shifted)
    ("BLK_S3_S_TO_X2", [(12, 12)], "S3-S → X2", 30.0, _MAX_SPEED_KMH),
    ("BLK_S3_N_TO_X2", [(11, 13)], "S3-N → X2", 30.0, _MAX_SPEED_KMH),
    ("BLK_X2_TO_S4_S", [(13, 13)], "X2 → S4-S", 30.0, _MAX_SPEED_KMH),
    ("BLK_X2_TO_S4_N", [(12, 14)], "X2 → S4-N", 30.0, _MAX_SPEED_KMH),
    # Depot lead (throat → ladder near-end) + the five plain ladder connectors
    # (the turnouts SW_LADk are switch boundaries, no block).
    ("BLK_DEPOT_LEAD", [(7, 11), (7, 12), (7, 13), (8, 13), (8, 14), (8, 15)],
     "Depot lead", 70.0, _MAX_SPEED_KMH),
    ("BLK_DEPOT_CT0", [(3, 10)], "Depot ladder 0", 20.0, _MAX_SPEED_KMH),
    ("BLK_DEPOT_CT1", [(4, 11)], "Depot ladder 1", 20.0, _MAX_SPEED_KMH),
    ("BLK_DEPOT_CT2", [(5, 12)], "Depot ladder 2", 20.0, _MAX_SPEED_KMH),
    ("BLK_DEPOT_CT3", [(6, 13)], "Depot ladder 3", 20.0, _MAX_SPEED_KMH),
    ("BLK_DEPOT_CT4", [(7, 14)], "Depot ladder 4", 20.0, _MAX_SPEED_KMH),
]


def _add_blocks(network: RailwayNetwork) -> None:
    """Register every block in BLOCKS via create_segment_with_tracks."""
    for (block_id, positions, name, length_cm, speed) in BLOCKS:
        kwargs = {
            # The tuple's id is this scene's BLOCK id: saved routes, tests and
            # hardware bindings already name it, so it is pinned and the
            # segment takes the matching geometry-axis id (see entity_ids).
            "block_id": block_id,
            "segment_id": segment_id_for_block(block_id),
            "positions": positions,
            "name": name,
            "length_cm": length_cm,
        }
        if speed is not None:
            kwargs["speed_limit_kmh"] = speed
        network.create_segment_with_tracks(**kwargs)


# One-way travel policy. The S2<->S3 corridor and the crossover approaches on
# the N (return) track each carry traffic in a single direction, so marking
# them one-way (a) lets trains pack tight there with no head-on risk and (b)
# lets the dispatcher's "stay back one switch" rule keep the bidirectional
# junction blocks (the crossovers and depot lead) clear: a truncated authority
# may pack right up to a one-way block but holds back from a two-way block that
# sits straight off a switch. For all four blocks the desired flow is the
# block's own "forward" traversal (verified against each block's ordered_tiles):
#   BLK_S2_S_TO_S3_S_A / _B : S corridor, outbound (S2-S -> S3-S)
#   BLK_X1_TO_S2_N          : N return leg, S2-N -> X1
#   BLK_S3_N_TO_X2          : N return leg, X2 -> S3-N
_ONE_WAY_FORWARD_BLOCKS = [
    "BLK_S2_S_TO_S3_S_A",
    "BLK_S2_S_TO_S3_S_B",
    "BLK_X1_TO_S2_N",
    "BLK_S3_N_TO_X2",
]


def _set_one_way_tracks(network: RailwayNetwork) -> None:
    """Restrict the corridor + crossover-approach blocks to one-way travel."""
    for block_id in _ONE_WAY_FORWARD_BLOCKS:
        network.blocks[block_id].allowed_travel_directions = {"forward"}


# Each station entry: (station_id, name, first_tile, extra_tiles, platform_side)
# Island stations (S1, S4): N-track platform on its SW side, S-track on its NE
#   side — both platforms face inward toward the other track.
# Side-platform stations (S2, S3): N-track platform on NE side, S-track on SW
#   side — both face outward.
# Depot platforms (NE/SW group): platform_side is tunable; rendering is the
#   only thing it affects.
STATIONS = [
    ("STN_1", "Station 1", [
        ("PLT_S1_S", "S1 South", (1, 0), [(1, 1), (2, 1)], "right"),
        ("PLT_S1_N", "S1 North", (0, 1), [(0, 2), (1, 2)], "left"),
    ]),
    ("STN_2", "Station 2", [
        ("PLT_S2_S", "S2 South", (4, 3), [(4, 4), (5, 4)], "left"),
        ("PLT_S2_N", "S2 North", (3, 4), [(3, 5), (4, 5)], "right"),
    ]),
    ("STN_3", "Station 3", [
        ("PLT_S3_S", "S3 South", (11, 10), [(11, 11), (12, 11)], "left"),
        ("PLT_S3_N", "S3 North", (10, 11), [(10, 12), (11, 12)], "right"),
    ]),
    ("STN_4", "Station 4", [
        ("PLT_S4_S", "S4 South", (14, 13), [(14, 14), (15, 14)], "right"),
        ("PLT_S4_N", "S4 North", (13, 14), [(13, 15), (14, 15)], "left"),
    ]),
    ("STN_DEPOT", "Depot", [
        ("PLT_DEPOT_1", "Depot 1", (1, 10), [(1, 11), (0, 11)], "right"),
        ("PLT_DEPOT_2", "Depot 2", (2, 11), [(2, 12), (1, 12)], "right"),
        ("PLT_DEPOT_3", "Depot 3", (3, 12), [(3, 13), (2, 13)], "right"),
        ("PLT_DEPOT_4", "Depot 4", (4, 13), [(4, 14), (3, 14)], "right"),
        ("PLT_DEPOT_5", "Depot 5", (5, 14), [(5, 15), (4, 15)], "right"),
        ("PLT_DEPOT_6", "Depot 6", (6, 15), [(6, 16), (5, 16)], "right"),
    ]),
]


def _add_stations(network: RailwayNetwork) -> None:
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
                top_speed_kmh=_MAX_SPEED_KMH,
            )
            for extra in extra_xys:
                network.extend_station(platform.platform_id, [extra])
            platform_ids.append(platform.platform_id)
        network.create_station_group(
            station_id=group_id,
            name=group_name,
            platform_ids=platform_ids,
        )


def _add_tunnels(network: RailwayNetwork) -> None:
    """Close the NE corridor into a loop by tunneling S1↔S4 on each track.

    Endpoints are the outermost dead-end edges of the S/N track endpoints
    (S4 ends shifted by (+4, +4) from demo_4):
      S track: (1, 0, W) ↔ (15, 14, N)
      N track: (0, 1, W) ↔ (14, 15, N)
    """
    network.add_tunnel((1, 0, "W"), (15, 14, "N"))
    network.add_tunnel((0, 1, "W"), (14, 15, "N"))


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the Demo 5.1 network from scratch and return it.

    The network is identical to Demo 5 (the demo_5.1 change is only in
    setup_demo_train); this mirrors the name demo_main.create_comprehensive_demo
    so bootstrap can load the scene by module name without other edits.
    """
    print("\n" + "=" * 70)
    print("Demo 5.1: Four-Station Loop + Depot, trains start stabled (best-effort)")
    print("=" * 70)

    network = RailwayNetwork()

    _add_plain_tracks(network)
    _add_crossovers(network)
    _add_throat(network)
    _add_lead_crossover(network)
    _add_depot(network)
    _add_blocks(network)
    _set_one_way_tracks(network)
    _add_stations(network)
    _add_tunnels(network)

    print(
        f"  ✓ tracks={len(network.tracks)}  "
        f"blocks={len(network.blocks)}  "
        f"platforms={len(network.platforms)}  "
        f"switches={len(network.switches)}"
    )

    return network


def setup_demo_train(
    network: RailwayNetwork,
    path_dispatcher: PathDispatcher,
    sim_controller,
):
    """Place the five demo trains stabled in the depot and register the
    shared CIRCULAR route.

    Mirrors the name of demo_main.setup_demo_train. The route is the same
    Demo 5 loop; the demo_5.1 change is the fleet — five trains seated one per
    depot platform (1-5) at the platform mouth facing EAST, rather than four
    trains seated on their loop platforms. Each train's planning task is
    phase-shifted to a different loop stop so they fan out as they path out of
    the depot.
    """
    from train_route import (
        Route,
        RouteMode,
        RouteTask,
        PlatformTarget,
        StationTarget,
    )
    from runtime_payloads import AcquisitionMode
    from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE

    # ---- Shared route (CIRCULAR, 12 tasks: stop+dwell × 6) ----
    route = Route(
        route_id="R_DEMO5_LOOP",
        name="Demo 5.1: Four-Station Loop",
        mode=RouteMode.CIRCULAR,
        acquisition_mode=AcquisitionMode.BEST_EFFORT,
        is_active=True,
    )
    tasks = [
        # S1 (any platform) — terminus, train reverses here
        RouteTask(task_kind="stop",  task_location=StationTarget("STN_1"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=StationTarget("STN_1"),
                  task_time=4.0),
        # S2-S platform (outbound leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S2_S"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S2_S"),
                  task_time=2.0),
        # S3-S platform (outbound leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S3_S"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S3_S"),
                  task_time=2.0),
        # S4 (any platform) — terminus
        RouteTask(task_kind="stop",  task_location=StationTarget("STN_4"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=StationTarget("STN_4"),
                  task_time=5.0),
        # S3-N platform (return leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S3_N"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S3_N"),
                  task_time=2.0),
        # S2-N platform (return leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S2_N"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S2_N"),
                  task_time=2.0),
    ]
    for t in tasks:
        route.add_task(t)
    sim_controller.route_executor.register_route(route)

    # ---- Secondary route: 4 stops down the S track only ----
    # Registered but unassigned — the user can wire it to a train via the UI.
    s_line_route = Route(
        route_id="R_DEMO5_S_LINE",
        name="Demo 5.1: S-Track 4-Stop",
        mode=RouteMode.CIRCULAR,
        acquisition_mode=AcquisitionMode.BEST_EFFORT,
        is_active=True,
    )
    for plat in ("PLT_S1_S", "PLT_S2_S", "PLT_S3_S", "PLT_S4_S"):
        s_line_route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(plat),
                task_direction="either",
            )
        )
    sim_controller.route_executor.register_route(s_line_route)

    # ---- Shared speed profile ----
    demo_profile = SpeedProfile(
        profile_id="demo5_profile",
        name="Demo 5.1 Profile",
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
    network.speed_profiles.register(demo_profile)

    # ---- Stable the five trains in the depot as a pull-out "parade" ----
    # (train_id, starting_block, initial_direction, planning-task-index, start_tile)
    # Each train sits on a depot platform's MOUTH tile (the base, where the stub
    # meets its ladder turnout) facing EAST, so its nose points out toward the
    # ladder and its body trails up the stub. The planning index is an even
    # ("stop") task in the shared loop; on tick 0 each train plans a path out of
    # the depot to its first stop.
    #
    # The depot necks down to ONE lead that joins the N-track return leg PAST the
    # one-way throat, so a depot train can only reach an S-track (outbound) stop
    # by detouring to a terminus and reversing. If two trains aim for the S1
    # reversal pocket at once — one dwelling at S1, one crossing X1 to reverse
    # through it — they meet head-on over the X1 scissors and deadlock. To avoid
    # that, the trains are assigned consecutive loop stops in the loop's travel
    # direction from the depot exit, ordered so the platform nearest the lead
    # (D5) pulls out first: D5→S3_N, D4→S2_N, D3→S1, D2→S2_S, D1→S3_S. They then
    # follow each other onto the line like a real depot pull-out instead of
    # converging on S1. Depot platform 6 (nearest the throat) is left empty.
    # Base tiles match DEPOT_PLATFORM_BASES = (1+k, 10+k).
    train_specs = [
        ("T1", "PLT_DEPOT_1_BLOCK", "EAST", 4,  (1, 10)),  # D1 (last out) → PLT_S3_S
        ("T2", "PLT_DEPOT_2_BLOCK", "EAST", 2,  (2, 11)),  # D2 → PLT_S2_S
        ("T3", "PLT_DEPOT_3_BLOCK", "EAST", 0,  (3, 12)),  # D3 → STN_1
        ("T4", "PLT_DEPOT_4_BLOCK", "EAST", 10, (4, 13)),  # D4 → PLT_S2_N
        ("T5", "PLT_DEPOT_5_BLOCK", "EAST", 8,  (5, 14)),  # D5 (first out) → PLT_S3_N
    ]

    import time as _time
    from route_executor import RouteExecutionState, RouteRunStatus
    from train_route import RouteTaskRun

    now = _time.time()
    for (train_id, block_id, direction, phase_idx, start_tile) in train_specs:
        train = sim_controller.add_train(
            train_id=train_id,
            starting_block_id=block_id,
            initial_direction=direction,
            max_speed_kmh=_MAX_SPEED_KMH,
            length_cm=50.0,
            train_type="LOCOMOTIVE",
            initial_cab="A",
            start_tile=start_tile,
        )
        train.speed_profile_id = demo_profile.profile_id
        route.assign_train(train.train_id)

        # Pre-create the per-train RouteExecutionState with the prior loop
        # tasks marked done so this train's next planning task is the "stop"
        # at phase_idx. The train is stabled in the depot (not yet at that
        # stop), so on tick 0 the executor plans a path out of the depot to it.
        executor = sim_controller.route_executor
        exec_state = RouteExecutionState(
            train_id=train_id,
            route_id=route.route_id,
            started_at=now,
            run_number=executor._allocate_run_number(),
            current_route_status=RouteRunStatus.RUNNING,
        )
        for i in range(phase_idx):
            exec_state.task_runs.append(
                RouteTaskRun(
                    task_index=i,
                    finished_at=now,
                    # The operating clock reads 0.0 until the first
                    # tick, and this prefill runs at build time.
                    finished_at_operating=0.0,
                )
            )
        exec_state.task_runs.append(RouteTaskRun(task_index=phase_idx))
        executor.execution_states[train_id] = exec_state

        controller = sim_controller.get_or_create_controller(train.train_id)
        controller.commands.enqueue_command("power_on")

    print(f"  ✓ Registered route {route.route_id!r} with {len(route.tasks)} tasks")
    print(f"  ✓ Stabled 5 trains in the depot (phase-shifted): {[s[0] for s in train_specs]}")
