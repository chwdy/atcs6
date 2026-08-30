"""Regression: best-effort reversal-trap exclusion at the demo_5 S4 turn-back.

Scenario (the user's "4-stop S-line behind a turn-back train"):
  - LEAD sits on the S4-S platform. On the loop route its next move reverses
    back out through BLK_X2_TO_S4_S + SW_X2 to PLT_S3_N (a turn-back).
  - FOLLOW comes up the one-way S track toward its own S4-S stop.

S4-S is NOT a geometric dead-end (a loop tunnel links the S4 end back to S1),
so the deadlock is route-driven: it happens only because LEAD's route reverses.
Per the reversal-trap exclusion (design_new.md best-effort `create_path()`,
item 11), FOLLOW's best-effort authority must stop BEFORE the X2 crossover so
LEAD can always reverse out. Before the fix FOLLOW truncated to BLK_X2_TO_S4_S
(past X2), locking SW_X2 and reserving the block LEAD needed -> deadlock.

The negative test pins that this is route-aware, not geometry: an occupant with
no resolvable reversing next move does NOT restrict the follower (FOLLOW still
truncates onto BLK_X2_TO_S4_S, today's tight-follow behavior).
"""

import contextlib
import io
import time

import pytest

from bootstrap import bootstrap_runtime
from runtime_payloads import AcquisitionMode
from route_executor import RouteExecutionState, RouteRunStatus
from train_route import RouteTaskRun


pytestmark = pytest.mark.scene("demo_5")


# Loop route task index 7 is the STN_4 dwell; index 8 is the stop at PLT_S3_N.
# Seating LEAD with tasks 0..6 finished and the dwell (7) current makes its
# next *move* resolve to PLT_S3_N -> a reverse back through X2.
_S4_DWELL_PHASE_IDX = 7


def _stage(assign_lead_route: bool):
    """Build demo_5 and seat LEAD on S4-S + FOLLOW one block back on the S
    track. When `assign_lead_route` is True, LEAD is put on the loop route
    dwelling at STN_4 (next move = the PLT_S3_N turn-back).
    """
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_5"
        )

    sim.add_train(
        train_id="LEAD", starting_block_id="PLT_S4_S_BLOCK",
        initial_direction="WEST", max_speed_kmh=40.0, length_cm=50.0,
        train_type="LOCOMOTIVE", initial_cab="A",
    )
    if assign_lead_route:
        ex = sim.route_executor
        route = ex.route_registry.get("R_DEMO5_LOOP")
        assert route is not None
        route.assign_train("LEAD")
        now = time.time()
        st = RouteExecutionState(
            train_id="LEAD", route_id=route.route_id, started_at=now,
            run_number=ex._allocate_run_number(),
            current_route_status=RouteRunStatus.RUNNING,
        )
        for i in range(_S4_DWELL_PHASE_IDX):
            st.task_runs.append(RouteTaskRun(task_index=i, finished_at=now))
        st.task_runs.append(RouteTaskRun(task_index=_S4_DWELL_PHASE_IDX))
        ex.execution_states["LEAD"] = st

    sim.add_train(
        train_id="FOLLOW", starting_block_id="PLT_S3_S_BLOCK",
        initial_direction="NORTH", max_speed_kmh=40.0, length_cm=50.0,
        train_type="LOCOMOTIVE", initial_cab="A", start_tile=(12, 11),
    )
    for _ in range(3):  # settle occupancy (LEAD/FOLLOW unpowered -> inert)
        sim.update(0.01)
    assert network.blocks["PLT_S4_S_BLOCK"].occupied_by == "LEAD"
    return network, dispatcher, sim


def _follow_best_effort_to_s4(dispatcher):
    return dispatcher.create_path(
        train_id="FOLLOW",
        requested_destination_block_id="PLT_S4_S_BLOCK",
        requested_destination_direction="forward",
        acquisition_mode=AcquisitionMode.BEST_EFFORT,
    )


def test_reversing_occupant_blocks_follower_before_crossover():
    network, dispatcher, sim = _stage(assign_lead_route=True)
    ex = sim.route_executor

    # LEAD's next move is the PLT_S3_N turn-back (skips the dwell task).
    assert ex.find_next_move_target("LEAD") == "PLT_S3_N_BLOCK"

    # Trap helper: terminating at the X2 approach traps LEAD; the block before
    # X2 does not (only the occupant's immediate exit block traps).
    assert dispatcher._terminal_traps_reversing_occupant(
        "PLT_S4_S_BLOCK", "BLK_X2_TO_S4_S", "FOLLOW") is True
    assert dispatcher._terminal_traps_reversing_occupant(
        "PLT_S4_S_BLOCK", "BLK_S3_S_TO_X2", "FOLLOW") is False

    res = _follow_best_effort_to_s4(dispatcher)
    assert res.success
    path = dispatcher.get_active_path("FOLLOW")
    auth_end = path.get_authority_end_block_id()
    assert auth_end == "BLK_S3_S_TO_X2", (
        f"FOLLOW must truncate before X2; got {auth_end!r} "
        f"(pre-fix this was 'BLK_X2_TO_S4_S', past X2)."
    )
    seq = [getattr(e, "resource_id", None) for e in path.resource_sequence]
    assert "SW_X2" not in seq and "BLK_X2_TO_S4_S" not in seq

    # Exit stays clear for LEAD.
    assert network.blocks["BLK_X2_TO_S4_S"].reserved_by_path is None
    assert not network.switches["SW_X2"].locks

    # End-to-end: LEAD's turn-back can now be created (deadlock broken).
    le_ok = any(
        dispatcher.create_path(
            train_id="LEAD",
            requested_destination_block_id="PLT_S3_N_BLOCK",
            requested_destination_direction=d,
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        ).success
        for d in ("forward", "reverse")
    )
    assert le_ok, "LEAD turn-back must be creatable once FOLLOW stays before X2"


