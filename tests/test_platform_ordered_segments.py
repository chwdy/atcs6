"""D2 + H10 (Platform half): `Platform.ordered_segments` and the
`reverse_segment_orientation` platform sole-member guard.

design_new.md "Platform And Station": `Platform.ordered_segments` is the
canonical ordered list of the platform's member segment IDs, used to identify
the two platform-end segments. design_new.md "Segment",
`reverse_segment_orientation`: the reversal "rejects the operation unless the
segment is the sole member of both its owning `Block` and any `Platform` that
contains it."

`ordered_segments` is derived live from the network's tile -> segment reverse
index, so a platform whose single segment is split by a mid-platform edit reads
as the several child segments with no membership bookkeeping — and that is the
case the platform guard must catch, since each split child is the sole member of
its own fresh block (so the block guard alone would let the reversal through).
"""

import pytest

from network import RailwayNetwork
from track import Tile
from entity_ids import segment_id_for_block


def _platform_net(platform_id: str, n_tiles: int, y: int = 0):
    """A straight EW run of `n_tiles` tiles made into one platform.

    `add_station` mints the platform's single segment; `extend_station` grows
    that same segment tile by tile, so a freshly built platform is one segment.
    Returns (network, platform).
    """
    net = RailwayNetwork()
    for x in range(n_tiles):
        net.add_track(Tile(x=x, y=y, track_bits=2))  # EW straight, all joined
    platform = net.add_station(0, y, station_id=platform_id, length_cm=500.0)
    if n_tiles > 1:
        net.extend_station(platform_id, [(x, y) for x in range(1, n_tiles)])
    return net, platform


# ---------------------------------------------------------------------------
# D2: Platform.ordered_segments
# ---------------------------------------------------------------------------


def test_single_segment_platform_lists_its_one_segment():
    """A platform built from one station segment lists exactly that segment,
    in its own derived geometry-axis id (`segment_id_for_block`)."""
    net, platform = _platform_net("PLT_A", n_tiles=5)
    expected_seg = segment_id_for_block("PLT_A_BLOCK")
    assert platform.ordered_segments(net) == [expected_seg]
    # And the reverse index resolves that segment back to this platform.
    assert net.segment_to_platform_id(expected_seg) == "PLT_A"


def test_split_segment_makes_platform_multi_segment():
    """Removing a middle tile splits the platform's single segment into two
    children; `ordered_segments` reflects both with no bookkeeping, because it
    reads the live tile -> segment index. Each child resolves back to the
    platform through the segment -> platform reverse index."""
    net, platform = _platform_net("PLT_B", n_tiles=5)
    assert len(platform.ordered_segments(net)) == 1

    # A mid-platform edit (what a signal/station placement does internally):
    # remove the middle tile from its segment, splitting the run in two.
    net.remove_tile_from_segment(2, 0)

    ordered = platform.ordered_segments(net)
    assert len(ordered) == 2, ordered
    # Each child is a real 2-tile segment that is the sole member of its own
    # fresh block (the pre-split block had a single member).
    for seg_id in ordered:
        assert len(net.get_segment(seg_id).ordered_tiles) == 2
        block = net.blocks[net.segment_to_block_id(seg_id)]
        assert block.segment_ids == [seg_id]
        assert net.segment_to_platform_id(seg_id) == "PLT_B"


def test_segment_to_platform_id_is_none_for_a_segment_in_no_platform():
    """A segment that belongs to no platform resolves to None."""
    net = RailwayNetwork()
    for x in range(2):
        net.add_track(Tile(x=x, y=3, track_bits=2))
    net.create_segment_with_tracks("SEG_FREE", [(0, 3), (1, 3)], length_cm=100.0)
    assert net.segment_to_platform_id("SEG_FREE") is None
    # An unknown id is None too.
    assert net.segment_to_platform_id("SEG_DOES_NOT_EXIST") is None


# ---------------------------------------------------------------------------
# H10 (Platform half): reverse_segment_orientation platform sole-member guard
# ---------------------------------------------------------------------------


def test_reverse_rejected_for_member_of_multi_segment_platform():
    """The core of the guard: a segment that is one of several in a platform
    cannot be reversed, even though it is the sole member of its own block (so
    the block guard would allow it). The segment is left unchanged."""
    net, platform = _platform_net("PLT_C", n_tiles=5)
    net.remove_tile_from_segment(2, 0)  # split -> two-segment platform
    ordered = platform.ordered_segments(net)
    assert len(ordered) == 2

    child = ordered[0]
    # Precondition: the block guard alone would NOT reject — the child is the
    # sole member of its block.
    assert net.blocks[net.segment_to_block_id(child)].segment_ids == [child]

    geom = net._get_segment_geometry_manager()
    before = [(e.x, e.y, e.entry_edge) for e in net.get_segment(child).ordered_tiles]
    accepted = geom.reverse_segment_orientation(child)
    after = [(e.x, e.y, e.entry_edge) for e in net.get_segment(child).ordered_tiles]

    assert accepted is False
    assert before == after, "a rejected reversal must not mutate the segment"


def test_reverse_allowed_for_sole_segment_of_platform():
    """A platform with exactly one segment is the sole-member case: reversing
    that segment is allowed and actually flips its stored tile order."""
    net, platform = _platform_net("PLT_D", n_tiles=5)
    seg = platform.ordered_segments(net)[0]
    assert len(platform.ordered_segments(net)) == 1

    geom = net._get_segment_geometry_manager()
    before = [(e.x, e.y) for e in net.get_segment(seg).ordered_tiles]
    accepted = geom.reverse_segment_orientation(seg)
    after = [(e.x, e.y) for e in net.get_segment(seg).ordered_tiles]

    assert accepted is True
    assert after == list(reversed(before)), "tile order should be flipped"


def test_reverse_allowed_for_segment_in_no_platform():
    """A multi-tile segment that is sole member of its block and belongs to no
    platform still reverses — the platform guard must not over-reject."""
    net = RailwayNetwork()
    for x in range(2):
        net.add_track(Tile(x=x, y=7, track_bits=2))
    net.create_segment_with_tracks("SEG_LONE", [(0, 7), (1, 7)], length_cm=100.0)
    geom = net._get_segment_geometry_manager()
    assert net.segment_to_platform_id("SEG_LONE") is None
    assert geom.reverse_segment_orientation("SEG_LONE") is True


# ---------------------------------------------------------------------------
# block <-> platform reverse indices (design_new.md "Platform And Station":
# the block -> platform index is two-way with platform -> blocks)
# ---------------------------------------------------------------------------
