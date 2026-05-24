"""
Operational State Machine sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: this
sub-module owns the canonical operational state (``state``: ``OFF`` /
``READY`` / ``RUNNING_MOVING`` / ``RUNNING_AT_STATION``), the current
handle position, and the diagnostic ``handle_reason`` string.

External callers access operational state as:

  - ``controller.ops.state`` (TrainState)
  - ``controller.ops.handle_position`` (HandlePosition)
  - ``controller.ops.handle_reason`` (str — diagnostic)

The state-transition methods, predicates (``is_stopped``,
``is_recovery_clean``, ``active_conditions``, ``power_on``), and
related helpers stay on the façade because they orchestrate writes
across multiple sub-modules and the cleanest call shape stays
``controller.<X>``. Their bodies read/write ``self.ops.state`` etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from enums import HandlePosition, TrainState

if TYPE_CHECKING:
    from train_controller import TrainController


class OperationalStateMachine:
    """Owns the canonical operational state, handle position, and handle reason."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Canonical operational state per design_new.md "Controller state machine".
        self.state: TrainState = TrainState.OFF
        # Current handle position (writer: manual driving, EB workflow).
        self.handle_position: HandlePosition = HandlePosition.N
        # Diagnostic: reason for current handle position (set by ATO).
        self.handle_reason: str = ""
