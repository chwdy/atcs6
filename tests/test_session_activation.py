"""Activate, Play/Pause and playback speed against the session state.

design_new.md "Session state" transition table and "Activation starts the
railway over": Activate is refused while Live or Halted for runtime_failure; a
success starts the railway over, leaves Setup, clears the layout cut-out, and
writes `simulation_running = True`. Play/Pause and `set_speed` exist only in
Simulation ("Session mode and operating environment", Pacing).
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

import operation_environment
from app_factory import create_app
from app_state import AppState
from enums import OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operation_environment, "ENVIRONMENT_PATH", str(tmp_path / "env.json")
    )
    operation_environment.reset_to_defaults()
    network = RailwayNetwork()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
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
    return app.test_client(), state


def _value(state):
    return state.session_state().to_dict()


def test_activate_from_paused_goes_live(env):
    client, state = env
    assert _value(state)["value"] == "paused"

    response = client.post("/api/operation/activate")

    assert response.status_code == 200
    assert response.get_json()["session_state"] == {"value": "live", "halt_reason": None}
    assert state.simulation_running is True


def test_activate_is_refused_while_live(env):
    client, state = env
    state.simulation_running = True
    assert client.post("/api/operation/activate").status_code == 409


def test_activate_is_refused_after_a_runtime_failure(env):
    client, state = env
    state.sim_controller.failed_runtime = True
    assert client.post("/api/operation/activate").status_code == 409


def test_activate_clears_setup_and_the_cut_out(env):
    client, state = env
    state.setup_active = True
    state.sim_controller.layout_cut_out_active = True

    assert client.post("/api/operation/activate").status_code == 200

    assert state.setup_active is False
    assert state.sim_controller.layout_cut_out_active is False
    assert state.session_state().is_live


def test_play_and_pause_follow_the_session_state(env):
    client, state = env
    assert client.post("/api/simulation/control", json={"action": "start"}).status_code == 200
    assert client.post("/api/simulation/control", json={"action": "start"}).status_code == 409
    assert client.post("/api/simulation/control", json={"action": "stop"}).status_code == 200
    assert client.post("/api/simulation/control", json={"action": "stop"}).status_code == 409


def test_play_pause_and_speed_are_refused_outside_simulation(env):
    client, state = env
    state.sim_controller.operation_mode = OperationMode.VIRTUAL_HARDWARE
    for body in ({"action": "start"}, {"action": "stop"}, {"action": "set_speed", "speed": 2}):
        assert client.post("/api/simulation/control", json=body).status_code == 409
    assert state.simulation_speed == 1.0


def test_set_speed_in_simulation(env):
    client, state = env
    response = client.post("/api/simulation/control", json={"action": "set_speed", "speed": 3})
    assert response.status_code == 200
    assert state.simulation_speed == 3.0
    assert client.post(
        "/api/simulation/control", json={"action": "set_speed", "speed": 0}
    ).status_code == 400


def test_the_clear_hardware_failure_route_is_gone(env):
    client, _state = env
    assert client.post("/api/runtime/clear-hardware-failure").status_code in (404, 405)
