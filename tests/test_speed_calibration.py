"""C5 — Speed calibration: the shuttle pass, its state machine, and the commit.

design_new.md "Speed calibration". A pass commands voltages — coarse steps
from `max_voltage` down by `CALIBRATION_VOLTAGE_STEP_V`, then narrowing to the
starting voltage, then filling where the speed changes fast — times the head
across one operator-named measured block between two confirmed block entries,
turns the train around at each end itself, and takes the median of
`CALIBRATION_ROUND_TRIPS` round trips per step — half the crossings in each
direction, so the stretch's grade cancels.

These drive `advance(...)` with synthetic observations rather than a moving
simulation: the state machine is the contract, and feeding it confirmations
directly is what makes a failed step, the overrun, the re-measure, and the
reversal each testable on their own.
"""

import pytest

from constants import (
    CALIBRATION_CAP_RETRY_LIMIT,
    CALIBRATION_FILL_SPEED_FRACTION,
    CALIBRATION_HALF_POWER_S,
    CALIBRATION_MIN_CROSSING_SPEED_KMH,
    CALIBRATION_MIN_VOLTAGE_GAP_V,
    CALIBRATION_MOVE_VOLTAGE_V,
    CALIBRATION_OFF_B_WAIT_READS,
    CALIBRATION_ROUND_TRIPS,
    CALIBRATION_STEP_TIMEOUT_S,
    CALIBRATION_VOLTAGE_STEP_V,
    OCCUPANCY_OFF_THRESHOLD,
    OCCUPANCY_ON_THRESHOLD,
)
from model_scale import cm_per_sec_to_kmh, kmh_to_cm_per_sec
from speed_calibration import (
    PHASE_FAILED,
    PHASE_LEG,
    PHASE_MEASURED,
    PHASE_MOVING,
    PHASE_RESETTING,
    STEP_COARSE,
    STEP_FILLED,
    STEP_NARROWING,
    CalibrationObservation,
    SpeedCalibration,
    coarse_voltages,
)
from speed_profile import make_default_speed_profile
from tests.train_test_helpers import skip_calibration_trials

BLOCK_LENGTH_CM = 100.0


class _StubTrain:
    train_id = "T1"

    def __init__(self, length_cm=0.0):
        self.length_cm = length_cm


def _open_run(cal=None, train=None, **kwargs):
    profile = make_default_speed_profile("T1", 100.0)
    cal = cal or SpeedCalibration()
    run = cal.start(
        train or _StubTrain(),
        profile,
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        block_length_cm=BLOCK_LENGTH_CM,
        **kwargs,
    )
    skip_calibration_trials(run)
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


def _loco_kmh(voltage):
    """A locomotive that does not move steadily below 3 V and runs 20 km/h
    there, rising in a straight line to 100 km/h at 12 V."""
    if voltage < 3.0 - 1e-9:
        return 0.0
    return 20.0 + (voltage - 3.0) * 80.0 / 9.0


def _seconds_at(kmh):
    return BLOCK_LENGTH_CM / kmh_to_cm_per_sec(kmh)


def _fail_step(cal, run, clock):
    """Run one leg of the current step that never reaches B: it times out, the
    step fails, and the train — which never left its outer block — is found
    standing there, so the next step starts. Returns the advanced clock."""
    cal.advance(run, CalibrationObservation(now=clock))  # arm the leg
    cal.advance(run, CalibrationObservation(now=clock, path_present=True))
    clock += CALIBRATION_STEP_TIMEOUT_S + 1.0
    cal.advance(run, CalibrationObservation(now=clock, path_present=True))
    if run.phase == PHASE_RESETTING:
        clock += 1.0
        standing_in = 1 - run.heading_to
        cal.advance(
            run,
            CalibrationObservation(
                now=clock, is_stopped=True, outer_block_index=standing_in
            ),
        )
    return clock


def _run_full_pass(cal, run, *, loco=_loco_kmh):
    """Run the pass to its end. `loco(voltage)` is the locomotive's speed at a
    voltage, `0` where a step fails."""
    clock = 0.0
    guard = 0
    while run.is_measuring and guard < 5000:
        guard += 1
        kmh = loco(run.current_voltage)
        if kmh <= 0.0:
            clock = _fail_step(cal, run, clock)
        else:
            clock = _crossing(cal, run, clock, seconds=_seconds_at(kmh))
    return clock


def _voltages(run):
    return [s.track_voltage for s in run.samples]


# --- the sweep ---------------------------------------------------------------


def test_the_coarse_steps_run_down_from_max_voltage():
    steps = coarse_voltages(12.0)
    assert steps[0] == 12.0
    assert steps == [12.0, 10.0, 8.0, 6.0, 4.0, 2.0]
    assert all(a - b == CALIBRATION_VOLTAGE_STEP_V for a, b in zip(steps, steps[1:]))


def test_the_first_step_is_the_coarse_step_at_max_voltage():
    _cal, run, profile = _open_run()
    assert run.current_voltage == profile.max_voltage
    assert run.current_step_kind == STEP_COARSE


def test_a_full_pass_runs_coarse_then_narrows_then_fills():
    """design_new.md "Speed calibration": coarse steps down to the first that
    fails (2 V), narrowing between 4 V and 2 V to the starting voltage, then
    filling halfway between coarse steps whose speeds are far apart."""
    cal, run, _profile = _open_run()
    _run_full_pass(cal, run)
    assert run.phase == PHASE_MEASURED, run.failure_reason
    kinds = {round(s.track_voltage, 3): s.kind for s in run.samples}
    assert [v for v, k in kinds.items() if k == STEP_COARSE] == [12.0, 10.0, 8.0, 6.0, 4.0]
    # 2 V fails; 3 V passes; 2.5 V and 2.75 V fail.
    assert sorted(run.failed_voltages) == [2.0, 2.5, 2.75]
    assert [v for v, k in kinds.items() if k == STEP_NARROWING] == [3.0]
    # Coarse steps 2 V apart differ by 17.8 km/h, more than 15% of 100 km/h.
    assert sorted(v for v, k in kinds.items() if k == STEP_FILLED) == [5.0, 7.0, 9.0, 11.0]


