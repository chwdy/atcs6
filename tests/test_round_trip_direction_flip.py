"""Regression: ROUND_TRIP `direction` must flip when a bare-`stop` apex
completes through `handle_path_completion` (the Phase-3 stop-completion path).

design_new.md "Canonical Next-Target Selection" holds the canonical ROUND_TRIP
truth table; case `c733`
requires the forward apex to reflect: `(2, forward) -> (1, reverse)`.
design_new.md "Route-definition validation" sanctions a turnaround terminal
with no dwell as a bare
`stop` task. `RouteRun.direction` is flipped by `_advance_to_next_task`, which
every walk-advance site pairs with `advance_walk_position` EXCEPT
`handle_path_completion`. Without the flip, a ROUND_TRIP whose forward apex is
a bare stop (no trailing dwell, finished by driving rather than the
covered-stop path) oscillates at the top of the route — C -> B -> C -> B ...
— instead of reflecting C -> B -> A. A trailing dwell or the covered-stop
path masks the bug, which is why prior audits missed it.
"""

from enums import Direction
from network import RailwayNetwork
from route_executor import RouteExecutionState, RouteExecutor
from runtime_payloads import ResolvedRouteTarget
from tests.train_test_helpers import set_train_span
from train import Train
from train_route import BlockTarget, Route, RouteMode, RouteTask, RouteTaskRun


def _apex_ready_round_trip():
    """A 3-stop ROUND_TRIP [A, B, C] driven forward to the apex: A and B
    finished, the bare-`stop` apex C in-flight at walk position 2,
    `direction == "forward"`. The next stop completion is the forward apex.
    """
    network = RailwayNetwork()
    network.blocks = {"BLK_A": object(), "BLK_B": object(), "BLK_C": object()}

    def _stop(block_id):
        return RouteTask(
            task_kind="stop",
            task_location=BlockTarget(block_id=block_id),
            task_direction="forward",
        )

    route = Route(
        route_id="ROUTE_RT",
        name="Round-trip bare-stop apex",
        mode=RouteMode.ROUND_TRIP,
        tasks=[_stop("BLK_A"), _stop("BLK_B"), _stop("BLK_C")],
    )
    route.activate(0.0)
    route.assign_train("T_RT")

    train = Train(train_id="T_RT")
    network.trains[train.train_id] = train

    executor = RouteExecutor(network)
    executor.register_route(route)

    controller = executor._get_controller(train)
    controller.commands.cab = "A"
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
        direction="forward",
        task_runs=[
            RouteTaskRun(task_index=0, finished_at=10.0, resolved_target=_target("BLK_A")),
            RouteTaskRun(task_index=1, finished_at=20.0, resolved_target=_target("BLK_B")),
            RouteTaskRun(task_index=2, finished_at=None, resolved_target=_target("BLK_C")),
        ],
    )
    executor.execution_states[train.train_id] = exec_state
    return executor, exec_state, train, route


def test_round_trip_bare_stop_apex_flips_direction_and_reflects():
    executor, exec_state, train, route = _apex_ready_round_trip()
    assert exec_state.direction == "forward"

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
    # clocks itself — callers pass no clock value (design_new.md
    # "`RouteRun.advance_walk_position(walk_position)`"), so assert the pair was
    # written rather than pinning a caller-supplied instant.
    assert exec_state.task_runs[2].finished_at is not None
    assert exec_state.task_runs[2].finished_at_operating is not None
    # The forward apex must reflect: direction flips to reverse
    # (design_new.md "Canonical Next-Target Selection", case `c733`). This is
    # the assertion that FAILS
    # before the fix — handle_path_completion never called the flip helper.
    assert exec_state.direction == "reverse"
    # The reflected next walk-position targets B (task_index 1).
    assert exec_state.task_runs[3].task_index == 1

    # Reflection must CONTINUE to A, not bounce back to C. Drive the append
    # the way the next stop completion would (after marking B finished).
    exec_state.task_runs[3].finished_at = 40.0
    exec_state._append_next_walk_task(route)
    assert exec_state.task_runs[4].task_index == 0  # A — would be 2 (C) if unflipped
