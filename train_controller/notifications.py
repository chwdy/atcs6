"""
Notification Emission sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the
Notification Emission sub-module owns the per-controller toast queue
that the frontend drains via ``GET /api/trains/<id>``.

This is the output-layer counterpart to the input-layer command
surface. Compute-layer sub-modules (ATC overspeed escalation, command
acceptance/rejection, etc.) push messages here through
``controller.notifications.emit(msg)``; the frontend reads them via
``controller.notifications.get_and_clear()``.

External callers access this sub-module as:

  - ``controller.notifications.pending`` — list[str], read-only by
    convention; consumers should drain via ``get_and_clear()``.
  - ``controller.notifications.emit(msg)`` — append one message.
  - ``controller.notifications.get_and_clear()`` — return the current
    queue and reset it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from train_controller import TrainController


class Notifications:
    """Owns the per-controller toast queue."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        self.pending: List[str] = []

    def emit(self, message: str) -> None:
        """Queue a toast message for the next frontend poll."""
        self.pending.append(message)

    def get_and_clear(self) -> List[str]:
        """Return the current queue and reset it.

        Called by the frontend to drain pending toast messages.
        """
        out = self.pending.copy()
        self.pending.clear()
        return out
