"""Tests for the switch power controller and throw continuity verification.

Covers design_new.md "Power" (Switch power-controller role) and the
`SwitchManager` throw+verify integration:
- the ±throw output interface records the commanded alignment,
- port derivation (entry + MAIN/DIVERGE branch blocks) from tile geometry,
- continuity verification confirms in simulation (reading derived from the
  commanded `Switch.position`, so always as-expected),
- it is skipped for crossings (no single entry leg) and when energization is
  indeterminate,
- it fails on a measured mismatch (the real-hardware / stub-interface path),
- and `SwitchManager.request_position_change(...)` surfaces all of the above.
"""

import pytest

from track import TrackSegment, TRACK_EW, TRACK_NW, TRACK_NS, TRACK_SE
from switch import Switch
from enums import SwitchState
from block_power import BlockPowerController, OutputInterface, SimulatedOutputInterface
from switch_manager import SwitchManager
from switch_power import (
    SwitchPowerController,
    SimulatedSwitchOutputInterface,
    SwitchThrowVerification,
)


# ---------------------------------------------------------------------------
# Lightweight fakes — real TrackSegment / Switch geometry, minimal network.
# ---------------------------------------------------------------------------


class FakeBlock:
    def __init__(self, voltage: float = 0.0):
        self.current_voltage = voltage


class FakeNetwork:
    """Just enough of RailwayNetwork for SwitchPowerController / SwitchManager."""

    def __init__(self):
        self.tracks = {}
        self.switches = {}
        self.blocks = {}
        self._tile_block = {}
        self.power_controller = None
        self.switch_power_controller = None
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


class StubMeasuredInterface(OutputInterface):
    """Non-simulated output interface returning caller-set 'measured' voltages.

    Used to exercise the real-hardware verification path: its readings are
    independent of the commanded per-block source voltages.
    """

    def __init__(self, measured=None):
        self._measured = dict(measured or {})

    def set_voltage(self, block_id, voltage):
        self._measured[block_id] = voltage
        return True

    def get_actual_voltage(self, block_id):
        return self._measured.get(block_id, 0.0)

    def get_all_voltages(self):
        return dict(self._measured)

    def enter_safe_state(self):
        for block_id in self._measured:
            self._measured[block_id] = 0.0


def _standard_switch_network():
    """Switch at (1,0) = EW|NW.

    Geometry: throat (common entry leg) = W; MAIN(0) exit E -> (2,0);
    DIVERGE(1) exit N -> (1,1); entry across W -> (0,0).
    """
    net = FakeNetwork()
    net.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_EW | TRACK_NW))
    # Neighbour tiles must exist for get_neighbor; their own geometry is
    # irrelevant to port derivation.
    net.add_track(TrackSegment(x=0, y=0, track_bits=TRACK_EW))  # entry side
    net.add_track(TrackSegment(x=2, y=0, track_bits=TRACK_EW))  # MAIN branch
    net.add_track(TrackSegment(x=1, y=1, track_bits=TRACK_NS))  # DIVERGE branch
    net._tile_block = {
        (0, 0): "BLK_ENTRY",
        (2, 0): "BLK_MAIN",
        (1, 1): "BLK_DIV",
    }
    net.blocks = {
        "BLK_ENTRY": FakeBlock(),
        "BLK_MAIN": FakeBlock(),
        "BLK_DIV": FakeBlock(),
    }
    sw = Switch(switch_id="SW_1_0", x=1, y=0)
    net.switches = {"SW_1_0": sw}
    return net, sw


# ---------------------------------------------------------------------------
# Throw output interface
# ---------------------------------------------------------------------------


def test_simulated_switch_output_records_throw():
    iface = SimulatedSwitchOutputInterface()
    assert iface.get_last_throw("SW_1") is None
    assert iface.set_throw("SW_1", 1) is True
    assert iface.get_last_throw("SW_1") == 1
    iface.set_throw("SW_1", 0)
    assert iface.get_last_throw("SW_1") == 0


def test_controller_throw_delegates_to_interface():
    spc = SwitchPowerController()
    assert spc.throw("SW_2", 1) is True
    assert spc.output_interface.get_last_throw("SW_2") == 1


# ---------------------------------------------------------------------------
# Port derivation from geometry
# ---------------------------------------------------------------------------


def test_derive_ports_standard_switch():
    net, sw = _standard_switch_network()
    spc = SwitchPowerController(net)
    entry, branches = spc._derive_ports(sw)
    assert entry == "BLK_ENTRY"
    assert branches == {0: "BLK_MAIN", 1: "BLK_DIV"}


def test_derive_ports_crossover_returns_none():
    """A scissors crossover (NS+EW+NW+SE) has no single common entry leg."""
    net = FakeNetwork()
    net.add_track(
        TrackSegment(x=1, y=0, track_bits=TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE)
    )
    sw = Switch(switch_id="X", x=1, y=0)
    net.switches = {"X": sw}
    spc = SwitchPowerController(net)
    assert spc._derive_ports(sw) is None


# ---------------------------------------------------------------------------
# Continuity verification — simulation (always as-expected)
# ---------------------------------------------------------------------------


