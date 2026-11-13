"""Tests for switch throw output.

Covers design_new.md "Power" (Switch throw output) and the `SwitchManager`
throw integration:
- the throw output interface records the commanded alignment,
- `SwitchManager.request_position_change(...)` emits the throw to the interface,
- a throw the interface cannot issue (no hardware binding) fails the command,
- and a setup with no output interface wired treats the throw as a no-op.

A throw is blind — a solenoid point motor reports no position back — so there is
no confirmation step to test. The only failure the software can observe is a
command that never left the interface.
"""

import pytest

from track import Tile, TRACK_EW, TRACK_NW, TRACK_NS
from switch import Switch
from switch_manager import SwitchManager
from switch_power import SimulatedSwitchDriver, SwitchPowerController
from runtime_payloads import HardwareFailure


# ---------------------------------------------------------------------------
# Lightweight fakes — real Tile / Switch geometry, minimal network.
# ---------------------------------------------------------------------------


class SwitchOutputHolder:
    """Wires a `SwitchPowerController` over whatever `SwitchOutput` is assigned.

    `SwitchManager` reaches the controller, never the interface — the controller
    is `SwitchOutput`'s only caller (design_new.md "Power", Switch
    power-controller role). These fakes are still written in terms of the
    interface, because that is what a test wants to assert against, so assigning
    one builds the controller that owns it.
    """

    @property
    def switch_output_interface(self):
        return self._switch_output_interface

    @switch_output_interface.setter
    def switch_output_interface(self, iface):
        self._switch_output_interface = iface
        self.switch_power_controller = (
            SwitchPowerController(output_interface=iface) if iface is not None else None
        )


class FakeNetwork(SwitchOutputHolder):
    """Just enough of RailwayNetwork for SwitchManager."""

    def __init__(self):
        self.tracks = {}
        self.switches = {}
        self.blocks = {}
        self._tile_block = {}
        self.power_controller = None
        self.switch_output_interface = None
        self.block_manager = None

    def add_track(self, seg):
        self.tracks[(seg.x, seg.y)] = seg

    def get_track(self, x, y):
        return self.tracks.get((x, y))

    def get_neighbor(self, x, y, edge):
        off = {"N": (0, 1), "E": (1, 0), "S": (0, -1), "W": (-1, 0)}.get(edge)
        if off is None:
            return None
        return self.tracks.get((x + off[0], y + off[1]))

    def tile_to_block_id(self, x, y):
        return self._tile_block.get((x, y))


def _standard_switch_network():
    """Switch at (1,0) = EW|NW.

    Geometry: throat (common entry leg) = W; MAIN(0) exit E -> (2,0);
    DIVERGE(1) exit N -> (1,1); entry across W -> (0,0).
    """
    net = FakeNetwork()
    net.add_track(Tile(x=1, y=0, track_bits=TRACK_EW | TRACK_NW))
    net.add_track(Tile(x=0, y=0, track_bits=TRACK_EW))  # entry side
    net.add_track(Tile(x=2, y=0, track_bits=TRACK_EW))  # MAIN branch
    net.add_track(Tile(x=1, y=1, track_bits=TRACK_NS))  # DIVERGE branch
    net._tile_block = {
        (0, 0): "BLK_ENTRY",
        (2, 0): "BLK_MAIN",
        (1, 1): "BLK_DIV",
    }
    sw = Switch(switch_id="SW_1_0", x=1, y=0)
    net.switches = {"SW_1_0": sw}
    return net, sw


# ---------------------------------------------------------------------------
# Throw output interface
# ---------------------------------------------------------------------------


def test_simulated_switch_output_records_throw():
    iface = SimulatedSwitchDriver()
    assert iface.get_last_throw("SW_1") is None
    assert iface.set_throw("SW_1", 1) is True
    assert iface.get_last_throw("SW_1") == 1
    iface.set_throw("SW_1", 0)
    assert iface.get_last_throw("SW_1") == 0


def test_simulated_switch_output_safe_state_drops_throws():
    iface = SimulatedSwitchDriver()
    iface.set_throw("SW_1", 1)
    iface.enter_safe_state()
    assert iface.get_last_throw("SW_1") is None


# ---------------------------------------------------------------------------
# SwitchManager integration
# ---------------------------------------------------------------------------


def test_request_position_change_emits_throw():
    net, sw = _standard_switch_network()
    net.switch_output_interface = SimulatedSwitchDriver()
    mgr = SwitchManager(switches=net.switches, network=net)

    ok, msg = mgr.request_position_change("SW_1_0", 1)
    assert ok, msg
    assert int(sw.position) == 1
    # The physical throw was emitted to the hardware interface.
    assert net.switch_output_interface.get_last_throw("SW_1_0") == 1


def test_request_position_change_rejects_unissued_throw():
    """A switch the output interface has no binding for fails the command.

    There is no confirmation step behind this — the refusal has to stop at
    `set_throw` returning False, or a throw that never reached a coil would be
    reported to the operator as a success.
    """

    class RefusingInterface(SimulatedSwitchDriver):
        def set_throw(self, switch_id, target_state):
            return False          # no hardware binding for this switch

    net, sw = _standard_switch_network()
    net.switch_output_interface = RefusingInterface()
    mgr = SwitchManager(switches=net.switches, network=net)

    ok, reason = mgr.request_position_change("SW_1_0", 1)
    assert not ok
    assert "no hardware binding" in reason
    # Commanded position stays authoritative — no rollback.
    assert int(sw.position) == 1


class _RecordingRuntime:
    """The two things `SwitchPowerController` reports an unissued throw to."""

    def __init__(self):
        self.failures = []
        self.events = []
        self.notification_center = self

    def enter_hardware_failure(self, failure):
        self.failures.append(failure)

    def emit(self, **event):
        self.events.append(event)