def test_the_starting_voltage_is_the_lowest_step_that_passed():
    """design_new.md "Speed calibration", The starting voltage is the lowest
    step that ran every crossing: it is the curve's first point."""
    cal, run, _profile = _open_run()
    _run_full_pass(cal, run)
    curve = cal.measured_curve(run)
    assert curve[0][0] == 3.0
    assert curve[0][1] == pytest.approx(20.0, rel=1e-6)
    # The narrowing stopped once the gap to the failed step below was no more
    # than CALIBRATION_MIN_VOLTAGE_GAP_V.
    assert curve[0][0] - max(run.failed_voltages) <= CALIBRATION_MIN_VOLTAGE_GAP_V + 1e-9


def test_the_narrowing_runs_down_to_the_starting_voltage_when_every_coarse_step_passes():
    """When even the lowest coarse step passes, the lower bound is the starting
    voltage (design_new.md "Speed calibration", The pass narrows in on the
    lowest voltage that runs every crossing): no step runs at or below it."""
    cal, run, profile = _open_run()
    _run_full_pass(cal, run, loco=lambda v: 10.0 + 7.5 * v)
    assert run.phase == PHASE_MEASURED, run.failure_reason
    curve = cal.measured_curve(run)
    starting = profile.starting_voltage
    assert starting < curve[0][0] <= starting + CALIBRATION_MIN_VOLTAGE_GAP_V + 1e-9


def test_the_coarse_steps_stay_above_the_starting_voltage():
    """design_new.md "Speed calibration", The sweep runs down from
    `max_voltage` in `CALIBRATION_VOLTAGE_STEP_V` steps: while the voltage stays
    above the starting voltage."""
    assert coarse_voltages(12.0, 4.0) == [12.0, 10.0, 8.0, 6.0]
    assert coarse_voltages(12.0, 1.0) == [12.0, 10.0, 8.0, 6.0, 4.0, 2.0]


def test_filling_needs_room_between_the_two_steps():
    """A pair whose voltages are no more than twice
    `CALIBRATION_MIN_VOLTAGE_GAP_V` apart is not split, however far apart its
    speeds are."""
    cal, run, _profile = _open_run()
    # A locomotive that jumps from 20 to 90 km/h between 3 V and 3.4 V.
    _run_full_pass(cal, run, loco=lambda v: 0.0 if v < 3.0 else (20.0 if v < 3.4 else 90.0 + v))
    assert run.phase == PHASE_MEASURED, run.failure_reason
    for low, high in zip(cal.measured_curve(run), cal.measured_curve(run)[1:]):
        if high[1] - low[1] > CALIBRATION_FILL_SPEED_FRACTION * cal.measured_curve(run)[-1][1]:
            assert high[0] - low[0] <= 2 * CALIBRATION_MIN_VOLTAGE_GAP_V + 1e-9


def test_the_curve_holds_one_point_per_passed_step_at_the_voltage_it_drove():
    """design_new.md "Speed calibration", A pass measures `voltage_speed_curve`:
    each step that passed gives one point, at the voltage the step drove; a
    failed step gives none."""
    cal, run, _profile = _open_run()
    _run_full_pass(cal, run)
    curve = cal.measured_curve(run)
    assert [v for v, _s in curve] == sorted(_voltages(run))
    assert not set(run.failed_voltages) & {v for v, _s in curve}
    assert curve[-1][0] == run.max_voltage


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
    assert arm.drive_voltage == run.current_voltage
    _observe(cal, run, 1.0, ("B",), path_present=True)
    stop = _observe(cal, run, 3.0, ("C",), path_present=True)
    assert stop.deactivate_route is False
    assert stop.drive_voltage == 0.0


def test_a_step_takes_the_median_of_both_directions():
    cal, run, _profile = _open_run()
    # Alternate slow/fast legs — a grade makes one direction measure slow by
    # what it makes the other measure fast, and equal counts each way cancel it.
    clock = 0.0
    for seconds in (3.0, 1.0) * CALIBRATION_ROUND_TRIPS:
        clock = _crossing(cal, run, clock, seconds=seconds)
    assert len(run.samples) == 1
    slow = cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 3.0)
    fast = cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 1.0)
    assert run.samples[0].speed_kmh == pytest.approx((slow + fast) / 2)


def test_one_mistimed_crossing_does_not_move_the_step():
    """design_new.md "Speed calibration", Both directions are measured, and a
    step's sample is their median: a crossing a detector timed wrong lands at
    an end of the order and leaves the sample where the others put it."""
    cal, run, _profile = _open_run()
    clock = 0.0
    for seconds in (2.0, 2.0, 2.0, 8.0):
        clock = _crossing(cal, run, clock, seconds=seconds)
    assert run.samples[0].speed_kmh == pytest.approx(
        cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 2.0)
    )


def test_the_rig_run_of_2026_10_31_measures_on():
    """The crossing times the rig recorded measuring Up at its three highest
    steps. The locomotive ran at nearly the same speed at all three, and one
    mistimed crossing at 90% (3.71 s) made a mean of 80% read faster than 90%,
    so the pass re-measured 80% and failed. The median keeps the three steps in
    order."""
    cal, run, _profile = _open_run()
    clock = 0.0
    for times in (
        (2.62, 1.99, 1.92, 1.89),
        (3.71, 1.94, 2.01, 1.91),
        (2.13, 2.01, 2.02, 1.88),
    ):
        for seconds in times:
            clock = _crossing(cal, run, clock, seconds=seconds)
    assert run.phase != PHASE_FAILED, run.failure_reason
    assert run.step_remeasured is False
    assert _voltages(run) == coarse_voltages(run.max_voltage)[:3]
    speeds = [s.speed_kmh for s in run.samples]
    assert speeds == sorted(speeds, reverse=True)


