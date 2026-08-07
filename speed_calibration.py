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

This class also owns the second, smaller procedure (design_new.md
"Lighting-voltage calibration"): finding the one voltage at which a locomotive
is lit and still holds, and writing it to `lighting_voltage`. It comes first for
a newly added locomotive, because a speed pass takes its bottom step from that
anchor. It has no measurement step at all — the operator watching the
locomotive is the instrument — so `LightingCalibrationRun` carries a candidate
voltage the operator steps up and down and nothing else.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from constants import (
    CALIBRATION_MANUAL_LAPS,
    CALIBRATION_VOLTAGE_STEPS,
    LIGHTING_VOLTAGE_STEP_V,
)


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


@dataclass
class LightingCalibrationRun:
    """Transient working record of one lighting-voltage run (in-memory only).

    Holds the candidate voltage the operator is stepping through and the
    ceiling it may not pass. `TrainPhysics` reads the candidate while the run
    is open and drives it onto the rails in place of the stopped-but-lit
    voltage, so each step is answered by the locomotive itself.
    """

    train_id: str
    profile_id: str
    candidate_voltage: float
    ceiling_voltage: float


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
        self.lighting_runs: Dict[str, LightingCalibrationRun] = {}

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

    # -- lighting voltage -------------------------------------------------
    # design_new.md "Lighting-voltage calibration". No measurement step: the
    # pass puts a candidate voltage on the rails and the operator says whether
    # the locomotive moved. Nothing on the rig can answer that — at this level
    # the drive is too weak to load a current channel (that is what the
    # lighting zone is) and first movement is a creep of a few millimetres.

    def start_lighting(self, train, profile) -> LightingCalibrationRun:
        """Open a lighting-voltage run for this train, seeded from the profile.

        The caller (the service layer) enforces the state gates first.
        """
        ceiling = self.lighting_ceiling(profile)
        run = LightingCalibrationRun(
            train_id=train.train_id,
            profile_id=profile.profile_id,
            candidate_voltage=_clamp(
                float(getattr(profile, "lighting_voltage", 0.0)), 0.0, ceiling
            ),
            ceiling_voltage=ceiling,
        )
        self.lighting_runs[train.train_id] = run
        return run

    def step_lighting(self, train_id: str, direction: int) -> Optional[LightingCalibrationRun]:
        """Move the candidate one `LIGHTING_VOLTAGE_STEP_V` up or down.

        The step control stops at the ceiling and at `0`, so the panel can
        never show a value `commit_lighting` would refuse.
        """
        run = self.lighting_runs.get(train_id)
        if run is None:
            return None
        sign = 1.0 if int(direction) >= 0 else -1.0
        run.candidate_voltage = _clamp(
            run.candidate_voltage + sign * LIGHTING_VOLTAGE_STEP_V,
            0.0,
            run.ceiling_voltage,
        )
        return run

    def commit_lighting(self, train_id: str, profile) -> Optional[float]:
        """Write the candidate to `lighting_voltage` and close the run.

        `lighting_voltage` is also the 0%-power point of `voltage_power_curve`,
        so the commit moves that curve's bottom anchor with it. A stored point
        at or below the new anchor is dropped: it names a voltage that does not
        move this locomotive.
        """
        run = self.lighting_runs.get(train_id)
        if run is None:
            return None
        value = _clamp(run.candidate_voltage, 0.0, self.lighting_ceiling(profile))
        profile.lighting_voltage = value
        profile.voltage_power_curve = self._reanchored_curve(profile, value)
        self.lighting_runs.pop(train_id, None)
        self._emit_command_result(
            train_id,
            operation="lighting_calibration",
            success=True,
            reason=f"lighting voltage set to {value:.2f} V",
        )
        return value

    def cancel_lighting(self, train_id: str) -> None:
        """Close the run without writing. The candidate is discarded and the
        train's `track_voltage` goes back to what its state ordinarily outputs."""
        self.lighting_runs.pop(train_id, None)

    def lighting_candidate(self, train_id: str) -> Optional[float]:
        """The open run's candidate voltage, or None when no run is open.

        This is the read `TrainPhysics.compute_track_voltage(...)` makes.
        """
        run = self.lighting_runs.get(train_id)
        return None if run is None else run.candidate_voltage

    @staticmethod
    def lighting_ceiling(profile) -> float:
        """The highest candidate a run may reach.

        A candidate stays below the profile's `max_voltage` and below the
        Hardware Setup's `current_sense_min_drive_v` — the second because
        design_new.md "Power" requires that level to sit above the profile's
        `lighting_voltage`, so a lit but motionless train lands in the lighting
        zone instead of reading as driven. This pass is the one thing that
        moves `lighting_voltage`, so it is where that bound is kept.
        """
        hi = float(getattr(profile, "max_voltage", 12.0))
        try:
            from hardware_config import get_hardware_config

            hi = min(hi, float(get_hardware_config().current_sense_min_drive_v))
        except Exception:
            pass
        return max(0.0, round(hi - LIGHTING_VOLTAGE_STEP_V, 3))

    @staticmethod
    def _reanchored_curve(profile, anchor: float) -> List[Tuple[float, float]]:
        """`voltage_power_curve` with its 0%-power point moved to `anchor`.

        A point is dropped when it sits at or below the new anchor (it names a
        voltage that does not move this locomotive) and when it carries 0%
        power (it is the old anchor). The second test is what keeps the curve
        to exactly one 0% point when the anchor moves *down*: the old anchor is
        above the new one, so the first test alone would leave it in place.
        """
        kept = [
            (float(v), float(p))
            for v, p in getattr(profile, "voltage_power_curve", []) or []
            if float(v) > anchor and float(p) > 0.0
        ]
        if not kept:
            kept = [(float(getattr(profile, "max_voltage", 12.0)), 100.0)]
        return [(anchor, 0.0)] + sorted(kept)

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
        self._emit_command_result(
            run.train_id, operation="speed_calibration", success=success, reason=reason
        )

    def _emit_command_result(
        self, train_id: str, *, operation: str, success: bool, reason: str
    ) -> None:
        """One command-result event on the stream `NotificationCenter` owns.

        Both calibration procedures report here; neither opens an event sink of
        its own (design_new.md "Speed calibration" / "Lighting-voltage
        calibration").
        """
        sc = self._sc
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref

            nc.emit(
                source="SpeedCalibration",
                operation=operation,
                kind=Kind.INFO if success else Kind.WARNING,
                message=reason or (
                    f"{operation} finished" if success else f"{operation} failed"
                ),
                entities=[train_ref(train_id)],
                payload={
                    "command_id": f"calibrate:{train_id}",
                    "command_name": operation,
                    "train_id": train_id,
                    "success": success,
                    "reason": reason,
                },
            )
        except Exception:
            pass


def _clamp(value: float, low: float, high: float) -> float:
    """Hold a candidate voltage inside `[low, high]`, on a clean step grid."""
    return round(min(max(float(value), low), high), 3)
