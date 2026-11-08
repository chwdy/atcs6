"""The speed pass's two trial stages, and the ATO minimum test.

design_new.md "Speed calibration":
- The pass starts with two trial stages: the lighting trial starts at the
  train's lighting voltage, or CALIBRATION_LIGHTING_TRIAL_START_V; Keep / No
  lamps holds the lighting voltage and starts the starting trial there; It moves
  saves both voltages and starts the sweep. Trial legs drive the operator's
  voltage from the first tick, with no step timeout and no timed crossing.
- ATO minimum test: legs across the stretch at a voltage the operator adjusts;
  a stall is listed, not failed; the panel marks a voltage steady after
  ATO_MINIMUM_TEST_ROUND_TRIPS clean round trips; Commit writes the voltage.

The state machine is driven directly, as in test_speed_calibration.py.
"""

import pytest

from constants import (
    ATO_MINIMUM_TEST_ROUND_TRIPS,
    CALIBRATION_LIGHTING_TRIAL_START_V,
    CALIBRATION_STEP_TIMEOUT_S,
)
from speed_calibration import (
    CROSSING_STALLED,
    CROSSING_USED,
    PASS_LIGHTING_TRIAL,
    PASS_STARTING_TRIAL,
    PASS_SWEEP,
    PHASE_CLEARING,
    PHASE_FAILED,
    PHASE_LEG,
    PHASE_RESETTING,
    PHASE_STOPPING,
    CalibrationObservation,
    SpeedCalibration,
)
from speed_profile import SpeedProfile

BLOCK_LENGTH_CM = 100.0


class _StubTrain:
    train_id = "T1"
    length_cm = 0.0


def _profile(**fields) -> SpeedProfile:
    return SpeedProfile(
        profile_id="P",
        name="P",
        starting_voltage=2.0,
        voltage_speed_curve=[(3.0, 20.0), (12.0, 100.0)],
        **fields,
    )


def _open_pass(profile=None):
    cal = SpeedCalibration()
    run = cal.start(
        _StubTrain(),
        profile or _profile(),
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        block_length_cm=BLOCK_LENGTH_CM,
    )
    return cal, run


def _open_test(voltage=4.0, profile=None):
    cal = SpeedCalibration()
    run = cal.start_ato_minimum_test(
        _StubTrain(),
        profile or _profile(),
        track_voltage=voltage,
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        block_length_cm=BLOCK_LENGTH_CM,
    )
    return cal, run


def _tick(cal, run, now, entered=(), **observation):
    if entered:
        cal.record_detected_entries(run, entered, now)
    return cal.advance(run, CalibrationObservation(now=now, **observation))


# ---- the trial stages ------------------------------------------------------


def test_the_lighting_trial_starts_at_the_default_when_no_lighting_voltage_is_set():
    _cal, run = _open_pass()
    assert run.pass_stage == PASS_LIGHTING_TRIAL
    assert run.current_voltage == CALIBRATION_LIGHTING_TRIAL_START_V


def test_the_lighting_trial_starts_at_the_stored_lighting_voltage():
    _cal, run = _open_pass(_profile(lighting_voltage=1.2))
    assert run.current_voltage == 1.2


def test_a_trial_leg_drives_the_operators_voltage_from_its_first_tick():
    cal, run = _open_pass()
    cal.set_trial_voltage("T1", 1.7)
    command = _tick(cal, run, 0.0)
    assert command.arm_leg_to == "C"
    assert command.drive_voltage == pytest.approx(1.7)
    _tick(cal, run, 0.1, path_present=True)
    assert run.half_power is False
    assert cal.get_propulsion_override("T1").track_voltage == pytest.approx(1.7)


def test_a_trial_leg_has_no_step_timeout():
    cal, run = _open_pass()
    _tick(cal, run, 0.0)
    _tick(cal, run, 0.1, path_present=True)
    _tick(cal, run, CALIBRATION_STEP_TIMEOUT_S * 3, path_present=True)
    assert run.phase == PHASE_LEG
    assert run.pass_stage == PASS_LIGHTING_TRIAL


def test_keep_holds_the_lighting_voltage_and_starts_the_starting_trial_there():
    cal, run = _open_pass()
    cal.set_trial_voltage("T1", 1.4)
    cal.keep_lighting_voltage("T1", 1.4)
    assert run.pass_stage == PASS_STARTING_TRIAL
    assert run.held_lighting_voltage == 1.4
    assert run.current_voltage == 1.4


