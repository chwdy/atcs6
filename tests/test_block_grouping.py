"""Block grouping — design_new.md **Block grouping**.

`BlockGroupingManager` owns block membership: creating a block over segments,
adding or removing a `Segment` from `segment_ids`, and deleting a block. It
edits only the power-grouping axis — never runtime state, never segment
geometry.

The rules under test:

- A block is a switch-free connected run of segments joined end-to-end, so an
  add or a remove happens only at either END, and only when the result is still
  one connected walk with every member stored in the run's direction.
- Every segment belongs to exactly one block. Adding a segment moves it out of
  its prior block; removing one returns it to its own single-segment block;
  deleting a block re-homes all its members the same way.
- No grouping entity is ever left empty (the Non-empty parent rule).

These were previously exercised through the district REST endpoints. The REST
surface for block grouping is a later phase, so the logic is covered directly
against the manager here.
"""

import pytest

from network import RailwayNetwork
from track import Tile


def _line_network() -> RailwayNetwork:
    """A straight EW run split into three one-segment blocks A-B-C.

    A~B and B~C are directly joined; A is not adjacent to C (B sits between
    them, standing in for a switch / non-member gap).
    """
    net = RailwayNetwork()
    for x in range(6):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight, all joined
    net.create_segment_with_tracks("A", [(0, 0), (1, 0)], name="A")
    net.create_segment_with_tracks("B", [(2, 0), (3, 0)], name="B")
    net.create_segment_with_tracks("C", [(4, 0), (5, 0)], name="C")
    return net


@pytest.fixture
def net() -> RailwayNetwork:
    return _line_network()


@pytest.fixture
def grouping(net):
    return net._get_block_grouping_manager()


# --- create -----------------------------------------------------------------


def test_create_groups_segments_into_one_block(net, grouping):
    block = grouping.group_into_block("G", ["A", "B"], name="Yard")
    assert block.block_id == "G"
    assert block.name == "Yard"
    assert set(block.segment_ids) == {"A", "B"}
    # The grouped members are gone as blocks; C is untouched.
    assert set(net.blocks) == {"G", "C"}


def test_create_rejects_an_empty_member_list(grouping):
    with pytest.raises(ValueError):
        grouping.create_block("G", [])


def test_create_rejects_an_unknown_segment(grouping):
    with pytest.raises(ValueError):
        grouping.create_block("G", ["NOPE"])


def test_create_rejects_a_duplicate_block_id(net, grouping):
    with pytest.raises(ValueError):
        grouping.create_block("A", ["B"])


# --- connected switch-free run ---------------------------------------------


def test_create_accepts_a_connected_run(net, grouping):
    block = grouping.group_into_block("G", ["A", "B", "C"])
    assert len(block.segment_ids) == 3
    assert len(block.ordered_tiles) == 6


def test_create_rejects_a_gap_in_the_run(grouping):
    # A and C are not adjacent — B sits between them and is not a member.
    with pytest.raises(ValueError):
        grouping.group_into_block("G", ["A", "C"])


def test_create_rejects_a_run_through_a_switch(grouping, net):
    """A block is switch-free: a member carrying a switch tile is rejected."""
    net.get_track(2, 0).switch_id = "SW1"
    assert grouping.segments_form_switch_free_run(["B"]) is False
    with pytest.raises(ValueError):
        grouping.group_into_block("G", ["A", "B"])


def test_run_order_does_not_depend_on_the_member_list_order(net, grouping):
    """The same members named in any order produce the same run."""
    first = grouping.group_into_block("G", ["C", "A", "B"]).segment_ids
    net2 = _line_network()
    second = (
        net2._get_block_grouping_manager()
        .group_into_block("G", ["B", "C", "A"])
        .segment_ids
    )
    assert first == second == ["A", "B", "C"]


# --- add_segment_to_block ---------------------------------------------------


def test_add_segment_at_an_end(net, grouping):
    grouping.group_into_block("G", ["A", "B"])
    assert grouping.add_segment_to_block("G", "C") is True
    assert net.blocks["G"].segment_ids == ["A", "B", "C"]
    assert "C" not in net.blocks


def test_add_segment_is_false_when_already_a_member(net, grouping):
    grouping.group_into_block("G", ["A", "B"])
    assert grouping.add_segment_to_block("G", "B") is False


