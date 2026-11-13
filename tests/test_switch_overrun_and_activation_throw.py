"""Two switch rules decided on the 2026-11-02 rig run.

- design_new.md "Switch entity", An overrun onto unclaimed points is not an
  intrusion: a train that runs onto points no path has locked and no other
  train protects is recorded on the switch; points something claims stay a
  fatal `switch_intrusion`.
- design_new.md "Switch entity", Activation throws every switch to the layout:
  a Virtual Hardware or Real Hardware activation throws every switch that is
  neither disabled nor manual once to `Switch.position`, which makes it known.
"""

from enums import OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from runtime_payloads import FatalConflictKind, SwitchLockClaim
from simulation import SimulationController
from switch import Switch, SwitchState
from switch_manager import SwitchManager
from track import TRACK_EW, TRACK_NW, Tile

ROUTE = frozenset({"W", "N"})


def _manager(switch):
    captured = []
    events = []
    manager = SwitchManager(
        switches={switch.switch_id: switch},
        network=None,
        fatal_conflict_reporter=captured.append,
    )
    manager.notification_emitter = lambda **event: events.append(event)
    return manager, captured, events


def test_an_overrun_onto_unclaimed_points_is_recorded_not_fatal():
    switch = Switch(switch_id="SW_1", x=0, y=0)
    manager, captured, events = _manager(switch)

    assert manager.on_physical_entry("SW_1", "T1", "P1", ROUTE) is True

    assert captured == []
    assert switch.route_owners.get(ROUTE) == "T1"
    (event,) = events
    assert str(getattr(event["kind"], "value", event["kind"])) == "warning"
    assert "SW_1" in event["message"]


def test_points_another_path_has_locked_stay_an_intrusion():
    switch = Switch(switch_id="SW_1", x=0, y=0)
    assert switch.acquire_lock(
        SwitchLockClaim(path_id="P2", train_id="T2", locked_state=1, source_switch_id=None)
    )
    manager, captured, _ = _manager(switch)

    assert manager.on_physical_entry("SW_1", "T1", "P1", ROUTE) is False

    assert captured[0].conflict_kind == FatalConflictKind.SWITCH_INTRUSION
    assert switch.route_owners == {}


def test_points_another_train_protects_stay_an_intrusion():
    switch = Switch(switch_id="SW_1", x=0, y=0)
    switch.train_protection_claims.append(("T2", "SW_9", 0))
    manager, captured, _ = _manager(switch)

    assert manager.on_physical_entry("SW_1", "T1", "P1", ROUTE) is False
    assert captured[0].conflict_kind == FatalConflictKind.SWITCH_INTRUSION


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
    switches["SW_D"].disabled = True
    return network, switches


def test_a_hardware_activation_throws_every_switch_to_the_layout():
    network, switches = _network_with_switches()
    sim = SimulationController(network, PathDispatcher(network))
    sim.activate_interfaces(OperationMode.VIRTUAL_HARDWARE, None, None, None)
    assert not any(s.position_known for s in switches.values())

    sim.finish_activation()

    power = sim.switch_power_controller
    assert power.get_last_throw("SW_A") == 1  # thrown to the layout's DIVERGE
    assert switches["SW_A"].position_known is True
    assert switches["SW_A"].flip_count == 0  # the model's position did not change
    for skipped in ("SW_M", "SW_D"):
        assert power.get_last_throw(skipped) is None
        assert switches[skipped].position_known is False
    (event,) = [
        n for n in sim.notification_center.peek_recent(50)
        if n.operation == "throw_all_to_layout"
    ]
    assert event.payload == {"thrown": ["SW_A"], "skipped": ["SW_D", "SW_M"], "failed": []}
