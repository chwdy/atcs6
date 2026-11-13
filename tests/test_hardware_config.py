"""Hardware configuration store, factory, REST surface, and bootstrap wiring.

design_new.md "Power -> Hardware configuration": a chip inventory (controller +
chips) plus the port-binding maps (block_track, block_occupancy,
block_occupancy_current, switch_output), persisted to an
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
import operation_environment
from block_occupancy import SimulatedOccupancyDriver
from block_power import SimulatedTrackDriver
from enums import OperationMode
from hardware.fake import FakeCh347Device
from switch_power import SimulatedSwitchDriver


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    """Point the store's file at a temp path and reset the singleton."""
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
    from hardware import commission
    commission.get_session().close()
    hardware_config.reset_to_defaults()


# --- example configs -------------------------------------------------------
def _real_payload(**overrides):
    payload = {
        "environment": "real_hardware",
        "controller": {"kind": "ch347", "device_port": None},
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
        ],
        "block_track": {
            "B1": {"chip_id": "pwm1", "forward": 0, "reverse": 1},
        },
        "switch_output": {
            "SW_A": {"chip_id": "sw1", "port_main": 5, "port_diverge": 6},
        },
        # A switch's rails carry the feed of the block at its main entrance, so
        # a completely-wired rig names one (design_new.md "Power", Bindings).
        "switch_track_source": {"SW_A": "B1"},
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
    }
    payload.update(overrides)
    return payload


# --- defaults / serialization ---------------------------------------------
def test_defaults_are_an_empty_setup_and_virtual_hardware():
    cfg = hardware_config.get_hardware_config()
    assert cfg.chips == []
    assert cfg.controller.kind == "ch347"
    assert operation_environment.get_selected() == OperationMode.VIRTUAL_HARDWARE


