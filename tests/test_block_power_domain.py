"""One train per Block (power domain) — design_new.md **One train per Block**.

A `Block` is the power + occupancy + reservation unit: a switch-free connected
run of one or more `Segment`s wired to one power feed, carrying one `track
voltage` and admitting one train at a time. Its member segments are geometry
only — they hold no occupancy — so the whole block is one occupancy section.

That gives one exclusion rule, not two: a second train physically drawing power
in a block another train holds is `FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN`,
reported through `SimulationController.enter_failed_runtime(...)`. This holds
whether the two trains are on the same member segment or on different ones,
which is the case a grouped block must catch — it is the reason a shared feed is
one block rather than several.
"""

import pytest

from block_manager import ReservedBlockTraversal
from network import RailwayNetwork
from runtime_payloads import FatalConflictKind
from track import Tile


class _CapturingSim:
    """Captures `enter_failed_runtime` payloads instead of freezing a runtime."""

    def __init__(self) -> None:
        self.failed = []

    def enter_failed_runtime(self, payload) -> None:
        self.failed.append(payload)


def _line_network() -> RailwayNetwork:
    """A straight EW run split into three one-segment blocks A-B-C.

    A~B and B~C are directly joined; A is not adjacent to C.
    """
    net = RailwayNetwork()
    for x in range(6):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight, all joined
    net.create_segment_with_tracks("A", [(0, 0), (1, 0)], name="A")
    net.create_segment_with_tracks("B", [(2, 0), (3, 0)], name="B")
    net.create_segment_with_tracks("C", [(4, 0), (5, 0)], name="C")
    return net


def _grouped_network() -> RailwayNetwork:
    """A-B-C grouped onto one power feed: one block `G` over three segments."""
    net = _line_network()
    net._get_block_grouping_manager().group_into_block(
        block_id="G", segment_ids=["A", "B", "C"], name="Grouped"
    )
    return net


def _fwd(exit_edge="E"):
    return ReservedBlockTraversal(exit_edge=exit_edge, block_direction="forward")


# --- the grouped block is one occupancy section ----------------------------


def test_grouping_produces_one_block_over_its_segments():
    net = _grouped_network()
    assert set(net.blocks) == {"G"}
    assert net.blocks["G"].segment_ids == ["A", "B", "C"]
    # Every member tile now answers with the grouped block.
    for x in range(6):
        assert net.tile_to_block_id(x, 0) == "G"


def test_grouped_block_geometry_is_the_sum_of_its_segments():
    net = _grouped_network()
    for segment_id, length in (("A", 10.0), ("B", 20.0), ("C", 30.0)):
        net.get_segment(segment_id).length_cm = length
    block = net.blocks["G"]
    assert block.length_cm == 60.0
    assert len(block.ordered_tiles) == 6


# --- fatal physical-entry conflict -----------------------------------------


def test_second_train_in_same_segment_is_fatal_occupied_by_other_train():
    net = _line_network()
    bm = net.block_manager
    sim = _CapturingSim()
    bm.fatal_conflict_reporter = sim.enter_failed_runtime

    assert bm.occupy_block("BLK_A", "T1") is True
    assert bm.occupy_block("BLK_A", "T2") is False
    assert len(sim.failed) == 1
    assert sim.failed[0].conflict_kind == FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN
    assert sim.failed[0].prior_owner_train_id == "T1"
    assert sim.failed[0].resource_id == "BLK_A"


def test_second_train_in_a_different_segment_of_the_same_block_is_fatal():
    """The case the fold exists for.

    Segments A and C are different parts of block G. A train on A and a train
    on C are in one block on one feed, so the second entry is the same fatal
    `OCCUPIED_BY_OTHER_TRAIN` as two trains on one segment — occupancy lives on
    the block, so there is no separate segment-level conflict to raise.
    """
    net = _grouped_network()
    bm = net.block_manager
    sim = _CapturingSim()
    bm.fatal_conflict_reporter = sim.enter_failed_runtime

    # Both trains address the one block their segments belong to.
    assert net.segment_to_block_id("A") == net.segment_to_block_id("C") == "G"
    assert net.blocks["G"].segment_ids == ["A", "B", "C"]

    assert bm.occupy_block("G", "T1") is True
    assert bm.occupy_block("G", "T2") is False
    assert len(sim.failed) == 1
    assert sim.failed[0].conflict_kind == FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN
    assert sim.failed[0].prior_owner_train_id == "T1"
    assert sim.failed[0].resource_id == "G"


