"""Real hardware driver package — chips, interface adapters, and self-test, all
exercised against the in-memory `FakeCh347Device` so no USB hardware is needed
(design_new.md "Power", Hardware interface boundary).

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_drivers.py -v
"""

import time
from types import SimpleNamespace

import pytest

from block_occupancy import BlockOccupancyDetector
from constants import OCCUPANCY_ON_THRESHOLD
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
    DriveZone,
    OccupancyDriver,
    SwitchDriver,
    TrackDriver,
    build_real_interfaces,
    deciding_source,
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


def test_virtual_device_can_inject_transport_and_stuck_output_faults():
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    chip.configure()
    chip.set_duty_cycle(0, 0.25)

    dev.set_fault("stuck_i2c_output")
    chip.set_duty_cycle(0, 0.75)
    assert dev.duty_cycle(0x40, 0) < 0.3

    dev.set_fault("i2c_write_error")
    with pytest.raises(OSError, match="i2c_write_error"):
        chip.set_duty_cycle(0, 0.5)

    dev.clear_faults()
    chip.set_duty_cycle(0, 0.5)
    assert 0.49 < dev.duty_cycle(0x40, 0) < 0.51


def test_virtual_device_refuses_the_bus_after_close_like_the_real_one():
    """`Ch347Device` refuses every transaction once closed. The fake must too,
    or Virtual Hardware reports a write through a closed handle as landed."""
    dev = FakeCh347Device(present_addresses={0x40})
    dev.open()
    dev.close()

    with pytest.raises(RuntimeError, match="i2c_write: the CH347 is not open"):
        dev.i2c_write(0x40, b"\x00")
    with pytest.raises(RuntimeError, match="latch_out: the CH347 is not open"):
        dev.latch_out(b"\x00", "cs0")
    with pytest.raises(RuntimeError, match="load_in: the CH347 is not open"):
        dev.load_in(1, "cs1")

    dev.open()
    assert dev.i2c_write(0x40, b"\x00") is True


def test_virtual_transport_fault_enters_the_runtime_hardware_failure_path():
    from tests.test_unidentified_trains import build_sim

    network, sim, _detector = build_sim()
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm", "pca9685", i2c_address=0x40),
        ChipConfig("out", "hc595", spi_select="cs0"),
    ]
    _inp, track, _switch = build_real_interfaces(
        dev,
        specs,
        block_track={"BLK_0": ("pwm", 0, 1)},
        block_occupancy={},
        switch_output={},
    )
    sim.power_controller.output_interface = track
    dev.set_fault("i2c_write_error")

    assert sim.power_controller.set_block_voltage("BLK_0", 8.0) is False
    assert sim.hardware_failure is True
    # The failure detail rides the event stream, not a retained field.
    hw_event = sim.notification_center.latest_by_operation("enter_hardware_failure")
    assert hw_event is not None
    assert hw_event.payload["resource_id"] == "BLK_0"
    assert dev.duty_cycle(0x40, 0) == 0.0


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


def test_block_voltage_readback_reads_bus_voltage_not_commanded():
    """design_new.md "Power", the block-voltage read surface: the Real/Virtual
    readback reads the block's bound current-sense channel bus voltage — the
    actual rail voltage — never the commanded value. `get_actual_voltage` stays
    the commanded value the occupancy drive-zone reads."""
    dev = FakeCh347Device(present_addresses={0x40, 0x41})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("ina1", "ina3221", i2c_address=0x41),
    ]
    _inp, blk, _sw = build_real_interfaces(
        dev, specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={},
        switch_output={},
        block_occupancy_current={"B1": ("ina1", 0, 0.0, 0.01, 0.01)},
    )
    blk.set_voltage("B1", 12.0)                     # commanded 12 V
    dev.set_ina3221_bus_voltage(0x41, 1, 7.5)       # channel = port 0 + 1
    assert blk.get_actual_voltage("B1") == 12.0     # commanded, unchanged
    assert abs(blk.get_readback_voltage("B1") - 7.5) < 0.1  # bus readback, not commanded
    # A block with no current-sense binding has no measurement facility.
    assert blk.get_readback_voltage("NOPE") is None


