"""Per-train default SpeedProfile seeding (design_new.md:2235, :2257, :2259).

Every train must have a default `SpeedProfile` whose `power_speed_curve` maps
100% -> the OWNING train's configured `max_speed`, seeded when the train's
default profile is created. `SimulationController.add_train(...)` is the
canonical runtime entry point for adding a train; before this change it left
`speed_profile_id = None`, so profile-less trains shared the module-global
`DEFAULT_SPEED_PROFILE` whose curve hardcodes 100% -> 100 km/h regardless of the
train's max_speed — wrong manual `power_percent` behaviour for any train whose
`max_speed != 100`.
"""

from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from train_physics import TrainPhysics


def _two_block_line():
    net = RailwayNetwork()
    for x in range(0, 4):
        net.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    net.create_block_with_tracks(
        block_id="BLK_A", positions=[(0, 0), (1, 0)], name="Block A", length_cm=100.0
    )
    net.create_block_with_tracks(
        block_id="BLK_B", positions=[(2, 0), (3, 0)], name="Block B", length_cm=100.0
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

    # The train points at a registered, per-train profile (not None, not shared).
    assert train.speed_profile_id is not None
    assert train.speed_profile_id != "default"
    profile = net.speed_profiles.get(train.speed_profile_id)
    assert profile is not None

    # 100% power anchors at the train's max_speed; the curve stays linear.
    assert profile.power_percent_to_speed(100.0) == 60.0
    assert profile.power_percent_to_speed(50.0) == 30.0
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
    fast_profile = net.speed_profiles.get(fast.speed_profile_id)
    slow_profile = net.speed_profiles.get(slow.speed_profile_id)
    assert fast_profile.power_percent_to_speed(100.0) == 120.0
    assert slow_profile.power_percent_to_speed(100.0) == 40.0