@pytest.mark.parametrize("raises", [False, True])
def test_an_unissued_throw_is_reported_by_the_switch_power_controller(raises):
    """design_new.md "Power", An unissued throw is fatal to a hardware-backed
    session: `SwitchPowerController` is the only reporter; `SwitchManager`
    reports nothing itself."""

    class FailingInterface(SimulatedSwitchDriver):
        def set_throw(self, switch_id, target_state):
            if raises:
                raise OSError("read error")
            return False

    runtime = _RecordingRuntime()
    net, _sw = _standard_switch_network()
    net.switch_output_interface = FailingInterface()
    net.switch_power_controller.wire_dependencies(
        notification_center=runtime.notification_center,
        enter_hardware_failure=runtime.enter_hardware_failure,
    )
    manager_events = []
    mgr = SwitchManager(
        switches=net.switches,
        network=net,
        notification_emitter=lambda **event: manager_events.append(event),
    )

    ok, _reason = mgr.request_position_change("SW_1_0", 1)

    assert ok is False
    assert len(runtime.failures) == 1
    assert isinstance(runtime.failures[0], HardwareFailure)
    assert runtime.failures[0].component == "switch_output"
    assert runtime.failures[0].resource_id == "SW_1_0"
    assert [e["source"] for e in runtime.events] == ["SwitchPowerController"]
    assert not any(e.get("kind") == "warning" for e in manager_events)


def test_request_position_change_no_interface_is_noop_success():
    """Without a SwitchOutput wired, the throw is a no-op."""
    net, sw = _standard_switch_network()
    # net.switch_output_interface stays None
    mgr = SwitchManager(switches=net.switches, network=net)

    ok, msg = mgr.request_position_change("SW_1_0", 1)
    assert ok, msg
    assert int(sw.position) == 1


# ---------------------------------------------------------------------------
# What decides whether a coil fires is the OUTPUT's last throw, not the model's
# position. The two part company routinely (a scene assigns positions at build
# time, a bench test pulses coils directly, someone throws a point by hand), and
# when they do, re-commanding the position the model already holds is the only
# way an operator has to put the points back where the app thinks they are.
# ---------------------------------------------------------------------------


def test_a_switch_never_thrown_is_driven_even_at_its_current_position():
    """At session build every `Switch.position` is a guess: the scene assigned
    it and no coil was ever driven. The first command naming that alignment has
    to reach the motor, or the app goes on trusting a guess."""
    net, sw = _standard_switch_network()
    net.switch_output_interface = SimulatedSwitchDriver()
    mgr = SwitchManager(switches=net.switches, network=net)
    assert int(sw.position) == 0
    assert net.switch_output_interface.get_last_throw("SW_1_0") is None

    ok, msg = mgr.request_position_change("SW_1_0", 0)

    assert ok, msg
    assert net.switch_output_interface.get_last_throw("SW_1_0") == 0


def test_recommanding_the_position_already_held_still_drives_the_coil():
    """The operator's re-sync: the model and the points have drifted apart, and
    asking for the alignment the model already believes in must move the motor."""
    net, sw = _standard_switch_network()
    net.switch_output_interface = SimulatedSwitchDriver()
    mgr = SwitchManager(switches=net.switches, network=net)
    mgr.request_position_change("SW_1_0", 1)

    # Something drove the points elsewhere without telling the model — a bench
    # test, a hand throw, a power cycle. The output's record is what changes.
    net.switch_output_interface.clear()

    ok, msg = mgr.request_position_change("SW_1_0", 1)

    assert ok, msg
    assert int(sw.position) == 1
    assert net.switch_output_interface.get_last_throw("SW_1_0") == 1


def test_a_throw_the_output_just_made_is_not_repeated():
    """The skip that remains: the output itself last drove this switch here, so
    there is nothing to correct and no reason to pulse a solenoid again."""
    throws = []

    class CountingInterface(SimulatedSwitchDriver):
        def set_throw(self, switch_id, target_state):
            throws.append((switch_id, target_state))
            return super().set_throw(switch_id, target_state)

    net, _sw = _standard_switch_network()
    net.switch_output_interface = CountingInterface()
    mgr = SwitchManager(switches=net.switches, network=net)

    mgr.request_position_change("SW_1_0", 1)
    mgr.request_position_change("SW_1_0", 1)
    mgr.request_position_change("SW_1_0", 1)

    assert throws == [("SW_1_0", 1)]


# ---------------------------------------------------------------------------
# SwitchPowerController: the rail feed
# ---------------------------------------------------------------------------



def test_controller_safe_state_de_energizes_the_motors():
    iface = SimulatedSwitchDriver()
    controller = SwitchPowerController(output_interface=iface)
    controller.set_throw("SW_1_0", 1)

    controller.enter_safe_state()

    assert iface.get_last_throw("SW_1_0") is None


def test_reassigning_the_interface_retargets_the_controller():
    """The holder's two views of one wiring must not drift: swapping the driver
    must not leave the manager driving the interface it replaced. This is the
    real `SwitchOutputOwner` that `SimulationController` and `RailwayNetwork`
    both mix in, not the test fake above."""
    from switch_power import SwitchOutputOwner

    class Holder(SwitchOutputOwner):
        pass

    holder = Holder()
    original = SimulatedSwitchDriver()
    holder.switch_output_interface = original
    holder.switch_power_controller = SwitchPowerController(output_interface=original)

    replacement = SimulatedSwitchDriver()
    holder.switch_output_interface = replacement

    assert holder.switch_output_interface is replacement
    assert holder.switch_power_controller.output_interface is replacement


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
