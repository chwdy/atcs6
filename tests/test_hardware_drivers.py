"""Real hardware driver package — chips, interface adapters, and self-test, all
exercised against the in-memory `FakeCh347Device` so no USB hardware is needed
(design_new.md "Power", Hardware interface boundary).

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_drivers.py -v
"""

import pytest

from hardware_config import ChipConfig, ControllerConfig, HardwareConfig
from hardware import self_test
from hardware.catalog import CHIP_CATALOG, catalog_payload, ports_for_chip
from hardware.chips.hc165 import HC165Chain
from hardware.chips.hc595 import HC595Chain
from hardware.chips.ina3221 import INA3221
from hardware.chips.pca9685 import PCA9685
from hardware.fake import FakeCh347Device
from hardware.interfaces import (
    Ch347OccupancyInputInterface,
    Ch347SwitchOutputInterface,
    Ch347TrackOutputInterface,
    build_real_interfaces,
)


# --- catalog ---------------------------------------------------------------
def test_catalog_roles_and_ports():
    assert CHIP_CATALOG["pca9685"].role == "track_output"
    assert CHIP_CATALOG["hc595"].role == "switch_output"
    assert CHIP_CATALOG["hc165"].role == "occupancy_input"
    assert CHIP_CATALOG["ina3221"].role == "occupancy_input"
    assert ports_for_chip("pca9685", 1) == 8      # bipolar pairs
    assert ports_for_chip("hc595", 3) == 24       # cascade
    assert ports_for_chip("pca9685", 5) == 8      # PWM not cascadable -> ignores chain
    assert ports_for_chip("ina3221", 1) == 3      # 3 current-sense channels
    assert ports_for_chip("ina3221", 4) == 3      # not cascadable -> ignores chain


def test_catalog_payload_serializable():
    payload = catalog_payload()
    assert {c["kind"] for c in payload["chips"]} == {"pca9685", "hc595", "hc165", "ina3221"}
    ina = next(c for c in payload["chips"] if c["kind"] == "ina3221")
    assert ina["role"] == "occupancy_input" and ina["buses"] == ["i2c"]
    assert ina["port_kind"] == "current_sense" and ina["direction"] == "input"
    hc595 = next(c for c in payload["chips"] if c["kind"] == "hc595")
    assert hc595["buses"] == ["spi", "gpio"] and hc595["direction"] == "output"
    assert payload["spi_select_lines"] == ["cs0", "cs1"]
    assert payload["gpio_lines"][0] == "gpio0"


# --- chips against the fake ------------------------------------------------
def test_pca9685_duty_and_probe():
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    chip.configure()
    assert chip.probe() is True
    chip.set_duty_cycle(3, 0.5)
    assert abs(dev.duty_cycle(0x40, 3) - 0.5) < 0.01
    # absent address -> probe fails
    assert PCA9685(FakeCh347Device(), address=0x41).probe() is False


def test_hc595_bit_packing_across_cascade():
    dev = FakeCh347Device()
    chip = HC595Chain(dev, select="cs0", chain_length=3)
    assert chip.num_outputs == 24
    chip.set_output(0, True)
    chip.set_output(23, True)
    assert dev.latched_bit("cs0", 0) is True
    assert dev.latched_bit("cs0", 23) is True
    assert dev.latched_bit("cs0", 5) is False
    chip.all_off()
    assert dev.latched_bit("cs0", 0) is False


def test_hc165_reads_bits():
    dev = FakeCh347Device()
    dev.set_input_bit("cs1", 7, True, width=1)
    chip = HC165Chain(dev, select="cs1", chain_length=1)
    assert chip.num_inputs == 8
    assert chip.read_bit(7) is True
    assert chip.read_bit(0) is False


def test_ina3221_current_sign_and_probe():
    dev = FakeCh347Device(present_addresses={0x40})
    chip = INA3221(dev, address=0x40, shunt_ohms=0.1)
    chip.configure()
    assert chip.probe() is True
    # Tolerance is one shunt LSB: 40 µV / 0.1 Ω = 0.4 mA per count, so a read is
    # exact only to ±0.4 mA (−15 mA lands at 37.5 counts -> 15.2 mA).
    lsb_a = INA3221.SHUNT_LSB_V / 0.1
    dev.set_ina3221_current(0x40, 1, 0.020)      # 20 mA forward
    assert abs(chip.read_current(1) - 0.020) <= lsb_a
    dev.set_ina3221_current(0x40, 2, -0.015)     # 15 mA reverse (regeneration)
    assert abs(chip.read_current(2) + 0.015) <= lsb_a
    assert abs(chip.read_current(3)) <= lsb_a     # empty channel reads ~0
    # absent address -> probe fails, matching the PCA9685 convention
    assert INA3221(FakeCh347Device(), address=0x41).probe() is False