def test_the_rig_run_of_2026_11_02_records_the_flat_top():
    """design_new.md "Speed calibration", A step that measures close to the
    step above it records the same speed. The rig ran Up at about 150 km/h at
    its two highest steps; the second step's median came out about 2% faster than the
    first's, and every pass ended on it. Inside
    `CALIBRATION_SAME_SPEED_FRACTION` that is the same speed, so the step
    records the first step's speed and the sweep goes on."""
    cal, run, _profile = _open_run()
    clock = 0.0
    for times in (
        (2.01, 1.99, 2.00, 1.87),
        (2.23, 1.40, 3.73, 1.74),
    ):
        for seconds in times:
            clock = _crossing(cal, run, clock, seconds=seconds)
    assert run.phase != PHASE_FAILED, run.failure_reason
    assert run.step_remeasured is False
    assert len(run.samples) == 2
    assert run.samples[1].speed_kmh == run.samples[0].speed_kmh
    assert _outcomes(run)[4:8] == [(coarse_voltages(run.max_voltage)[1], 1, "used")] * 4


def test_a_clearly_faster_step_measured_close_the_second_time_records_the_same_speed():
    """design_new.md "Speed calibration", A step that measures clearly faster
    than the step above it is measured once more: a re-measure close to the step
    above records that step's speed."""
    cal, run, _profile = _open_run()
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)  # clearly faster
    assert run.step_remeasured is True
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=8.6)  # under 5% faster
    assert run.phase != PHASE_FAILED, run.failure_reason
    assert len(run.samples) == 2
    assert run.samples[1].speed_kmh == run.samples[0].speed_kmh


def test_a_step_collects_two_crossings_per_round_trip():
    cal, run, _profile = _open_run()
    assert run.crossings_target == 2 * CALIBRATION_ROUND_TRIPS
    clock = 0.0
    for _ in range(run.crossings_target - 1):
        clock = _crossing(cal, run, clock, seconds=2.0)
    assert run.samples == []  # still on the first step
    _crossing(cal, run, clock, seconds=2.0)
    assert len(run.samples) == 1


# --- failed steps, overrun, monotonicity ---------------------------------------


def _measure_top_step(cal, run, *, seconds=2.0):
    """Measure the `max_voltage` step cleanly; return the clock with the next
    coarse step due."""
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=seconds)
    assert run.current_voltage == run.max_voltage - CALIBRATION_VOLTAGE_STEP_V
    return clock


def _window_s():
    return BLOCK_LENGTH_CM / kmh_to_cm_per_sec(CALIBRATION_MIN_CROSSING_SPEED_KMH)


def test_a_step_that_does_not_move_the_train_fails_and_the_pass_narrows():
    """design_new.md "Speed calibration", A step that does not move the train
    fails: it gives no point, its leg route is torn down, and the pass goes on
    to the narrowing step halfway down to the failed voltage."""
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    cal.advance(run, CalibrationObservation(now=clock))  # arm the second step's leg
    cal.advance(run, CalibrationObservation(now=clock, path_present=True))
    command = cal.advance(
        run,
        CalibrationObservation(
            now=clock + CALIBRATION_STEP_TIMEOUT_S + 1.0, path_present=True
        ),
    )
    assert run.phase == PHASE_RESETTING
    assert command.deactivate_route is True
    assert command.arm_leg_to is None
    assert run.failed_voltages == [10.0]
    assert _voltages(run) == [12.0]
    # The pass drives nothing while the train stands between steps ...
    assert cal.get_propulsion_override("T1").track_voltage == 0.0
    # ... and the next step is the narrowing step halfway to the failure.
    assert run.current_voltage == 11.0
    assert run.current_step_kind == STEP_NARROWING


def test_after_a_failed_step_a_train_standing_in_an_outer_block_starts_the_next_leg():
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    clock = _fail_step(cal, run, clock)
    assert run.phase == PHASE_LEG
    assert run.leg_armed is True
    assert run.current_voltage == 11.0


def test_after_a_failed_step_a_train_left_on_b_is_moved_into_an_outer_block():
    """design_new.md "Speed calibration", A step passes or fails: a failed step
    that left the train on B moves it into the nearer outer block first."""
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    cal.advance(run, CalibrationObservation(now=clock))  # arm
    _observe(cal, run, clock + 1.0, ("B",), path_present=True)
    cal.advance(run, CalibrationObservation(now=clock + 1.0 + _window_s() + 1.0, path_present=True))
    assert run.phase == PHASE_RESETTING
    command = cal.advance(
        run,
        CalibrationObservation(
            now=clock + _window_s() + 5.0, is_stopped=True, measured_block_held=True
        ),
    )
    assert run.phase == PHASE_MOVING
    assert command.move_into_place is True


def test_a_crossing_that_stops_inside_the_stretch_fails_the_step():
    """design_new.md "Speed calibration", A crossing that stops inside the
    stretch fails the step: a part-crossing is no measurement."""
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    cal.advance(run, CalibrationObservation(now=clock))  # arm the second step's leg
    _observe(cal, run, clock + 1.0, ("B",), path_present=True)
    entered_b = clock + 1.0
    # Still inside the window: the pass keeps waiting for the far block.
    cal.advance(run, CalibrationObservation(now=entered_b + _window_s() - 1.0, path_present=True))
    assert run.phase == PHASE_LEG
    command = cal.advance(
        run, CalibrationObservation(now=entered_b + _window_s() + 1.0, path_present=True)
    )
    assert run.phase == PHASE_RESETTING
    assert run.failed_voltages == [10.0]
    assert command.deactivate_route is True


