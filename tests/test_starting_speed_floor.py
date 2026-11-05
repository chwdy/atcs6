"""A moving train never runs below its starting speed.

design_new.md:
- "TrainPhysics", A moving train never runs below its starting speed — a speed
  above 0 and below the starting speed becomes the starting speed when the tick
  drives the train forward, and 0 otherwise.
- "A speed curve does not fall below the starting speed ahead of a stop" — every
  lookahead sample's limit is 0 or at least the starting speed; a braking curve
  holds the starting speed until the sample is within
  RESERVATION_STOP_ARRIVAL_TOLERANCE_CM of the stop; a posted limit below the
  starting speed reads as the starting speed.
- Step `914f` reads a zero stop limit against the starting speed, so a train
  reaching its stop at the starting speed is cut, not braked by EB.
"""

import pytest

from constants import RESERVATION_STOP_ARRIVAL_TOLERANCE_CM
from enums import Direction, HandlePosition
from network import RailwayNetwork
from speed_profile import SpeedProfile
from track import TRACK_EW, Tile
from train import Train, TrainCar
from train_controller import TrainController
from train_controller_types import (
    PHYSICS_KIND_EMERGENCY_BRAKE,
    PHYSICS_KIND_HANDLE,
    PHYSICS_KIND_TARGET_SPEED,
    PhysicsCommand,
)
from train_physics import TrainPhysics

STARTING_KMH = 20.0


def _profile() -> SpeedProfile:
    return SpeedProfile(
        profile_id="FLOOR",
        name="Floor",
        voltage_speed_curve=[(3.0, STARTING_KMH), (12.0, 80.0)],
        # A gentle brake, so the plain braking curve falls below the starting
        # speed well before the stop.
        deceleration_rate=1.0,
    )


# ---- physics ---------------------------------------------------------------


@pytest.fixture
def physics():
    return TrainPhysics(train_id="T", network=RailwayNetwork())


@pytest.mark.parametrize(
    "command,expected",
    [
        # A positive commanded speed drives the train: it runs at the floor.
        (PhysicsCommand(kind=PHYSICS_KIND_TARGET_SPEED, target_speed_kmh=5.0), STARTING_KMH),
        # ATO (no atc_limit on the command) driving toward a positive target.
        (PhysicsCommand(kind=PHYSICS_KIND_HANDLE, handle=HandlePosition.P3, target_speed_kmh=40.0), STARTING_KMH),
        # ATO whose commanded speed is 0 (a stop, or not engaged): it stops.
        (PhysicsCommand(kind=PHYSICS_KIND_HANDLE, handle=HandlePosition.B4, target_speed_kmh=0.0), 0.0),
        # Manual `simulation`: a power notch under a positive ATC limit.
        (PhysicsCommand(kind=PHYSICS_KIND_HANDLE, handle=HandlePosition.P1, atc_limit_kmh=60.0), STARTING_KMH),
        # Manual `simulation`: coasting or braking below the floor stops it.
        (PhysicsCommand(kind=PHYSICS_KIND_HANDLE, handle=HandlePosition.N, atc_limit_kmh=60.0), 0.0),
        (PhysicsCommand(kind=PHYSICS_KIND_HANDLE, handle=HandlePosition.B2, atc_limit_kmh=60.0), 0.0),
        # A power notch under a zero ATC limit does not drive.
        (PhysicsCommand(kind=PHYSICS_KIND_HANDLE, handle=HandlePosition.P5, atc_limit_kmh=0.0), 0.0),
        # An emergency brake never drives.
        (PhysicsCommand(kind=PHYSICS_KIND_EMERGENCY_BRAKE), 0.0),
    ],
)
def test_a_speed_below_the_floor_becomes_the_floor_or_zero(physics, command, expected):
    assert physics._apply_starting_speed_floor(command, _profile(), 7.0) == expected


def test_speeds_at_or_above_the_floor_and_zero_are_left_alone(physics):
    drive = PhysicsCommand(kind=PHYSICS_KIND_TARGET_SPEED, target_speed_kmh=30.0)
    assert physics._apply_starting_speed_floor(drive, _profile(), 0.0) == 0.0
    assert physics._apply_starting_speed_floor(drive, _profile(), STARTING_KMH) == STARTING_KMH
    assert physics._apply_starting_speed_floor(drive, _profile(), 33.0) == 33.0