def test_verify_confirms_in_sim_when_aligned_and_energized():
    net, sw = _standard_switch_network()
    net.power_controller = BlockPowerController(net)  # default Simulated interface
    net.blocks["BLK_ENTRY"].current_voltage = 9.0
    spc = SwitchPowerController(net)

    sw.position = SwitchState.MAIN
    res = spc.verify_throw(sw, 0)
    assert res.confirmed and not res.skipped

    sw.position = SwitchState.DIVERGE
    res = spc.verify_throw(sw, 1)
    assert res.confirmed and not res.skipped


def test_verify_skips_for_crossover():
    net = FakeNetwork()
    net.add_track(
        TrackSegment(x=1, y=0, track_bits=TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE)
    )
    sw = Switch(switch_id="X", x=1, y=0)
    net.switches = {"X": sw}
    net.power_controller = BlockPowerController(net)
    res = SwitchPowerController(net).verify_throw(sw, 0)
    assert res.skipped and not res.confirmed


def test_verify_skips_when_no_power_controller():
    net, sw = _standard_switch_network()
    res = SwitchPowerController(net).verify_throw(sw, 0)
    assert res.skipped and not res.confirmed


def test_verify_skips_when_energization_indeterminate():
    net, sw = _standard_switch_network()
    net.power_controller = BlockPowerController(net)
    spc = SwitchPowerController(net)

    # Neither side energized.
    assert spc.verify_throw(sw, 0).skipped

    # Both sides energized.
    net.blocks["BLK_ENTRY"].current_voltage = 9.0
    net.blocks["BLK_MAIN"].current_voltage = 9.0
    assert spc.verify_throw(sw, 0).skipped


# ---------------------------------------------------------------------------
# Continuity verification — real-hardware / stub-interface path
# ---------------------------------------------------------------------------


def test_verify_fails_on_measured_mismatch():
    """Switch did not physically move: entry energized but the commanded
    branch reads 0 (isolated)."""
    net, sw = _standard_switch_network()
    sw.position = SwitchState.MAIN
    net.blocks["BLK_ENTRY"].current_voltage = 9.0  # commanded source
    stub = StubMeasuredInterface(
        {"BLK_ENTRY": 9.0, "BLK_MAIN": 0.0, "BLK_DIV": 0.0}
    )
    net.power_controller = BlockPowerController(net, output_interface=stub)
    res = SwitchPowerController(net).verify_throw(sw, 0)
    assert not res.confirmed and not res.skipped
    assert "continuity mismatch" in (res.reason or "")


def test_verify_confirms_on_measured_match():
    """Switch moved: entry and the commanded branch read the same value."""
    net, sw = _standard_switch_network()
    sw.position = SwitchState.MAIN
    net.blocks["BLK_ENTRY"].current_voltage = 9.0
    stub = StubMeasuredInterface(
        {"BLK_ENTRY": 9.0, "BLK_MAIN": 9.0, "BLK_DIV": 0.0}
    )
    net.power_controller = BlockPowerController(net, output_interface=stub)
    res = SwitchPowerController(net).verify_throw(sw, 0)
    assert res.confirmed and not res.skipped


# ---------------------------------------------------------------------------
# SwitchManager integration
# ---------------------------------------------------------------------------


def test_request_position_change_confirms_in_sim():
    net, sw = _standard_switch_network()
    net.power_controller = BlockPowerController(net)
    net.switch_power_controller = SwitchPowerController(net)
    net.blocks["BLK_ENTRY"].current_voltage = 9.0
    mgr = SwitchManager(switches=net.switches, simulation_controller=None, network=net)

    ok, msg = mgr.request_position_change("SW_1_0", 1)
    assert ok, msg
    assert int(sw.position) == 1
    # The physical throw was emitted to the hardware interface.
    assert net.switch_power_controller.output_interface.get_last_throw("SW_1_0") == 1


def test_request_position_change_rejects_unconfirmed_throw():
    net, sw = _standard_switch_network()
    net.blocks["BLK_ENTRY"].current_voltage = 9.0
    stub = StubMeasuredInterface(
        {"BLK_ENTRY": 9.0, "BLK_MAIN": 0.0, "BLK_DIV": 0.0}
    )
    net.power_controller = BlockPowerController(net, output_interface=stub)
    net.switch_power_controller = SwitchPowerController(net)
    mgr = SwitchManager(switches=net.switches, simulation_controller=None, network=net)

    ok, reason = mgr.request_position_change("SW_1_0", 1)
    assert not ok
    assert "continuity mismatch" in reason
    # Commanded position stays authoritative — no rollback.
    assert int(sw.position) == 1


def test_request_position_change_no_controller_is_noop_success():
    """Without a SwitchPowerController wired, verification is a no-op."""
    net, sw = _standard_switch_network()
    net.power_controller = BlockPowerController(net)
    # net.switch_power_controller stays None
    mgr = SwitchManager(switches=net.switches, simulation_controller=None, network=net)

    ok, msg = mgr.request_position_change("SW_1_0", 1)
    assert ok, msg
    assert int(sw.position) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
