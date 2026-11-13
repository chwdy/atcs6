"""Safe state is a de-energize, not a lock.

design_v2.md "Safe state, watchdog and hardware failure", Safe state /
fail-safe and Safe state is a de-energize, not a lock:
`OutputSafety.enter_safe_state()`
latches every '595 coil bit low and zeroes every PWM channel. That is the whole
of the physical safety mechanism — there is no output gate to raise, no
permission to grant, and nothing to reactivate afterwards.

It matters that it is only a de-energize. When safe state also revoked a
permission the drivers checked before every write, a stopped Real Hardware
session halted itself: `stop` revoked, the stopped session's de-energize drain
wrote 0V, the driver refused a write nobody had any physical reason to refuse,
and the runtime reported that refusal as a failed device. The rig was fine. The
tests below pin the behavior that replaces it — a refusal now means exactly one
thing, a block the driver cannot reach.
"""

import time

from block_power import BlockPowerController
from hardware.fake import FakeCh347Device
from hardware.interfaces import build_real_interfaces
from hardware_config import ChipConfig
from network import RailwayNetwork
from operation_environment import OperationMode
from simulation import SimulationController


def drivers(watchdog_timeout_s=None):
    """The real driver stack over a fake device."""
    dev = FakeCh347Device(present_addresses={0x40})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=1),
    ]
    _inp, block_out, switch_out = build_real_interfaces(
        dev,
        specs,
        {"BLK_A": ("pwm1", 0, 1)},
        {},
        {"SW_A": ("sw1", 1, 2, None)},
        watchdog_timeout_s=watchdog_timeout_s,
    )
    return dev, block_out, switch_out


def hardware_session(watchdog_timeout_s=None):
    """A Real Hardware runtime over that stack."""
    dev, block_out, switch_out = drivers(watchdog_timeout_s)
    sim = SimulationController(
        RailwayNetwork(),
        None,
        operation_mode=OperationMode.REAL_HARDWARE,
        block_output_interface=block_out,
        switch_output_interface=switch_out,
        environment_active=True,
    )
    return dev, sim


# --- what safe state does ---------------------------------------------------


def test_safe_state_drops_the_coils_and_the_track():
    dev, block_out, switch_out = drivers()
    block_out.set_voltage("BLK_A", 9.0)
    switch_out.set_throw("SW_A", 1)

    block_out.enter_safe_state()

    assert dev.duty_cycle(0x40, 0) == 0.0
    assert dev.latched_bit("cs0", 1) is False
    assert dev.latched_bit("cs0", 2) is False
    assert block_out.get_actual_voltage("BLK_A") == 0.0


def test_either_driver_safes_both_outputs():
    """One object owns the sequence, so track and points can never disagree
    about whether the rig is driving."""
    dev, block_out, switch_out = drivers()
    block_out.set_voltage("BLK_A", 9.0)

    switch_out.enter_safe_state()          # the switch side, not the track side

    assert dev.duty_cycle(0x40, 0) == 0.0


# --- stop, then start -------------------------------------------------------


def test_the_rail_drives_again_after_a_safe_state():
    """The regression this design removes: a stop must not cost a reactivation."""
    dev, sim = hardware_session()
    sim.power_controller.set_block_voltage("BLK_A", 9.0)

    sim.enter_safe_state()                 # what POST control {"action": "stop"} does

    assert sim.power_controller.set_block_voltage("BLK_A", 9.0) is True
    assert dev.duty_cycle(0x40, 0) > 0.0
    assert sim.hardware_failure is False
    assert sim.environment_active is True


def test_a_stopped_sessions_de_energize_drain_is_not_a_failure():
    """The exact write that halted the rig: 0V to a block, outputs already safed."""
    dev, sim = hardware_session()
    sim.power_controller.set_block_voltage("BLK_A", 9.0)
    sim.enter_safe_state()

    assert sim.power_controller.set_block_voltage("BLK_A", 0.0) is True

    assert sim.hardware_failure is False
    assert sim.runtime_halted is False


def test_stop_start_survives_repeatedly():
    _dev, sim = hardware_session()

    for _ in range(5):
        sim.enter_safe_state()
        assert sim.power_controller.set_block_voltage("BLK_A", 9.0) is True

    assert sim.hardware_failure is False


# --- what a refusal still means --------------------------------------------


def test_an_unreachable_block_is_still_fatal():
    """A refusal has one cause left: the driver cannot reach that block
    (design_v2.md "Block and switch power controllers", A refused write is
    fatal to the session, and never recorded as voltage)."""
    _dev, sim = hardware_session()

    assert sim.power_controller.set_block_voltage("BLK_NOT_BOUND", 6.0) is False

    assert sim.hardware_failure is True
    # The unreachable block is named by the emitted event, not a retained field.
    hw_event = sim.notification_center.latest_by_operation("enter_hardware_failure")
    assert hw_event is not None
    assert hw_event.payload["resource_id"] == "BLK_NOT_BOUND"