def test_as_dict_shape():
    keys = set(hardware_config.as_dict().keys())
    assert keys == {
        "controller", "chips",
        "block_track", "block_occupancy", "block_occupancy_current",
        "switch_output", "switch_track_source", "idle_deadband_v",
        "current_sense_min_drive_v",
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


# --- idle deadband: the drive threshold the hybrid detector selects on -------
def test_idle_deadband_defaults_and_round_trips():
    assert hardware_config.get_hardware_config().idle_deadband_v == (
        hardware_config.DEFAULT_IDLE_DEADBAND_V
    )
    hardware_config.apply_updates({"idle_deadband_v": 1.5}, persist=False)
    assert hardware_config.get_hardware_config().idle_deadband_v == 1.5
    assert hardware_config.as_dict()["idle_deadband_v"] == 1.5


def test_idle_deadband_null_restores_the_default():
    hardware_config.apply_updates({"idle_deadband_v": 1.5}, persist=False)
    hardware_config.apply_updates({"idle_deadband_v": None}, persist=False)
    assert hardware_config.get_hardware_config().idle_deadband_v == (
        hardware_config.DEFAULT_IDLE_DEADBAND_V
    )


def test_idle_deadband_survives_an_unrelated_update():
    """Absent keys keep their value, so editing chips must not reset it."""
    hardware_config.apply_updates({"idle_deadband_v": 0.8}, persist=False)
    hardware_config.apply_updates(_real_payload(), persist=False)
    assert hardware_config.get_hardware_config().idle_deadband_v == 0.8


def test_idle_deadband_survives_the_layout_snapshot_round_trip():
    """A layout file carries the whole hardware configuration, so the deadband
    must come back with it — the persist path and the whole-config parse path
    have to agree on the key."""
    hardware_config.apply_updates({"idle_deadband_v": 1.25}, persist=False)
    snapshot = hardware_config.persisted_dict()
    hardware_config.reset_to_defaults()
    hardware_config.apply_snapshot(snapshot, persist=False)
    assert hardware_config.get_hardware_config().idle_deadband_v == 1.25


def test_switch_track_source_survives_the_layout_snapshot_round_trip():
    """The switch feed map is a required binding, so losing it on a layout load
    leaves the rig un-activatable with `missing_switch_track_source` — and the
    operator has to re-enter every switch's feed by hand.

    Same shape as the deadband round-trip above: the persist path and the
    whole-config parse path are two separate code paths and both have to carry
    every key.
    """
    hardware_config.apply_updates(_real_payload(), persist=False)
    snapshot = hardware_config.persisted_dict()
    assert snapshot["switch_track_source"] == {"SW_A": "B1"}

    hardware_config.reset_to_defaults()
    hardware_config.apply_snapshot(snapshot, persist=False)

    assert hardware_config.get_hardware_config().switch_track_source == {"SW_A": "B1"}


def test_rejects_negative_idle_deadband():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({"idle_deadband_v": -0.1})


def test_rejects_idle_deadband_that_would_retire_current_sense():
    """At or above the ceiling every block reads idle, so the current-sense
    source would never be selected — refused rather than silently ignored."""
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload(
            {"idle_deadband_v": hardware_config.MAX_IDLE_DEADBAND_V}
        )


def test_rejects_non_numeric_idle_deadband():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({"idle_deadband_v": "loud"})


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
    m["B3"] = hardware_config.CurrentBinding(
        "cur1", 2, threshold_ma=10.0, driven_threshold_ma=25.0
    )
    out = hardware_config._current_binding_tuples(m)
    default_a = hardware_config.DEFAULT_CURRENT_THRESHOLD_MA / 1000.0
    # An unset driven threshold is the channel's own threshold.
    assert out["B1"] == ("cur1", 0, 0.0, default_a, default_a)
    assert out["B2"] == ("cur1", 1, 0.002, 0.020, 0.020)
    assert out["B3"] == ("cur1", 2, 0.0, 0.010, 0.025)


def test_driven_threshold_round_trips_and_rejects_negative():
    cfg = hardware_config.config_from_payload({
        "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}],
        "block_occupancy_current": {
            "B1": {"chip_id": "cur1", "port": 0, "threshold_ma": 10, "driven_threshold_ma": 25},
        },
    })
    assert cfg.block_occupancy_current["B1"].driven_threshold_ma == 25.0
    entry = hardware_config._current_binding_dict(cfg.block_occupancy_current)["B1"]
    assert entry["driven_threshold_ma"] == 25.0
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}],
            "block_occupancy_current": {
                "B1": {"chip_id": "cur1", "port": 0, "driven_threshold_ma": -1},
            },
        })


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


def test_set_current_binding_sets_and_clears():
    hardware_config.apply_updates(
        {"chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x40"}]},
        persist=False,
    )
    hardware_config.set_current_binding(
        "B1", "cur1", 0, baseline_ma=1.0, threshold_ma=12, driven_threshold_ma=25,
        persist=False,
    )
    b = hardware_config.get_hardware_config().block_occupancy_current["B1"]
    assert (b.port, b.baseline_ma, b.threshold_ma, b.driven_threshold_ma) == (0, 1.0, 12.0, 25.0)
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
            {"switch_output": {"SW": {"chip_id": "unidentified train", "port_main": 0, "port_diverge": 1}}}
        )


def test_binding_port_out_of_range_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
            # 0..7 only
            "switch_output": {"SW": {"chip_id": "s", "port_main": 8, "port_diverge": 1}},
        })


def test_binding_role_mismatch_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "p", "kind": "pca9685", "i2c_address": "0x40"}],
            # PWM is track, not switch
            "switch_output": {"SW": {"chip_id": "p", "port_main": 0, "port_diverge": 1}},
        })


def test_double_bound_port_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
            "switch_output": {
                "SW_A": {"chip_id": "s", "port_main": 3, "port_diverge": 4},
                "SW_B": {"chip_id": "s", "port_main": 3, "port_diverge": 5},
            },
        })


def test_switch_coils_sharing_a_port_rejected():
    """Both coils on one bit would drive main and diverge at once."""
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
            "switch_output": {"SW": {"chip_id": "s", "port_main": 2, "port_diverge": 2}},
        })


def test_switch_standby_sharing_a_coil_port_rejected():
    with pytest.raises(hardware_config.HardwareConfigError):
        hardware_config.config_from_payload({
            "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
            "switch_output": {
                "SW": {"chip_id": "s", "port_main": 2, "port_diverge": 3, "port_standby": 2},
            },
        })


