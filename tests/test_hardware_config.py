"""Hardware configuration store, factory, REST surface, and bootstrap wiring.

design_new.md "Power -> Hardware configuration": two independent backend choices
(output = virtual|gpio, occupancy = virtual|i2c) plus a device target
(ch347|raspberry_pi), persisted to an app-managed JSON file and applied at the
next session build. These tests cover the config store + validation, disk
persistence (round-trip + fail-safe), the operation-mode derivation, the
interface factory (virtual -> defaults, real -> driver or a clear
HardwareBackendUnavailable), the GET/PUT /api/hardware/config routes, and the
bootstrap fallback to sim mode when a selected real driver is not in the build.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_config.py -v
"""

import json
import sys
import threading
import types
from datetime import datetime

import pytest

import hardware_config
from block_occupancy import SimulatedInputInterface
from block_power import SimulatedOutputInterface
from enums import OperationMode
from switch_power import SimulatedSwitchOutputInterface


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    """Point the store's file at a temp path and reset the singleton around each
    test, so persistence tests never touch the real hardware_config.json."""
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    hardware_config.reset_to_defaults()
    yield
    hardware_config.reset_to_defaults()


# --- defaults / serialization ---------------------------------------------


def test_defaults_are_all_virtual_sim_mode():
    cfg = hardware_config.get_hardware_config()
    assert cfg.output_backend == "virtual"
    assert cfg.occupancy_backend == "virtual"
    assert cfg.device_target is None
    assert cfg.device_port is None
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


def test_as_dict_has_exactly_the_four_fields():
    assert set(hardware_config.as_dict().keys()) == {
        "output_backend",
        "occupancy_backend",
        "device_target",
        "device_port",
    }


# --- operation-mode derivation --------------------------------------------


def test_derive_mode_gpio_output_is_real():
    hardware_config.apply_updates(
        {"output_backend": "gpio", "device_target": "raspberry_pi"}, persist=False
    )
    assert hardware_config.derive_operation_mode() == OperationMode.REAL


def test_derive_mode_i2c_occupancy_only_is_real():
    # Real occupancy sensing while train control stays virtual is a valid config.
    hardware_config.apply_updates(
        {"occupancy_backend": "i2c", "device_target": "ch347"}, persist=False
    )
    cfg = hardware_config.get_hardware_config()
    assert cfg.output_backend == "virtual"
    assert hardware_config.derive_operation_mode() == OperationMode.REAL


# --- validation ------------------------------------------------------------


def test_real_backend_requires_device_target():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.apply_updates({"output_backend": "gpio"}, persist=False)
    # rejected update leaves the singleton untouched
    assert hardware_config.get_hardware_config().output_backend == "virtual"


def test_rejects_unknown_output_backend():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.apply_updates({"output_backend": "spi"}, persist=False)


def test_rejects_unknown_device_target():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.apply_updates(
            {"output_backend": "gpio", "device_target": "arduino"}, persist=False
        )


def test_unknown_keys_are_ignored():
    hardware_config.apply_updates({"nonsense": "x", "output_backend": "virtual"}, persist=False)
    assert not hasattr(hardware_config.get_hardware_config(), "nonsense")


def test_empty_device_port_reads_as_none():
    hardware_config.apply_updates(
        {"output_backend": "gpio", "device_target": "ch347", "device_port": "  "},
        persist=False,
    )
    assert hardware_config.get_hardware_config().device_port is None


# --- disk persistence ------------------------------------------------------


def test_apply_updates_persists_and_round_trips():
    hardware_config.apply_updates(
        {
            "output_backend": "gpio",
            "occupancy_backend": "i2c",
            "device_target": "ch347",
            "device_port": "/dev/ttyUSB0",
        }
    )
    # file was written
    with open(hardware_config.CONFIG_PATH) as fh:
        on_disk = json.load(fh)
    assert on_disk["output_backend"] == "gpio"
    assert on_disk["device_port"] == "/dev/ttyUSB0"

    # a fresh load reproduces it
    hardware_config.reset_to_defaults()
    assert hardware_config.get_hardware_config().output_backend == "virtual"
    hardware_config.load_from_disk()
    assert hardware_config.get_hardware_config().output_backend == "gpio"
    assert hardware_config.get_hardware_config().occupancy_backend == "i2c"
    assert hardware_config.get_hardware_config().device_target == "ch347"


def test_apply_updates_persist_false_does_not_write():
    hardware_config.apply_updates(
        {"output_backend": "gpio", "device_target": "ch347"}, persist=False
    )
    import os

    assert not os.path.exists(hardware_config.CONFIG_PATH)


def test_load_missing_file_keeps_defaults():
    hardware_config.load_from_disk()  # file does not exist
    assert hardware_config.get_hardware_config().output_backend == "virtual"


