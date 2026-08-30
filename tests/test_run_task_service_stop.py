"""Regression: a `run` task's timed stop drives to completion through
`TrainController.begin_service_stop()` / `consume_service_stop()`, never
through dispatcher-committed stop completion.

design_new.md Planning Kernel step `6a0d` ("Run timer, service stop, and
normal-progress authority release"): while `RouteTaskRun.run_elapsed_seconds
< task_time`, the step accumulates `delta_seconds` and keeps rolling. Once
the window elapses, it calls the idempotent `TrainController.
begin_service_stop()` (setting `ops.service_stop_active = True` and clearing
`ato.running`) and, if live authority exists, latches
`pending_path_cancel_request(error=None, kind="info", run_complete=True)`
— the classification that must NOT set
`RouteRun.path_cancelled` (design_new.md "pending_path_cancel_request" /
M-PAYLOAD). Only once the train is stopped, the release request has been
consumed, and `received_path_plan is None` does the step call
`TrainController.consume_service_stop()` and `RouteRun.
advance_walk_position(...)` to finish the task.

This is also the regression guard for the H2/H-API-CHAIN fatal-freeze bug: a
`run` task must never reach `RouteExecutor.handle_path_completion(...)`'s
`task_kind == "stop"` match loop, which would otherwise raise
`SimulationController.enter_failed_runtime(...)` for a healthy run.
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
    controller.ato.running = True
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
    assert controller.ops.service_stop_active is False
    assert controller.ato.running is True  # untouched while still rolling


def test_run_task_timer_elapses_begins_service_stop_without_cancelling_run():
    executor, exec_state, controller, train, route, projection, _stub = (
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
    assert controller.ops.service_stop_active is True
    assert controller.ato.running is False
    assert row.finished_at is None  # not yet — still moving

    # The onward authority release is classified as run-progress, not a plain
    # cancel: RouteExecutor.handle_path_cancellation(...) must read this and
    # NOT set RouteRun.path_cancelled (M-PAYLOAD / display_state != CANCELLED).
    req = controller.route_hooks.phase_latches.path_cancel_request
    assert req is not None
    assert req.run_complete is True
    assert req.error is None, "a run_complete release carries no ErrorRecord"


def test_run_task_completes_only_after_stopped_with_authority_released():
    executor, exec_state, controller, train, route, projection, stub = (
        _run_task_ready(task_time=20.0)
    )
    row = exec_state.task_runs[0]
    controller.physics_sync.current_speed_kmh = 40.0
    executor._run_task_step(
        train, route, exec_state, controller, projection, now=8.0, delta_seconds=8.0
    )
    executor._run_task_step(
        train, route, exec_state, controller, projection, now=23.0, delta_seconds=15.0
    )
    assert controller.ops.service_stop_active is True

    # Still moving: must keep waiting even though the timer has elapsed.
    still_waiting = executor._run_task_step(
        train, route, exec_state, controller, projection, now=24.0, delta_seconds=1.0
    )
    assert still_waiting is True
    assert row.finished_at is None

    # Stops, but the cancel hasn't committed yet (path still live) — must wait.
    controller.physics_sync.current_speed_kmh = 0.0
    still_waiting = executor._run_task_step(
        train, route, exec_state, controller, projection, now=25.0, delta_seconds=1.0
    )
    assert still_waiting is True
    assert row.finished_at is None
    assert controller.ops.service_stop_active is True  # not cleared yet

    # Phase 3 commits the cancel: consumes the latch and releases the path.
    controller.route_hooks.phase_latches.clear("path_cancel_request")
    del stub.paths_by_train[train.train_id]

    finished = executor._run_task_step(
        train, route, exec_state, controller, projection, now=26.0, delta_seconds=1.0
    )

    assert finished is True
    assert controller.ops.service_stop_active is False  # consume_service_stop() ran
    # Finished through `advance_walk_position(...)`, which samples both clocks
    # itself — callers pass no clock value (design_new.md
    # "`RouteRun.advance_walk_position(walk_position)`").
    assert row.finished_at is not None
    assert row.finished_at_operating is not None
    assert row.run_elapsed_seconds == 20.0  # unchanged by the final tick


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
