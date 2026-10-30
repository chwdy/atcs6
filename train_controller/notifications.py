"""
Notification Emission sub-module of ``TrainController``.

Per design_new.md "Runtime notifications", `NotificationCenter` owns the one
runtime event stream and no component keeps an event queue, a toast buffer, or
a log of its own. This sub-module is the thin per-component adapter that rule
explicitly allows: it fills in `source="TrainController"` and the controller's
usual `entities` (its own train), and writes straight through to the one
stream. It holds no queue and no state.

External callers access this sub-module as:

  - ``controller.notifications.emit(message, ...)`` — emit one event about
    this train.

Operator-facing "what happened to this train" reads are filters over the one
stream (``GET /api/trains/<id>/notifications``), not a drain of a per-train
buffer: that read acknowledges nothing and empties nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from runtime_payloads import ErrorRecord
    from train_controller import TrainController


class Notifications:
    """Thin per-controller adapter onto the one `NotificationCenter` stream."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    def emit(
        self,
        message: str,
        *,
        operation: str = "tick",
        kind: Optional[str] = None,
        error: Optional["ErrorRecord"] = None,
        payload: Optional[Dict[str, Any]] = None,
        latch_event: bool = False,
    ) -> None:
        """Emit one event about this train.

        `latch_event` passes straight through to `NotificationCenter.emit(...)`,
        which exempts a latch write or a latch consumption from the
        unchanged-repeat suppression (design_new.md "Error events", Latch events
        are exempt). It is a control argument, not an event field.

        Best-effort: resolves the sink defensively and no-ops when none is
        wired (isolated unit-test controllers), and never raises into the
        caller — a reporting failure must not change what the controller did.
        """
        nc = self._c.notification_center
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref

            nc.emit(
                source="TrainController",
                operation=operation,
                kind=kind if kind is not None else Kind.WARNING,
                message=message,
                entities=[train_ref(self._c.train_id)],
                error=error,
                payload=payload,
                latch_event=latch_event,
            )
        except Exception:
            pass
