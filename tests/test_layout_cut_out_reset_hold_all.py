"""Emergency stop, Reset and Hold all on a running scene.

design_new.md "Emergency cut-out", Layout emergency cut-out and Reset, and
"Hold all". Emergency stop cuts every output, stops every train, cancels every
live path (each train keeps its route), cuts every powered train out, and halts
the session; Reset releases every train; Hold all sends ATO stop to every ATO
train, keeping its reservation.
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
from enums import TrainStatus

pytestmark = pytest.mark.scene("demo_main")


@pytest.fixture
def running():
    """The demo scene after one live tick: its train is engaged with a path."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, persistence = bootstrap_runtime(
            demo=True, setup_train=True
        )
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=persistence,
        simulation_lock=threading.Lock(),
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    sim.update(0.1)
    train_id = next(iter(network.trains))
    assert dispatcher.active_path_id_by_train(train_id) is not None
    app = create_app(state)
    app.config["TESTING"] = True
    return app.test_client(), state, train_id


def test_emergency_stop_cancels_paths_and_keeps_routes(running):
    client, state, train_id = running
    sim = state.sim_controller
    route_before = sim.route_executor.assigned_route_id_for(train_id)

    response = client.post("/api/simulation/emergency-cut-out")

    assert response.status_code == 200
    controller = sim.controllers[train_id]
    assert controller.eb.cut_out is True
    assert controller.ops.train_status == TrainStatus.EB
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert state.path_dispatcher.active_path_id_by_train(train_id) is None
    assert sim.route_executor.assigned_route_id_for(train_id) == route_before
    assert state.session_state().to_dict() == {
        "value": "halted",
        "halt_reason": "layout_emergency_cut_out",
    }


def test_emergency_stop_while_halted_changes_nothing(running):
    client, state, train_id = running
    client.post("/api/simulation/emergency-cut-out")
    assert client.post("/api/simulation/emergency-cut-out").status_code == 200
    assert state.sim_controller.layout_cut_out_active is True


def test_reset_releases_every_train_and_restores_the_mode(running):
    client, state, train_id = running
    client.post("/api/simulation/emergency-cut-out")

    response = client.post("/api/simulation/reset")

    assert response.status_code == 200
    controller = state.sim_controller.controllers[train_id]
    assert controller.eb.active is False
    assert controller.ops.train_status == TrainStatus.ATO
    assert controller.commands.engaged is False
    assert state.session_state().is_live


def test_reset_discards_an_emergency_request_latched_while_halted(running):
    """A request the stopped pass latched while Halted (a locomotive pushed by
    hand into the next block) is discarded, not applied on the first Live tick
    (design_new.md "Emergency cut-out", Reset step 2)."""
    from runtime_payloads import ErrorRecord

    client, state, train_id = running
    client.post("/api/simulation/emergency-cut-out")
    latches = state.sim_controller.controllers[train_id].route_hooks.phase_latches
    latches.set_emergency_request("cut_out", ErrorRecord("pushed by hand", "overrun_entry"))

    assert client.post("/api/simulation/reset").status_code == 200

    assert latches.emergency_request is None
    state.sim_controller.update(0.1)
    controller = state.sim_controller.controllers[train_id]
    assert controller.eb.active is False


def test_reset_returns_a_paused_session_to_paused(running):
    client, state, _train_id = running
    state.simulation_running = False
    client.post("/api/simulation/emergency-cut-out")
    client.post("/api/simulation/reset")
    assert state.session_state().to_dict()["value"] == "paused"


def test_reset_is_refused_without_an_emergency_stop(running):
    client, _state, _train_id = running
    assert client.post("/api/simulation/reset").status_code == 409


def test_hold_all_stops_ato_trains_and_keeps_their_reservation(running):
    client, state, train_id = running
    controller = state.sim_controller.controllers[train_id]
    assert controller.commands.engaged is True

    response = client.post("/api/simulation/hold-all")

    assert response.status_code == 200
    assert response.get_json()["stopped_train_ids"] == [train_id]
    assert controller.commands.engaged is False
    assert state.path_dispatcher.active_path_id_by_train(train_id) is not None
    assert state.session_state().is_live


def test_hold_all_leaves_a_manual_train_alone(running):
    client, state, train_id = running
    controller = state.sim_controller.controllers[train_id]
    controller.commands.control_mode = "manual"
    assert client.post("/api/simulation/hold-all").get_json()["stopped_train_ids"] == []


def test_emergency_stop_in_setup_closes_the_tools_and_stays_in_setup(running, monkeypatch):
    client, state, train_id = running
    closed = []
    monkeypatch.setattr(
        "hardware.setup_tools.close_setup_tools", lambda: closed.append(True)
    )
    state.setup_active = True

    response = client.post("/api/simulation/emergency-cut-out")

    assert response.status_code == 200
    assert closed == [True]
    assert state.session_state().to_dict()["value"] == "setup"
    assert state.sim_controller.controllers[train_id].eb.active is False
