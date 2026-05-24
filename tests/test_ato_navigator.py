"""Unit tests for `ato_navigator` pure helpers (sample / notch / rate-limit).

These cover the static helpers that don't need a controller back-reference.
End-to-end ATO behaviour (`ATOComputer.compute_handle`) is exercised by
`tests/test_trains_ato.py` via `TrainController.tick()`.
"""

from enums import HandlePosition
from train_controller.ato import ATOComputer, rate_limit_handle


# ---------------------------------------------------------------------------
# sample_curve_at
# ---------------------------------------------------------------------------


def test_sample_curve_at_empty_returns_zero():
    assert ATOComputer.sample_curve_at([], 100.0) == 0.0


def test_sample_curve_at_before_first_returns_first():
    curve = [
        {"distance_cm": 10, "operational_limit": 30},
        {"distance_cm": 100, "operational_limit": 60},
    ]
    assert ATOComputer.sample_curve_at(curve, -5) == 30


def test_sample_curve_at_after_last_returns_last():
    curve = [
        {"distance_cm": 10, "operational_limit": 30},
        {"distance_cm": 100, "operational_limit": 60},
    ]
    assert ATOComputer.sample_curve_at(curve, 200) == 60


def test_sample_curve_at_interpolates_linearly():
    curve = [
        {"distance_cm": 0, "operational_limit": 0},
        {"distance_cm": 100, "operational_limit": 50},
    ]
    # Halfway between 0 and 100 → halfway between 0 and 50.
    assert ATOComputer.sample_curve_at(curve, 50) == 25


def test_sample_curve_at_supports_atc_limit_key():
    curve = [
        {"distance_cm": 0, "operational_limit": 30, "atc_limit": 60},
        {"distance_cm": 100, "operational_limit": 30, "atc_limit": 60},
    ]
    assert ATOComputer.sample_curve_at(curve, 50, key="atc_limit") == 60


# ---------------------------------------------------------------------------
# pick_brake_notch_for_multiplier
# ---------------------------------------------------------------------------


def test_pick_brake_notch_zero_or_negative_returns_neutral():
    fn = ATOComputer.pick_brake_notch_for_multiplier
    assert fn(0.0) == HandlePosition.N
    assert fn(-0.5) == HandlePosition.N


def test_pick_brake_notch_picks_smallest_sufficient():
    fn = ATOComputer.pick_brake_notch_for_multiplier
    # Multipliers (op_decel-relative): B1=0.20, B2=0.40, B3=0.55, B4=0.70,
    # B5=0.85, B6=1.00, B7=1.30, B8=1.70.
    assert fn(0.10) == HandlePosition.B1
    assert fn(0.20) == HandlePosition.B1
    assert fn(0.21) == HandlePosition.B2
    assert fn(0.55) == HandlePosition.B3
    assert fn(0.99) == HandlePosition.B6
    assert fn(1.00) == HandlePosition.B6
    assert fn(1.05) == HandlePosition.B7
    assert fn(1.30) == HandlePosition.B7
    assert fn(1.40) == HandlePosition.B8
    assert fn(1.70) == HandlePosition.B8


def test_pick_brake_notch_caps_at_b8():
    fn = ATOComputer.pick_brake_notch_for_multiplier
    # If more decel than B8 provides is requested, cap at B8 (ATC EB will
    # catch the resulting overspeed if it persists).
    assert fn(2.0) == HandlePosition.B8
    assert fn(99.0) == HandlePosition.B8


# ---------------------------------------------------------------------------
# calculate_braking_notch_for_speed (raw speed-gap helper for ATO-not-running)
# ---------------------------------------------------------------------------


def test_calculate_braking_notch_for_speed_no_gap_returns_neutral():
    fn = ATOComputer.calculate_braking_notch_for_speed
    assert fn(20.0, 20.0) == HandlePosition.N
    assert fn(15.0, 20.0) == HandlePosition.N


def test_calculate_braking_notch_for_speed_buckets():
    fn = ATOComputer.calculate_braking_notch_for_speed
    # Thresholds: >25=B8, >18=B7, >12=B6, >8=B5, >5=B4, >3=B3, >1=B2, else B1.
    assert fn(30.0, 0.0) == HandlePosition.B8
    assert fn(20.0, 0.0) == HandlePosition.B7
    assert fn(14.0, 0.0) == HandlePosition.B6
    assert fn(10.0, 0.0) == HandlePosition.B5
    assert fn(7.0, 0.0) == HandlePosition.B4
    assert fn(4.0, 0.0) == HandlePosition.B3
    assert fn(2.0, 0.0) == HandlePosition.B2
    assert fn(0.5, 0.0) == HandlePosition.B1


# ---------------------------------------------------------------------------
# Power-notch table
# ---------------------------------------------------------------------------


def test_calculate_power_notch_buckets():
    fn = ATOComputer.calculate_power_notch
    # Thresholds: >8=P5, >5=P4, >3=P3, >1.5=P2, else P1.
    assert fn(10.0, 60.0) == HandlePosition.P5
    assert fn(54.0, 60.0) == HandlePosition.P4
    assert fn(56.0, 60.0) == HandlePosition.P3
    assert fn(58.0, 60.0) == HandlePosition.P2
    assert fn(59.0, 60.0) == HandlePosition.P1


# ---------------------------------------------------------------------------
# rate_limit_handle (module-level)
# ---------------------------------------------------------------------------


def test_rate_limit_handle_eb_bypasses():
    assert (
        rate_limit_handle(HandlePosition.N, HandlePosition.EB, allow_fast=False)
        == HandlePosition.EB
    )


def test_rate_limit_handle_one_step_default():
    """Default mode allows ±1 notch per call."""
    # N (idx 5) → B3 (idx 8): 3 steps requested, only 1 allowed → B1 (idx 6)
    out = rate_limit_handle(HandlePosition.N, HandlePosition.B3, allow_fast=False)
    assert out == HandlePosition.B1


def test_rate_limit_handle_two_step_when_fast():
    """`allow_fast=True` allows ±2 notches per call."""
    # N (idx 5) → B3 (idx 8): 3 steps requested, 2 allowed → B2 (idx 7)
    out = rate_limit_handle(HandlePosition.N, HandlePosition.B3, allow_fast=True)
    assert out == HandlePosition.B2


def test_rate_limit_handle_no_change_when_already_at_target():
    out = rate_limit_handle(HandlePosition.B3, HandlePosition.B3, allow_fast=False)
    assert out == HandlePosition.B3


def test_rate_limit_handle_moves_toward_power_one_step():
    """B2 (idx 7) → N (idx 5): two steps requested, one allowed → B1 (idx 6)."""
    out = rate_limit_handle(HandlePosition.B2, HandlePosition.N, allow_fast=False)
    assert out == HandlePosition.B1