def test_switch_standby_bit_may_be_shared_between_switches():
    """One TB6612 drives two point motors behind a single STBY pin, so the two
    switches on it name the same standby bit. Only the coils are exclusive."""
    cfg = hardware_config.config_from_payload({
        "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
        "switch_output": {
            "SW_A": {"chip_id": "s", "port_main": 0, "port_diverge": 1, "port_standby": 2},
            "SW_B": {"chip_id": "s", "port_main": 3, "port_diverge": 4, "port_standby": 2},
        },
    })
    assert cfg.switch_output["SW_A"].port_standby == 2
    assert cfg.switch_output["SW_B"].port_standby == 2


def test_switch_standby_sharing_another_switch_coil_rejected():
    """Standby is held high for the whole pulse, so a bit that is also some other
    switch's coil would energize that coil for the length of every throw. Refused
    whichever entry declares it first."""
    chips = [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}]
    coil_first = {
        "SW_A": {"chip_id": "s", "port_main": 0, "port_diverge": 1},
        "SW_B": {"chip_id": "s", "port_main": 3, "port_diverge": 4, "port_standby": 1},
    }
    standby_first = {
        "SW_B": coil_first["SW_B"],
        "SW_A": coil_first["SW_A"],
    }
    for switch_output in (coil_first, standby_first):
        with pytest.raises(hardware_config.HardwareConfigError, match="coil bit of switch"):
            hardware_config.config_from_payload(
                {"chips": chips, "switch_output": switch_output}
            )


def test_switch_standby_port_round_trips():
    cfg = hardware_config.config_from_payload({
        "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
        "switch_output": {
            "SW": {"chip_id": "s", "port_main": 0, "port_diverge": 1, "port_standby": 7},
        },
    })
    assert cfg.switch_output["SW"].port_standby == 7
    assert cfg.switch_output["SW"].ports == [0, 1, 7]


def test_switch_binding_without_standby_is_none():
    cfg = hardware_config.config_from_payload({
        "chips": [{"chip_id": "s", "kind": "hc595", "spi_select": "cs0"}],
        "switch_output": {"SW": {"chip_id": "s", "port_main": 0, "port_diverge": 1}},
    })
    assert cfg.switch_output["SW"].port_standby is None
    assert cfg.switch_output["SW"].ports == [0, 1]


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
    assert "enabled" not in on_disk and "develop" not in on_disk
    assert {c["chip_id"] for c in on_disk["chips"]} == {"pwm1", "sw1", "in1"}
    # a fresh load reproduces it
    hardware_config.reset_to_defaults()
    assert operation_environment.get_selected() is not OperationMode.REAL_HARDWARE
    hardware_config.load_from_disk()
    assert operation_environment.get_selected() is not OperationMode.REAL_HARDWARE
    assert len(hardware_config.get_hardware_config().chips) == 3


def test_current_map_round_trips_on_disk():
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
    ))
    hardware_config.reset_to_defaults()
    hardware_config.load_from_disk()
    cfg = hardware_config.get_hardware_config()
    assert cfg.block_occupancy_current["B1"].threshold_ma == 18.0
    assert cfg.block_occupancy_current["B1"].baseline_ma == 2.0


def test_apply_updates_persist_false_does_not_write():
    hardware_config.apply_updates(_real_payload(), persist=False)
    assert not os.path.exists(hardware_config.CONFIG_PATH)


def test_load_missing_file_keeps_defaults():
    hardware_config.load_from_disk()
    assert operation_environment.get_selected() == OperationMode.VIRTUAL_HARDWARE


def test_load_corrupt_file_keeps_defaults():
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        fh.write("{ not json ]")
    hardware_config.load_from_disk()
    assert operation_environment.get_selected() == OperationMode.VIRTUAL_HARDWARE