def test_a_device_error_is_still_fatal():
    dev, sim = hardware_session()
    dev.set_fault("i2c_write_error")

    assert sim.power_controller.set_block_voltage("BLK_A", 9.0) is False

    assert sim.hardware_failure is True


# --- loss of control --------------------------------------------------------


def test_a_stalled_control_loop_drops_the_outputs():
    """The in-process watchdog is what covers a control loop that stops
    refreshing: it cannot survive the process dying, but it does cover a tick
    that wedges while a train is under power."""
    dev, block_out, _switch_out = drivers(watchdog_timeout_s=0.3)
    block_out.set_voltage("BLK_A", 9.0)
    assert dev.duty_cycle(0x40, 0) > 0.0

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and dev.duty_cycle(0x40, 0) != 0.0:
        time.sleep(0.01)

    assert dev.duty_cycle(0x40, 0) == 0.0


def test_the_control_loop_recovers_after_a_watchdog_drop():
    """Dropping the outputs is the whole response. A loop that comes back
    drives the rail again on its next write rather than needing a new session:
    the watchdog protects the layout, it does not end the session."""
    dev, block_out, _switch_out = drivers(watchdog_timeout_s=0.3)
    block_out.set_voltage("BLK_A", 9.0)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and dev.duty_cycle(0x40, 0) != 0.0:
        time.sleep(0.01)

    assert block_out.set_voltage("BLK_A", 9.0) is True
    assert dev.duty_cycle(0x40, 0) > 0.0


# --- the simulated side is untouched ---------------------------------------


def test_a_simulated_session_stops_and_starts_the_same_way():
    network = RailwayNetwork()
    sim = SimulationController(network, None)
    pc: BlockPowerController = sim.power_controller

    pc.set_block_voltage("BLK_A", 9.0)
    sim.enter_safe_state()
    assert pc.get_block_voltage("BLK_A") == 0.0

    assert pc.set_block_voltage("BLK_A", 9.0) is True
    assert sim.hardware_failure is False


# --- the runtime-thread wrapper ---------------------------------------------
#
# design_v2.md "Safe state, watchdog and hardware failure", Reconciling the
# recorded values is a separate, later step: `enter_safe_state()` is the
# sequence and touches no recorded model field, so the watchdog daemon may run
# it off-thread. `SimulationController.enter_output_safe_state()` is the
# runtime-thread entry that wraps it — it advances the safe-state generation
# and reconciles the recorded voltages afterwards.


def _recorded_block(sim):
    """The recorded model row for BLK_A, which the bare test network lacks."""
    from block import Block

    block = Block(block_id="BLK_A", name="A")
    sim.network.blocks["BLK_A"] = block
    return block


def test_the_sequence_drops_points_before_it_zeroes_track():
    """Points first, then block voltages — the order the doc fixes."""
    calls = []

    class _Recorder:
        def __init__(self, name):
            self.name = name

        def de_energize_device_outputs(self):
            calls.append(self.name)

        def zero_device_outputs(self):
            calls.append(self.name)

        def reconcile_recorded_voltages(self):
            pass

    _dev, sim = hardware_session()
    sim.switch_power_controller = _Recorder("switch")
    sim.power_controller = _Recorder("block")

    sim.enter_safe_state()

    assert calls == ["switch", "block"]


def test_the_sequence_records_nothing_so_it_is_thread_safe():
    """The device write must not touch the recorded (applied-output) voltage:
    that is what lets the `OutputWatchdog` daemon run the same sequence
    off-thread."""
    _dev, sim = hardware_session()
    sim.power_controller.set_block_voltage("BLK_A", 9.0)

    sim.enter_safe_state()

    assert sim.power_controller.get_recorded_voltage("BLK_A") == 9.0, (
        "the sequence itself must leave the recorded voltage alone"
    )


def test_the_runtime_wrapper_reconciles_the_recorded_voltage():
    _dev, sim = hardware_session()
    sim.power_controller.set_block_voltage("BLK_A", 9.0)

    sim.enter_output_safe_state()

    assert sim.power_controller.get_recorded_voltage("BLK_A") == 0.0


def test_the_runtime_wrapper_advances_the_safe_state_generation():
    """An open calibration pass compares this before every command and every
    sample, so a stop abandons the pass instead of measuring across the gap."""
    _dev, sim = hardware_session()
    before = sim.safe_state_generation

    sim.enter_output_safe_state()
    assert sim.safe_state_generation == before + 1

    sim.enter_output_safe_state()
    assert sim.safe_state_generation == before + 2


def test_the_bare_sequence_does_not_advance_the_generation():
    """The watchdog calls the sequence directly; the runtime advances the
    generation when it later consumes the trip record, not off-thread."""
    _dev, sim = hardware_session()
    before = sim.safe_state_generation

    sim.enter_safe_state()

    assert sim.safe_state_generation == before
