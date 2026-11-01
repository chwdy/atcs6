"""C5 — Speed calibration: the shuttle pass, its state machine, and the commit.

design_new.md "Speed calibration". A pass sweeps `power_percent` down from 100%
in `CALIBRATION_POWER_STEP_PERCENT` steps, times the head across
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
    CALIBRATION_MIN_CROSSING_SPEED_KMH,
    CALIBRATION_POWER_STEP_PERCENT,
    CALIBRATION_ROUND_TRIPS,
    CALIBRATION_STEP_TIMEOUT_S,
)
from model_scale import cm_per_sec_to_kmh, kmh_to_cm_per_sec
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


def _observe(cal, run, now, entered=(), **observation):
    """One tick: Observe records the credited entries on the run, then Plan
    step `c9e1` advances the pass (design_new.md "Speed calibration",
    `CalibrationRun`)."""
    if entered:
        cal.record_detected_entries(run, entered, now)
    return cal.advance(run, CalibrationObservation(now=now, **observation))


def _crossing(cal, run, clock, *, seconds, overrun_block=None):
    """Drive one whole leg: arm, enter B, enter the far block, then stop.

    Returns the advanced clock. `seconds` is how long the head takes to cross B,
    which is the only thing the measurement reads.
    """
    cal.advance(run, CalibrationObservation(now=clock))  # arm the leg
    far = run.heading_toward_block_id
    clock += 1.0
    _observe(cal, run, clock, ("B",))
    clock += seconds
    entered = (overrun_block,) if overrun_block else (far,)
    _observe(cal, run, clock, entered)
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


def test_the_sweep_runs_down_from_100():
    steps = power_sweep()
    assert steps[0] == 100.0
    assert steps[-1] == float(CALIBRATION_POWER_STEP_PERCENT)
    assert all(a - b == CALIBRATION_POWER_STEP_PERCENT for a, b in zip(steps, steps[1:]))


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
    _observe(cal, run, 1.0, ())
    assert run.crossing_started_at is None


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
    _observe(cal, run, 1.0, ("B",), path_present=True)
    stop = _observe(cal, run, 3.0, ("C",), path_present=True)
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


def _measure_top_step(cal, run, *, seconds=2.0):
    """Measure the 100% step cleanly; return the clock with the next step due."""
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=seconds)
    assert run.current_power == 100.0 - CALIBRATION_POWER_STEP_PERCENT
    return clock


def _window_s():
    return BLOCK_LENGTH_CM / kmh_to_cm_per_sec(CALIBRATION_MIN_CROSSING_SPEED_KMH)


def test_a_step_that_does_not_move_the_train_ends_the_sweep():
    """design_new.md "Speed calibration", A step that does not move the train
    records `0 km/h`: that step and every step below it record 0 km/h without
    being commanded, and the pass is finished."""
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    cal.advance(run, CalibrationObservation(now=clock))  # arm the 90% leg
    # The leg's authority appears, starting the step clock (design_new.md "Speed
    # calibration": the timeout does not run until the path exists).
    cal.advance(run, CalibrationObservation(now=clock, path_present=True))
    command = cal.advance(
        run,
        CalibrationObservation(
            now=clock + CALIBRATION_STEP_TIMEOUT_S + 1.0, path_present=True
        ),
    )
    assert run.phase == PHASE_MEASURED
    assert [s.power_percent for s in run.samples] == run.power_steps
    assert run.samples[0].speed_kmh > 0.0
    assert all(s.speed_kmh == 0.0 for s in run.samples[1:])
    # The stalled leg never reached the far block, so its route is torn down.
    assert command.deactivate_route is True
    assert command.arm_leg_to is None
    # No step below the stall is commanded: the pass no longer drives.
    assert cal.get_propulsion_override("T1") is None
    assert "did not move at 90% and below" in cal._measured_reason(run)


def test_a_crossing_that_stops_inside_the_stretch_ends_the_sweep():
    """design_new.md "Speed calibration", A crossing that stops inside the
    stretch ends the sweep: a part-crossing is no measurement, and the power that
    did not carry the train across B is the top edge of the stall band."""
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    cal.advance(run, CalibrationObservation(now=clock))  # arm the 90% leg
    _observe(cal, run, clock + 1.0, ("B",), path_present=True)
    entered_b = clock + 1.0
    # Still inside the window: the pass keeps waiting for the far block.
    cal.advance(run, CalibrationObservation(now=entered_b + _window_s() - 1.0, path_present=True))
    assert run.is_measuring
    command = cal.advance(
        run, CalibrationObservation(now=entered_b + _window_s() + 1.0, path_present=True)
    )
    assert run.phase == PHASE_MEASURED
    assert [s.speed_kmh for s in run.samples[1:]] == [0.0] * (len(run.power_steps) - 1)
    assert command.deactivate_route is True


def test_a_stall_drops_the_crossings_its_step_already_measured():
    """A step whose power moved the train on one crossing but not on the next
    records 0 km/h, whatever crossings it already measured."""
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    clock = _crossing(cal, run, clock, seconds=2.0)  # one clean 90% crossing
    assert len(run.crossings) == 1
    cal.advance(run, CalibrationObservation(now=clock))  # arm the leg back
    cal.advance(run, CalibrationObservation(now=clock, path_present=True))
    cal.advance(run, CalibrationObservation(
        now=clock + CALIBRATION_STEP_TIMEOUT_S + 1.0, path_present=True))
    assert run.phase == PHASE_MEASURED
    assert run.samples[1].power_percent == 90.0
    assert run.samples[1].speed_kmh == 0.0


@pytest.mark.parametrize("enters_b", [False, True])
def test_a_sweep_in_which_every_step_records_zero_fails(enters_b):
    """design_new.md "Speed calibration", A sweep in which every step records
    `0 km/h` fails the pass: all-zero is a stretch the pass could not measure on,
    and storing it would say the loco stands still at 100%. The 100% step
    stalling settles every step below it, so the pass fails at once."""
    cal, run, profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm
    if enters_b:
        _observe(cal, run, 1.0, ("B",), path_present=True)
        timed_out = 1.0 + _window_s() + 1.0
    else:
        cal.advance(run, CalibrationObservation(now=0.0, path_present=True))
        timed_out = CALIBRATION_STEP_TIMEOUT_S + 1.0
    command = cal.advance(run, CalibrationObservation(now=timed_out, path_present=True))
    assert run.phase == PHASE_FAILED
    assert "every step recorded 0 km/h" in run.failure_reason
    assert command.deactivate_route is True
    assert profile.calibrated_at is None


def test_time_standing_short_of_the_reservation_is_not_counted():
    """design_new.md "Speed calibration", Time the train stands short of its
    reservation is not counted: another train's claim holding the leg short
    delays the step instead of recording a false stall."""
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm
    cal.advance(run, CalibrationObservation(now=0.0, path_present=True))
    held = 3 * CALIBRATION_STEP_TIMEOUT_S
    cal.advance(run, CalibrationObservation(now=held, path_present=True, held_short=True))
    assert run.is_measuring
    assert run.samples == []
    # Released: the wait for B counts again, from where it left off.
    cal.advance(run, CalibrationObservation(
        now=held + CALIBRATION_STEP_TIMEOUT_S - 1.0, path_present=True))
    assert run.is_measuring
    cal.advance(run, CalibrationObservation(
        now=held + CALIBRATION_STEP_TIMEOUT_S + 1.0, path_present=True))
    assert run.phase == PHASE_FAILED  # the 100% step: every step records 0


def test_a_crossing_held_short_is_not_taken_for_a_stall():
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm
    far = run.heading_toward_block_id
    _observe(cal, run, 1.0, ("B",), path_present=True)
    late = 1.0 + 2 * _window_s()
    cal.advance(run, CalibrationObservation(now=late, path_present=True, held_short=True))
    assert run.is_measuring
    _observe(cal, run, late + 1.0, (far,), path_present=True)
    assert len(run.crossings) == 1


def test_a_leg_that_overruns_the_stretch_fails_the_pass():
    cal, run, _profile = _open_run()
    _crossing(cal, run, 0.0, seconds=2.0, overrun_block="D")
    assert run.phase == PHASE_FAILED
    assert "overran" in run.failure_reason


def test_an_overrun_while_the_train_stops_fails_the_pass():
    """The stopping half is still the leg: a block past the far outer block
    confirming while the train brakes fails the pass (design_new.md "Speed
    calibration", A leg that overruns the stretch fails the pass)."""
    cal, run, _profile = _open_run()
    _observe(cal, run, 0.0)  # arm
    far = run.heading_toward_block_id
    _observe(cal, run, 1.0, ("B",))
    _observe(cal, run, 3.0, (far,))
    assert run.phase != PHASE_FAILED  # crossing measured, now stopping

    _observe(cal, run, 3.5, ("D",), is_stopped=False)

    assert run.phase == PHASE_FAILED
    assert "overran" in run.failure_reason


def test_an_overrun_confirmed_after_the_stop_still_fails_the_pass():
    """Detector lag can confirm the overrun block after the train stands; the
    record survives until the next leg arms, so the pass still fails."""
    cal, run, _profile = _open_run()
    _observe(cal, run, 0.0)  # arm
    far = run.heading_toward_block_id
    _observe(cal, run, 1.0, ("B",))
    _observe(cal, run, 3.0, (far,))
    _observe(cal, run, 4.0, is_stopped=True)  # stopped: next tick re-arms
    assert run.phase != PHASE_FAILED

    _observe(cal, run, 4.5, ("D",))

    assert run.phase == PHASE_FAILED


def test_observe_records_the_crossing_and_the_overrun_on_the_run():
    cal, run, _profile = _open_run()
    cal.record_detected_entries(run, ("B",), 1.0)
    assert run.crossing_started_at is None  # the leg is not armed yet

    _observe(cal, run, 1.0)  # arm
    far = run.heading_toward_block_id
    cal.record_detected_entries(run, ("B",), 2.0)
    cal.record_detected_entries(run, (far,), 4.5)
    cal.record_detected_entries(run, ("D",), 5.0)

    assert run.crossing_started_at == 2.0
    assert run.crossing_finished_at == 4.5
    assert run.overrun_seen is True


def test_a_failed_pass_leaves_no_open_run_to_block_a_restart():
    """A measurement fault ends the pass AND removes its run from the registry,
    so a spent, failed pass never blocks a new one at the readiness check
    (`services.train_service.calibration_readiness` refuses a start while
    `runs.get(train_id)` is set). The `run` object the caller holds still carries
    `PHASE_FAILED` and its reason for inspection (design_new.md "Speed
    calibration", `fail`)."""
    cal, run, _profile = _open_run()
    _crossing(cal, run, 0.0, seconds=2.0, overrun_block="D")  # overrun -> fail
    assert run.phase == PHASE_FAILED  # the held object records the fault
    assert run.failure_reason  # and its reason, for the caller
    assert cal.runs.get("T1") is None  # but no open run lingers in the registry


def test_a_step_faster_than_the_step_above_it_is_measured_once_more_then_fails():
    cal, run, _profile = _open_run()
    # 100% slow, then 90% fast twice: the re-measure tells a noisy crossing
    # apart from a locomotive that really does run faster at less power
    # (design_new.md "Speed calibration", A step that measures faster than the
    # step above it is measured once more).
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)
    assert run.step_index == 1
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)
    assert run.step_remeasured is True
    assert run.step_index == 1  # repeated, not banked
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)
    assert run.phase == PHASE_FAILED
    assert "faster" in run.failure_reason


