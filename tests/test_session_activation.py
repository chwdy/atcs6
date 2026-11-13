"""Activate against the session state.

design_v2.md "Session state" transition table and "Operating environment and
activation": Activate is accepted from Setup or Halted and refused while Live
; a success leaves Setup and goes Live.
"""

from __future__ import annotations

import threading

import pytest

import hardware_config
import operation_environment
from block import Block
from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from persistence import PersistenceManager
from simulation import SimulationController


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        operation_environment, "ENVIRONMENT_PATH", str(tmp_path / "env.json")
    )
    operation_environment.reset_to_defaults()
    # The smallest complete Virtual Hardware setup: one block with its track,
    # digital and current bindings.
    hardware_config.apply_updates({
        "chips": [
            {"chip_id": "pwm", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "out", "kind": "hc595", "spi_select": "cs0"},
            {"chip_id": "in", "kind": "hc165", "spi_select": "cs1"},
            {"chip_id": "cur", "kind": "ina3221", "i2c_address": "0x41"},
        ],
        "block_track": {"B1": {"chip_id": "pwm", "forward": 0, "reverse": 1}},
        "block_occupancy": {"B1": {"chip_id": "in", "port": 0}},
        "block_occupancy_current": {"B1": {"chip_id": "cur", "port": 0}},
    }, persist=False)
    network = RailwayNetwork()
    network.blocks["B1"] = Block(block_id="B1")
    sim = SimulationController(network)
    state = AppState(
        network=network,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
        simulation_lock=threading.Lock(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    return app.test_client(), state


def _value(state):
    return state.session_state().to_dict()


def test_a_fresh_session_is_not_activated_and_activate_goes_live(env):
    client, state = env
    assert _value(state) == {"value": "halted", "halt_reason": "not_activated"}

    response = client.post("/api/operation/activate")

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["session_state"] == {"value": "live", "halt_reason": None}


def test_activate_is_refused_while_live(env):
    client, state = env
    assert client.post("/api/operation/activate").status_code == 200
    assert client.post("/api/operation/activate").status_code == 409



def test_activate_from_setup_leaves_setup(env):
    client, state = env
    state.setup_active = True

    assert client.post("/api/operation/activate").status_code == 200

    assert state.setup_active is False
    assert state.session_state().is_live
