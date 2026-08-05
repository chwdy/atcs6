import json
import threading
from datetime import datetime

import operation_environment
import hardware_config
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
    hardware_config.reset_to_defaults()
    operation_environment.reset_to_defaults()
    yield
    hardware_config.reset_to_defaults()
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
            "is_active": True,
        }
        assert client.get("/api/operation/environment").get_json() == {
            "selected": "real_hardware",
            "active": "simulation",
            "is_active": True,
            "activation_issues": [],
            "commissioning": False,
        }


def test_complete_setup_is_ready_in_both_hardware_environments():
    cfg = hardware_config.config_from_payload({
        "chips": [
            {"chip_id": "pwm", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "out", "kind": "hc595", "spi_select": "cs0"},
            {"chip_id": "in", "kind": "hc165", "spi_select": "cs1"},
        ],
        "block_track": {"B1": {"chip_id": "pwm", "forward": 0, "reverse": 1}},
        "block_occupancy": {"B1": {"chip_id": "in", "port": 0}},
    })
    virtual = hardware_config.activation_report(
        cfg, OperationMode.VIRTUAL_HARDWARE, ["B1"], []
    )
    real = hardware_config.activation_report(
        cfg, OperationMode.REAL_HARDWARE, ["B1"], []
    )

    assert virtual["ready"] is True
    assert virtual["warnings"] == []
    # Safe state is a write, so nothing reaches the boards once this process is
    # gone. Real Hardware reports that as advisory and still activates.
    assert real["ready"] is True
    assert real["issues"] == []
    assert [note["code"] for note in real["warnings"]] == [
        "outputs_latch_on_host_death"
    ]


def test_incomplete_virtual_setup_is_not_activated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    network = RailwayNetwork()
    network.blocks["B1"] = type("BlockStub", (), {"current_voltage": 0.0})()
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
    operation_environment.select(OperationMode.VIRTUAL_HARDWARE, persist=False)

    with app.test_client() as client:
        response = client.post("/api/operation/activate")

    assert response.status_code == 409
    assert response.get_json()["status"] == "blocked"
    assert sim.operation_mode is OperationMode.VIRTUAL_HARDWARE
    assert sim.environment_active is False