def test_same_train_re_entry_is_idempotent_not_a_conflict():
    net = _grouped_network()
    bm = net.block_manager
    sim = _CapturingSim()
    bm.fatal_conflict_reporter = sim.enter_failed_runtime
    assert bm.occupy_block("G", "T1") is True
    assert bm.occupy_block("G", "T1") is True
    assert sim.failed == []


def test_separate_blocks_never_conflict():
    """Ungrouped blocks are independent feeds, so two trains coexist."""
    net = _line_network()
    bm = net.block_manager
    sim = _CapturingSim()
    bm.fatal_conflict_reporter = sim.enter_failed_runtime
    assert bm.occupy_block("BLK_A", "T1") is True
    assert bm.occupy_block("BLK_C", "T2") is True
    assert sim.failed == []


# --- reservation eligibility ------------------------------------------------


def test_reserve_rejected_when_block_held_by_other_train():
    net = _grouped_network()
    bm = net.block_manager
    bm.occupy_block("G", "T1")
    result = bm.reserve_blocks_for_path(
        ["G"], "P1", "T2", directions={"G": _fwd()}
    )
    assert result.success is False
    assert result.reason == "occupied"
    assert result.failed_block_id == "G"


def test_reserve_rejected_when_block_reserved_for_other_path():
    net = _grouped_network()
    bm = net.block_manager
    assert bm.reserve_blocks_for_path(
        ["G"], "P1", "T1", directions={"G": _fwd()}
    ).success is True
    result = bm.reserve_blocks_for_path(
        ["G"], "P2", "T2", directions={"G": _fwd()}
    )
    assert result.success is False
    assert result.reason == "reserved_by_other_path"


def test_a_reservation_never_changes_a_blocks_caution():
    """design_new.md BlockManager, A reservation never changes a block's
    caution: reserving a CAUTION block for a traversal out of its other end
    leaves `block.caution` as it was; the caution cap reads the traversal."""
    from block import CautionState

    net = _grouped_network()
    bm = net.block_manager
    net.blocks["G"].caution = CautionState(exit_edge="E")
    assert bm.reserve_blocks_for_path(
        ["G"], "P1", "T1", directions={"G": _fwd("W")}
    ).success is True
    assert net.blocks["G"].caution == CautionState(exit_edge="E")


# --- power: one feed per block ---------------------------------------------


def test_a_grouped_block_is_one_entry_in_the_energized_set():
    """A block is one feed, so three segments wired together carry one voltage
    under one block id — not one entry per segment."""
    from train import Train
    from train_physics import TrainPhysics

    net = _grouped_network()
    train = Train(train_id="T1")
    net.trains["T1"] = train
    net.block_manager.occupy_block("G", "T1")
    physics = TrainPhysics(train_id="T1", network=net)
    assert physics._energized_blocks_for_train(train) == {"G"}


def test_a_reservation_alone_energizes_nothing():
    """A reserved block is pre-energized only once it is the *next* block ahead
    of a train that is actually somewhere (design_new.md "Power", Only the next
    reserved block is pre-energized). A train with no live span is nowhere, so
    there is no head to be one block ahead of, and nothing is driven."""
    from train import Train
    from train_physics import TrainPhysics

    net = _grouped_network()
    train = Train(train_id="T1")
    net.trains["T1"] = train
    net.block_manager.reserve_blocks_for_path(
        ["G"], "P1", "T1", directions={"G": _fwd()}
    )
    physics = TrainPhysics(train_id="T1", network=net)
    assert physics._energized_blocks_for_train(train) == set()


def test_deenergize_keeps_a_held_block_energized():
    net = _grouped_network()
    bm = net.block_manager
    bm.occupy_block("G", "T1")
    bm._mark_for_deenergize("G")
    assert bm.take_blocks_to_deenergize() == []


def test_deenergize_zeroes_a_block_once_unheld():
    net = _grouped_network()
    bm = net.block_manager
    bm.occupy_block("G", "T1")
    bm.clear_block("G")
    bm._mark_for_deenergize("G")
    assert bm.take_blocks_to_deenergize() == ["G"]


