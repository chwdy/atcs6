"""Route clean-up around assignment and deletion (design_new.md "Route
assignment writers"; "`RouteExecutor.remove_train_route_run(train_id)`", An
emptied one-off route is deleted; "Route deletion waits for live teardown";
`POST /api/paths` in the availability-flag REST surface).

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_one_off_route_cleanup.py -q``
"""

import threading
from datetime import datetime

import pytest

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from train import Train, TrainCar

from services import train_route_service


@pytest.fixture
def state(tmp_path):
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path)),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    network.add_train(Train(train_id="TRAIN_A", consist=[car], max_speed=80.0))
    sim_controller.get_or_create_controller("TRAIN_A")
    return state


def _one_off(state, route_id):
    return train_route_service.create_route(
        state,
        route_id=route_id,
        name=route_id,
        tasks=[{"task_kind": "stop", "task_location": f"BLK_{route_id}"}],
    )


def _rex(state):
    return state.sim_controller.route_executor


def _controller(state):
    return state.sim_controller.get_controller("TRAIN_A")


def _start_run(state, route):
    rex = _rex(state)
    assert rex.request_route_assignment("TRAIN_A", route.route_id).success
    rex._ensure_run_for(state.network.trains["TRAIN_A"], route)
    assert rex.execution_state_for("TRAIN_A") is not None
    return rex


def test_an_aborted_one_off_route_is_deleted(state):
    rex = _start_run(state, _one_off(state, "Q"))

    assert rex.request_route_assignment("TRAIN_A", None).success  # abort
    state.sim_controller._consume_pending_route_abort(
        _controller(state), state.network.trains["TRAIN_A"]
    )

    assert "Q" not in rex.route_registry
    assert rex.get_historical_route("Q") is not None


def test_a_power_off_with_no_run_deletes_the_emptied_one_off_route(state):
    rex = _rex(state)
    _one_off(state, "Q")
    assert rex.request_route_assignment("TRAIN_A", "Q").success
    controller = _controller(state)
    controller.powered = True
    assert controller.commands.power_off_cmd().success

    state.sim_controller._commit_train_act(controller, state.network.trains["TRAIN_A"])

    assert "Q" not in rex.route_registry


def test_a_deactivate_with_no_run_deletes_the_one_off_route_its_dropped_reassignment_kept(state):
    """design_new.md "Action latched-cancel consumption", step 4: an earlier
    abort or power-off consumer kept an emptied one-off route only because a
    held structural re-assignment named it; the deactivate consumer drops that
    re-assignment and so deletes the route."""
    rex = _rex(state)
    _one_off(state, "Q")
    assert rex.request_route_assignment("TRAIN_A", "Q").success
    rex._commit_pending_route_assignment("TRAIN_A")
    rex.record_pending_reassign("TRAIN_A", "Q")

    rex.remove_train_route_run("TRAIN_A")  # the earlier consumer: no run
    assert "Q" in rex.route_registry  # kept for the held re-assignment

    rex.settle_deactivate_without_run("TRAIN_A")

    assert "TRAIN_A" not in rex._pending_reassign
    assert "Q" not in rex.route_registry


def test_assignment_refuses_an_unknown_route(state):
    result = _rex(state).request_route_assignment("TRAIN_A", "NOPE")

    assert result.success is False
    assert "TRAIN_A" not in _rex(state)._pending_route_assignments


def test_assignment_refuses_a_route_being_deleted(state):
    rex = _start_run(state, _one_off(state, "Q"))
    _one_off(state, "R")
    rex.request_route_deletion("R")

    result = rex.request_route_assignment("TRAIN_A", "R")

    assert result.success is False
    assert "TRAIN_A" not in rex._pending_route_assignments
    assert _controller(state).route_hooks.phase_latches.route_abort is False


def test_a_dropped_held_one_off_request_route_is_deleted(state):
    rex = _start_run(state, _one_off(state, "Q"))
    _one_off(state, "Q2")
    _one_off(state, "Q3")
    assert rex.request_route_assignment("TRAIN_A", "Q2").success  # held

    assert rex.request_route_assignment("TRAIN_A", "Q3").success  # replaces it

    assert "Q2" not in rex.route_registry
    assert rex._pending_route_assignments["TRAIN_A"]["requested_route_id"] == "Q3"
    assert "Q" in rex.route_registry  # the running route is untouched


def test_re_requesting_the_held_route_keeps_it(state):
    rex = _start_run(state, _one_off(state, "Q"))
    _one_off(state, "Q2")
    assert rex.request_route_assignment("TRAIN_A", "Q2").success
    assert rex.request_route_assignment("TRAIN_A", "Q2").success

    assert "Q2" in rex.route_registry


def test_deleting_a_route_drops_held_requests_naming_it(state):
    rex = _start_run(state, _one_off(state, "Q"))
    train_route_service.create_route(
        state,
        route_id="R",
        name="R",
        mode="circular",
        tasks=[
            {"task_kind": "stop", "task_location": "BLK_1"},
            {"task_kind": "stop", "task_location": "BLK_2"},
        ],
    )
    assert rex.request_route_assignment(
        "TRAIN_A", "R", start_override={"start_mode": None, "start_task_index": 1}
    ).success

    train_route_service.delete_route(state, "R")

    held = rex._pending_route_assignments["TRAIN_A"]
    assert held == {"requested_route_id": None, "start_override": None}
    state.sim_controller._consume_pending_route_abort(
        _controller(state), state.network.trains["TRAIN_A"]
    )
    assert rex.assigned_route_id_for("TRAIN_A") is None
    assert "R" not in rex.route_registry


def test_a_refused_quick_route_leaves_no_route_behind(state):
    """An unidentified train refuses the assignment; the one-off route the
    Quick Route built is deleted again."""
    state.network.trains["TRAIN_A"].is_unidentified = True
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/paths",
            json={"train_id": "TRAIN_A", "requested_destination_block_id": "BLK_9"},
        )

    assert response.status_code == 400
    assert not any(
        name.startswith("Quick:")
        for name in (r.name for r in _rex(state).route_registry.values())
    )


def test_replacing_an_unstarted_one_off_route_deletes_it(state):
    """A one-off route assigned to a train with no run yet (a Quick Route made
    while paused), then replaced through the no-run branch, is deleted rather
    than left in the registry with no train on it."""
    rex = _rex(state)
    _one_off(state, "Q")
    _one_off(state, "Q2")
    assert rex.request_route_assignment("TRAIN_A", "Q").success   # no run
    assert rex.execution_state_for("TRAIN_A") is None

    assert rex.request_route_assignment("TRAIN_A", "Q2").success  # replaces it

    assert "Q" not in rex.route_registry
    assert rex.get_historical_route("Q") is not None
    assert rex.assigned_route_id_for("TRAIN_A") == "Q2"


def test_unassigning_an_unstarted_one_off_route_deletes_it(state):
    rex = _rex(state)
    _one_off(state, "Q")
    assert rex.request_route_assignment("TRAIN_A", "Q").success

    assert rex.request_route_assignment("TRAIN_A", None).success

    assert "Q" not in rex.route_registry
    assert rex.assigned_route_id_for("TRAIN_A") is None


def test_re_requesting_the_assigned_unstarted_route_keeps_it(state):
    rex = _rex(state)
    _one_off(state, "Q")
    assert rex.request_route_assignment("TRAIN_A", "Q").success

    assert rex.request_route_assignment("TRAIN_A", "Q").success

    assert "Q" in rex.route_registry
    assert rex.assigned_route_id_for("TRAIN_A") == "Q"
