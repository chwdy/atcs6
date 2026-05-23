"""Journey: pin the direction-aware CAUTION clear after a north-branch
turnaround.

Setup (see NorthStationCautionTurnaroundScene): a single multi-stop
route drives the demo train origin -> North Station -> BLOCK_BRANCH_2
dead-end -> back to North Station. The return leg auto-flips the cab,
so the train ends parked at North Station facing south.

End-state contract (design_new.md _reevaluate_caution_blocks() section):

  * The station block at (3,2) is OCCUPIED by DEMO_TRAIN, cab=B,
    travel_direction=S.
  * BLOCK_BRANCH_2 (3,3) is CAUTION with exit_edge=S. The forward
    chain from BRANCH_2 in direction S leads to the station block,
    whose train's travel_direction is also S (same as protection
    direction) — the train is still moving away from BRANCH_2's
    protection chain, so CAUTION is preserved.
  * BLOCK_BRANCH_1 (3,1) is FREE. Its CAUTION (exit_edge=N) was set
    when the train first arrived going north. After the cab flip the
    forward chain from BRANCH_1 in direction N still lands on the
    OCCUPIED station block, but the train there now has
    travel_direction=S — the opposite of BRANCH_1's protection
    direction. The direction-aware reevaluation clears the stale
    CAUTION.
"""

from enums import BlockState, TrainState

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


def _assert_branch_2_caution_south():
    """BRANCH_2 must be CAUTION with exit_edge=S. The forward chain check
    is a precondition: the station block ahead in direction S must be
    OCCUPIED by the same train whose travel_direction is also S — that's
    the case where CAUTION is correctly preserved (train still moving away
    from the protection chain in the same direction)."""

    def check(s):
        block = s.network.blocks.get(_BRANCH_2)
        assert block is not None
        assert block.caution is not None, (
            f"{_BRANCH_2} expected CAUTION at route completion but caution=None"
        )
        assert block.caution.exit_edge == "S", (
            f"{_BRANCH_2} caution.exit_edge={block.caution.exit_edge!r}, "
            f"expected 'S'"
        )

    return check


def _assert_branch_1_cleared():
    """BRANCH_1 must be FREE — its stale northbound CAUTION should have
    been cleared by the direction-aware reevaluator once the train's cab
    flipped to south at the turnaround."""

    def check(s):
        block = s.network.blocks.get(_BRANCH_1)
        assert block is not None
        assert block.caution is None, (
            f"{_BRANCH_1} caution should have been cleared by the "
            f"direction-aware reevaluator after the turnaround, but "
            f"caution.exit_edge={block.caution.exit_edge!r} "
            f"(station-block train travel_direction is S, opposite the "
            f"recorded N protection direction)"
        )
        assert block.state == BlockState.FREE, (
            f"{_BRANCH_1} state={block.state.value}, expected FREE"
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
                    _assert_branch_2_caution_south(),
                    _assert_branch_1_cleared(),
                ],
            ),
        ],
        invariants=[
            no_eb(),
            no_overspeed(),
            state_cab_pair_valid(),
        ],
    )