def test_ina3221_configure_enables_all_channels():
    dev = FakeCh347Device(present_addresses={0x40})
    INA3221(dev, address=0x40).configure()
    config = dev.registers[0x40][0x00]
    assert config & 0x4000 and config & 0x2000 and config & 0x1000   # CH1/2/3 enable
    assert config & 0x0007 == 0x0007                                 # continuous shunt+bus


def test_ina3221_bus_voltage_and_manufacturer_id():
    dev = FakeCh347Device(present_addresses={0x40})
    chip = INA3221(dev, address=0x40)
    dev.set_ina3221_bus_voltage(0x40, 1, 12.0)
    assert abs(chip.read_bus_voltage(1) - 12.0) < 0.05
    dev.set_register(0x40, chip.REG_MANUF_ID, INA3221.MANUFACTURER_ID)
    assert chip.read_manufacturer_id() == INA3221.MANUFACTURER_ID


def test_ina3221_rejects_bad_channel():
    chip = INA3221(FakeCh347Device(present_addresses={0x40}), address=0x40)
    with pytest.raises(ValueError):
        chip.read_current(0)
    with pytest.raises(ValueError):
        chip.read_current(4)


# --- interfaces ------------------------------------------------------------
@pytest.fixture
def wired():
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=3),
        ChipConfig("in1", "hc165", spi_select="cs1"),
    ]
    inp, blk, sw = build_real_interfaces(
        dev, specs,
        block_track={"B1": ("pwm1", 0), "B2": ("pwm1", 3)},
        block_occupancy={"B1": ("in1", 2), "B2": ("in1", 7)},
        switch_output={"SW_A": ("sw1", 5)},
    )
    return dev, inp, blk, sw


def test_track_voltage_bipolar_h_bridge(wired):
    dev, _inp, blk, _sw = wired
    assert isinstance(blk, Ch347TrackOutputInterface)
    # forward: IN1 (ch0) drives, IN2 (ch1) off
    assert blk.set_voltage("B1", 6.0) is True
    assert abs(dev.duty_cycle(0x40, 0) - 0.5) < 0.01
    assert dev.duty_cycle(0x40, 1) == 0.0
    # reverse: IN2 drives, IN1 off
    blk.set_voltage("B1", -12.0)
    assert dev.duty_cycle(0x40, 0) == 0.0
    assert abs(dev.duty_cycle(0x40, 1) - 1.0) < 0.01
    assert blk.get_actual_voltage("B1") == -12.0
    # unbound block is a no-op failure
    assert blk.set_voltage("NOPE", 5.0) is False


def test_switch_throw_bit(wired):
    dev, _inp, _blk, sw = wired
    assert isinstance(sw, Ch347SwitchOutputInterface)
    assert sw.set_throw("SW_A", 1) is True
    assert dev.latched_bit("cs0", 5) is True
    assert sw.get_last_throw("SW_A") == 1
    sw.set_throw("SW_A", 0)
    assert dev.latched_bit("cs0", 5) is False
    assert sw.set_throw("UNBOUND", 1) is False


def test_occupancy_read_maps_bound_blocks(wired):
    dev, inp, _blk, _sw = wired
    assert isinstance(inp, Ch347OccupancyInputInterface)
    dev.set_input_bit("cs1", 2, True, width=1)
    currents = inp.read_block_currents()
    assert currents["B1"] is True
    assert currents["B2"] is False


