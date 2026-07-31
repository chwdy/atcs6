"""Real hardware driver package — chips, interface adapters, and self-test, all
exercised against the in-memory `FakeCh347Device` so no USB hardware is needed
(design_new.md "Power", Hardware interface boundary).

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_drivers.py -v
"""

import time

import pytest

from hardware_config import (
    ChipConfig,
    ControllerConfig,
    CurrentBinding,
    HardwareConfig,
    PortBinding,
    TrackBinding,
)
from hardware import interfaces as hw_interfaces
from hardware import self_test
from hardware.catalog import (
    CHIP_CATALOG,
    MAX_OUTPUT_HIGH_S,
    MAX_PORT_TEST_PULSE_S,
    PORT_TEST_PULSE_S,
    SWITCH_THROW_PULSE_S,
    catalog_payload,
    ports_for_chip,
)
from hardware.chips.hc165 import HC165Chain
from hardware.chips.hc595 import HC595Chain, sleep_holding
from hardware.chips.ina219 import INA219
from hardware.chips.ina3221 import INA3221
from hardware.chips.pca9685 import PCA9685
from hardware.fake import FakeCh347Device
from hardware.interfaces import (
    OccupancyDriver,
    SwitchDriver,
    TrackDriver,
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
    assert {c["kind"] for c in payload["chips"]} == {
        "pca9685", "hc595", "hc165", "ina3221", "ina219",
    }
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


# --- '595 hold guard: no coil bit may stay energized ------------------------
def test_hold_guard_forces_a_stuck_bit_low():
    """The backstop for a caller that raises a bit and never lowers it — the
    case a `finally` cannot reach."""
    dev = FakeCh347Device()
    chain = HC595Chain(dev, select="cs0", max_high_s=0.05)
    chain.set_output(3, True)
    assert dev.latched_bit("cs0", 3) is True

    time.sleep(0.15)
    assert dev.latched_bit("cs0", 3) is False
    assert chain.any_high is False


def test_hold_guard_reports_what_it_released():
    released = []
    dev = FakeCh347Device()
    chain = HC595Chain(
        dev, select="cs0", max_high_s=0.05, on_auto_release=released.append
    )
    chain.set_output(0, True)
    chain.set_output(2, True)
    time.sleep(0.15)
    assert released == [0b101]


def test_hold_guard_does_not_fire_once_the_bits_are_low():
    """A normal pulse drops its own bits, so the guard must disarm — otherwise
    every throw would leave a timer running to re-latch an all-low chain."""
    dev = FakeCh347Device()
    chain = HC595Chain(dev, select="cs0", max_high_s=0.05)
    chain.set_output(1, True)
    chain.set_output(1, False)
    writes = []
    real_latch = dev.latch_out
    dev.latch_out = lambda data, select: (writes.append(select), real_latch(data, select))[1]
    time.sleep(0.15)
    assert writes == []          # the guard disarmed; nothing re-latched


def test_hold_guard_deadline_is_far_longer_than_a_throw():
    # A guard that could expire mid-throw would cut the coil pulse short.
    assert MAX_OUTPUT_HIGH_S > SWITCH_THROW_PULSE_S * 10


def test_switch_coil_bits_are_guarded_by_default(wired):
    """The runtime's own chains get the guard without opting in."""
    _dev, _inp, _blk, sw = wired
    assert sw._chips["sw1"].max_high_s == MAX_OUTPUT_HIGH_S


# --- init: the chain must come up de-energized ------------------------------
def test_build_clears_every_shift_out_chain_before_configuring_anything():
    """A '595 powers up with undefined contents, so the clear must land before
    the slow I2C setup — not after a PCA9685 has been configured."""
    dev = FakeCh347Device(present_addresses={0x40})
    order = []
    real_latch, real_write = dev.latch_out, dev.i2c_write
    dev.latch_out = lambda data, select: (order.append(("latch", select)), real_latch(data, select))[1]
    dev.i2c_write = lambda addr, data: (order.append(("i2c", addr)), real_write(addr, data))[1]
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0"),
    ]
    build_real_interfaces(
        dev, specs, block_track={"B1": ("pwm1", 0, 1)}, block_occupancy={},
        switch_output={"SW_A": ("sw1", 5, 6, 4)},
    )
    # The '595 clear precedes the first I2C byte, even though the PWM chip is
    # listed first.
    assert order[0] == ("latch", "cs0")
    for bit in (4, 5, 6):
        assert dev.latched_bit("cs0", bit) is False


