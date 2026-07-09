"""Hardware configuration store, factory, REST surface, and bootstrap wiring.

design_new.md "Power -> Hardware configuration": a chip inventory (controller +
chips) plus three port-binding maps (block_track, block_occupancy,
switch_output), persisted to an app-managed JSON file and applied at the next
session build. These tests cover the config store + validation, disk persistence
(round-trip + fail-safe), the operation-mode derivation, the interface factory
(sim -> no overrides, real -> interfaces built from a fake device or a clear
HardwareBackendUnavailable), the REST routes, and the bootstrap fallback to sim
mode when the real driver cannot be brought up.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_config.py -v
"""

import json
import os
import threading
from datetime import datetime

import pytest

import hardware_config
from block_occupancy import SimulatedInputInterface
from block_power import SimulatedOutputInterface
from enums import OperationMode
from hardware.fake import FakeCh347Device
from switch_power import SimulatedSwitchOutputInterface


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    """Point the store's file at a temp path, reset the singleton, and stub
    atexit so factory tests do not leak process-exit handlers."""
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(hardware_config.atexit, "register", lambda *a, **k: None)
    hardware_config.reset_to_defaults()
    yield
    from hardware import commission
    commission.get_session().close()
    hardware_config.reset_to_defaults()


# --- example configs -------------------------------------------------------
def _real_payload(**overrides):
    payload = {
        "enabled": True,
        "controller": {"kind": "ch347", "device_port": None},
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
        ],
        "block_track": {"B1": {"chip_id": "pwm1", "port": 0}},
        "switch_output": {"SW_A": {"chip_id": "sw1", "port": 5}},
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
    }
    payload.update(overrides)
    return payload


# --- defaults / serialization ---------------------------------------------
def test_defaults_are_all_virtual_sim_mode():
    cfg = hardware_config.get_hardware_config()
    assert cfg.enabled is False
    assert cfg.chips == []
    assert cfg.controller.kind == "ch347"
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


def test_as_dict_shape():
    keys = set(hardware_config.as_dict().keys())
    assert keys == {
        "enabled", "controller", "chips",
        "block_track", "block_occupancy", "switch_output",
    }


def test_as_dict_enriches_chips_with_derived_fields():
    hardware_config.apply_updates(_real_payload(), persist=False)
    chips = {c["chip_id"]: c for c in hardware_config.as_dict()["chips"]}
    assert chips["pwm1"]["role"] == "track_output"
    assert chips["pwm1"]["bus"] == "i2c"
    assert chips["pwm1"]["port_count"] == 8    # 16 channels -> 8 bipolar ports
    assert chips["sw1"]["port_count"] == 24    # 3-chip '595 chain


# --- operation-mode derivation --------------------------------------------
def test_enabled_with_chip_is_real():
    hardware_config.apply_updates(_real_payload(), persist=False)
    assert hardware_config.derive_operation_mode() == OperationMode.REAL


def test_enabled_without_chips_is_sim():
    hardware_config.apply_updates({"enabled": True, "chips": []}, persist=False)
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


def test_chips_but_not_enabled_is_sim():
    hardware_config.apply_updates(_real_payload(enabled=False), persist=False)
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


# --- validation ------------------------------------------------------------
def test_pca9685_requires_i2c_address():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload(
            {"chips": [{"chip_id": "p", "kind": "pca9685"}]}
        )


def test_shift_register_requires_spi_select():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload(
            {"chips": [{"chip_id": "s", "kind": "hc595"}]}
        )


def test_rejects_unknown_chip_kind():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload(
            {"chips": [{"chip_id": "x", "kind": "mcp23017"}]}
        )


def test_rejects_duplicate_chip_id():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({"chips": [
            {"chip_id": "a", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "a", "kind": "pca9685", "i2c_address": "0x41"},
        ]})


def test_rejects_non_cascadable_chain_length():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({"chips": [
            {"chip_id": "p", "kind": "pca9685", "i2c_address": "0x40", "chain_length": 2},
        ]})


def test_binding_unknown_chip_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload(
            {"switch_output": {"SW": {"chip_id": "ghost", "port": 0}}}
        )


def test_binding_port_out_of_range_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
            "switch_output": {"SW": {"chip_id": "s", "port": 8}},  # 0..7 only
        })


def test_binding_role_mismatch_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "p", "kind": "pca9685", "i2c_address": "0x40"}],
            "switch_output": {"SW": {"chip_id": "p", "port": 0}},  # PWM is track, not switch
        })


def test_double_bound_port_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
            "switch_output": {
                "SW_A": {"chip_id": "s", "port": 3},
                "SW_B": {"chip_id": "s", "port": 3},
            },
        })


def test_hex_and_decimal_addresses_coerce():
    a = hardware_config.config_from_payload(
        {"chips": [{"chip_id": "p", "kind": "pca9685", "i2c_address": "0x54"}]}
    )
    assert a.chips[0].i2c_address == 0x54
    b = hardware_config.config_from_payload(
        {"chips": [{"chip_id": "p", "kind": "pca9685", "i2c_address": 64}]}
    )
    assert b.chips[0].i2c_address == 0x40


