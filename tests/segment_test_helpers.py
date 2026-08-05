"""Geometry builders for tests, in the two-axis track model.

A test fixture names its track runs by the id it later asserts on — the one it
passes to the dispatcher, reserves, and reads back out of `network.blocks`. That
is the BLOCK id. The production entry points take the id on the other axis
(`create_segment(segment_id, ...)` mints geometry first and derives the owning
block from it), so a fixture calling them directly would name its segment and
silently get a derived block id its assertions do not know.

These two builders take the block id the test means, pin it, and give the member
segment the matching geometry-axis id (see `entity_ids`) — so fixtures stay
written in block terms while the two ids stay distinct, as the model requires.
"""

from __future__ import annotations

from entity_ids import segment_id_for_block


def make_segment_block(network, block_id: str, *args, **kwargs):
    """`network.create_segment(...)` with the owning block pinned to `block_id`."""
    return network.create_segment(
        segment_id_for_block(block_id), *args, block_id=block_id, **kwargs
    )


def make_segment_block_with_tracks(network, block_id: str, *args, **kwargs):
    """`create_segment_with_tracks(...)` with the block pinned to `block_id`."""
    return network.create_segment_with_tracks(
        segment_id_for_block(block_id), *args, block_id=block_id, **kwargs
    )