def test_a_failed_step_drops_the_crossings_it_already_measured():
    """A step whose voltage moved the train on one crossing but not on the next
    fails, whatever crossings it already measured."""
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    clock = _crossing(cal, run, clock, seconds=2.0)  # one clean crossing of the second step
    assert len(run.crossings) == 1
    clock = _fail_step(cal, run, clock)
    assert run.crossings == []
    assert run.failed_voltages == [10.0]
    assert _voltages(run) == [12.0]


@pytest.mark.parametrize("enters_b", [False, True])
def test_a_pass_whose_top_step_fails_fails(enters_b):
    """design_new.md "Speed calibration", A pass whose top step fails fails the
    pass: a locomotive that does not cross B at full voltage gives no curve."""
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
    assert "max_voltage" in run.failure_reason
    assert command.deactivate_route is True
    assert profile.calibrated_at is None


def test_a_filled_step_that_fails_is_measured_once_more_then_fails_the_pass():
    """design_new.md "Speed calibration", A filled step that fails is measured
    once more: a lower voltage already ran every crossing, so a failure there is
    a dirty rail or a bad contact."""
    cal, run, _profile = _open_run()

    def loco(v):
        if run.current_step_kind == STEP_FILLED:
            return 0.0
        return _loco_kmh(v)

    _run_full_pass(cal, run, loco=loco)
    assert run.phase == PHASE_FAILED
    assert "dirty rail" in run.failure_reason
    filled_attempts = [c.attempt for c in run.crossing_log if c.outcome == "failed_step"]
    assert filled_attempts[-2:] == [1, 2]


def test_a_finished_curve_the_commit_would_refuse_fails_the_pass():
    """design_new.md "Speed calibration", A finished curve the commit would
    refuse fails the pass: when only the `max_voltage` step passes, the curve
    has one point."""
    cal, run, _profile = _open_run()
    _run_full_pass(cal, run, loco=lambda v: 90.0 if v >= 12.0 else 0.0)
    assert run.phase == PHASE_FAILED
    assert "at least two points" in run.failure_reason


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
    assert run.phase == PHASE_FAILED  # the max_voltage step failed


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
    # The top step slow, then the second step fast twice: the re-measure tells a noisy crossing
    # apart from a locomotive that really does run faster at less power
    # (design_new.md "Speed calibration", A step that measures faster than the
    # step above it is measured once more).
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)
    assert len(run.samples) == 1
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)
    assert run.step_remeasured is True
    assert len(run.samples) == 1  # repeated, not banked
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)
    assert run.phase == PHASE_FAILED
    assert "clearly outside" in run.failure_reason


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


def test_discarding_all_drops_a_held_result_too():
    """`/api/network/clear` and `load_layout` discard held results as well."""
    cal, run, _profile = _open_run()
    _run_full_pass(cal, run)
    cal.discard_all("network cleared")
    assert cal.runs.get("T1") is None


# --- commit ------------------------------------------------------------------


def test_measuring_every_step_does_not_commit():
    cal, run, profile = _open_run()
    before = list(profile.voltage_speed_curve)
    _run_full_pass(cal, run)
    assert run.phase == PHASE_MEASURED
    assert profile.voltage_speed_curve == before
    assert profile.calibrated_at is None


def test_commit_writes_the_curve_and_stamp_and_nothing_else():
    cal, run, profile = _open_run()
    profile.lighting_voltage = 1.2
    accel_before = profile.acceleration_rate
    model_before = profile.model
    _run_full_pass(cal, run)
    curve = cal.commit("T1", profile)

    assert profile.voltage_speed_curve == curve
    assert profile.calibrated_at is not None
    # Commit writes voltage_speed_curve and calibrated_at only (design_new.md
    # "What commit writes"): `model` is edited on the model profile, not here;
    # the lighting voltage is set by hand; and a motion rate describes
    # behaviour rather than measured speed.
    assert profile.model == model_before
    assert profile.lighting_voltage == 1.2
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
    _run_full_pass(cal, run)
    curve = cal.commit("T1", profile)
    speeds = [s for _v, s in curve]
    assert speeds == sorted(speeds)
    # And physics can read it back: every measured speed maps to its voltage.
    for voltage, speed in curve:
        assert profile.speed_to_voltage(speed) == pytest.approx(voltage, abs=1e-6)


def test_the_committed_curve_runs_in_order_of_voltage():
    # The sweep measures max_voltage first; the curve still reads from the
    # starting voltage up.
    cal, run, profile = _open_run()
    _run_full_pass(cal, run)
    curve = cal.commit("T1", profile)
    voltages = [v for v, _s in curve]
    assert voltages == sorted(voltages)
    assert voltages[-1] == profile.max_voltage


def test_commit_rejects_a_curve_that_falls_with_voltage():
    cal, run, profile = _open_run()
    _run_full_pass(cal, run)
    assert run.samples[0].track_voltage == 12.0
    run.samples[0] = type(run.samples[0])(track_voltage=12.0, speed_kmh=1.0)
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
    assert override is not None and override.track_voltage == 0.0


# --- capped-crossing discard (design_new.md:3047, "A capped crossing is discarded") ---


def test_a_capped_crossing_is_discarded_and_the_pass_goes_on():
    """An external ATC restriction during a measured crossing taints it; the
    crossing is discarded rather than banked and the pass keeps going, leaving
    the profile's previous curve in place (design_new.md:3047)."""
    cal, run, profile = _open_run()
    curve_before = list(profile.voltage_speed_curve)
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
    assert profile.voltage_speed_curve == curve_before