def test_hybrid_occupancy_selects_source_by_drive_state():
    # A block bound to both a '165 bit and an INA3221 channel: idle -> the digital
    # detector decides; driven -> the current channel decides and the stale '165
    # bit is ignored.
    dev = FakeCh347Device(present_addresses={0x40, 0x41})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("in1", "hc165", spi_select="cs1"),
        ChipConfig("cur1", "ina3221", i2c_address=0x41),
    ]
    inp, blk, _sw = build_real_interfaces(
        dev, specs,
        block_track={"B1": ("pwm1", 0)},
        block_occupancy={"B1": ("in1", 3)},
        switch_output={},
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015)},  # CH1, 15 mA threshold
        idle_deadband_v=0.25,
    )
    # Idle (0 V commanded): the digital detector is the source.
    dev.set_input_bit("cs1", 3, True, width=1)
    assert inp.read_block_currents()["B1"] is True
    dev.set_input_bit("cs1", 3, False, width=1)
    assert inp.read_block_currents()["B1"] is False
    # Driven: the current channel is the source; the '165 bit (set True) is ignored.
    dev.set_input_bit("cs1", 3, True, width=1)
    blk.set_voltage("B1", 8.0)
    dev.set_ina3221_current(0x41, 1, 0.002)      # 2 mA < 15 mA -> clear
    assert inp.read_block_currents()["B1"] is False
    dev.set_ina3221_current(0x41, 1, 0.030)      # 30 mA > 15 mA -> occupied
    assert inp.read_block_currents()["B1"] is True


def test_current_only_block_reads_ina_regardless_of_drive():
    # A block with only a current binding uses the INA even when idle (best effort:
    # reads ~0 with no drive).
    dev = FakeCh347Device(present_addresses={0x41})
    specs = [ChipConfig("cur1", "ina3221", i2c_address=0x41)]
    inp, _blk, _sw = build_real_interfaces(
        dev, specs, block_track={}, block_occupancy={}, switch_output={},
        block_occupancy_current={"B1": ("cur1", 2, 0.0, 0.015)},
    )
    assert inp is not None
    dev.set_ina3221_current(0x41, 3, 0.030)      # port 2 -> CH3
    assert inp.read_block_currents()["B1"] is True


def test_output_enable_bit_driven_low_at_build():
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0"),
    ]
    build_real_interfaces(
        dev, specs, block_track={}, block_occupancy={}, switch_output={},
        output_enable={"pwm1": ("sw1", 2)},
    )
    assert dev.latched_bit("cs0", 2) is False   # /OE low = PCA enabled, held


def test_safe_state_zeros_outputs(wired):
    dev, _inp, blk, sw = wired
    blk.set_voltage("B2", 8.0)
    sw.set_throw("SW_A", 1)
    blk.enter_safe_state()
    sw.enter_safe_state()
    assert dev.duty_cycle(0x40, 6) == 0.0
    assert dev.latched_bit("cs0", 5) is False
    assert blk.get_actual_voltage("B2") == 0.0


# --- self-test / port-test -------------------------------------------------
def _cfg(*specs):
    return HardwareConfig(controller=ControllerConfig(kind="ch347"), chips=list(specs))


def test_self_test_reports_reachability_and_ports():
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = _cfg(
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=3),
        ChipConfig("in1", "hc165", spi_select="cs1"),
    )
    report = self_test.run_self_test(cfg, device=dev)
    assert report["ok"] is True
    assert report["controller"]["reachable"] is True
    chips = {c["chip_id"]: c for c in report["chips"]}
    assert chips["pwm1"]["reachable"] is True
    assert chips["sw1"]["port_count"] == 24
    assert len(chips["sw1"]["ports"]) == 24


def test_self_test_reports_ina3221():
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = _cfg(ChipConfig("cur1", "ina3221", i2c_address=0x40))
    report = self_test.run_self_test(cfg, device=dev)
    chip = report["chips"][0]
    assert chip["reachable"] is True
    assert chip["role"] == "occupancy_input"
    assert chip["bus"] == "i2c"
    assert chip["port_count"] == 3


def test_self_test_absent_pca_is_unreachable():
    dev = FakeCh347Device(present_addresses=set())
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))
    report = self_test.run_self_test(cfg, device=dev)
    assert report["chips"][0]["reachable"] is False


def test_self_test_probes_only_named_chip():
    # The per-chip Self-test button must probe just that chip, not silently
    # re-probe every chip and overwrite their statuses.
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = _cfg(
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0"),
        ChipConfig("in1", "hc165", spi_select="cs1"),
    )
    report = self_test.run_self_test(cfg, device=dev, chip_id="sw1")
    assert report["ok"] is True
    assert [c["chip_id"] for c in report["chips"]] == ["sw1"]
    # The controller line still carries its I2C scan, so the report is complete.
    assert report["controller"]["reachable"] is True


