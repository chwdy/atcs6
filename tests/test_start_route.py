"""Start route starts a stopped train on its assigned route under ATO in one command.

design_new.md "Start route": accepted only when the train is stopped, has no EB
latched, has no calibration run open, has no power-off pending, and has a route
assigned. On acceptance it makes, in order, the power-up write (only when OFF),
the `set_mode("ato")` write, and the `/ato/go` write, and adds no effect of its
own. Issued on an ATO train that is already engaged, it changes nothing.
"""

from __future__ import annotations

import contextlib
import io
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from app_factory import create_app
from app_state import AppState
from bootstrap import bootstrap_runtime
from enums import TrainStatus
from services import train_service
from speed_calibration import CalibrationPropulsionOverride

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
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def _controller(state):
    train_id = next(iter(state.network.trains))
    return train_id, state.sim_controller.get_or_create_controller(train_id)


def _power_off_now(controller):
    """Take the stopped train to OFF the way Action's power-off step does: the
    OFF cleanup, then the step's own clear of the latch."""
    assert controller.commands.power_off_cmd().success
    controller.complete_power_off()
    controller.route_hooks.phase_latches.clear("power_off")
    assert controller.ops.train_status == TrainStatus.OFF


def test_start_route_from_off_powers_up_selects_ato_and_engages(state):
    train_id, controller = _controller(state)
    _power_off_now(controller)
    controller.manual.manual_power_percent = 30

    result = train_service.start_route(state, train_id)

    assert result.success is True, result.message
    assert controller.powered is True
    assert controller.commands.control_mode == "ato"
    assert controller.commands.engaged is True
    assert controller.ops.train_status == TrainStatus.ATO
    # The set_mode("ato") write clears the retained percentage.
    assert controller.manual.manual_power_percent == 0


def test_start_route_from_manual_selects_ato_and_engages(state):
    train_id, controller = _controller(state)
    assert controller.commands.set_mode("manual").success
    assert controller.commands.engaged is False

    result = train_service.start_route(state, train_id)

    assert result.success is True, result.message
    assert controller.commands.control_mode == "ato"
    assert controller.commands.engaged is True


def test_start_route_on_an_engaged_ato_train_changes_nothing(state):
    train_id, controller = _controller(state)
    assert controller.commands.control_mode == "ato"
    assert controller.commands.engaged is True

    result = train_service.start_route(state, train_id)

    assert result.success is True
    assert controller.commands.engaged is True


def test_start_route_is_refused_without_a_route(state):
    train_id, controller = _controller(state)
    assert controller.commands.set_mode("manual").success
    controller.route_executor.request_route_assignment(train_id, None)
    assert controller.route_executor.assigned_route_id_for(train_id) is None

    result = train_service.start_route(state, train_id)

    assert result.success is False
    assert controller.commands.control_mode == "manual"
    assert controller.commands.engaged is False


def test_start_route_is_refused_under_eb(state):
    train_id, controller = _controller(state)
    assert controller.eb.activate().success

    result = train_service.start_route(state, train_id)

    assert result.success is False
    assert controller.commands.engaged is False


def test_start_route_is_refused_with_a_power_off_pending(state):
    train_id, controller = _controller(state)
    assert controller.commands.power_off_cmd().success

    result = train_service.start_route(state, train_id)

    assert result.success is False
    assert controller.commands.control_mode == "manual"
    assert controller.commands.engaged is False


def test_start_route_is_refused_while_moving(state):
    train_id, controller = _controller(state)
    assert controller.commands.set_mode("manual").success
    controller.physics_sync.current_speed_kmh = 10.0

    result = train_service.start_route(state, train_id)

    assert result.success is False
    assert controller.commands.control_mode == "manual"


def test_start_route_is_refused_while_calibrating(state, monkeypatch):
    train_id, controller = _controller(state)
    assert controller.commands.set_mode("manual").success
    override = CalibrationPropulsionOverride(track_voltage=0.0)
    monkeypatch.setattr(
        controller,
        "_speed_calibration",
        SimpleNamespace(
            get_propulsion_override=lambda tid: override
            if tid == train_id
            else None
        ),
    )

    result = train_service.start_route(state, train_id)

    assert result.success is False
    assert controller.commands.control_mode == "manual"


def test_the_start_route_endpoint_returns_the_train_status(state):
    train_id, controller = _controller(state)
    assert controller.commands.set_mode("manual").success
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(f"/api/trains/{train_id}/start-route")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["controller"]["train_status"] == "ato"