def test_reversed_polarity_swaps_the_two_rails():
    """The rail binding's `reversed` flag trades the block's two channels: a positive
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


def test_a_re_picked_coil_port_lands_on_the_next_throw(wired, monkeypatch):
    """design_new.md "Hardware setup", A binding is read per write: the coil
    ports are looked up per throw, not captured when the session was built.

    An operator wiring a rig up re-picks these ports precisely because the first
    guess drove the wrong pin. Captured at build time, the correction would
    change nothing an operator can see until the environment is activated again,
    so the dropdown reads as a control that does nothing and they go rewire the
    layout instead."""
    dev, _inp, _blk, sw = wired
    live = {"SW_A": ("sw1", 5, 6, 4)}
    sw._binding_of = live.get

    sw.set_throw("SW_A", 1)
    assert sw._chips["sw1"].is_high(6) is False       # pulsed and released
    seen = []
    monkeypatch.setattr(
        hw_interfaces,
        "time",
        _SleepSpy(lambda _s: seen.append(
            {bit: dev.latched_bit("cs0", bit) for bit in (6, 9)}
        )),
    )

    # The operator moves the diverge coil from bit 6 to bit 9.
    live["SW_A"] = ("sw1", 5, 9, 4)
    assert sw.set_throw("SW_A", 1) is True

    assert seen == [{6: False, 9: True}]


def test_a_cleared_binding_stops_driving_rather_than_falling_back(wired):
    """The live lookup answers for every switch, including one the operator has
    unbound: a switch the setup no longer wires must stop driving, not keep the
    ports this session happened to start with."""
    _dev, _inp, _blk, sw = wired
    assert sw.set_throw("SW_A", 1) is True            # the built-in map works

    sw._binding_of = {}.get
    assert sw.set_throw("SW_A", 1) is False


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
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015, 0.015)},  # CH1, 15 mA threshold
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


def test_driven_threshold_judges_only_the_driven_zone():
    # design_new.md "Occupancy source follows drive state": a channel's driven
    # threshold judges it while the block is driven, and `threshold_ma` judges
    # it in the lighting zone. 15 mA sits between the two (10 mA lighting,
    # 25 mA driven) — the stray current a coupled neighbour channel reads.
    dev = FakeCh347Device(present_addresses={0x40, 0x41})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("cur1", "ina3221", i2c_address=0x41),
    ]
    inp, blk, _sw = build_real_interfaces(
        dev, specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={},
        switch_output={},
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.010, 0.025)},
        idle_deadband_v=0.25,
        current_sense_min_drive_v=4.0,
    )
    dev.set_ina3221_current(0x41, 1, 0.015)
    blk.set_voltage("B1", 8.0)                   # driven: judged against 25 mA
    assert inp.read_block_currents()["B1"] is False
    detail = inp.last_read_detail()["B1"]
    assert detail["threshold_ma"] == pytest.approx(25.0)
    dev.set_ina3221_current(0x41, 1, 0.030)
    assert inp.read_block_currents()["B1"] is True
    dev.set_ina3221_current(0x41, 1, 0.015)
    blk.set_voltage("B1", 2.0)                   # lighting: judged against 10 mA
    assert inp.read_block_currents()["B1"] is True
    assert inp.last_read_detail()["B1"]["threshold_ma"] == pytest.approx(10.0)


def _lighting_zone_block(hold_reads=3):
    """A block bound to both sources, wired so 2 V lands in the lighting zone.

    Returns (device, occupancy input, track output) with B1 already commanded to
    2 V — between the 0.25 V idle deadband and the 4 V current-sense floor.
    """
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
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015, 0.015)},  # CH1, 15 mA
        idle_deadband_v=0.25,
        current_sense_min_drive_v=4.0,
        lighting_clear_hold_reads=hold_reads,
    )
    blk.set_voltage("B1", 2.0)
    return dev, inp, blk


def _establish(dev, inp):
    """Read current on B1 for the `OCCUPANCY_ON_THRESHOLD` reads in a row that
    establish its presence — the only presence the lighting hold keeps."""
    dev.set_ina3221_current(0x41, 1, 0.030)      # 30 mA > 15 mA
    for _ in range(OCCUPANCY_ON_THRESHOLD):
        assert inp.read_block_currents()["B1"] is True


def test_lighting_zone_reports_the_current_channel():
    # Between the deadband and the current-sense floor the current channel is
    # still the source that decides, and it reports current on one read.
    dev, inp, _blk = _lighting_zone_block()
    dev.set_ina3221_current(0x41, 1, 0.030)      # 30 mA > 15 mA
    assert inp.read_block_currents()["B1"] is True


def test_lighting_zone_does_not_hold_a_single_reading():
    # A presence read once is not established, so the clear read after it is
    # reported clear rather than held.
    dev, inp, _blk = _lighting_zone_block(hold_reads=3)
    dev.set_ina3221_current(0x41, 1, 0.030)
    assert inp.read_block_currents()["B1"] is True
    dev.set_ina3221_current(0x41, 1, 0.0)
    assert inp.read_block_currents()["B1"] is False


def test_lighting_zone_noise_spikes_never_confirm_an_empty_block():
    # The 2026-10-24 rig trip. A locomotive starting on the next block put one
    # 12.8 mA reading on an empty block's channel (10 mA trip; 7.2 mA on the
    # read after). The hold reported that 7.2 mA read as current, the detector
    # saw two "present" reads in a row, and the empty block confirmed. Replay
    # the same shape against this fixture's 15 mA trip through the real
    # detector: isolated spikes over the trip, quiet reads under it.
    dev, inp, _blk = _lighting_zone_block()
    detector = BlockOccupancyDetector(
        network=SimpleNamespace(blocks={"B1": None}), input_interface=inp
    )
    for spike_ma, quiet_ma in [(19.2, 10.8), (16.0, 3.6), (22.0, 9.6)] * 10:
        for amps in (spike_ma / 1000.0, quiet_ma / 1000.0):
            dev.set_ina3221_current(0x41, 1, amps)
            entered, _vacated = detector.step()
            assert "B1" not in entered
    assert "B1" not in detector.confirmed_occupied

    # A locomotive that really arrives still confirms on the usual run.
    dev.set_ina3221_current(0x41, 1, 0.030)
    entered = set()
    for _ in range(OCCUPANCY_ON_THRESHOLD):
        entered |= detector.step()[0]
    assert entered == {"B1"}


def test_lighting_zone_ignores_the_digital_detector():
    # THE bug this rule exists for. Lighting voltage is a duty cycle of the full
    # supply, so the '165's comparator asserts on a fraction of reads with
    # nothing on the block at all. It gets no vote here: it must neither confirm
    # an empty block nor keep a clear one held.
    dev, inp, _blk = _lighting_zone_block(hold_reads=3)
    dev.set_input_bit("cs1", 3, True, width=1)   # '165 asserts, current clear
    for _ in range(10):
        assert inp.read_block_currents()["B1"] is False


def test_lighting_zone_holds_through_a_clear_read():
    # A locomotive drawing under threshold at standby reads like an empty block,
    # so one clear read must not vacate it — that is the confirm/vacate loop
    # between occupancy and drive.
    dev, inp, _blk = _lighting_zone_block(hold_reads=3)
    _establish(dev, inp)
    dev.set_ina3221_current(0x41, 1, 0.0)              # goes quiet
    assert inp.read_block_currents()["B1"] is True
    assert inp.read_block_currents()["B1"] is True


def test_lighting_zone_releases_after_sustained_clear():
    # ...but the hold is not a latch: a sustained clear releases the block.
    dev, inp, _blk = _lighting_zone_block(hold_reads=3)
    _establish(dev, inp)
    dev.set_ina3221_current(0x41, 1, 0.0)
    assert inp.read_block_currents()["B1"] is True     # 1st clear
    assert inp.read_block_currents()["B1"] is True     # 2nd
    assert inp.read_block_currents()["B1"] is False    # 3rd -> released
    assert inp.read_block_currents()["B1"] is False    # and stays released


def test_lighting_zone_digital_flicker_cannot_hold_a_block():
    # The regression: a '165 asserting on the rail drive used to reset the clear
    # window on every flicker, so a held block never ran a window to completion
    # and stayed occupied for as long as software kept driving it.
    dev, inp, _blk = _lighting_zone_block(hold_reads=3)
    _establish(dev, inp)
    dev.set_ina3221_current(0x41, 1, 0.0)              # current says clear
    dev.set_input_bit("cs1", 3, True, width=1)         # '165 flickers on
    assert inp.read_block_currents()["B1"] is True     # 1st clear
    dev.set_input_bit("cs1", 3, False, width=1)
    assert inp.read_block_currents()["B1"] is True     # 2nd clear
    dev.set_input_bit("cs1", 3, True, width=1)         # flicker again
    assert inp.read_block_currents()["B1"] is False    # 3rd -> released anyway


def test_lighting_zone_current_flicker_resets_the_clear_window():
    # The deciding source does reset the window: a channel that dips under
    # threshold and back delays a release instead of racing one.
    dev, inp, _blk = _lighting_zone_block(hold_reads=3)
    _establish(dev, inp)
    for _ in range(20):
        dev.set_ina3221_current(0x41, 1, 0.0)
        assert inp.read_block_currents()["B1"] is True     # 1st clear
        assert inp.read_block_currents()["B1"] is True     # 2nd clear
        dev.set_ina3221_current(0x41, 1, 0.030)            # back over threshold
        assert inp.read_block_currents()["B1"] is True
    # Still occupied after 60 reads, because no window ever ran to completion.
    assert inp.read_block_currents()["B1"] is True


def test_lighting_zone_clear_window_resets_on_leaving_the_zone():
    # The window counts an unbroken run inside the zone only. A block driven up
    # out of it was answered by a source trusted to clear on one read, so its
    # part-finished count must not carry back in.
    dev, inp, blk = _lighting_zone_block(hold_reads=3)
    _establish(dev, inp)
    dev.set_ina3221_current(0x41, 1, 0.0)
    assert inp.read_block_currents()["B1"] is True     # 1st clear
    assert inp.read_block_currents()["B1"] is True     # 2nd clear
    blk.set_voltage("B1", 8.0)                         # driven: current decides
    dev.set_ina3221_current(0x41, 1, 0.030)
    assert inp.read_block_currents()["B1"] is True
    blk.set_voltage("B1", 2.0)                         # back to lighting
    dev.set_ina3221_current(0x41, 1, 0.0)
    assert inp.read_block_currents()["B1"] is True     # count restarted at 1
    assert inp.read_block_currents()["B1"] is True
    assert inp.read_block_currents()["B1"] is False


def test_lighting_zone_release_window_restarts_after_a_reconfirm():
    # After a release the count starts over, so a block that re-confirms and
    # goes quiet again waits the full window instead of dropping out on its
    # next silent read.
    dev, inp, _blk = _lighting_zone_block(hold_reads=3)
    _establish(dev, inp)
    dev.set_ina3221_current(0x41, 1, 0.0)
    for _ in range(2):
        assert inp.read_block_currents()["B1"] is True
    assert inp.read_block_currents()["B1"] is False    # released
    _establish(dev, inp)                               # re-confirmed
    dev.set_ina3221_current(0x41, 1, 0.0)
    assert inp.read_block_currents()["B1"] is True     # full window again
    assert inp.read_block_currents()["B1"] is True
    assert inp.read_block_currents()["B1"] is False


def test_lighting_zone_without_a_current_channel_falls_back_to_the_digital_bit():
    # A block bound to no current channel has nothing else to ask, so the '165
    # decides there — under the same clear window.
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("in1", "hc165", spi_select="cs1"),
    ]
    inp, blk, _sw = build_real_interfaces(
        dev, specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={"B1": ("in1", 3)},
        switch_output={},
        block_occupancy_current={},
        idle_deadband_v=0.25,
        current_sense_min_drive_v=4.0,
        lighting_clear_hold_reads=3,
    )
    blk.set_voltage("B1", 2.0)
    dev.set_input_bit("cs1", 3, True, width=1)
    for _ in range(OCCUPANCY_ON_THRESHOLD):            # establish the presence
        assert inp.read_block_currents()["B1"] is True
    dev.set_input_bit("cs1", 3, False, width=1)
    assert inp.read_block_currents()["B1"] is True     # 1st clear
    assert inp.read_block_currents()["B1"] is True     # 2nd
    assert inp.read_block_currents()["B1"] is False    # 3rd -> released


# --- the track driver owns the drive zone ------------------------------------
# design_new.md "Hardware setup", Occupancy source follows drive state: the
# track driver classifies each block's commanded voltage into a zone and the
# occupancy driver asks it, rather than comparing the voltage itself.


@pytest.mark.parametrize("volts, zone", [
    (0.0, DriveZone.IDLE),
    (0.25, DriveZone.IDLE),          # at the deadband is still idle
    (2.0, DriveZone.LIGHTING),
    (-2.0, DriveZone.LIGHTING),      # the sign is direction, not drive strength
    (4.0, DriveZone.DRIVEN),         # at the current-sense floor is driven
    (-9.0, DriveZone.DRIVEN),
])
def test_the_track_driver_reports_the_zone_of_what_it_commanded(volts, zone):
    _dev, _inp, blk = _lighting_zone_block()      # 0.25 V deadband, 4 V floor
    blk.set_voltage("B1", volts)
    assert blk.drive_zone("B1") is zone


def test_a_block_never_written_is_idle():
    _dev, _inp, blk = _lighting_zone_block()
    assert blk.drive_zone("NEVER_WRITTEN") is DriveZone.IDLE


def test_the_safe_state_puts_every_block_in_the_idle_zone():
    _dev, _inp, blk = _lighting_zone_block()
    blk.set_voltage("B1", 9.0)
    assert blk.drive_zone("B1") is DriveZone.DRIVEN
    blk.enter_safe_state()
    assert blk.drive_zone("B1") is DriveZone.IDLE


def test_the_detector_reads_the_zone_the_track_driver_reports():
    # One owner: whatever zone the track driver says, the detector acts on — it
    # does not keep a second copy of the levels to disagree with.
    dev, inp, blk = _lighting_zone_block()
    for volts in (0.0, 2.0, 9.0, -2.0, 0.0):
        blk.set_voltage("B1", volts)
        inp.read_block_currents()
        assert inp.last_read_detail()["B1"]["zone"] == blk.drive_zone("B1").value
        assert inp.read_all_sources()["B1"]["zone"] == blk.drive_zone("B1").value


def test_zero_volts_is_read_through_the_digital_detector():
    # A commanded 0 V is the idle zone: the rails are undriven, so the '165 —
    # not the current channel — decides.
    dev, inp, blk = _lighting_zone_block()
    blk.set_voltage("B1", 0.0)
    dev.set_input_bit("cs1", 3, True, width=1)     # '165 sees a locomotive
    dev.set_ina3221_current(0x41, 1, 0.0)          # no drive, no current
    assert inp.read_block_currents()["B1"] is True
    assert inp.last_read_detail()["B1"]["source"] == "digital"


class _ReconnectableFakeDevice(FakeCh347Device):
    """A fake device that, like the real bridge, accepts a reconnect hook, so a
    test can run the re-initialization a dropped USB link triggers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reconnect_hooks = []

    def add_reconnect_hook(self, hook):
        self.reconnect_hooks.append(hook)


