"""Pathfinding must never write the canonical `Switch.position`.

design_new.md "Switch Entity": the `Switch.position` mutation workflow is
validate -> write -> notify -> throw, owned by `SwitchManager`, and "nothing
writes `Switch.position` without that pairing". `Tile.switch_state`'s
setter writes through to that canonical field, so a pathfinding query that
probes "which state would allow this traversal?" must answer it without
assigning the state — not even transiently.
"""

import pytest

from enums import SwitchState
from network import RailwayNetwork
from switch import Switch
from track import Tile

TRACK_NS = 1 << 0
TRACK_NE = 1 << 2


def _switch_tile():
    network = RailwayNetwork()
    tile = Tile(x=0, y=0, track_bits=TRACK_NS | TRACK_NE)
    tile.switch_id = "SW1"
    network.add_track(tile)
    switch = Switch(switch_id="SW1", x=0, y=0)
    switch.position = SwitchState(1)  # DIVERGE
    network.switches["SW1"] = switch
    tile.network = network
    return tile, switch


def test_probe_resolves_both_states_without_touching_the_switch():
    tile, switch = _switch_tile()
    assert tile.get_required_switch_state("N", "S") == 0  # main
    assert tile.get_required_switch_state("N", "E") == 1  # diverge
    assert switch.position == SwitchState(1), "probe moved the live switch"


def test_probe_leaves_the_switch_intact_when_get_exits_raises(monkeypatch):
    """The old implementation assigned `switch_state`, called `get_exits`, then
    restored it — with no try/finally. An exception in between left the live,
    authoritative `Switch.position` flipped and un-notified.
    """
    tile, switch = _switch_tile()

    def boom(*_args, **_kwargs):
        raise RuntimeError("geometry lookup failed mid-probe")

    monkeypatch.setattr(Tile, "get_exits", boom)

    with pytest.raises(RuntimeError):
        tile.get_required_switch_state("N", "S")

    assert switch.position == SwitchState(1), (
        "a failed probe corrupted the canonical Switch.position"
    )


def test_get_exits_override_does_not_write_switch_state():
    tile, switch = _switch_tile()
    # Ask for the main-state answer while the switch physically sits at diverge.
    assert tile.get_exits("N", switch_state_override=0) == ["S"]
    assert switch.position == SwitchState(1)
    # And the un-overridden read still reflects the real state.
    assert tile.get_exits("N") == ["E"]