def test_load_invalid_config_keeps_defaults():
    # enabled real-mode file whose binding points at a missing chip must not boot
    # the runtime into a broken real mode.
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        json.dump({"enabled": True, "chips": [],
                   "switch_output": {
                       "SW": {"chip_id": "unidentified train", "port_main": 0, "port_diverge": 1},
                   }}, fh)
    hardware_config.load_from_disk()
    assert operation_environment.get_selected() == OperationMode.VIRTUAL_HARDWARE


def test_legacy_flat_file_loads_as_the_default():
    # The previous coarse schema (output_backend/...) has no recognized keys, so
    # it loads as the all-virtual default rather than crashing.
    with open(hardware_config.CONFIG_PATH, "w") as fh:
        json.dump({"output_backend": "gpio", "device_target": "ch347"}, fh)
    hardware_config.load_from_disk()
    assert operation_environment.get_selected() == OperationMode.VIRTUAL_HARDWARE


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
        OccupancyDriver,
        SwitchDriver,
        TrackDriver,
    )
    assert isinstance(blk, TrackDriver)
    assert isinstance(sw, SwitchDriver)
    assert isinstance(inp, OccupancyDriver)


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
    operation_environment.select(OperationMode.REAL_HARDWARE, persist=False)
    with pytest.raises(hardware_config.HardwareBackendUnavailable):
        hardware_config.build_session_interfaces()


# --- what activation captures ----------------------------------------------
# design_new.md "Hardware setup", A binding is read per write: a session is
# built from chips, so those are captured; the port a chip drives for a logical
# entity is looked up per write. `setup_awaits_activation` is what tells an
# operator which kind of edit they just made.


def test_nothing_awaits_activation_without_a_session(monkeypatch):
    """With no hardware-backed session up, nothing is out of date."""
    hardware_config.apply_updates(_real_payload(), persist=False)
    assert hardware_config.setup_awaits_activation() is False


def test_a_captured_edit_awaits_activation(monkeypatch):
    _patch_fake_device(monkeypatch)
    hardware_config.apply_updates(_real_payload(), persist=False)
    hardware_config.build_session_interfaces()
    assert hardware_config.setup_awaits_activation() is False

    # A chip's own parameters decide what gets constructed, so this one only
    # reaches the rig through a rebuild.
    hardware_config.apply_updates(
        _real_payload(chips=[
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 4},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
        ]),
        persist=False,
    )
    assert hardware_config.setup_awaits_activation() is True


def test_a_per_write_edit_does_not_await_activation(monkeypatch):
    """Re-picking a coil port, flipping a rail-polarity flag, or moving a rail
    feed takes effect on the next write. Reporting them as pending would tell
    the operator to re-activate for a change that has already landed."""
    _patch_fake_device(monkeypatch)
    hardware_config.apply_updates(_real_payload(), persist=False)
    hardware_config.build_session_interfaces()

    hardware_config.set_switch_binding("SW_A", "sw1", 7, 8, persist=False)
    hardware_config.set_track_binding(
        "B1", "pwm1", 0, 1, polarity_reversed=True, persist=False
    )
    hardware_config.set_switch_track_source("SW_A", None, persist=False)

    assert hardware_config.setup_awaits_activation() is False


def test_releasing_the_device_clears_what_was_captured(monkeypatch):
    _patch_fake_device(monkeypatch)
    hardware_config.apply_updates(_real_payload(), persist=False)
    hardware_config.build_session_interfaces()
    hardware_config.apply_updates(_real_payload(chips=[]), persist=False)
    assert hardware_config.setup_awaits_activation() is True

    hardware_config.close_live_device()

    assert hardware_config.setup_awaits_activation() is False


def test_the_live_coil_lookup_answers_from_the_current_setup():
    """`switch_coil_ports` is the switch driver's per-write read."""
    hardware_config.apply_updates(_real_payload(), persist=False)
    assert hardware_config.switch_coil_ports("SW_A") == ("sw1", 5, 6, None)

    hardware_config.set_switch_binding("SW_A", "sw1", 7, 8, port_standby=4, persist=False)
    assert hardware_config.switch_coil_ports("SW_A") == ("sw1", 7, 8, 4)

    hardware_config.set_switch_binding("SW_A", None, persist=False)
    assert hardware_config.switch_coil_ports("SW_A") is None


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
    )
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_get_returns_current_config(client):
    resp = client.get("/api/hardware/config")
    assert resp.status_code == 200
    cfg = resp.get_json()["config"]
    assert cfg["chips"] == []