def test_a_reconnect_reinitialization_reports_every_block_idle():
    # A reconnect re-initializes the PWM chips, which zeroes every channel under
    # the track driver. The driver must then report the rails as the idle zone
    # they are really in; if it kept reporting the old drive, the detector would
    # read a dead block through the current channel and see nothing there.
    dev = _ReconnectableFakeDevice(present_addresses={0x40, 0x41})
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
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015, 0.015)},
        idle_deadband_v=0.25,
        current_sense_min_drive_v=4.0,
    )
    blk.set_voltage("B1", 9.0)
    assert blk.drive_zone("B1") is DriveZone.DRIVEN

    (resync,) = dev.reconnect_hooks
    resync()

    assert blk.drive_zone("B1") is DriveZone.IDLE
    dev.set_input_bit("cs1", 3, True, width=1)     # the locomotive is still there
    assert inp.read_block_currents()["B1"] is True
    assert inp.last_read_detail()["B1"]["source"] == "digital"


@pytest.mark.parametrize("zone, has_digital, has_current, source", [
    (DriveZone.IDLE, True, True, "digital"),
    (DriveZone.IDLE, False, True, "current"),     # best effort, no drive
    (DriveZone.IDLE, True, False, "digital"),
    (DriveZone.LIGHTING, True, True, "current"),  # the '165 reads the duty cycle
    (DriveZone.LIGHTING, True, False, "digital"),
    (DriveZone.DRIVEN, True, True, "current"),
    (DriveZone.DRIVEN, True, False, "digital"),
    (DriveZone.DRIVEN, False, False, "none"),
])
def test_deciding_source_precedence(zone, has_digital, has_current, source):
    assert deciding_source(zone, has_digital, has_current) == source