def test_no_lamps_starts_the_starting_trial_at_zero():
    cal, run = _open_pass()
    cal.keep_lighting_voltage("T1", None)
    assert run.held_lighting_voltage is None
    assert run.current_voltage == 0.0


def test_it_moves_hands_over_both_voltages_and_the_sweep_starts_above_the_starting_voltage():
    cal, run = _open_pass()
    cal.keep_lighting_voltage("T1", 1.4)
    cal.set_trial_voltage("T1", 2.6)
    assert cal.starting_trial_values("T1") == (1.4, 2.6)
    cal.begin_sweep("T1", 2.6)
    assert run.pass_stage == PASS_SWEEP
    assert run.starting_voltage == 2.6
    assert run.current_voltage == run.max_voltage
    # The next Plan pass tears the trial leg down and the train stops ...
    command = _tick(cal, run, 1.0, path_present=True)
    assert command.deactivate_route is True and command.drive_voltage == 0.0
    assert run.phase == PHASE_RESETTING
    assert cal.get_propulsion_override("T1").track_voltage == 0.0
    # ... then the sweep's first leg arms at max_voltage from where it stands.
    command = _tick(cal, run, 2.0, is_stopped=True, outer_block_index=0)
    assert command.arm_leg_to == "C"
    assert command.drive_voltage == run.max_voltage


def test_a_trial_leg_runs_off_until_b_reads_clear_then_turns_around():
    cal, run = _open_pass()
    _tick(cal, run, 0.0)
    _tick(cal, run, 0.1, path_present=True)
    _tick(cal, run, 5.0, ("B",), path_present=True, measured_block_held=True)
    _tick(cal, run, 30.0, ("C",), path_present=True, measured_block_held=True)
    assert run.phase == PHASE_CLEARING
    assert run.crossing_log == []  # a trial times no crossing
    _tick(cal, run, 31.0, path_present=True, measured_block_held=True)
    assert run.phase == PHASE_CLEARING
    command = _tick(cal, run, 32.0, path_present=True, measured_block_held=False)
    assert run.phase == PHASE_STOPPING and command.drive_voltage == 0.0
    assert cal.get_propulsion_override("T1").track_voltage == 0.0
    _tick(cal, run, 33.0, is_stopped=True)
    assert run.phase == PHASE_LEG and run.leg_armed is False
    assert run.heading_toward_block_id == "A"


def test_a_new_trial_voltage_clears_the_last_it_moves_refusal():
    """Rig 2026-11-06: the refusal at the lighting voltage stayed on screen
    after the operator raised the voltage, so every voltage looked refused."""
    cal, run = _open_pass()
    cal.keep_lighting_voltage("T1", 2.2)
    cal.refuse_starting_trial("T1", "lighting voltage 2.2 V is not below the starting voltage 2.2 V")
    cal.set_trial_voltage("T1", 2.6)
    assert run.trial_save_error == ""


def test_the_trial_voltage_must_stay_from_zero_to_max_voltage():
    cal, _run = _open_pass()
    with pytest.raises(ValueError):
        cal.set_trial_voltage("T1", -0.1)
    with pytest.raises(ValueError):
        cal.set_trial_voltage("T1", 12.5)


# ---- the sweep's starting voltage -------------------------------------------


def test_the_narrowing_never_runs_at_or_below_the_starting_voltage():
    cal, run = _open_pass()
    cal.keep_lighting_voltage("T1", None)
    cal.begin_sweep("T1", 2.6)
    run.pending_teardown = False
    voltages = []
    clock = 0.0
    while run.is_measuring and len(voltages) < 40:
        voltages.append(run.current_voltage)
        cal.advance(run, CalibrationObservation(now=clock, outer_block_index=0, is_stopped=True))
        far = run.heading_toward_block_id
        seconds = 10.0 / run.current_voltage  # faster at a higher voltage
        _tick(cal, run, clock + 0.5, ("B",), path_present=True)
        _tick(cal, run, clock + 0.5 + seconds, (far,), path_present=True)
        _tick(cal, run, clock + 1.5 + seconds, is_stopped=True)
        clock += 2.0 + seconds
    assert run.phase not in (PHASE_FAILED,), run.failure_reason
    assert min(voltages) > 2.6


# ---- the ATO minimum test --------------------------------------------------


