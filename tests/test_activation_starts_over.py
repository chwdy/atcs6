"""Environment activation starts the railway over (design_new.md "Activation
starts the railway over").

A successful `POST /api/operation/activate` keeps the layout, the Hardware
Setup, every train with its last position, and every route with its
assignments. Before the new environment's first tick it cancels every live
path, moves every running route run to history as DEACTIVATED, and turns every
train off. Pending latches are left alone.
"""

import threading
from datetime import datetime

import hardware_config
import operation_environment
import pytest

from app_factory import create_app
from app_state import AppState
from enums import PathStatus
from persistence import PersistenceManager
from train_route import Route, RouteMode, RouteRunStatus, RouteTask
from tests.test_reserved_block_taken_by_another_train import (
    build_abc,
    rolling_train_with_authority_to_c,
)


@pytest.fixture(autouse=True)
def _reset_environment():
    hardware_config.reset_to_defaults()
    operation_environment.reset_to_defaults()
    yield
    hardware_config.reset_to_defaults()
    operation_environment.reset_to_defaults()


def _rolling_train_on_a_route():
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )
    route = Route(
        route_id="R",
        name="R",
        mode=RouteMode.ONE_OFF,
        tasks=[RouteTask(task_kind="stop", task_location="BLK_C")],
    )
    executor = sim.route_executor
    executor.register_route(route)
    route.assign_train("T1")
    executor._ensure_run_for(train, route)
    return network, sim, dispatcher, controller, path_id, route


def test_start_railway_over_drops_paths_runs_and_power_but_keeps_routes():
    network, sim, dispatcher, controller, path_id, route = _rolling_train_on_a_route()
    executor = sim.route_executor
    run = executor.execution_state_for("T1")
    span_before = controller.physics_sync.current_span
    controller.route_hooks.phase_latches.set_route_abort(has_active_path=False)

    sim.start_railway_over()

    # 1. The live path is cancelled and its reservations released.
    assert dispatcher.paths[path_id].status == PathStatus.CANCELLED
    assert dispatcher.active_path_id_by_train("T1") is None
    assert network.blocks["BLK_C"].reserved_by_path is None
    # 2. The running run moved to history as DEACTIVATED; the assignment stays.
    assert executor.execution_state_for("T1") is None
    assert run in executor.history_for_train("T1")
    assert run.current_route_status == RouteRunStatus.DEACTIVATED
    assert route.assigned_train_ids == ["T1"]
    # 3. The train is off, and keeps its last position.
    assert controller.powered is False
    assert controller.physics_sync.current_span is span_before
    # Pending latches are left alone.
    assert controller.route_hooks.phase_latches.route_abort is True


def test_start_railway_over_discards_a_stopped_pass_emergency_request():
    """The one latch activation clears itself is the emergency request
    (design_new.md "Activation starts the railway over")."""
    from runtime_payloads import ErrorRecord

    _network, sim, _dispatcher, controller, _path_id, _route = (
        _rolling_train_on_a_route()
    )
    latches = controller.route_hooks.phase_latches
    latches.set_emergency_request("brake", ErrorRecord("moved by a Setup tool", "locked_entry"))
    latches.set_route_abort(has_active_path=False)

    sim.start_railway_over()

    assert latches.emergency_request is None
    assert latches.route_abort is True


def test_a_successful_activation_starts_the_railway_over(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    network, sim, dispatcher, controller, path_id, route = _rolling_train_on_a_route()
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    app = create_app(state)
    app.config["TESTING"] = True

    with app.test_client() as client:
        response = client.post("/api/operation/activate")

    assert response.status_code == 200, response.get_json()
    assert dispatcher.active_path_id_by_train("T1") is None
    assert sim.route_executor.execution_state_for("T1") is None
    assert route.assigned_train_ids == ["T1"]
    assert controller.powered is False


def test_activation_while_running_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    network, sim, dispatcher, controller, path_id, route = _rolling_train_on_a_route()
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
        simulation_lock=threading.Lock(),
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    app = create_app(state)
    app.config["TESTING"] = True

    with app.test_client() as client:
        response = client.post("/api/operation/activate")

    assert response.status_code == 409
    assert dispatcher.active_path_id_by_train("T1") == path_id
    assert controller.powered is True


def test_a_direct_cancel_reports_no_latch():
    """design_new.md "Latch consumption events": activation's cancel is not the
    consumption of `path_cancel_request`, so its `cancel_path` event carries
    `payload.latch = None`."""
    _network, sim, dispatcher, _controller, path_id, _route = (
        _rolling_train_on_a_route()
    )
    events = []
    original = sim.notification_center.emit
    sim.notification_center.emit = lambda **event: (
        events.append(event), original(**event)
    )[1]

    sim.start_railway_over()

    cancels = [e for e in events if e.get("operation") == "cancel_path"]
    assert cancels and all(e["payload"]["latch"] is None for e in cancels)