def test_ending_open_runs_drops_an_open_pass():
    """A session stop ends an open pass (design_new.md "What ends a calibration
    run"): `SimulationController.enter_output_safe_state()` calls
    `end_open_runs(...)`."""
    cal, _run, _profile = _open_run()
    cal.end_open_runs("session stopped")
    assert cal.runs.get("T1") is None
    assert cal.get_propulsion_override("T1") is None


def test_ending_open_runs_keeps_a_held_result():
    """A finished pass is a held result, not an open run, so a session stop
    leaves it for the operator to commit or discard."""
    cal, run, _profile = _open_run()
    _run_full_pass(cal, run)
    assert run.phase == PHASE_MEASURED
    cal.end_open_runs("session stopped")
    assert cal.runs.get("T1") is run


def test_ending_open_runs_closes_a_lighting_run():
    cal = SpeedCalibration()
    profile = make_default_speed_profile("T1", 100.0)
    cal.start_lighting(_StubTrain(), profile)
    cal.end_open_runs("session stopped")
    assert cal.lighting_runs.get("T1") is None


def test_discarding_all_drops_a_held_result_too():
    """`/api/network/clear` and `load_layout` discard held results as well."""
    cal, run, _profile = _open_run()
    _run_full_pass(cal, run)
    cal.discard_all("network cleared")
    assert cal.runs.get("T1") is None


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


