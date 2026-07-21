#!/usr/bin/env python3
"""json_to_scene.py — bake a saved layout JSON into a Python scene module.

Usage:
    python json_to_scene.py INPUT.json OUTPUT.py [--scene-id NAME] [--verify]

Reads a layout file (the same JSON the editor saves under ``layouts/``) and emits
a demo_6-style scene module: readable ``TILES`` / ``SWITCHES`` / ``STATIONS`` /
``BLOCKS`` / ``TUNNELS`` / ``SIGNALS`` data lists plus the standard builder
functions, ``SCENE_ID``, ``create_comprehensive_demo()`` and a
``setup_demo_train()`` stub.

How it works
------------
The JSON snapshot is loaded through the app's own canonical loader
(``RailwayNetwork.from_dict``) and then introspected to produce the data lists.
That keeps the output faithful to exactly what loading the layout in-app would
build. ``--verify`` rebuilds the generated module and the ``from_dict`` load of
the same JSON and asserts the two networks are equivalent.

Scope / limitations
-------------------
* This bakes the LAYOUT only. Train placement and routes are not part of the
  layout JSON, so ``setup_demo_train()`` is a no-op stub — add scene-specific
  train logic by hand if needed (see demo_6.py for an example).
* ``dead_end_ports`` are not emitted: the network re-derives them from the full
  track geometry (verified identical to the stored values).
* Switches are emitted as ``(switch_id, x, y)``. If a switch carries non-default
  geometry/metadata (lengths, radii, name, ...), that is NOT captured — the
  converter prints a warning listing those switches.
"""

import argparse
import json
import sys

from network import RailwayNetwork


# Track-bit value -> constant name, high bit last (matches track.py).
BIT_NAMES = [
    (1, "TRACK_NS"),
    (2, "TRACK_EW"),
    (4, "TRACK_NE"),
    (8, "TRACK_NW"),
    (16, "TRACK_SE"),
    (32, "TRACK_SW"),
]

# Switch fields that, when non-zero / non-empty, mean the (id, x, y) tuple is
# lossy. Used only to warn the user.
_SWITCH_DEFAULTS = {
    "main_length_cm": 0.0,
    "diverge_length_cm": 0.0,
    "main_radius_cm": None,
    "diverge_radius_cm": None,
    "switch_type": "standard",
    "name": "",
    "notes": "",
    "disabled": False,
    "manual": False,
}


def bits_expr(bits: int) -> str:
    """Render a track-bit mask as an OR of TRACK_* constants (or ``0``)."""
    parts = [name for val, name in BIT_NAMES if bits & val]
    return " | ".join(parts) if parts else "0"


def _side_str(platform_side) -> str:
    """PlatformSide enum (or string) -> the string add_station accepts."""
    return getattr(platform_side, "value", platform_side)


def _xy(t) -> str:
    return f"({t[0]}, {t[1]})"


def _xy_list(points) -> str:
    return "[" + ", ".join(_xy(p) for p in points) + "]"


# --------------------------------------------------------------------------- #
# Data-list generators (each returns the text for one `NAME = [ ... ]` block)
# --------------------------------------------------------------------------- #
def gen_tiles(net) -> str:
    lines = ["TILES = ["]
    for (x, y) in sorted(net.tracks.keys()):
        t = net.tracks[(x, y)]
        comment = f"  # {t.switch_id}" if t.switch_id else ""
        lines.append(f"    ({x}, {y}, {bits_expr(t.track_bits)}),{comment}")
    lines.append("]")
    return "\n".join(lines)


def gen_switches(net) -> tuple[str, list[str]]:
    warnings = []
    lines = ["SWITCHES = ["]
    for sid in sorted(net.switches.keys(), key=lambda s: (net.switches[s].x, net.switches[s].y)):
        sw = net.switches[sid]
        lines.append(f"    ({sid!r}, {sw.x}, {sw.y}),")
        for field, default in _SWITCH_DEFAULTS.items():
            if getattr(sw, field, default) != default:
                warnings.append(sid)
                break
    lines.append("]")
    return "\n".join(lines), warnings


