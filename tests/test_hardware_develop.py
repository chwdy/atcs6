"""Develop mode: the real-mode stack on the in-memory fake device.

design_new.md "Power -> Hardware configuration": the persisted `develop` flag
makes `build_session_interfaces` wire the same `Ch347*` interfaces onto a
`FakeCh347Device` (no USB hardware, watchdog unarmed). These tests cover the
config round-trip of the flag, the develop factory path (no monkeypatching —
develop must work with no `ch347api` installed), the occupancy pipeline reading
injected fake state, the `/api/debug/hw` endpoints, and the commissioning tools
(self-test / occupancy-test / commission) running on the fake.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_develop.py -v
"""

import json
import threading
from datetime import datetime

import pytest

import hardware_config
import operation_environment
from enums import OperationMode
from hardware.fake import FakeCh347Device


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    """Point the store's file at a temp path, reset the singleton, and stub
    atexit so factory tests do not leak process-exit handlers."""
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    monkeypatch.setattr(hardware_config.atexit, "register", lambda *a, **k: None)
    hardware_config.reset_to_defaults()
    yield
    from hardware import commission
    commission.get_session().close()
    hardware_config.reset_to_defaults()


def _develop_payload(**overrides):
    """A full develop-mode configuration: every chip kind, all binding maps."""
    payload = {
        "enabled": True,
        "develop": True,
        "controller": {"kind": "ch347", "device_port": None},
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
            {"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"},
        ],
        "block_track": {
            "B1": {"chip_id": "pwm1", "forward": 0, "reverse": 1},
        },
        "switch_output": {
            "SW_A": {"chip_id": "sw1", "port_main": 5, "port_diverge": 6},
        },
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
        "block_occupancy_current": {
            "B1": {"chip_id": "cur1", "port": 0, "threshold_ma": 15.0},
        },
    }
    payload.update(overrides)
    return payload


# --- config round-trip ------------------------------------------------------
def test_develop_defaults_false():
    assert operation_environment.get_selected() is OperationMode.SIMULATION
    assert hardware_config.is_develop() is False


def test_virtual_hardware_round_trips_in_its_separate_store():
    hardware_config.apply_updates(_develop_payload())
    assert hardware_config.as_dict()["develop"] is True
    with open(hardware_config.CONFIG_PATH) as fh:
        assert "develop" not in json.load(fh)
    with open(operation_environment.ENVIRONMENT_PATH) as fh:
        assert json.load(fh)["selected"] == "virtual_hardware"
    hardware_config.reset_to_defaults()
    assert operation_environment.get_selected() is OperationMode.SIMULATION
    hardware_config.load_from_disk()
    assert hardware_config.is_develop() is False
    operation_environment.load_from_disk()
    assert hardware_config.is_develop() is True


def test_legacy_develop_flag_selects_virtual_hardware_independently():
    hardware_config.apply_updates({"develop": True}, persist=False)
    assert hardware_config.is_develop() is True
    assert hardware_config.derive_operation_mode() == OperationMode.VIRTUAL_HARDWARE


# --- factory ---------------------------------------------------------------
def test_factory_develop_wires_interfaces_on_fake_without_driver():
    # No monkeypatching of open_ch347_device: develop mode must never touch the
    # real driver, so it works with no ch347api installed.
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    from hardware.interfaces import (
        OccupancyDriver,
        SwitchDriver,
        TrackDriver,
    )
    assert isinstance(blk, TrackDriver)
    assert isinstance(sw, SwitchDriver)
    assert isinstance(inp, OccupancyDriver)
    assert hardware_config.derive_operation_mode() == OperationMode.VIRTUAL_HARDWARE
    # The session device is the opened fake, reachable for the debug endpoints.
    device = hardware_config.get_session_device()
    assert isinstance(device, FakeCh347Device)
    assert device.opened
    # No physical track to protect: the loss-of-control watchdog stays unarmed.
    assert blk._watchdog is None


def test_factory_develop_off_still_needs_real_driver(monkeypatch):
    import hardware.ch347_device as chdev

    def _boom(port=None):
        raise RuntimeError("no device attached")

    monkeypatch.setattr(chdev, "open_ch347_device", _boom)
    hardware_config.apply_updates(_develop_payload(develop=False), persist=False)
    with pytest.raises(hardware_config.HardwareBackendUnavailable):
        hardware_config.build_session_interfaces()
    assert hardware_config.get_session_device() is None