def test_lighting_voltage_default_stays_below_the_current_sense_floor():
    # A seeded profile sets no lighting voltage, so a standing train sits at
    # 0 V in the idle zone, where its digital detector reads it; the readiness
    # bound against the current-sense floor applies only to a set lighting
    # voltage (design_new.md "`current_sense_min_drive_v` must sit above
    # `lighting_voltage`").
    from speed_profile import DEFAULT_SPEED_PROFILE, SpeedProfile, copied_profile

    assert DEFAULT_SPEED_PROFILE.lighting_voltage is None
    assert SpeedProfile(profile_id="t").lighting_voltage is None
    assert copied_profile(DEFAULT_SPEED_PROFILE, profile_id=1, max_speed_kmh=80.0).lighting_voltage is None


def test_current_only_block_reads_ina_regardless_of_drive():
    # A block with only a current binding uses the INA even when idle (best effort:
    # reads ~0 with no drive).
    dev = FakeCh347Device(present_addresses={0x41})
    specs = [ChipConfig("cur1", "ina3221", i2c_address=0x41)]
    inp, _blk, _sw = build_real_interfaces(
        dev, specs, block_track={}, block_occupancy={}, switch_output={},
        block_occupancy_current={"B1": ("cur1", 2, 0.0, 0.015, 0.015)},
    )
    assert inp is not None
    dev.set_ina3221_current(0x41, 3, 0.030)      # port 2 -> CH3
    assert inp.read_block_currents()["B1"] is True


