"""Journey: pin the directional CAUTION behavior for BLOCK_EAST_BRANCH_2 when
the demo train transits through the diagonal east-branch station block.

Regression coverage for block_manager.py `_next_block_along_direction`
(exit-tile loop, the bug fix that requires the tile's own track to have an
edge in the protect direction before treating it as a candidate exit).

Setup (see EastBranchTerminalScene): the train runs through a no-dwell
multi-stop route origin → East Terminal → east branch Station.

Sequence of interest:
  * Train reaches East Terminal, stops, immediately departs for east branch
    Station via the next path.
  * As the train enters the diagonal east-branch station block, its tail
    eventually vacates BLOCK_EAST_BRANCH_2 (tiles (9,5)/(9,6)).
  * At tail-vacate time, the east-branch station block ahead is OCCUPIED,
    so BLOCK_EAST_BRANCH_2 MUST enter CAUTION — not jump directly to FREE.
  * While the train is parked at east branch Station, BLOCK_EAST_BRANCH_2
    must REMAIN in CAUTION.

Before the fix, `_next_block_along_direction` mis-picked (9,5) (TRACK_NS,
no E edge) as the exit tile for direction="E"; the walk stepped into the
empty cell (10,5), failed, and the reevaluator force-cleared the caution.
"""

from enums import BlockState, TrainState

from ..assertions import assert_state
from ..events import Event, Journey
from ..invariants import no_eb, no_overspeed, state_cab_pair_valid
from ..triggers import block_vacated, train_stopped_for


_TARGET_BLOCK = "BLOCK_EAST_BRANCH_2"


def _assert_block_state(block_id: str, expected: BlockState):
    def check(s):
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id!r} missing from network"
        actual = block.state
        assert actual == expected, (
            f"{block_id} state={actual.value} expected={expected.value}; "
            f"caution={block.caution!r} occupied_by={block.occupied_by!r}"
        )

    return check


def _assert_forward_block_occupied(behind_id: str):
    """At the moment `behind_id` tail-vacates, the *next* forward block on
    the live topology chain must be OCCUPIED — this is the precondition
    that makes a CAUTION-not-FREE outcome the design-correct one. If this
    assertion ever fails, the test result is meaningless and we say so.
    """

    def check(s):
        block = s.network.blocks.get(behind_id)
        assert block is not None
        # Mirror BlockManager._next_block_along_direction's contract: the
        # block must be in CAUTION and have an exit_edge recorded.
        assert block.caution is not None, (
            f"{behind_id} expected to be in CAUTION at tail-vacate but "
            f"caution=None; state={block.state.value}"
        )
        bm = s.sim_controller.block_manager
        next_fwd, _entry_edge = bm._next_block_along_direction(
            block, block.caution.exit_edge
        )
        assert next_fwd is not None, (
            f"topology walk failed to find a forward block from {behind_id} "
            f"in direction {block.caution.exit_edge!r}; the test "
            f"precondition (forward chain reachable) is broken"
        )
        assert next_fwd.state == BlockState.OCCUPIED, (
            f"forward block {next_fwd.block_id} state="
            f"{next_fwd.state.value}, expected OCCUPIED — test precondition "
            f"(train still ahead) not satisfied"
        )

    return check


def build_east_branch_caution_journey(train_id: str = "DEMO_TRAIN") -> Journey:
    return Journey(
        train_id=train_id,
        events=[
            Event(
                name="block_east_branch_2_tail_vacated",
                trigger=block_vacated(_TARGET_BLOCK),
                assertions=[
                    _assert_forward_block_occupied(_TARGET_BLOCK),
                    _assert_block_state(_TARGET_BLOCK, BlockState.CAUTION),
                ],
            ),
            Event(
                name="train_stopped_at_east_branch_station",
                # Warmup just past the origin → East Terminal acceleration; the
                # no-dwell stop at East Terminal lasts only a tick or two, far
                # below settle_ticks=30, so it can't trip the trigger early.
                trigger=train_stopped_for(settle_ticks=30, after_tick=50),
                assertions=[
                    assert_state(TrainState.READY),
                    _assert_block_state(_TARGET_BLOCK, BlockState.CAUTION),
                ],
            ),
        ],
        invariants=[
            no_eb(),
            no_overspeed(),
            state_cab_pair_valid(),
        ],
    )
