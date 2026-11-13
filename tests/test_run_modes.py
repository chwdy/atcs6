"""Operating environments share one backend-injection seam.

design_v2.md "Operating environment and activation": Virtual Hardware and Real
Hardware select complete interface sets behind the same contract.
The backend is chosen once when the operation session is built and never
switched mid-run; no code above the interfaces branches on the mode — it
manifests only as which interfaces are wired.
"""

from block_occupancy import InactiveOccupancyInput
from block_power import SimulatedTrackDriver
from enums import OperationMode
from network import RailwayNetwork
from simulation import SimulationController
from switch_power import SimulatedSwitchDriver


def _sim(**kwargs):
    net = RailwayNetwork()
    return SimulationController(net, **kwargs)


def test_a_fresh_controller_is_virtual_hardware_with_placeholder_interfaces():
    sim = _sim()
    assert sim.operation_mode == OperationMode.VIRTUAL_HARDWARE
    assert sim.environment_active is False
    assert isinstance(sim.input_interface, InactiveOccupancyInput)
    assert isinstance(sim.power_controller.output_interface, SimulatedTrackDriver)
    assert isinstance(
        sim.switch_output_interface, SimulatedSwitchDriver
    )


def test_operation_mode_accepts_string_and_enum():
    assert _sim(operation_mode="real_hardware").operation_mode == OperationMode.REAL_HARDWARE
    assert _sim(operation_mode="virtual_hardware").operation_mode == OperationMode.VIRTUAL_HARDWARE
    assert _sim(operation_mode=OperationMode.REAL_HARDWARE).operation_mode == OperationMode.REAL_HARDWARE


def test_backend_injection_seam_wires_provided_interfaces():
    """A real backend injects its interfaces at session build; the runtime wires
    exactly those, not the simulated defaults."""

    class _StubInput:
        pass

    class _StubBlockOut:
        pass

    class _StubSwitchOut:
        pass

    in_iface, blk_out, sw_out = _StubInput(), _StubBlockOut(), _StubSwitchOut()
    sim = _sim(
        operation_mode=OperationMode.REAL_HARDWARE,
        input_interface=in_iface,
        block_output_interface=blk_out,
        switch_output_interface=sw_out,
    )
    assert sim.input_interface is in_iface
    assert sim.power_controller.output_interface is blk_out
    assert sim.switch_output_interface is sw_out


def test_operation_mode_values():
    assert OperationMode.VIRTUAL_HARDWARE.value == "virtual_hardware"
    assert OperationMode.REAL_HARDWARE.value == "real_hardware"