# --- occupancy pipeline on injected fake state ------------------------------
def test_injected_occupancy_bit_reaches_the_pipeline():
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    device = hardware_config.get_session_device()
    assert inp.read_block_currents()["B1"] is False
    device.set_input_bit("cs1", 2, True)   # B1's '165 binding: in1 port 2
    assert inp.read_block_currents()["B1"] is True
    device.set_input_bit("cs1", 2, False)
    assert inp.read_block_currents()["B1"] is False


def test_injected_current_decides_while_driven():
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    device = hardware_config.get_session_device()
    # Drive B1 above the idle deadband: the current channel now decides.
    blk.set_voltage("B1", 8.0)
    device.set_ina3221_current(0x41, 1, 0.025)   # 25 mA > 15 mA threshold
    assert inp.read_block_currents()["B1"] is True
    device.set_ina3221_current(0x41, 1, 0.0)
    assert inp.read_block_currents()["B1"] is False
    # Idle again: the '165 bit decides even with current present.
    blk.set_voltage("B1", 0.0)
    device.set_ina3221_current(0x41, 1, 0.025)
    assert inp.read_block_currents()["B1"] is False


# --- REST surface ----------------------------------------------------------
@pytest.fixture
def client():
    from app_factory import create_app
    from app_state import AppState
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from persistence import PersistenceManager
    from simulation import SimulationController

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
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client, state


def _boot_develop_session(state):
    """Mirror bootstrap: build develop interfaces and wire them into the
    running SimulationController, so the debug endpoints see the live fake."""
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    state.sim_controller.input_interface = inp
    return inp, blk, sw


def test_debug_endpoints_409_when_not_develop(client):
    test_client, _ = client
    assert test_client.get("/api/debug/hw").status_code == 409
    resp = test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "B1", "occupied": True})
    assert resp.status_code == 409
    resp = test_client.post("/api/debug/hw/current",
                            json={"block_id": "B1", "current_ma": 25.0})
    assert resp.status_code == 409


def test_debug_occupancy_poke_and_readback(client):
    test_client, state = client
    inp, blk, sw = _boot_develop_session(state)
    resp = test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "B1", "occupied": True})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    # The runtime pipeline reads the injected bit...
    assert inp.read_block_currents()["B1"] is True
    # ...and the GET read-back reports it, with the verdict.
    data = test_client.get("/api/debug/hw").get_json()
    assert data["develop_active"] is True
    assert data["blocks"]["B1"]["occupancy_bit"] is True
    assert data["blocks"]["B1"]["occupied"] is True


def test_debug_current_poke_and_readback(client):
    test_client, state = client
    inp, blk, sw = _boot_develop_session(state)
    blk.set_voltage("B1", 8.0)   # driven -> current decides
    resp = test_client.post("/api/debug/hw/current",
                            json={"block_id": "B1", "current_ma": 25.0})
    assert resp.status_code == 200
    data = test_client.get("/api/debug/hw").get_json()
    assert data["blocks"]["B1"]["current_ma"] == pytest.approx(25.0, abs=1.0)
    assert data["blocks"]["B1"]["volts"] == pytest.approx(8.0, abs=0.1)
    assert data["blocks"]["B1"]["occupied"] is True


def test_debug_unbound_block_404(client):
    test_client, state = client
    _boot_develop_session(state)
    resp = test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "GHOST", "occupied": True})
    assert resp.status_code == 404
    resp = test_client.post("/api/debug/hw/current",
                            json={"block_id": "GHOST", "current_ma": 25.0})
    assert resp.status_code == 404


def test_debug_missing_fields_400(client):
    test_client, state = client
    _boot_develop_session(state)
    assert test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "B1"}).status_code == 400
    assert test_client.post("/api/debug/hw/current",
                            json={"current_ma": 5.0}).status_code == 400


def test_config_get_reports_session_info(client):
    test_client, state = client
    data = test_client.get("/api/hardware/config").get_json()
    assert data["session"]["operation_environment"] == "simulation"
    assert data["session"]["virtual_hardware_active"] is False
    _boot_develop_session(state)
    data = test_client.get("/api/hardware/config").get_json()
    assert data["session"]["develop_active"] is True