def test_safe_state_zeroes_the_channels_and_leaves_them_drivable():
    """Safe state is a de-energize, not a lock: the operator stops, the rig
    goes dead, and starting again drives it. Nothing has to be reactivated,
    because nothing was taken away but the voltage."""
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0"),
    ]
    _inp, blk, _sw = build_real_interfaces(
        dev,
        specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={},
        switch_output={},
    )
    assert blk.set_voltage("B1", 8.0) is True

    blk.enter_safe_state()

    assert dev.duty_cycle(0x40, 0) == 0.0
    assert blk.get_actual_voltage("B1") == 0.0
    assert blk.set_voltage("B1", 8.0) is True
    assert dev.duty_cycle(0x40, 0) > 0.0


def test_track_output_works_again_after_a_reconnect_resync():
    """A reconnect must not leave the rail dead for the rest of the session.
"""
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0"),
    ]
    _inp, blk, _sw = build_real_interfaces(
        dev,
        specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={},
        switch_output={},
    )
    assert blk.set_voltage("B1", 8.0) is True

    dev.simulate_reconnect()

    # The resync deliberately does not restore voltage: a reopened bridge is a
    # blank bridge, so it lands on zero and the control loop re-commands.
    assert dev.duty_cycle(0x40, 0) == 0.0
    # ...and that re-command has to actually land.
    assert blk.set_voltage("B1", 8.0) is True
    assert dev.duty_cycle(0x40, 0) > 0.0


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
    result = self_test.run_port_test(cfg, "unidentified train", 0, "read_input",
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


def test_commission_learns_switch_bits_from_the_saved_setup():
    """The panel posts chips without bindings, so the saved `switch_output` map
    is what tells the session which shift-out bits drive a coil (design_new.md
    "The commissioning session", Switch bits). Open reports them, and `output`
    refuses to hold one on; a bit no binding names may be held."""
    from hardware.commission import CommissionSession
    from hardware_config import SwitchBinding

    fake = FakeCh347Device()
    cfg = _cfg(ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=3))
    saved = {
        "SW_A": SwitchBinding("sw1", port_main=0, port_diverge=1, port_standby=2),
        # A binding to a chip this session does not build is ignored.
        "SW_B": SwitchBinding("other", port_main=4, port_diverge=5),
    }
    sess = CommissionSession()
    sess.open(cfg, open_device=lambda: fake, saved_switch_output=saved)
    assert sess.switch_ports_by_chip() == {"sw1": [0, 1, 2]}
    with pytest.raises(ValueError):
        sess.set_output("sw1", 1, 1.0)
    sess.set_output("sw1", 3, 1.0)
    assert fake.latched_bit("cs0", 3) is True
    sess.set_output("sw1", 3, 0.0)
    sess.close()


