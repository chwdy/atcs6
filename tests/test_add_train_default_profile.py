"""Per-train default SpeedProfile seeding (design_new.md "SpeedProfile").

Every train must have a default `SpeedProfile` whose `voltage_speed_curve`
reaches the OWNING train's configured `max_speed` at `max_voltage` (so 100%
power is that speed), seeded when the train's
default profile is created. `SimulationController.add_train(...)` is the
canonical runtime entry point for adding a train; before this change it left
`speed_profile_id = None`, so profile-less trains shared the module-global
`DEFAULT_SPEED_PROFILE` whose curve hardcodes 100% -> 100 km/h regardless of the
train's max_speed — wrong manual `power_percent` behaviour for any train whose
`max_speed != 100`.
"""

import pytest
from network import RailwayNetwork
from track import Tile, TRACK_EW
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from train import Train
from train_physics import TrainPhysics
from tests.segment_test_helpers import make_segment_block_with_tracks


def _two_block_line():
    net = RailwayNetwork()
    for x in range(0, 4):
        net.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(net, "BLK_A", positions=[(0, 0), (1, 0)], name="Block A", length_cm=100.0
    )
    make_segment_block_with_tracks(net, "BLK_B", positions=[(2, 0), (3, 0)], name="Block B", length_cm=100.0
    )
    return net


def test_add_train_seeds_profile_anchored_at_max_speed():
    """A 60 km/h train added through `add_train` gets its own default profile
    whose power curve maps 100% -> 60 km/h (not the shared 100)."""
    net = _two_block_line()
    sim = SimulationController(net, PathDispatcher(net))

    train = sim.add_train(
        train_id="T60",
        starting_block_id="BLK_A",
        initial_direction="EAST",
        max_speed_kmh=60.0,
        length_cm=20.0,
    )

    # The train points at a registered, per-train instance profile (not None,
    # not shared) that derives from a model profile seeded alongside it.
    assert train.speed_profile_id is not None
    assert train.speed_profile_id != "default"
    instance = net.speed_profiles.get(train.speed_profile_id)
    assert instance is not None
    assert instance.derived_from is not None

    # The resolved profile anchors 100% power at the train's max_speed; the curve
    # stays linear. (Consumers read a train's calibration through resolve.)
    profile = net.speed_profiles.resolve(train.speed_profile_id)
    assert profile.power_percent_to_speed(100.0) == 60.0
    assert profile.power_percent_to_speed(50.0) == pytest.approx(7.5 + (60.0 - 7.5) / 2)
    assert profile.power_percent_to_speed(0.0) == 0.0

    # The physics resolver (`TrainPhysics._get_speed_profile`) returns it.
    physics = TrainPhysics(train.train_id, net)
    resolved = physics._get_speed_profile()
    assert resolved.profile_id == train.speed_profile_id
    assert resolved.power_percent_to_speed(100.0) == 60.0


def test_two_trains_get_distinct_profiles_for_distinct_max_speeds():
    """Two trains with different max_speed get independent default curves; the
    shared module-global default would have mapped both 100% -> 100 km/h."""
    net = _two_block_line()
    sim = SimulationController(net, PathDispatcher(net))

    fast = sim.add_train(
        train_id="FAST",
        starting_block_id="BLK_A",
        initial_direction="EAST",
        max_speed_kmh=120.0,
        length_cm=20.0,
    )
    slow = sim.add_train(
        train_id="SLOW",
        starting_block_id="BLK_B",
        initial_direction="EAST",
        max_speed_kmh=40.0,
        length_cm=20.0,
    )

    assert fast.speed_profile_id != slow.speed_profile_id
    fast_profile = net.speed_profiles.resolve(fast.speed_profile_id)
    slow_profile = net.speed_profiles.resolve(slow.speed_profile_id)
    assert fast_profile.power_percent_to_speed(100.0) == 120.0
    assert slow_profile.power_percent_to_speed(100.0) == 40.0


def test_profile_less_train_resolves_to_max_speed_anchored_default():
    """A train created by a path that does NOT seed a profile (from_dict load,
    step replay, editor add-without-place) has speed_profile_id=None; the
    `_get_speed_profile` resolver must still anchor its power curve to the
    train's own max_speed rather than the shared 100 km/h DEFAULT_SPEED_PROFILE.
    """
    net = _two_block_line()
    # Register a train directly, bypassing SimulationController.add_train's
    # seeding (mirrors network.from_dict / step_player / editor add-without-place).
    train = Train(train_id="T75", max_speed=75.0)
    net.add_train(train)
    assert train.speed_profile_id is None

    resolved = TrainPhysics(train.train_id, net)._get_speed_profile()
    assert resolved.power_percent_to_speed(100.0) == 75.0
    assert resolved.power_percent_to_speed(50.0) == pytest.approx(9.375 + (75.0 - 9.375) / 2)
    assert resolved.power_percent_to_speed(0.0) == 0.0
    # Distinct from the shared module-global default (which would map 100 -> 100).
    assert resolved.profile_id != "default"


def test_profile_resolution_tolerates_none_max_speed():
    """A train whose `max_speed` is None — reachable via `Train.from_dict` on an
    explicit `"max_speed": null` (the 100.0 default only applies when the key is
    absent) — must not crash profile resolution. The factory coerces a
    missing/non-numeric max_speed to the module-global top speed instead of
    raising on `float(None)`.
    """
    from speed_profile import make_default_speed_profile

    # The factory itself does not raise and yields a well-formed curve.
    prof = make_default_speed_profile("TNONE", None)
    assert prof.power_percent_to_speed(0.0) == 0.0
    assert prof.power_percent_to_speed(100.0) == 100.0  # coerced fallback top speed

    # The physics resolver tolerates a None-max_speed train end-to-end.
    net = _two_block_line()
    train = Train(train_id="TNONE", max_speed=75.0)
    net.add_train(train)
    train.max_speed = None  # simulate from_dict explicit-null load
    resolved = TrainPhysics(train.train_id, net)._get_speed_profile()
    assert resolved is not None
    assert resolved.power_percent_to_speed(0.0) == 0.0
