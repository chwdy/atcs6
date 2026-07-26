"""C7 — Run modes: the sim/real backend injection seam.

design_new.md "Run modes": operation mode runs against one of two hardware
backends — sim mode (the v1 default; wires the simulated interfaces) or real
mode (injects a real implementation of each interface behind the same contract).
The backend is chosen once when the operation session is built and never
switched mid-run; no code above the interfaces branches on the mode — it
manifests only as which interfaces are wired.
"""

from block_occupancy import SimulatedOccupancyDriver
from block_power import SimulatedTrackDriver
from enums import OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from switch_power import SimulatedSwitchDriver


def _sim(**kwargs):
    net = RailwayNetwork()
    return SimulationController(net, PathDispatcher(net), **kwargs)


def test_default_is_sim_mode_with_simulated_interfaces():
    sim = _sim()
    assert sim.operation_mode == OperationMode.SIM
    assert isinstance(sim.input_interface, SimulatedOccupancyDriver)
    assert isinstance(sim.power_controller.output_interface, SimulatedTrackDriver)
    assert isinstance(
        sim.switch_output_interface, SimulatedSwitchDriver
    )


def test_operation_mode_accepts_string_and_enum():
    assert _sim(operation_mode="real").operation_mode == OperationMode.REAL
    assert _sim(operation_mode=OperationMode.REAL).operation_mode == OperationMode.REAL


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
        operation_mode=OperationMode.REAL,
        input_interface=in_iface,
        block_output_interface=blk_out,
        switch_output_interface=sw_out,
    )
    assert sim.input_interface is in_iface
    assert sim.power_controller.output_interface is blk_out
    assert sim.switch_output_interface is sw_out


def test_operation_mode_values():
    assert OperationMode.SIM.value == "sim"
    assert OperationMode.REAL.value == "real"
