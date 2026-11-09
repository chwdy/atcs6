"""DetectorReadingLog - the per-pass record of what every block detector read.

design_new.md "Event log", Detector reading log. The event log records what the
runtime decided; this file records what the detectors told it, pass by pass, so
a rig run that went wrong can be read back as the exact readings the runtime
acted on and when it read them.

One JSON object per line. Every pass that reads the detectors writes one line:

    {"pass": 812, "pass_kind": "tick", "wall_time": 1761745392.48,
     "simulation_time": 81.2, "delta_seconds": 0.1, "operating_delta_seconds": 0.1,
     "readings": {"B_7": {"present": true, "source": "current", "zone": "driven",
                          "current_ma": 34.5, "threshold_ma": 10.0}}}

- `readings` lists only the blocks whose reading differs from the line before,
  and is left out when none did.
- A line that carries `full` lists every block the read returned instead. The
  first line of every file is one, and so is the first line after the layout or
  the operating environment changes, or after a write failed. `full` names which
  of those it was, and the line also carries the environment and the debounce
  thresholds the readings feed.

A block's reading is what `OccupancyInput.read_block_currents()` returned for it
(`present`), plus, for a driver that reports it (`OccupancyDriver.last_read_detail()`),
which source answered, the drive zone, the measured mA (to 0.1 mA) or the raw
'165 bit, the threshold the mA was judged against, and whether the lighting hold
kept the block present. It is the read as the driver returned it, before the
debounce; the line is written before attribution runs.

Writing never stops the session: every failure is caught here and reported as a
`warning` on the `NotificationCenter` stream under one condition key, which the
next successful write resolves.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, TextIO

from constants import (
    DETECTOR_READING_LOG_MAX_FILE_BYTES,
    DETECTOR_READING_LOG_MAX_TOTAL_BYTES,
)
from notification_center import Kind
from runtime_payloads import ErrorRecord

if TYPE_CHECKING:
    from block_occupancy import BlockOccupancyDetector
    from notification_center import NotificationCenter

logger = logging.getLogger(__name__)

#: Where the real server keeps the files: beside the shutdown record, in the
#: per-machine `.runtime/` folder (gitignored), anchored to the code rather than
#: to whatever directory the server was started from.
DETECTOR_READING_LOG_DIR = Path(__file__).resolve().parent / ".runtime" / "detector_readings"

#: Every file this log writes matches this pattern, and pruning touches nothing else.
_FILE_PREFIX = "readings-"
_FILE_SUFFIX = ".jsonl"

#: `full` values: why a line lists every block.
FULL_START = "start"  # the first line of a file opened at enable()
FULL_NEW_FILE = "new_file"  # the first line of a file opened because the last one was full
FULL_ENVIRONMENT = "environment"  # the detector or the operating environment changed
FULL_LAYOUT = "layout"  # a layout was loaded, or the set of read blocks changed
FULL_WRITE_FAILURE = "write_failure"  # lines before this one may be missing

#: The condition key a failed write is reported under; a successful write resolves it.
WRITE_FAILED_CONDITION_KEY = "detector_reading_log:write_failed"

#: The `last_read_detail()` fields copied into a reading, when present.
_DETAIL_FIELDS = ("source", "zone")


def block_reading(present: bool, detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One block's reading as written to the file.

    `present` is the boolean the detector debounce counts. The rest comes from a
    hardware driver's per-block read detail and is absent for the simulated
    driver, which reports nothing but the boolean.
    """
    reading: Dict[str, Any] = {"present": bool(present)}
    if not detail:
        return reading
    for key in _DETAIL_FIELDS:
        value = detail.get(key)
        if value is not None:
            reading[key] = value
    if "raw_bit" in detail:
        reading["raw_bit"] = bool(detail["raw_bit"])
    current_ma = detail.get("current_ma")
    if current_ma is not None:
        # Trip thresholds are around 10-15 mA, so a tenth of a mA is far finer
        # than anything that moves a verdict, and rounding there keeps smaller
        # noise from counting as a changed reading.
        reading["current_ma"] = round(float(current_ma), 1)
    threshold_ma = detail.get("threshold_ma")
    if threshold_ma is not None:
        reading["threshold_ma"] = round(float(threshold_ma), 1)
    if detail.get("held_from_previous"):
        reading["held_from_previous"] = True
    return reading


