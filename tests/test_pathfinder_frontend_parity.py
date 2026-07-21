"""Pin the frontend and backend pathfinders to agree on segment binding
through switch / crossing tiles.

The two pathfinders solve different problems — `frontend/src/lib/pathfinders.ts`
provides editor preview, `railway_pathfinder.py` provides operational
train routing — and live in different languages. They must still agree
on one shared rule: entering a tile on one segment of a switch limits
the valid exits to that segment's paired edge.

Drift on this rule was the original motivation. The previous frontend
`ExistingTrackPathfinder` flattened every segment into a single edge
set, which let a path "cheat" across disconnected segments inside a
crossing or switch. The fix added `ConstructionPathfinder.getExitsForEntry`
in `frontend/src/lib/pathfinders.ts`. This file pins that helper's contract
against the backend's `Tile.get_exits(entry, use_switch_state=False)`
so future changes on either side trip CI before drift accumulates.

Covers:
  * Primitive agreement on every `(track_bits, entry edge)` combination.
  * BFS-level agreement on a crossing fixture where segment binding
    actually changes the reachable set (the soup model would say all
    four arms are reachable; segment binding correctly isolates NS
    traffic from EW traffic).

Does not cover:
  * The literal JS BFS code path — running it would require Node. The
    Python mirror in this file tracks `pathfinders.ts` line-for-line and
    must be updated whenever the JS BFS structure changes.
"""

from collections import deque
from typing import Optional, Set, Tuple

from network import RailwayNetwork
from railway_pathfinder import RailwayPathfinder
from track import Tile


# ---------------------------------------------------------------- conversion

# The JS frontend uses full-word edges ('NORTH'/'EAST'/'SOUTH'/'WEST');
# the backend stores edges as single letters ('N'/'E'/'S'/'W'). This pair
# of tables bridges them at the JS/backend boundary inside the test.
SHORT_TO_LONG = {'N': 'NORTH', 'E': 'EAST', 'S': 'SOUTH', 'W': 'WEST'}
LONG_TO_SHORT = {v: k for k, v in SHORT_TO_LONG.items()}


# --------------------------------------------------------------- JS primitive

# Direct translation of the SEGMENTS array in
# `ConstructionPathfinder.getExitsForEntry` (frontend/src/lib/pathfinders.ts).
# Each entry is (track_bits bit value, edge_a, edge_b): entering edge_a
# yields edge_b as the segment's paired exit, and vice-versa. Keep this
# table aligned with the JS source — that is the whole point of the file.
JS_SEGMENTS = [
    (1,  'NORTH', 'SOUTH'),
    (2,  'EAST',  'WEST'),
    (4,  'NORTH', 'EAST'),
    (8,  'NORTH', 'WEST'),
    (16, 'SOUTH', 'EAST'),
    (32, 'SOUTH', 'WEST'),
]


def js_get_exits_for_entry(track_bits: int, entry: str) -> Set[str]:
    """Python port of `ConstructionPathfinder.getExitsForEntry`. Returns
    the set of valid exit edges (full-word form) for the given entry."""
    exits: Set[str] = set()
    for bit, a, b in JS_SEGMENTS:
        if (track_bits & bit) == 0:
            continue
        if a == entry:
            exits.add(b)
        elif b == entry:
            exits.add(a)
    return exits


# -------------------------------------------------------------- primitive test

def test_segment_binding_primitive_agrees_with_backend():
    """For every non-empty `track_bits` (1..63) and every entry edge,
    the frontend `getExitsForEntry` must return the same exit set as the
    backend's `Tile.get_exits(entry, use_switch_state=False)`.

    If this fails, the segment-binding rule has drifted between the JS
    `SEGMENTS` table and the backend `BIT_TO_EDGES` table.
    """
    for track_bits in range(1, 64):
        track = Tile(x=0, y=0, track_bits=track_bits)
        for entry_long in ('NORTH', 'EAST', 'SOUTH', 'WEST'):
            entry_short = LONG_TO_SHORT[entry_long]
            backend_exits = {
                SHORT_TO_LONG[e]
                for e in track.get_exits(entry_short, use_switch_state=False)
            }
            frontend_exits = js_get_exits_for_entry(track_bits, entry_long)
            assert backend_exits == frontend_exits, (
                f"track_bits={track_bits}, entry={entry_long!r}: "
                f"backend={sorted(backend_exits)}, "
                f"frontend={sorted(frontend_exits)}"
            )


# ----------------------------------------------------------- BFS scenario test

# Map JS-style full-word exit edge → (dx, dy, neighbor entry edge).
# Matches the `neighbors` table inside `findPathOnExistingTracks`.
# Coordinate convention: N = +y (same on both sides; see DIRECTION_OFFSETS
# in enums.py and the JS neighbors array).
JS_EDGE_TO_DELTA = {
    'NORTH': (0,  1, 'SOUTH'),
    'EAST':  (1,  0, 'WEST'),
    'SOUTH': (0, -1, 'NORTH'),
    'WEST':  (-1, 0, 'EAST'),
}


