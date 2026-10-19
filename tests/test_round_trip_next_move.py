"""The next-move read follows the ROUND_TRIP walk, turn-back included.

design_new.md "`find_next_move_target` canonical definition": the helper steps
the directed walk from the run's first unfinished walk-position by the same
rule `RouteRun._append_next_walk_task(...)` grows it with, so a ROUND_TRIP
occupant standing at an end group resolves the stop it turns back to — the
case the reversal-trap exclusion exists for. `None` means no next move, which
on a running walk is only a ONE_OFF walk with no move task left.
"""

from network import RailwayNetwork
from route_executor import RouteExecutionState, RouteExecutor
from tests.train_test_helpers import set_train_span
from enums import Direction
from train import Train
from train_route import BlockTarget, Route, RouteMode, RouteTask, RouteTaskRun


def _stop(block_id):
    return RouteTask(
        task_kind="stop",
        task_location=BlockTarget(block_id=block_id),
        task_direction="either",
    )


def _dwell(block_id):
    return RouteTask(
        task_kind="dwell",
        task_location=BlockTarget(block_id=block_id),
        task_time=5.0,
    )


def _executor_with_run(mode, tasks, rows):
    network = RailwayNetwork()
    network.blocks = {"BLK_A": object(), "BLK_B": object(), "BLK_C": object()}
    route = Route(route_id="R", name="r", mode=mode, tasks=tasks)
    route.assign_train("T")
    train = Train(train_id="T")
    network.trains["T"] = train
    executor = RouteExecutor(network)
    executor.register_route(route)
    controller = executor._get_controller(train)
    controller._apply_head_frame("end_a")
    set_train_span(controller, (0, 0), facing_direction=Direction.EAST)
    executor.execution_states["T"] = RouteExecutionState(
        train_id="T", route_id="R", started_at=0.0, task_runs=rows
    )
    return executor, route


def _round_trip_tasks():
    # Groups G0 = [A, dwell A], G1 = [B, dwell B], G2 = [C, dwell C].
    return [
        _stop("BLK_A"), _dwell("BLK_A"),
        _stop("BLK_B"), _dwell("BLK_B"),
        _stop("BLK_C"), _dwell("BLK_C"),
    ]


def test_round_trip_train_dwelling_at_the_far_end_turns_back():
    """Dwelling at the apex C (the forward end group), the next move is B going
    back, not "no move" — the occupant is about to reverse out."""
    rows = [RouteTaskRun(task_index=i, finished_at=float(i + 1)) for i in range(5)]
    rows.append(RouteTaskRun(task_index=5))  # dwell at C, still running
    executor, _route = _executor_with_run(
        RouteMode.ROUND_TRIP, _round_trip_tasks(), rows
    )

    target = executor.find_next_move_target("T")

    assert target is not None and target != "unresolved"
    assert target.requested_destination_block_id == "BLK_B"


def test_round_trip_train_on_the_way_back_resolves_the_previous_stop():
    """Dwelling at B on the reverse visit, the next move is A, not C."""
    rows = [RouteTaskRun(task_index=i, finished_at=float(i + 1)) for i in range(6)]
    rows.append(RouteTaskRun(task_index=2, walk_direction="reverse", finished_at=7.0))
    rows.append(RouteTaskRun(task_index=3, walk_direction="reverse"))  # dwell at B
    executor, _route = _executor_with_run(
        RouteMode.ROUND_TRIP, _round_trip_tasks(), rows
    )

    target = executor.find_next_move_target("T")

    assert target is not None and target != "unresolved"
    assert target.requested_destination_block_id == "BLK_A"


def test_round_trip_seed_at_the_far_end_sees_the_turn_back():
    """The hypothetical-seed walk used by NEAREST seeding shares the kernel: a
    run seeded at the last stop turns back to the stop before it."""
    executor, route = _executor_with_run(
        RouteMode.ROUND_TRIP, _round_trip_tasks(), [RouteTaskRun(task_index=0)]
    )
    resolver = executor._get_route_target_resolver()

    assert resolver._next_move_block_after_index(route, None, 4) == "BLK_B"


def test_one_off_train_dwelling_at_its_last_stop_has_no_next_move():
    """A ONE_OFF run dwelling at its last stop is still RUNNING but has no move
    left, so the helper returns None (exempt), not "unresolved"."""
    tasks = [_stop("BLK_A"), _stop("BLK_B"), _dwell("BLK_B")]
    rows = [
        RouteTaskRun(task_index=0, finished_at=1.0),
        RouteTaskRun(task_index=1, finished_at=2.0),
        RouteTaskRun(task_index=2),
    ]
    executor, _route = _executor_with_run(RouteMode.ONE_OFF, tasks, rows)

    assert executor.find_next_move_target("T") is None


def test_route_next_walk_step_matches_the_run_append():
    """`Route.next_walk_step(...)` is the step the run's append takes."""
    route = Route(
        route_id="R", name="r", mode=RouteMode.ROUND_TRIP, tasks=_round_trip_tasks()
    )
    run = RouteExecutionState(
        train_id="T", route_id="R", started_at=0.0,
        task_runs=[RouteTaskRun(task_index=i) for i in range(6)],
    )
    walk = []
    for pos in range(5, 15):
        run._append_next_walk_task(route, pos)
        row = run.task_runs[pos + 1]
        walk.append((row.task_index, row.walk_direction))
    expected = []
    step = (5, "forward")
    for _ in range(10):
        step = route.next_walk_step(*step)
        expected.append(step)
    assert walk == expected
    # C forward -> B reverse -> A reverse -> B forward.
    assert walk[:6] == [
        (2, "reverse"), (3, "reverse"), (0, "reverse"), (1, "reverse"),
        (2, "forward"), (3, "forward"),
    ]
