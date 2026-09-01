"""C5 — Speed calibration: the shuttle pass, its state machine, and the commit.

design_new.md "Speed calibration". A pass sweeps `power_percent` in
`CALIBRATION_POWER_STEP_PERCENT` increments up to 100%, times the head across
one operator-named measured block between two confirmed block entries, turns the
train around at each end itself, and averages `CALIBRATION_ROUND_TRIPS` round
trips per step — half the crossings in each direction, so the stretch's grade
cancels.

These drive `advance(...)` with synthetic observations rather than a moving
simulation: the state machine is the contract, and feeding it confirmations
directly is what makes the stall band, the overrun, the re-measure, and the
reversal each testable on their own.
"""

import pytest

from constants import (
    CALIBRATION_POWER_STEP_PERCENT,
    CALIBRATION_ROUND_TRIPS,
    CALIBRATION_STEP_TIMEOUT_S,
)
from model_scale import cm_per_sec_to_kmh
from speed_calibration import (
    PHASE_FAILED,
    PHASE_MEASURED,
    CalibrationObservation,
    SpeedCalibration,
    power_sweep,
)
from speed_profile import make_default_speed_profile

BLOCK_LENGTH_CM = 100.0


class _StubTrain:
    train_id = "T1"


def _open_run(cal=None, **kwargs):
    profile = make_default_speed_profile("T1", 100.0)
    cal = cal or SpeedCalibration()
    run = cal.start(
        _StubTrain(),
        profile,
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        block_length_cm=BLOCK_LENGTH_CM,
        **kwargs,
    )
    return cal, run, profile


def _crossing(cal, run, clock, *, seconds, overrun_block=None):
    """Drive one whole leg: arm, enter B, enter the far block, then stop.

    Returns the advanced clock. `seconds` is how long the head takes to cross B,
    which is the only thing the measurement reads.
    """
    cal.advance(run, CalibrationObservation(now=clock))  # arm the leg
    far = run.heading_toward_block_id
    clock += 1.0
    cal.advance(run, CalibrationObservation(now=clock, newly_confirmed=("B",)))
    clock += seconds
    entered = (overrun_block,) if overrun_block else (far,)
    cal.advance(run, CalibrationObservation(now=clock, newly_confirmed=entered))
    if run.phase == PHASE_FAILED:
        return clock
    clock += 1.0
    cal.advance(run, CalibrationObservation(now=clock, is_stopped=True))
    return clock


def _run_full_pass(cal, run, *, seconds_for_step=None):
    """Measure every step. `seconds_for_step(power)` sets each crossing time."""
    clock = 0.0
    guard = 0
    while run.is_measuring and guard < 5000:
        guard += 1
        secs = (seconds_for_step or (lambda power: 2.0))(run.current_power)
        clock = _crossing(cal, run, clock, seconds=secs)
    return clock


# --- the sweep ---------------------------------------------------------------


def test_sweep_runs_in_power_increments_up_to_100():
    steps = power_sweep()
    assert steps[0] == float(CALIBRATION_POWER_STEP_PERCENT)
    assert steps[-1] == 100.0
    assert all(b - a == CALIBRATION_POWER_STEP_PERCENT for a, b in zip(steps, steps[1:]))


def test_zero_percent_is_the_origin_and_is_not_measured():
    # 0% power is lighting_voltage, where the train is lit and holds, so
    # (0%, 0 km/h) is the curve's origin by definition.
    cal, run, _profile = _open_run()
    assert 0.0 not in run.power_steps
    _run_full_pass(cal, run)
    assert cal.measured_curve(run)[0] == (0.0, 0.0)


# --- the timed crossing ------------------------------------------------------


def test_speed_comes_from_block_length_over_the_entry_to_entry_time():
    cal, run, _profile = _open_run()
    _crossing(cal, run, 0.0, seconds=4.0)
    expected = cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 4.0)
    assert run.crossings == [pytest.approx(expected)]


