"""Who writes the handle, and what a bench throw records.

design_new.md Module boundaries rule 5, One neutral helper: the neutral notch is
written only by `set_manual_mode_type()` entering `power_percent`,
`complete_power_off()`, and `eb.release()`. Power-on and the ghost freeze write
no handle. design_new.md `SwitchManager.record_bench_alignment(...)`: a bench
throw passes `manual_override = True`, so a `manual` switch's alignment is
recorded like any other.
"""

from enums import HandlePosition
from simulation import SimulationController
from switch import Switch
from switch_manager import SwitchManager
from tests.test_unidentified_adoption import adopt, an_unidentified_on
from tests.test_unidentified_trains import build_sim


def _off_train():
    _network, sim, detector = build_sim()
    adopt(sim, an_unidentified_on(sim, detector), "T1")
    controller = sim.controllers["T1"]
    assert controller.powered is False
    return controller


def test_power_on_leaves_an_eb_latched_while_off_at_eb():
    controller = _off_train()
    assert controller.eb.activate().success
    assert controller.ops.handle_position == HandlePosition.EB

    assert controller.commands.power_on_cmd().success

    assert controller.ops.handle_position == HandlePosition.EB


def test_eb_release_returns_the_handle_to_neutral():
    controller = _off_train()
    assert controller.commands.power_on_cmd().success
    assert controller.eb.activate().success

    assert controller.eb.release().success

    assert controller.ops.handle_position == HandlePosition.N


def test_the_ghost_freeze_writes_only_the_speed():
    controller = _off_train()
    controller.ops.handle_position = HandlePosition.EB
    controller.physics_sync.current_speed_kmh = 12.0

    SimulationController._freeze_lost_train_controller(controller)

    assert controller.physics_sync.current_speed_kmh == 0.0
    assert controller.ops.handle_position == HandlePosition.EB


def test_a_bench_throw_records_a_manual_switch():
    switch = Switch(switch_id="SW_1", x=0, y=0)
    switch.manual = True
    manager = SwitchManager(switches={"SW_1": switch}, network=None)

    ok, _message = manager.record_bench_alignment("SW_1", 1)

    assert ok is True
    assert switch.position == 1