def test_get_catalog(client):
    resp = client.get("/api/hardware/catalog")
    assert resp.status_code == 200
    body = resp.get_json()
    kinds = {c["kind"] for c in body["chips"]}
    assert kinds == {"pca9685", "hc595", "hc165", "ina3221", "ina219"}
    assert "cs0" in body["spi_select_lines"] and "gpio0" in body["gpio_lines"]


def test_put_valid_config_succeeds_and_persists(client):
    resp = client.put("/api/hardware/config", json=_real_payload())
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"
    with open(hardware_config.CONFIG_PATH) as fh:
        persisted = json.load(fh)
    assert "enabled" not in persisted and "develop" not in persisted


def test_put_invalid_config_returns_400_and_writes_nothing(client):
    resp = client.put(
        "/api/hardware/config",
        json={"enabled": True, "chips": [{"chip_id": "p", "kind": "pca9685"}]},
    )
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
    assert operation_environment.get_selected() is not OperationMode.REAL_HARDWARE
    assert not os.path.exists(hardware_config.CONFIG_PATH)


def test_scan_endpoint_with_fake_device(client, monkeypatch):
    _patch_fake_device(monkeypatch, present=(0x40,))
    resp = client.post("/api/hardware/scan", json={"config": _real_payload()})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["controller"]["reachable"] is True
    assert body["controller"]["i2c_scan"] == ["0x40"]


def test_scan_endpoint_no_device_is_graceful(client, monkeypatch):
    import hardware.ch347_device as chdev
    monkeypatch.setattr(chdev, "open_ch347_device",
                        lambda port=None: (_ for _ in ()).throw(RuntimeError("busy")))
    resp = client.post("/api/hardware/scan", json={"config": _real_payload()})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


def test_scan_endpoint_invalid_config_400(client):
    resp = client.post("/api/hardware/scan",
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
        "map": "switch_output", "logical_id": "SW_Z", "chip_id": "sw1",
        "port_main": 2, "port_diverge": 3, "port_standby": 4,
    })
    assert r.status_code == 200
    bound = hardware_config.get_hardware_config().switch_output["SW_Z"]
    assert (bound.port_main, bound.port_diverge, bound.port_standby) == (2, 3, 4)
    r2 = client.put("/api/hardware/binding", json={"map": "switch_output", "logical_id": "SW_Z"})
    assert r2.status_code == 200
    assert "SW_Z" not in hardware_config.get_hardware_config().switch_output


def test_binding_endpoint_switch_without_standby(client):
    client.put("/api/hardware/config", json=_real_payload())
    r = client.put("/api/hardware/binding", json={
        "map": "switch_output", "logical_id": "SW_Z", "chip_id": "sw1",
        "port_main": 2, "port_diverge": 3,
    })
    assert r.status_code == 200
    assert hardware_config.get_hardware_config().switch_output["SW_Z"].port_standby is None


def test_binding_endpoint_switch_needs_both_coils(client):
    client.put("/api/hardware/config", json=_real_payload())
    r = client.put("/api/hardware/binding", json={
        "map": "switch_output", "logical_id": "SW_Z", "chip_id": "sw1", "port_main": 2,
    })
    assert r.status_code == 400
    assert "SW_Z" not in hardware_config.get_hardware_config().switch_output