def test_only_entries_credited_to_this_train_move_the_clock():
    # The pass watches confirmed entries attribution credited to the calibration
    # train, so a block it never names cannot start its clock.
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))
    cal.advance(run, CalibrationObservation(now=1.0, newly_confirmed=()))
    assert run.clock_started_at is None


# --- the shuttle -------------------------------------------------------------


def test_the_pass_turns_the_train_around_at_each_end():
    cal, run, _profile = _open_run()
    assert run.heading_toward_block_id == "C"
    clock = _crossing(cal, run, 0.0, seconds=2.0)
    # Stopped at C; the next leg runs back toward A and flips the cab.
    assert run.heading_toward_block_id == "A"
    command = cal.advance(run, CalibrationObservation(now=clock))
    assert command.reverse_cab is True
    assert command.arm_leg_to == "A"


def test_a_leg_arms_a_one_off_route_to_its_far_end_and_finishes_without_a_route_op():
    # A leg without a route has no authority (atc_limit 0), so it arms a one-off
    # route to the far end. Reaching the far block finishes the leg through
    # ordinary stop completion, which tears the route down for it — so the
    # success command carries no route op (design_new.md "Speed calibration",
    # A leg finishes through ordinary stop completion).
    cal, run, _profile = _open_run()
    arm = cal.advance(run, CalibrationObservation(now=0.0))
    assert arm.arm_leg_to == "C"
    assert arm.power_percent == run.current_power
    cal.advance(
        run, CalibrationObservation(now=1.0, newly_confirmed=("B",), path_present=True)
    )
    stop = cal.advance(
        run, CalibrationObservation(now=3.0, newly_confirmed=("C",), path_present=True)
    )
    assert stop.deactivate_route is False
    assert stop.power_percent == 0.0


def test_a_step_averages_both_directions():
    cal, run, _profile = _open_run()
    # Alternate slow/fast legs — a grade makes one direction measure slow by
    # what it makes the other measure fast, and equal counts each way cancel it.
    clock = 0.0
    for seconds in (3.0, 1.0) * CALIBRATION_ROUND_TRIPS:
        clock = _crossing(cal, run, clock, seconds=seconds)
    assert run.step_index == 1
    slow = cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 3.0)
    fast = cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 1.0)
    assert run.samples[0].speed_kmh == pytest.approx((slow + fast) / 2)


def test_a_step_collects_two_crossings_per_round_trip():
    cal, run, _profile = _open_run()
    assert run.crossings_target == 2 * CALIBRATION_ROUND_TRIPS
    clock = 0.0
    for _ in range(run.crossings_target - 1):
        clock = _crossing(cal, run, clock, seconds=2.0)
    assert run.step_index == 0  # still on the first step
    _crossing(cal, run, clock, seconds=2.0)
    assert run.step_index == 1


# --- stall band, overrun, monotonicity ---------------------------------------


def test_a_step_that_does_not_move_the_train_records_zero():
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm
    # The leg's authority appears, starting the step clock (design_new.md "Speed
    # calibration": the timeout does not run until the path exists).
    cal.advance(run, CalibrationObservation(now=0.0, path_present=True))
    command = cal.advance(
        run,
        CalibrationObservation(now=CALIBRATION_STEP_TIMEOUT_S + 1.0, path_present=True),
    )
    assert run.samples[0].speed_kmh == 0.0
    assert run.step_index == 1
    # The train never left the outer block, so it is not turned around.
    assert run.needs_reverse is False
    # A stalled leg never reached the far block, so its route is torn down.
    assert command.deactivate_route is True


def test_a_leg_that_starts_crossing_and_never_arrives_fails_the_pass():
    # A part-crossing is not a slow measurement, it is no measurement: timing it
    # would put a speed on the curve the locomotive never held.
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm
    cal.advance(
        run, CalibrationObservation(now=1.0, newly_confirmed=("B",), path_present=True)
    )
    cal.advance(
        run,
        CalibrationObservation(
            now=1.0 + CALIBRATION_STEP_TIMEOUT_S + 1.0, path_present=True
        ),
    )
    assert run.phase == PHASE_FAILED
    assert "never reached" in run.failure_reason


