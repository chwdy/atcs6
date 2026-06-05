"""One-off scan: classify every tile in every 'previous scene' by track type,
focused on multi-path tiles (the ones a 'one segment per tile' rule touches)."""
import contextlib
import glob
import importlib
import io
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())
from track import TrackSegment, is_square_corner_switch, FORBIDDEN_TRACK_BITS  # noqa


def kind(tb: int) -> str:
    """Finer-grained label than track_type for the multi-path question."""
    t = TrackSegment(0, 0, track_bits=tb)
    n = t.num_paths
    if n <= 1:
        return "single/empty"
    tt = t.track_type.name
    if tt == "DIAMOND":
        if tb == 3:
            return "DIAMOND-cross(NS+EW=3)"
        return f"DIAMOND-parallel(bits={tb})"  # 24 or 36, benign double-track
    if tt == "SWITCH":
        return "SWITCH(square-corner)" if is_square_corner_switch(tb) else "SWITCH"
    return tt  # WYE / SCISSORS / DOUBLE_CROSS / COMPLEX / FORBIDDEN


def tally(name, tiles):
    """tiles: iterable of (x, y, track_bits)."""
    by = Counter()
    multi = []
    for x, y, tb in tiles:
        k = kind(tb)
        by[k] += 1
        if TrackSegment(0, 0, track_bits=tb).num_paths >= 2:
            multi.append((x, y, tb, k))
    multi_by = Counter(k for *_, k in multi)
    print("=" * 72)
    print(f"{name}  |  {sum(by.values())} tiles  |  {len(multi)} multi-path")
    if multi_by:
        print("  multi-path breakdown:", dict(multi_by))
    return by, multi_by


grand = Counter()

# --- 1. Saved layouts (JSON) ---
for f in sorted(glob.glob("layouts/*.json")):
    d = json.load(open(f))
    tracks = d.get("snapshot", {}).get("tracks", [])
    _, mb = tally(f"LAYOUT {f}", [(t.get("x"), t.get("y"), t.get("track_bits", 0)) for t in tracks])
    grand.update(mb)

# --- 2. Journey scenes (build network, scan network.tracks) ---
SCENES = [
    ("demo_scene", "DemoScene"),
    ("demo4_scene", "Demo4Scene"),
    ("demo_4_scene", "Demo4Scene"),
    ("demo_5_scene", "Demo5Scene"),
    ("empty_scene", "EmptyScene"),
    ("dock_turnaround_scene", "DockTurnaroundScene"),
    ("north_station_turnaround_scene", "NorthStationTurnaroundScene"),
    ("north_station_caution_turnaround_scene", "NorthStationCautionTurnaroundScene"),
    ("east_branch_terminal_scene", "EastBranchTerminalScene"),
    ("tunnel_scene", "TunnelScene"),
    ("tunnel_wn_scene", "TunnelWNScene"),
]
for mod_name, cls_name in SCENES:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mod = importlib.import_module(f"tests.journey.scenes.{mod_name}")
            cls = getattr(mod, cls_name)
            fx = cls().build()
            net = fx.network
            tiles = [(t.x, t.y, t.track_bits) for t in net.tracks.values()]
        _, mb = tally(f"SCENE {mod_name}.{cls_name}", tiles)
        grand.update(mb)
    except Exception as e:
        print("=" * 72)
        print(f"SCENE {mod_name}.{cls_name}  |  BUILD FAILED: {type(e).__name__}: {e}")

print("=" * 72)
print("GRAND TOTAL multi-path tiles across all scenes (by kind):")
for k, c in grand.most_common():
    print(f"  {c:4d}  {k}")