def test_add_rejects_a_segment_that_joins_neither_end(net, grouping):
    """C does not touch a block made of A alone — B sits between them."""
    grouping.create_block("G", ["A"])
    with pytest.raises(ValueError):
        grouping.add_segment_to_block("G", "C")
    assert net.blocks["G"].segment_ids == ["A"]


def test_add_rejects_an_unknown_block_or_segment(grouping):
    grouping.create_block("G", ["A"])
    with pytest.raises(ValueError):
        grouping.add_segment_to_block("MISSING", "B")
    with pytest.raises(ValueError):
        grouping.add_segment_to_block("G", "NOPE")


def test_add_moves_a_segment_out_of_its_prior_block(net, grouping):
    grouping.group_into_block("G1", ["A", "B"])
    grouping.create_block("G2", ["C"])
    # B is in G1 and touches C, so adding it to G2 moves it across.
    assert grouping.add_segment_to_block("G2", "B") is True
    assert net.blocks["G2"].segment_ids == ["B", "C"]
    assert net.blocks["G1"].segment_ids == ["A"]


def test_add_deletes_a_prior_block_left_empty(net, grouping):
    """Non-empty parent rule: a block whose last segment moves away is gone."""
    grouping.create_block("G1", ["B"])
    grouping.create_block("G2", ["C"])
    assert grouping.add_segment_to_block("G2", "B") is True
    assert "G1" not in net.blocks
    assert net.blocks["G2"].segment_ids == ["B", "C"]


# --- remove_segment_from_block ---------------------------------------------


def test_remove_end_segment_rehomes_it(net, grouping):
    grouping.group_into_block("G", ["A", "B", "C"])
    assert grouping.remove_segment_from_block("G", "A") is True
    assert net.blocks["G"].segment_ids == ["B", "C"]
    # A is back to being its own single-segment block.
    assert net.blocks["A"].segment_ids == ["A"]
    assert net.segment_to_block_id("A") == "A"


def test_remove_middle_segment_is_rejected(net, grouping):
    """Removing B from A-B-C would split the run in two."""
    grouping.group_into_block("G", ["A", "B", "C"])
    with pytest.raises(ValueError):
        grouping.remove_segment_from_block("G", "B")
    assert net.blocks["G"].segment_ids == ["A", "B", "C"]


def test_remove_is_false_when_not_a_member(net, grouping):
    grouping.group_into_block("G", ["A", "B"])
    assert grouping.remove_segment_from_block("G", "C") is False


def test_remove_rejects_an_unknown_block(grouping):
    with pytest.raises(ValueError):
        grouping.remove_segment_from_block("MISSING", "A")


def test_remove_last_segment_deletes_the_block(net, grouping):
    """Non-empty parent rule: the block goes rather than being left empty, and
    its segment lands back in its own block."""
    grouping.create_block("G", ["A"])
    assert grouping.remove_segment_from_block("G", "A") is True
    assert "G" not in net.blocks
    assert net.blocks["A"].segment_ids == ["A"]


# --- delete_block -----------------------------------------------------------


def test_delete_block_rehomes_every_segment(net, grouping):
    grouping.group_into_block("G", ["A", "B", "C"])
    assert grouping.delete_block("G") is True
    assert "G" not in net.blocks
    # Deleting a grouping never deletes its segments; each gets its own block.
    assert set(net.blocks) == {"A", "B", "C"}
    for segment_id in ("A", "B", "C"):
        assert net.blocks[segment_id].segment_ids == [segment_id]
        assert net.tile_to_block_id(*_first_tile(net, segment_id)) == segment_id


def test_delete_unknown_block_is_false(grouping):
    assert grouping.delete_block("MISSING") is False


def _first_tile(net, segment_id):
    entry = net.get_segment(segment_id).ordered_tiles[0]
    return entry.x, entry.y


# --- lookups ----------------------------------------------------------------


def test_segment_registry_and_reverse_lookup(net, grouping):
    grouping.group_into_block("G", ["A", "B"])
    assert set(net.segments) == {"A", "B", "C"}
    assert net.segment_to_block_id("A") == "G"
    assert net.segment_to_block_id("C") == "C"
    assert net.segment_to_block_id("NOPE") is None
    assert net.get_segment("B").segment_id == "B"
    assert net.get_segment("NOPE") is None
