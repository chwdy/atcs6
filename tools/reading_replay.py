"""Play the readings of a detector reading log back into a SimulationController.

The detector reading log (design_v2.md "Detector reading log") writes
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
  a full runtime step, a `"stopped"` pass runs the stopped pass, each with the
  pass's recorded `wall_time` (design_v2.md "Detector reading log", A recorded
  run can be replayed in a test). A test calls `play_to(n)`, does what the
  operator did between pass n and pass n+1 (turn a knob, set a destination,
  ...), and plays on.

What a replay can and cannot show: the readings are played back exactly as the
rig recorded them. They do not react to what the replayed app does. If the code
under test drives a train differently from the rig run, the recorded train still
moves as it did on the rig. A replay is therefore a faithful test of what the
runtime decides from a given series of readings — which blocks each train
holds, claims, the hold time, detector noise — and not of how a train responds
to the voltage the runtime sets.

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
        if "command" in line:
            continue  # an operator command, not a pass (see `RecordedRun`)
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
        play_pass(self.sim, self.input, recorded)


def play_pass(sim: Any, replay_input: ReplayOccupancyInput, recorded: RecordedPass) -> None:
    """Run one recorded pass on `sim`, reading `recorded`'s readings.

    A `"tick"` pass runs a full runtime step and a `"stopped"` pass runs the
    stopped pass, both at the pass's recorded `wall_time`, as the runtime loop
    ran them on the rig. Raises `ReplayDiverged` when the pass did not read
    `replay_input` exactly once, as the rig's pass did.
    """
    replay_input.load(recorded)
    before = replay_input.read_count
    live = recorded.pass_kind != PASS_STOPPED
    sim.note_session_live(live)
    sim.run_train_step(live=live, wall_time=recorded.wall_time)
    reads = replay_input.read_count - before
    if reads != 1:
        raise ReplayDiverged(
            f"pass {recorded.pass_no} was a {recorded.pass_kind!r} pass that read "
            f"the detectors once on the rig, but the replay read them {reads} "
            f"time(s) (environment_active={sim.environment_active}, "
            f"hardware_failure={sim.hardware_failure}, "
            f"reads_the_recording={sim.input_interface is replay_input})"
        )


# ---------------------------------------------------------------------------
# Whole recorded runs: the start state, the operator commands and the readings
# ---------------------------------------------------------------------------

#: State-changing requests a replay does not send: they drive or read a device
#: (a scan, a port test, the bench, commissioning) or poke the Virtual Hardware
#: device, and the readings they caused are already in the recording. A
#: hardware setup edit (`/api/hardware/config`, `/api/hardware/binding`) is
#: data only, so it is sent.
DEVICE_ROUTE_PREFIXES = ("/api/hardware/", "/api/virtual-hardware/", "/api/debug/hw")
DATA_ONLY_HARDWARE_ROUTES = ("/api/hardware/config", "/api/hardware/binding")


def sends_by_default(command: "RecordedCommand") -> bool:
    if command.path in DATA_ONLY_HARDWARE_ROUTES:
        return True
    return not command.path.startswith(DEVICE_ROUTE_PREFIXES)


@dataclass(frozen=True)
class RecordedCommand:
    """One operator request the server answered, from a line carrying `command`."""

    command_no: int
    after_pass: int
    method: str
    path: str
    query: str
    body: Any
    status: int
    wall_time: float = 0.0

    @classmethod
    def from_line(cls, line: Dict[str, Any]) -> "RecordedCommand":
        c = line["command"]
        return cls(
            command_no=int(line.get("command_no", 0)),
            after_pass=int(line["after_pass"]),
            method=str(c["method"]),
            path=str(c["path"]),
            query=str(c.get("query") or ""),
            body=c.get("body"),
            status=int(c["status"]),
            wall_time=float(line.get("wall_time", 0.0)),
        )

    def describe(self) -> str:
        url = self.path + (f"?{self.query}" if self.query else "")
        return f"command {self.command_no} ({self.method} {url}, after pass {self.after_pass})"


@dataclass
class RecordedRun:
    """A run recorded from the server's boot: the state it booted with, every
    pass's readings, and every operator command with the pass it ran after."""

    start_state: Dict[str, Any]
    passes: List[RecordedPass]
    commands: List[RecordedCommand]


