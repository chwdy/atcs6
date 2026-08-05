"""Identifier derivation for the two track-model axes.

A `Block` and a `Segment` are different entities with different lifetimes: a
segment is geometry, a block is the power / occupancy domain that owns it. They
live in separate registries (`RailwayNetwork.blocks` vs the segments reachable
through them), so nothing forces their ids apart at the storage layer — but an
id that names both is ambiguous to every human reading a panel, an event, or a
hardware binding, and it forced defensive code wherever the two spaces met.

So the contract is: **a block id and a segment id are never equal.** The
default one-feed-per-segment grouping still pairs them, and these two functions
are how that pairing is spelled — a matched `BLK_`/`SEG_` suffix pair, derived
and therefore stable across saves, boots, and re-bakes. Stability is
load-bearing: `hardware_config.json` binds physical channels by block id, so a
block id that changed per load would silently detach a rig's wiring.

The two functions are inverses on the canonical shapes::

    SEG_4F4CF0        <-> BLK_4F4CF0
    PLT_NWY_1_SEG     <-> PLT_NWY_1_BLOCK

They are NOT inverses on an arbitrary id, because a legacy id carries no marker
saying which axis minted it (`PLT_NWY_1_APPROACH` was a block id that also
named its segment). A caller that must pin an exact block id — a baked scene
replaying ids that saved routes and hardware bindings already reference — passes
`block_id` explicitly to `create_segment` / `create_segment_with_tracks` rather
than relying on a round trip through these.
"""

from __future__ import annotations

BLOCK_PREFIX = "BLK_"
SEGMENT_PREFIX = "SEG_"
BLOCK_SUFFIX = "_BLOCK"
SEGMENT_SUFFIX = "_SEG"


def block_id_for_segment(segment_id: str) -> str:
    """The id of the single-segment block that owns `segment_id` by default.

    Used on the forward path: new geometry mints a segment id and the block
    that owns it takes the matching id on the block axis.
    """
    if segment_id.startswith(SEGMENT_PREFIX):
        return BLOCK_PREFIX + segment_id[len(SEGMENT_PREFIX):]
    if segment_id.endswith(SEGMENT_SUFFIX):
        return segment_id[: -len(SEGMENT_SUFFIX)] + BLOCK_SUFFIX
    return BLOCK_PREFIX + segment_id


def segment_id_for_block(block_id: str) -> str:
    """The id of the member segment of a default single-segment block.

    Used on the migration path — loading a save, or a scene, written when the
    two axes shared one id. The block id is the one already referenced by
    hardware bindings, saved routes, and stored paths, so it is the id that
    survives; the segment takes the derived one.
    """
    if block_id.startswith(BLOCK_PREFIX):
        return SEGMENT_PREFIX + block_id[len(BLOCK_PREFIX):]
    if block_id.endswith(BLOCK_SUFFIX):
        return block_id[: -len(BLOCK_SUFFIX)] + SEGMENT_SUFFIX
    return block_id + SEGMENT_SUFFIX
