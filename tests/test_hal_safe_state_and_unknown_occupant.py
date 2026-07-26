"""HAL safe-state (M1) + unknown-occupant attribution (M2).

design_new.md "Power":
- enter_safe_state() (2235): the runtime drives every block track voltage to 0
  and de-energizes every switch motor when it stops driving outputs (simulation
  stop or process shutdown).
- Attribution (2243): a confirmed block entry that matches no modeled train is
  committed to the reserved UNKNOWN_TRAIN_ID sentinel (not a wrong train, not
  dropped) and cleared when its current drops for the off-window.

Both paths are real-hardware-only — the simulated detector builds current from
live spans and the simulated outputs have no physical effect — so they are
exercised here as unit contracts.
"""

from block import Block
from block_occupancy import BlockOccupancyDetector, SimulatedOccupancyDriver
from block_power import BlockPowerController, SimulatedTrackDriver
from constants import UNKNOWN_TRAIN_ID
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
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
    sim = SimulationController(network, PathDispatcher(network))
    sim.power_controller.output_interface.set_voltage("B1", 9.0)
    sim.switch_output_interface.set_throw("S1", 1)
    sim.enter_safe_state()
    assert sim.power_controller.output_interface.get_actual_voltage("B1") == 0.0
    assert sim.switch_output_interface.get_last_throw("S1") is None


# ----------------------------------------------------------- M2 unknown occupant

def _detector():
    network = RailwayNetwork()
    network.blocks["BLK_X"] = Block(block_id="BLK_X", length_cm=100.0)
    controllers = {}
    detector = BlockOccupancyDetector(
        network=network,
        input_interface=SimulatedOccupancyDriver(network, controllers),
        controllers=controllers,
    )
    return network, detector


def test_unattributable_entry_attributes_to_unknown_without_writing_block_state():
    network, detector = _detector()
    block = network.blocks["BLK_X"]
    # No reserving train, no covering span -> unmodeled occupant. `attribute()`
    # is pure: it routes the block to the sentinel and writes nothing.
    entered_by_train, _ = detector.attribute({"BLK_X"}, set())
    assert entered_by_train == {UNKNOWN_TRAIN_ID: {"BLK_X"}}
    assert block.occupied_by is None


def test_unknown_vacate_attributes_to_unknown_without_writing_block_state():
    network, detector = _detector()
    block = network.blocks["BLK_X"]
    block.occupied_by = UNKNOWN_TRAIN_ID
    _, vacated_by_train = detector.attribute(set(), {"BLK_X"})
    assert vacated_by_train == {UNKNOWN_TRAIN_ID: {"BLK_X"}}
    assert block.occupied_by == UNKNOWN_TRAIN_ID


# design_new.md "Attribution": the unmodeled occupant has no per-train Phase-3
# transaction, so the Step-0 preamble commits it through the single writer
# `BlockManager.occupy_blocks(...)`.
def _sim_with_block():
    network = RailwayNetwork()
    network.blocks["BLK_X"] = Block(block_id="BLK_X", length_cm=100.0)
    sim = SimulationController(network, PathDispatcher(network))
    return network, sim


def test_preamble_commits_unknown_occupant_through_block_manager():
    network, sim = _sim_with_block()
    block = network.blocks["BLK_X"]
    sim._commit_unknown_occupancy({"BLK_X"}, set())
    assert block.occupied_by == UNKNOWN_TRAIN_ID
    assert sim.failed_runtime is False


def test_preamble_unknown_occupant_on_held_block_is_a_fatal_conflict():
    network, sim = _sim_with_block()
    block = network.blocks["BLK_X"]
    block.occupied_by = "T1"
    # Real hardware reads current on a block software believes T1 holds. The
    # commit must raise the fatal physical-entry conflict, not silently
    # overwrite T1's ownership.
    sim._commit_unknown_occupancy({"BLK_X"}, set())
    assert sim.failed_runtime is True
    assert block.occupied_by == "T1"


def test_preamble_clears_the_unknown_occupant_on_vacate():
    network, sim = _sim_with_block()
    block = network.blocks["BLK_X"]
    block.occupied_by = UNKNOWN_TRAIN_ID
    sim._commit_unknown_occupancy(set(), {"BLK_X"})
    assert block.occupied_by is None
