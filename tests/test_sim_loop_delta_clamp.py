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
    """End-to-end: feeding update() one giant delta corrupts the cruise, while
    the clamp the loop applies keeps trains cruising at or below their max.

    A train has finite movement authority, so a giant unclamped step teleports
    its head far past that authority in a single integration and the ATC halts
    it -- the cruise collapses well below where a clamped step leaves it. (The
    same overshoot trips a spurious overspeed EB instead when block authority is
    degenerate; the clamp prevents either failure mode.) The clamp keeps each
    step sub-second, so the cruise continues normally and never spikes past max.
    """
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

    # Both sims are identical up to here; require a real cruise so the contrast
    # below is not vacuous.
    cruising = max_speed(net_b, sim_b)
    assert cruising > 10.0, (
        f"scenario not exercised: no train cruising after warmup (max {cruising:.2f} "
        "km/h) -- the regression guard would be vacuous"
    )

    # Clamped (what the loop feeds update()): a sub-second step, so the cruise
    # continues and never spikes past the train's max -- the property the clamp
    # buys.
    sim_b.update(_clamp_sim_delta(18.0, 1.0))
    clamped = max_speed(net_b, sim_b)
    assert 0.0 < clamped <= max_train_speed, (
        f"clamped step should keep trains cruising at/under max, got {clamped:.2f}"
    )

    # Unclamped single giant step: integrated whole, the head teleports past its
    # movement authority and the ATC halts the train, so the cruise collapses
    # below where it was running (and below the clamped result).
    sim_a.update(18.0)
    unclamped = max_speed(net_a, sim_a)
    assert unclamped < cruising, (
        f"unclamped giant delta should corrupt the cruise, got {unclamped:.2f} "
        f"km/h (was cruising at {cruising:.2f})"
    )
