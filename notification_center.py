"""NotificationCenter - single runtime notification owner.

Per design_new.md "Runtime notifications" section, all runtime components
(TrainController, PathDispatcher, RouteExecutor, SimulationController) emit
notifications through this central object. There are no component-local
notification queues.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from constants import NOTIFICATION_RETENTION_ROWS


class Severity(Enum):
    """Severity levels for runtime notifications."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class EventKind:
    """Operational event `kind` vocabulary for the event log (design_new.md
    "Event log"): the events that mark what each train did, extending the
    notification `kind`s defined in "Runtime notifications". Components emit
    these through `NotificationCenter` at the points they already report from —
    the event log is the persisted projection of that one stream, not a second
    sink.
    """

    TRAIN_PLACED = "train_placed"
    TRAIN_DEPARTED = "train_departed"
    TRAIN_ARRIVED = "train_arrived"
    ENTERED_DWELL = "entered_dwell"
    BLOCK_ENTERED = "block_entered"
    BLOCK_VACATED = "block_vacated"
    SWITCH_THROWN = "switch_thrown"
    PATH_CREATED = "path_created"
    PATH_COMPLETED = "path_completed"
    PATH_CANCELLED = "path_cancellation"
    EB_ACTIVATED = "eb_activated"
    EB_RELEASED = "eb_released"


@dataclass
class Notification:
    """A single runtime notification record.

    Attributes:
        notification_id: Monotonic id assigned at emit time.
        event_time: Simulation event time (seconds) when the notification
            was emitted.
        source: Component that emitted, e.g. "TrainController",
            "PathDispatcher", "RouteExecutor", "SimulationController".
        kind: Free-form short kind, e.g. "command_result",
            "path_cancellation", "path_completion", "fatal_runtime",
            "train_added".
        severity: Severity level (INFO / WARNING / ERROR / FATAL).
        train_id: Optional related train id.
        path_id: Optional related path id.
        payload: Free-form per-kind payload.
    """

    notification_id: int
    event_time: float
    source: str
    kind: str
    severity: Severity
    train_id: Optional[str] = None
    path_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-compatible dict for the persisted event log."""
        return {
            "notification_id": self.notification_id,
            "event_time": self.event_time,
            "source": self.source,
            "kind": self.kind,
            "severity": self.severity.value,
            "train_id": self.train_id,
            "path_id": self.path_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Notification":
        """Reconstruct from a persisted event-log record."""
        raw_sev = data.get("severity", "info")
        try:
            severity = Severity(raw_sev)
        except ValueError:
            severity = Severity.INFO
        return cls(
            notification_id=int(data.get("notification_id", 0)),
            event_time=float(data.get("event_time", 0.0) or 0.0),
            source=str(data.get("source", "")),
            kind=str(data.get("kind", "")),
            severity=severity,
            train_id=data.get("train_id"),
            path_id=data.get("path_id"),
            payload=dict(data.get("payload") or {}),
        )


class NotificationCenter:
    """Sole owner of runtime notifications.

    Components emit via :meth:`emit`; consumers drain via :meth:`drain`.
    A bounded archive is kept for :meth:`peek_recent`.
    """

    def __init__(self) -> None:
        self._next_id: int = 1
        self._queue: List[Notification] = []
        self._archive: List[Notification] = []
        # Event-log disk persistence (design_new.md "Event log"): when a path is
        # set, every emitted notification is appended as one JSON line, so a
        # run's record outlives the session. The in-memory queue/archive are
        # unchanged; this is the persisted projection of the same one stream.
        self._log_path: Optional[str] = None

    def emit(
        self,
        source: str,
        kind: str,
        severity: Severity,
        event_time: Optional[float] = None,
        train_id: Optional[str] = None,
        path_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Notification:
        """Append a new Notification and return it.

        Args:
            source: Emitting component name.
            kind: Short kind discriminator.
            severity: Severity level.
            event_time: Wall-clock `time.time()` seconds. Omit it and this call
                samples it. Pass it only to share one local sample between this
                notification and a related entity write in the same step, which
                is the one case design_new.md "Time sampling" allows. Never pass
                `simulation_time`: the stream is wall-clock by design, so a
                playback-scaled value would not order against its siblings.
            train_id: Optional related train id.
            path_id: Optional related path id.
            payload: Optional per-kind payload (copied defensively).
        """
        n = Notification(
            notification_id=self._next_id,
            event_time=time.time() if event_time is None else event_time,
            source=source,
            kind=kind,
            severity=severity,
            train_id=train_id,
            path_id=path_id,
            payload=dict(payload) if payload else {},
        )
        self._next_id += 1
        self._queue.append(n)
        self._archive.append(n)
        self._trim_archive()
        self._persist(n)
        return n

    def _trim_archive(self) -> None:
        """Keep the most recent `NOTIFICATION_RETENTION_ROWS` events and drop
        the oldest beyond that (design_new.md "Runtime notifications",
        Retention).

        `_next_id` is untouched, so ids keep counting past a drop: a gap in the
        ids means events aged out, not that the stream lost order. The module
        global is re-read on every call so a live settings change takes effect
        on the next emit.
        """
        limit = NOTIFICATION_RETENTION_ROWS
        if limit > 0 and len(self._archive) > limit:
            del self._archive[: len(self._archive) - limit]

    def peek_recent(self, n: int = 50) -> List[Notification]:
        """Return up to the last `n` archived notifications."""
        if n <= 0:
            return []
        return list(self._archive[-n:])

    @property
    def log_path(self) -> Optional[str]:
        """The persisted event-log path, or `None` when persistence is off.

        The read-only viewer reads the persisted stream through this so that an
        earlier run's incidents survive a restart (design_new.md "Event log").
        """
        return self._log_path

    # -- event-log persistence (design_new.md "Event log") ----------------

    def enable_event_log(self, path: str) -> None:
        """Persist every subsequent emit to ``path`` as append-only JSONL.

        A fresh `NotificationCenter` numbers from 1, but the log is append-only
        across sessions. Resume past the highest id already on disk so
        "the increasing `notification_id` is the order the log reads back in"
        holds for the whole file, not just within one session.
        """
        directory = os.path.dirname(path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass
        self._log_path = path
        self._next_id = max(self._next_id, self._last_logged_id(path) + 1)

    @staticmethod
    def _last_logged_id(path: str) -> int:
        """Highest `notification_id` already persisted at `path`; 0 when none.

        A malformed line is skipped rather than failing the boot — a corrupt
        log must not stop the runtime from recording new events.
        """
        highest = 0
        if not path or not os.path.exists(path):
            return highest
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        highest = max(highest, int(json.loads(line)["notification_id"]))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            return highest
        return highest

    def _persist(self, n: Notification) -> None:
        """Append one notification to the disk event log; never raises (a log
        write must not affect the emitting component)."""
        if not self._log_path:
            return
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(n.to_dict()) + "\n")
        except OSError:
            pass

    @staticmethod
    def read_event_log(path: str) -> List[Notification]:
        """Read a persisted event log back in `notification_id` order (the
        order the log reads back). The read-only viewer/replay layers consume
        this; replay reconstructs the picture from these events plus the layout
        and must not re-run the simulation engine."""
        out: List[Notification] = []
        if not path or not os.path.exists(path):
            return out
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(Notification.from_dict(json.loads(line)))
        except (OSError, ValueError):
            pass
        return out

    def __len__(self) -> int:
        """Number of notifications currently in the live (undrained) queue."""
        return len(self._queue)
