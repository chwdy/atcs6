"""Regression: a same-route structural ``Route.tasks`` edit must re-allocate a
fresh ``RouteRun`` for each assigned train.

Pins design_new.md "Shared-route edit teardown". The Action (teardown)
``pending_route_deactivate`` consumer's ``remove_train_route_run(...)``
UNASSIGNS the train from the run's ``route_id`` as part of the clear, so a
same-route re-assignment cannot be applied up front -- it would be undone by
that unassign. The fix holds the re-assignment on
``RouteExecutor._pending_reassign`` and the deactivate consumer re-applies it
AFTER the clear (``apply_pending_reassign``).

Pre-fix, the train was left unassigned with no ``RUNNING`` run, so the next
eligible Plan found ``train_id not in Route.assigned_train_ids`` and never
allocated a fresh run -- the route was silently abandoned for that train.

Run with: ``conda run -n general python -m pytest
tests/test_structural_edit_reassign.py -q``
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
from train_route import RouteRunStatus

from services import train_route_service


@pytest.fixture
def state_with_train():
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=threading.Lock(),
    )
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="TRAIN_A", consist=[car], max_speed=80.0)
    network.add_train(train)
    return state, train


def _setup_running_run(state, train, *, route_id="R", stop_block="BLK_1"):
    """Register `route_id` with one stop, assign the train, allocate a RUNNING
    run, and ensure the controller exists for the deactivate consumer."""
    route = train_route_service.create_route(
        state,
        route_id=route_id,
        name="R",
        tasks=[{"task_kind": "stop", "task_location": stop_block}],
    )
    route.assign_train(train.train_id)
    rex = state.sim_controller.route_executor
    rex._ensure_run_for(train, route)
    state.sim_controller.get_or_create_controller(train.train_id)
    return route, rex



def test_name_only_edit_is_not_structural(state_with_train):
    """A non-structural edit (name only) records no held re-assignment and does
    not tear the run down."""
    state, train = state_with_train
    route, rex = _setup_running_run(state, train)
    controller = state.sim_controller.get_controller("TRAIN_A")

    train_route_service.update_route(state, "R", name="Renamed")

    assert rex._pending_reassign == {}
    assert controller.route_hooks.phase_latches.route_deactivate is False
    assert rex.execution_state_for("TRAIN_A") is not None    # run untouched


def test_explicit_reassign_clears_a_stale_held_reassign(state_with_train):
    """An explicit assign/retarget/unassign supersedes a pending same-route
    re-allocation, so a stale held entry must not fire on a later deactivate."""
    from services import train_service

    state, train = state_with_train
    route, rex = _setup_running_run(state, train)

    # Simulate a structural edit that recorded a held re-assignment...
    rex.record_pending_reassign("TRAIN_A", "R")
    assert rex._pending_reassign == {"TRAIN_A": "R"}

    # ...then the operator explicitly unassigns the train. That clears the stash.
    train_service.assign_route(state, "TRAIN_A", route_id=None)
    assert "TRAIN_A" not in rex._pending_reassign