def load_run(*paths: str | os.PathLike, to_pass: Optional[int] = None) -> RecordedRun:
    """Read a run from its log files, oldest first.

    The first pass line must be the run's `"start"` line, carrying
    `start_state`: a replay can only start where the server started, because
    nothing else records the runtime state a later pass depends on. Commands
    are ordered by `command_no`; a gap in it means a command line was lost, so
    it is an error.
    """
    lines: List[Dict[str, Any]] = []
    for path in paths:
        lines.extend(read_lines(path))
    pass_lines = [line for line in lines if "command" not in line]
    if not pass_lines:
        raise ValueError("the recording has no passes")
    first = pass_lines[0]
    if first.get("full") != "start" or "start_state" not in first:
        raise ValueError(
            f"pass {first.get('pass')}: a run replays from the server's boot; the "
            "first pass line must be the `start` line carrying `start_state` (a log "
            "written before the start state was recorded can only be replayed with "
            "ReadingPlayer, setting the starting state by hand)"
        )
    commands = sorted(
        (RecordedCommand.from_line(line) for line in lines if "command" in line),
        key=lambda c: c.command_no,
    )
    for expected, command in enumerate(commands, start=1):
        if command.command_no != expected:
            raise ValueError(f"command {expected} is missing from the recording")
    passes = fold_lines(pass_lines)
    if to_pass is not None:
        passes = [p for p in passes if p.pass_no <= to_pass]
        commands = [c for c in commands if c.after_pass < to_pass]
    return RecordedRun(start_state=first["start_state"], passes=passes, commands=commands)


class _NoDevice(RuntimeError):
    pass


def _refuse_device(*_args: Any, **_kwargs: Any) -> Any:
    raise _NoDevice("a replay never opens a hardware device")


class RunReplay:
    """Rebuild the server from a recorded run's start state and play the run.

    Use as a context manager. It points the hardware setup, the selected
    environment and the layouts folder at `work_dir`, and blocks every
    hardware device from opening, so a replay never
    writes the operator's files or touches a rig that happens to be plugged in.
    Activation is given the recording's readings in place of a device.

    `play_to(n)` sends each recorded command after the pass it ran after, then
    plays the pass. A command whose answer differs from the rig's is noted in
    `divergences`, which a test normally expects to be empty. A command
    `send(command)` turns down is noted in `skipped`.

    Between `play_to` calls the test reads `state`, `network`, `sim` and
    `client` as it would on a live server.
    """

    def __init__(
        self,
        run: RecordedRun,
        work_dir: str | os.PathLike,
        *,
        send: Any = sends_by_default,
        reading_log_dir: str | os.PathLike | None = None,
    ) -> None:
        self.run = run
        self.work_dir = os.fspath(work_dir)
        self.send = send
        self.reading_log_dir = reading_log_dir
        self.divergences: List[str] = []
        self.skipped: List[RecordedCommand] = []
        self.input = ReplayOccupancyInput(run.passes)
        self._next_pass = 0
        self._next_command = 0
        self._stack: Any = None

    # -- set up ------------------------------------------------------------

    def __enter__(self) -> "RunReplay":
        import contextlib
        from unittest import mock

        import bootstrap
        import hardware.ch347_device
        import hardware_config
        import operation_environment
        from app_factory import create_app
        from app_state import AppState
        from detector_reading_log import SimulationLock
        from network import RailwayNetwork
        from persistence import PersistenceManager

        start = self.run.start_state

        stack = contextlib.ExitStack()
        self._stack = stack
        try:
            os.makedirs(self.work_dir, exist_ok=True)
            stack.enter_context(mock.patch.object(
                hardware_config, "CONFIG_PATH", os.path.join(self.work_dir, "hardware_config.json")
            ))
            stack.enter_context(mock.patch.object(
                operation_environment,
                "ENVIRONMENT_PATH",
                os.path.join(self.work_dir, "operation_environment.json"),
            ))
            stack.enter_context(mock.patch.object(
                operation_environment, "_selected", operation_environment.get_selected()
            ))
            stack.enter_context(mock.patch.object(
                hardware.ch347_device, "open_ch347_device", _refuse_device
            ))
            replay_input = self.input

            def build_session_interfaces(cfg=None, *, environment=None, track_polarity_of=None):
                return replay_input, None, None

            stack.enter_context(mock.patch.object(
                hardware_config, "build_session_interfaces", build_session_interfaces
            ))
            stack.callback(hardware_config.reset_to_defaults)

            # What boot read from disk, written where boot will read it.
            hardware_config.reset_to_defaults()
            hardware_config.apply_snapshot(start["hardware"])
            operation_environment.select(start["selected_environment"])

            network = RailwayNetwork()
            network.from_dict(start["layout"])
            stack.enter_context(mock.patch.object(
                bootstrap, "create_empty_network", lambda: network
            ))
            network, sim, _pm = bootstrap.bootstrap_runtime(
                demo=False,
                detector_reading_log_dir=self.reading_log_dir,
            )
            lock = SimulationLock()
            self.state = AppState(
                network=network,
                sim_controller=sim,
                persistence_manager=PersistenceManager(
                    storage_dir=os.path.join(self.work_dir, "layouts")
                ),
                simulation_lock=lock,
            )
            lock.count_passes_with(
                lambda: self.state.sim_controller.detector_reading_log.pass_count
            )
            app = create_app(self.state)
            app.config["TESTING"] = True
            self.client = stack.enter_context(app.test_client())
        except BaseException:
            stack.close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._stack is not None:
            log = getattr(getattr(self, "state", None), "sim_controller", None)
            if log is not None:
                log.detector_reading_log.close()
            self._stack.close()
            self._stack = None

    # -- reading the replayed server ---------------------------------------

    @property
    def network(self) -> Any:
        return self.state.network

    @property
    def sim(self) -> Any:
        return self.state.sim_controller

    @property
    def last_played(self) -> Optional[RecordedPass]:
        return self.run.passes[self._next_pass - 1] if self._next_pass > 0 else None

    # -- playing -----------------------------------------------------------

    def play_to(self, pass_no: Optional[int] = None) -> Optional[RecordedPass]:
        """Play every pass up to and including `pass_no` (the whole run when
        None), each preceded by the commands that ran before it. With None, the
        commands that ran after the last pass are sent too."""
        passes = self.run.passes
        last = passes[-1].pass_no
        target = last if pass_no is None else pass_no
        if target > last:
            raise ValueError(f"pass {target} is past the recording's last pass {last}")
        while self._next_pass < len(passes) and passes[self._next_pass].pass_no <= target:
            recorded = passes[self._next_pass]
            self._send_commands(before_pass=recorded.pass_no)
            with self.state.simulation_lock:
                play_pass(self.state.sim_controller, self.input, recorded)
            self._next_pass += 1
        if pass_no is None:
            self._send_commands(before_pass=None)
        return self.last_played

    def _send_commands(self, before_pass: Optional[int]) -> None:
        commands = self.run.commands
        while self._next_command < len(commands):
            command = commands[self._next_command]
            if before_pass is not None and command.after_pass >= before_pass:
                return
            self._next_command += 1
            if not self.send(command):
                self.skipped.append(command)
                continue
            response = self.client.open(
                command.path,
                method=command.method,
                query_string=command.query or None,
                json=command.body,
            )
            if response.status_code != command.status:
                detail = response.get_json(silent=True)
                self.divergences.append(
                    f"{command.describe()}: the rig answered {command.status}, "
                    f"the replay answered {response.status_code}: {detail}"
                )


