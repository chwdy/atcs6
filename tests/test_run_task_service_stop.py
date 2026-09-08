"""Regression: a `run` task's timed stop completes as a stop at the run's
current authority end, through the ordinary stop-arrival check — never through
a service brake or a run-progress cancel.

design_new.md "run task" / Planning Kernel step `6a0d`: while
`RouteTaskRun.run_elapsed_seconds < task_time`, the step accumulates
`delta_seconds` and keeps rolling. Once the window elapses it freezes the run
row's `resolved_target` on the live authority-end block and stops acquiring
onward authority; with no fresh authority granted, the train brakes to that
authority end under ordinary ATC, and the ordinary stop-arrival check completes
it there — `find_next_completion_task` presents the elapsed run as
`task_kind == "stop"`. There is no `begin_service_stop` and no `run_complete`
cancel.

This is also the regression guard for the H2 fatal-freeze bug: a run task still
in flight is the served completion projection with `task_kind == "run"`, which
the stop-arrival check rejects; only an elapsed run presents as a stop.
"""

from enums import Direction, TrainState
from network import RailwayNetwork
from route_executor import ResolvedNextTaskProjection, RouteExecutionState, RouteExecutor
from runtime_payloads import ResolvedRouteTarget
from tests.train_test_helpers import make_synthetic_path, set_train_span
from train import Train
from train_route import BlockTarget, Route, RouteMode, RouteTask, RouteTaskRun


class _StubDispatcher:
    """Minimal stand-in exposing both surfaces `_run_task_step` reads: the
    controller's `received_path_plan` (via `get_active_path`) and
    `RouteExecutor._get_active_path_id` (via `active_path_id_by_train`).
    Both the controller and the executor share this one instance so a
    cleared path is invisible to both at once, like the real dispatcher.
    """

    def __init__(self):
        self.paths_by_train = {}

    def get_active_path(self, train_id):
        path = self.paths_by_train.get(train_id)
        if path is None:
            return None
        return path if path.is_active() else None

    def active_path_id_by_train(self, train_id):
        path = self.get_active_path(train_id)
        return path.path_id if path is not None else None


def _run_task_ready(task_time=20.0):
    network = RailwayNetwork()
    network.blocks = {"BLK_A": object(), "BLK_B": object()}

    task = RouteTask(
        task_kind="run",
        task_location=BlockTarget(block_id="BLK_B"),
        task_time=task_time,
    )
    route = Route(
        route_id="ROUTE_RUN",
        name="Timed run",
        mode=RouteMode.ONE_OFF,
        tasks=[task],
    )
    route.activate(0.0)
    route.assign_train("T_RUN")

    train = Train(train_id="T_RUN")
    network.trains[train.train_id] = train

    executor = RouteExecutor(network)
    executor.register_route(route)

    controller = executor._get_controller(train)
    controller.commands.cab = "A"
    controller.commands.control_mode = "ato"
    controller.ops.state = TrainState.RUNNING_MOVING
    controller.ato.engaged = True
    set_train_span(controller, (0, 0), facing_direction=Direction.EAST)

    stub = _StubDispatcher()
    path = make_synthetic_path(
        train_id=train.train_id, block_id="BLK_B", block_tiles=[(0, 0)]
    )
    stub.paths_by_train[train.train_id] = path
    controller.path_dispatcher = stub
    executor.path_dispatcher = stub

    target = ResolvedRouteTarget(
        requested_destination_block_id="BLK_B",
        requested_destination_direction="forward",
    )
    exec_state = RouteExecutionState(
        train_id=train.train_id,
        route_id=route.route_id,
        started_at=0.0,
        task_runs=[RouteTaskRun(task_index=0, resolved_target=target)],
    )
    executor.execution_states[train.train_id] = exec_state

    projection = ResolvedNextTaskProjection(
        walk_position=0,
        task_kind="run",
        direction="forward",
        resolved_target=target,
    )
    return executor, exec_state, controller, train, route, projection, stub


def test_run_task_accrues_timer_and_keeps_rolling_within_window():
    executor, exec_state, controller, train, route, projection, _stub = (
        _run_task_ready(task_time=20.0)
    )
    row = exec_state.task_runs[0]
    controller.physics_sync.current_speed_kmh = 40.0  # moving

    still_rolling = executor._run_task_step(
        train, route, exec_state, controller, projection, now=8.0, delta_seconds=8.0
    )

    assert still_rolling is False  # caller falls through to authority acquisition
    assert row.run_elapsed_seconds == 8.0
    assert controller.ato.engaged is True  # intent untouched while still rolling