def test_self_test_unknown_chip_id_probes_nothing():
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))
    report = self_test.run_self_test(cfg, device=dev, chip_id="ghost")
    assert report["ok"] is True
    assert report["chips"] == []


def test_self_test_open_failure_reports_unreachable():
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))

    def _boom():
        raise RuntimeError("busy")

    report = self_test.run_self_test(cfg, open_device=_boom)
    assert report["ok"] is False
    assert report["controller"]["reachable"] is False


def test_port_test_read_input():
    dev = FakeCh347Device()
    dev.set_input_bit("cs1", 4, True, width=1)
    cfg = _cfg(ChipConfig("in1", "hc165", spi_select="cs1"))
    assert self_test.run_port_test(cfg, "in1", 4, "read_input", device=dev) == {
        "ok": True, "value": True
    }


def test_port_test_read_current_returns_ma():
    dev = FakeCh347Device(present_addresses={0x41})
    dev.set_ina3221_current(0x41, 2, 0.025)     # port 1 -> INA channel 2, 25 mA
    cfg = _cfg(ChipConfig("cur1", "ina3221", i2c_address=0x41))
    result = self_test.run_port_test(cfg, "cur1", 1, "read_input", device=dev)
    assert result["ok"] is True
    assert abs(result["current_ma"] - 25.0) <= 0.5   # within one shunt LSB
    assert "bus_v" in result


def test_port_test_pulse_output():
    dev = FakeCh347Device()
    cfg = _cfg(ChipConfig("sw1", "hc595", spi_select="cs0"))
    # hold_s default is 0.4s; patch time.sleep to keep the test fast.
    import hardware.self_test as st
    orig = st.time.sleep
    st.time.sleep = lambda *_a, **_k: None
    try:
        result = self_test.run_port_test(cfg, "sw1", 3, "pulse_output", device=dev)
    finally:
        st.time.sleep = orig
    assert result["ok"] is True


def test_port_test_unknown_chip():
    cfg = _cfg(ChipConfig("in1", "hc165", spi_select="cs1"))
    result = self_test.run_port_test(cfg, "ghost", 0, "read_input",
                                     device=FakeCh347Device())
    assert result["ok"] is False


# --- commission session (live input + output marquee) ----------------------
def test_commission_session_drives_and_reads():
    from hardware.commission import CommissionSession

    fake = FakeCh347Device(present_addresses={0x40})
    cfg = _cfg(
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=3),
        ChipConfig("in1", "hc165", spi_select="cs1"),
    )
    sess = CommissionSession()
    sess.open(cfg, open_device=lambda: fake)
    # switch bit
    sess.set_output("sw1", 5, 1.0)
    assert fake.latched_bit("cs0", 5) is True
    # track PWM (bipolar pair: forward channel driven, reverse off)
    sess.set_output("pwm1", 0, 0.5)
    assert abs(fake.duty_cycle(0x40, 0) - 0.5) < 0.01
    assert fake.duty_cycle(0x40, 1) == 0.0
    # live input read
    fake.set_input_bit("cs1", 3, True, width=1)
    bits = sess.read_inputs("in1")
    assert bits[3] is True and bits[0] is False
    assert sess.status()["open"] is True
    sess.close()
    assert fake.closed is True
    assert sess.status()["open"] is False


def test_commission_reads_currents():
    from hardware.commission import CommissionSession

    fake = FakeCh347Device(present_addresses={0x41})
    fake.set_ina3221_current(0x41, 1, 0.010)
    fake.set_ina3221_current(0x41, 3, 0.030)
    cfg = _cfg(ChipConfig("cur1", "ina3221", i2c_address=0x41))
    sess = CommissionSession()
    sess.open(cfg, open_device=lambda: fake)
    currents = sess.read_currents("cur1")
    assert len(currents) == 3
    assert abs(currents[0] - 10.0) <= 0.5     # CH1
    assert abs(currents[2] - 30.0) <= 0.5     # CH3
    # a non-current chip is rejected clearly
    import pytest as _pytest
    with _pytest.raises(KeyError):
        sess.read_currents("nope")
    sess.close()