def test_a_sweep_in_which_nothing_moved_fails_rather_than_storing_a_dead_curve():
    # All-zero is not a locomotive that cannot move; it is a stretch the pass
    # could not measure on. Storing it would say the loco stands still at 100%.
    cal, run, profile = _open_run()
    clock = 0.0
    for _ in range(len(run.power_steps)):
        cal.advance(run, CalibrationObservation(now=clock))  # arm
        # authority appears, starting the step clock
        cal.advance(run, CalibrationObservation(now=clock, path_present=True))
        clock += CALIBRATION_STEP_TIMEOUT_S + 1.0
        cal.advance(
            run, CalibrationObservation(now=clock, path_present=True)
        )  # times out
    assert run.phase == PHASE_FAILED
    assert "no step moved the train" in run.failure_reason
    assert profile.calibrated_at is None


def test_a_leg_that_overruns_the_stretch_fails_the_pass():
    cal, run, _profile = _open_run()
    _crossing(cal, run, 0.0, seconds=2.0, overrun_block="D")
    assert run.phase == PHASE_FAILED
    assert "overran" in run.failure_reason


def test_a_slower_step_is_measured_once_more_then_fails():
    cal, run, _profile = _open_run()
    # Step 1 fast, step 2 slow twice: the re-measure tells a noisy crossing
    # apart from a locomotive that really does run slower at more power.
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)
    assert run.step_index == 1
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)
    assert run.step_remeasured is True
    assert run.step_index == 1  # repeated, not banked
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)
    assert run.phase == PHASE_FAILED
    assert "slower" in run.failure_reason


def test_a_session_stop_abandons_the_pass():
    cal, run, _profile = _open_run(safe_state_generation=1)
    command = cal.advance(
        run, CalibrationObservation(now=0.0, safe_state_generation=2)
    )
    assert run.phase == PHASE_FAILED
    assert command.power_percent == 0.0


# --- commit ------------------------------------------------------------------


def test_measuring_every_step_does_not_commit():
    cal, run, profile = _open_run()
    before = list(profile.power_speed_curve)
    _run_full_pass(cal, run)
    assert run.phase == PHASE_MEASURED
    assert profile.power_speed_curve == before
    assert profile.calibrated_at is None


def test_commit_writes_the_curve_and_stamp_and_nothing_else():
    cal, run, profile = _open_run()
    voltage_curve_before = list(profile.voltage_power_curve)
    accel_before = profile.acceleration_rate
    model_before = profile.model
    _run_full_pass(cal, run)
    curve = cal.commit("T1", profile)

    assert profile.power_speed_curve == curve
    assert profile.calibrated_at is not None
    # Commit writes power_speed_curve and calibrated_at only (design_new.md
    # "What commit writes"): `model` is edited on the model profile, not here;
    # Lighting-voltage calibration is the one procedure that moves the first
    # stage; and a motion rate describes behaviour rather than measured speed.
    assert profile.model == model_before
    assert profile.voltage_power_curve == voltage_curve_before
    assert profile.acceleration_rate == accel_before
    assert "T1" not in cal.runs


def test_commit_is_all_or_nothing():
    cal, run, profile = _open_run()
    _crossing(cal, run, 0.0, seconds=2.0)
    assert not run.is_measured
    with pytest.raises(ValueError):
        cal.commit("T1", profile)
    assert profile.calibrated_at is None