def gen_stations(net) -> tuple[str, list[str]]:
    grouped = set()
    lines = ["STATIONS = ["]
    for group in net.station_groups.values():
        lines.append(f"    ({group.station_id!r}, {group.name!r}, [")
        for pid in group.platform_ids:
            grouped.add(pid)
            p = net.platforms.get(pid)
            if p is None:
                lines.append(f"        # MISSING platform {pid!r}")
                continue
            positions = [tuple(pt) for pt in p.positions]
            first, extras = positions[0], positions[1:]
            lines.append(
                f"        ({pid!r}, {p.name!r}, {_xy(first)}, {_xy_list(extras)}, "
                f"{_side_str(p.platform_side)!r}, {p.length_cm}, {p.top_speed_kmh}),"
            )
        lines.append("    ]),")
    lines.append("]")
    return "\n".join(lines), grouped


def gen_platforms(net, grouped: set) -> str:
    """Emit the platforms that belong to no station group.

    Grouped platforms are created by ``_add_stations``; these ungrouped ones
    are created directly by ``_add_platforms`` with the same anchor-tile +
    extensions construction but no ``create_station_group`` call. Each still
    auto-creates its ``{platform_id}_BLOCK``.
    """
    lines = ["PLATFORMS = ["]
    for pid in sorted(net.platforms):
        if pid in grouped:
            continue
        p = net.platforms[pid]
        positions = [tuple(pt) for pt in p.positions]
        first, extras = positions[0], positions[1:]
        # The auto-created station block name is reproduced verbatim: a platform
        # renamed after creation keeps its original id-based block name, which
        # ``add_station(name=...)`` would not regenerate.
        block = net.blocks.get(f"{pid}_BLOCK")
        block_name = block.name if block is not None else ""
        lines.append(
            f"    ({pid!r}, {p.name!r}, {_xy(first)}, {_xy_list(extras)}, "
            f"{_side_str(p.platform_side)!r}, {p.length_cm}, {p.top_speed_kmh}, "
            f"{block_name!r}),"
        )
    lines.append("]")
    return "\n".join(lines)


def gen_blocks(net) -> str:
    platform_block_ids = {f"{pid}_BLOCK" for pid in net.platforms}
    lines = ["BLOCKS = ["]
    for bid in sorted(net.blocks.keys()):
        if bid in platform_block_ids:
            continue  # auto-created by add_station
        b = net.blocks[bid]
        if len(b.segments) > 1:
            # A block that groups several segments is baked as its member
            # segments — each as its own block, the default one-feed-per-segment
            # layout — plus a BLOCK_GROUPS entry that folds them back together.
            # Membership here comes from each segment's own tile order, because
            # every member tile points at the grouped block, not the segment.
            for seg in b.segments:
                positions = [(e.x, e.y) for e in seg.ordered_tiles]
                lines.append(
                    f"    ({seg.segment_id!r}, {seg.name!r}, "
                    f"{_xy_list(positions)}, {seg.length_cm}, "
                    f"{seg.speed_limit_kmh}),"
                )
            continue
        # Editor-authored blocks serialize with empty ``ordered_tiles`` and
        # carry their membership on the tracks (``track.segment_id``).
        # ``get_block_positions`` reads that track-derived membership, so it is
        # the correct source for every block regardless of how it was authored.
        positions = net.get_block_positions(bid)
        lines.append(
            f"    ({bid!r}, {b.name!r}, {_xy_list(positions)}, "
            f"{b.length_cm}, {b.speed_limit_kmh}),"
        )
    lines.append("]")
    return "\n".join(lines)


def gen_tunnels(net) -> str:
    lines = ["TUNNELS = ["]
    for tp in net.tunnels_to_list():
        a, b = tp["a"], tp["b"]
        lines.append(
            f"    (({a['x']}, {a['y']}, {a['edge']!r}), "
            f"({b['x']}, {b['y']}, {b['edge']!r})),"
        )
    lines.append("]")
    return "\n".join(lines)


def gen_signals(snapshot) -> str:
    signals = snapshot.get("signals") or {}
    lines = ["SIGNALS = ["]
    for sig in signals.values():
        lines.append(f"    {sig!r},")
    lines.append("]")
    return "\n".join(lines)


