"""A block the output driver cannot reach must not read as energized.

`TrackDriver.set_voltage` (hardware/interfaces.py) returns False for a block it
cannot drive — one with no `block_track` binding, or a binding naming a chip the
session did not build. Nothing above it looked at that return value, so the
runtime commanded 10V onto a dead rail, wrote `Block.current_voltage = 10`, and
the locomotive never moved. From the layout that is indistinguishable from a
software fault: the app shows a train running, with the block energized, on
track carrying no volts.

`BlockPowerController.set_block_voltage` now records only what actually landed,
and reports the refusal once per block.
"""

from block_power import BlockPowerController
from enums import OperationMode
from tests.test_unidentified_trains import build_sim


class RefusingTrackDriver:
    """Stands in for a driver with no binding for one block."""

    def __init__(self, unreachable):
        self.unreachable = set(unreachable)
        self.written = {}

    def set_voltage(self, block_id, voltage):
        if block_id in self.unreachable:
            return False
        self.written[block_id] = voltage
        return True

    def get_actual_voltage(self, block_id):
        return self.written.get(block_id, 0.0)

    def get_all_voltages(self):
        return dict(self.written)

    def enter_safe_state(self):
        self.written = {k: 0.0 for k in self.written}


def power_controller(unreachable):
    network, sim, _detector = build_sim()
    pc = BlockPowerController(
        network=network, output_interface=RefusingTrackDriver(unreachable)
    )
    pc.wire_dependencies(
        notification_center=sim.notification_center,
        controllers=sim.controllers,
        enter_hardware_failure=sim.enter_hardware_failure,
    )
    return network, sim, pc


def test_a_refused_write_never_claims_the_block_is_energized():
    network, sim, pc = power_controller({"BLK_1"})

    assert pc.set_block_voltage("BLK_1", 10.0) is False
    assert pc.get_recorded_voltage("BLK_1") == 0.0
    # A hardware failure latches its own flag, not the fatal-conflict one; both
    # sit on the one halt gate (design_new.md "Power", Hardware I/O failure).
    assert sim.hardware_failure is True
    assert sim.failed_runtime is False
    assert sim.runtime_halted is True
    # The failing block is named by the emitted event; the halt retains no payload.
    hw_event = sim.notification_center.latest_by_operation("enter_hardware_failure")
    assert hw_event is not None
    assert hw_event.payload["resource_id"] == "BLK_1"


def test_a_hardware_failure_stays_latched_until_an_activation_succeeds():
    """The safe state revoked output permission and only a successful
    activation grants it again; the conflict clearer does not touch the latch
    (design_new.md "Power", Recovery is reactivation)."""
    import pytest

    _network, sim, pc = power_controller({"BLK_1"})
    # A hardware I/O failure only arises in a hardware-backed environment.
    sim.operation_mode = OperationMode.VIRTUAL_HARDWARE
    pc.set_block_voltage("BLK_1", 10.0)

    assert sim.environment_active is False
    assert sim.hardware_failure is True
    assert not hasattr(sim, "clear_hardware_failure")
    with pytest.raises(ValueError, match="not halted"):
        sim.clear_failed_runtime()
    assert sim.hardware_failure is True


def test_a_successful_hardware_activation_clears_the_latch():
    _network, sim, pc = power_controller({"BLK_1"})
    sim.operation_mode = OperationMode.VIRTUAL_HARDWARE
    pc.set_block_voltage("BLK_1", 10.0)

    sim.environment_active = True  # the activation bound new interfaces
    sim.finish_activation()

    assert sim.hardware_failure is False
    assert sim.runtime_halted is False
    cleared = sim.notification_center.latest_by_operation("finish_activation")
    assert cleared is not None


def test_activating_simulation_leaves_the_latch_set():
    """Running Simulation proves nothing about the rig."""
    _network, sim, pc = power_controller({"BLK_1"})
    sim.operation_mode = OperationMode.VIRTUAL_HARDWARE
    pc.set_block_voltage("BLK_1", 10.0)

    sim.operation_mode = OperationMode.SIMULATION
    sim.environment_active = True
    sim.finish_activation()

    assert sim.hardware_failure is True


def test_the_halt_retains_no_payload():
    """The halt keeps only its bool latch; its diagnostic lives in the event
    stream (design_new.md "Power", Hardware I/O failure; "Error events")."""
    _network, sim, pc = power_controller({"BLK_1"})
    sim.operation_mode = OperationMode.VIRTUAL_HARDWARE

    assert pc.set_block_voltage("BLK_1", 10.0) is False

    assert not hasattr(sim, "hardware_failure_payload")
    assert not hasattr(sim, "hardware_failure_at")
    assert not hasattr(sim, "hardware_failure_activation_generation")
    assert sim.hardware_failure is True
    hw_event = sim.notification_center.latest_by_operation("enter_hardware_failure")
    assert hw_event is not None
    assert hw_event.kind == "error"
    assert hw_event.payload["resource_id"] == "BLK_1"


def test_a_landed_write_still_records_its_voltage():
    network, sim, pc = power_controller({"BLK_1"})

    assert pc.set_block_voltage("BLK_0", 10.0) is True
    assert pc.get_recorded_voltage("BLK_0") == 10.0


def test_a_refused_write_is_reported_once_not_every_tick():
    """A train sits on a block for thousands of ticks, and the fault is a static
    wiring one — the stream has to stay readable."""
    network, sim, pc = power_controller({"BLK_1"})

    for _ in range(50):
        pc.set_block_voltage("BLK_1", 10.0)

    notes = [
        n for n in sim.notification_center.peek_recent(200)
        if n.error is not None
        and n.error.origin == "block_track_output_unreachable"
    ]
    assert len(notes) == 1
    assert notes[0].payload["block_id"] == "BLK_1"
    assert "no track binding" in notes[0].message
    # A refused write is a recoverable output fault, not a fatal session entry:
    # `error` is reserved for `enter_failed_runtime` / `enter_hardware_failure`
    # (design_new.md "Error events"), so this reports as `warning`.
    assert notes[0].kind == "warning"


def test_the_halt_gate_refuses_ticks_while_a_hardware_failure_is_latched():
    """`update()` refuses all further advancement while either fatal latch is
    set, on the one gate (design_new.md "Power", Hardware I/O failure)."""
    _network, sim, pc = power_controller({"BLK_1"})
    pc.set_block_voltage("BLK_1", 10.0)
    assert sim.runtime_halted is True

    before = sim.simulation_time
    before_operating = sim.operating_clock()
    sim.update(1.0)

    # A halted session is stopped, so neither clock advances and no train or
    # stopped-observation pass runs behind the gate (design_new.md step `ee19`).
    assert sim.simulation_time == before
    assert sim.operating_clock() == before_operating


def test_each_unreachable_block_gets_its_own_report():
    network, sim, pc = power_controller({"BLK_0", "BLK_1"})

    pc.set_block_voltage("BLK_0", 8.0)
    pc.set_block_voltage("BLK_1", 8.0)
    pc.set_block_voltage("BLK_0", 9.0)

    notes = [
        n for n in sim.notification_center.peek_recent(200)
        if n.error is not None
        and n.error.origin == "block_track_output_unreachable"
    ]
    assert sorted(n.payload["block_id"] for n in notes) == ["BLK_0", "BLK_1"]