def test_deenergize_ungrouped_block_unchanged():
    net = _line_network()
    bm = net.block_manager
    bm._mark_for_deenergize("BLK_A")
    assert bm.take_blocks_to_deenergize() == ["BLK_A"]


# --- persistence ------------------------------------------------------------


def test_grouped_block_round_trips_with_its_segments():
    """One `blocks` key carries the whole grouping — the block plus each member
    segment's own geometry. There is no separate grouping list."""
    net = _grouped_network()
    for segment_id, length in (("A", 11.0), ("B", 22.0), ("C", 33.0)):
        net.get_segment(segment_id).length_cm = length

    snapshot = net.to_layout_dict()
    assert "power_districts" not in snapshot
    assert snapshot["blocks"]["G"]["segment_ids"] == ["A", "B", "C"]

    net2 = RailwayNetwork()
    net2.from_dict(snapshot)
    block = net2.blocks["G"]
    assert block.segment_ids == ["A", "B", "C"]
    assert [s.length_cm for s in block.segments] == [11.0, 22.0, 33.0]
    assert block.length_cm == 66.0
    assert net2.segment_to_block_id("B") == "G"


def test_legacy_grouping_list_folds_into_one_block_on_load():
    """A save written before blocks grouped segments stored the grouping in its
    own list. Loading it folds those members into the single block the grouping
    always described, keeping the one-train power domain."""
    net = _line_network()
    snapshot = net.to_layout_dict()
    snapshot["power_districts"] = [
        {"district_id": "D_OLD", "block_ids": ["A", "B", "C"], "name": "Yard"}
    ]

    net2 = RailwayNetwork()
    net2.from_dict(snapshot)
    assert set(net2.blocks) == {"D_OLD"}
    block = net2.blocks["D_OLD"]
    assert block.name == "Yard"
    assert set(block.segment_ids) == {"A", "B", "C"}
    for x in range(6):
        assert net2.tile_to_block_id(x, 0) == "D_OLD"


def test_ungrouped_save_still_loads_as_one_block_per_segment():
    net = _line_network()
    net2 = RailwayNetwork()
    net2.from_dict(net.to_layout_dict())
    assert set(net2.blocks) == {"BLK_A", "BLK_B", "BLK_C"}
    for block in net2.blocks.values():
        assert len(block.segments) == 1


# --- block-level geometry writes -------------------------------------------


def test_block_geometry_write_is_rejected_on_a_grouped_block():
    """Geometry belongs to a `Segment`. The block's geometry reads are a sum /
    minimum / concatenation across its members, none of which invert, so a
    block-level write on a grouped block has no single place to land and must
    not silently pick one member."""
    net = _grouped_network()
    block = net.blocks["G"]
    for field, value in (
        ("length_cm", 5.0),
        ("min_radius_cm", 30.0),
        ("speed_limit_kmh", 40.0),
        ("grade_percent", 1.5),
    ):
        with pytest.raises(ValueError) as exc:
            setattr(block, field, value)
        assert field in str(exc.value)
    # Nothing landed on any member.
    assert [s.length_cm for s in block.segments] == [0.0, 0.0, 0.0]


def test_block_geometry_write_still_works_on_a_one_segment_block():
    net = _line_network()
    block = net.blocks["BLK_A"]
    block.length_cm = 12.5
    block.speed_limit_kmh = 40.0
    assert block.length_cm == 12.5
    assert net.get_segment("A").length_cm == 12.5
    assert net.get_segment("A").speed_limit_kmh == 40.0


def test_rebuilding_tile_order_leaves_a_grouped_block_intact():
    """`rebuild_segment_ordered_tiles` builds one flat walk from the tiles that
    name a block. On a grouped block every member tile names the block, so a
    rebuild would flatten all members into one segment. It must be a no-op
    there — the grouped block's order is already the members' concatenation."""
    net = _grouped_network()
    block = net.blocks["G"]
    before = list(block.ordered_tiles)
    per_segment_before = [len(s.ordered_tiles) for s in block.segments]

    net._get_segment_geometry_manager().rebuild_segment_ordered_tiles("G")

    assert [len(s.ordered_tiles) for s in block.segments] == per_segment_before
    assert list(block.ordered_tiles) == before
    assert len(block.ordered_tiles) == 6
