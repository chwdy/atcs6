"""NotificationCenter - single runtime notification owner.

Per design_new.md "Runtime notifications" section, all runtime components
(TrainController, PathDispatcher, RouteExecutor, SimulationController) emit
notifications through this central object. There are no component-local
notification queues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(Enum):
    """Severity levels for runtime notifications."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


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


class NotificationCenter:
    """Sole owner of runtime notifications.

    Components emit via :meth:`emit`; consumers drain via :meth:`drain`.
    A bounded archive is kept for :meth:`peek_recent`.
    """

    def __init__(self) -> None:
        self._next_id: int = 1
        self._queue: List[Notification] = []
        self._archive: List[Notification] = []

    def emit(
        self,
        source: str,
        kind: str,
        severity: Severity,
        event_time: float,
        train_id: Optional[str] = None,
        path_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Notification:
        """Append a new Notification and return it.

        Args:
            source: Emitting component name.
            kind: Short kind discriminator.
            severity: Severity level.
            event_time: Simulation event time in seconds.
            train_id: Optional related train id.
            path_id: Optional related path id.
            payload: Optional per-kind payload (copied defensively).
        """
        n = Notification(
            notification_id=self._next_id,
            event_time=event_time,
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
        return n

    def peek_recent(self, n: int = 50) -> List[Notification]:
        """Return up to the last `n` archived notifications."""
        if n <= 0:
            return []
        return list(self._archive[-n:])

    def __len__(self) -> int:
        """Number of notifications currently in the live (undrained) queue."""
        return len(self._queue)