def test_build_refuses_to_start_with_an_energized_coil_bit(monkeypatch):
    """If anything leaves a coil bit high during init, the build fails loudly
    instead of handing back a session with a solenoid cooking."""
    dev = FakeCh347Device()
    specs = [ChipConfig("sw1", "hc595", spi_select="cs0")]

    # Stand in for a future init step that raises a bit after the clear.
    real_assert = hw_interfaces._assert_switch_ports_low

    def _raise_a_coil_first(switch_chips, switch_output):
        switch_chips["sw1"].set_output(5, True)
        return real_assert(switch_chips, switch_output)

    monkeypatch.setattr(hw_interfaces, "_assert_switch_ports_low", _raise_a_coil_first)
    with pytest.raises(RuntimeError, match="switch coil outputs are energized"):
        build_real_interfaces(
            dev, specs, block_track={}, block_occupancy={},
            switch_output={"SW_A": ("sw1", 5, 6, 4)},
        )
    # And it de-energized on the way out.
    assert dev.latched_bit("cs0", 5) is False


def test_port_test_de_energizes_a_coil_bit_when_the_pulse_raises(monkeypatch):
    """`run_port_test` turns an exception into a failed-test report, so without
    a finally the coil would stay hot behind an "ok: false"."""
    dev = FakeCh347Device()
    chain = HC595Chain(dev, select="cs0")

    def _boom(_chip, _seconds):
        raise RuntimeError("bridge dropped mid-pulse")

    # A '595 hold goes through sleep_holding (it re-latches to outlive the hold
    # guard), so that is where a mid-pulse failure lands.
    monkeypatch.setattr(self_test, "sleep_holding", _boom)
    with pytest.raises(RuntimeError):
        self_test._pulse("hc595", chain, 5)
    assert dev.latched_bit("cs0", 5) is False


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
        block_track={"B1": ("pwm1", 0, 1), "B2": ("pwm1", 6, 7)},
        block_occupancy={"B1": ("in1", 2), "B2": ("in1", 7)},
        # main coil on bit 5, diverge on bit 6, TB6612 standby on bit 4
        switch_output={"SW_A": ("sw1", 5, 6, 4)},
    )
    return dev, inp, blk, sw


def test_track_voltage_bipolar_h_bridge(wired):
    dev, _inp, blk, _sw = wired
    assert isinstance(blk, TrackDriver)
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


