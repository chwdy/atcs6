"""A layout save file carries the hardware configuration.

design_v2.md "Hardware setup", Saved with the layout: the binding maps are keyed by `block_id` / `switch_id`, so the wiring only means
anything alongside the layout those ids come from. `save_merged_layout` writes
the whole configuration into the file's `hardware` section and
`load_layout_with_fallback` adopts it, writing it to the app-managed config file
so it applies on the next activation.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_layout_hardware_persistence.py -v
"""

import json

import pytest

import hardware_config
import operation_environment
from enums import OperationMode
from network import RailwayNetwork
from persistence import PersistenceManager


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    """Point the hardware store's file at a temp path and reset the singleton, so
    a test never touches the repository's real `hardware_config.json`."""
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    hardware_config.reset_to_defaults()
    yield
    hardware_config.reset_to_defaults()


@pytest.fixture
def manager(tmp_path):
    """A PersistenceManager writing into a temp layouts directory."""
    return PersistenceManager(storage_dir=str(tmp_path / "layouts"))


def _wired_payload():
    """A real-mode configuration whose bindings name layout ids."""
    return {
        "enabled": True,
        "develop": True,
        "controller": {"kind": "ch347", "device_port": None},
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 2},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
        ],
        "block_track": {"B1": {"chip_id": "pwm1", "forward": 0, "reverse": 1}},
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
        "switch_output": {"SW_A": {"chip_id": "sw1", "port_main": 5, "port_diverge": 6}},
    }


def _save(manager, name="wired"):
    return manager.save_merged_layout(name, RailwayNetwork())


# --- save ------------------------------------------------------------------
def test_saved_layout_carries_the_hardware_configuration(manager):
    hardware_config.apply_updates(_wired_payload(), persist=False)
    path = _save(manager)["path"]

    with open(path) as fh:
        wrapper = json.load(fh)

    hw = wrapper["hardware"]
    assert "enabled" not in hw
    assert "develop" not in hw
    assert {c["chip_id"] for c in hw["chips"]} == {"pwm1", "sw1", "in1"}
    assert hw["block_track"]["B1"] == {
        "chip_id": "pwm1", "forward": 0, "reverse": 1, "reversed": False,
    }
    assert hw["switch_output"]["SW_A"]["port_main"] == 5
    # exactly the on-disk form, no derived enrichment
    assert set(hw) == set(hardware_config.persisted_dict())


def test_empty_setup_is_saved_independently_of_environment(manager):
    with open(_save(manager, "virtual")["path"]) as fh:
        hw = json.load(fh)["hardware"]
    assert hw["chips"] == []
    assert "enabled" not in hw and "develop" not in hw


def test_list_layouts_reports_has_hardware(manager):
    _save(manager, "wired")
    entry = next(l for l in manager.list_layouts() if l["filename"] == "wired")
    assert entry["has_hardware"] is True


# --- load ------------------------------------------------------------------
def test_loading_a_layout_adopts_and_persists_its_wiring(manager):
    hardware_config.apply_updates(_wired_payload(), persist=False)
    _save(manager)
    hardware_config.reset_to_defaults()
    assert operation_environment.get_selected() is not OperationMode.REAL_HARDWARE
    operation_environment.select(OperationMode.REAL_HARDWARE, persist=False)

    result = manager.load_layout_with_fallback("wired", RailwayNetwork())

    assert result["success"] is True
    assert result["hardware"] == "applied"
    cfg = hardware_config.get_hardware_config()
    assert operation_environment.get_selected() is OperationMode.REAL_HARDWARE
    assert cfg.block_track["B1"].forward == 0
    assert cfg.switch_output["SW_A"].port_diverge == 6
    # written through to the app-managed file, so it survives the restart that
    # applies it
    with open(hardware_config.CONFIG_PATH) as fh:
        assert {c["chip_id"] for c in json.load(fh)["chips"]} == {"pwm1", "sw1", "in1"}


def test_loading_replaces_rather_than_merges(manager):
    """A layout saved in sim mode clears wiring the running session had, because
    a saved section is a whole configuration, not a partial edit."""
    _save(manager, "virtual")
    hardware_config.apply_updates(_wired_payload(), persist=False)

    manager.load_layout_with_fallback("virtual", RailwayNetwork())

    cfg = hardware_config.get_hardware_config()
    assert cfg.chips == []
    assert cfg.block_track == {}
    assert operation_environment.get_selected() is not OperationMode.REAL_HARDWARE


def test_layout_without_a_hardware_section_leaves_the_configuration_alone(manager):
    """Layouts saved before hardware was carried must not wipe the rig."""
    _save(manager, "legacy")
    path = manager.storage_dir / "legacy.json"
    wrapper = json.loads(path.read_text())
    del wrapper["hardware"]
    path.write_text(json.dumps(wrapper))

    hardware_config.apply_updates(_wired_payload(), persist=False)
    result = manager.load_layout_with_fallback("legacy", RailwayNetwork())

    assert result["hardware"] == "absent"
    assert operation_environment.get_selected() is OperationMode.VIRTUAL_HARDWARE
    assert hardware_config.get_hardware_config().block_track["B1"].reverse == 1


def test_invalid_hardware_section_is_skipped_not_fatal(manager):
    """Fail-safe: the layout still loads, the running configuration is untouched,
    and the reason comes back on the result."""
    _save(manager, "broken")
    path = manager.storage_dir / "broken.json"
    wrapper = json.loads(path.read_text())
    wrapper["hardware"] = {
        "enabled": True,
        "chips": [{"chip_id": "p", "kind": "not_a_chip_kind"}],
    }
    path.write_text(json.dumps(wrapper))

    result = manager.load_layout_with_fallback("broken", RailwayNetwork())

    assert result["success"] is True
    assert result["hardware"] == "error"
    assert result["hardware_error"]
    assert hardware_config.get_hardware_config().chips == []


def test_binding_naming_a_chip_the_layout_lacks_is_dropped(manager):
    """The same rule the setup screen uses: a binding pointing at a chip that is
    gone is dropped, not a fatal error."""
    _save(manager, "dangling")
    path = manager.storage_dir / "dangling.json"
    wrapper = json.loads(path.read_text())
    payload = _wired_payload()
    payload["chips"] = [c for c in payload["chips"] if c["chip_id"] != "sw1"]
    payload["enabled"] = False
    wrapper["hardware"] = payload
    path.write_text(json.dumps(wrapper))

    result = manager.load_layout_with_fallback("dangling", RailwayNetwork())

    assert result["hardware"] == "applied"
    cfg = hardware_config.get_hardware_config()
    assert cfg.switch_output == {}
    assert cfg.block_track["B1"].chip_id == "pwm1"