def test_a_stretch_that_caps_every_crossing_fails_the_pass():
    """When one step is capped on CALIBRATION_CAP_RETRY_LIMIT consecutive
    crossings in one direction — the stretch is too short for that step even at
    the zero reservation-end standoff — the pass fails and reports rather than
    shuttling forever (design_new.md, "A capped crossing is discarded"). Legs
    alternate, so with every crossing capped the limit-th one in the first
    direction is crossing 2 * limit - 1."""
    cal, run, profile = _open_run()
    curve_before = list(profile.voltage_speed_curve)
    clock = 0.0
    crossings = 2 * CALIBRATION_CAP_RETRY_LIMIT - 1
    for i in range(crossings):
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
        if i < crossings - 1:
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
    assert profile.voltage_speed_curve == curve_before


def test_a_clean_crossing_the_other_way_does_not_reset_the_cap_count():
    """The counts are per direction: a stretch that caps only one direction
    still reaches the limit while the other direction measures cleanly."""
    cal, run, _profile = _open_run()
    clock = 0.0
    capped = 0
    for _ in range(4 * CALIBRATION_CAP_RETRY_LIMIT):
        cal.advance(run, CalibrationObservation(now=clock))  # arm the leg
        far = run.heading_toward_block_id
        clock += 1.0
        _observe(cal, run, clock, ("B",))
        if far == "C":
            cal.taint_current_crossing("T1", "reservation-end braking")
            capped += 1
        clock += 2.0
        _observe(cal, run, clock, (far,))
        if run.phase == PHASE_FAILED:
            break
        clock += 1.0
        cal.advance(run, CalibrationObservation(now=clock, is_stopped=True))
    assert run.phase == PHASE_FAILED
    assert "toward C" in run.failure_reason
    assert capped == CALIBRATION_CAP_RETRY_LIMIT
    assert run.samples == []


def test_a_crossing_in_a_direction_that_has_its_count_is_not_needed():
    """design_new.md "Speed calibration", A capped crossing is discarded: a
    measured crossing in a direction that already has CALIBRATION_ROUND_TRIPS
    is not used, and the step waits for the other direction."""
    from speed_calibration import CROSSING_NOT_NEEDED

    cal, run, _profile = _open_run()
    clock = 0.0
    for _ in range(3 * CALIBRATION_ROUND_TRIPS):
        cal.advance(run, CalibrationObservation(now=clock))  # arm the leg
        far = run.heading_toward_block_id
        clock += 1.0
        _observe(cal, run, clock, ("B",))
        if far == "A":
            cal.taint_current_crossing("T1", "reservation-end braking")
        clock += 2.0
        _observe(cal, run, clock, (far,))
        clock += 1.0
        cal.advance(run, CalibrationObservation(now=clock, is_stopped=True))
    outcomes = [(c.toward_block_id, c.outcome) for c in run.crossing_log]
    assert ("C", CROSSING_NOT_NEEDED) in outcomes
    assert run.crossing_headings.count(run.outer_block_ids.index("C")) == CALIBRATION_ROUND_TRIPS
    assert run.samples == []


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


def _open_run_with_runoff(train_length_cm):
    """A run whose ends each have one run-off block past a 50 cm outer block:
    A2 past A, C2 past C."""
    return _open_run(
        train=_StubTrain(length_cm=train_length_cm),
        runoff_block_ids=(("A2",), ("C2",)),
        end_lengths_cm=((50.0, 40.0), (50.0, 40.0)),
    )


def test_the_run_off_lasts_until_a_run_off_block_puts_the_tail_past_b():
    """design_new.md "Speed calibration", The run-off lasts until the train is
    off B: while B still reads occupied, the step's power stays on until the
    head's entry into a run-off block shows the train's length is past B — the
    far end's blocks before it (C, 50 cm) hold the 20 cm train — then half
    power, then 0."""
    cal, run, _profile = _open_run_with_runoff(20.0)
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    assert run.phase == "clearing"
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage
    # Long after any timed run-off, B still reads occupied: power stays on.
    cal.advance(run, CalibrationObservation(
        now=clock + 10.0, path_present=True, measured_block_held=True))
    assert run.phase == "clearing"
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage
    # The head enters the run-off block: the train is off B by its length.
    end = clock + 10.5
    _observe(cal, run, end, (run.end_block_ids(run.heading_to)[1],),
             path_present=True, measured_block_held=True)
    assert run.phase == "stopping"
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage / 2
    cal.advance(run, CalibrationObservation(
        now=end + CALIBRATION_HALF_POWER_S + 0.1, measured_block_held=True))
    assert cal.get_propulsion_override("T1").track_voltage == 0.0


def test_a_train_longer_than_the_blocks_before_the_run_off_block_runs_on():
    """A 60 cm train whose head has entered the run-off block past a 50 cm outer
    block may still have its tail on B, so the run-off goes on until B reads
    clear."""
    cal, run, _profile = _open_run_with_runoff(60.0)
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    _observe(cal, run, clock + 1.0, (run.end_block_ids(run.heading_to)[1],),
             path_present=True, measured_block_held=True)
    assert run.phase == "clearing"
    cal.advance(run, CalibrationObservation(
        now=clock + 1.5, path_present=True, measured_block_held=False))
    assert run.phase == "stopping"


def test_the_run_off_judges_the_tail_by_the_longer_length():
    """The entered length is 10 cm but the pass measured 60 cm: the longer one
    decides, so entering the run-off block past a 50 cm outer block does not
    stop the train."""
    cal, run, _profile = _open_run_with_runoff(10.0)
    run.measured_length_cm = 60.0
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    _observe(cal, run, clock + 1.0, (run.end_block_ids(run.heading_to)[1],),
             path_present=True, measured_block_held=True)
    assert run.phase == "clearing"


def test_a_run_off_block_is_not_an_overrun():
    """design_new.md "Speed calibration", A leg that overruns the stretch fails
    the pass: only a block past the far end is an overrun."""
    cal, run, _profile = _open_run_with_runoff(20.0)
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    _observe(cal, run, clock + 1.0, (run.end_block_ids(run.heading_to)[1],),
             path_present=True, measured_block_held=True)
    assert run.phase != PHASE_FAILED
    assert run.overrun_seen is False


