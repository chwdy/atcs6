"""Regression: ROUND_TRIP walk reflection via per-row `walk_direction`.

design_new.md "RouteRun._append_next_walk_task" (line 1857) and the "RouteTaskRun
schema" (line 1836) hold the canonical ROUND_TRIP reflection contract: each
`RouteTaskRun` stores the direction of its own visit in `walk_direction`, and
`_append_next_walk_task(from_walk_position)` reflects at task-GROUP boundaries —
a group being one non-`dwell` task plus its contiguous `dwell` rows. The append
helper never reads or writes the run-level `RouteRun.direction` (whose only
writer is Plan at steps 592a/66aa, line 1774).

Two things are pinned here:

1. A bare-`stop` apex reflects `(2, forward) -> (1, reverse) -> (0, reverse)`
   when finished through `handle_path_completion` — the dispatcher stop path,
   which drives the append through `advance_walk_position` alone with no
   separate direction flip.
2. A ROUND_TRIP *with dwells* keeps each dwell attached to its own stop on both
   the forward and the reverse visit. The pre-fix task-level reflection
   (`task_count - 2` / `1` arithmetic) detached dwells from their stops and
   repeated the apex — a broken walk for any round trip with a station hold,
   which is the ordinary way a hold is authored (line 1641).
"""

from enums import Direction
from network import RailwayNetwork
from route_executor import RouteExecutionState, RouteExecutor
from runtime_payloads import ResolvedRouteTarget
from tests.train_test_helpers import set_train_span
from train import Train
from train_route import BlockTarget, Route, RouteMode, RouteTask, RouteTaskRun


def _stop(block_id):
    return RouteTask(
        task_kind="stop",
        task_location=BlockTarget(block_id=block_id),
        task_direction="forward",
    )


def _dwell(block_id):
    return RouteTask(
        task_kind="dwell",
        task_location=BlockTarget(block_id=block_id),
        task_time=5.0,
    )


def _apex_ready_round_trip():
    """A 3-stop ROUND_TRIP [A, B, C] driven forward to the apex: A and B
    finished, the bare-`stop` apex C in-flight at walk position 2. The next
    stop completion is the forward apex.
    """
    network = RailwayNetwork()
    network.blocks = {"BLK_A": object(), "BLK_B": object(), "BLK_C": object()}

    route = Route(
        route_id="ROUTE_RT",
        name="Round-trip bare-stop apex",
        mode=RouteMode.ROUND_TRIP,
        tasks=[_stop("BLK_A"), _stop("BLK_B"), _stop("BLK_C")],
    )
    route.assign_train("T_RT")

    train = Train(train_id="T_RT")
    network.trains[train.train_id] = train

    executor = RouteExecutor(network)
    executor.register_route(route)

    controller = executor._get_controller(train)
    controller._apply_head_frame("end_a")
    set_train_span(controller, (0, 0), facing_direction=Direction.EAST)

    def _target(block_id):
        return ResolvedRouteTarget(
            requested_destination_block_id=block_id,
            requested_destination_direction="forward",
        )

    exec_state = RouteExecutionState(
        train_id=train.train_id,
        route_id=route.route_id,
        started_at=0.0,
        task_runs=[
            RouteTaskRun(task_index=0, finished_at=10.0, resolved_target=_target("BLK_A")),
            RouteTaskRun(task_index=1, finished_at=20.0, resolved_target=_target("BLK_B")),
            RouteTaskRun(task_index=2, finished_at=None, resolved_target=_target("BLK_C")),
        ],
    )
    executor.execution_states[train.train_id] = exec_state
    return executor, exec_state, train, route


def test_round_trip_bare_stop_apex_reflects_via_walk_direction():
    executor, exec_state, train, route = _apex_ready_round_trip()

    # The apex stop is the lowest non-finished walk-position (list position 2);
    # `try_complete_active_path` resolves it via `find_next_completion_task(...)`
    # and hands that walk-position to the callback (design_new.md step `bc55`).
    ok = executor.handle_path_completion(
        train_id=train.train_id,
        path_id="P_APEX",
        walk_position=2,
    )
    assert ok is True
    # The apex stop is finished through the canonical writer, which samples both
    # clocks itself (design_new.md "`RouteExecutor.advance_walk_position(...)`").
    assert exec_state.task_runs[2].finished_at is not None
    assert exec_state.task_runs[2].finished_at_operating is not None

    # The forward apex reflects to B through the appended row's stored
    # `walk_direction` (design_new.md line 1857), NOT a `RouteRun.direction`
    # flip: the run-level direction is Plan-armed at 592a/66aa (line 1774) and is
    # not touched by the completion callback.
    assert exec_state.task_runs[3].task_index == 1
    assert exec_state.task_runs[3].walk_direction == "reverse"

    # Reflection must CONTINUE to A (reverse), not bounce back to C. Drive the
    # append the way the next stop completion would (after marking B finished).
    exec_state.task_runs[3].finished_at = 40.0
    exec_state._append_next_walk_task(route, 3)
    assert exec_state.task_runs[4].task_index == 0  # A — would be 2 (C) if unreflected
    assert exec_state.task_runs[4].walk_direction == "reverse"


def test_round_trip_with_dwells_reflects_at_group_boundaries():
    """ROUND_TRIP [A, dwellA, B, dwellB] — two task groups G0={0,1}, G1={2,3}.

    The correct directed walk keeps each dwell with its stop on both visits:
    A,dwellA,B,dwellB (forward) -> A,dwellA (reverse) -> B,dwellB (forward) ...
    The pre-fix task-level reflection produced A,dwellA,B,dwellB,B,dwellA,A,...
    (dwell detached, apex repeated).
    """
    route = Route(
        route_id="ROUTE_RT_DWELL",
        name="Round-trip with dwells",
        mode=RouteMode.ROUND_TRIP,
        tasks=[_stop("BLK_A"), _dwell("BLK_A"), _stop("BLK_B"), _dwell("BLK_B")],
    )
    exec_state = RouteExecutionState(
        train_id="T_RTD",
        route_id=route.route_id,
        started_at=0.0,
        task_runs=[RouteTaskRun(task_index=i) for i in range(4)],
    )

    # (task_index, walk_direction) expected at each walk-position.
    expected = [
        (0, "forward"), (1, "forward"), (2, "forward"), (3, "forward"),  # forward leg
        (0, "reverse"), (1, "reverse"),   # reflect: group G0 in reverse
        (2, "forward"), (3, "forward"),   # reflect: group G1 forward again
        (0, "reverse"), (1, "reverse"),
    ]

    # Drive the walk the way `advance_walk_position` does: finish the current
    # walk-position and append from it. The append is a no-op except when the
    # finished row is the current tail, so the pre-allocated forward leg is
    # consumed before reflection begins.
    for i in range(len(expected) - 1):
        exec_state.task_runs[i].finished_at = float(i + 1)
        exec_state._append_next_walk_task(route, i)
        row = exec_state.task_runs[i + 1]
        assert (row.task_index, row.walk_direction) == expected[i + 1], (
            f"walk-position {i + 1}: got ({row.task_index}, {row.walk_direction}), "
            f"want {expected[i + 1]}"
        )

    # Every dwell row is immediately preceded on the walk by its own stop — the
    # invariant the pre-fix task-level reflection violated.
    for i, row in enumerate(exec_state.task_runs):
        if route.tasks[row.task_index].task_kind == "dwell":
            assert i > 0
            prev = exec_state.task_runs[i - 1]
            assert route.tasks[prev.task_index].task_kind == "stop"
            assert prev.task_index == row.task_index - 1