class DetectorReadingLog:
    """Writes one line per detector-reading pass to a JSONL file.

    Off until `enable(directory)` is called; the real server enables it at boot,
    and a unit test or scenario runner that never enables it pays one attribute
    check per pass.
    """

    def __init__(
        self,
        notification_center: Optional["NotificationCenter"] = None,
        max_file_bytes: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
    ) -> None:
        self._notification_center = notification_center
        self._max_file_bytes = (
            DETECTOR_READING_LOG_MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
        )
        self._max_total_bytes = (
            DETECTOR_READING_LOG_MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
        )
        self._dir: Optional[Path] = None
        self._path: Optional[Path] = None
        # The open file, line-buffered: every line reaches the OS as it is
        # written, so a crash loses nothing already read, without paying an
        # open and close on every pass. None until opened, and again after a
        # failed write, so the next pass reopens it.
        self._file: Optional[TextIO] = None
        self._bytes_in_file: int = 0
        # Counts every recorded pass in this process, written or not, so a gap
        # in `pass` on disk shows exactly where lines were lost.
        self._pass_count: int = 0
        # Counts every recorded operator command in this process, so a gap in
        # `command_no` shows a command line was lost.
        self._command_count: int = 0
        # What the file says each block last read. A change line is the
        # difference against this, so it moves only when a line was written.
        self._last_readings: Dict[str, Dict[str, Any]] = {}
        self._last_detector: Optional[object] = None
        self._last_environment: Optional[str] = None
        # Why the next line must list every block; None when it need not.
        self._full_reason: Optional[str] = FULL_START
        # Whether the last write failed, so the first success resolves the
        # reported condition.
        self._write_failing: bool = False
        # What the server held when it finished booting, written once on the
        # `"start"` line; None until `set_start_state(...)` is called.
        self._start_state: Optional[Dict[str, Any]] = None

    # -- configuration ------------------------------------------------------

    @property
    def path(self) -> Optional[str]:
        """The file being written now, or None while the log is off."""
        return str(self._path) if self._path is not None else None

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    def enable(self, directory: str | os.PathLike) -> None:
        """Start writing into a new file in `directory`.

        Each process writes its own files, named for the local time each was
        opened, so a run is found by when it happened. Opening a file also
        deletes the oldest files until the folder holds no more than
        `DETECTOR_READING_LOG_MAX_TOTAL_BYTES`.
        A directory that cannot be created is a failed write like any other:
        it is reported, the log stays on, and each later pass tries again, so
        the first line that does get written is the `"start"` line and resolves
        the warning (design_new.md "Detector reading log", A failed write never
        stops the session).
        """
        self._dir = Path(directory)
        self._full_reason = FULL_START
        try:
            self._open_new_file()
        except Exception as exc:  # noqa: BLE001 - logging must not stop the boot
            self._report_failure(exc)
            self._close_file()
            self._path = None

    @property
    def pass_count(self) -> int:
        """How many passes the log has recorded in this process: the `pass`
        value of the last pass line, or 0 before the first."""
        return self._pass_count

    def set_start_state(self, start_state: Dict[str, Any]) -> None:
        """Keep what the server held at the end of boot, to write on the
        `"start"` line (see `start_state_of`)."""
        self._start_state = start_state

    def request_full(self, reason: str) -> None:
        """Make the next line list every block, saying `reason`.

        A reason already waiting is kept: the first one says what made the
        earlier lines incomplete or stale.
        """
        if self._full_reason is None:
            self._full_reason = reason

    # -- the per-pass write -------------------------------------------------

    def record_pass(
        self,
        *,
        pass_kind: str,
        wall_time: Optional[float],
        simulation_time: float,
        delta_seconds: float,
        operating_delta_seconds: float,
        detector: "BlockOccupancyDetector",
        environment: Optional[str],
    ) -> None:
        """Write this pass's line. Never raises.

        Called right after `detector.step()` read the detectors, with that
        detector, so `detector.last_raw` is this pass's read. `environment` is
        the active environment's name, or None while none is active (Setup).
        """
        if self._dir is None:
            return
        self._pass_count += 1
        try:
            self._write_pass(
                pass_kind=pass_kind,
                wall_time=time.time() if wall_time is None else wall_time,
                simulation_time=simulation_time,
                delta_seconds=delta_seconds,
                operating_delta_seconds=operating_delta_seconds,
                detector=detector,
                environment=environment,
            )
        except Exception as exc:  # noqa: BLE001 - logging must not stop the tick
            # The lines from here until the next success are missing, so the
            # next written line restates every block, into a reopened file. A
            # log that has written nothing yet keeps `"start"` for that line.
            if self._full_reason != FULL_START:
                self._full_reason = FULL_WRITE_FAILURE
            self._close_file()
            self._report_failure(exc)
            return
        if self._write_failing:
            self._write_failing = False
            nc = self._notification_center
            if nc is not None:
                nc.resolve_condition(WRITE_FAILED_CONDITION_KEY)

    def _write_pass(
        self,
        *,
        pass_kind: str,
        wall_time: float,
        simulation_time: float,
        delta_seconds: float,
        operating_delta_seconds: float,
        detector: "BlockOccupancyDetector",
        environment: Optional[str],
    ) -> None:
        raw = detector.last_raw
        detail_reader = getattr(detector.input_interface, "last_read_detail", None)
        details: Dict[str, Dict[str, Any]] = (
            detail_reader() if callable(detail_reader) else {}
        ) or {}
        readings = {
            block_id: block_reading(present, details.get(block_id))
            for block_id, present in raw.items()
        }

        reason = self._full_reason
        if reason is None:
            if (
                detector is not self._last_detector
                or environment != self._last_environment
            ):
                reason = FULL_ENVIRONMENT
            elif readings.keys() != self._last_readings.keys():
                reason = FULL_LAYOUT
        if self._path is None or self._bytes_in_file >= self._max_file_bytes:
            self._open_new_file()
            reason = reason or FULL_NEW_FILE

        line: Dict[str, Any] = {
            "pass": self._pass_count,
            "pass_kind": pass_kind,
            "wall_time": wall_time,
            "simulation_time": simulation_time,
            "delta_seconds": delta_seconds,
            "operating_delta_seconds": operating_delta_seconds,
        }
        if reason is not None:
            line["full"] = reason
            line["environment"] = environment
            line["on_threshold"] = detector.on_threshold
            line["off_threshold"] = detector.off_threshold
            line["readings"] = readings
            if reason == FULL_START and self._start_state is not None:
                line["start_state"] = self._start_state
        else:
            changed = {
                block_id: reading
                for block_id, reading in readings.items()
                if self._last_readings.get(block_id) != reading
            }
            if changed:
                line["readings"] = changed

        text = json.dumps(line, separators=(",", ":")) + "\n"
        if self._file is None:
            self._file = open(self._path, "a", encoding="utf-8", buffering=1)
        self._file.write(text)
        self._bytes_in_file += len(text)

        self._last_readings = readings
        self._last_detector = detector
        self._last_environment = environment
        self._full_reason = None

    # -- operator commands ----------------------------------------------------

    def record_command(
        self,
        *,
        method: str,
        path: str,
        query: str,
        body: Any,
        status: int,
        after_pass: int,
    ) -> None:
        """Write one line for a state-changing request the server answered.
        Never raises.

        `after_pass` is the `pass` of the last pass that ran before the request
        changed anything, so a replay sends it between that pass and the next.
        The line is written when the request is answered, which can be after a
        later pass line, so `after_pass`, not the line's place in the file, says
        when it ran. The caller holds `simulation_lock`, as `record_pass(...)`'s
        caller does, so the two never write at once.
        """
        if self._dir is None:
            return
        self._command_count += 1
        line: Dict[str, Any] = {
            "command_no": self._command_count,
            "after_pass": int(after_pass),
            "wall_time": time.time(),
            "command": {
                "method": method,
                "path": path,
                "query": query,
                "body": body,
                "status": int(status),
            },
        }
        try:
            text = json.dumps(line, separators=(",", ":"), default=str) + "\n"
            if self._path is None:
                self._open_new_file()
                if self._full_reason is None:
                    self._full_reason = FULL_NEW_FILE
            if self._file is None:
                self._file = open(self._path, "a", encoding="utf-8", buffering=1)
            self._file.write(text)
            self._bytes_in_file += len(text)
        except Exception as exc:  # noqa: BLE001 - logging must not fail a request
            self._close_file()
            self._report_failure(exc, "record_command")
            return
        if self._write_failing:
            self._write_failing = False
            nc = self._notification_center
            if nc is not None:
                nc.resolve_condition(WRITE_FAILED_CONDITION_KEY)

    # -- files --------------------------------------------------------------

    def _open_new_file(self) -> None:
        """Point the log at a fresh file and prune the oldest ones."""
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self._dir / f"{_FILE_PREFIX}{stamp}{_FILE_SUFFIX}"
        suffix = 1
        while path.exists():
            path = self._dir / f"{_FILE_PREFIX}{stamp}-{suffix}{_FILE_SUFFIX}"
            suffix += 1
        self._close_file()
        self._file = open(path, "a", encoding="utf-8", buffering=1)
        self._path = path
        self._bytes_in_file = 0
        self._prune(keep=path)

    def _close_file(self) -> None:
        """Close the open file, if any. Never raises."""
        f, self._file = self._file, None
        if f is None:
            return
        try:
            f.close()
        except Exception:  # noqa: BLE001 - a failed close loses nothing more
            pass

    def close(self) -> None:
        """Close the file. The next recorded pass reopens it."""
        self._close_file()

    def _prune(self, keep: Path) -> None:
        """Delete the oldest files until the folder fits `max_total_bytes`,
        never the file just opened.

        Measured in bytes rather than files, so the bound holds both for many
        short runs (every server restart opens a file) and for a few long ones.
        Best effort: a file that cannot be listed or deleted is left, and the
        new file is written either way.
        """
        if self._max_total_bytes <= 0 or self._dir is None:
            return
        try:
            files = [
                (p.stat(), p)
                for p in self._dir.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}")
                if p.is_file()
            ]
        except OSError:
            return
        files.sort(key=lambda entry: (entry[0].st_mtime, entry[1].name))
        total = sum(st.st_size for st, _p in files)
        for st, old in files:
            if total <= self._max_total_bytes:
                break
            if old == keep:
                continue
            try:
                old.unlink()
            except OSError:
                continue
            total -= st.st_size

    # -- failure reporting ----------------------------------------------------

    def _report_failure(self, exc: BaseException, operation: str = "record_pass") -> None:
        """Report a failed write on the event stream. Never raises.

        Every failure is emitted under the same condition key and the same
        payload, so `NotificationCenter` keeps one event per stretch of failing
        writes; the next successful write resolves the key.
        """
        self._write_failing = True
        nc = self._notification_center
        if nc is None:
            return
        try:
            nc.emit(
                source="DetectorReadingLog",
                operation=operation,
                kind=Kind.WARNING,
                message=(
                    "detector reading log write failed; the session keeps running "
                    "and the next line written lists every block"
                ),
                error=ErrorRecord(
                    text=f"{type(exc).__name__}: {exc}",
                    origin="detector_reading_log_write",
                ),
                payload={"directory": str(self._dir) if self._dir else None},
                condition_key=WRITE_FAILED_CONDITION_KEY,
            )
        except Exception:  # noqa: BLE001 - last resort: a failed emit must not be silent
            logger.exception("detector reading log: reporting a failed write raised")


