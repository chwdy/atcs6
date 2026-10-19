"""The operator status read model: pending actions and the hold reason.

design_new.md "Pending actions": one entry per Action latch set on the train,
plus one while a route assignment is held, in the order `emergency_request`,
`power_off`, `route_deactivate`, `route_abort`, the held assignment,
`path_cancel_request`, each labelled as the Latch Table says.

design_new.md "Hold reason": for a stopped train, the first row of the table
whose condition holds, as `{code, question, detail}`; None while it moves.
Both are served in `operator_status_by_train_id` and on every command reply.
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
from operator_status import operator_status_for

pytestmark = pytest.mark.scene("demo_main")


@pytest.fixture
def state():
    """The demo scene: its one train is powered, in ATO, engaged, with a route."""
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
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def _train(state):
    train = next(iter(state.network.trains.values()))
    return train, state.sim_controller.get_or_create_controller(train.train_id)


def _status(state):
    train, _controller = _train(state)
    return operator_status_for(
        state.sim_controller,
        train,
        session_state=state.session_state(),
    )


def _hold_code(state, **kwargs):
    hold = _status(state, **kwargs)["hold_reason"]
    return None if hold is None else hold["code"]


# --- pending actions ----------------------------------------------------------


def test_no_latch_means_no_pending_action(state):
    assert _status(state)["pending_actions"] == []


def test_a_power_off_lists_power_off_before_its_path_cancel(state):
    _train_obj, controller = _train(state)
    controller.route_hooks.phase_latches.set_power_off(
        reason="test", has_active_path=True
    )

    pending = _status(state)["pending_actions"]

    assert [p["latch"] for p in pending] == ["power_off", "path_cancel_request"]
    assert pending[0]["label"] == "stopping to power off"
    assert pending[1]["label"] == "stopping to cancel its path"


def test_a_held_route_assignment_is_listed_after_the_abort(state):
    train, controller = _train(state)
    state.sim_controller.update(0.1)  # Plan allocates the RUNNING run
    executor = state.sim_controller.route_executor
    assert executor.execution_state_for(train.train_id) is not None

    executor.request_route_assignment(train.train_id, None)

    pending = _status(state)["pending_actions"]
    labels = [p["label"] for p in pending]
    assert labels.index("stopping to leave its route") < labels.index(
        "leaving its route once stopped"
    )
    held = next(p for p in pending if p["label"] == "leaving its route once stopped")
    assert held["latch"] is None


# --- hold reason --------------------------------------------------------------


def test_a_moving_train_in_a_live_session_has_no_hold_reason(state):
    _train_obj, controller = _train(state)
    controller.physics_sync.current_speed_kmh = 20.0
    assert _status(state)["hold_reason"] is None


def test_a_paused_session_comes_before_the_train(state):
    _train_obj, controller = _train(state)
    controller.eb.activate()
    state.simulation_running = False
    hold = _status(state)["hold_reason"]
    assert hold == {"code": "paused", "question": 1, "detail": None}


def test_a_train_frozen_with_speed_reads_the_session_row(state):
    """A stopped session moves no train, so a train it froze with speed still
    stored reads its question-1 row like a standing one."""
    _train_obj, controller = _train(state)
    controller.physics_sync.current_speed_kmh = 20.0
    state.simulation_running = False
    assert _hold_code(state) == "paused"


def test_setup_holds_every_train(state):
    state.setup_active = True
    assert _hold_code(state) == "setup"


def test_an_inactive_environment_reads_halted_not_activated(state):
    state.sim_controller.environment_active = False
    hold = _status(state)["hold_reason"]
    assert hold == {"code": "halted", "question": 1, "detail": "not_activated"}


def test_a_runtime_failure_reads_halted_runtime_failure(state):
    state.sim_controller.failed_runtime = True
    assert _status(state)["hold_reason"]["detail"] == "runtime_failure"


def test_an_off_train_reads_powered_off(state):
    _train_obj, controller = _train(state)
    controller.complete_power_off()
    assert _hold_code(state) == "powered_off"


def test_eb_and_cut_out_are_told_apart(state):
    _train_obj, controller = _train(state)
    controller.eb.activate()
    assert _hold_code(state) == "emergency_brake"
    controller.eb.release()
    controller.eb.activate_cut_out()
    assert _hold_code(state) == "emergency_cut_out"


def test_an_ato_train_not_started_reads_ato_not_engaged(state):
    _train_obj, controller = _train(state)
    controller.commands.ato_stop()
    assert _hold_code(state) == "ato_not_engaged"


def test_a_pending_action_names_its_label(state):
    _train_obj, controller = _train(state)
    controller.route_hooks.phase_latches.set_route_deactivate(
        reason="test", has_active_path=False
    )
    hold = _status(state)["hold_reason"]
    assert hold["code"] == "pending_action"
    assert hold["question"] == 3
    assert hold["detail"] == "stopping to end its route run"


def test_a_train_with_no_route_reads_no_route(state):
    train, controller = _train(state)
    controller.commands.set_mode("manual")
    state.sim_controller.route_executor.request_route_assignment(train.train_id, None)
    assert _hold_code(state) == "no_route"


def test_an_engaged_train_before_its_first_plan_reads_no_reservation(state):
    _train_obj, controller = _train(state)
    assert controller.path_projection.received_path_plan is None
    assert _hold_code(state) == "no_reservation"


# --- where it is served -------------------------------------------------------


def test_the_network_snapshot_serves_operator_status(state):
    train, _controller = _train(state)
    state.simulation_running = False
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        body = client.get("/api/network").get_json()
    status = body["operator_status_by_train_id"][train.train_id]
    assert status["ato_status"] == "READY"
    assert status["hold_reason"]["code"] == "paused"
    assert body["session_state"] == {"value": "paused", "halt_reason": None}
    assert "ato_status" not in body["controllers_by_train_id"][train.train_id]


def test_a_refused_command_reply_carries_the_hold_reason(state):
    train, controller = _train(state)
    controller.eb.activate()
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(f"/api/trains/{train.train_id}/power-on")
    assert response.status_code == 400
    body = response.get_json()
    assert body["operator_status"]["hold_reason"]["code"] == "emergency_brake"