def test_reversed_polarity_swaps_the_two_rails():
    """`Block.polarity_reversed` trades the block's two channels: a positive
    voltage drives the rail the binding calls `reverse`. The binding itself is
    untouched, which is what lets the detection rail stay where it is wired."""
    dev = FakeCh347Device(present_addresses={0x40})
    reversed_blocks = {"B2"}
    _inp, blk, _sw = build_real_interfaces(
        dev,
        [ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
        block_track={"B1": ("pwm1", 0, 1), "B2": ("pwm1", 6, 7)},
        block_occupancy={},
        switch_output={},
        track_polarity_of=lambda block_id: block_id in reversed_blocks,
    )
    # B1 is normal: +6 V drives the forward channel, as ever.
    blk.set_voltage("B1", 6.0)
    assert abs(dev.duty_cycle(0x40, 0) - 0.5) < 0.01
    assert dev.duty_cycle(0x40, 1) == 0.0
    # B2 is reversed: the same +6 V drives ch7, the one the binding calls reverse.
    blk.set_voltage("B2", 6.0)
    assert dev.duty_cycle(0x40, 6) == 0.0
    assert abs(dev.duty_cycle(0x40, 7) - 0.5) < 0.01
    # ... and a negative command drives the other one.
    blk.set_voltage("B2", -6.0)
    assert abs(dev.duty_cycle(0x40, 6) - 0.5) < 0.01
    assert dev.duty_cycle(0x40, 7) == 0.0
    # The commanded voltage is the nominal one either way: polarity is a wiring
    # correction below the interface, so `get_actual_voltage` (and the drive-state
    # test that reads it) never learns which rail carried it.
    assert blk.get_actual_voltage("B2") == -6.0


def test_polarity_is_read_per_write_not_captured_at_build():
    """The driver holds a callable, not a snapshot, so flipping a block in the
    editor lands on the next track write with no session rebuild."""
    dev = FakeCh347Device(present_addresses={0x40})
    reversed_blocks: set = set()
    _inp, blk, _sw = build_real_interfaces(
        dev,
        [ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={},
        switch_output={},
        track_polarity_of=lambda block_id: block_id in reversed_blocks,
    )
    blk.set_voltage("B1", 12.0)
    assert abs(dev.duty_cycle(0x40, 0) - 1.0) < 0.01

    reversed_blocks.add("B1")            # the operator flips it mid-session
    blk.set_voltage("B1", 12.0)
    assert dev.duty_cycle(0x40, 0) == 0.0
    assert abs(dev.duty_cycle(0x40, 1) - 1.0) < 0.01


def test_switch_throw_pulses_the_side_coil_and_rests_low(wired):
    """A solenoid point motor burns out if a coil is held, so a throw is a pulse:
    the commanded coil ends the call de-energized, as does the standby bit."""
    dev, _inp, _blk, sw = wired
    assert isinstance(sw, SwitchDriver)
    assert sw.set_throw("SW_A", 1) is True
    assert sw.get_last_throw("SW_A") == 1
    # Nothing is left driven once the throw returns.
    assert dev.latched_bit("cs0", 5) is False    # main coil
    assert dev.latched_bit("cs0", 6) is False    # diverge coil
    assert dev.latched_bit("cs0", 4) is False    # standby
    assert sw.set_throw("SW_A", 0) is True
    assert sw.get_last_throw("SW_A") == 0
    assert sw.set_throw("UNBOUND", 1) is False


class _SleepSpy:
    """Stands in for the `time` module inside `hardware.interfaces`, so the
    latched state can be sampled during the pulse without a real wait."""

    def __init__(self, on_sleep):
        self._on_sleep = on_sleep

    def sleep(self, seconds):
        self._on_sleep(seconds)


def test_switch_throw_energizes_only_the_target_coil(wired, monkeypatch):
    """Sample the latched state mid-pulse: exactly one coil is up, with standby."""
    dev, _inp, _blk, sw = wired
    seen = []
    monkeypatch.setattr(
        hw_interfaces,
        "time",
        _SleepSpy(lambda seconds: seen.append({
            "pulse_s": seconds,
            **{bit: dev.latched_bit("cs0", bit) for bit in (4, 5, 6)},
        })),
    )
    sw.set_throw("SW_A", 0)          # MAIN -> bit 5
    sw.set_throw("SW_A", 1)          # DIVERGE -> bit 6

    assert seen[0] == {"pulse_s": SWITCH_THROW_PULSE_S, 4: True, 5: True, 6: False}
    assert seen[1] == {"pulse_s": SWITCH_THROW_PULSE_S, 4: True, 5: False, 6: True}


def test_switch_throw_write_order_matches_the_bench_proven_sequence(wired, monkeypatch):
    """The order of the individual bit writes is the safety contract, not just
    the end state: standby rises first, the other coil is forced low while the
    target is driven, and standby falls *before* the coil — dropping the coil
    first would leave the bridge enabled with both inputs low, its brake mode,
    which shorts the coil.
    """
    _dev, _inp, _blk, sw = wired
    writes = []
    chip = sw._chips["sw1"]
    real_set = chip.set_output
    monkeypatch.setattr(
        chip, "set_output", lambda port, level: (writes.append((port, level)), real_set(port, level))[1]
    )
    monkeypatch.setattr(hw_interfaces, "time", _SleepSpy(lambda _s: writes.append("PULSE")))

    sw.set_throw("SW_A", 0)          # MAIN: coil 5, other coil 6, standby 4
    assert writes == [
        (4, True),    # standby high — enable the bridge
        (5, True),    # target coil high
        (6, False),   # other input low, asserted every throw
        "PULSE",
        (4, False),   # standby low FIRST -> outputs high-impedance
        (5, False),   # coil low — bridge already off, settles the resting pattern
    ]


def test_switch_without_standby_still_de_energizes_its_coil():
    """With no standby bit the coil write is the only de-energize, so it must
    happen unconditionally."""
    dev = FakeCh347Device()
    specs = [ChipConfig("sw1", "hc595", spi_select="cs0")]
    _inp, _blk, sw = build_real_interfaces(
        dev, specs, block_track={}, block_occupancy={},
        switch_output={"SW_A": ("sw1", 0, 1, None)},
    )
    assert sw.set_throw("SW_A", 1) is True
    assert dev.latched_bit("cs0", 0) is False
    assert dev.latched_bit("cs0", 1) is False


def test_switch_throw_is_correct_from_a_stale_bit_pattern(wired, monkeypatch):
    """A throw must not depend on how the previous one left the chain: with the
    opposite coil's bit already high, the throw still drives only its own side."""
    dev, _inp, _blk, sw = wired
    sw._chips["sw1"].set_output(6, True)     # stale DIVERGE bit, as if left high
    assert dev.latched_bit("cs0", 6) is True

    seen = {}
    monkeypatch.setattr(
        hw_interfaces,
        "time",
        _SleepSpy(lambda _s: seen.update({bit: dev.latched_bit("cs0", bit) for bit in (4, 5, 6)})),
    )
    sw.set_throw("SW_A", 0)                  # throw MAIN

    assert seen == {4: True, 5: True, 6: False}   # stale bit cleared during the pulse
    assert dev.latched_bit("cs0", 5) is False
    assert dev.latched_bit("cs0", 6) is False


def test_switch_throw_de_energizes_even_when_the_pulse_raises(wired, monkeypatch):
    """The de-energize runs in a finally, so a failed throw leaves no coil driven."""
    dev, _inp, _blk, sw = wired

    def boom(_seconds):
        raise RuntimeError("bus glitch")

    monkeypatch.setattr(hw_interfaces, "time", _SleepSpy(boom))
    with pytest.raises(RuntimeError):
        sw.set_throw("SW_A", 1)
    assert dev.latched_bit("cs0", 6) is False
    assert dev.latched_bit("cs0", 4) is False


def test_occupancy_read_maps_bound_blocks(wired):
    dev, inp, _blk, _sw = wired
    assert isinstance(inp, OccupancyDriver)
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
        block_track={"B1": ("pwm1", 0, 1)},
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


# --- bus scan / port-test --------------------------------------------------
def _cfg(*specs):
    return HardwareConfig(controller=ControllerConfig(kind="ch347"), chips=list(specs))


def test_bus_scan_reports_answering_addresses():
    dev = FakeCh347Device(present_addresses={0x40, 0x44})
    cfg = _cfg(
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=3),
    )
    report = self_test.run_bus_scan(cfg, device=dev)
    assert report["ok"] is True
    assert report["controller"]["reachable"] is True
    assert report["controller"]["i2c_scan"] == ["0x40", "0x44"]


def test_bus_scan_reports_addresses_no_chip_is_configured_for():
    # The scan asks the bus, not the chip list, so a part strapped somewhere the
    # user did not expect shows up rather than reading as silence.
    dev = FakeCh347Device(present_addresses={0x41})
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))
    report = self_test.run_bus_scan(cfg, device=dev)
    assert report["controller"]["i2c_scan"] == ["0x41"]