def start_state_of(network: Any) -> Dict[str, Any]:
    """What the server holds at the end of boot, for the `"start"` line.

    - `layout` — the layout as a layout save holds it (`to_layout_dict()`);
    - `hardware` — the hardware setup as a layout save holds it;
    - `locomotives` — the models, sets and counters, as the locomotive file holds them;
    - `selected_environment` — the environment selected at boot;
    - `trains_on_track` — the trains that hold a block at boot. A layout save
      records no train position, so a replay cannot put these back.
    """
    import hardware_config
    import locomotive_file
    import operation_environment

    return {
        "layout": network.to_layout_dict(),
        "hardware": hardware_config.persisted_dict(),
        "locomotives": locomotive_file.contents(network),
        "selected_environment": operation_environment.get_selected().value,
        "trains_on_track": sorted(
            {
                block.occupied_by
                for block in network.blocks.values()
                if block.occupied_by is not None
            }
        ),
    }


class SimulationLock:
    """The lock that serializes operator requests with runtime passes, and
    remembers, per thread, how many passes the reading log had recorded when
    that thread last released it.

    A request changes state while it holds this lock, and the runtime loop
    runs each pass while it holds it. So the count a request thread saw at its
    last release is the `pass` of the last pass before the request's change:
    the `after_pass` its command line records, exact even when a pass runs
    between the change and the moment the request is answered.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._local = threading.local()
        self._pass_count: Optional[Any] = None

    def count_passes_with(self, pass_count: Any) -> None:
        """Read the pass count from `pass_count()` at each release."""
        self._pass_count = pass_count

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        if self._pass_count is not None:
            # Read while still held, so no pass can slip in between.
            self._local.released_at = self._pass_count()
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> "SimulationLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

    def forget_release(self) -> None:
        """Forget this thread's last release, at the start of a request."""
        self._local.released_at = None

    def last_release(self) -> Optional[int]:
        """The pass count at this thread's last release since
        `forget_release()`, or None when it has not released the lock since."""
        return getattr(self._local, "released_at", None)


def read_detector_reading_log(path: str | os.PathLike) -> list[Dict[str, Any]]:
    """Read one file back as a list of line objects, skipping malformed lines."""
    out: list[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out
