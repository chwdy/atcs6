"""Journey: pin that a cab flip does NOT retire a CAUTION margin — the
reevaluator is occupancy-only — after a north-branch turnaround.

Setup (see NorthStationCautionTurnaroundScene): a single multi-stop
route drives the demo train origin -> North Station -> BLOCK_BRANCH_2
dead-end -> back to North Station. The return leg auto-flips the cab,
so the train ends parked at North Station facing south.

End-state contract (design_new.md _reevaluate_caution_blocks() section):

  * The station block at (3,2) is OCCUPIED by DEMO_TRAIN, cab=B,
    travel_direction=S.
  * BLOCK_BRANCH_2 (3,3) is still OCCUPIED by the train's rear. The train
    stops at the dead end at its starting speed with its rear in the
    station, so after the cab flip its head already stands in the station
    block, and the return stop completes there without moving (Stop-arrival
    check, condition 4b). BRANCH_2 is therefore not vacated, and carries no
    CAUTION margin to check.
  * BLOCK_BRANCH_1 (3,1) stays CAUTION with exit_edge=N. Its CAUTION was
    set when the train first passed through going north. After the cab flip
    the forward chain from BRANCH_1 in direction N still lands on the
    OCCUPIED station block, so the reevaluator — which reads only occupancy
    and connectivity, never the occupant's travel_direction — preserves it.
    The margin retires only when the train's new path reserves BRANCH_1 the
    other way (the Opposing-traversal caution clear in reserve_blocks_for_path),
    which this route never does.
"""

from enums import BlockState

from ..harness import TrainState

from ..assertions import assert_cab, assert_state
from ..events import Event, Journey
from ..invariants import no_eb, no_overspeed, state_cab_pair_valid
from ..triggers import route_run_removed_after_assigned


_BRANCH_1 = "BLOCK_BRANCH_1"
_BRANCH_2 = "BLOCK_BRANCH_2"


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


def _assert_branch_2_still_held():
    """BRANCH_2 still holds the train's rear: the return stop completed in
    place (Stop-arrival check, condition 4b), so the block was never vacated
    and no CAUTION margin was set on it."""

    def check(s):
        block = s.network.blocks.get(_BRANCH_2)
        assert block is not None
        assert block.state == BlockState.OCCUPIED, (
            f"{_BRANCH_2} state={block.state.value}, expected occupied by the "
            f"train's rear"
        )

    return check


def _assert_branch_1_caution_preserved():
    """BRANCH_1 must STAY CAUTION with exit_edge=N after the turnaround.

    Its northbound margin was set when the train first passed through going
    north. The reevaluator reads only occupancy and connectivity — never the
    occupant's `travel_direction` — so a cab flip does NOT retire the margin
    (design_new.md: the reevaluator's keep test is occupancy alone). The margin
    retires only when the train's new path reserves BRANCH_1 the other way (the
    Opposing-traversal caution clear in `reserve_blocks_for_path`) or the
    tail-vacate cascade clears it as the train leaves the protected chain —
    neither has happened here, so the margin correctly persists."""

    def check(s):
        block = s.network.blocks.get(_BRANCH_1)
        assert block is not None
        assert block.caution is not None, (
            f"{_BRANCH_1} caution should be PRESERVED after the cab flip — the "
            f"reevaluator no longer clears on reversal — but caution=None"
        )
        assert block.caution.exit_edge == "N", (
            f"{_BRANCH_1} caution.exit_edge={block.caution.exit_edge!r}, "
            f"expected 'N'"
        )

    return check


def build_north_station_caution_turnaround_journey(
    train_id: str = "DEMO_TRAIN",
) -> Journey:
    return Journey(
        train_id=train_id,
        events=[
            Event(
                name="turnaround_route_completes",
                trigger=route_run_removed_after_assigned(),
                assertions=[
                    assert_state(TrainState.READY),
                    assert_cab("B"),
                    _assert_branch_2_still_held(),
                    _assert_branch_1_caution_preserved(),
                ],
            ),
        ],
        invariants=[
            no_eb(),
            no_overspeed(),
            state_cab_pair_valid(),
        ],
    )