def gen_block_groups(net) -> str:
    """Emit each block that groups several segments onto one power feed.

    Grouping is an operator decision about shared power wiring (design_new.md
    "Block grouping"); it is not re-derivable from geometry, so it must be baked
    explicitly. ``gen_blocks`` emits every member segment as its own block — the
    default one-feed-per-segment layout — and this emits the fold that turns
    each group into the single block it describes.
    """
    lines = ["BLOCK_GROUPS = ["]
    for bid in sorted(net.blocks.keys()):
        b = net.blocks[bid]
        if len(b.segments) < 2:
            continue
        lines.append(f"    ({bid!r}, {b.name!r}, {list(b.segment_ids)!r}),")
    lines.append("]")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Static builder functions emitted verbatim into every generated scene.
# --------------------------------------------------------------------------- #
_BUILDERS = '''
def _add_tracks(network: RailwayNetwork) -> None:
    """Place every baked track tile (dead-end ports are re-derived from geometry)."""
    for (x, y, bits) in TILES:
        network.add_track(Tile(x, y, track_bits=bits))


def _add_switches(network: RailwayNetwork) -> None:
    """Create the Switch entity for each switch tile and link the tile to it."""
    for (switch_id, x, y) in SWITCHES:
        network.add_switch(Switch(switch_id=switch_id, x=x, y=y))
        track = network.get_track(x, y)
        if track is not None:
            track.switch_id = switch_id


def _add_stations(network: RailwayNetwork) -> None:
    """Build each platform from its anchor tile + extensions, then group them.

    add_station auto-creates one ``{platform_id}_BLOCK`` per platform, so those
    blocks are not listed in BLOCKS.
    """
    for (group_id, group_name, platforms) in STATIONS:
        platform_ids = []
        for (platform_id, name, first_tile, extra_tiles, side, length_cm, top_speed_kmh) in platforms:
            platform = network.add_station(
                x=first_tile[0],
                y=first_tile[1],
                station_id=platform_id,
                name=name,
                length_cm=length_cm,
                platform_side=side,
                top_speed_kmh=top_speed_kmh,
            )
            for extra in extra_tiles:
                network.extend_station(platform.platform_id, [extra])
            platform_ids.append(platform.platform_id)
        network.create_station_group(
            station_id=group_id,
            name=group_name,
            platform_ids=platform_ids,
        )


def _add_platforms(network: RailwayNetwork) -> None:
    """Build platforms that belong to no station group.

    Same anchor-tile + extensions construction as ``_add_stations`` but with no
    ``create_station_group`` call. Each platform still auto-creates its
    ``{platform_id}_BLOCK``.
    """
    for (platform_id, name, first_tile, extra_tiles, side, length_cm, top_speed_kmh, block_name) in PLATFORMS:
        platform = network.add_station(
            x=first_tile[0],
            y=first_tile[1],
            station_id=platform_id,
            name=name,
            length_cm=length_cm,
            platform_side=side,
            top_speed_kmh=top_speed_kmh,
        )
        for extra in extra_tiles:
            network.extend_station(platform.platform_id, [extra])
        station_block = network.blocks.get(f"{platform.platform_id}_BLOCK")
        if station_block is not None:
            station_block.name = block_name


def _add_blocks(network: RailwayNetwork) -> None:
    """Create the non-platform blocks from their baked tile lists."""
    for (block_id, name, positions, length_cm, speed_limit_kmh) in BLOCKS:
        network.create_segment_with_tracks(
            segment_id=block_id,
            positions=positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
        )


def _add_tunnels(network: RailwayNetwork) -> None:
    """Join the baked tunnel endpoint pairs."""
    for (a, b) in TUNNELS:
        network.add_tunnel(a, b)


def _add_signals(network: RailwayNetwork) -> None:
    """Rebuild signals via the canonical Signal.from_dict loader."""
    if not SIGNALS:
        return
    from railway_signal import Signal
    for sig in SIGNALS:
        signal = Signal.from_dict(sig)
        network.signals[signal.signal_id] = signal
        track = network.get_track(*signal.position)
        if track is not None and signal.signal_id not in track.signal_ids:
            track.signal_ids.append(signal.signal_id)


def _add_block_groups(network: RailwayNetwork) -> None:
    """Fold each baked grouping into one block owning its member segments.

    Grouping is an operator decision about shared power wiring, not something
    re-derivable from geometry, so it is baked. Each member segment already
    exists as its own one-segment block from ``_add_blocks``; grouping moves
    those segments into one block and drops the emptied ones. Runs after every
    block exists (a grouping may name a platform block).
    """
    if not BLOCK_GROUPS:
        return
    grouping = network._get_block_grouping_manager()
    for (block_id, name, segment_ids) in BLOCK_GROUPS:
        grouping.group_into_block(
            block_id=block_id, segment_ids=segment_ids, name=name
        )


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the baked network from scratch and return it."""
    network = RailwayNetwork()
    _add_tracks(network)
    _add_switches(network)
    _add_stations(network)
    _add_platforms(network)
    _add_blocks(network)
    _add_block_groups(network)
    _add_tunnels(network)
    _add_signals(network)
    network.recompute_all_flank_protection()
    return network


def setup_demo_train(network, path_dispatcher=None, sim_controller=None):
    """No trains/routes are stored in the layout JSON, so this is a stub.

    Add scene-specific train placement + routes here by hand if the scene needs
    rolling stock (see demo_6.py for an example).
    """
    return
'''


