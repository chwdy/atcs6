"""Tile -> Segment -> Block resolution over a block that groups two segments.

A tile belongs to a `Segment` (design_new.md "segment"); the segment is grouped
into a `Block` (design_new.md "Block grouping"), and the block is what carries
power, occupancy and reservation. Which of the two a caller wants is the whole
question this layer answers, so every test here uses a block whose id differs
from every segment id it groups:

    BLK_YARD  groups  SEG_WEST  +  SEG_EAST

For a block that owns a single segment the two ids are equal, which makes an
aliasing mistake invisible — nearly the whole suite would still pass. These
tests exist to make that mistake visible: each one fails if a segment id is used
where a block id belongs, or the reverse.
"""

import pytest

from network import RailwayNetwork
from track import Tile


WEST_TILES = [(0, 0), (1, 0)]
EAST_TILES = [(2, 0), (3, 0)]
ALL_TILES = WEST_TILES + EAST_TILES


def _grouped_network() -> RailwayNetwork:
    """A straight EW run of two segments grouped into one block.

    `SEG_WEST` is 100cm over 2 tiles; `SEG_EAST` is 60cm over 2 tiles. The
    lengths differ so a per-tile length computed against the block's summed
    length instead of the owning segment's is detectable.
    """
    net = RailwayNetwork()
    for x in range(4):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight, all joined
    net.create_segment_with_tracks(
        "SEG_WEST", WEST_TILES, name="West", length_cm=100.0
    )
    net.create_segment_with_tracks(
        "SEG_EAST", EAST_TILES, name="East", length_cm=60.0
    )
    net._get_block_grouping_manager().group_into_block(
        "BLK_YARD", ["SEG_WEST", "SEG_EAST"], name="Yard"
    )
    return net


@pytest.fixture
def net() -> RailwayNetwork:
    return _grouped_network()


# --- tile membership --------------------------------------------------------


def test_grouping_leaves_one_block_over_two_segments(net):
    assert set(net.blocks) == {"BLK_YARD"}
    assert net.blocks["BLK_YARD"].segment_ids == ["SEG_WEST", "SEG_EAST"]


def test_tile_keeps_its_own_segment_id_after_grouping(net):
    """Grouping must not stamp the block id onto the tiles.

    Re-indexing a grouped block once wrote `block.block_id` into the tile
    index, which made every tile claim membership of a segment that does not
    exist. The tile index stores the SEGMENT.
    """
    for x, y in WEST_TILES:
        assert net.get_track(x, y).segment_id == "SEG_WEST"
    for x, y in EAST_TILES:
        assert net.get_track(x, y).segment_id == "SEG_EAST"
    for x, y in ALL_TILES:
        assert net.get_track(x, y).segment_id != "BLK_YARD"


def test_tile_resolves_to_the_grouping_block(net):
    """Every tile of either member resolves to the one grouping block."""
    for x, y in ALL_TILES:
        assert net.get_track(x, y).block_id == "BLK_YARD"


def test_network_reverse_indices_answer_the_two_questions_differently(net):
    assert net.tile_to_segment_id(0, 0) == "SEG_WEST"
    assert net.tile_to_segment_id(3, 0) == "SEG_EAST"
    assert net.tile_to_block_id(0, 0) == "BLK_YARD"
    assert net.tile_to_block_id(3, 0) == "BLK_YARD"


def test_tile_block_id_is_read_only(net):
    """Block membership comes from grouping the segment, not from the tile."""
    with pytest.raises(AttributeError):
        net.get_track(0, 0).block_id = "SOMETHING_ELSE"


def test_unowned_tile_resolves_to_neither(net):
    net.add_track(Tile(x=9, y=9, track_bits=2))
    assert net.tile_to_segment_id(9, 9) is None
    assert net.tile_to_block_id(9, 9) is None
    assert net.get_track(9, 9).segment_id is None
    assert net.get_track(9, 9).block_id is None


# --- tile lookups by owner --------------------------------------------------


def test_block_tracks_span_every_member_segment(net):
    positions = sorted(net.get_block_positions("BLK_YARD"))
    assert positions == sorted(ALL_TILES)


def test_segment_tracks_are_scoped_to_one_segment(net):
    assert sorted(net.get_segment_positions("SEG_WEST")) == sorted(WEST_TILES)
    assert sorted(net.get_segment_positions("SEG_EAST")) == sorted(EAST_TILES)


def test_segment_id_is_not_a_block_id_for_tile_lookups(net):
    """`get_block_tracks` keyed by a segment id must find nothing."""
    assert net.get_block_tracks("SEG_WEST") == []
    assert net.get_segment_tracks("BLK_YARD") == []


# --- geometry ---------------------------------------------------------------


def test_block_geometry_aggregates_its_members(net):
    block = net.blocks["BLK_YARD"]
    assert block.length_cm == 160.0
    assert [(e.x, e.y) for e in block.ordered_tiles] == ALL_TILES


def test_per_tile_length_comes_from_the_owning_segment(net):
    """Per-tile length spreads the SEGMENT's length over the SEGMENT's tiles.

    Reading the block instead would give 160/4 = 40cm everywhere.
    """
    assert net._get_tile_length_cm(0, 0) == 50.0
    assert net._get_tile_length_cm(2, 0) == 30.0