def test_a_train_starting_from_a_stand_jumps_to_its_starting_speed(physics):
    """The first driven tick from a stand runs at the starting speed, not at
    one tick of acceleration."""
    command = PhysicsCommand(
        kind=PHYSICS_KIND_HANDLE, handle=HandlePosition.P5, target_speed_kmh=60.0
    )
    assert physics._apply_physics_command(command, _profile(), 0.0, 0.1) == STARTING_KMH


# ---- lookahead speed curves -------------------------------------------------


@pytest.fixture
def controller():
    network = RailwayNetwork()
    for x in range(20):
        network.add_track(Tile(x=x, y=0, track_bits=TRACK_EW))
    network.speed_profiles.register(_profile())
    car = TrainCar(car_id="CAR", car_type="LOCOMOTIVE", length_cm=30.0)
    train = Train(train_id="T_FLOOR", consist=[car], max_speed=80.0)
    train.speed_profile_id = "FLOOR"
    network.add_train(train)
    network.place_train(train, 0, 0, Direction.EAST)
    controller = TrainController(train_id=train.train_id, network=network)
    controller.commands.control_mode = "ato"
    controller.powered = True
    return controller


def _stop_point(controller, clearance_cm):
    return clearance_cm - controller.atc.effective_safety_margin()


def test_every_limit_is_zero_or_at_least_the_starting_speed(controller):
    curves = controller.calculate_speed_curves(max_distance_cm=300.0)
    for point in curves:
        for key in ("atc_limit", "operational_limit"):
            value = point[key]
            assert value == 0.0 or value >= STARTING_KMH - 1e-9, (point, key)


def test_the_braking_curve_holds_the_starting_speed_up_to_the_stop_window(controller):
    clearance = 300.0
    stop = _stop_point(controller, clearance)
    curves = controller.calculate_speed_curves(max_distance_cm=clearance)
    before = [p for p in curves if p["distance_cm"] < stop - RESERVATION_STOP_ARRIVAL_TOLERANCE_CM]
    inside = [p for p in curves if p["distance_cm"] >= stop - RESERVATION_STOP_ARRIVAL_TOLERANCE_CM]
    assert before and inside
    # Just short of the window, where the plain braking curve would have
    # fallen near zero, the train may still run at its starting speed.
    assert before[-1]["atc_limit"] == pytest.approx(STARTING_KMH)
    assert before[-1]["operational_limit"] == pytest.approx(STARTING_KMH)
    # From the window on the limit is 0: the power is cut there.
    assert all(p["atc_limit"] == 0.0 and p["operational_limit"] == 0.0 for p in inside)


def test_a_posted_limit_below_the_starting_speed_reads_as_the_starting_speed(controller):
    blocks = [{"id": "SLOW", "start_cm": 0.0, "end_cm": 400.0, "speed_limit_kmh": 10.0}]
    curves = controller.calculate_speed_curves(
        max_distance_cm=1000.0, lookahead_blocks=blocks
    )
    first = curves[0]
    assert first["atc_limit"] == pytest.approx(STARTING_KMH)
    assert first["atc_limit_reason_code"] == "speed_limit"


# ---- a stop at the starting speed is cut, not EB'd -------------------------


def test_a_route_stop_at_a_high_starting_speed_raises_no_eb(monkeypatch):
    """A train whose starting speed is above OVERSPEED_EB_THRESHOLD_KMH reaches
    every stop at that speed; step `914f` must read the stop's zero limit
    against the starting speed, or every stop would raise EB."""
    from tests.journey.harness import JourneyHarness
    from tests.journey.journeys.north_station_turnaround_journey import (
        build_north_station_turnaround_journey,
    )
    from tests.journey.scenes.north_station_turnaround_scene import (
        NorthStationTurnaroundScene,
    )

    real = SpeedProfile.starting_speed_kmh
    monkeypatch.setattr(
        SpeedProfile,
        "starting_speed_kmh",
        property(lambda self: max(real.fget(self), 25.0)),
    )
    harness = JourneyHarness(
        scene=NorthStationTurnaroundScene(),
        journey=build_north_station_turnaround_journey(),
        dt=0.1,
        max_ticks=3500,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()


test_a_route_stop_at_a_high_starting_speed_raises_no_eb = pytest.mark.scene("demo_main")(
    test_a_route_stop_at_a_high_starting_speed_raises_no_eb
)
