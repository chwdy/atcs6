"""The two-tier claim: reservation (soft) + braking-distance lock (hard).

Covers `Block.locked_by_path` / `is_locked`, the `BlockManager` lock writers
(`lock_blocks_for_path` / `unlock_blocks_for_path`), the clear-on-consume /
clear-on-release obligations, and the derived `Path.get_lock_end_block_id()`
(design_new.md "Block" field schema, "BlockManager", "Path field schema").

Reuses the straight-line six-block harness (BLK_0..BLK_5) from the seam-cap
suite so the manager runs against a real network. The placed train "T" sits on
BLK_0; every reservation/lock here is on BLK_1 onward.
"""

from block_manager import ReservedBlockTraversal
from path import Path, ResourceSequenceEntry
from enums import PathStatus

from tests.test_block_occupancy_cap import build_line, place


def _fwd(exit_edge="E"):
    return ReservedBlockTraversal(exit_edge=exit_edge, block_direction="forward")


def _line():
    """A straight line with a placed train, returning (network, block_manager)."""
    network = build_line()
    sim, *_ = place(network, head_x=0, length_cm=40.0)
    return network, sim.block_manager


def _reserve(bm, block_ids, train_id="T", path_id="P1"):
    result = bm.reserve_blocks_for_path(
        list(block_ids),
        path_id,
        train_id,
        directions={bid: _fwd() for bid in block_ids},
    )
    assert result.success is True, result.reason


def _block_seq(block_ids):
    return [
        ResourceSequenceEntry(
            sequence_index=i, resource_type="block", resource_id=bid
        )
        for i, bid in enumerate(block_ids)
    ]


# --- BlockManager lock writers -------------------------------------------


def test_lock_sets_only_reserved_blocks():
    network, bm = _line()
    _reserve(bm, ["BLK_1", "BLK_2"])
    # BLK_3 is not reserved by P1, so it is skipped, not locked.
    bm.lock_blocks_for_path(["BLK_1", "BLK_2", "BLK_3"], "P1")
    assert network.blocks["BLK_1"].locked_by_path == "P1"
    assert network.blocks["BLK_1"].is_locked is True
    assert network.blocks["BLK_2"].locked_by_path == "P1"
    assert network.blocks["BLK_3"].locked_by_path is None


def test_lock_requires_own_reservation():
    network, bm = _line()
    _reserve(bm, ["BLK_1"], path_id="P1")
    # P2 does not reserve BLK_1, so its lock request is skipped.
    bm.lock_blocks_for_path(["BLK_1"], "P2")
    assert network.blocks["BLK_1"].locked_by_path is None


def test_unlock_clears_only_own_lock_and_keeps_reservation():
    network, bm = _line()
    _reserve(bm, ["BLK_1", "BLK_2"])
    bm.lock_blocks_for_path(["BLK_1", "BLK_2"], "P1")
    # Wrong path: ignored.
    bm.unlock_blocks_for_path(["BLK_1"], "P2")
    assert network.blocks["BLK_1"].locked_by_path == "P1"
    # Own path: cleared, but the reservation survives the unlock — a
    # reserved-but-not-locked block is exactly what Preemption may later take.
    bm.unlock_blocks_for_path(["BLK_1"], "P1")
    assert network.blocks["BLK_1"].locked_by_path is None
    assert network.blocks["BLK_1"].reserved_by_path == "P1"


# --- clear obligations ----------------------------------------------------


def test_releasing_reservation_drops_the_lock():
    network, bm = _line()
    _reserve(bm, ["BLK_1"])
    bm.lock_blocks_for_path(["BLK_1"], "P1")
    bm.release_path_block_reservations(["BLK_1"], "P1")
    assert network.blocks["BLK_1"].reserved_by_path is None
    assert network.blocks["BLK_1"].locked_by_path is None


def test_head_entry_consumes_the_lock():
    network, bm = _line()
    _reserve(bm, ["BLK_1"])
    bm.lock_blocks_for_path(["BLK_1"], "P1")
    assert bm.occupy_blocks(["BLK_1"], "T")
    assert network.blocks["BLK_1"].occupied_by == "T"
    assert network.blocks["BLK_1"].locked_by_path is None


def test_tail_vacate_clears_residual_lock():
    network, bm = _line()
    block = network.blocks["BLK_1"]
    _reserve(bm, ["BLK_1"])
    bm.lock_blocks_for_path(["BLK_1"], "P1")
    block.occupied_by = "T"
    assert bm.on_block_tail_vacated("BLK_1", "T", "E")
    assert block.locked_by_path is None


# --- Path.get_lock_end_block_id ------------------------------------------


def test_lock_end_is_the_furthest_locked_block():
    network, bm = _line()
    _reserve(bm, ["BLK_1", "BLK_2", "BLK_3"])
    bm.lock_blocks_for_path(["BLK_1", "BLK_2"], "P1")
    path = Path(
        path_id="P1",
        status=PathStatus.ACTIVE,
        train_id="T",
        resource_sequence=_block_seq(["BLK_1", "BLK_2", "BLK_3"]),
        current_resource_index=0,
        network=network,
    )
    # Furthest forward block whose Block.locked_by_path == path_id.
    assert path.get_lock_end_block_id() == "BLK_2"
    assert path.get_reservation_end_block_id() == "BLK_3"


def test_lock_end_ignores_other_paths_and_blocks_behind_the_head():
    network, bm = _line()
    _reserve(bm, ["BLK_1", "BLK_2"])
    bm.lock_blocks_for_path(["BLK_1", "BLK_2"], "P1")
    # A block locked by another path does not count toward this path's lock end.
    network.blocks["BLK_3"].locked_by_path = "PX"
    path = Path(
        path_id="P1",
        resource_sequence=_block_seq(["BLK_1", "BLK_2", "BLK_3"]),
        current_resource_index=0,
        network=network,
    )
    assert path.get_lock_end_block_id() == "BLK_2"
    # Blocks behind current_resource_index never count. With nothing of this
    # path's locked ahead, the lock end falls back to the head's block.
    path.current_resource_index = 2
    assert path.get_lock_end_block_id() == "BLK_3"


def test_lock_end_falls_back_to_the_head_block_when_nothing_ahead_is_locked():
    """design_new.md "Path field schema", `lock_end_block_id`: entering a block
    clears its lock flag, so a train standing in its reservation-end block still
    has a lock end — its own block."""
    network, bm = _line()
    _reserve(bm, ["BLK_1", "BLK_2"])
    path = Path(
        path_id="P1",
        resource_sequence=_block_seq(["BLK_1", "BLK_2"]),
        current_resource_index=1,
        network=network,
    )
    assert path.get_lock_end_block_id() == "BLK_2"
    assert path.get_lock_end_block_id() == path.get_reservation_end_block_id()


def test_lock_end_is_none_without_a_wired_network():
    path = Path(
        path_id="P1",
        resource_sequence=[
            ResourceSequenceEntry(
                sequence_index=0, resource_type="block", resource_id="BLK_1"
            )
        ],
        current_resource_index=0,
    )
    assert path.get_lock_end_block_id() is None