def test_bus_scan_empty_bus_is_still_ok():
    dev = FakeCh347Device(present_addresses=set())
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))
    report = self_test.run_bus_scan(cfg, device=dev)
    assert report["ok"] is True
    assert report["controller"]["i2c_scan"] == []


def test_bus_scan_open_failure_reports_unreachable():
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))

    def _boom():
        raise RuntimeError("busy")

    report = self_test.run_bus_scan(cfg, open_device=_boom)
    assert report["ok"] is False
    assert report["controller"]["reachable"] is False
    assert report["controller"]["i2c_scan"] == []


def test_port_test_read_input():
    dev = FakeCh347Device()
    dev.set_input_bit("cs1", 4, True, width=1)
    cfg = _cfg(ChipConfig("in1", "hc165", spi_select="cs1"))
    assert self_test.run_port_test(cfg, "in1", 4, "read_input", device=dev) == {
        "ok": True, "value": True
    }


def test_port_test_read_input_on_gpio_latched_hc165():
    # A '165 is wired on either bus: a chip-select pad (spi) or a plain pin
    # (gpio). Only ch347_device branches on the pad name — everything above it
    # is bus-agnostic — so the same read must work with a gpio load line.
    dev = FakeCh347Device()
    dev.set_input_bit("gpio5", 4, True, width=1)
    cfg = _cfg(ChipConfig("in1", "hc165", bus="gpio", spi_select="gpio5"))
    assert self_test.run_port_test(cfg, "in1", 4, "read_input", device=dev) == {
        "ok": True, "value": True
    }


def test_gpio_transfer_pad_may_be_an_hc165_load_line():
    # With a '595 on cs0 and a '165 on cs1, a gpio-latched chip has to borrow a
    # pad for its transfer. cs1 is the only one left, and borrowing a '165 load
    # line is safe: the transfer just freezes and re-loads its parallel inputs.
    specs = [
        ChipConfig("sw1", "hc595", spi_select="cs0"),
        ChipConfig("in1", "hc165", spi_select="cs1"),
        ChipConfig("sw2", "hc595", bus="gpio", spi_select="gpio2"),
    ]
    assert hw_interfaces.gpio_transfer_cs(specs) == "cs1"


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
    # The default hold is a full second (measurable on a meter); ask for the
    # shortest one so the test does not wait on it.
    result = self_test.run_port_test(
        cfg, "sw1", 3, "pulse_output", device=dev, pulse_s=0.0
    )
    assert result["ok"] is True
    assert dev.latched_bit("cs0", 3) is False     # de-energized on the way out


def test_port_test_pulse_is_long_enough_to_measure():
    """The bench pulse answers "is this the wire I think it is", read off a
    multimeter — so the default width has to be measurable, not a throw blip."""
    assert PORT_TEST_PULSE_S >= 1.0
    assert PORT_TEST_PULSE_S > SWITCH_THROW_PULSE_S * 50


def test_port_test_reports_the_width_it_held():
    """The UI tells the user how long the line was driven, so the width the
    server actually used comes back with the result."""
    dev = FakeCh347Device()
    cfg = _cfg(ChipConfig("sw1", "hc595", spi_select="cs0"))
    result = self_test.run_port_test(
        cfg, "sw1", 1, "pulse_output", device=dev, pulse_s=0.02
    )
    assert result["pulse_s"] == 0.02


def test_port_test_pulse_width_is_clamped_and_defaulted():
    """The width crosses REST, so a garbage or open-ended request cannot park a
    line high: junk falls back to the default, over-long is cut to the ceiling."""
    assert self_test.clamp_pulse_s(None) == PORT_TEST_PULSE_S
    assert self_test.clamp_pulse_s("nonsense") == PORT_TEST_PULSE_S
    assert self_test.clamp_pulse_s(float("nan")) == PORT_TEST_PULSE_S
    assert self_test.clamp_pulse_s(9_999.0) == MAX_PORT_TEST_PULSE_S
    assert self_test.clamp_pulse_s(-5.0) == 0.0
    assert self_test.clamp_pulse_s("0.25") == 0.25
    # A caller with a different default (the switch throw test) keeps it.
    assert self_test.clamp_pulse_s(None, default=SWITCH_THROW_PULSE_S) == SWITCH_THROW_PULSE_S