def build_module_text(snapshot: dict, scene_id: str, source_name: str) -> tuple[str, list[str]]:
    """Return (python_source, warnings)."""
    net = RailwayNetwork()
    net.from_dict(snapshot)

    tiles = gen_tiles(net)
    switches, sw_warn = gen_switches(net)
    stations, grouped = gen_stations(net)
    platforms = gen_platforms(net, grouped)
    blocks = gen_blocks(net)
    block_groups = gen_block_groups(net)
    tunnels = gen_tunnels(net)
    signals = gen_signals(snapshot)

    warnings = []
    if sw_warn:
        warnings.append(
            f"{len(sw_warn)} switch(es) carry non-default metadata not captured "
            f"by the (id, x, y) form: {sorted(set(sw_warn))[:10]}"
        )

    n_platform_blocks = sum(
        1 for bid in net.blocks if bid in {f"{pid}_BLOCK" for pid in net.platforms}
    )
    header = f'''"""Scene baked from {source_name} by json_to_scene.py.

Auto-generated — do not hand-edit; re-run json_to_scene.py against the source
layout instead. Layout only: {len(net.tracks)} tracks, {len(net.switches)} switches,
{len(net.platforms)} platforms ({len(net.station_groups)} station groups),
{len(net.blocks)} blocks ({n_platform_blocks} auto-created platform blocks),
{sum(1 for b in net.blocks.values() if len(b.segments) > 1)} block groups, {len(net.tunnels) // 2} tunnels,
{len(net.signals)} signals. Trains/routes are not
part of the layout JSON, so setup_demo_train() is a stub.
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
from switch import Switch


SCENE_ID = {scene_id!r}'''

    body = "\n\n\n".join(
        [header, tiles, switches, stations, platforms, blocks, block_groups, tunnels, signals]
    )
    return body + "\n\n" + _BUILDERS, warnings


