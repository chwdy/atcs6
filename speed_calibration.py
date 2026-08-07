"""Speed calibration — measure a locomotive's voltage/power/speed response and
write it into its `SpeedProfile` (design_new.md "Speed calibration").

A calibration pass commands a fixed sweep of `track_voltage` steps and measures
the speed the train actually reaches at each, instead of asking for a target
speed: the second-stage curve that turns a target speed into a control value is
the very thing the pass is building, so it cannot lean on it. Each step yields
one `(track_voltage, power_percent)` point for `voltage_power_curve` and one
`(power_percent, speed_kmh)` point for `power_speed_curve`. On completion the
collected points are written onto the locomotive's linked `SpeedProfile`, so the
calibration stays tied to the locomotive across placements.

`CalibrationRun` is the transient working record of one pass — in-memory only;
only its committed result (the profile curves) persists, through ordinary
`SpeedProfile` persistence. Two measurement modes:
- **Auto** times the loco against block-current detection (head crosses two
  block seams a known distance apart). `run_pass` measures every step.
- **Manual** times the loco against an operator marker: `record` is issued as
  the head passes a chosen point each lap, and calibration averages
  `CALIBRATION_MANUAL_LAPS` laps per step so one noisy lap does not set a point.

Both produce the same `(track_voltage, power_percent, speed_kmh)` samples and
feed the same commit. In simulation the measured speed comes from the model, so
a pass reproduces the model's own curve; on real hardware the same pass captures
the physical locomotive's true response. The measurement source is the only
difference — `measured_speed_fn` supplies it, defaulting to the model.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from constants import CALIBRATION_MANUAL_LAPS, CALIBRATION_VOLTAGE_STEPS


@dataclass(frozen=True)
class CalibrationSample:
    """One measured calibration point."""

    track_voltage: float
    power_percent: float
    speed_kmh: float


@dataclass
class CalibrationRun:
    """Transient working record of one calibration pass (in-memory only)."""

    train_id: str
    profile_id: str
    mode: str  # "auto" | "manual"
    commanded_voltages: List[float]
    samples: List[CalibrationSample] = field(default_factory=list)
    step_index: int = 0
    # Manual mode: lap speeds accumulated for the current voltage step.
    _lap_speeds: List[float] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.step_index >= len(self.commanded_voltages)


# Real hardware supplies the measured speed for a commanded (voltage, power);
# in simulation it defaults to the model's own response.
MeasuredSpeedFn = Callable[[float, float], float]


class SpeedCalibration:
    """Runs calibration passes and commits the measured curves.

    Holds the active `CalibrationRun` per train. Calibration is a controlled
    pass — it must not run while the train is following an ATO route; that gate
    is enforced by the caller (the service layer) before `start`.
    """

    def __init__(self, simulation_controller=None) -> None:
        self._sc = simulation_controller
        self.runs: Dict[str, CalibrationRun] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self, train, profile, mode: str = "auto") -> CalibrationRun:
        """Begin a calibration pass over the commanded voltage sweep."""
        run = CalibrationRun(
            train_id=train.train_id,
            profile_id=profile.profile_id,
            mode="manual" if mode == "manual" else "auto",
            commanded_voltages=self._commanded_voltages(profile),
        )
        self.runs[train.train_id] = run
        return run

    @staticmethod
    def _commanded_voltages(profile) -> List[float]:
        """`CALIBRATION_VOLTAGE_STEPS` evenly-spaced voltages from the profile's
        `lighting_voltage` (0% power anchor) to its `max_voltage` (100%)."""
        lo = float(getattr(profile, "lighting_voltage", 1.5))
        hi = float(getattr(profile, "max_voltage", 12.0))
        n = max(2, int(CALIBRATION_VOLTAGE_STEPS))
        return [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    # -- measurement ------------------------------------------------------

    def run_pass(
        self,
        run: CalibrationRun,
        profile,
        measured_speed_fn: Optional[MeasuredSpeedFn] = None,
    ) -> CalibrationRun:
        """Auto mode: measure every commanded step, then commit."""
        while not run.is_complete:
            self._measure_step(run, profile, measured_speed_fn)
        self._commit(run, profile)
        return run

    def record(
        self,
        train_id: str,
        profile,
        measured_speed_fn: Optional[MeasuredSpeedFn] = None,
    ) -> Optional[CalibrationSample]:
        """Manual mode: the operator marks one lap. Accumulates lap speeds for
        the current voltage step; after `CALIBRATION_MANUAL_LAPS` laps it
        averages them into one sample, advances, and commits when complete.
        Returns the committed sample for the step, or None mid-step."""
        run = self.runs.get(train_id)
        if run is None or run.is_complete:
            return None
        voltage = run.commanded_voltages[run.step_index]
        power = float(profile.voltage_to_power(voltage))
        run._lap_speeds.append(self._measure_speed(voltage, power, profile, measured_speed_fn))
        if len(run._lap_speeds) < max(1, int(CALIBRATION_MANUAL_LAPS)):
            return None
        avg_speed = sum(run._lap_speeds) / len(run._lap_speeds)
        sample = CalibrationSample(track_voltage=voltage, power_percent=power, speed_kmh=avg_speed)
        run.samples.append(sample)
        run.step_index += 1
        run._lap_speeds = []
        if run.is_complete:
            self._commit(run, profile)
        return sample

    def _measure_step(
        self,
        run: CalibrationRun,
        profile,
        measured_speed_fn: Optional[MeasuredSpeedFn],
    ) -> CalibrationSample:
        voltage = run.commanded_voltages[run.step_index]
        power = float(profile.voltage_to_power(voltage))
        speed = self._measure_speed(voltage, power, profile, measured_speed_fn)
        sample = CalibrationSample(track_voltage=voltage, power_percent=power, speed_kmh=speed)
        run.samples.append(sample)
        run.step_index += 1
        return sample

    @staticmethod
    def _measure_speed(
        voltage: float,
        power: float,
        profile,
        measured_speed_fn: Optional[MeasuredSpeedFn],
    ) -> float:
        """The speed measured at a commanded (voltage, power). Real hardware
        reads physical motion via `measured_speed_fn`; in simulation it is the
        model's own response (`SpeedProfile.power_percent_to_speed`)."""
        if measured_speed_fn is not None:
            return float(measured_speed_fn(voltage, power))
        return float(profile.power_percent_to_speed(power))

    # -- commit -----------------------------------------------------------

    def _commit(self, run: CalibrationRun, profile) -> None:
        """Write the collected samples onto the locomotive's `SpeedProfile`:
        measured `(voltage, power)` become `voltage_power_curve` and
        `(power, speed)` become `power_speed_curve`. Clears the run and reports
        the result through the `command_result` stream."""
        profile.voltage_power_curve = sorted(
            {(s.track_voltage, s.power_percent) for s in run.samples}
        )
        profile.power_speed_curve = sorted(
            {(s.power_percent, s.speed_kmh) for s in run.samples}
        )
        self.runs.pop(run.train_id, None)
        self._emit_result(
            run, success=True, reason=f"calibrated {len(run.samples)} points"
        )

    def _emit_result(self, run: CalibrationRun, *, success: bool, reason: str) -> None:
        """Report progress/result through the existing `command_result` stream
        (no new event sink), per design_new.md "Speed calibration"."""
        sc = self._sc
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref

            nc.emit(
                source="SpeedCalibration",
                operation="speed_calibration",
                kind=Kind.INFO if success else Kind.WARNING,
                message=reason or (
                    "speed calibration finished" if success
                    else "speed calibration failed"
                ),
                entities=[train_ref(run.train_id)],
                payload={
                    "command_id": f"calibrate:{run.train_id}",
                    "command_name": "speed_calibration",
                    "train_id": run.train_id,
                    "success": success,
                    "reason": reason,
                },
            )
        except Exception:
            pass
