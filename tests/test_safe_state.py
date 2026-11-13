"""The safe state (design_v2.md "Safe state, watchdog and hardware failure"):
the runtime drives every block voltage to 0 and de-energizes every point motor
when it stops driving the outputs.
"""

from block_power import BlockPowerController, SimulatedTrackDriver
from network import RailwayNetwork
from simulation import SimulationController
from switch_power import SimulatedSwitchDriver


# --------------------------------------------------------------- M1 safe state

def test_block_output_interface_enter_safe_state_zeroes_voltages():
    iface = SimulatedTrackDriver()
    iface.set_voltage("B1", 5.0)
    iface.set_voltage("B2", 8.0)
    iface.enter_safe_state()
    assert iface.get_actual_voltage("B1") == 0.0
    assert iface.get_actual_voltage("B2") == 0.0
    assert all(v == 0.0 for v in iface.get_all_voltages().values())


def test_switch_output_interface_enter_safe_state_deenergizes():
    iface = SimulatedSwitchDriver()
    iface.set_throw("S1", 1)
    iface.enter_safe_state()
    assert iface.get_last_throw("S1") is None


def test_block_power_controller_delegates_safe_state():
    iface = SimulatedTrackDriver()
    controller = BlockPowerController(output_interface=iface)
    iface.set_voltage("B1", 7.0)
    controller.enter_safe_state()
    assert iface.get_actual_voltage("B1") == 0.0


def test_simulation_controller_enter_safe_state_drives_all_outputs_safe():
    network = RailwayNetwork()
    sim = SimulationController(network)
    sim.power_controller.output_interface.set_voltage("B1", 9.0)
    sim.switch_output_interface.set_throw("S1", 1)
    sim.enter_safe_state()
    assert sim.power_controller.output_interface.get_actual_voltage("B1") == 0.0
    assert sim.switch_output_interface.get_last_throw("S1") is None


# ------------------------------------------------------------- M2 unidentified trains
