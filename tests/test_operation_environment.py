import json
import threading
from datetime import datetime

import operation_environment
import pytest
from app_factory import create_app
from app_state import AppState
from enums import OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


@pytest.fixture(autouse=True)
def _reset_environment():
    operation_environment.reset_to_defaults()
    yield
    operation_environment.reset_to_defaults()


def test_environment_store_round_trip_is_independent_of_hardware_setup(
    tmp_path, monkeypatch
):
    path = tmp_path / "operation_environment.json"
    monkeypatch.setattr(operation_environment, "ENVIRONMENT_PATH", str(path))
    operation_environment.reset_to_defaults()

    operation_environment.select(OperationMode.VIRTUAL_HARDWARE)
    assert json.loads(path.read_text()) == {"selected": "virtual_hardware"}

    operation_environment.reset_to_defaults()
    assert operation_environment.load_from_disk() is OperationMode.VIRTUAL_HARDWARE


def test_environment_api_reports_selected_separately_from_active(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
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

    with app.test_client() as client:
        response = client.put(
            "/api/operation/environment", json={"selected": "real_hardware"}
        )
        assert response.status_code == 200
        assert response.get_json() == {
            "status": "success",
            "selected": "real_hardware",
            "active": "simulation",
        }
        assert client.get("/api/operation/environment").get_json() == {
            "selected": "real_hardware",
            "active": "simulation",
        }