def test_a_measurable_hold_outlives_the_guard_without_raising_the_ceiling():
    """A 1s port-test hold is longer than a guarded chain will hold a bit on one
    write, so `sleep_holding` re-latches inside the window. The bit must still be
    high at the end — a guard release mid-measurement would read as a dead wire."""
    dev = FakeCh347Device()
    released: list = []
    chain = HC595Chain(dev, select="cs0", max_high_s=0.05,
                       on_auto_release=released.append)
    chain.set_output(2, True)
    sleep_holding(chain, 0.2)                     # 4x the ceiling
    assert dev.latched_bit("cs0", 2) is True      # never auto-released
    assert released == []
    chain.set_output(2, False)
    chain.cancel_guard()


def test_the_guard_still_fires_once_the_refreshes_stop():
    """Refreshing must not disarm the guard permanently: once a holder stops
    (crashed, stalled), the chain goes low on the usual deadline."""
    dev = FakeCh347Device()
    released: list = []
    chain = HC595Chain(dev, select="cs0", max_high_s=0.05,
                       on_auto_release=released.append)
    chain.set_output(2, True)
    sleep_holding(chain, 0.12)
    time.sleep(0.15)                              # no more refreshes
    assert released == [0b100]
    assert dev.latched_bit("cs0", 2) is False


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


# --- loss-of-control watchdog ---------------------------------------------
# design_new.md "Power -> Hardware interface boundary": a real output
# implementation arms a watchdog that drops the outputs to the safe state when
# the control loop stops refreshing them.
#
# These tests run under pytest-xdist, so a scheduling hiccup can stretch any
# wall-clock gap. Only the "it DOES fire" direction may race against a short
# timeout (load can only delay it, and we poll). Every "it does NOT fire"
# assertion uses a timeout long enough that load cannot reach it.
_WATCHDOG_NEVER = 30.0     # cannot elapse during a test
_WATCHDOG_SOON = 0.3       # long enough to assert on, short enough to poll out


def test_watchdog_drops_outputs_to_safe_state_when_control_loop_stalls():
    import time

    fake = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=1),
    ]
    _inp, blk, sw = build_real_interfaces(
        fake,
        specs,
        {"BLK_A": ("pwm1", 0, 1)},
        {},
        {"SW_A": ("sw1", 1, 2, None)},
        watchdog_timeout_s=_WATCHDOG_SOON,
    )

    # The control loop drives the track and throws a switch.
    assert blk.set_voltage("BLK_A", 9.0) is True
    assert sw.set_throw("SW_A", 1) is True

    # Then it stalls: no further writes land. Poll generously — load can only
    # delay the watchdog, never prevent it.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and blk.get_actual_voltage("BLK_A") != 0.0:
        time.sleep(0.01)

    # Both output interfaces reached the safe state, not just the track.
    assert blk.get_actual_voltage("BLK_A") == 0.0
    assert sw.get_last_throw("SW_A") is None


def test_watchdog_does_not_fire_while_the_control_loop_refreshes():
    fake = FakeCh347Device(present_addresses={0x40})
    specs = [ChipConfig("pwm1", "pca9685", i2c_address=0x40)]
    _inp, blk, _sw = build_real_interfaces(
        fake, specs, {"BLK_A": ("pwm1", 0, 1)}, {}, {}, watchdog_timeout_s=_WATCHDOG_NEVER
    )

    for _ in range(5):
        assert blk.set_voltage("BLK_A", 9.0) is True

    assert blk.get_actual_voltage("BLK_A") == 9.0
    assert blk._watchdog.fired is False


def test_watchdog_is_unarmed_by_default_so_test_harnesses_stay_inert():
    fake = FakeCh347Device(present_addresses={0x40})
    specs = [ChipConfig("pwm1", "pca9685", i2c_address=0x40)]
    _inp, blk, _sw = build_real_interfaces(fake, specs, {"BLK_A": ("pwm1", 0, 1)}, {}, {})
    assert blk._watchdog is None


def test_watchdog_never_fires_until_it_is_first_petted():
    """It starts disarmed, so a build that never drives an output stays quiet."""
    import time

    from hardware.watchdog import OutputWatchdog

    fired = []
    wd = OutputWatchdog(lambda: fired.append(1), timeout_s=0.05)
    try:
        time.sleep(0.25)  # several timeout windows with no pet
        assert fired == []
        assert wd.fired is False
    finally:
        wd.stop()


def test_disarm_clears_a_pending_deadline():
    from hardware.watchdog import OutputWatchdog

    fired = []
    wd = OutputWatchdog(lambda: fired.append(1), timeout_s=_WATCHDOG_NEVER)
    try:
        wd.pet()
        wd.disarm()
        assert wd.fired is False
        assert fired == []
    finally:
        wd.stop()


