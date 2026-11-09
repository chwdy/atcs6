"""Play the readings of a detector reading log back into a SimulationController.

The detector reading log (design_new.md "Event log", Detector reading log) writes
one line per pass, and most lines list only the blocks whose reading changed.
This module turns those lines back into what each pass actually read, and feeds
them to the runtime one pass at a time, so a rig run that went wrong can be
re-run in a test with no rig attached.

Three pieces:

- `load_passes(path, ...)` reads one or more log files (or a cut-down file made by
  `tools/cut_reading_log.py`) and returns one `RecordedPass` per line, each holding
  every block's reading at that pass.
- `ReplayOccupancyInput` is an `OccupancyInput` that answers
  `read_block_currents()` with the `present` values of the pass it was last
  given. Pass it to `SimulationController(..., input_interface=...)` in place of
  a hardware driver: the runtime then treats occupancy as coming from real
  detectors, exactly as it did on the rig.
- `ReadingPlayer` steps the controller through the passes: a `"tick"` pass runs
  `update(...)` with the deltas the rig used, a `"stopped"` pass runs
  `poll_hardware_occupancy()`. A test calls `play_to(n)`, does what the operator
  did between pass n and pass n+1 (adopt a train, power it on, ...), and plays on.

What a replay can and cannot show: the readings are played back exactly as the
rig recorded them. They do not react to what the replayed app does. If the code
under test drives a train differently from the rig run, the recorded train still
moves as it did on the rig. A replay is therefore a faithful test of what the app
decides from a given series of readings — attribution, unidentified trains, the
clear rule, detector noise — and not of how a train responds to the voltage the
app sets.

If the controller runs no detector read on a pass the rig read (for example, the
test left the session not active), `ReadingPlayer` raises `ReplayDiverged` naming
the pass, instead of silently skipping readings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from block_occupancy import OccupancyInput

PASS_TICK = "tick"
PASS_STOPPED = "stopped"


class ReplayDiverged(AssertionError):
    """The replayed runtime did not do what the rig did on a recorded pass."""


@dataclass(frozen=True)
class RecordedPass:
    """One pass of the recorded run, with every block's reading at that pass.

    `readings` maps block id to the reading object the log wrote for it
    (`present`, plus `source`, `zone`, `current_ma`, `threshold_ma`, `raw_bit` and
    `held_from_previous` when the hardware driver reported them).
    `full` is the line's `full` value when the line listed every block, else None.
    `line` is the line as written, for anything else a test wants to read from it.
    """

    pass_no: int
    pass_kind: str
    wall_time: float
    simulation_time: float
    delta_seconds: float
    operating_delta_seconds: float
    readings: Dict[str, Dict[str, Any]]
    full: Optional[str] = None
    line: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def present(self) -> Dict[str, bool]:
        """block id -> the boolean the runtime's debounce counted."""
        return {block_id: bool(r.get("present")) for block_id, r in self.readings.items()}

    def present_blocks(self) -> Set[str]:
        return {block_id for block_id, r in self.readings.items() if r.get("present")}


def read_lines(path: str | os.PathLike) -> List[Dict[str, Any]]:
    """Every line of one file as a dict. A malformed line is an error here,
    unlike `detector_reading_log.read_detector_reading_log`: a fixture with a
    broken line would replay the wrong readings without saying so."""
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for number, text in enumerate(f, start=1):
            text = text.strip()
            if not text:
                continue
            try:
                out.append(json.loads(text))
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: not a JSON line: {exc}") from exc
    return out


def fold_lines(lines: Iterable[Dict[str, Any]]) -> List[RecordedPass]:
    """Turn log lines, in file order, into one `RecordedPass` per line.

    The first line must carry `full` (list every block): before it there is
    nothing to fold the changes onto. A line with `full` replaces the state; any
    other line changes only the blocks it lists. A gap in `pass` means lines were
    lost to a failed write, and the log then writes a `full` line; a gap that is
    not followed straight away by a `full` line leaves the state unknown, so it is
    an error.
    """
    passes: List[RecordedPass] = []
    state: Dict[str, Dict[str, Any]] = {}
    previous: Optional[int] = None
    for line in lines:
        pass_no = int(line["pass"])
        full = line.get("full")
        if previous is None and full is None:
            raise ValueError(
                f"pass {pass_no}: the first line must list every block (carry `full`); "
                "cut the file with tools/cut_reading_log.py, which writes that line"
            )
        if previous is not None and full is None and pass_no != previous + 1:
            raise ValueError(
                f"pass {pass_no} follows pass {previous} with lines missing and no "
                "line listing every block, so the readings in between are unknown"
            )
        if full is not None:
            state = {}
        for block_id, reading in (line.get("readings") or {}).items():
            state[block_id] = dict(reading)
        passes.append(
            RecordedPass(
                pass_no=pass_no,
                pass_kind=str(line.get("pass_kind", PASS_TICK)),
                wall_time=float(line.get("wall_time", 0.0)),
                simulation_time=float(line.get("simulation_time", 0.0)),
                delta_seconds=float(line.get("delta_seconds", 0.0)),
                operating_delta_seconds=float(line.get("operating_delta_seconds", 0.0)),
                readings={block_id: dict(r) for block_id, r in state.items()},
                full=full,
                line=line,
            )
        )
        previous = pass_no
    return passes


