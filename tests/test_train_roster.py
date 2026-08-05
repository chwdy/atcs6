"""C4 — Train roster: catalog persistence + placement import.

design_new.md "Train Roster": the `TrainRoster` is configuration data saved with
the layout snapshot. Placing a train may import a `RosterEntry`:
`SimulationController.add_train(...)` seeds the new `Train`'s config fields and
its `SpeedProfile` link from the named entry. The entry is a placement template;
the placed `Train` is a separate runtime instance.
"""

from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from speed_profile import make_default_speed_profile
from track import TRACK_EW, Tile
from train_roster import RosterEntry, TrainRoster
from tests.segment_test_helpers import make_segment_block_with_tracks


# --- entity / persistence -------------------------------------------------


def test_roster_entry_roundtrip():
    e = RosterEntry(
        entry_id="E1", name="Big Boy", model="Athearn 4014",
        purchase_date="2024-01-15", purchase_price=499.99,
        consist_length_cm=42.0, max_speed=88.0, min_radius_cm=25.0,
        max_slope=3.5, speed_profile_id="PROF_X",
    )
    assert RosterEntry.from_dict(e.to_dict()) == e


def test_roster_roundtrip():
    roster = TrainRoster()
    roster.add_entry(RosterEntry(entry_id="E1", name="A"))
    roster.add_entry(RosterEntry(entry_id="E2", name="B"))
    restored = TrainRoster.from_dict(roster.to_dict())
    assert set(restored.entries) == {"E1", "E2"}
    assert restored.get_entry("E2").name == "B"


def test_network_persists_roster():
    net = RailwayNetwork()
    net.train_roster.add_entry(RosterEntry(entry_id="E1", max_speed=77.0))
    net2 = RailwayNetwork()
    net2.from_dict(net.to_dict())
    assert net2.train_roster.get_entry("E1").max_speed == 77.0


# --- placement import (end-to-end add_train) ------------------------------


def _placeable_network():
    net = RailwayNetwork()
    for x in range(3):
        net.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(net, "BLOCK_1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0)
    return net


def test_add_train_imports_roster_entry_defaults():
    net = _placeable_network()
    sim = SimulationController(net, PathDispatcher(net))
    net.train_roster.add_entry(
        RosterEntry(
            entry_id="E1", name="Loco", consist_length_cm=42.0, max_speed=88.0,
            min_radius_cm=25.0, max_slope=3.5,
        )
    )
    train = sim.add_train(
        train_id="T1", starting_block_id="BLOCK_1", initial_direction="EAST",
        max_speed_kmh=100.0, length_cm=100.0, roster_entry_id="E1",
    )
    # Entry defaults seed the placed Train (overriding the call args).
    assert train.max_speed == 88.0
    assert train.min_radius_cm == 25.0
    assert train.max_slope == 3.5
    assert train.consist[0].length_cm == 42.0


def test_add_train_links_existing_speed_profile():
    net = _placeable_network()
    sim = SimulationController(net, PathDispatcher(net))
    profile = make_default_speed_profile("CALIBRATED_LOCO", 90.0)
    net.speed_profiles.register(profile)
    net.train_roster.add_entry(
        RosterEntry(entry_id="E1", speed_profile_id=profile.profile_id)
    )
    train = sim.add_train(
        train_id="T1", starting_block_id="BLOCK_1", initial_direction="EAST",
        max_speed_kmh=100.0, length_cm=50.0, roster_entry_id="E1",
    )
    # The placed train keeps the roster's profile link (no fresh default).
    assert train.speed_profile_id == profile.profile_id


def test_add_train_without_roster_uses_call_args():
    net = _placeable_network()
    sim = SimulationController(net, PathDispatcher(net))
    train = sim.add_train(
        train_id="T1", starting_block_id="BLOCK_1", initial_direction="EAST",
        max_speed_kmh=64.0, length_cm=48.0,
    )
    assert train.max_speed == 64.0
    assert train.consist[0].length_cm == 48.0
