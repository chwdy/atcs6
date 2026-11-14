"""Activation throws every switch to the layout (design_v2.md "Operating
environment and activation", Activation throws every switch): an activation
throws every switch with an available motor once to the position the layout
shows, which makes its position known. A switch marked manual is thrown like
any other (design_v2.md "Switches", A switch marked manual).
"""

from enums import OperationMode
from network import RailwayNetwork
from simulation import SimulationController
from switch import Switch, SwitchState
from track import TRACK_EW, TRACK_NW, Tile


def _network_with_switches():
    network = RailwayNetwork()
    switches = {}
    for x, name in ((0, "SW_A"), (2, "SW_M"), (4, "SW_D")):
        tile = Tile(x, 0, track_bits=TRACK_EW | TRACK_NW)
        network.add_track(tile)
        switch = Switch(switch_id=name, x=x, y=0)
        network.add_switch(switch)
        tile.switch_id = name
        switches[name] = switch
    switches["SW_A"].position = SwitchState.DIVERGE
    switches["SW_M"].manual = True
    switches["SW_D"].motor_unavailable = True
    return network, switches


def test_a_hardware_activation_throws_every_switch_to_the_layout():
    network, switches = _network_with_switches()
    sim = SimulationController(network)
    sim.activate_interfaces(OperationMode.VIRTUAL_HARDWARE, None, None, None)
    assert not any(s.position_known for s in switches.values())

    sim.finish_activation()

    power = sim.switch_power_controller
    assert power.get_last_throw("SW_A") == 1  # thrown to the layout's DIVERGE
    assert switches["SW_A"].position_known is True
    assert switches["SW_A"].position == SwitchState.DIVERGE  # model unchanged
    assert power.get_last_throw("SW_M") == 0  # the manual switch is thrown too
    assert switches["SW_M"].position_known is True
    assert power.get_last_throw("SW_D") is None  # motor unavailable: skipped
    assert switches["SW_D"].position_known is False
    (event,) = [
        n for n in sim.notification_center.peek_recent(50)
        if n.operation == "throw_all_to_layout"
    ]
    assert event.payload == {"thrown": ["SW_A", "SW_M"], "skipped": ["SW_D"], "failed": []}
