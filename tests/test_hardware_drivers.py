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
    assert ports_for_chip("pca9685", 1) == 8      # bipolar pairs
    assert ports_for_chip("hc595", 3) == 24       # cascade
    assert ports_for_chip("pca9685", 5) == 8      # PWM not cascadable -> ignores chain


def test_catalog_payload_serializable():
    payload = catalog_payload()
    assert {c["kind"] for c in payload["chips"]} == {"pca9685", "hc595", "hc165"}
    assert payload["spi_select_lines"][:2] == ["cs0", "cs1"]


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


def test_self_test_absent_pca_is_unreachable():
    dev = FakeCh347Device(present_addresses=set())
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))
    report = self_test.run_self_test(cfg, device=dev)
    assert report["chips"][0]["reachable"] is False


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