def test_run_task_timer_elapses_freezes_authority_end_without_cancelling():
    executor, exec_state, controller, train, route, projection, stub = (
        _run_task_ready(task_time=20.0)
    )
    row = exec_state.task_runs[0]
    controller.physics_sync.current_speed_kmh = 40.0  # still moving

    # First tick takes it to 8s (under window); second crosses the 20s mark.
    executor._run_task_step(
        train, route, exec_state, controller, projection, now=8.0, delta_seconds=8.0
    )
    done = executor._run_task_step(
        train, route, exec_state, controller, projection, now=23.0, delta_seconds=15.0
    )

    assert done is True  # caller must not acquire fresh authority
    assert row.run_elapsed_seconds == 20.0  # capped at task_time, not 23.0
    # A timed-run stop does not clear ATO intent (design_new.md "ato.engaged").
    assert controller.ato.engaged is True
    # Completion is dispatcher-side stop-arrival, not this step.
    assert row.finished_at is None

    # The run is now a stop at the current authority end: its resolved_target is
    # frozen on that block, and NO cancel is latched (design_new.md "run task").
    path = stub.paths_by_train[train.train_id]
    assert (
        row.resolved_target.requested_destination_block_id
        == path.get_authority_end_block_id()
    )
    assert controller.route_hooks.phase_latches.path_cancel_request is None


def test_run_task_presents_as_a_stop_at_authority_end_once_elapsed():
    executor, exec_state, controller, train, route, projection, stub = (
        _run_task_ready(task_time=20.0)
    )
    controller.physics_sync.current_speed_kmh = 40.0
    executor._run_task_step(
        train, route, exec_state, controller, projection, now=8.0, delta_seconds=8.0
    )
    # Before expiry the served completion projection is a `run` (the stop-arrival
    # check rejects it).
    pre = executor.find_next_completion_task(train.train_id)
    assert pre is not None and pre.task_kind == "run"

    # Elapse the timer.
    executor._run_task_step(
        train, route, exec_state, controller, projection, now=23.0, delta_seconds=15.0
    )

    # Now the elapsed run presents as a `stop` at the authority end, so the
    # ordinary stop-arrival check completes it there.
    post = executor.find_next_completion_task(train.train_id)
    assert post is not None
    assert post.task_kind == "stop"
    path = stub.paths_by_train[train.train_id]
    assert (
        post.stored_resolved_target.requested_destination_block_id
        == path.get_authority_end_block_id()
    )
    # _run_task_step itself never finishes the run — the dispatcher's stop
    # completion does.
    assert exec_state.task_runs[0].finished_at is None


def test_run_task_is_the_served_projection_and_never_completes_as_a_stop():
    """The H2 regression guard, at its doc-correct home.

    A `run` task's live authority must never be finished by dispatcher stop
    completion. Under design_new.md's Stop-arrival check,
    `PathDispatcher.try_complete_active_path(...)` resolves the served row
    through `RouteExecutor.find_next_completion_task(...)` and accepts an arrival
    ONLY when that row's `task_kind == "stop"`. `find_next_completion_task`
    returns the lowest non-finished walk-position, so while the run task is
    still in flight it — not any later stop — is the served projection, and its
    `task_kind == "run"` makes the arrival predicate false. The run's authority
    is released by Plan step `6a0d`, never completed here.

    `handle_path_completion(...)` no longer searches `task_runs` for a matching
    stop (it finishes exactly the resolved walk-position), so a healthy run can
    neither be skipped into a later stop nor trip
    `SimulationController.enter_failed_runtime(...)`.
    """
    executor, exec_state, controller, train, route, _projection, _stub = (
        _run_task_ready(task_time=20.0)
    )
    # Append a trailing stop after the run so the walk has a real stop later:
    # the served completion projection must still be the run, not that stop.
    route.tasks.append(
        RouteTask(task_kind="stop", task_location=BlockTarget(block_id="BLK_A"))
    )
    exec_state.task_runs.append(
        RouteTaskRun(
            task_index=1,
            resolved_target=ResolvedRouteTarget(
                requested_destination_block_id="BLK_A",
                requested_destination_direction="forward",
            ),
        )
    )

    # The lowest non-finished walk-position is the run task at position 0 — the
    # served completion projection. Its task_kind is "run", which the
    # Stop-arrival check rejects, so completion never fires for it and the later
    # stop is never reached while the run is in flight.
    projection = executor.find_next_completion_task(train.train_id)
    assert projection is not None
    assert projection.walk_position == 0
    assert projection.task_kind == "run"