def test_the_committed_curve_runs_in_order_of_power():
    # The sweep measures 100% first; the curve still reads from 0% up.
    cal, run, profile = _open_run()
    _run_full_pass(cal, run, seconds_for_step=lambda power: 100.0 / power)
    curve = cal.commit("T1", profile)
    powers = [p for p, _s in curve]
    assert powers == sorted(powers)
    assert powers[0] == 0.0 and powers[-1] == 100.0


def test_commit_rejects_a_curve_that_falls_with_power():
    cal, run, profile = _open_run()
    _run_full_pass(cal, run)
    assert run.samples[0].power_percent == 100.0
    run.samples[0] = type(run.samples[0])(power_percent=100.0, speed_kmh=0.0)
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
    _observe(cal, run, 1.0, ("B",))
    assert run.is_measuring
    assert run.crossing_started_at is not None

    tainted = cal.taint_current_crossing(
        "T1", "CAUTION restriction during a measured crossing"
    )
    assert tainted is True
    assert run.current_crossing_tainted is True
    # Tainting never fails the pass.
    assert run.phase != PHASE_FAILED

    # The crossing completes at the far block: it is discarded (nothing banked),
    # the taint is spent, and the pass survives to re-measure the step.
    _observe(cal, run, 3.0, ("C",))
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
        _observe(cal, run, clock, ("B",))
        assert run.is_measuring
        cal.taint_current_crossing(
            "T1", "reservation-end braking during a measured crossing"
        )
        clock += 2.0
        _observe(cal, run, clock, (far,))
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


