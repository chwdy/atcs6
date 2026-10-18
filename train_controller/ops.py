"""
Operational State Machine sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition and "Controller
state machine": this sub-module owns the derived ``train_status`` read
(``OFF`` / ``MANUAL`` / ``ATO`` / ``EB``), the current handle position, and
the diagnostic ``handle_reason`` string.

``train_status`` is not stored and has no writer. It is computed on every
read from the three stored inputs ``powered`` (on the controller façade),
``eb.active`` (the EB latch), and ``commands.control_mode`` (the operator's
driving-source selection).

External callers access operational state as:

  - ``controller.ops.train_status`` (TrainStatus — derived)
  - ``controller.ops.handle_position`` (HandlePosition)
  - ``controller.ops.handle_reason`` (str — diagnostic)

The predicates (``is_stopped``, ``active_conditions``, ``power_on``) stay on
the façade because they orchestrate reads across multiple sub-modules and the
cleanest call shape stays ``controller.<X>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from enums import HandlePosition, TrainStatus

if TYPE_CHECKING:
    from train_controller import TrainController


class OperationalStateMachine:
    """Owns the derived train-status read, handle position, and handle reason."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Current handle position (writer: manual driving, EB workflow).
        self.handle_position: HandlePosition = HandlePosition.N
        # Diagnostic: reason for current handle position (set by ATO).
        self.handle_reason: str = ""

    # ------------------------------------------------------------------
    # Derived overall train status (design_new.md "Controller state machine")
    # ------------------------------------------------------------------

    @property
    def train_status(self) -> TrainStatus:
        """The train's overall status, derived each read.

        Per design_new.md "Controller state machine": computed in precedence
        order from the stored ``powered`` flag, the ``eb.active`` latch, whether
        a calibration run is open for the train, and ``commands.control_mode``.
        There is no stored ``train_status`` field and no writer.
        """
        c = self._c
        if not c.powered:
            return TrainStatus.OFF
        if c.eb.active:
            return TrainStatus.EB
        if c.calibration_run_open():
            return TrainStatus.CALIBRATING
        if c.commands.control_mode == "ato":
            return TrainStatus.ATO
        return TrainStatus.MANUAL

    # ------------------------------------------------------------------
    # Handle-position reset helper (design_new.md "One neutral helper")
    # ------------------------------------------------------------------

    def reset_handle_to_neutral(self) -> None:
        """Write the neutral notch — the non-driving neutral reset helper
        (design_new.md "One neutral helper"). Called by
        `TrainController._apply_power_off_request()`,
        `TrainController.commands.set_manual_mode_type()` when entering
        `power_percent`, and `TrainController.complete_power_off()`.
        """
        self.handle_position = HandlePosition.N