def test_non_reversing_occupant_still_held_back_by_junction_rule():
    """The junction-fouling exclusion is occupancy-independent. Even with NO
    reversing occupant (so the reversal-trap does not apply), the follower
    still holds back before X2 rather than parking on the two-way block past
    it. BLK_X2_TO_S4_S is two-way and sits straight off SW_X2, so a truncated
    authority stops at BLK_S3_S_TO_X2 (before X2), keeping the crossover clear.
    This is what makes the rule deadlock-proof without a timing window: the
    follower never fouls the junction, whether or not an opposing train is
    already there.
    """
    network, dispatcher, sim = _stage(assign_lead_route=False)

    # No reversing occupant: the reversal-trap exclusion does NOT fire here.
    assert sim.route_executor.find_next_move_target("LEAD") is None
    assert dispatcher._terminal_traps_reversing_occupant(
        "PLT_S4_S_BLOCK", "BLK_X2_TO_S4_S", "FOLLOW") is False

    res = _follow_best_effort_to_s4(dispatcher)
    assert res.success
    auth_end = dispatcher.get_active_path("FOLLOW").get_authority_end_block_id()
    assert auth_end == "BLK_S3_S_TO_X2", (
        f"The junction-fouling exclusion must hold FOLLOW before X2 even with "
        f"no reversing occupant (BLK_X2_TO_S4_S is a two-way block off SW_X2); "
        f"got {auth_end!r}."
    )
    # The exit and crossover stay clear, exactly as in the reversing case.
    seq = [getattr(e, "resource_id", None)
           for e in dispatcher.get_active_path("FOLLOW").resource_sequence]
    assert "SW_X2" not in seq and "BLK_X2_TO_S4_S" not in seq


def test_caution_terminal_block_is_exempt_from_junction_rule():
    """Caution carve-out (design_new.md item 11): the junction-fouling exclusion
    yields when the two-way terminal block off the switch is in CAUTION. A
    caution block is one a leader has just passed forward through this junction,
    so the follower is allowed to close up onto it at the caution speed cap
    instead of being held a block back.

    Same staging as the held-back case, but BLK_X2_TO_S4_S is put in CAUTION (as
    LEAD's forward passage to S4-S would have left it). The exclusion must now
    let FOLLOW's best-effort authority terminate ON BLK_X2_TO_S4_S, rather than
    stopping short at BLK_S3_S_TO_X2 as it does for a non-caution two-way block.
    """
    from block import CautionState

    network, dispatcher, sim = _stage(assign_lead_route=False)
    foul_block = network.blocks["BLK_X2_TO_S4_S"]

    # The candidate segment FOLLOW would truncate (two-way block off SW_X2).
    cands = dispatcher._find_walk_candidates(
        "PLT_S3_S_BLOCK", "PLT_S4_S_BLOCK", "FOLLOW")
    assert cands, "expected a walk from S3-S to S4-S"
    segment = cands[0].walk
    assert "BLK_X2_TO_S4_S" in segment.blocks

    # Baseline: with no caution the exclusion holds the terminal back.
    assert foul_block.caution is None
    assert dispatcher._terminal_fouls_two_way_switch_block(
        segment, "BLK_X2_TO_S4_S") is True

    # Carve-out: with the block in CAUTION the exclusion yields.
    foul_block.caution = CautionState(exit_edge="W")  # leader vacated toward S4-S
    assert dispatcher._terminal_fouls_two_way_switch_block(
        segment, "BLK_X2_TO_S4_S") is False, (
        "a two-way block in CAUTION must be exempt from the junction-fouling "
        "exclusion so a follower can close up behind the leader"
    )

    # End to end: FOLLOW's best-effort authority now closes up ONTO the caution
    # block, instead of stopping a block short at BLK_S3_S_TO_X2.
    res = _follow_best_effort_to_s4(dispatcher)
    assert res.success
    auth_end = dispatcher.get_active_path("FOLLOW").get_authority_end_block_id()
    assert auth_end == "BLK_X2_TO_S4_S", (
        f"the caution carve-out must let FOLLOW terminate on the caution block "
        f"off SW_X2; got {auth_end!r}"
    )
