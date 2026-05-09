"""Unit tests for `ato_navigator` pure helpers (sample / notch / rate-limit).

These cover the static helpers that don't need a controller back-reference.
End-to-end ATO behaviour (`ATONavigator.compute_handle`) is exercised by
`tests/test_trains_ato.py` via `TrainController.tick()`.
"""

from enums import HandlePosition
from ato_navigator import ATONavigator, rate_limit_handle


# ---------------------------------------------------------------------------
# sample_curve_at
# ---------------------------------------------------------------------------


def test_sample_curve_at_empty_returns_zero():
    assert ATONavigator.sample_curve_at([], 100.0) == 0.0


def test_sample_curve_at_before_first_returns_first():
    curve = [
        {"distance_cm": 10, "operational_limit": 30},
        {"distance_cm": 100, "operational_limit": 60},
    ]
    assert ATONavigator.sample_curve_at(curve, -5) == 30


def test_sample_curve_at_after_last_returns_last():
    curve = [
        {"distance_cm": 10, "operational_limit": 30},
        {"distance_cm": 100, "operational_limit": 60},
    ]
    assert ATONavigator.sample_curve_at(curve, 200) == 60


def test_sample_curve_at_interpolates_linearly():
    curve = [
        {"distance_cm": 0, "operational_limit": 0},
        {"distance_cm": 100, "operational_limit": 50},
    ]
    # Halfway between 0 and 100 → halfway between 0 and 50.
    assert ATONavigator.sample_curve_at(curve, 50) == 25


def test_sample_curve_at_supports_atc_limit_key():
    curve = [
        {"distance_cm": 0, "operational_limit": 30, "atc_limit": 60},
        {"distance_cm": 100, "operational_limit": 30, "atc_limit": 60},
    ]
    assert ATONavigator.sample_curve_at(curve, 50, key="atc_limit") == 60


# ---------------------------------------------------------------------------
# Braking-notch tables
# ---------------------------------------------------------------------------


def test_calculate_braking_notch_buckets():
    notch = ATONavigator.calculate_braking_notch
    assert notch(40.0) == HandlePosition.B8
    assert notch(28.0) == HandlePosition.B7
    assert notch(22.0) == HandlePosition.B6
    assert notch(15.0) == HandlePosition.B5
    assert notch(11.0) == HandlePosition.B4
    assert notch(7.0) == HandlePosition.B3
    assert notch(3.0) == HandlePosition.B2
    assert notch(1.0) == HandlePosition.B1


def test_calculate_braking_notch_for_speed_uses_diff():
    fn = ATONavigator.calculate_braking_notch_for_speed
    # 50 km/h current, 10 target → diff 40 → B8
    assert fn(50.0, 10.0) == HandlePosition.B8
    # 30 km/h current, 25 target → diff 5 (not > 5) → B2
    assert fn(30.0, 25.0) == HandlePosition.B2


# ---------------------------------------------------------------------------
# Power-notch table
# ---------------------------------------------------------------------------


def test_calculate_power_notch_buckets():
    fn = ATONavigator.calculate_power_notch
    # Far below target: full power
    assert fn(10.0, 60.0) == HandlePosition.P5
    # Within fine-control zone: graduated
    assert fn(56.0, 60.0) == HandlePosition.P2
    assert fn(58.5, 60.0) == HandlePosition.P1


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