# --- commissioning tools on the fake ---------------------------------------
def test_scan_runs_on_fake_when_develop_set(client):
    test_client, _ = client
    payload = _develop_payload()
    hardware_config.apply_updates(payload, persist=False)
    resp = test_client.post("/api/hardware/scan", json={"config": payload})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["controller"]["reachable"] is True
    # The fake is seeded from the payload's chip list, so every configured I2C
    # address answers the scan.
    assert data["controller"]["i2c_scan"] == ["0x40", "0x41"]


def test_scan_runs_on_fake_before_enable(client):
    # The pre-enable commissioning flow: develop is persisted but enabled is
    # still false (you cannot enable until every chip is proven). The gate is
    # the develop flag alone, so Detect / Rescan still run on the fake.
    test_client, _ = client
    payload = _develop_payload(enabled=False)
    hardware_config.apply_updates(payload, persist=False)
    assert hardware_config.is_develop() is False   # not a running develop session yet
    resp = test_client.post("/api/hardware/scan", json={"config": payload})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["controller"]["reachable"] is True


def test_scan_still_fails_without_develop(client):
    test_client, _ = client
    payload = _develop_payload(develop=False)
    hardware_config.apply_updates(payload, persist=False)
    resp = test_client.post("/api/hardware/scan", json={"config": payload})
    # No fake gate, no physical device: the controller line reports unreachable.
    assert resp.get_json()["controller"]["reachable"] is False


# The scan follows the flag on the config UNDER TEST, not the persisted one, so
# the setup panel's Develop toggle changes which device answers with no Save in
# between. Both directions matter: reading the persisted flag instead made a
# scan sent with develop off still open the fake and report its seeded addresses
# as a genuine "Connected".
def test_scan_follows_payload_develop_off_over_persisted_on(client):
    test_client, _ = client
    hardware_config.apply_updates(_develop_payload(), persist=False)
    assert hardware_config.is_develop() is True
    resp = test_client.post(
        "/api/hardware/scan", json={"config": _develop_payload(develop=False)}
    )
    data = resp.get_json()
    assert data["controller"]["reachable"] is False
    assert data["controller"]["i2c_scan"] == []


def test_scan_follows_payload_develop_on_over_persisted_off(client):
    test_client, _ = client
    hardware_config.apply_updates(_develop_payload(develop=False), persist=False)
    assert hardware_config.is_develop() is False
    resp = test_client.post(
        "/api/hardware/scan", json={"config": _develop_payload(develop=True)}
    )
    data = resp.get_json()
    assert data["controller"]["reachable"] is True
    assert data["controller"]["i2c_scan"] == ["0x40", "0x41"]


def test_occupancy_test_reads_live_session_fake(client):
    test_client, state = client
    _boot_develop_session(state)
    device = hardware_config.get_session_device()
    device.set_input_bit("cs1", 2, True)
    resp = test_client.post(
        "/api/hardware/occupancy-test",
        json={"config": _develop_payload(), "block_id": "B1"},
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert data["digital"]["value"] is True


def test_commission_session_opens_on_fake(client):
    test_client, _ = client
    payload = _develop_payload()
    hardware_config.apply_updates(payload, persist=False)
    assert test_client.post("/api/operation/commissioning/enter").status_code == 200
    resp = test_client.post("/api/hardware/commission/open", json={"config": payload})
    assert resp.get_json()["ok"] is True
    resp = test_client.post("/api/hardware/commission/inputs", json={"chip_id": "in1"})
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["ports"]) == 8


def test_virtual_hardware_fault_api_controls_the_live_fake(client):
    test_client, state = client
    _boot_develop_session(state)

    response = test_client.put(
        "/api/virtual-hardware/fault",
        json={"kind": "i2c_write_error", "enabled": True},
    )

    assert response.status_code == 200
    assert response.get_json()["faults"]["i2c_write_error"] is True
    device = hardware_config.get_session_device()
    with pytest.raises(OSError, match="i2c_write_error"):
        device.i2c_write(0x40, b"\x00")
    state_payload = test_client.get("/api/virtual-hardware").get_json()
    assert state_payload["virtual_hardware_active"] is True
    assert state_payload["faults"]["i2c_write_error"] is True

    cleared = test_client.post("/api/virtual-hardware/faults/clear").get_json()
    assert not any(cleared["faults"].values())