def test_the_committed_curve_is_non_decreasing_so_it_reads_backwards():
    cal, run, profile = _open_run()
    # Faster at every higher step, which is what an ordinary locomotive does.
    _run_full_pass(cal, run, seconds_for_step=lambda power: 100.0 / power)
    curve = cal.commit("T1", profile)
    speeds = [s for _p, s in curve]
    assert speeds == sorted(speeds)
    # And physics can read it back: every measured speed maps to a power.
    for power, speed in curve:
        assert profile._speed_to_power(speed) == pytest.approx(power, abs=1e-6)


def test_commit_rejects_a_curve_that_falls_with_power():
    cal, run, profile = _open_run()
    _run_full_pass(cal, run)
    run.samples[-1] = type(run.samples[-1])(power_percent=100.0, speed_kmh=0.0)
    with pytest.raises(ValueError):
        cal.commit("T1", profile)


# --- manual marking ----------------------------------------------------------


def test_manual_marks_pair_up_into_crossings():
    cal, run, _profile = _open_run(mode="manual", marker_spacing_cm=200.0)
    cal.record("T1", now=0.0)  # opens
    cal.record("T1", now=4.0)  # closes
    assert run.crossings == [pytest.approx(cm_per_sec_to_kmh(200.0 / 4.0))]
    # The reversal dwell falls between pairs, so it is never timed.
    cal.record("T1", now=30.0)
    assert len(run.crossings) == 1


def test_manual_marking_leaves_the_operators_manual_power_intact():
    """design_new.md "Speed calibration", Manual marking: "The operator drives the
    shuttle in this mode" and runs the train at the step's commanded power_percent.

    So a measuring manual pass must put no propulsion override on the train:
    `get_propulsion_override` returns None, leaving the operator's own manual power
    in control. Returning a `power_percent = 0` override instead would replace that
    manual power with zero (train_controller reads a 0 override as "calibration is
    driving at 0%") and pin the train still, making the drive-it-yourself pass
    impossible. An auto pass in the same unarmed opening state, by contrast, does
    put an override on the train — the asymmetry this pass must preserve."""
    manual_cal, manual_run, _p = _open_run(mode="manual", marker_spacing_cm=200.0)
    assert manual_run.is_measuring  # the pass is live and would otherwise drive
    assert manual_cal.get_propulsion_override("T1") is None

    # Auto profiling in the same unarmed opening state DOES drive (a 0 while it
    # acquires the leg): the override the manual pass must not emit.
    auto_cal, _auto_run, _p2 = _open_run(mode="auto")
    auto_override = auto_cal.get_propulsion_override("T1")
    assert auto_override is not None and auto_override.kind == "power_percent"


# --- braking-override abandonment (design_new.md "A braking override voids the pass") ---


def test_braking_override_abandons_the_pass_and_keeps_the_curve():
    """A braking override during a measured crossing abandons the pass, reports
    the reason, and leaves the profile's previous curve in place (design_new.md
    "Speed calibration", A braking override voids the pass)."""
    cal, run, profile = _open_run()
    curve_before = list(profile.power_speed_curve)
    # Arm the leg and enter the measured block so a crossing is in progress.
    cal.advance(run, CalibrationObservation(now=0.0))
    cal.advance(run, CalibrationObservation(now=1.0, newly_confirmed=("B",)))
    assert run.is_measuring

    abandoned = cal.abandon_speed_pass("T1", "emergency brake during a measured crossing")
    assert abandoned is True
    assert run.phase == PHASE_FAILED
    assert "emergency brake" in run.failure_reason
    # A pass that does not measure every step writes nothing to the profile.
    assert profile.power_speed_curve == curve_before

    # Idempotent: a second abandon (or one on a finished/failed pass) is a no-op.
    assert cal.abandon_speed_pass("T1", "again") is False


def test_abandon_speed_pass_ignores_a_non_measuring_or_manual_pass():
    # A manual pass drives nothing and is never abandoned by this path.
    cal, _run, _profile = _open_run(mode="manual")
    assert cal.abandon_speed_pass("T1", "override") is False
    # No open run for a train at all -> no-op.
    assert cal.abandon_speed_pass("T_NONE", "override") is False