# --------------------------------------------------------------------------- #
# Round-trip verification: generated module vs canonical from_dict load.
# --------------------------------------------------------------------------- #
def _fingerprint(net) -> dict:
    tracks = {}
    for (x, y), t in net.tracks.items():
        tracks[(x, y)] = (
            t.track_bits,
            tuple(sorted(t.dead_end_ports or [])),
            t.switch_id,
            t.segment_id,
            t.station_id,
            tuple(sorted(t.signal_ids or [])),
        )
    switches = {sid: (s.x, s.y) for sid, s in net.switches.items()}
    platforms = {}
    for pid, p in net.platforms.items():
        platforms[pid] = (
            p.name,
            tuple(sorted(tuple(pt) for pt in p.positions)),
            _side_str(p.platform_side),
            p.length_cm,
            p.top_speed_kmh,
        )
    groups = {g.station_id: (g.name, tuple(g.platform_ids)) for g in net.station_groups.values()}
    blocks = {}
    for bid, b in net.blocks.items():
        blocks[bid] = (
            b.name,
            b.length_cm,
            b.speed_limit_kmh,
            tuple(sorted(net.get_block_positions(bid))),
        )
    tunnels = set()
    for tp in net.tunnels_to_list():
        a = (tp["a"]["x"], tp["a"]["y"], tp["a"]["edge"])
        b = (tp["b"]["x"], tp["b"]["y"], tp["b"]["edge"])
        tunnels.add(frozenset([a, b]))
    signals = {
        sid: (tuple(s.position), s.detection_direction, s.monitored_block_id)
        for sid, s in net.signals.items()
    }
    block_groups = {
        bid: (b.name, tuple(b.segment_ids))
        for bid, b in net.blocks.items()
        if len(b.segments) > 1
    }
    return {
        "tracks": tracks,
        "switches": switches,
        "platforms": platforms,
        "groups": groups,
        "blocks": blocks,
        "tunnels": tunnels,
        "signals": signals,
        "block_groups": block_groups,
    }


def _diff_section(name: str, a: dict, b: dict) -> list[str]:
    msgs = []
    if isinstance(a, set):
        if a != b:
            msgs.append(f"  [{name}] differ: only-generated={a - b} only-canonical={b - a}")
        return msgs
    a_keys, b_keys = set(a), set(b)
    if a_keys != b_keys:
        msgs.append(f"  [{name}] key mismatch: only-generated={sorted(a_keys - b_keys)[:8]} "
                    f"only-canonical={sorted(b_keys - a_keys)[:8]}")
    for k in sorted(a_keys & b_keys):
        if a[k] != b[k]:
            msgs.append(f"  [{name}] {k}: generated={a[k]!r} != canonical={b[k]!r}")
            if len(msgs) > 20:
                msgs.append(f"  [{name}] ... (more differences truncated)")
                break
    return msgs


def verify(output_path: str, snapshot: dict) -> bool:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_generated_scene", output_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    gen_net = mod.create_comprehensive_demo()
    canon = RailwayNetwork()
    canon.from_dict(snapshot)

    fa, fb = _fingerprint(gen_net), _fingerprint(canon)
    all_msgs = []
    for section in ("tracks", "switches", "platforms", "groups", "blocks", "tunnels", "signals", "block_groups"):
        all_msgs += _diff_section(section, fa[section], fb[section])

    if all_msgs:
        print("VERIFY FAILED — generated scene differs from canonical from_dict load:")
        print("\n".join(all_msgs[:60]))
        return False

    print("VERIFY OK — generated scene is equivalent to the canonical from_dict load:")
    print(f"  tracks={len(fa['tracks'])} switches={len(fa['switches'])} "
          f"platforms={len(fa['platforms'])} groups={len(fa['groups'])} "
          f"blocks={len(fa['blocks'])} tunnels={len(fa['tunnels'])} signals={len(fa['signals'])} "
          f"block_groups={len(fa['block_groups'])}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Bake a layout JSON into a Python scene module.")
    ap.add_argument("input", help="Path to the layout JSON (with a top-level 'snapshot').")
    ap.add_argument("output", help="Path to write the generated .py scene module.")
    ap.add_argument("--scene-id", default=None, help="SCENE_ID for the module (default: output filename stem).")
    ap.add_argument("--verify", action="store_true", help="Round-trip check the generated module against from_dict.")
    args = ap.parse_args()

    with open(args.input) as f:
        wrapper = json.load(f)
    snapshot = wrapper.get("snapshot", wrapper)

    import os
    scene_id = args.scene_id or os.path.splitext(os.path.basename(args.output))[0]

    text, warnings = build_module_text(snapshot, scene_id, os.path.basename(args.input))
    with open(args.output, "w") as f:
        f.write(text)
    print(f"Wrote {args.output} (SCENE_ID={scene_id!r}).")
    for w in warnings:
        print(f"  WARNING: {w}")

    if args.verify:
        ok = verify(args.output, snapshot)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