def test_the_run_off_ends_when_the_train_stands():
    """With no run-off block to prove the train is off B, the run-off ends when
    the ATC limit has held the train at its reservation end."""
    cal, run, _profile = _open_run(train=_StubTrain(length_cm=20.0))
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    assert run.phase == "clearing"
    cal.advance(run, CalibrationObservation(
        now=clock + 4.0, path_present=True, is_stopped=True, measured_block_held=True))
    assert run.phase == "stopping"


def test_the_pass_ends_the_leg_once_the_standing_train_is_off_b():
    """design_new.md "Speed calibration", The pass ends each leg once the train
    stands off B: the command asks for the leg route to be torn down."""
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=False)
    command = cal.advance(run, CalibrationObservation(
        now=clock + CALIBRATION_HALF_POWER_S + 1.0, is_stopped=True))
    assert command is not None and command.deactivate_route is True
    assert run.phase == "leg"


def test_the_run_off_ends_at_once_when_b_reads_clear():
    cal, run, _profile = _open_run(train=_StubTrain(length_cm=20.0))
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    cal.advance(run, CalibrationObservation(
        now=clock + 0.1, path_present=True, measured_block_held=False))
    assert run.phase == "stopping"
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage / 2
    cal.advance(run, CalibrationObservation(now=clock + CALIBRATION_HALF_POWER_S + 0.2))
    assert cal.get_propulsion_override("T1").track_voltage == 0.0


def test_a_train_already_off_the_measured_block_starts_its_stop_at_once():
    cal, run, _profile = _open_run()
    _cross_into_far_block(cal, run, measured_block_held=False)
    assert len(run.crossings) == 1
    assert run.phase == "stopping"
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage / 2


def test_the_standing_train_waits_for_b_to_read_clear():
    """design_new.md "Speed calibration", The pass turns the train around at
    each end: the stopped train waits until it is off B in both views before the
    next leg, because B holds its presence through the lighting zone."""
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    heading = run.heading_to
    stopped = clock + 2.0
    for read in range(5):
        cal.advance(run, CalibrationObservation(
            now=stopped + 0.1 * read, is_stopped=True, measured_block_held=True))
    assert run.heading_to == heading
    assert run.phase == "stopping"
    cal.advance(run, CalibrationObservation(now=stopped + 1.0, is_stopped=True))
    assert run.heading_to == 1 - heading


def test_a_train_that_stops_with_its_tail_on_b_fails():
    """design_new.md "Speed calibration", A train that cannot get off B fails the
    pass: still on B after CALIBRATION_OFF_B_WAIT_READS standing reads."""
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=True) + 2.0
    # The ATC limit holds the train at its reservation end, which ends the
    # run-off; the half-power stop runs its course first.
    cal.advance(run, CalibrationObservation(
        now=clock, is_stopped=True, measured_block_held=True))
    clock += CALIBRATION_HALF_POWER_S
    for _ in range(CALIBRATION_OFF_B_WAIT_READS):
        clock += 0.1
        cal.advance(run, CalibrationObservation(
            now=clock, is_stopped=True, measured_block_held=True))
    assert run.is_measuring
    cal.advance(run, CalibrationObservation(
        now=clock + 0.1, is_stopped=True, measured_block_held=True))
    assert run.phase == PHASE_FAILED
    assert "tail still on B" in run.failure_reason


# --- the pass measures the train's length -------------------------------------
# design_new.md "Speed calibration", The pass measures the train's length.


def _release_b_after(cal, run, clock, reads, *, read_s=0.1):
    """Advance `reads` detector reads after the far block's confirmation, with
    the block list letting go of B on the last one."""
    for read in range(1, reads + 1):
        cal.advance(run, CalibrationObservation(
            now=clock + read * read_s,
            measured_block_held=read < reads,
            measured_block_listed=read < reads,
        ))
    return clock + reads * read_s


def test_the_pass_measures_the_train_length_from_b_clearing():
    cal, run, _profile = _open_run(train=_StubTrain(length_cm=100.0))
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    lag = OCCUPANCY_OFF_THRESHOLD - OCCUPANCY_ON_THRESHOLD
    _release_b_after(cal, run, clock, lag + 4)
    # 4 reads of 0.1 s at 50 cm/s between head into C and tail off B.
    assert run.measured_length_cm == pytest.approx(20.0)


def test_the_pass_keeps_the_shortest_length_it_measures():
    cal, run, _profile = _open_run()
    lag = OCCUPANCY_OFF_THRESHOLD - OCCUPANCY_ON_THRESHOLD
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    clock = _release_b_after(cal, run, clock, lag + 4)
    cal.advance(run, CalibrationObservation(now=clock + 1.0, is_stopped=True))
    assert run.measured_length_cm == pytest.approx(20.0)
    # The leg back: B held through a lighting-zone hold reads clear late, which
    # measures long and leaves the shorter length in place.
    cal.advance(run, CalibrationObservation(now=10.0))  # arm the leg back
    far = run.heading_toward_block_id
    _observe(cal, run, 11.0, ("B",), path_present=True)
    _observe(cal, run, 13.0, (far,), path_present=True, measured_block_held=True)
    _release_b_after(cal, run, 13.0, lag + 30)
    assert run.measured_length_cm == pytest.approx(20.0)


def test_a_clear_within_the_detector_lag_gives_no_length():
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=True)
    _release_b_after(cal, run, clock, OCCUPANCY_OFF_THRESHOLD - OCCUPANCY_ON_THRESHOLD)
    assert run.measured_length_cm is None


# --- half power at each end of a leg ------------------------------------------
# design_new.md "Speed calibration", The pass starts and stops the train at half
# power first.


