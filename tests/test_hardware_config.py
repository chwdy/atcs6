"""Hardware configuration store, factory, REST surface, and bootstrap wiring.

design_new.md "Power -> Hardware configuration": a chip inventory (controller +
chips) plus the port-binding maps (block_track, block_occupancy,
block_occupancy_current, switch_output, output_enable), persisted to an
app-managed JSON file and applied at the next
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
        "block_track", "block_occupancy", "block_occupancy_current",
        "switch_output", "output_enable",
    }


def test_as_dict_enriches_chips_with_derived_fields():
    hardware_config.apply_updates(_real_payload(), persist=False)
    chips = {c["chip_id"]: c for c in hardware_config.as_dict()["chips"]}
    assert chips["pwm1"]["role"] == "track_output"
    assert chips["pwm1"]["bus"] == "i2c"
    assert chips["pwm1"]["port_count"] == 8    # 16 channels -> 8 bipolar ports
    assert chips["pwm1"]["port_kind"] == "bipolar_pair"
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


def test_bus_defaults_to_first_allowed_bus():
    # A chip that omits `bus` takes its kind's first allowed bus (pca -> i2c,
    # hc595 -> spi), and that resolved bus is what gets stored.
    cfg = hardware_config.config_from_payload({"chips": [
        {"chip_id": "p", "kind": "pca9685", "i2c_address": "0x40"},
        {"chip_id": "s", "kind": "hc595", "spi_select": "cs0"},
    ]})
    by_id = {c.chip_id: c for c in cfg.chips}
    assert by_id["p"].bus == "i2c"
    assert by_id["s"].bus == "spi"


def test_chip_rejected_on_unsupported_bus():
    # The PCA9685 is I2C only; putting it on SPI is rejected.
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({"chips": [
            {"chip_id": "p", "kind": "pca9685", "bus": "spi", "spi_select": "cs0"},
        ]})


def test_shift_register_on_gpio_bus_validates_and_persists():
    hardware_config.apply_updates({"chips": [
        {"chip_id": "s", "kind": "hc595", "bus": "gpio", "spi_select": "gpio2", "chain_length": 2},
    ]}, persist=False)
    chip = hardware_config.as_dict()["chips"][0]
    assert chip["bus"] == "gpio" and chip["spi_select"] == "gpio2"
    assert chip["port_count"] == 16 and chip["direction"] == "output"


def test_gpio_chip_rejects_a_spi_cs_latch():
    # cs0/cs1 are SPI chip-select lines; a GPIO chip must latch on a gpio pin.
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({"chips": [
            {"chip_id": "s", "kind": "hc595", "bus": "gpio", "spi_select": "cs0"},
        ]})


def test_spi_chip_rejects_a_gpio_latch():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({"chips": [
            {"chip_id": "s", "kind": "hc595", "bus": "spi", "spi_select": "gpio2"},
        ]})


def test_removing_a_chip_prunes_its_dangling_bindings():
    # The chips-only Hardware Setup screen re-saves just the chip list; removing a
    # chip a block was bound to must drop that binding, not fail validation.
    hardware_config.apply_updates(_real_payload(), persist=False)
    assert "B1" in hardware_config.get_hardware_config().block_track
    hardware_config.apply_updates({"chips": [
        {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
    ]}, persist=False)
    cfg = hardware_config.get_hardware_config()
    assert "B1" not in cfg.block_track       # its track chip 'pwm1' is gone -> pruned
    assert "B1" not in cfg.block_occupancy   # its detector chip 'in1' is gone -> pruned
    assert "SW_A" in cfg.switch_output       # 'sw1' still configured -> kept


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


def test_ina3221_requires_i2c_address():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload(
            {"chips": [{"chip_id": "c", "kind": "ina3221"}]}
        )


def test_ina3221_current_binding_validates_with_three_ports_and_calibration():
    cfg = hardware_config.config_from_payload({
        "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}],
        "block_occupancy_current": {
            "B1": {"chip_id": "cur1", "port": 2, "baseline_ma": 1.5, "threshold_ma": 20},
        },
    })
    binding = cfg.block_occupancy_current["B1"]
    assert binding.port == 2 and binding.baseline_ma == 1.5 and binding.threshold_ma == 20.0
    # port 3 is out of range for a 3-channel INA3221
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}],
            "block_occupancy_current": {"B1": {"chip_id": "cur1", "port": 3}},
        })


def test_current_binding_tuples_resolves_defaults_and_converts_ma():
    # The seam build_session_interfaces relies on: None -> module default, mA -> A.
    m = {
        "B1": hardware_config.CurrentBinding("cur1", 0),                      # defaults
        "B2": hardware_config.CurrentBinding("cur1", 1, baseline_ma=2.0, threshold_ma=20.0),
    }
    out = hardware_config._current_binding_tuples(m)
    assert out["B1"] == ("cur1", 0, 0.0, hardware_config.DEFAULT_CURRENT_THRESHOLD_MA / 1000.0)
    assert out["B2"] == ("cur1", 1, 0.002, 0.020)


def test_occupancy_maps_split_by_port_kind():
    # The digital detector goes in block_occupancy; the current-sense channel goes
    # in block_occupancy_current. Crossing them is rejected by port kind.
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}],
            "block_occupancy": {"B1": {"chip_id": "cur1", "port": 0}},   # INA in digital map
        })
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"}],
            "block_occupancy_current": {"B1": {"chip_id": "in1", "port": 0}},  # '165 in current map
        })


def test_output_enable_binding_validates_track_key_and_switch_bit():
    cfg = hardware_config.config_from_payload({
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0"},
        ],
        "output_enable": {"pwm1": {"chip_id": "sw1", "port": 2}},
    })
    assert cfg.output_enable["pwm1"].port == 2
    # key must be a track chip
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0"}],
            "output_enable": {"sw1": {"chip_id": "sw1", "port": 2}},
        })
    # /OE driver bit must be a switch ('595) output, not the PWM chip
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"}],
            "output_enable": {"pwm1": {"chip_id": "pwm1", "port": 0}},
        })


def test_set_current_binding_sets_and_clears():
    hardware_config.apply_updates(
        {"chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}]},
        persist=False,
    )
    hardware_config.set_current_binding(
        "B1", "cur1", 0, baseline_ma=1.0, threshold_ma=12, persist=False
    )
    b = hardware_config.get_hardware_config().block_occupancy_current["B1"]
    assert (b.port, b.baseline_ma, b.threshold_ma) == (0, 1.0, 12.0)
    hardware_config.set_current_binding("B1", None, persist=False)
    assert "B1" not in hardware_config.get_hardware_config().block_occupancy_current
    # a digital ('165) chip in the current map is rejected, nothing committed
    hardware_config.apply_updates(
        {"chips": [{"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"}]},
        persist=False,
    )
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.set_current_binding("B1", "in1", 0, persist=False)


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


def test_current_and_output_enable_maps_round_trip_on_disk():
    hardware_config.apply_updates(_real_payload(
        chips=[
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
            {"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"},
        ],
        block_occupancy={},
        block_occupancy_current={
            "B1": {"chip_id": "cur1", "port": 0, "baseline_ma": 2.0, "threshold_ma": 18},
        },
        output_enable={"pwm1": {"chip_id": "sw1", "port": 7}},
    ))
    hardware_config.reset_to_defaults()
    hardware_config.load_from_disk()
    cfg = hardware_config.get_hardware_config()
    assert cfg.block_occupancy_current["B1"].threshold_ma == 18.0
    assert cfg.block_occupancy_current["B1"].baseline_ma == 2.0
    assert cfg.output_enable["pwm1"].port == 7


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
    assert kinds == {"pca9685", "hc595", "hc165", "ina3221"}
    assert "cs0" in body["spi_select_lines"] and "gpio0" in body["gpio_lines"]


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


def test_binding_endpoint_sets_current_binding(client):
    client.put("/api/hardware/config", json={
        "enabled": True,
        "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}],
    })
    r = client.put("/api/hardware/binding", json={
        "map": "block_occupancy_current", "logical_id": "B1",
        "chip_id": "cur1", "port": 2, "baseline_ma": 0.5, "threshold_ma": 18,
    })
    assert r.status_code == 200
    b = hardware_config.get_hardware_config().block_occupancy_current["B1"]
    assert b.port == 2 and b.threshold_ma == 18.0 and b.baseline_ma == 0.5
    r2 = client.put("/api/hardware/binding",
                    json={"map": "block_occupancy_current", "logical_id": "B1"})
    assert r2.status_code == 200
    assert "B1" not in hardware_config.get_hardware_config().block_occupancy_current


def test_commission_currents_endpoint_with_fake(client, monkeypatch):
    import hardware.ch347_device as chdev
    fake = FakeCh347Device(present_addresses={0x41})
    monkeypatch.setattr(chdev, "open_ch347_device", lambda port=None: fake)
    fake.set_ina3221_current(0x41, 1, 0.020)

    cfg_payload = {"enabled": True,
                   "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"}]}
    assert client.post("/api/hardware/commission/open",
                       json={"config": cfg_payload}).get_json()["ok"] is True
    resp = client.post("/api/hardware/commission/currents",
                       json={"chip_id": "cur1"}).get_json()
    assert resp["ok"] is True
    assert len(resp["currents"]) == 3
    assert abs(resp["currents"][0]["current_ma"] - 20.0) <= 0.5
    client.post("/api/hardware/commission/close", json={})


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
