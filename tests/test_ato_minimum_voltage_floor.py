"""A moving ATO train is driven no lower than its ATO minimum voltage.

design_new.md "SpeedProfile", ATO drives no lower than its ATO minimum voltage:
when a train in `ato` mode moves, `TrainPhysics` outputs the higher of the
backward reading of its speed and the ATO minimum voltage. The two differ only
when the ATO minimum voltage lies inside a flat stretch of the curve, whose
lowest voltage the backward reading returns. A manual train has no floor, and a
standing ATO train still outputs what a powered train nothing drives outputs.
"""

from types import SimpleNamespace

from speed_profile import SpeedProfile
from train_controller_types import PHYSICS_KIND_HANDLE, PhysicsCommand
from train_physics import TrainPhysics

# Flat from 4 V to 6 V at 20 km/h: the backward reading of 20 km/h is 4 V.
CURVE = [(3.0, 10.0), (4.0, 20.0), (6.0, 20.0), (12.0, 80.0)]


def _profile():
    return SpeedProfile(
        voltage_speed_curve=list(CURVE),
        starting_voltage=2.0,
        ato_min_voltage=5.0,
        max_voltage=12.0,
    )


def _controller(speed_kmh, voltage_floor):
    command = PhysicsCommand(
        kind=PHYSICS_KIND_HANDLE,
        voltage_source_speed_kmh=speed_kmh,
        voltage_floor=voltage_floor,
    )
    return SimpleNamespace(powered=True, produce_physics_command=lambda: command)


def _physics():
    return TrainPhysics.__new__(TrainPhysics)


def test_a_moving_ato_train_on_a_flat_stretch_gets_its_ato_minimum_voltage():
    profile = _profile()
    assert profile.speed_to_voltage(20.0) == 4.0
    voltage = _physics().compute_track_voltage(_controller(20.0, 5.0), profile)
    assert voltage == 5.0


def test_above_the_floor_the_backward_reading_stands():
    profile = _profile()
    voltage = _physics().compute_track_voltage(_controller(50.0, 5.0), profile)
    assert voltage == profile.speed_to_voltage(50.0)
    assert voltage > 5.0


def test_a_standing_ato_train_is_not_raised():
    profile = _profile()
    voltage = _physics().compute_track_voltage(_controller(0.0, 5.0), profile)
    assert voltage == profile.speed_to_voltage(0.0)


def test_a_manual_train_has_no_floor():
    profile = _profile()
    voltage = _physics().compute_track_voltage(_controller(20.0, 0.0), profile)
    assert voltage == 4.0