def load_passes(
    *paths: str | os.PathLike,
    from_pass: Optional[int] = None,
    to_pass: Optional[int] = None,
) -> List[RecordedPass]:
    """Read the files in order and return the passes from `from_pass` to
    `to_pass`, both included.

    Several files are read as one run, for a run whose log was split into a new
    file part-way (`full = "new_file"`). The readings are folded from the first
    line of the first file, so a pass taken from the middle still holds every
    block's reading.
    """
    lines: List[Dict[str, Any]] = []
    for path in paths:
        lines.extend(read_lines(path))
    passes = fold_lines(lines)
    return [
        p
        for p in passes
        if (from_pass is None or p.pass_no >= from_pass)
        and (to_pass is None or p.pass_no <= to_pass)
    ]


class ReplayOccupancyInput(OccupancyInput):
    """An `OccupancyInput` that answers with a recorded pass's readings.

    `load(recorded_pass)` sets the readings the next `read_block_currents()`
    returns. `last_read_detail()` returns the hardware detail the rig's driver
    reported for that pass, so a replayed run writes the same detector reading
    log line the rig wrote. `detected_block_ids()` is every block the recording
    names: those blocks had a physical detector behind them on the rig.
    """

    def __init__(self, passes: Sequence[RecordedPass] = ()) -> None:
        self._present: Dict[str, bool] = {}
        self._detail: Dict[str, Dict[str, Any]] = {}
        self._blocks: Set[str] = set()
        for p in passes:
            self._blocks.update(p.readings)
        #: How many times the runtime read this input. `ReadingPlayer` checks it
        #: moved by one on every pass.
        self.read_count = 0

    def load(self, recorded_pass: RecordedPass) -> None:
        self._present = recorded_pass.present()
        self._detail = {
            block_id: {k: v for k, v in r.items() if k != "present"}
            for block_id, r in recorded_pass.readings.items()
        }
        self._blocks.update(recorded_pass.readings)

    def read_block_currents(self) -> Dict[str, bool]:
        self.read_count += 1
        return dict(self._present)

    def last_read_detail(self) -> Dict[str, Dict[str, Any]]:
        return {block_id: dict(d) for block_id, d in self._detail.items() if d}

    def detected_block_ids(self) -> Set[str]:
        return set(self._blocks)


class ReadingPlayer:
    """Steps a `SimulationController` through recorded passes.

    The controller must have been built with `input_interface=replay_input`, the
    same `ReplayOccupancyInput` given here, so its detector reads come from the
    recording.
    """

    def __init__(
        self,
        sim: Any,
        replay_input: ReplayOccupancyInput,
        passes: Sequence[RecordedPass],
    ) -> None:
        if getattr(sim, "input_interface", None) is not replay_input:
            raise ValueError(
                "the controller does not read this ReplayOccupancyInput; build it "
                "with SimulationController(..., input_interface=replay_input)"
            )
        if not passes:
            raise ValueError("no passes to play")
        self.sim = sim
        self.input = replay_input
        self.passes = list(passes)
        self._next = 0

    @property
    def last_played(self) -> Optional[RecordedPass]:
        """The pass played last, or None before the first."""
        return self.passes[self._next - 1] if self._next > 0 else None

    @property
    def next_pass_no(self) -> Optional[int]:
        return self.passes[self._next].pass_no if self._next < len(self.passes) else None

    def pass_by_number(self, pass_no: int) -> RecordedPass:
        for p in self.passes:
            if p.pass_no == pass_no:
                return p
        raise KeyError(f"pass {pass_no} is not in this recording")

    def play_to(self, pass_no: Optional[int] = None) -> Optional[RecordedPass]:
        """Play every pass up to and including `pass_no` (all of them when None).

        Returns the last pass played.
        """
        last = self.passes[-1].pass_no
        target = last if pass_no is None else pass_no
        if target > last:
            raise ValueError(f"pass {target} is past the recording's last pass {last}")
        while self._next < len(self.passes) and self.passes[self._next].pass_no <= target:
            self._play_one(self.passes[self._next])
            self._next += 1
        return self.last_played

    def _play_one(self, recorded: RecordedPass) -> None:
        self.input.load(recorded)
        before = self.input.read_count
        if recorded.pass_kind == PASS_STOPPED:
            self.sim.poll_hardware_occupancy()
        else:
            self.sim.update(
                recorded.delta_seconds,
                operating_delta_seconds=recorded.operating_delta_seconds,
            )
        reads = self.input.read_count - before
        if reads != 1:
            raise ReplayDiverged(
                f"pass {recorded.pass_no} was a {recorded.pass_kind!r} pass that read "
                f"the detectors once on the rig, but the replay read them {reads} "
                f"time(s) (environment_active={self.sim.environment_active}, "
                f"hardware_failure={self.sim.hardware_failure}, "
                f"failed_runtime={self.sim.failed_runtime})"
            )
