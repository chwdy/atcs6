"""Binding coverage: which layout entities are wired at all.

`_validate` answers whether each binding that exists points at a real chip and
an in-range port. It has nothing to say about the bindings nobody made, so a
config covering three of twelve blocks passes it and goes live — and in real
mode an unbound block reads permanently clear (`OccupancyDriver` returns only
bound blocks, and the detector reads a missing key as no current), so no train
is ever detected entering it. `coverage_report` is how that gap gets named
before it is discovered as a train that stops committing progress.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_coverage.py -v
"""

import threading
from datetime import datetime

import pytest

import hardware_config
from app_factory import create_app
from app_state import AppState
from block import Block
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from switch import Switch
from track import Tile, TRACK_EW


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(hardware_config.atexit, "register", lambda *a, **k: None)
    hardware_config.reset_to_defaults()
    yield
    hardware_config.reset_to_defaults()


def _config(**overrides):
    payload = {
        "enabled": True,
        "develop": True,
        "controller": {"kind": "ch347", "device_port": None},
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
        ],
        "block_track": {"B1": {"chip_id": "pwm1", "forward": 0, "reverse": 1}},
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
        "switch_output": {"SW_A": {"chip_id": "sw1", "port_main": 5, "port_diverge": 6}},
    }
    payload.update(overrides)
    hardware_config.apply_updates(payload, persist=False)
    return hardware_config.get_hardware_config()


# --------------------------------------------------------------- the report


def test_a_half_wired_layout_names_what_is_missing():
    cfg = _config()

    report = hardware_config.coverage_report(cfg, ["B1", "B2", "B3"], ["SW_A", "SW_B"])

    assert report["blocks"]["total"] == 3
    assert report["blocks"]["occupancy"]["bound"] == ["B1"]
    assert report["blocks"]["occupancy"]["unbound"] == ["B2", "B3"]
    assert report["blocks"]["track"]["unbound"] == ["B2", "B3"]
    assert report["switches"]["unbound"] == ["SW_B"]
    assert report["complete"] is False


def test_a_fully_wired_layout_is_complete():
    cfg = _config()

    report = hardware_config.coverage_report(cfg, ["B1"], ["SW_A"])

    assert report["blocks"]["occupancy"]["unbound"] == []
    assert report["blocks"]["track"]["unbound"] == []
    assert report["switches"]["unbound"] == []
    assert report["complete"] is True


def test_either_occupancy_source_counts_as_detected():
    """The driver falls back to whichever source a block has, so a block with
    only a current-sense channel is detected and must not be reported as a gap."""
    cfg = _config(
        chips=[
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
            {"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"},
        ],
        block_track={},
        switch_output={},
        block_occupancy={"B1": {"chip_id": "in1", "port": 2}},
        block_occupancy_current={"B2": {"chip_id": "cur1", "port": 0}},
    )

    report = hardware_config.coverage_report(cfg, ["B1", "B2", "B3"], [])

    assert report["blocks"]["occupancy"]["bound"] == ["B1", "B2"]
    assert report["blocks"]["occupancy"]["unbound"] == ["B3"]


def test_bindings_naming_absent_entities_are_reported_as_stale():
    """A block renamed or deleted after it was wired. The runtime simply never
    addresses it, but it is dead config and it inflates the bound counts."""
    cfg = _config()

    report = hardware_config.coverage_report(cfg, ["B9"], ["SW_Z"])

    assert report["stale"]["block_occupancy"] == ["B1"]
    assert report["stale"]["block_track"] == ["B1"]
    assert report["stale"]["switch_output"] == ["SW_A"]
    assert report["complete"] is False


def test_an_empty_layout_is_complete_rather_than_a_pile_of_gaps():
    cfg = _config(block_track={}, block_occupancy={}, switch_output={})

    report = hardware_config.coverage_report(cfg, [], [])

    assert report["blocks"]["total"] == 0
    assert report["switches"]["total"] == 0
    assert report["complete"] is True


# ------------------------------------------------------------------- route


@pytest.fixture
def client():
    network = RailwayNetwork()
    for x, block_id in enumerate(("B1", "B2")):
        network.add_track(Tile(x=x, y=0, track_bits=TRACK_EW))
        network.blocks[block_id] = Block(block_id=block_id, length_cm=100.0)
    network.switches["SW_A"] = Switch(switch_id="SW_A", x=0, y=0)
    path_dispatcher = PathDispatcher(network)
    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=SimulationController(network, path_dispatcher),
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_the_route_reports_the_loaded_layout(client):
    _config()

    body = client.get("/api/hardware/coverage").get_json()

    assert body["blocks"]["total"] == 2
    assert body["blocks"]["occupancy"]["unbound"] == ["B2"]
    assert body["switches"]["bound"] == ["SW_A"]
    assert body["complete"] is False