def test_load_corrupt_file_keeps_virtual_defaults():
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        fh.write("{ not json ]")
    hardware_config.load_from_disk()
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


def test_load_invalid_config_file_keeps_virtual_defaults():
    # A structurally valid file whose content violates the rules (real backend
    # with no device target) must not boot the runtime into a broken real mode.
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        json.dump({"output_backend": "gpio", "device_target": None}, fh)
    hardware_config.load_from_disk()
    assert hardware_config.get_hardware_config().output_backend == "virtual"
    assert hardware_config.derive_operation_mode() == OperationMode.SIM


# --- interface factory -----------------------------------------------------


def test_factory_all_virtual_returns_no_overrides():
    inp, blk, sw = hardware_config.build_session_interfaces()
    assert inp is None and blk is None and sw is None


def test_factory_gpio_without_driver_raises():
    hardware_config.apply_updates(
        {"output_backend": "gpio", "device_target": "raspberry_pi"}, persist=False
    )
    with pytest.raises(hardware_config.HardwareBackendUnavailable):
        hardware_config.build_session_interfaces()


def test_factory_i2c_without_driver_raises():
    hardware_config.apply_updates(
        {"occupancy_backend": "i2c", "device_target": "ch347"}, persist=False
    )
    with pytest.raises(hardware_config.HardwareBackendUnavailable):
        hardware_config.build_session_interfaces()


def test_factory_wires_real_driver_when_package_present(monkeypatch):
    """When the hardware driver package exists, the factory constructs the real
    interfaces from the config (device target + port passed through). Proven with
    a stub package so no physical hardware is needed."""

    class _GpioOut:
        def __init__(self, target, port):
            self.target, self.port = target, port

    class _GpioSwitchOut:
        def __init__(self, target, port):
            self.target, self.port = target, port

    gpio_mod = types.ModuleType("hardware.gpio_output")
    gpio_mod.GpioOutputInterface = _GpioOut
    gpio_mod.GpioSwitchOutputInterface = _GpioSwitchOut
    monkeypatch.setitem(sys.modules, "hardware", types.ModuleType("hardware"))
    monkeypatch.setitem(sys.modules, "hardware.gpio_output", gpio_mod)

    hardware_config.apply_updates(
        {
            "output_backend": "gpio",
            "device_target": "ch347",
            "device_port": "/dev/ttyUSB0",
        },
        persist=False,
    )
    inp, blk, sw = hardware_config.build_session_interfaces()
    assert inp is None  # occupancy stayed virtual
    assert isinstance(blk, _GpioOut) and blk.target == "ch347" and blk.port == "/dev/ttyUSB0"
    assert isinstance(sw, _GpioSwitchOut)


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
    assert cfg == {
        "output_backend": "virtual",
        "occupancy_backend": "virtual",
        "device_target": None,
        "device_port": None,
    }


def test_put_valid_config_succeeds_and_persists(client):
    resp = client.put(
        "/api/hardware/config",
        json={"output_backend": "gpio", "device_target": "raspberry_pi"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["config"]["output_backend"] == "gpio"
    # persisted to the isolated file
    with open(hardware_config.CONFIG_PATH) as fh:
        assert json.load(fh)["device_target"] == "raspberry_pi"


def test_put_invalid_config_returns_400_and_writes_nothing(client):
    import os

    resp = client.put("/api/hardware/config", json={"output_backend": "gpio"})
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
    # nothing committed, nothing written
    assert hardware_config.get_hardware_config().output_backend == "virtual"
    assert not os.path.exists(hardware_config.CONFIG_PATH)


# --- bootstrap wiring ------------------------------------------------------


def test_bootstrap_falls_back_to_sim_when_driver_missing():
    """A persisted real-mode config whose driver is absent must not stop the
    server booting: bootstrap falls back to sim mode with simulated interfaces."""
    import bootstrap

    hardware_config.apply_updates(
        {"output_backend": "gpio", "device_target": "ch347"}
    )  # persists to the isolated file

    network, _dispatcher, sim, _persistence = bootstrap.bootstrap_runtime(
        demo=False, setup_train=False
    )
    assert sim.operation_mode == OperationMode.SIM
    assert isinstance(sim.input_interface, SimulatedInputInterface)
    assert isinstance(sim.power_controller.output_interface, SimulatedOutputInterface)
    assert isinstance(
        sim.switch_power_controller.output_interface, SimulatedSwitchOutputInterface
    )


def test_bootstrap_sim_config_wires_sim_mode():
    import bootstrap

    # default (all-virtual) persisted config
    hardware_config.apply_updates({"output_backend": "virtual"})
    _network, _dispatcher, sim, _persistence = bootstrap.bootstrap_runtime(
        demo=False, setup_train=False
    )
    assert sim.operation_mode == OperationMode.SIM
    assert isinstance(sim.input_interface, SimulatedInputInterface)