def test_a_cap_before_the_clock_starts_discards_nothing():
    """A crossing is discarded only for a cap during it (design_new.md, "A capped
    crossing is discarded"). A cap in the run-up — before the measured block
    confirms — leaves the crossing that follows clean."""
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm the leg
    far = run.heading_toward_block_id
    assert run.crossing_started_at is None

    assert cal.taint_current_crossing(
        "T1", "reservation-end braking during a measured crossing"
    ) is False
    assert run.current_crossing_tainted is False

    _observe(cal, run, 1.0, ("B",))
    _observe(cal, run, 3.0, (far,))
    assert len(run.crossings) == 1


def test_taint_current_crossing_ignores_a_train_with_no_open_pass():
    cal, _run, _profile = _open_run()
    # No open run for a train at all -> no-op.
    assert cal.taint_current_crossing("T_NONE", "caution") is False


# --- the turnaround waits for the train to leave B ----------------------------
# design_new.md "Speed calibration", The pass turns the train around at each
# end: the next leg is timed from a fresh entry into B, so the train must be
# off B before it stops.


def _cross_into_far_block(cal, run, *, measured_block_held):
    """Arm, enter B, enter the far block; return the clock at the far entry."""
    cal.advance(run, CalibrationObservation(now=0.0))  # arm the leg
    far = run.heading_toward_block_id
    _observe(cal, run, 1.0, ("B",), path_present=True)
    _observe(
        cal, run, 3.0, (far,), path_present=True,
        measured_block_held=measured_block_held,
    )
    return 3.0