def test_commission_current_read_failure_is_an_error_not_zero():
    """A channel that cannot be read fails the whole read: a missing reading is
    not a clear one (design_new.md "The commissioning session", Every failure
    reaches the operator)."""
    from hardware.commission import CommissionSession

    fake = FakeCh347Device(present_addresses={0x41})
    cfg = _cfg(ChipConfig("cur1", "ina3221", i2c_address=0x41))
    sess = CommissionSession()
    sess.open(cfg, open_device=lambda: fake)
    sess.read_currents("cur1")             # configures the chip
    fake.set_fault("i2c_read_error")
    with pytest.raises(Exception):
        sess.read_currents("cur1")
    fake.clear_faults()
    sess.close()


def test_commission_open_bounds_the_session_timeout():
    """design_new.md "Hardware REST payload schemas": the commissioning `enter`
    body carries `timeout_seconds` with `0 < timeout_seconds <=
    COMMISSION_MAX_SESSION_SECONDS`. A window just under the ceiling opens the
    session; one just over is rejected before the session opens — a too-long
    window is refused, not silently cut."""
    from constants import COMMISSION_MAX_SESSION_SECONDS
    from hardware.commission import CommissionSession

    cfg = _cfg(ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=1))

    # Just under the ceiling: accepted, the session opens.
    ok = CommissionSession()
    ok.open(
        cfg,
        open_device=lambda: FakeCh347Device(),
        timeout_seconds=COMMISSION_MAX_SESSION_SECONDS - 1.0,
    )
    assert ok.status()["open"] is True
    ok.close()

    # Just over the ceiling: rejected, and no device was opened.
    over = CommissionSession()
    with pytest.raises(ValueError):
        over.open(
            cfg,
            open_device=lambda: FakeCh347Device(),
            timeout_seconds=COMMISSION_MAX_SESSION_SECONDS + 1.0,
        )
    assert over.status()["open"] is False


