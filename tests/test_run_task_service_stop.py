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
    assert row.finished_at == 26.0
    assert row.run_elapsed_seconds == 20.0  # unchanged by the final tick


def test_handle_path_completion_never_fires_for_a_run_task():
    """The H2 regression guard: even if a run task's live authority somehow
    reached its own target (the scenario the terminal exclusion in
    `create_path()` / `extend_path()` exists to prevent), dispatcher
    completion must skip past it rather than raise
    `SimulationController.enter_failed_runtime(...)`.
    """
    executor, exec_state, controller, train, route, _projection, _stub = (
        _run_task_ready(task_time=20.0)
    )
    # Route out a trailing stop after the run task so the walk has something
    # to legitimately match; confirms the skip-and-continue behavior, not
    # just the absence of a crash.
    stop_task = RouteTask(
        task_kind="stop", task_location=BlockTarget(block_id="BLK_A")
    )
    route.tasks.append(stop_task)
    stop_target = ResolvedRouteTarget(
        requested_destination_block_id="BLK_A",
        requested_destination_direction="forward",
    )
    exec_state.task_runs.append(
        RouteTaskRun(task_index=1, resolved_target=stop_target)
    )

    calls = []
    sc_stub = type(
        "StubSimController", (), {"enter_failed_runtime": lambda self, p: calls.append(p)}
    )()
    executor.simulation_controller = sc_stub

    ok = executor.handle_path_completion(
        train_id=train.train_id,
        path_id="P_RUN",
        authority_end_block_id="BLK_A",
        requested_destination_direction="forward",
        event_time=30.0,
    )

    assert calls == []  # enter_failed_runtime must never be called
    assert ok is True
    assert exec_state.task_runs[1].finished_at == 30.0  # matched the stop, not the run