def _js_mirror_reachable_tiles(
    network: RailwayNetwork, start_pos: Tuple[int, int]
) -> Set[Tuple[int, int]]:
    """Python mirror of `ExistingTrackPathfinder.findPathOnExistingTracks`
    (frontend/src/lib/pathfinders.ts) after the entry-aware fix. Returns the set
    of (x, y) reachable from `start_pos`.

    Same BFS structure as the JS: state carries the entry edge into the
    current tile; segment binding is respected via the JS-side
    `getExitsForEntry` primitive; visited keys include the entry edge."""
    if network.get_track(*start_pos) is None:
        return set()
    reachable: Set[Tuple[int, int]] = {start_pos}
    visited: Set[Tuple[int, int, Optional[str]]] = set()
    queue: deque = deque([(start_pos[0], start_pos[1], None)])
    while queue:
        x, y, entry = queue.popleft()
        key = (x, y, entry)
        if key in visited:
            continue
        visited.add(key)
        track = network.get_track(x, y)
        if track is None:
            continue
        # Start tile has no entry — every edge is fair game. Every other
        # tile respects segment binding via the JS-side primitive.
        if entry is None:
            exits = {SHORT_TO_LONG[e] for e in track.edges}
        else:
            exits = js_get_exits_for_entry(track.track_bits, entry)
        for exit_edge in exits:
            dx, dy, n_entry = JS_EDGE_TO_DELTA[exit_edge]
            nx, ny = x + dx, y + dy
            neighbor = network.get_track(nx, ny)
            if neighbor is None:
                continue
            neighbor_edges_long = {SHORT_TO_LONG[e] for e in neighbor.edges}
            if n_entry not in neighbor_edges_long:
                continue
            reachable.add((nx, ny))
            queue.append((nx, ny, n_entry))
    return reachable


def _project_tiles_to_blocks(
    network: RailwayNetwork,
    tiles: Set[Tuple[int, int]],
    exclude_block: str,
) -> Set[str]:
    """Map a tile reachability set to the set of OTHER block IDs touched
    by those tiles, so the result can be compared against the backend's
    `get_reachable_endpoints` (which excludes the start block)."""
    blocks: Set[str] = set()
    for x, y in tiles:
        track = network.get_track(x, y)
        if track is None:
            continue
        bid = track.block_id
        if bid and bid != exclude_block:
            blocks.add(bid)
    return blocks


def test_bfs_reachability_respects_crossing_segment_isolation():
    """A crossing tile (`track_bits=3` = NS + EW) has two perpendicular
    segments with no shared edge. NS traffic and EW traffic CANNOT
    transition between each other inside the tile. Both the frontend
    mirror BFS and the backend `get_reachable_endpoints` must reflect
    this isolation; the previous frontend "soup" code would have said
    all four arms are mutually reachable.

    Layout (N = +y):

            A   (1, 2)   NS
            |
        C - X - B        crossing X at (1, 1); C=(0,1), B=(2,1) on EW
            |
            D   (1, 0)   NS
    """
    network = RailwayNetwork()
    # Crossing at (1, 1) — left out of any block here; the four arms are
    # separate blocks so the test exercises path isolation through the crossing.
    network.add_track(Tile(x=1, y=1, track_bits=3))
    # NS arms: A north of crossing, D south of crossing.
    network.add_track(Tile(x=1, y=2, track_bits=1))
    network.add_track(Tile(x=1, y=0, track_bits=1))
    # EW arms: B east of crossing, C west of crossing.
    network.add_track(Tile(x=2, y=1, track_bits=2))
    network.add_track(Tile(x=0, y=1, track_bits=2))

    network.create_segment_with_tracks(segment_id="A", positions=[(1, 2)])
    network.create_segment_with_tracks(segment_id="D", positions=[(1, 0)])
    network.create_segment_with_tracks(segment_id="B", positions=[(2, 1)])
    network.create_segment_with_tracks(segment_id="C", positions=[(0, 1)])

    pf = RailwayPathfinder(network)

    # From A on the NS arm: backend reaches only D.
    assert set(pf.get_reachable_endpoints("A", include_stations=False)) == {"D"}
    # Frontend mirror from A's tile (1, 2): same answer.
    tiles_a = _js_mirror_reachable_tiles(network, (1, 2))
    blocks_a = _project_tiles_to_blocks(network, tiles_a, exclude_block="A")
    assert blocks_a == {"D"}, (
        f"frontend mirror reachable from A = {blocks_a}, expected {{'D'}}"
    )

    # From B on the EW arm: backend reaches only C.
    assert set(pf.get_reachable_endpoints("B", include_stations=False)) == {"C"}
    tiles_b = _js_mirror_reachable_tiles(network, (2, 1))
    blocks_b = _project_tiles_to_blocks(network, tiles_b, exclude_block="B")
    assert blocks_b == {"C"}, (
        f"frontend mirror reachable from B = {blocks_b}, expected {{'C'}}"
    )