def test_enter_safe_state_disarms_the_watchdog():
    """`enter_safe_state()` IS the safe state, so the watchdog must stand down."""
    fake = FakeCh347Device(present_addresses={0x40})
    specs = [ChipConfig("pwm1", "pca9685", i2c_address=0x40)]
    _inp, blk, _sw = build_real_interfaces(
        fake, specs, {"BLK_A": ("pwm1", 0, 1)}, {}, {}, watchdog_timeout_s=_WATCHDOG_NEVER
    )
    blk.set_voltage("BLK_A", 9.0)
    blk.enter_safe_state()
    assert blk.get_actual_voltage("BLK_A") == 0.0
    assert blk._watchdog.fired is False


# --- INA219 (the single-channel current sensor) ----------------------------
def test_ina219_current_sign_and_probe():
    dev = FakeCh347Device(present_addresses={0x45})
    chip = INA219(dev, address=0x45, shunt_ohms=0.1)
    chip.configure()
    assert chip.probe() is True
    # One shunt LSB is 10 uV / 0.1 ohm = 0.1 mA, so reads are exact to +/-0.1 mA.
    lsb_a = INA219.SHUNT_LSB_V / 0.1
    dev.set_ina219_current(0x45, 0.045)           # 45 mA forward
    assert abs(chip.read_current() - 0.045) <= lsb_a
    dev.set_ina219_current(0x45, -0.030)          # 30 mA reverse (regeneration)
    assert abs(chip.read_current() + 0.030) <= lsb_a
    assert INA219(FakeCh347Device(), address=0x45).probe() is False


def test_ina219_configure_sets_continuous_shunt_bus():
    dev = FakeCh347Device(present_addresses={0x45})
    INA219(dev, address=0x45).configure()
    assert dev.registers[0x45][0x00] & 0x0007 == 0x0007


def test_ina219_bus_voltage():
    dev = FakeCh347Device(present_addresses={0x45})
    chip = INA219(dev, address=0x45)
    dev.set_ina219_bus_voltage(0x45, 11.8)
    assert abs(chip.read_bus_voltage() - 11.8) < 0.05


def test_ina219_rejects_a_second_channel():
    # The occupancy interface addresses every current sensor as `port + 1`, so a
    # port past the chip's one channel must fail loudly rather than alias to it.
    chip = INA219(FakeCh347Device(present_addresses={0x45}), address=0x45)
    with pytest.raises(ValueError):
        chip.read_current(2)


def test_ina219_fills_the_current_sense_occupancy_slot():
    ck = CHIP_CATALOG["ina219"]
    assert (ck.role, ck.port_kind, ck.ports_per_unit) == ("occupancy_input", "current_sense", 1)
    assert ck.direction == "input"
    assert ports_for_chip("ina219", chain_length=4) == 1     # not cascadable

    dev = FakeCh347Device(present_addresses={0x40, 0x45})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("cur1", "ina219", i2c_address=0x45),
    ]
    inp, blk, _sw = build_real_interfaces(
        dev, specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={},
        switch_output={},
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015)},
    )
    dev.set_ina219_current(0x45, 0.002)                      # 2 mA, under threshold
    assert inp.read_block_currents() == {"B1": False}
    dev.set_ina219_current(0x45, 0.120)                      # 120 mA, a loco
    assert inp.read_block_currents() == {"B1": True}


# --- PWM readback + the track direction sweep ------------------------------
def test_pca9685_read_duty_cycle_round_trip():
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    chip.configure()
    chip.set_duty_cycle(3, 0.75)
    assert abs(chip.read_duty_cycle(3) - 0.75) < 0.001
    chip.set_duty_cycle(3, 0.0)
    assert chip.read_duty_cycle(3) == 0.0


def _writes(chip):
    """Record every (channel, duty) write a chip receives, in order."""
    seen = []
    original = chip.set_duty_cycle

    def spy(channel, duty):
        seen.append((channel, round(duty, 3)))
        original(channel, duty)

    chip.set_duty_cycle = spy
    return seen


def test_drive_sequence_runs_both_polarities_and_ends_de_energized():
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    seen = _writes(chip)
    # The two rails are two independently-chosen channels (ch4 forward, ch9
    # reverse), not a fixed adjacent pair: forward, both off, then reverse.
    self_test._drive_sequence(chip, forward=4, reverse=9, hold_s=0.0)
    assert (4, 1.0) in seen and (9, 1.0) in seen
    assert seen.index((4, 1.0)) < seen.index((9, 1.0))
    assert seen[-2:] == [(4, 0.0), (9, 0.0)]
    assert chip.read_duty_cycle(4) == 0.0 and chip.read_duty_cycle(9) == 0.0


