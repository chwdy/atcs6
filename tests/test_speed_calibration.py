"""C5 — Speed calibration: voltage sweep, measurement, curve commit.

design_new.md "Speed calibration": a pass commands a fixed sweep of
`track_voltage` steps and measures the speed reached at each, writing the
measured `(voltage, power)` and `(power, speed)` points onto the locomotive's
`SpeedProfile`. In simulation the measured speed reproduces the model; on real
hardware a `measured_speed_fn` supplies the physical reading. Auto measures the
whole sweep at once; manual averages `CALIBRATION_MANUAL_LAPS` operator-marked
laps per step.
"""

from constants import CALIBRATION_MANUAL_LAPS, CALIBRATION_VOLTAGE_STEPS
from speed_calibration import SpeedCalibration
from speed_profile import make_default_speed_profile


class _StubTrain:
    train_id = "T1"


def test_start_builds_the_commanded_voltage_sweep():
    profile = make_default_speed_profile("T1", 100.0)
    run = SpeedCalibration().start(_StubTrain(), profile, mode="auto")
    assert len(run.commanded_voltages) == CALIBRATION_VOLTAGE_STEPS
    assert run.commanded_voltages[0] == profile.lighting_voltage
    assert run.commanded_voltages[-1] == profile.max_voltage


def test_auto_pass_fills_and_commits_curves():
    profile = make_default_speed_profile("T1", 100.0)
    cal = SpeedCalibration()
    run = cal.start(_StubTrain(), profile, mode="auto")
    cal.run_pass(run, profile)
    assert run.is_complete
    assert len(run.samples) == CALIBRATION_VOLTAGE_STEPS
    # Committed onto the profile, and the run is cleared.
    assert len(profile.voltage_power_curve) == CALIBRATION_VOLTAGE_STEPS
    assert len(profile.power_speed_curve) >= 2
    assert "T1" not in cal.runs


def test_sim_measurement_reproduces_the_model():
    profile = make_default_speed_profile("T1", 100.0)
    cal = SpeedCalibration()
    run = cal.start(_StubTrain(), profile, mode="auto")
    cal.run_pass(run, profile)
    # Each measured speed equals the model's response at the step's power.
    for s in run.samples:
        assert abs(s.speed_kmh - profile.power_percent_to_speed(s.power_percent)) < 1e-9


def test_hardware_measured_speed_fn_overrides_the_model():
    profile = make_default_speed_profile("T1", 100.0)
    cal = SpeedCalibration()
    run = cal.start(_StubTrain(), profile, mode="auto")
    cal.run_pass(run, profile, measured_speed_fn=lambda v, p: 42.0)
    assert all(speed == 42.0 for _power, speed in profile.power_speed_curve)


def test_manual_mode_averages_laps_then_commits():
    profile = make_default_speed_profile("T1", 100.0)
    cal = SpeedCalibration()
    run = cal.start(_StubTrain(), profile, mode="manual")
    steps = len(run.commanded_voltages)
    for _step in range(steps):
        sample = None
        for _lap in range(CALIBRATION_MANUAL_LAPS):
            sample = cal.record("T1", profile)
        # the final lap of each step commits one averaged sample
        assert sample is not None
    assert run.is_complete
    assert "T1" not in cal.runs
    assert len(profile.power_speed_curve) >= 2


def test_manual_record_midstep_returns_none():
    profile = make_default_speed_profile("T1", 100.0)
    cal = SpeedCalibration()
    cal.start(_StubTrain(), profile, mode="manual")
    if CALIBRATION_MANUAL_LAPS > 1:
        assert cal.record("T1", profile) is None  # first lap, not yet averaged