def test_a_leg_starts_at_half_power():
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm the leg
    cal.advance(run, CalibrationObservation(now=1.0, path_present=True))  # authority
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage / 2
    cal.advance(run, CalibrationObservation(
        now=1.0 + CALIBRATION_HALF_POWER_S - 0.1, path_present=True))
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage / 2
    cal.advance(run, CalibrationObservation(
        now=1.0 + CALIBRATION_HALF_POWER_S + 0.1, path_present=True))
    assert cal.get_propulsion_override("T1").track_voltage == run.current_voltage


def test_the_turnaround_waits_for_the_half_power_stop():
    """A ceiling may bring the train to a stand during the half-power stop; the
    pass still lets the stop run its course before it turns the train."""
    cal, run, _profile = _open_run()
    clock = _cross_into_far_block(cal, run, measured_block_held=False)
    heading = run.heading_to
    cal.advance(run, CalibrationObservation(now=clock + 0.1, is_stopped=True))
    assert run.heading_to == heading
    assert run.phase == "stopping"
    cal.advance(run, CalibrationObservation(
        now=clock + CALIBRATION_HALF_POWER_S + 0.1, is_stopped=True))
    assert run.heading_to == 1 - heading


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


# --- the panel's crossing list -----------------------------------------------
# design_new.md "Speed calibration", The panel lists every crossing.


def _outcomes(run):
    return [(c.track_voltage, c.attempt, c.outcome) for c in run.crossing_log]


def test_every_crossing_is_listed_with_its_time_speed_and_direction():
    cal, run, _profile = _open_run()
    clock = 0.0
    for seconds in (2.0, 3.0, 2.0, 3.0):
        clock = _crossing(cal, run, clock, seconds=seconds)
    log = run.crossing_log
    assert [c.outcome for c in log] == ["used"] * 4
    assert [c.toward_block_id for c in log] == ["C", "A", "C", "A"]
    assert [c.seconds for c in log] == [2.0, 3.0, 2.0, 3.0]
    assert log[0].speed_kmh == pytest.approx(cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 2.0))
    listed = run.to_dict()["crossings"]
    assert listed[1] == {
        "track_voltage": 12.0, "attempt": 1, "toward_block_id": "A",
        "seconds": 3.0, "speed_kmh": pytest.approx(cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 3.0)),
        "outcome": "used",
    }


def test_a_capped_crossing_is_listed_as_discarded():
    cal, run, _profile = _open_run()
    cal.advance(run, CalibrationObservation(now=0.0))  # arm
    _observe(cal, run, 1.0, ("B",))
    cal.taint_current_crossing("T1", "CAUTION restriction during a measured crossing")
    _observe(cal, run, 3.0, ("C",))
    assert _outcomes(run) == [(12.0, 1, "discarded")]


def test_a_measured_again_step_lists_both_attempts():
    cal, run, _profile = _open_run()
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)  # top step slow
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)  # second step fast: measured again
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)  # second step again, now slower
    second = coarse_voltages(run.max_voltage)[1]
    outcomes = _outcomes(run)
    assert outcomes[4:8] == [(second, 1, "replaced")] * 4
    assert outcomes[8:12] == [(second, 2, "used")] * 4


def test_a_failed_step_lists_the_crossing_that_failed_it():
    cal, run, _profile = _open_run()
    clock = _measure_top_step(cal, run)
    clock = _crossing(cal, run, clock, seconds=2.0)  # one clean crossing of the second step
    _fail_step(cal, run, clock)
    outcomes = _outcomes(run)
    second = coarse_voltages(run.max_voltage)[1]
    assert outcomes[-2:] == [(second, 1, "dropped"), (second, 1, "failed_step")]
    assert run.crossing_log[-1].seconds is None



# -- the move into an outer block -----------------------------------------
# design_new.md "Speed calibration", The pass first moves the train into an
# outer block.


def test_a_pass_opened_outside_the_outer_blocks_moves_the_train_first():
    cal, run, _ = _open_run(heading_to=None)
    assert run.phase == PHASE_MOVING

    command = cal.advance(run, CalibrationObservation(now=0.0))

    assert command.move_into_place is True
    assert command.arm_leg_to is None
    assert command.drive_voltage == CALIBRATION_MOVE_VOLTAGE_V


def test_the_move_drives_half_its_power_first_then_its_power():
    cal, run, _ = _open_run(heading_to=None)
    # Unwired, the unit tests read the route's path as always present, so the
    # power before the path appears is not checked here.
    cal.advance(run, CalibrationObservation(now=0.0))

    cal.advance(run, CalibrationObservation(now=1.0, path_present=True))
    assert run.half_power is True
    assert cal.get_propulsion_override("T1").track_voltage == CALIBRATION_MOVE_VOLTAGE_V * 0.5

    cal.advance(
        run,
        CalibrationObservation(now=1.0 + CALIBRATION_HALF_POWER_S, path_present=True),
    )
    assert cal.get_propulsion_override("T1").track_voltage == CALIBRATION_MOVE_VOLTAGE_V