def test_drive_sequence_never_drives_both_rails_at_once():
    # Both H-bridge inputs high is a shoot-through across the driver, so no
    # moment of the sweep may leave both bound channels energized.
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    live = {4: 0.0, 9: 0.0}
    original = chip.set_duty_cycle

    def spy(channel, duty):
        original(channel, duty)
        if channel in live:
            live[channel] = duty
        assert not (live[4] > 0 and live[9] > 0), f"both rails driven: {live}"

    chip.set_duty_cycle = spy
    self_test._drive_sequence(chip, forward=4, reverse=9, hold_s=0.0)


def test_drive_sequence_de_energizes_when_a_write_raises():
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    chip.configure()
    original = chip.set_duty_cycle
    calls = {"n": 0}

    def flaky(channel, duty):
        calls["n"] += 1
        if calls["n"] == 3:          # fail partway through, with a rail hot
            raise IOError("bridge dropped")
        original(channel, duty)

    chip.set_duty_cycle = flaky
    with pytest.raises(IOError):
        self_test._drive_sequence(chip, forward=0, reverse=1, hold_s=0.0)
    # The finally clause still ran, so the track is dead.
    assert chip.read_duty_cycle(0) == 0.0 and chip.read_duty_cycle(1) == 0.0


def test_track_test_sweeps_the_blocks_two_bound_channels(monkeypatch):
    monkeypatch.setattr(self_test, "TRACK_TEST_HOLD_S", 0.0)
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = HardwareConfig(
        controller=ControllerConfig(kind="ch347"),
        chips=[ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
        block_track={"B1": TrackBinding("pwm1", 4, 9)},
    )
    res = self_test.run_track_test(cfg, "B1", device=dev)
    assert res["ok"] is True and res["volts"] == 12.0
    # Both bound channels were exercised and left off.
    assert dev.duty_cycle(0x40, 4) == 0.0 and dev.duty_cycle(0x40, 9) == 0.0


def test_track_test_reports_a_block_with_no_track_binding():
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = HardwareConfig(
        controller=ControllerConfig(kind="ch347"),
        chips=[ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
    )
    res = self_test.run_track_test(cfg, "B9", device=dev)
    assert res["ok"] is False and "no track binding" in res["error"]


def test_pulse_output_drives_a_single_pwm_channel():
    # The picker's double-click pulses one channel (raw line), not a pair.
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = _cfg(ChipConfig("pwm1", "pca9685", i2c_address=0x40))
    res = self_test.run_port_test(cfg, "pwm1", 5, "pulse_output", device=dev,
                                  pulse_s=0.0)
    assert res["ok"] is True
    assert dev.duty_cycle(0x40, 5) == 0.0        # returned to off
    assert dev.duty_cycle(0x40, 10) == 0.0       # the old pair-mate is untouched


# --- the block occupancy test ----------------------------------------------
def _occupancy_cfg():
    """A block wired to both detectors plus track power."""
    return HardwareConfig(
        controller=ControllerConfig(kind="ch347"),
        chips=[
            ChipConfig("pwm1", "pca9685", i2c_address=0x40),
            ChipConfig("in1", "hc165", spi_select="cs1"),
            ChipConfig("cur1", "ina3221", i2c_address=0x41),
        ],
        block_track={"B1": TrackBinding("pwm1", 0, 1)},
        block_occupancy={"B1": PortBinding("in1", 3)},
        block_occupancy_current={
            "B1": CurrentBinding("cur1", 1, baseline_ma=0.0, threshold_ma=15.0)
        },
    )


def _occupancy_device():
    return FakeCh347Device(present_addresses={0x40, 0x41})


def test_occupancy_test_idle_block_is_decided_by_the_digital_detector():
    dev = _occupancy_device()
    dev.set_input_bit("cs1", 3, True)                    # detector: occupied
    dev.set_ina3221_current(0x41, 2, 0.001)              # 1 mA, under threshold
    res = self_test.run_occupancy_test(_occupancy_cfg(), "B1", device=dev)
    assert res["ok"] is True
    assert res["driven"] is False and res["track"]["volts"] == 0.0
    assert res["source"] == "digital" and res["occupied"] is True
    # Both detectors are still reported, so a disagreement is visible.
    assert res["digital"]["occupied"] is True
    assert res["current"]["occupied"] is False


def test_occupancy_test_applies_the_same_polarity_the_runtime_applies():
    # The bench test and the runtime must not disagree about a block, or the
    # polarity settings look broken exactly where they are doing their job.
    cfg = _occupancy_cfg()
    cfg.chips[1] = ChipConfig("in1", "hc165", spi_select="cs1", active_low=True)
    dev = _occupancy_device()
    dev.set_ina3221_current(0x41, 2, 0.001)
    res = self_test.run_occupancy_test(cfg, "B1", device=dev)   # line sits at 0
    assert res["digital"]["value"] is False        # the pin, unvarnished
    assert res["digital"]["occupied"] is True      # what it means on this wiring
    assert res["source"] == "digital" and res["occupied"] is True


def test_occupancy_test_driven_block_is_decided_by_the_current_detector():
    dev = _occupancy_device()
    dev.set_input_bit("cs1", 3, True)                    # detector still says occupied
    dev.set_ina3221_current(0x41, 2, 0.001)              # but almost no current
    PCA9685(dev, address=0x40).set_duty_cycle(0, 1.0)    # block driven at full
    res = self_test.run_occupancy_test(_occupancy_cfg(), "B1", device=dev)
    assert res["driven"] is True and abs(res["track"]["volts"] - 12.0) < 0.01
    assert res["source"] == "current" and res["occupied"] is False


def test_occupancy_test_reads_reverse_polarity_as_driven():
    dev = _occupancy_device()
    dev.set_ina3221_current(0x41, 2, 0.120)              # 120 mA -> occupied
    PCA9685(dev, address=0x40).set_duty_cycle(1, 0.75)   # reverse rail at 75%
    res = self_test.run_occupancy_test(_occupancy_cfg(), "B1", device=dev)
    assert res["track"]["volts"] < 0 and abs(res["track"]["volts"] + 9.0) < 0.05
    assert res["driven"] is True
    assert res["source"] == "current" and res["occupied"] is True


def test_occupancy_test_below_the_deadband_still_counts_as_idle():
    dev = _occupancy_device()
    dev.set_input_bit("cs1", 3, False)
    dev.set_ina3221_current(0x41, 2, 0.120)              # current says occupied
    PCA9685(dev, address=0x40).set_duty_cycle(0, 0.01)   # 0.12 V, under deadband
    res = self_test.run_occupancy_test(_occupancy_cfg(), "B1", device=dev)
    assert res["driven"] is False
    assert res["source"] == "digital" and res["occupied"] is False


def test_occupancy_test_current_only_block_uses_current_when_idle():
    cfg = _occupancy_cfg()
    cfg.block_occupancy = {}                             # no digital detector wired
    dev = _occupancy_device()
    dev.set_ina3221_current(0x41, 2, 0.120)
    res = self_test.run_occupancy_test(cfg, "B1", device=dev)
    assert res["digital"]["bound"] is False
    assert res["source"] == "current" and res["occupied"] is True


def test_occupancy_test_unbound_block_reports_no_source():
    res = self_test.run_occupancy_test(_occupancy_cfg(), "B9", device=_occupancy_device())
    assert res["ok"] is True
    assert res["source"] == "none" and res["occupied"] is False
    assert res["digital"]["bound"] is False and res["current"]["bound"] is False


def test_occupancy_test_survives_an_unreadable_detector():
    cfg = _occupancy_cfg()
    dev = FakeCh347Device(present_addresses={0x40})      # INA3221 at 0x41 absent
    dev.set_input_bit("cs1", 3, True)
    res = self_test.run_occupancy_test(cfg, "B1", device=dev)
    assert res["ok"] is True
    assert "error" in res["current"]                     # the failure is reported
    assert res["source"] == "digital" and res["occupied"] is True


def test_occupancy_test_reports_a_busy_controller():
    def _boom():
        raise RuntimeError("busy")

    res = self_test.run_occupancy_test(_occupancy_cfg(), "B1", open_device=_boom)
    assert res["ok"] is False and "busy" in res["error"]


# --- detector polarity (active-low comparators) ------------------------------
# An LM339 with no inverter stage pulls its output LOW when a train draws
# current, so the raw '165 bit reads 0 occupied / 1 clear. The real input
# interface normalizes that below read_block_currents(), so nothing above the
# interface knows a polarity exists.
def _polarity_wired(active_low: bool):
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("in1", "hc165", spi_select="cs1", active_low=active_low),
    ]
    inp, _blk, _sw = build_real_interfaces(
        dev, specs, block_track={}, block_occupancy={"B1": ("in1", 2)},
        switch_output={},
    )
    return dev, inp


def test_active_high_detector_reads_bit_straight_through():
    dev, inp = _polarity_wired(active_low=False)
    assert inp.read_block_currents()["B1"] is False   # idle line low
    dev.set_input_bit("cs1", 2, True)
    assert inp.read_block_currents()["B1"] is True


def test_active_low_detector_is_inverted_inside_the_interface():
    dev, inp = _polarity_wired(active_low=True)
    # Raw bit 0 (comparator pulled low) means a train IS drawing current.
    assert inp.read_block_currents()["B1"] is True
    # Raw bit 1 (line idles high through its pull-up) means clear.
    dev.set_input_bit("cs1", 2, True)
    assert inp.read_block_currents()["B1"] is False


def test_polarity_is_per_chip_not_global():
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("normal", "hc165", spi_select="cs1"),
        ChipConfig("inverted", "hc165", spi_select="cs2", active_low=True),
    ]
    inp, _blk, _sw = build_real_interfaces(
        dev, specs, block_track={},
        block_occupancy={"B1": ("normal", 0), "B2": ("inverted", 0)},
        switch_output={},
    )
    # Both lines sit at 0: the active-high block is clear, the active-low one occupied.
    reads = inp.read_block_currents()
    assert reads["B1"] is False
    assert reads["B2"] is True