def main(argv: Optional[List[str]] = None) -> int:
    """Replay a recorded run and print where it went.

        /opt/homebrew/anaconda3/envs/general/bin/python tools/reading_replay.py \\
            .runtime/detector_readings/readings-20261108-101500-000000.jsonl [--to-pass N]

    Prints how many passes and commands were replayed, each command whose
    answer differed from the recorded one, the commands it did not send, and
    which blocks each train holds at the end. Exits 1 when the replay diverged.
    """
    import argparse
    import sys
    import tempfile

    parser = argparse.ArgumentParser(description="Replay a recorded run.")
    parser.add_argument("files", nargs="+", help="the run's log files, oldest first")
    parser.add_argument("--to-pass", type=int, help="stop after this pass")
    args = parser.parse_args(argv)

    run = load_run(*args.files, to_pass=args.to_pass)
    with tempfile.TemporaryDirectory() as work_dir, RunReplay(run, work_dir) as replay:
        try:
            replay.play_to()
        except ReplayDiverged as exc:
            replay.divergences.append(str(exc))
        last = replay.last_played
        print(
            f"replayed {last.pass_no if last else 0} passes and "
            f"{len(run.commands) - len(replay.skipped)} commands "
            f"({len(replay.skipped)} device commands not sent)"
        )
        for text in replay.divergences:
            print(f"DIVERGED  {text}")
        for train_id, train in sorted(replay.sim.train_runtime.trains.items()):
            state = "lost" if train.lost else "train"
            print(f"{state:12} {train_id:16} {' '.join(train.blocks) or '(no blocks)'}")
        return 1 if replay.divergences else 0


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