# --- disk persistence ------------------------------------------------------
def test_apply_updates_persists_and_round_trips():
    hardware_config.apply_updates(_real_payload())
    with open(hardware_config.CONFIG_PATH) as fh:
        on_disk = json.load(fh)
    assert on_disk["enabled"] is True
    assert {c["chip_id"] for c in on_disk["chips"]} == {"pwm1", "sw1", "in1"}
    # a fresh load reproduces it
    hardware_config.reset_to_defaults()
    assert hardware_config.is_real() is False
    hardware_config.load_from_disk()
    assert hardware_config.is_real() is True
    assert len(hardware_config.get_hardware_config().chips) == 3


def test_apply_updates_persist_false_does_not_write():
    hardware_config.apply_updates(_real_payload(), persist=False)
    assert not os.path.exists(hardware_config.CONFIG_PATH)


def test_load_missing_file_keeps_defaults():
    hardware_config.load_from_disk()
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


def test_load_corrupt_file_keeps_defaults():
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        fh.write("{ not json ]")
    hardware_config.load_from_disk()
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


def test_load_invalid_config_keeps_defaults():
    # enabled real-mode file whose binding points at a missing chip must not boot
    # the runtime into a broken real mode.
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        json.dump({"enabled": True, "chips": [],
                   "switch_output": {"SW": {"chip_id": "ghost", "port": 0}}}, fh)
    hardware_config.load_from_disk()
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


def test_legacy_flat_file_loads_as_sim():
    # The previous coarse schema (output_backend/...) has no recognized keys, so
    # it loads as the all-virtual default rather than crashing.
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        json.dump({"output_backend": "gpio", "device_target": "ch347"}, fh)
    hardware_config.load_from_disk()
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


# --- interface factory -----------------------------------------------------
def test_factory_sim_returns_no_overrides():
    inp, blk, sw = hardware_config.build_session_interfaces()
    assert inp is None and blk is None and sw is None


def _patch_fake_device(monkeypatch, present=(0x40,)):
    import hardware.ch347_device as chdev
    fake = FakeCh347Device(present_addresses=set(present))

    def _opener(port=None):
        fake.open()
        return fake

    monkeypatch.setattr(chdev, "open_ch347_device", _opener)
    return fake


def test_factory_real_wires_interfaces_from_fake_device(monkeypatch):
    _patch_fake_device(monkeypatch)
    hardware_config.apply_updates(_real_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    from hardware.interfaces import (
        Ch347OccupancyInputInterface,
        Ch347SwitchOutputInterface,
        Ch347TrackOutputInterface,
    )
    assert isinstance(blk, Ch347TrackOutputInterface)
    assert isinstance(sw, Ch347SwitchOutputInterface)
    assert isinstance(inp, Ch347OccupancyInputInterface)


def test_factory_real_role_without_chip_stays_none(monkeypatch):
    _patch_fake_device(monkeypatch)
    hardware_config.apply_updates({
        "enabled": True,
        "chips": [{"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"}],
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 0}},
    }, persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    assert inp is not None
    assert blk is None and sw is None   # no track / switch chip


def test_factory_device_open_failure_raises_backend_unavailable(monkeypatch):
    import hardware.ch347_device as chdev

    def _boom(port=None):
        raise RuntimeError("no device attached")

    monkeypatch.setattr(chdev, "open_ch347_device", _boom)
    hardware_config.apply_updates(_real_payload(), persist=False)
    with pytest.raises(hardware_config.HardwareBackendUnavailable):
        hardware_config.build_session_interfaces()


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
        yield test_client


def test_get_returns_current_config(client):
    resp = client.get("/api/hardware/config")
    assert resp.status_code == 200
    cfg = resp.get_json()["config"]
    assert cfg["enabled"] is False
    assert cfg["chips"] == []


def test_get_catalog(client):
    resp = client.get("/api/hardware/catalog")
    assert resp.status_code == 200
    body = resp.get_json()
    kinds = {c["kind"] for c in body["chips"]}
    assert kinds == {"pca9685", "hc595", "hc165"}
    assert "cs0" in body["spi_select_lines"] and "gpio0" in body["spi_select_lines"]


def test_put_valid_config_succeeds_and_persists(client):
    resp = client.put("/api/hardware/config", json=_real_payload())
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"
    with open(hardware_config.CONFIG_PATH) as fh:
        assert json.load(fh)["enabled"] is True


def test_put_invalid_config_returns_400_and_writes_nothing(client):
    resp = client.put(
        "/api/hardware/config",
        json={"enabled": True, "chips": [{"chip_id": "p", "kind": "pca9685"}]},
    )
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
    assert hardware_config.is_real() is False
    assert not os.path.exists(hardware_config.CONFIG_PATH)


def test_self_test_endpoint_with_fake_device(client, monkeypatch):
    _patch_fake_device(monkeypatch, present=(0x40,))
    resp = client.post("/api/hardware/self-test", json={"config": _real_payload()})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    chips = {c["chip_id"]: c for c in body["chips"]}
    assert chips["pwm1"]["reachable"] is True     # 0x40 present on the fake
    assert chips["pwm1"]["port_count"] == 8


