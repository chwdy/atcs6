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
from typing import Any, Callable, Dict, List, Optional

from constants import NOTIFICATION_RETENTION_ROWS
from runtime_payloads import ErrorRecord


class Kind:
    """The event level — the only level field on an event, and the only field
    runtime code may branch on (design_new.md "Runtime notifications", Event
    schema).

    `debug` is per-tick detail a developer reads; `info` is a normal thing that
    happened; `warning` is something that did not work but may work on a retry;
    `error` is something that failed. There is no `fatal`: a fatal runtime or
    hardware failure is an `error` event whose `source` + `operation` say what
    froze the session.
    """

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


#: Every level, for validation.
KINDS = frozenset({Kind.DEBUG, Kind.INFO, Kind.WARNING, Kind.ERROR})

#: The levels at which an event counts as reporting a failure — what
#: `latest_error_for` answers with.
ERROR_KINDS = frozenset({Kind.WARNING, Kind.ERROR})

#: The v1 `entity_type` values (design_new.md "Runtime notifications").
ENTITY_TYPES = frozenset(
    {"train", "route_run", "walk_position", "path", "block", "switch"}
)


@dataclass(frozen=True)
class EntityRef:
    """One `{entity_type, entity_id}` ref inside an event's `entities` list.

    `entities` is the only entity routing key: a per-train view filters for a
    ref with `entity_type == "train"` rather than reading a separate scalar.
    """

    entity_type: str
    entity_id: str

    def to_dict(self) -> Dict[str, str]:
        return {"entity_type": self.entity_type, "entity_id": self.entity_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EntityRef":
        return cls(
            entity_type=str(data.get("entity_type", "")),
            entity_id=str(data.get("entity_id", "")),
        )


def train_ref(train_id: str) -> EntityRef:
    return EntityRef("train", str(train_id))


def route_run_ref(run_id: str) -> EntityRef:
    return EntityRef("route_run", str(run_id))


def walk_position_ref(run_id: str, index: int) -> EntityRef:
    """A task ref. Its id is the `RouteRun` id and the integer walk index
    joined by `"#"`, so a run-level filter and a task-level filter both find an
    event about a task."""
    return EntityRef("walk_position", f"{run_id}#{int(index)}")


def path_ref(path_id: str) -> EntityRef:
    return EntityRef("path", str(path_id))


def block_ref(block_id: str) -> EntityRef:
    return EntityRef("block", str(block_id))


def switch_ref(switch_id: str) -> EntityRef:
    return EntityRef("switch", str(switch_id))


@dataclass
class Notification:
    """One runtime event (design_new.md "Runtime notifications", Event schema).

    Attributes:
        notification_id: Assigned by `NotificationCenter` in emit order. It is
            the order the stream reads back in.
        event_time: System-time Unix seconds, sampled by the emitting function
            at the moment it emits. Never `simulation_time`: the stream is
            wall-clock by design, so a playback-scaled value would not order
            against its siblings.
        source: The class that emitted the event ("PathDispatcher",
            "RouteExecutor", "TrainController", "BlockManager",
            "SwitchManager", "SimulationController").
        operation: The name of the function that emitted the event
            ("cancel_path", "set_handle"). Every emitting function writes its
            own name here and never another function's. `source` + `operation`
            is the "where did this come from" key a reader filters on.
        kind: How serious the event is — see `Kind`. The only level field, and
            the only field runtime code may branch on.
        entities: Ordered `EntityRef`s naming everything the event is about.
            One event may name several, and every per-entity view is a
            membership filter over this list.
        message: One human-readable line saying what the operation did. Always
            present, at every level.
        error: The failure detail, present when the event reports something
            that did not work. `message` says what was attempted; `error.text`
            says what went wrong. Independent of `kind`: nothing derives one
            from the other, so an operator-requested cancellation is
            `kind = "info"` and still carries the record naming what ended it.
        payload: Structured per-operation data.
    """

    notification_id: int
    event_time: float
    source: str
    operation: str
    kind: str
    entities: List[EntityRef] = field(default_factory=list)
    message: str = ""
    error: Optional[ErrorRecord] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-compatible dict for the persisted event log."""
        return {
            "notification_id": self.notification_id,
            "event_time": self.event_time,
            "source": self.source,
            "operation": self.operation,
            "kind": self.kind,
            "entities": [e.to_dict() for e in self.entities],
            "message": self.message,
            "error": self.error.to_dict() if self.error is not None else None,
            "payload": self.payload,
        }

    def has_entity(self, entity_type: str, entity_id: str) -> bool:
        """True when this event names that entity — the membership test every
        per-entity view filters on."""
        return any(
            e.entity_type == entity_type and e.entity_id == entity_id
            for e in self.entities
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Notification":
        """Reconstruct from a persisted event-log record."""
        raw_error = data.get("error")
        return cls(
            notification_id=int(data.get("notification_id", 0)),
            event_time=float(data.get("event_time", 0.0) or 0.0),
            source=str(data.get("source", "")),
            operation=str(data.get("operation", "")),
            kind=str(data.get("kind", Kind.INFO)),
            entities=[
                EntityRef.from_dict(e) for e in (data.get("entities") or [])
            ],
            message=str(data.get("message", "")),
            error=ErrorRecord.from_dict(raw_error) if raw_error else None,
            payload=dict(data.get("payload") or {}),
        )


class NotificationCenter:
    """Sole owner of runtime notifications.

    Components emit via :meth:`emit`; consumers drain via :meth:`drain`.
    A bounded archive is kept for :meth:`peek_recent`.
    """

    #: How many appends between persisted-log trims. See `_trim_log`.
    _TRIM_EVERY_ROWS = 500

    def __init__(self) -> None:
        self._next_id: int = 1
        self._queue: List[Notification] = []
        self._archive: List[Notification] = []
        # Active error conditions, keyed by the caller-supplied `condition_key`
        # (design_new.md "Error events", Suppression mechanism). Each value is
        # the event that is currently the live occurrence of that condition, so
        # `emit(...)` can compare a fresh call's fingerprint against it and
        # `latest_error_for(...)` can tell which retained events are still the
        # current reason. The condition's owner clears its key through
        # `resolve_condition(...)`. Runtime-only: it starts empty after a
        # restart, so the first still-present condition in a new process emits
        # once for that process.
        self._active_conditions: Dict[str, Notification] = {}
        # Event-log disk persistence (design_new.md "Event log"): when a path is
        # set, every emitted notification is appended as one JSON line, so a
        # run's record outlives the session. The in-memory queue/archive are
        # unchanged; this is the persisted projection of the same one stream.
        self._log_path: Optional[str] = None
        # Persisted-log trimming is batched; see `_trim_log`.
        self._rows_since_trim: int = 0

    def emit(
        self,
        source: str,
        operation: str,
        kind: str,
        message: str,
        entities: Optional[List[EntityRef]] = None,
        error: Optional[ErrorRecord] = None,
        payload: Optional[Dict[str, Any]] = None,
        event_time: Optional[float] = None,
        latch_event: bool = False,
        condition_key: Optional[str] = None,
    ) -> Optional[Notification]:
        """Append one event and return it, or `None` when it was dropped as an
        unchanged repeat of an already-active error condition.

        This is the one way an event enters the stream (design_new.md "Runtime
        notifications", The emit call).

        Args:
            source: The class emitting.
            operation: The emitting function's own name, never another's.
            kind: The level — see `Kind`.
            message: One human-readable line saying what the operation did.
            entities: Everything the event is about.
            error: The failure detail, when there is one.
            payload: Structured per-operation data (copied defensively).
            event_time: Wall-clock `time.time()` seconds. Omit it and this call
                samples it. Pass it only to share one local sample between this
                event and a related entity write in the same step, which is the
                one case design_new.md "Time sampling" allows. Never pass
                `simulation_time`: the stream is wall-clock by design, so a
                playback-scaled value would not order against its siblings.
            latch_event: True from a latch writer helper or a latch's
                consume-side emitter. It exempts the event from the
                active-condition suppression below and is a control argument
                only — it is not part of the persisted Event schema and is
                never stored. The point of the write/consume pair is to show
                intent and commit separately (design_new.md "Error events",
                Latch events are exempt), so dropping either one as a repeat
                would hide the very thing the pair exists to show.
            condition_key: Identifies one live suppressible error condition
                (design_new.md "Error events", Suppression mechanism). A
                control argument only — not part of the persisted Event schema
                and never stored. When it is set on an event that carries an
                `ErrorRecord` and is not a latch event, this event is that
                condition's current occurrence: the call is dropped while the
                key is already active with the same `entities`, `operation`,
                `ErrorRecord.origin`, and `payload`, and a changed fingerprint
                is appended and replaces the active value. The condition's owner
                clears the key with `resolve_condition(...)` when its predicate
                goes false, after which the otherwise-identical event emits
                again. Events with `error is None`, latch events, and events
                with no condition key are always appended.
        """
        refs = list(entities or [])
        data = dict(payload) if payload else {}

        # Suppression applies only to an error-carrying, non-latch event that
        # names a live condition (design_new.md "Error events", Suppression
        # mechanism). Everything else — command results, error-free events,
        # latch writes/consumptions, and un-keyed errors — is always appended.
        suppressible = (
            error is not None and not latch_event and condition_key is not None
        )
        if suppressible:
            active = self._active_conditions.get(condition_key)
            if active is not None and self._same_condition_fingerprint(
                active, refs, operation, error, data
            ):
                return None

        n = Notification(
            notification_id=self._next_id,
            event_time=time.time() if event_time is None else event_time,
            source=source,
            operation=operation,
            kind=kind,
            entities=refs,
            message=message,
            error=error,
            payload=data,
        )
        self._next_id += 1
        self._queue.append(n)
        self._archive.append(n)
        if suppressible:
            # Either the key was not active, or its fingerprint changed; this
            # event is now the live occurrence of that condition.
            self._active_conditions[condition_key] = n
        self._trim_archive()
        self._persist(n)
        return n

    @staticmethod
    def _same_condition_fingerprint(
        active: Notification,
        entities: List[EntityRef],
        operation: str,
        error: Optional[ErrorRecord],
        payload: Dict[str, Any],
    ) -> bool:
        """True when a fresh emit matches the active condition's fingerprint —
        the same `entities`, `operation`, `ErrorRecord.origin`, and `payload`
        (design_new.md "Error events", Suppression mechanism).

        A match means the condition has not changed since it was recorded, so
        the fresh call is a repeat and is dropped. A changed origin or payload —
        such as a meet ceiling moving to a different block — is a different
        fingerprint, so the fresh call is appended and replaces the active
        value.
        """
        active_origin = active.error.origin if active.error is not None else None
        incoming_origin = error.origin if error is not None else None
        return (
            active.operation == operation
            and active.entities == entities
            and active_origin == incoming_origin
            and active.payload == payload
        )

    def resolve_condition(self, condition_key: str) -> None:
        """Clear one active error condition (design_new.md "Error events",
        Resolution and recurrence).

        The component that owns the live predicate calls this on the first tick
        the predicate goes false. It removes the active key and is idempotent —
        resolving a key that is not active is a no-op. It does not delete or
        rewrite the historical event that recorded the condition; that event
        stays in the stream for historical viewers. Once the key is cleared, an
        otherwise-identical event for that condition is appended again if the
        condition later recurs, and `latest_error_for(...)` stops returning the
        now-resolved occurrence.
        """
        self._active_conditions.pop(condition_key, None)

    def latest_error_for(
        self,
        entity_type: str,
        entity_id: str,
        operation: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> Optional[Notification]:
        """The most recent retained `warning`/`error` event for that entity
        whose condition is still active, narrowed by `operation` and `origin`
        when given, or `None` (design_new.md "Error events", Reading the
        current reason).

        This combines the retained event row with the runtime active-condition
        map: an event is a candidate only while it is still the live occurrence
        of an unresolved `condition_key`. It never returns a condition that its
        owner has resolved, and it never returns an error that was emitted with
        no condition key — those live on only as history, which the event-log
        viewers read directly. After a restart the active map begins empty and
        is repopulated as live predicates are re-evaluated, so a still-present
        condition emits a fresh event and becomes current again.

        The `error is not None` and `kind in ERROR_KINDS` tests keep ordinary
        `info` traffic and level-but-diagnostic-free events out of the answer.
        This derived read is how a surface answers "why is this train not
        moving" or "what happened at this task".
        """
        if not self._active_conditions:
            return None
        # The events that are currently the live occurrence of some condition.
        # Identity, not equality: each suppressible emit registers exactly one
        # distinct Notification under its key.
        active_events = {id(n) for n in self._active_conditions.values()}
        for n in reversed(self._archive):
            if id(n) not in active_events:
                continue
            if n.error is None or n.kind not in ERROR_KINDS:
                continue
            if not n.has_entity(entity_type, entity_id):
                continue
            if operation is not None and n.operation != operation:
                continue
            if origin is not None and n.error.origin != origin:
                continue
            return n
        return None

    def events_for(
        self,
        entity_type: str,
        entity_id: str,
        kind: Optional[str] = None,
        since_notification_id: Optional[int] = None,
    ) -> List[Notification]:
        """Events naming that entity, most recent first — the filter behind
        `GET /api/trains/<train_id>/notifications`. A read: it acknowledges
        nothing and empties nothing."""
        out: List[Notification] = []
        for n in reversed(self._archive):
            if not n.has_entity(entity_type, entity_id):
                continue
            if kind is not None and n.kind != kind:
                continue
            if since_notification_id is not None and n.notification_id <= since_notification_id:
                continue
            out.append(n)
        return out

    def latest_by_operation(self, operation: str) -> Optional[Notification]:
        """The most recent retained event whose `operation` matches, or `None`.

        A read over the one stream (design_new.md "Runtime notifications"). A
        status surface that wants the current detail of a latched condition — the
        hardware-failure halt banner reads the last `enter_hardware_failure`
        event this way — filters the stream for the writer's own event instead of
        reading a retained diagnostic field, so the controller keeps no second
        copy of that detail. It acknowledges nothing and empties nothing.
        """
        for n in reversed(self._archive):
            if n.operation == operation:
                return n
        return None

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
            return
        self._rows_since_trim += 1
        if self._rows_since_trim >= self._TRIM_EVERY_ROWS:
            self._rows_since_trim = 0
            self._trim_log()

    def _trim_log(self) -> None:
        """Hold the persisted log to the same `NOTIFICATION_RETENTION_ROWS`
        window the in-memory stream holds (design_new.md "Event log",
        Persistence): one cap governs the whole stream, so a row an operator
        can no longer see in the viewer is a row no query can reach.

        Rewritten in batches rather than per append, because the cap is a
        window and not a per-row invariant — trimming on every event would
        rewrite the whole file thousands of times a session for no visible
        difference. Never raises."""
        limit = NOTIFICATION_RETENTION_ROWS
        if not self._log_path or limit <= 0:
            return
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= limit:
                return
            with open(self._log_path, "w", encoding="utf-8") as f:
                f.writelines(lines[-limit:])
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


def make_notification_emitter(
    notification_center: "NotificationCenter", source: str
) -> Callable[..., Optional[Notification]]:
    """Build the narrow notification callback a resource manager is handed.

    `BlockManager` and `SwitchManager` hold only this callback, never
    `NotificationCenter` or `SimulationController` (design_new.md
    "BlockManager" / "SwitchManager"). The returned function takes
    `(operation, kind, message, entities, error=None, payload=None)`, fixes the
    emitting component's `source` name, and forwards the rest to
    `NotificationCenter.emit(...)`, which samples `event_time` itself. The
    runtime wiring passes one of these (with `source="BlockManager"` /
    `"SwitchManager"`) in place of the old back-reference.
    """

    def emit(operation, kind, message, entities, error=None, payload=None):
        return notification_center.emit(
            source=source,
            operation=operation,
            kind=kind,
            message=message,
            entities=entities,
            error=error,
            payload=payload,
        )

    return emit
