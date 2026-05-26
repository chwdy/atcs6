"""ETA calculation tests for TrainController._calculate_time_to_target_sec.

Covers the two regressions the redesign fixes:
1. From-stop acceleration is integrated (not ignored), so ETA reflects the
   real time to reach curve ceiling when the train is below it.
2. Sparse curves still produce an ETA via the kinematic fallback when a
   real target_stop_cm is published.

Run with: pytest tests/test_eta_calculation.py -v
"""

import math
import pytest

from model_scale import get_speed_conversion_factor
from network import RailwayNetwork
from train_controller import TrainController


CM_PER_SECOND_PER_KMH = get_speed_conversion_factor()  # model cm/s per prototype km/h


def _make_controller(current_speed_kmh: float = 0.0, power_on: bool = True) -> TrainController:
    from enums import TrainState
    network = RailwayNetwork()
    controller = TrainController(train_id="TRAIN_ETA", network=network)
    # Per design_new.md: power_on is a read-only derived property; use
    # controller.ops.state to power on/off.
    if power_on:
        controller.ops.state = TrainState.READY
    else:
        controller.ops.state = TrainState.OFF
    controller.physics_sync.current_speed_kmh = current_speed_kmh
    return controller


def _stub_lookahead(controller: TrainController, *, target_stop_cm: float, samples):
    """Replace _compute_canonical_lookahead with a fixed payload.

    ``samples`` is a list of (distance_cm, operational_limit_kmh) tuples.
    """
    payload = {
        "lookahead": {
            "target_stop_cm": target_stop_cm,
            "speed_curves": [
                {"distance_cm": d, "operational_limit": v} for d, v in samples
            ],
        }
    }
    controller._compute_canonical_lookahead = lambda: payload


class TestEtaIntegration:
    def test_stopped_train_accounts_for_acceleration(self):
        # Train at 0 km/h, curve allows 60 km/h flat for 10 000 cm (100 m).
        controller = _make_controller(current_speed_kmh=0.0)
        _stub_lookahead(
            controller,
            target_stop_cm=10000.0,
            samples=[(0.0, 60.0), (10000.0, 60.0)],
        )

        eta = controller._calculate_time_to_target_sec()
        assert eta is not None

        # Naive "at curve speed" ETA = 10000 cm / (60 km/h × 27.78) ≈ 6.0 s.
        # Real ETA must be noticeably longer because train accelerates from 0.
        naive = 10000.0 / (60.0 * CM_PER_SECOND_PER_KMH)
        assert eta > naive * 1.5, (
            f"Stopped-train ETA={eta:.2f}s should be >> naive {naive:.2f}s "
            "(the old integration returned the naive value)."
        )

    def test_train_already_at_curve_speed_matches_naive(self):
        # Train at 60 km/h, curve allows 60 km/h flat for 10 000 cm.
        controller = _make_controller(current_speed_kmh=60.0)
        _stub_lookahead(
            controller,
            target_stop_cm=10000.0,
            samples=[(0.0, 60.0), (10000.0, 60.0)],
        )

        eta = controller._calculate_time_to_target_sec()
        naive = 10000.0 / (60.0 * CM_PER_SECOND_PER_KMH)
        assert eta is not None
        # When already at ceiling the integration should match the naive
        # estimate closely (within 5%).
        assert math.isclose(eta, naive, rel_tol=0.05)

    def test_powered_off_returns_none(self):
        controller = _make_controller(current_speed_kmh=0.0, power_on=False)
        _stub_lookahead(
            controller,
            target_stop_cm=10000.0,
            samples=[(0.0, 60.0), (10000.0, 60.0)],
        )
        assert controller._calculate_time_to_target_sec() is None

    def test_no_target_returns_none(self):
        controller = _make_controller(current_speed_kmh=20.0)
        _stub_lookahead(
            controller,
            target_stop_cm=0.0,
            samples=[(0.0, 60.0), (10000.0, 60.0)],
        )
        assert controller._calculate_time_to_target_sec() is None


class TestEtaSparseCurveFallback:
    def test_single_sample_with_target_uses_kinematic_fallback(self):
        # Only one curve sample is published but target_stop_cm is real.
        # The old code returned None and the UI showed "—".
        controller = _make_controller(current_speed_kmh=0.0)
        _stub_lookahead(
            controller,
            target_stop_cm=2000.0,  # 20 m
            samples=[(0.0, 60.0)],
        )

        eta = controller._calculate_time_to_target_sec()
        assert eta is not None
        assert eta > 0

        # Sanity check the kinematic estimate: v = sqrt(2·a·d).
        # a = _ETA_ACCEL_CM_PER_S2 = 0.5 model cm/s²
        # v = sqrt(2·0.5·2000) = sqrt(2000) ≈ 44.7 model cm/s. Time = v / a ≈ 89.4 s.
        a = TrainController._ETA_ACCEL_CM_PER_S2
        expected = math.sqrt(2.0 * a * 2000.0) / a
        assert math.isclose(eta, expected, rel_tol=0.05)

    def test_empty_curves_still_returns_eta_with_target(self):
        controller = _make_controller(current_speed_kmh=10.0)
        _stub_lookahead(
            controller,
            target_stop_cm=5000.0,
            samples=[],
        )
        eta = controller._calculate_time_to_target_sec()
        assert eta is not None
        assert eta > 0


class TestKinematicHelperBoundary:
    def test_negative_target_returns_none(self):
        controller = _make_controller()
        assert controller._kinematic_time_to_target_sec(-10.0, 0.0) is None

    def test_zero_target_returns_none(self):
        controller = _make_controller()
        assert controller._kinematic_time_to_target_sec(0.0, 0.0) is None
