"""Regression for H3: CAUTION -> FREE reversal must compare the occupant
train's ``travel_direction`` against the protection chain's ACTUAL
chain-entry edge at the occupant block, not the geometric opposite of the
CAUTION block's ``caution.exit_edge``.

Per design_new.md line 842: "the same connectivity walk that found the
forward block also yields the edge through which the chain enters that
block (for a straight adjacent block this is simply the side facing the
CAUTION block; through switch tiles and curves it is the entry edge the
walk actually used)" and CAUTION clears "only when that travel_direction
points back out through that chain-entry edge."

For a straight adjacent block the chain-entry edge equals
``opposite(caution_exit_edge)``, so the existing straight-turnaround
caution journey tests cannot distinguish the corrected behaviour from the
old one. This test pins the two load-bearing pieces directly, with no
dependency on switch-state geometry:

  1. ``_next_block_along_direction`` returns a 2-tuple
     ``(Block, chain_entry_edge)`` — the walk hands back the entry edge
     instead of discarding it.
  2. ``_forward_train_has_reversed`` keys the clear decision on the
     PASSED chain-entry edge (``travel_direction == chain_entry_edge``),
     not on ``opposite(caution_exit_edge)``. When the entry edge differs
     from that opposite (the switch-leg / curved case), the decision
     genuinely flips, which is the whole point of H3.
"""

import pytest

from track import Tile, TRACK_NS, TRACK_EW
from network import RailwayNetwork
from block import Block, CautionState
from block_manager import BlockManager


class _FakeController:
    def __init__(self, travel_direction):
        self.travel_direction = travel_direction


class _FakeSimulationController:
    """Minimal stand-in exposing only the back-reference accessor that
    ``BlockManager`` uses to read the occupant's travel direction."""

    def __init__(self, travel_direction):
        self._travel_direction = travel_direction

    def get_or_create_controller(self, train_id):
        return _FakeController(self._travel_direction)


def _straight_chain_manager():
    """A -E-> B straight chain: BLOCK_A at (0,0) exits E into BLOCK_B at
    (1,0). The walk reaches B with chain_entry_edge == 'W' == opposite('E').
    """
    net = RailwayNetwork()
    a = Tile(x=0, y=0, track_bits=TRACK_EW)
    a.segment_id = "A"
    net.add_track(a)
    b = Tile(x=1, y=0, track_bits=TRACK_EW)
    b.segment_id = "B"
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


@pytest.mark.parametrize(
    "chain_entry_edge,travel_direction,expected_reversed",
    [
        # Switch-leg / curved case: the chain enters the occupant block from
        # the N side even though the CAUTION block exited E. The occupant has
        # flipped its cab and now travels N — back OUT through the chain-entry
        # edge — so CAUTION must clear. The OLD code compared travel_direction
        # to opposite(exit_edge) == 'W' and would have KEPT caution here.
        ("N", "N", True),
        # Same geometry, occupant travels W (the OLD opposite-of-exit edge):
        # it is NOT heading back through the actual chain-entry edge 'N', so
        # CAUTION must be kept. The OLD code would have CLEARED here.
        ("N", "W", False),
        # Straight case (entry edge == opposite(exit)): both old and new
        # agree. Occupant heading back through 'W' clears.
        ("W", "W", True),
        ("W", "N", False),
    ],
)
def test_forward_train_has_reversed_keys_on_chain_entry_edge(
    chain_entry_edge, travel_direction, expected_reversed
):
    """``_forward_train_has_reversed`` clears CAUTION iff the occupant's
    ``travel_direction`` equals the PASSED chain-entry edge — independent of
    the CAUTION block's exit edge."""
    net = RailwayNetwork()
    occ_tile = Tile(x=1, y=1, track_bits=TRACK_NS)
    occ_tile.segment_id = "OCC"
    net.add_track(occ_tile)
    net.blocks["OCC"] = Block(block_id="OCC")
    net.blocks["OCC"].ordered_tiles = [(1, 1, "S")]
    net.blocks["OCC"].occupy(train_id="T_OCC")

    bm = BlockManager(net.blocks, network=net)
    bm.simulation_controller = _FakeSimulationController(travel_direction)

    assert (
        bm._forward_train_has_reversed(net.blocks["OCC"], chain_entry_edge)
        is expected_reversed
    )


def test_forward_train_has_reversed_keeps_caution_without_back_reference():
    """No SimulationController back-reference => never escalate to a clear
    (positive evidence of reversal is required)."""
    net = RailwayNetwork()
    occ_tile = Tile(x=1, y=1, track_bits=TRACK_NS)
    occ_tile.segment_id = "OCC"
    net.add_track(occ_tile)
    net.blocks["OCC"] = Block(block_id="OCC")
    net.blocks["OCC"].ordered_tiles = [(1, 1, "S")]
    net.blocks["OCC"].occupy(train_id="T_OCC")
    bm = BlockManager(net.blocks, network=net)
    bm.simulation_controller = None
    assert bm._forward_train_has_reversed(net.blocks["OCC"], "N") is False
