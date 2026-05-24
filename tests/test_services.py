"""Service-layer unit tests.

These exercise the service functions directly, without going through
HTTP. They are the architectural win of the refactor: the same code path
the Flask handlers use is now callable from any plain Python context
(CLI tools, journey tests, scripted scenarios) and raises typed
exceptions instead of returning Flask responses.

Run with: ``conda run -n general python -m pytest tests/test_services.py -q``
"""

from datetime import datetime
import threading

import pytest

from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from speed_profile import SpeedProfileRegistry
from train import Train, TrainCar

from services import NotFoundError, ValidationError
from services import schedule_service, train_route_service


@pytest.fixture
def state():
    """Fresh AppState with no HTTP layer attached."""
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    speed_profile_registry = SpeedProfileRegistry()

    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        speed_profile_registry=speed_profile_registry,
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


@pytest.fixture
def state_with_train(state):
    """AppState with one train registered."""
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="TRAIN_A", consist=[car], max_speed=80.0)
    state.network.add_train(train)
    return state


# ---------------------------------------------------- train_route_service ----


class TestTrainRouteService:
    def test_create_route_no_http(self, state):
        """`create_route` registers a Route and returns it. No Flask required."""
        route = train_route_service.create_route(
            state,
            route_id="ROUTE_T1",
            name="Test Route",
            mode="one_off",
        )

        assert route.route_id == "ROUTE_T1"
        assert route.name == "Test Route"
        assert route.mode.value == "one_off"

        registry = state.sim_controller.route_executor.route_registry
        assert "ROUTE_T1" in registry
        assert registry["ROUTE_T1"] is route

    def test_create_route_invalid_mode_raises_validation(self, state):
        """An unknown mode string surfaces as ValidationError."""
        with pytest.raises(ValidationError):
            train_route_service.create_route(
                state, route_id="ROUTE_BAD", mode="not_a_mode"
            )

    def test_update_then_delete_route(self, state_with_train):
        """`update_route` flips name; `delete_route` clears the registry."""
        train_route_service.create_route(
            state_with_train, route_id="ROUTE_T2", name="Initial"
        )
        updated = train_route_service.update_route(
            state_with_train, "ROUTE_T2", name="Renamed"
        )
        assert updated.name == "Renamed"
        assert updated.last_edited_at is not None

        train_route_service.delete_route(state_with_train, "ROUTE_T2")
        registry = state_with_train.sim_controller.route_executor.route_registry
        assert "ROUTE_T2" not in registry

    def test_require_route_raises_not_found(self, state):
        """`require_route` raises NotFoundError; `get_route` returns None."""
        assert train_route_service.get_route(state, "MISSING") is None
        with pytest.raises(NotFoundError):
            train_route_service.require_route(state, "MISSING")


# ----------------------------------------------------- schedule_service ----


class TestScheduleService:
    def test_create_schedule_attaches_to_train(self, state_with_train):
        """`create_schedule` adds to network.schedules and sets train.schedule."""
        schedule = schedule_service.create_schedule(
            state_with_train,
            train_id="TRAIN_A",
            stops=[
                {
                    "station_name": "Stop 1",
                    "station_pos": [0, 0],
                    "arrival_time": "2026-01-01T10:00:00",
                    "departure_time": "2026-01-01T10:05:00",
                }
            ],
            priority=3,
        )

        assert schedule.schedule_id in state_with_train.network.schedules
        assert schedule.priority == 3
        assert len(schedule.stops) == 1

        train = state_with_train.network.trains["TRAIN_A"]
        assert train.schedule is schedule

    def test_create_schedule_unknown_train_raises_not_found(self, state):
        """An unknown train_id surfaces as NotFoundError."""
        with pytest.raises(NotFoundError):
            schedule_service.create_schedule(
                state, train_id="GHOST_TRAIN", stops=[]
            )

    def test_create_schedule_missing_train_id_raises_validation(self, state):
        """A blank train_id is a validation error, not a not-found."""
        with pytest.raises(ValidationError):
            schedule_service.create_schedule(state, train_id="", stops=[])
