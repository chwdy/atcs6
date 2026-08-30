"""The CAUTION reevaluator's connectivity walk ``_next_block_along_direction``
returns a 2-tuple ``(Block, chain_entry_edge)`` — the edge through which the
chain enters the forward block — rather than discarding it.

Per design_new.md "`on_block_tail_vacated()` behaviour (directional CAUTION
evaluation)": the same connectivity walk that finds the forward block also
yields the edge through which the chain enters it (for a straight adjacent
block this is simply the side facing the CAUTION block; through switch tiles and
curves it is the entry edge the walk actually used).

The reevaluator's keep decision itself reads only occupancy — an OCCUPIED
forward block keeps the CAUTION — and never the occupant's ``travel_direction``.
Retiring a margin the protecting train reversed away from is the Opposing-traversal
caution clear in ``reserve_blocks_for_path``, not this walk.
"""

import pytest

from track import Tile, TRACK_NS, TRACK_EW
from network import RailwayNetwork
from block import Block, CautionState
from block_manager import BlockManager
from entity_ids import segment_id_for_block


def _straight_chain_manager():
    """A -E-> B straight chain: BLOCK_A at (0,0) exits E into BLOCK_B at
    (1,0). The walk reaches B with chain_entry_edge == 'W' == opposite('E').
    """
    net = RailwayNetwork()
    a = Tile(x=0, y=0, track_bits=TRACK_EW)
    a.segment_id = segment_id_for_block("A")
    net.add_track(a)
    b = Tile(x=1, y=0, track_bits=TRACK_EW)
    b.segment_id = segment_id_for_block("B")
    net.add_track(b)
    net.blocks["A"] = Block(block_id="A")
    net.blocks["A"].ordered_tiles = [(0, 0, "E")]
    net.blocks["B"] = Block(block_id="B")
    net.blocks["B"].ordered_tiles = [(1, 0, "E")]
    return net, BlockManager(net.blocks, network=net)


def test_next_block_along_direction_returns_chain_entry_edge():
    """The walk must return a (Block, chain_entry_edge) tuple. For the
    straight A -E-> B chain the entry edge is 'W' (the side of B facing A)."""
    net, bm = _straight_chain_manager()
    result = bm._next_block_along_direction(net.blocks["A"], "E")
    assert isinstance(result, tuple) and len(result) == 2, (
        "_next_block_along_direction must return (Block, chain_entry_edge); "
        f"got {result!r}"
    )
    next_block, chain_entry_edge = result
    assert next_block is not None and next_block.block_id == "B"
    assert chain_entry_edge == "W", (
        f"straight chain entry edge should be 'W' (opposite of exit 'E'); "
        f"got {chain_entry_edge!r}"
    )


# ---------------------------------------------------------------------------
# release_train_occupancy: the no-detector teardown path
# ---------------------------------------------------------------------------


def test_release_train_occupancy_drops_every_held_block_without_caution():
    """The ghosted-train teardown and the adoption re-key have no detector
    observation to wait for, so they take the blocks back directly
    (design_new.md "BlockManager", `release_train_occupancy(train_id)`).

    It leaves no CAUTION behind: there is no train travelling on to protect.
    """
    net, manager = _straight_chain_manager()
    net.blocks["A"].occupied_by = "T1"
    net.blocks["B"].occupied_by = "T1"

    released = manager.release_train_occupancy("T1")

    assert sorted(released) == ["A", "B"]
    assert net.blocks["A"].occupied_by is None
    assert net.blocks["B"].occupied_by is None
    assert net.blocks["A"].caution is None
    assert net.blocks["B"].caution is None


def test_release_train_occupancy_leaves_other_trains_alone():
    net, manager = _straight_chain_manager()
    net.blocks["A"].occupied_by = "T1"
    net.blocks["B"].occupied_by = "T2"

    released = manager.release_train_occupancy("T1")

    assert released == ["A"]
    assert net.blocks["B"].occupied_by == "T2"


def test_release_train_occupancy_clears_a_chain_held_on_its_behalf():
    """A chain protecting the released train must not stay in CAUTION forever,
    which is why the release re-evaluates."""
    net, manager = _straight_chain_manager()
    net.blocks["B"].occupied_by = "T1"
    net.blocks["A"].caution = CautionState(exit_edge="E")

    manager.release_train_occupancy("T1")

    assert net.blocks["B"].occupied_by is None
    # Nothing is left ahead for A to protect, so A comes back out of CAUTION.
    assert net.blocks["A"].caution is None


def test_release_train_occupancy_is_a_no_op_for_an_unknown_train():
    net, manager = _straight_chain_manager()
    net.blocks["A"].occupied_by = "T1"

    assert manager.release_train_occupancy("GHOST") == []
    assert net.blocks["A"].occupied_by == "T1"


# ---------------------------------------------------------------------------
# network.remove_train: removal re-evaluates caution, it does not just clear
# ---------------------------------------------------------------------------


def test_remove_train_clears_a_caution_chain_it_was_protecting():
    """design_new.md "Block occupancy": there is no separate `clear_block()`
    step — removing a train goes through the occupancy-release path that
    re-evaluates caution, so a downstream chain the removed train held in CAUTION
    cannot stay there forever once the train that held it behind is gone.

    Before removal was wired to this path it cleared each occupied block with
    `clear_block()`, which never re-evaluates caution: block A stayed in CAUTION
    with no train left ahead to protect.
    """
    from train import Train

    net, manager = _straight_chain_manager()
    net.block_manager = manager

    # T1 stands on B; A exits E into B and is in CAUTION protecting the space
    # behind T1.
    net.trains["T1"] = Train(train_id="T1")
    net.blocks["B"].occupied_by = "T1"
    net.blocks["A"].caution = CautionState(exit_edge="E")
    assert net.blocks["A"].caution is not None

    assert net.remove_train("T1") is True

    # T1 and its occupancy are gone, and A comes back out of CAUTION because
    # nothing is left ahead for it to protect.
    assert "T1" not in net.trains
    assert net.blocks["B"].occupied_by is None
    assert net.blocks["A"].caution is None