def test_rebuilding_one_member_leaves_the_other_intact(net):
    """A per-segment rebuild must not flatten the block's members together.

    Rebuilding at block level would walk all four tiles and write them to one
    segment, duplicating tiles into the block's concatenated read and emptying
    the other member.
    """
    geometry = net._get_segment_geometry_manager()
    geometry.rebuild_segment_ordered_tiles("SEG_EAST")

    west = net.get_segment("SEG_WEST")
    east = net.get_segment("SEG_EAST")
    assert [(e.x, e.y) for e in west.ordered_tiles] == WEST_TILES
    assert [(e.x, e.y) for e in east.ordered_tiles] == EAST_TILES
    assert [(e.x, e.y) for e in net.blocks["BLK_YARD"].ordered_tiles] == ALL_TILES


def test_extending_one_member_does_not_touch_the_other(net):
    net.add_track(Tile(x=4, y=0, track_bits=2))
    net.extend_segment("SEG_EAST", [(4, 0)])

    assert net.get_track(4, 0).segment_id == "SEG_EAST"
    assert net.get_track(4, 0).block_id == "BLK_YARD"
    assert sorted(net.get_segment_positions("SEG_WEST")) == sorted(WEST_TILES)
    assert sorted(net.get_block_positions("BLK_YARD")) == sorted(
        ALL_TILES + [(4, 0)]
    )


# --- runtime state lives on the block ---------------------------------------


def test_occupancy_is_shared_across_the_whole_block(net):
    """Occupancy is a block property, so a tile in either member reads it."""
    net.blocks["BLK_YARD"].occupy("T1")
    for x, y in ALL_TILES:
        assert net.get_track(x, y).occupied_by == "T1"


def test_reservation_is_shared_across_the_whole_block(net):
    net.blocks["BLK_YARD"].reserve_for_path("P1", "T1", direction="E")
    for x, y in ALL_TILES:
        assert net.get_track(x, y).reserved_by == "T1"


# --- lifecycle --------------------------------------------------------------


def test_deleting_one_member_keeps_the_block_and_its_other_member(net):
    assert net.delete_segment("SEG_EAST") is True

    assert set(net.blocks) == {"BLK_YARD"}
    assert net.blocks["BLK_YARD"].segment_ids == ["SEG_WEST"]
    assert net.get_segment("SEG_EAST") is None
    for x, y in EAST_TILES:
        assert net.get_track(x, y).segment_id is None
        assert net.get_track(x, y).block_id is None
    for x, y in WEST_TILES:
        assert net.get_track(x, y).block_id == "BLK_YARD"


def test_deleting_the_block_takes_every_member_segment(net):
    assert net.delete_block("BLK_YARD") is True

    assert net.blocks == {}
    assert net.get_segment("SEG_WEST") is None
    assert net.get_segment("SEG_EAST") is None
    for x, y in ALL_TILES:
        assert net.get_track(x, y).segment_id is None


def test_deleting_the_last_member_drops_the_block(net):
    net.delete_segment("SEG_EAST")
    net.delete_segment("SEG_WEST")
    assert net.blocks == {}


# --- serialization ----------------------------------------------------------


def test_wire_payload_carries_block_and_segment_separately(net):
    payload = net.get_track(2, 0).to_dict()
    # `block_section_id` is the wire name for the owning block.
    assert payload["block_section_id"] == "BLK_YARD"
    assert payload["segment_id"] == "SEG_EAST"


def test_layout_round_trip_preserves_the_grouping(net):
    reloaded = RailwayNetwork()
    reloaded.from_dict(net.to_layout_dict())

    assert reloaded.blocks["BLK_YARD"].segment_ids == ["SEG_WEST", "SEG_EAST"]
    for x, y in WEST_TILES:
        assert reloaded.get_track(x, y).segment_id == "SEG_WEST"
        assert reloaded.get_track(x, y).block_id == "BLK_YARD"
    for x, y in EAST_TILES:
        assert reloaded.get_track(x, y).segment_id == "SEG_EAST"
        assert reloaded.get_track(x, y).block_id == "BLK_YARD"


# --- block-addressed geometry edits -----------------------------------------


def test_block_addressed_tile_edit_refuses_a_grouped_block(net):
    """Adding a tile through a block-addressed endpoint has no single target.

    Tiles belong to segments. A block that groups several has no one member the
    edit obviously belongs to, so the service says so instead of silently
    picking one.
    """
    from services import block_service
    from services.exceptions import ValidationError

    class _State:
        pass

    state = _State()
    state.network = net
    net.add_track(Tile(x=4, y=0, track_bits=2))

    with pytest.raises(ValidationError) as exc:
        block_service.add_tracks_to_block(state, "BLK_YARD", positions=[[4, 0]])
    assert "SEG_WEST" in str(exc.value) and "SEG_EAST" in str(exc.value)


def test_block_addressed_tile_edit_works_on_a_lone_segment(net):
    """The same endpoint is unambiguous when the block owns one segment."""
    from services import block_service

    class _State:
        pass

    state = _State()
    state.network = net
    net.delete_segment("SEG_EAST")
    net.add_track(Tile(x=2, y=0, track_bits=2))

    result = block_service.add_tracks_to_block(state, "BLK_YARD", positions=[[2, 0]])
    assert result["added"] == 1
    assert net.get_track(2, 0).segment_id == "SEG_WEST"
    assert net.get_track(2, 0).block_id == "BLK_YARD"