def _test_leg(cal, run, clock, *, seconds=1.0):
    """One whole test leg that crosses B in `seconds`, then stops."""
    _tick(cal, run, clock, is_stopped=True)  # arm
    far = run.heading_toward_block_id
    _tick(cal, run, clock + 0.1, path_present=True)
    _tick(cal, run, clock + 1.0, ("B",), path_present=True)
    _tick(cal, run, clock + 1.0 + seconds, (far,), path_present=True)
    _tick(cal, run, clock + 2.0 + seconds)
    _tick(cal, run, clock + 3.0 + seconds, is_stopped=True)
    return clock + 4.0 + seconds


def test_the_test_refuses_a_voltage_below_the_measured_part_of_the_curve():
    with pytest.raises(ValueError):
        _open_test(2.5)
    with pytest.raises(ValueError):
        _open_test(12.5)
    cal, _run = _open_test(4.0)
    with pytest.raises(ValueError):
        cal.set_trial_voltage("T1", 2.9)


def test_the_test_lists_every_crossing_and_marks_a_steady_voltage():
    cal, run = _open_test(4.0)
    clock = 0.0
    for _ in range(2 * ATO_MINIMUM_TEST_ROUND_TRIPS):
        assert not run.is_steady
        clock = _test_leg(cal, run, clock)
    assert run.is_measuring
    assert run.is_steady
    assert all(c.outcome == CROSSING_USED and c.track_voltage == 4.0 for c in run.crossing_log)
    assert len(run.crossing_log) == 2 * ATO_MINIMUM_TEST_ROUND_TRIPS


def test_a_new_test_voltage_starts_the_steady_count_again():
    cal, run = _open_test(4.0)
    clock = 0.0
    for _ in range(2 * ATO_MINIMUM_TEST_ROUND_TRIPS):
        clock = _test_leg(cal, run, clock)
    assert run.is_steady
    cal.set_trial_voltage("T1", 4.5)
    assert run.steady_round_trips == 0
    assert run.current_voltage == 4.5


def test_a_stall_is_listed_not_failed_and_the_leg_goes_on():
    cal, run = _open_test(4.0)
    clock = _test_leg(cal, run, 0.0)
    clock = _test_leg(cal, run, clock)
    assert run.steady_legs == 2
    # The next leg reaches B, then runs past the crossing window.
    _tick(cal, run, clock, is_stopped=True)
    far = run.heading_toward_block_id
    _tick(cal, run, clock + 0.1, path_present=True)
    _tick(cal, run, clock + 1.0, ("B",), path_present=True)
    _tick(cal, run, clock + 1.0 + run.crossing_window_s + 1.0, path_present=True)
    assert run.phase == PHASE_LEG and run.is_measuring
    assert run.leg_stalled and run.steady_legs == 0
    # Long after, with no further timeout, the operator's higher voltage gets
    # it across: the crossing is listed stalled, with its time.
    cal.set_trial_voltage("T1", 5.0)
    _tick(cal, run, clock + 600.0, path_present=True)
    assert run.is_measuring
    _tick(cal, run, clock + 700.0, (far,), path_present=True)
    last = run.crossing_log[-1]
    assert last.outcome == CROSSING_STALLED and last.seconds is not None
    assert run.steady_legs == 0


def test_a_test_leg_that_stops_short_starts_again_and_lists_the_stall():
    cal, run = _open_test(4.0)
    _tick(cal, run, 0.0, is_stopped=True)
    _tick(cal, run, 0.1, path_present=True)
    command = _tick(cal, run, 5.0, path_present=False)
    assert command.deactivate_route is True
    assert run.phase == PHASE_RESETTING and run.is_measuring
    assert run.crossing_log[-1].outcome == CROSSING_STALLED
    assert run.crossing_log[-1].seconds is None


def test_commit_returns_the_voltage_and_stops_the_train_before_ending():
    cal, run = _open_test(4.0)
    cal.set_trial_voltage("T1", 4.4)
    assert cal.finish_ato_minimum_test("T1") == 4.4
    assert run.ending and "T1" in cal.runs
    _tick(cal, run, 1.0, is_stopped=True)
    assert "T1" not in cal.runs


# ---- an operator ending stops the train first (design_new.md "What ends a
# calibration run", An operator ending stops the train before it lets it go) --


def _driving_leg(cal, run):
    """Arm the next leg and let its path appear, so the run drives."""
    _tick(cal, run, 0.0)
    _tick(cal, run, 1.0, path_present=True)
    _tick(cal, run, 2.0, path_present=True)