def test_commission_pulse_bounds_the_pulse_width(monkeypatch):
    """design_new.md "Hardware REST payload schemas": the commissioning `pulse`
    "must be no greater than COMMISSION_MAX_PULSE_MS". A width just under the
    ceiling drives the bit; one just over is rejected before any bit is driven."""
    from constants import COMMISSION_MAX_PULSE_MS
    from hardware import commission as commission_mod
    from hardware.commission import CommissionSession

    # The width is validated before the coil is driven, so stub the hold: the
    # accepted case returns at once instead of sleeping out the full width.
    monkeypatch.setattr(commission_mod, "sleep_holding", lambda chip, secs: None)

    cfg = _cfg(ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=3))
    sess = CommissionSession()
    sess.open(cfg, open_device=lambda: FakeCh347Device())

    ceiling_s = COMMISSION_MAX_PULSE_MS / 1000.0
    # Just under the ceiling: accepted (no raise).
    sess.pulse_output("sw1", 1, pulse_s=ceiling_s - 0.001)
    # Just over the ceiling: rejected before any bit is driven.
    with pytest.raises(ValueError):
        sess.pulse_output("sw1", 1, pulse_s=ceiling_s + 0.001)
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
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015, 0.015)},
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


def test_drive_sequence_reports_each_leg_it_drives():
    # The mirror the block-voltage overlay reads: full one way, zero, full the
    # other, zero — reported as each leg starts, not once at the end.
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    seen = []
    self_test._drive_sequence(
        chip, forward=4, reverse=9, hold_s=0.0, on_voltage=seen.append
    )
    assert seen == [12.0, 0.0, -12.0, 0.0]


def test_drive_sequence_reports_zero_when_a_write_raises():
    # A sweep that dies with a rail hot must leave the mirror dead too, or the
    # layout keeps claiming voltage on a block the finally just de-energized.
    dev = FakeCh347Device(present_addresses={0x40})
    chip = PCA9685(dev, address=0x40)
    chip.configure()
    original = chip.set_duty_cycle
    calls = {"n": 0}

    def flaky(channel, duty):
        calls["n"] += 1
        if calls["n"] == 3:
            raise IOError("bridge dropped")
        original(channel, duty)

    chip.set_duty_cycle = flaky
    seen = []
    with pytest.raises(IOError):
        self_test._drive_sequence(
            chip, forward=0, reverse=1, hold_s=0.0, on_voltage=seen.append
        )
    assert seen[-1] == 0.0