def test_the_pass_keeps_driving_until_the_train_is_off_the_measured_block():
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=True)

    # The crossing is banked, but the tail is still on B: the step's power stays.
    assert len(run.crossings) == 1
    assert cal.get_propulsion_override("T1").value == run.current_power
    cal.advance(run, CalibrationObservation(
        now=clock + 0.5, path_present=True, measured_block_held=True))
    assert cal.get_propulsion_override("T1").value == run.current_power

    # Once the span no longer covers B the pass cuts power and, stopped, turns.
    cal.advance(run, CalibrationObservation(
        now=clock + 1.0, path_present=True, measured_block_held=False))
    assert cal.get_propulsion_override("T1").value == 0.0
    heading = run.heading_to
    cal.advance(run, CalibrationObservation(now=clock + 2.0, is_stopped=True))
    assert run.heading_to == 1 - heading
    assert run.needs_reverse is True


def test_a_train_already_off_the_measured_block_stops_at_once():
    cal, run, _profile = _open_run()
    _cross_into_far_block(cal, run, measured_block_held=False)
    assert len(run.crossings) == 1
    assert cal.get_propulsion_override("T1").value == 0.0


def test_a_leg_that_ends_with_the_train_still_on_the_measured_block_fails():
    """The far outer block is too short to hold the train past B: its leg ends
    at the far block's far seam with the tail still on B, so the leg back could
    never be timed."""
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    cal.advance(run, CalibrationObservation(
        now=clock + 1.0, path_present=False, measured_block_held=True))
    assert run.phase == PHASE_FAILED
    assert "too short" in run.failure_reason


def test_a_train_that_never_clears_the_measured_block_fails():
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    cal.advance(run, CalibrationObservation(
        now=clock + CALIBRATION_STEP_TIMEOUT_S + 1.0,
        path_present=True, measured_block_held=True))
    assert run.phase == PHASE_FAILED
    assert "never cleared" in run.failure_reason


def test_the_crossing_window_is_set_by_the_measured_block_length():
    """design_new.md "Speed calibration", The crossing window is set by B's
    length: a crossing gets the time B takes at
    `CALIBRATION_MIN_CROSSING_SPEED_KMH`, counted from B's confirmation, so a
    long run-up does not eat into it and a slow crawl is timed."""
    cal, run, _profile = _open_run()
    assert run.crossing_window_s == pytest.approx(_window_s())
    assert run.crossing_window_s > CALIBRATION_STEP_TIMEOUT_S
    cal.advance(run, CalibrationObservation(now=0.0))  # arm the leg
    far = run.heading_toward_block_id
    cal.advance(run, CalibrationObservation(now=0.0, path_present=True))
    entered_b = CALIBRATION_STEP_TIMEOUT_S - 1.0
    _observe(cal, run, entered_b, ("B",), path_present=True)
    crawl = run.crossing_window_s - 1.0  # far longer than the step timeout
    cal.advance(run, CalibrationObservation(now=entered_b + crawl - 0.5, path_present=True))
    assert run.is_measuring
    _observe(cal, run, entered_b + crawl, (far,), path_present=True)
    assert run.crossings == [pytest.approx(cm_per_sec_to_kmh(BLOCK_LENGTH_CM / crawl))]