def test_self_test_endpoint_no_device_is_graceful(client, monkeypatch):
    import hardware.ch347_device as chdev
    monkeypatch.setattr(chdev, "open_ch347_device",
                        lambda port=None: (_ for _ in ()).throw(RuntimeError("busy")))
    resp = client.post("/api/hardware/self-test", json={"config": _real_payload()})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


def test_self_test_endpoint_invalid_config_400(client):
    resp = client.post("/api/hardware/self-test",
                       json={"config": {"chips": [{"chip_id": "p", "kind": "pca9685"}]}})
    assert resp.status_code == 400


def test_port_test_endpoint_read_input(client, monkeypatch):
    fake = _patch_fake_device(monkeypatch)
    fake.set_input_bit("cs1", 2, True, width=1)
    resp = client.post("/api/hardware/port-test", json={
        "config": _real_payload(),
        "chip_id": "in1", "port": 2, "action": "read_input",
    })
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "value": True}


def test_binding_endpoint_set_and_clear(client):
    client.put("/api/hardware/config", json=_real_payload())
    r = client.put("/api/hardware/binding", json={
        "map": "switch_output", "logical_id": "SW_Z", "chip_id": "sw1", "port": 2,
    })
    assert r.status_code == 200
    assert hardware_config.get_hardware_config().switch_output["SW_Z"].port == 2
    r2 = client.put("/api/hardware/binding", json={"map": "switch_output", "logical_id": "SW_Z"})
    assert r2.status_code == 200
    assert "SW_Z" not in hardware_config.get_hardware_config().switch_output


def test_binding_endpoint_rejects_out_of_range(client):
    client.put("/api/hardware/config", json=_real_payload())
    r = client.put("/api/hardware/binding", json={
        "map": "switch_output", "logical_id": "SW_Z", "chip_id": "sw1", "port": 99,
    })
    assert r.status_code == 400


def test_commission_endpoints_with_fake(client, monkeypatch):
    import hardware.ch347_device as chdev
    fake = FakeCh347Device(present_addresses={0x40})
    monkeypatch.setattr(chdev, "open_ch347_device", lambda port=None: fake)

    client.put("/api/hardware/config", json=_real_payload())
    assert client.post("/api/hardware/commission/open",
                       json={"config": _real_payload()}).get_json()["ok"] is True
    client.post("/api/hardware/commission/output",
                json={"chip_id": "sw1", "port": 5, "duty": 1.0})
    assert fake.latched_bit("cs0", 5) is True
    fake.set_input_bit("cs1", 2, True, width=1)
    bits = client.post("/api/hardware/commission/inputs",
                       json={"chip_id": "in1"}).get_json()["ports"]
    assert bits[2]["value"] is True
    assert client.post("/api/hardware/commission/close", json={}).get_json()["ok"] is True
    assert fake.closed is True


# --- bootstrap wiring ------------------------------------------------------
def test_bootstrap_falls_back_to_sim_when_device_unavailable(monkeypatch):
    import bootstrap
    import hardware.ch347_device as chdev
    monkeypatch.setattr(chdev, "open_ch347_device",
                        lambda port=None: (_ for _ in ()).throw(RuntimeError("no device")))
    hardware_config.apply_updates(_real_payload())   # persists a real config
    _network, _dispatcher, sim, _persistence = bootstrap.bootstrap_runtime(
        demo=False, setup_train=False
    )
    assert sim.operation_mode == OperationMode.SIM
    assert isinstance(sim.input_interface, SimulatedInputInterface)
    assert isinstance(sim.power_controller.output_interface, SimulatedOutputInterface)
    assert isinstance(
        sim.switch_power_controller.output_interface, SimulatedSwitchOutputInterface
    )


def test_bootstrap_real_mode_wires_ch347_interfaces(monkeypatch):
    import bootstrap
    from hardware.interfaces import (
        Ch347OccupancyInputInterface,
        Ch347SwitchOutputInterface,
        Ch347TrackOutputInterface,
    )
    _patch_fake_device(monkeypatch)
    hardware_config.apply_updates(_real_payload())
    _network, _dispatcher, sim, _persistence = bootstrap.bootstrap_runtime(
        demo=False, setup_train=False
    )
    assert sim.operation_mode == OperationMode.REAL
    assert isinstance(sim.input_interface, Ch347OccupancyInputInterface)
    assert isinstance(sim.power_controller.output_interface, Ch347TrackOutputInterface)
    assert isinstance(
        sim.switch_power_controller.output_interface, Ch347SwitchOutputInterface
    )


def test_bootstrap_sim_config_wires_sim_mode():
    import bootstrap
    hardware_config.apply_updates({"enabled": False})
    _network, _dispatcher, sim, _persistence = bootstrap.bootstrap_runtime(
        demo=False, setup_train=False
    )
    assert sim.operation_mode == OperationMode.SIM
    assert isinstance(sim.input_interface, SimulatedInputInterface)