def test_track_test_mirrors_the_sweep_as_nominal_voltage(monkeypatch):
    monkeypatch.setattr(self_test, "TRACK_TEST_HOLD_S", 0.0)
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = HardwareConfig(
        controller=ControllerConfig(kind="ch347"),
        chips=[ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
        block_track={"B1": TrackBinding("pwm1", 4, 9)},
    )
    seen = []
    assert self_test.run_track_test(
        cfg, "B1", device=dev, on_voltage=seen.append
    )["ok"] is True
    assert seen == [12.0, 0.0, -12.0, 0.0]


def test_track_test_resolves_block_polarity_like_the_driver(monkeypatch):
    # The sweep sits under the block panel's Track direction control and is the
    # only way to check it: flip a backwards block to Reversed, sweep again, and
    # the loco must now run the right way. So the sweep trades the two channels
    # exactly as `TrackDriver.set_voltage` does — the first leg drives the
    # channel the binding calls `reverse`, and stays nominally positive.
    monkeypatch.setattr(self_test, "TRACK_TEST_HOLD_S", 0.0)
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = HardwareConfig(
        controller=ControllerConfig(kind="ch347"),
        chips=[ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
        block_track={"B1": TrackBinding("pwm1", 4, 9)},
    )
    driven = []
    original = PCA9685.set_duty_cycle

    def spy(self, channel, duty):
        if duty > 0:
            driven.append(channel)
        original(self, channel, duty)

    monkeypatch.setattr(PCA9685, "set_duty_cycle", spy)

    seen = []
    res = self_test.run_track_test(
        cfg, "B1", device=dev, on_voltage=seen.append, polarity_reversed=True
    )
    assert res["ok"] is True
    assert driven == [9, 4]                  # reverse rail first, not forward
    assert seen == [12.0, 0.0, -12.0, 0.0]   # nominal sign, same as the runtime
    assert dev.duty_cycle(0x40, 4) == 0.0 and dev.duty_cycle(0x40, 9) == 0.0


def test_track_test_drives_the_forward_rail_first_on_a_normal_block(monkeypatch):
    # The Normal counterpart of the test above, and the discovery case: a fresh
    # binding is Normal, so a block wired the wrong way round still reveals
    # itself by running the loco backwards on the first leg.
    monkeypatch.setattr(self_test, "TRACK_TEST_HOLD_S", 0.0)
    dev = FakeCh347Device(present_addresses={0x40})
    cfg = HardwareConfig(
        controller=ControllerConfig(kind="ch347"),
        chips=[ChipConfig("pwm1", "pca9685", i2c_address=0x40)],
        block_track={"B1": TrackBinding("pwm1", 4, 9)},
    )
    driven = []
    original = PCA9685.set_duty_cycle

    def spy(self, channel, duty):
        if duty > 0:
            driven.append(channel)
        original(self, channel, duty)

    monkeypatch.setattr(PCA9685, "set_duty_cycle", spy)

    assert self_test.run_track_test(cfg, "B1", device=dev)["ok"] is True
    assert driven == [4, 9]


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


def test_occupancy_test_judges_a_driven_block_against_its_driven_threshold():
    # The Test button mirrors the runtime: 20 mA clears a 15 mA lighting
    # threshold but not a 25 mA driven threshold, so a driven block reads clear.
    cfg = _occupancy_cfg()
    cfg.block_occupancy_current["B1"] = CurrentBinding(
        "cur1", 1, baseline_ma=0.0, threshold_ma=15.0, driven_threshold_ma=25.0
    )
    dev = _occupancy_device()
    dev.set_ina3221_current(0x41, 2, 0.020)
    PCA9685(dev, address=0x40).set_duty_cycle(0, 1.0)    # block driven at full
    res = self_test.run_occupancy_test(cfg, "B1", device=dev)
    assert res["driven"] is True
    assert res["current"]["threshold_ma"] == 25.0
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


def test_occupancy_test_uses_the_configured_current_sense_floor():
    # Both zone thresholds are per-configuration tunables, and the runtime
    # OccupancyDriver is built with the configured values (see the
    # `build_real_interfaces` call site in hardware_config). Resolving them from
    # the module defaults instead would have the bench test and the running
    # session reaching different verdicts about the same block — on exactly the
    # layouts whose operator tuned the setting.
    from hardware_config import DEFAULT_CURRENT_SENSE_MIN_DRIVE_V

    dev = _occupancy_device()
    dev.set_input_bit("cs1", 3, True)                    # detector: occupied
    dev.set_ina3221_current(0x41, 2, 0.001)              # current: near zero
    PCA9685(dev, address=0x40).set_duty_cycle(0, 0.5)    # 6 V on the rails

    res = self_test.run_occupancy_test(_occupancy_cfg(), "B1", device=dev)
    assert res["min_drive_v"] == DEFAULT_CURRENT_SENSE_MIN_DRIVE_V
    assert res["zone"] == "driven" and res["source"] == "current"

    cfg = _occupancy_cfg()
    cfg.current_sense_min_drive_v = 8.0                  # the operator raises it
    res = self_test.run_occupancy_test(cfg, "B1", device=dev)
    assert res["min_drive_v"] == 8.0
    # Same rails, same reads: the raised floor puts them in the lighting zone.
    # The current channel still decides there — what changes is that a clear
    # takes a run of reads, which `held_zone` is what tells the operator.
    assert res["zone"] == "lighting" and res["source"] == "current"
    assert res["held_zone"] is True and res["occupied"] is False


def test_occupancy_test_uses_the_configured_idle_deadband():
    dev = _occupancy_device()
    dev.set_input_bit("cs1", 3, True)
    dev.set_ina3221_current(0x41, 2, 0.001)
    PCA9685(dev, address=0x40).set_duty_cycle(0, 0.05)   # 0.6 V — over the default

    res = self_test.run_occupancy_test(_occupancy_cfg(), "B1", device=dev)
    assert res["zone"] == "lighting"

    cfg = _occupancy_cfg()
    cfg.idle_deadband_v = 1.0                            # the operator widens it
    res = self_test.run_occupancy_test(cfg, "B1", device=dev)
    assert res["deadband_v"] == 1.0
    assert res["zone"] == "idle" and res["source"] == "digital"


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