def test_stop_mid_leg_drives_half_then_nothing_and_keeps_the_leg_route():
    cal, run = _open_pass()
    run.pass_stage = PASS_SWEEP
    run.current_step_voltage = 8.0
    _driving_leg(cal, run)
    assert cal.get_propulsion_override("T1").track_voltage == 8.0
    cal.stop("T1", reason="cancelled by the operator")
    assert run.ending and "T1" in cal.runs
    assert cal.get_propulsion_override("T1").track_voltage == pytest.approx(4.0)
    # Within the half-voltage time nothing ends, even standing.
    assert _tick(cal, run, 0.1, is_stopped=True, path_present=True) is None
    assert "T1" in cal.runs
    # After it the drive is 0, and the run waits for the train to stand.
    assert _tick(cal, run, 5.0, is_stopped=False, path_present=True) is None
    assert cal.get_propulsion_override("T1").track_voltage == 0.0
    assert "T1" in cal.runs
    command = _tick(cal, run, 6.0, is_stopped=True, path_present=True)
    assert command.deactivate_route is True
    assert "T1" not in cal.runs


def test_a_stopping_run_takes_no_sample_and_no_operator_control():
    cal, run = _open_pass()
    _driving_leg(cal, run)
    cal.stop("T1")
    cal.record_detected_entries(run, ("B",), 2.5)
    assert run.crossing_started_at is None
    with pytest.raises(ValueError):
        cal.set_trial_voltage("T1", 2.0)
    cal.stop("T1")  # a second stop changes nothing
    assert run.ending


def test_stop_on_a_standing_run_ends_on_the_next_plan_pass():
    cal, run = _open_pass()
    cal.stop("T1")
    assert cal.get_propulsion_override("T1").track_voltage == 0.0
    _tick(cal, run, 0.0, is_stopped=True)
    assert "T1" not in cal.runs


def test_stop_discards_a_finished_pass_at_once():
    cal, run = _open_pass()
    run.phase = "measured"
    cal.stop("T1")
    assert "T1" not in cal.runs


class _StubCommands:
    def __init__(self, mode):
        self.control_mode = mode
        self.calls = []

    def set_mode(self, mode):
        self.calls.append(("set_mode", mode))
        self.control_mode = mode
        return True

    def power_off_cmd(self):
        self.calls.append(("power_off_cmd", None))
        return True

    def report_result(self, operation, result):
        pass


class _StubEb:
    active = False


class _StubController:
    def __init__(self, mode="manual"):
        self.commands = _StubCommands(mode)
        self.eb = _StubEb()
        self.powered = True
        self.stopped = True

    def is_stopped(self):
        return self.stopped


def _wired(controller):
    cal = SpeedCalibration()
    cal.wire_dependencies(controllers={"T1": controller})
    return cal


def test_a_discarded_held_result_gives_the_train_back_once_it_stands():
    """design_new.md "What ends a calibration run", A run the operator ends
    gives the train back: the mode it had before opening, then OFF when it was
    OFF. A moving train waits."""
    controller = _StubController(mode="manual")
    cal = _wired(controller)
    run = cal.start(
        _StubTrain(),
        _profile(),
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        block_length_cm=BLOCK_LENGTH_CM,
        control_mode_before_opening="ato",
        powered_before_opening=False,
    )
    run.phase = "measured"
    assert not run.is_measuring
    cal.stop("T1", reason="discarded")
    assert "T1" not in cal.runs
    controller.stopped = False
    cal.give_trains_back()
    assert controller.commands.calls == []
    controller.stopped = True
    cal.give_trains_back()
    assert controller.commands.calls == [("set_mode", "ato"), ("power_off_cmd", None)]
    assert cal.pending_give_backs == {}


def test_a_run_opened_while_a_give_back_waits_carries_the_earlier_state():
    controller = _StubController(mode="manual")
    cal = _wired(controller)
    cal.pending_give_backs["T1"] = ("ato", True)
    run = cal.start(
        _StubTrain(),
        _profile(),
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        block_length_cm=BLOCK_LENGTH_CM,
        control_mode_before_opening="manual",
    )
    assert run.control_mode_before_opening == "ato"
    assert cal.pending_give_backs == {}


def test_an_abandon_that_is_not_an_operator_ending_gives_nothing_back():
    controller = _StubController(mode="manual")
    cal = _wired(controller)
    cal.start(
        _StubTrain(),
        _profile(),
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        block_length_cm=BLOCK_LENGTH_CM,
        control_mode_before_opening="ato",
    )
    cal.abandon("T1", reason="emergency brake")
    cal.give_trains_back()
    assert controller.commands.calls == []
