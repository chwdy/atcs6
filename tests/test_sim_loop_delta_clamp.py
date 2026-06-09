"""Regression: the real-time sim loop must clamp its per-tick delta.

The loop integrates wall-clock time x `simulation_speed` (runtime.py). Normal
ticks — even with a few-times speed multiplier — are small and pass through
unchanged (one update() per tick). But if the loop stalls (lock contention, GC,
a host that fell behind) it resumes with a multi-second accumulated delta;
integrated in one step, `new_speed = previous + accel*delta` overshoots the
train's max speed and the head teleports past its authority, tripping a spurious
ATC overspeed emergency brake. `_clamp_sim_delta` caps that.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime
from runtime import MAX_SIM_DELTA_SECONDS, _clamp_sim_delta


def test_normal_ticks_pass_through_unclamped():
    # ~0.13s wall-clock per tick at 1x, and the same x a few-times multiplier,
    # stay well under the ceiling -> untouched, so the loop runs one update/tick.
    assert _clamp_sim_delta(0.13, 1.0) == pytest.approx(0.13)
    assert _clamp_sim_delta(0.13, 3.0) == pytest.approx(0.39)
    assert _clamp_sim_delta(0.2, 3.0) == pytest.approx(0.6)  # formerly-"big" 0.6s: fine


def test_clock_skew_floored_at_zero():
    assert _clamp_sim_delta(-5.0, 1.0) == 0.0


def test_giant_hitch_delta_is_clamped():
    # A multi-second stall (optionally amplified by the speed multiplier) is
    # capped at the ceiling, never integrated whole.
    assert _clamp_sim_delta(18.0, 1.0) == MAX_SIM_DELTA_SECONDS
    assert _clamp_sim_delta(6.0, 3.0) == MAX_SIM_DELTA_SECONDS
    assert MAX_SIM_DELTA_SECONDS <= 3.0  # stays below the ~3.5s EB threshold


def test_clamped_giant_delta_does_not_spike_speed_past_max():
    """End-to-end: a giant delta integrated whole overshoots the train's max;
    clamped (as the loop now does) it stays at or below max."""
    with contextlib.redirect_stdout(io.StringIO()):
        net_a, _da, sim_a, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
        net_b, _db, sim_b, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )

    def max_speed(net, sim):
        return max(
            (
                sim.get_or_create_controller(t).physics_sync.current_speed_kmh
                for t in net.trains
            ),
            default=0.0,
        )

    max_train_speed = 40.0  # demo_6 stock
    for _ in range(50):
        sim_a.update(0.2)
        sim_b.update(0.2)

    # Unclamped single giant step: speed overshoots the train's max (the bug).
    sim_a.update(18.0)
    assert max_speed(net_a, sim_a) > max_train_speed

    # Clamped (what the loop feeds update()): stays at or below max.
    sim_b.update(_clamp_sim_delta(18.0, 1.0))
    assert max_speed(net_b, sim_b) <= max_train_speed