def test_binding_endpoint_rejects_out_of_range(client):
    client.put("/api/hardware/config", json=_real_payload())
    r = client.put("/api/hardware/binding", json={
        "map": "switch_output", "logical_id": "SW_Z", "chip_id": "sw1",
        "port_main": 99, "port_diverge": 1,
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


def test_missing_switch_track_source_blocks_activation(client):
    """Every switch must name the block feeding its rails before a hardware
    environment may activate (design_new.md "Power", Bindings). Unlike the /OE
    inhibit this is not optional wiring a rig may lack — a switch with no feed
    is a switch whose rails no block drives."""
    client.put("/api/hardware/config", json=_real_payload(switch_track_source={}))

    report = hardware_config.activation_report(
        hardware_config.get_hardware_config(),
        OperationMode.VIRTUAL_HARDWARE, ["B1"], ["SW_A"],
    )

    assert report["ready"] is False
    assert "missing_switch_track_source" in [i["code"] for i in report["issues"]]
    assert [
        i["ids"] for i in report["issues"] if i["code"] == "missing_switch_track_source"
    ] == [["SW_A"]]


def test_switch_track_source_naming_an_absent_block_blocks_activation(client):
    """The feed must name a block the layout actually has: a rename that left
    the binding behind bonds the switch to nothing."""
    client.put(
        "/api/hardware/config",
        json=_real_payload(switch_track_source={"SW_A": "B_GONE"}),
    )

    report = hardware_config.activation_report(
        hardware_config.get_hardware_config(),
        OperationMode.VIRTUAL_HARDWARE, ["B1"], ["SW_A"],
    )

    assert report["ready"] is False
    assert "unknown_switch_track_source" in [i["code"] for i in report["issues"]]


def test_binding_endpoint_sets_switch_track_source(client):
    """The per-entity switch editor writes the feed through the same endpoint as
    the rest, naming a block rather than a chip port."""
    client.put("/api/hardware/config", json=_real_payload(switch_track_source={}))

    r = client.put("/api/hardware/binding", json={
        "map": "switch_track_source", "logical_id": "SW_A", "block_id": "B1",
    })
    assert r.status_code == 200
    assert hardware_config.get_hardware_config().switch_track_source["SW_A"] == "B1"

    r2 = client.put("/api/hardware/binding",
                    json={"map": "switch_track_source", "logical_id": "SW_A"})
    assert r2.status_code == 200
    assert "SW_A" not in hardware_config.get_hardware_config().switch_track_source


def test_real_hardware_warns_that_outputs_latch_if_the_host_dies(client):
    """Safe state is a write, so a dead process writes nothing and the boards
    hold their last commanded value. Advisory, not a blocker: it is a property
    of every rig without an independent gate, which is every rig here."""
    # Both occupancy sources are mandatory before activation (design_new.md
    # "Power"), so a ready rig binds B1 in the digital AND the current map; the
    # config is only stored for the readiness check, not device-initialized, so
    # the extra ina3221 chip needs no fake backend.
    client.put(
        "/api/hardware/config",
        json=_real_payload(
            chips=[
                {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
                {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0",
                 "chain_length": 3},
                {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
                {"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"},
            ],
            block_occupancy_current={"B1": {"chip_id": "cur1", "port": 0}},
        ),
    )
    cfg = hardware_config.get_hardware_config()

    virtual = hardware_config.activation_report(
        cfg, OperationMode.VIRTUAL_HARDWARE, ["B1"], ["SW_A"]
    )
    assert virtual["ready"] is True
    assert virtual["issues"] == []
    assert virtual["warnings"] == []      # no real track to leave energized

    real = hardware_config.activation_report(
        cfg, OperationMode.REAL_HARDWARE, ["B1"], ["SW_A"]
    )
    assert [w["code"] for w in real["warnings"]] == ["outputs_latch_on_host_death"]
    assert real["issues"] == []


def test_commission_currents_endpoint_with_fake(client, monkeypatch):
    import hardware.ch347_device as chdev
    fake = FakeCh347Device(present_addresses={0x41})
    monkeypatch.setattr(chdev, "open_ch347_device", lambda port=None: fake)
    fake.set_ina3221_current(0x41, 1, 0.020)

    cfg_payload = {"enabled": True,
                   "chips": [{"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"}]}
    operation_environment.select(OperationMode.REAL_HARDWARE, persist=False)
    assert client.post("/api/operation/setup").status_code == 200
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
    assert client.post("/api/operation/setup").status_code == 200
    assert client.post("/api/hardware/commission/open",
                       json={"config": _real_payload()}).get_json()["ok"] is True
    # Port 5 is SW_A's main coil: holding it on would burn the point motor, so
    # the session refuses and the bit stays low. (The UI sends coil ports to
    # /commission/pulse; this guards everything else.)
    held = client.post("/api/hardware/commission/output",
                       json={"chip_id": "sw1", "port": 5, "duty": 1.0}).get_json()
    assert held["ok"] is False and "coil" in held["error"]
    assert fake.latched_bit("cs0", 5) is False
    # An unbound bit on the same chain still holds — the refusal is per-port.
    client.post("/api/hardware/commission/output",
                json={"chip_id": "sw1", "port": 9, "duty": 1.0})
    assert fake.latched_bit("cs0", 9) is True
    fake.set_input_bit("cs1", 2, True, width=1)
    bits = client.post("/api/hardware/commission/inputs",
                       json={"chip_id": "in1"}).get_json()["ports"]
    assert bits[2]["value"] is True
    assert client.post("/api/hardware/commission/close", json={}).get_json()["ok"] is True
    assert fake.closed is True


def test_commission_open_clears_the_shift_out_chain(client, monkeypatch):
    """A reclaimed session inherits whatever bits an abandoned client left
    latched, and this screen gets poked at with coils wired up."""
    import hardware.ch347_device as chdev
    fake = FakeCh347Device(present_addresses={0x40})
    monkeypatch.setattr(chdev, "open_ch347_device", lambda port=None: fake)
    fake.latch_out(bytes([0xFF, 0xFF, 0xFF]), "cs0")     # as if left energized
    assert fake.latched_bit("cs0", 5) is True
    operation_environment.select(OperationMode.REAL_HARDWARE, persist=False)
    assert client.post("/api/operation/setup").status_code == 200

    assert client.post("/api/hardware/commission/open",
                       json={"config": _real_payload()}).get_json()["ok"] is True
    for bit in (5, 6):                                   # SW_A's two coil bits
        assert fake.latched_bit("cs0", bit) is False
    client.post("/api/hardware/commission/close", json={})


# --- bootstrap wiring ------------------------------------------------------
def test_bootstrap_keeps_real_environment_inactive_when_device_unavailable(monkeypatch):
    import bootstrap
    import hardware.ch347_device as chdev
    monkeypatch.setattr(chdev, "open_ch347_device",
                        lambda port=None: (_ for _ in ()).throw(RuntimeError("no device")))
    hardware_config.apply_updates(_real_payload(
        block_track={}, switch_output={}, switch_track_source={}, block_occupancy={}
    ))
    operation_environment.select(OperationMode.REAL_HARDWARE)
    _network, _dispatcher, sim, _persistence = bootstrap.bootstrap_runtime(
        demo=False, setup_train=False
    )
    assert sim.operation_mode == OperationMode.REAL_HARDWARE
    assert sim.environment_active is False
    assert isinstance(sim.input_interface, SimulatedOccupancyDriver)
    assert isinstance(sim.power_controller.output_interface, SimulatedTrackDriver)
    assert isinstance(
        sim.switch_output_interface, SimulatedSwitchDriver
    )


def test_bootstrap_activates_no_hardware_environment(monkeypatch):
    """A Real Hardware selection is not activated at boot: the session boots
    Halted for `not_activated` until the operator activates it, and boot runs no
    readiness check of its own (design_new.md "Activation starts the railway
    over", Boot activates no hardware environment; design_v2.md "Session state",
    Boot)."""
    import bootstrap
    _patch_fake_device(monkeypatch)
    hardware_config.apply_updates(_real_payload(
        block_track={}, switch_output={}, switch_track_source={}, block_occupancy={}
    ))
    operation_environment.select(OperationMode.REAL_HARDWARE)
    _network, _dispatcher, sim, _persistence = bootstrap.bootstrap_runtime(
        demo=False, setup_train=False
    )
    assert sim.operation_mode == OperationMode.REAL_HARDWARE
    assert sim.environment_active is False
    assert isinstance(sim.input_interface, SimulatedOccupancyDriver)
    assert [issue["code"] for issue in sim.activation_issues] == ["not_activated"]


