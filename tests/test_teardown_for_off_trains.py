"""Action teardown runs for OFF trains, and one teardown never undoes another
(design_new.md "`SimulationController._act_train(...)` flow", step 6; "Latch
Table", Clear/consume access; "Action latched-cancel consumption", the
`pending_route_deactivate` consumer; "Route assignment writers", A pending
teardown holds the request).

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_teardown_for_off_trains.py -q``
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from train import Train, TrainCar

from services import train_route_service


@pytest.fixture
def state_with_train():
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="TRAIN_A", consist=[car], max_speed=80.0)
    network.add_train(train)
    controller = sim_controller.get_or_create_controller("TRAIN_A")
    return state, train, controller


def _route(state, route_id):
    """A circular route, so an emptied route is never deleted for being a
    finished one-off."""
    return train_route_service.create_route(
        state,
        route_id=route_id,
        name=route_id,
        mode="circular",
        tasks=[
            {"task_kind": "stop", "task_location": f"BLK_{route_id}_1"},
            {"task_kind": "stop", "task_location": f"BLK_{route_id}_2"},
        ],
    )


def _assign(state, train, route):
    """Assign with no run, the way an assignment to a stopped train commits."""
    rex = state.sim_controller.route_executor
    assert rex.request_route_assignment(train.train_id, route.route_id).success
    return rex


def _run_action_teardown(state, train, controller):
    """The Action teardown steps (`5d2c`, `9a81`, `4f6e`, `26f5`)."""
    state.sim_controller._commit_train_act(controller, train)


def test_route_deletion_finishes_for_an_off_train(state_with_train):
    """A deactivate written against an OFF train is consumed on its next
    Action pass: the route is deleted without waiting for power-on."""
    state, train, controller = state_with_train
    route = _route(state, "R")
    rex = _assign(state, train, route)
    controller.powered = False

    train_route_service.delete_route(state, "R")
    assert controller.route_hooks.phase_latches.route_deactivate is True
    assert "R" in rex.route_registry  # still held by the assignment

    _run_action_teardown(state, train, controller)

    assert controller.route_hooks.phase_latches.route_deactivate is False
    assert "R" not in rex.route_registry
    assert rex.assigned_route_id_for("TRAIN_A") is None


def test_complete_power_off_leaves_the_power_off_latch(state_with_train):
    state, train, controller = state_with_train
    controller.powered = True
    assert controller.commands.power_off_cmd().success

    controller.complete_power_off()

    assert controller.powered is False
    assert controller.route_hooks.phase_latches.power_off is True


def test_activation_leaves_a_pending_power_off_to_remove_the_route(state_with_train):
    """Activation switches the train OFF directly but keeps its pending
    power-off, whose Action step then removes the route as usual."""
    state, train, controller = state_with_train
    route = _route(state, "R")
    rex = _assign(state, train, route)
    controller.powered = True
    assert controller.commands.power_off_cmd().success

    state.sim_controller.start_railway_over()

    assert controller.powered is False
    assert controller.route_hooks.phase_latches.power_off is True
    assert rex.assigned_route_id_for("TRAIN_A") == "R"

    _run_action_teardown(state, train, controller)

    assert controller.route_hooks.phase_latches.power_off is False
    assert rex.assigned_route_id_for("TRAIN_A") is None


def test_ghost_rising_edge_on_an_off_train_writes_the_power_off(state_with_train):
    state, train, controller = state_with_train
    route = _route(state, "R")
    rex = _assign(state, train, route)
    controller.powered = False

    state.sim_controller._release_lost_train("TRAIN_A", controller, notify=True)
    assert controller.route_hooks.phase_latches.power_off is True

    _run_action_teardown(state, train, controller)
    assert rex.assigned_route_id_for("TRAIN_A") is None


def test_a_settled_ghost_is_not_rewritten_every_pass(state_with_train):
    state, train, controller = state_with_train
    controller.powered = False

    state.sim_controller._release_lost_train("TRAIN_A", controller, notify=False)

    assert controller.route_hooks.phase_latches.power_off is False


def test_abort_then_deactivate_in_one_pass_keeps_the_replacement(state_with_train):
    """The abort step commits the held replacement; the deactivate step that
    follows finds no run and must not unassign it."""
    state, train, controller = state_with_train
    route_r = _route(state, "R")
    route_r2 = _route(state, "R2")
    rex = _assign(state, train, route_r)
    rex._ensure_run_for(train, route_r)
    assert rex.execution_state_for("TRAIN_A") is not None

    assert rex.request_route_assignment("TRAIN_A", "R2").success  # abort + hold
    train_route_service.delete_route(state, "R")                  # deactivate
    latches = controller.route_hooks.phase_latches
    assert latches.route_abort and latches.route_deactivate

    _run_action_teardown(state, train, controller)

    assert rex.assigned_route_id_for("TRAIN_A") == "R2"
    assert "TRAIN_A" in route_r2.assigned_train_ids
    assert "R" not in rex.route_registry
    assert not latches.route_abort and not latches.route_deactivate


def test_a_pending_teardown_holds_a_request_for_a_train_with_no_run(state_with_train):
    state, train, controller = state_with_train
    route_r = _route(state, "R")
    route_r2 = _route(state, "R2")
    rex = _assign(state, train, route_r)
    controller.powered = True
    assert controller.commands.power_off_cmd().success

    result = rex.request_route_assignment("TRAIN_A", "R2")

    assert result.success is True
    assert "TRAIN_A" in route_r.assigned_train_ids
    assert "TRAIN_A" not in route_r2.assigned_train_ids
    assert rex._pending_route_assignments["TRAIN_A"]["requested_route_id"] == "R2"

    _run_action_teardown(state, train, controller)

    assert rex.assigned_route_id_for("TRAIN_A") == "R2"
    assert controller.powered is False


def test_a_deactivate_with_no_run_commits_the_request_it_held(state_with_train):
    """With only a deactivate pending and no run, the deactivate step commits
    the request the pending teardown held (and starts it not engaged)."""
    state, train, controller = state_with_train
    route_r = _route(state, "R")
    route_r2 = _route(state, "R2")
    rex = _assign(state, train, route_r)
    train_route_service.delete_route(state, "R")
    assert rex.request_route_assignment("TRAIN_A", "R2").success
    assert "TRAIN_A" not in route_r2.assigned_train_ids
    controller.commands.engaged = True

    _run_action_teardown(state, train, controller)

    assert rex.assigned_route_id_for("TRAIN_A") == "R2"
    assert "TRAIN_A" not in rex._pending_route_assignments
    assert "R" not in rex.route_registry
    assert controller.commands.engaged is False


def test_a_deactivate_with_no_run_leaves_an_edited_route_assigned(state_with_train):
    """A structural edit's deactivate on a train with no run unassigns nothing
    and drops the held re-assignment."""
    state, train, controller = state_with_train
    route_r = _route(state, "R")
    rex = _assign(state, train, route_r)
    controller.route_hooks.phase_latches.set_route_deactivate(
        reason="route_tasks_edited:R", has_active_path=False
    )
    rex.record_pending_reassign("TRAIN_A", "R")

    _run_action_teardown(state, train, controller)

    assert rex.assigned_route_id_for("TRAIN_A") == "R"
    assert "TRAIN_A" not in rex._pending_reassign