def test_the_move_stops_on_the_detector():
    """Half power once the head is in the outer block, nothing once the block
    behind has confirmed clear (design_new.md "Speed calibration", The move
    stops on the detector)."""
    cal, run, _ = _open_run(heading_to=None)
    cal.advance(run, CalibrationObservation(now=0.0))
    run.move_target_block_id = "A"  # written by RouteExecutor with the route
    power = lambda: cal.get_propulsion_override("T1").track_voltage  # noqa: E731

    cal.advance(run, CalibrationObservation(
        now=5.0, path_present=True, head_block="X", span_blocks=("X",)))
    cal.advance(run, CalibrationObservation(
        now=5.0 + CALIBRATION_HALF_POWER_S, path_present=True,
        head_block="X", span_blocks=("X",)))
    assert power() == CALIBRATION_MOVE_VOLTAGE_V

    # The detector put the head in A; the tail is still on the block behind.
    cal.advance(run, CalibrationObservation(
        now=7.0, path_present=True, head_block="A", span_blocks=("A", "X")))
    assert power() == CALIBRATION_MOVE_VOLTAGE_V * 0.5
    cal.advance(run, CalibrationObservation(
        now=8.0, path_present=True, head_block="A", span_blocks=("A", "X")))
    assert power() == CALIBRATION_MOVE_VOLTAGE_V * 0.5

    # The span has moved off X, but the block list still holds X until its
    # detector confirms it clear: still half power.
    cal.advance(run, CalibrationObservation(
        now=8.2, path_present=True, head_block="A", span_blocks=("A",),
        listed_blocks=("A", "X")))
    assert run.move_blocks_behind == ("X",)
    assert power() == CALIBRATION_MOVE_VOLTAGE_V * 0.5

    # X confirmed clear: the train holds A alone, and the power stays cut.
    cal.advance(run, CalibrationObservation(
        now=8.5, path_present=True, head_block="A", span_blocks=("A",),
        listed_blocks=("A",)))
    assert power() == 0.0
    cal.advance(run, CalibrationObservation(
        now=9.0, path_present=True, head_block="A", span_blocks=("A",)))
    assert power() == 0.0


def test_after_the_move_the_opening_leg_runs_toward_the_other_outer_block():
    cal, run, _ = _open_run(heading_to=None)
    cal.advance(run, CalibrationObservation(now=0.0))
    cal.advance(run, CalibrationObservation(now=1.0, path_present=True))

    # The move's route completed with the train stopped in A (index 0).
    command = cal.advance(
        run,
        CalibrationObservation(
            now=5.0, is_stopped=True, head_block="A", outer_block_index=0
        ),
    )

    assert run.phase == PHASE_LEG
    assert command.arm_leg_to == "C"
    assert command.reverse_cab is False
    assert command.drive_voltage == run.max_voltage


def test_a_move_that_ends_outside_the_outer_blocks_fails_the_pass():
    cal, run, _ = _open_run(heading_to=None)
    cal.advance(run, CalibrationObservation(now=0.0))
    cal.advance(run, CalibrationObservation(now=1.0, path_present=True))

    cal.advance(run, CalibrationObservation(now=5.0, is_stopped=True, head_block="X"))

    assert run.phase == PHASE_FAILED
    assert "move" in run.failure_reason


def test_a_move_that_ends_with_the_train_still_on_b_fails_the_pass():
    cal, run, _ = _open_run(heading_to=None)
    cal.advance(run, CalibrationObservation(now=0.0))
    cal.advance(run, CalibrationObservation(now=1.0, path_present=True))

    cal.advance(
        run,
        CalibrationObservation(
            now=5.0, is_stopped=True, outer_block_index=0, measured_block_held=True
        ),
    )

    assert run.phase == PHASE_FAILED


def test_a_move_that_does_not_arrive_in_time_fails_the_pass():
    cal, run, _ = _open_run(heading_to=None)
    cal.advance(run, CalibrationObservation(now=0.0))
    cal.advance(run, CalibrationObservation(now=1.0, path_present=True))
    cal.advance(
        run, CalibrationObservation(now=1.0 + CALIBRATION_STEP_TIMEOUT_S, path_present=True)
    )
    assert run.phase == PHASE_MOVING

    cal.advance(
        run,
        CalibrationObservation(now=1.5 + CALIBRATION_STEP_TIMEOUT_S, path_present=True),
    )

    assert run.phase == PHASE_FAILED
    assert "did not reach" in run.failure_reason


def test_the_move_records_no_crossing_and_no_overrun():
    cal, run, _ = _open_run(heading_to=None)
    cal.advance(run, CalibrationObservation(now=0.0))

    cal.record_detected_entries(run, ("SIDING", "B", "A"), 2.0)

    assert run.overrun_seen is False
    assert run.crossing_started_at is None


# --- the measurement is reported on the event stream --------------------------
# design_new.md "Speed calibration", Every crossing and every step is reported.


def test_every_crossing_and_every_step_is_reported():
    from notification_center import NotificationCenter

    nc = NotificationCenter()
    cal = SpeedCalibration()
    cal.wire_dependencies(notification_center=nc)
    cal, run, _profile = _open_run(cal)
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=2.0)

    events = nc.peek_recent(100)
    crossings = [n for n in events if n.operation == "calibration_crossing"]
    steps = [n for n in events if n.operation == "calibration_step"]
    assert len(crossings) == run.crossings_target
    first = crossings[0].payload
    assert first["track_voltage"] == run.max_voltage
    assert first["seconds"] == pytest.approx(2.0)
    assert first["speed_kmh"] == pytest.approx(cm_per_sec_to_kmh(BLOCK_LENGTH_CM / 2.0))
    assert first["outcome"] == "used"
    assert {c.payload["toward_block_id"] for c in crossings} == {"A", "C"}
    (step,) = steps
    assert step.payload["recorded_kmh"] == pytest.approx(first["speed_kmh"])
    assert step.payload["crossing_count"] == run.crossings_target


def test_a_step_measured_again_is_reported_without_a_recorded_speed():
    from notification_center import NotificationCenter

    nc = NotificationCenter()
    cal = SpeedCalibration()
    cal.wire_dependencies(notification_center=nc)
    cal, run, _profile = _open_run(cal)
    clock = 0.0
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=9.0)
    for _ in range(run.crossings_target):
        clock = _crossing(cal, run, clock, seconds=1.0)  # clearly faster

    steps = [n.payload for n in nc.peek_recent(100) if n.operation == "calibration_step"]
    assert [s["recorded_kmh"] is None for s in steps] == [False, True]
    assert steps[1]["track_voltage"] == coarse_voltages(run.max_voltage)[1]
