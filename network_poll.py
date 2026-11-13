"""
Network poll reply — sends the layout only when it changed.

Per design_new.md "Data Flow To UI", **Layout version**: the frontend polls
`GET /api/network` several times a second while a session runs, and most of
that payload is the layout (tile geometry, block tile lists),
which changes only when someone edits it. This module splits the composite
runtime snapshot into its layout part and its live part, names the layout with
`layout_version` (a hash of the layout part), and leaves the layout out of the
reply when the client already holds the same version.

The version is a hash, not a counter, so no edit path can forget to bump it.
A field listed here as layout that does change during a run only costs a full
reply on that poll; it never leaves the client showing stale data.
"""

import hashlib
import json
from typing import Any, Dict, Optional, Tuple


# Snapshot keys whose whole section is layout.
LAYOUT_SECTIONS: Tuple[str, ...] = (
    "tunnels",
)

# Per-entity fields that change only when the layout is edited. Every other
# field of the entity is live and is sent on every poll.
BLOCK_LAYOUT_FIELDS = frozenset({
    "block_id",
    "name",
    "length_cm",
    "min_radius_cm",
    "grade_percent",
    "is_holding_point",
    "speed_limit_kmh",
    "allowed_travel_directions",
    "ordered_tiles",
    "segment_ids",
    "segments",
})

SWITCH_LAYOUT_FIELDS = frozenset({
    "switch_id",
    "x",
    "y",
    "main_length_cm",
    "diverge_length_cm",
    "main_radius_cm",
    "diverge_radius_cm",
    "switch_type",
    "name",
    "notes",
})

# The live fields of a tile. A tile carries live fields only when it holds a
# switch; every field of any other tile is layout.
TRACK_LIVE_FIELDS = frozenset({"switch_state", "switch_states"})


def _track_has_live_fields(track: Dict[str, Any]) -> bool:
    return bool(track.get("is_switch"))


def _split_fields(
    entity: Dict[str, Any], layout_fields: frozenset
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    layout = {k: v for k, v in entity.items() if k in layout_fields}
    live = {k: v for k, v in entity.items() if k not in layout_fields}
    return layout, live


def split_network_payload(
    payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split a full network payload into `(layout_part, live_part)`.

    `live_part` holds `live_blocks` and `live_switches` (maps keyed by id) and
    `live_tracks` (a list of `{x, y, <live fields>}` for the tiles that hold a
    switch). Keys outside `blocks`, `switches`, `tracks` and
    `LAYOUT_SECTIONS` belong to neither part: they are always sent.
    """
    layout: Dict[str, Any] = {
        key: payload[key] for key in LAYOUT_SECTIONS if key in payload
    }

    layout_blocks: Dict[str, Any] = {}
    live_blocks: Dict[str, Any] = {}
    for block_id, block in (payload.get("blocks") or {}).items():
        layout_blocks[block_id], live_blocks[block_id] = _split_fields(
            block, BLOCK_LAYOUT_FIELDS
        )
    layout["blocks"] = layout_blocks

    layout_switches: Dict[str, Any] = {}
    live_switches: Dict[str, Any] = {}
    for switch_id, switch in (payload.get("switches") or {}).items():
        layout_switches[switch_id], live_switches[switch_id] = _split_fields(
            switch, SWITCH_LAYOUT_FIELDS
        )
    layout["switches"] = layout_switches

    layout_tracks = []
    live_tracks = []
    for track in payload.get("tracks") or []:
        if not _track_has_live_fields(track):
            layout_tracks.append(track)
            continue
        live = {k: v for k, v in track.items() if k in TRACK_LIVE_FIELDS}
        layout_tracks.append(
            {k: v for k, v in track.items() if k not in TRACK_LIVE_FIELDS}
        )
        live_tracks.append({"x": track["x"], "y": track["y"], **live})
    layout["tracks"] = layout_tracks

    live_part = {
        "live_blocks": live_blocks,
        "live_switches": live_switches,
        "live_tracks": live_tracks,
    }
    return layout, live_part


def layout_version(layout_part: Dict[str, Any]) -> str:
    """Hash of the layout part. Equal layouts give equal versions, across
    server restarts too.

    Keys are not sorted: the payload builders emit them in a fixed order, and
    sorting costs about a third of the hash time. A reordering with no content
    change would only cost one full reply.
    """
    text = json.dumps(layout_part, separators=(",", ":"), default=str)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def build_network_poll_reply(
    payload: Dict[str, Any], client_layout_version: Optional[str]
) -> Dict[str, Any]:
    """Reply for `GET /api/network`.

    When `client_layout_version` equals the current version, the reply leaves
    the layout part out: it drops `LAYOUT_SECTIONS`, `blocks`, `switches` and
    `tracks`, and carries `live_blocks`, `live_switches` and `live_tracks`
    instead, with `layout_unchanged: True`. Otherwise it is the full payload
    with `layout_unchanged: False`. Both carry `layout_version`.
    """
    layout_part, live_part = split_network_payload(payload)
    version = layout_version(layout_part)

    if client_layout_version != version:
        return full_reply(payload, version)
    return live_only_reply(payload, live_part, version)


def full_reply(payload: Dict[str, Any], version: str) -> Dict[str, Any]:
    """The whole snapshot, named with its `layout_version`."""
    return {**payload, "layout_version": version, "layout_unchanged": False}


def live_only_reply(
    payload: Dict[str, Any], live_part: Dict[str, Any], version: str
) -> Dict[str, Any]:
    """The snapshot with the layout part left out and `live_part` (from
    `split_network_payload`) in its place."""
    omitted = set(LAYOUT_SECTIONS) | {"blocks", "switches", "tracks"}
    reply = {k: v for k, v in payload.items() if k not in omitted}
    reply.update(live_part)
    reply["layout_version"] = version
    reply["layout_unchanged"] = True
    return reply
