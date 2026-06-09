"""Phase 1 (Tier 2): notch response is a per-train SpeedProfile attribute.

`power_multipliers` / `brake_multipliers` default to the standard table, survive
serialization, and can be overridden per profile. TrainPhysics reads the passed
table so a profile override changes that train's notch response.
"""

from enums import BRAKE_MULTIPLIERS, POWER_MULTIPLIERS, HandlePosition
from speed_profile import SpeedProfile


def test_default_profile_has_standard_notch_tables():
    p = SpeedProfile(profile_id="t", name="T")
    assert p.power_multipliers == POWER_MULTIPLIERS
    assert p.brake_multipliers == BRAKE_MULTIPLIERS
    # a copy, not the shared module dict
    assert p.brake_multipliers is not BRAKE_MULTIPLIERS


def test_notch_tables_round_trip_and_override():
    p = SpeedProfile(profile_id="t", name="T")
    p.brake_multipliers[HandlePosition.B8] = 2.5
    p.power_multipliers[HandlePosition.P5] = 1.2
    restored = SpeedProfile.from_dict(p.to_dict())
    assert restored.brake_multipliers[HandlePosition.B8] == 2.5
    assert restored.power_multipliers[HandlePosition.P5] == 1.2


def test_missing_notch_tables_fall_back_to_standard():
    p = SpeedProfile.from_dict({"profile_id": "x", "name": "y"})
    assert p.brake_multipliers == BRAKE_MULTIPLIERS
    assert p.power_multipliers == POWER_MULTIPLIERS


def test_physics_acceleration_honors_passed_brake_table():
    from train_physics import TrainPhysics

    # B8 deceleration scales with the supplied brake table, not the module one.
    op_decel = 12.0
    standard = TrainPhysics._get_acceleration_for_handle(
        None, HandlePosition.B8, 10.0, op_decel, 30.0
    )
    custom = TrainPhysics._get_acceleration_for_handle(
        None,
        HandlePosition.B8,
        10.0,
        op_decel,
        30.0,
        power_multipliers=POWER_MULTIPLIERS,
        brake_multipliers={**BRAKE_MULTIPLIERS, HandlePosition.B8: 2.0},
    )
    assert standard == -op_decel * BRAKE_MULTIPLIERS[HandlePosition.B8]
    assert custom == -op_decel * 2.0
