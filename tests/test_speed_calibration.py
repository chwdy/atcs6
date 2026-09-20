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
    CALIBRATION_CAP_RETRY_LIMIT,
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


def test_a_failed_pass_leaves_no_open_run_to_block_a_restart():
    """A measurement fault ends the pass AND removes its run from the registry,
    so a spent, failed pass never blocks a new one at the readiness gate
    (`services.train_service._require_calibration_ready` refuses a start while
    `runs.get(train_id)` is set). The `run` object the caller holds still carries
    `PHASE_FAILED` and its reason for inspection (design_new.md "Speed
    calibration", `fail`)."""
    cal, run, _profile = _open_run()
    _crossing(cal, run, 0.0, seconds=2.0, overrun_block="D")  # overrun -> fail
    assert run.phase == PHASE_FAILED  # the held object records the fault
    assert run.failure_reason  # and its reason, for the caller
    assert cal.runs.get("T1") is None  # but no open run lingers in the registry


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


# --- override drive while acquiring ------------------------------------------


def test_pass_drives_a_zero_override_while_acquiring():
    """A pass in the unarmed opening state puts a propulsion override on the
    train — a 0 while it acquires the leg route — so the controller drives the
    pass rather than the operator (design_new.md "Speed calibration", Where it
    runs)."""
    cal, run, _profile = _open_run()
    assert run.is_measuring
    override = cal.get_propulsion_override("T1")
    assert override is not None and override.kind == "power_percent"


# --- capped-crossing discard (design_new.md:3047, "A capped crossing is discarded") ---


def test_a_capped_crossing_is_discarded_and_the_pass_goes_on():
    """An external ATC restriction during a measured crossing taints it; the
    crossing is discarded rather than banked and the pass keeps going, leaving
    the profile's previous curve in place (design_new.md:3047)."""
    cal, run, profile = _open_run()
    curve_before = list(profile.power_speed_curve)
    # Arm the leg and enter the measured block so a crossing is being timed.
    cal.advance(run, CalibrationObservation(now=0.0))
    cal.advance(run, CalibrationObservation(now=1.0, newly_confirmed=("B",)))
    assert run.is_measuring
    assert run.clock_started_at is not None

    tainted = cal.taint_current_crossing(
        "T1", "CAUTION restriction during a measured crossing"
    )
    assert tainted is True
    assert run.current_crossing_tainted is True
    # Tainting never fails the pass.
    assert run.phase != PHASE_FAILED

    # The crossing completes at the far block: it is discarded (nothing banked),
    # the taint is spent, and the pass survives to re-measure the step.
    cal.advance(run, CalibrationObservation(now=3.0, newly_confirmed=("C",)))
    assert run.crossings == []
    assert run.current_crossing_tainted is False
    assert run.phase != PHASE_FAILED
    assert profile.power_speed_curve == curve_before


def test_a_stretch_that_caps_every_crossing_fails_the_pass():
    """When one step is capped on CALIBRATION_CAP_RETRY_LIMIT consecutive
    crossings — the stretch is too short for that step even at the zero
    reservation-end standoff — the pass fails and reports rather than shuttling
    forever (design_new.md, "A capped crossing is discarded")."""
    cal, run, profile = _open_run()
    curve_before = list(profile.power_speed_curve)
    clock = 0.0
    for i in range(CALIBRATION_CAP_RETRY_LIMIT):
        cal.advance(run, CalibrationObservation(now=clock))  # arm the leg
        far = run.heading_toward_block_id
        clock += 1.0
        cal.advance(run, CalibrationObservation(now=clock, newly_confirmed=("B",)))
        assert run.is_measuring
        cal.taint_current_crossing(
            "T1", "reservation-end braking during a measured crossing"
        )
        clock += 2.0
        cal.advance(run, CalibrationObservation(now=clock, newly_confirmed=(far,)))
        if i < CALIBRATION_CAP_RETRY_LIMIT - 1:
            # Under the limit: the crossing is discarded, the pass continues, and
            # the train stops so the next leg reverses.
            assert run.phase != PHASE_FAILED
            clock += 1.0
            cal.advance(run, CalibrationObservation(now=clock, is_stopped=True))

    # The limit-th consecutive capped crossing ends the pass with a report.
    assert run.phase == PHASE_FAILED
    assert "too short" in run.failure_reason
    # Nothing was banked; the profile's curve is untouched.
    assert run.samples == []
    assert profile.power_speed_curve == curve_before


def test_taint_current_crossing_ignores_a_train_with_no_open_pass():
    cal, _run, _profile = _open_run()
    # No open run for a train at all -> no-op.
    assert cal.taint_current_crossing("T_NONE", "caution") is False
