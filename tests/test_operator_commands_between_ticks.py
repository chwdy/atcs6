"""Operator commands run between ticks and return their own result.

design_new.md "Operator-command processing slot": an operator command runs the
moment the API calls it, while the request holds `AppState.simulation_lock`;
it returns `{success, reason}`, the reply carries the train's status payload on
acceptance and refusal alike, the command reports one command-result event, and
a ghosted train refuses every command. "Driver input acceptance": `set_handle(EB)`
is accepted only on a manual train; the operator EB for every train is
`eb.activate()`, served at `/emergency-brake`.
"""

from __future__ import annotations

import contextlib
import io
import threading
from datetime import datetime

import pytest

from app_factory import create_app
from app_state import AppState
from bootstrap import bootstrap_runtime
from services import train_service

pytestmark = pytest.mark.scene("demo_main")


@pytest.fixture
def state():
    """The demo scene: its one train is powered, in ATO, and engaged."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, persistence = bootstrap_runtime(
            demo=True, setup_train=True
        )
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=persistence,
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def _train_id(state) -> str:
    return next(iter(state.network.trains))


def test_a_command_applies_at_once_without_a_tick(state):
    train_id = _train_id(state)
    controller = state.sim_controller.get_or_create_controller(train_id)
    assert controller.commands.engaged is True

    result = train_service.ato_stop(state, train_id)

    assert result.success is True
    assert controller.commands.engaged is False


def test_a_command_reports_one_result_event(state):
    train_id = _train_id(state)
    nc = state.sim_controller.notification_center

    train_service.ato_stop(state, train_id)

    event = nc.latest_by_operation("ato_stop")
    assert event is not None
    assert event.payload["success"] is True


def test_a_refused_command_reports_a_warning_result(state):
    train_id = _train_id(state)
    nc = state.sim_controller.notification_center

    result = train_service.power_on(state, train_id)  # already powered

    assert result.success is False
    event = nc.latest_by_operation("power_on_cmd")
    assert event is not None
    assert event.payload["success"] is False
    assert event.payload["reason"] == result.message


def test_the_command_holds_the_runtime_lock(state):
    """A command waits for the lock the runtime loop holds for a whole tick."""
    train_id = _train_id(state)
    acquired = state.simulation_lock.acquire()
    assert acquired
    results = []
    worker = threading.Thread(
        target=lambda: results.append(train_service.ato_stop(state, train_id))
    )
    worker.start()
    worker.join(timeout=0.2)
    assert worker.is_alive(), "the command ran while the tick held the lock"
    state.simulation_lock.release()
    worker.join(timeout=5)
    assert results and results[0].success is True


def test_set_handle_eb_is_refused_on_an_ato_train(state):
    train_id = _train_id(state)
    controller = state.sim_controller.get_or_create_controller(train_id)
    assert controller.commands.control_mode == "ato"

    result = train_service.set_handle(state, train_id, position="EB")

    assert result.success is False
    assert controller.eb.active is False


def test_the_operator_emergency_brake_works_on_an_ato_train(state):
    train_id = _train_id(state)
    controller = state.sim_controller.get_or_create_controller(train_id)

    result = train_service.emergency_brake(state, train_id)

    assert result.success is True
    assert controller.eb.active is True


def test_a_refused_reply_carries_the_train_status(state):
    app = create_app(state)
    app.config["TESTING"] = True
    train_id = _train_id(state)
    with app.test_client() as client:
        response = client.post(f"/api/trains/{train_id}/power-on")  # already on

    assert response.status_code == 400
    body = response.get_json()
    assert body["success"] is False
    assert body["reason"]
    assert body["error"] == body["reason"]
    assert body["controller"]["train_status"]


def test_the_emergency_brake_route_raises_eb(state):
    app = create_app(state)
    app.config["TESTING"] = True
    train_id = _train_id(state)
    with app.test_client() as client:
        response = client.post(f"/api/trains/{train_id}/emergency-brake")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["controller"]["train_status"] == "eb"
