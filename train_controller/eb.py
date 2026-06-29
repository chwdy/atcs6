"""
EB Workflow sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the EB
Workflow owns the ``active`` latch and the operator-EB activation /
release contract. Cross-sub-module state (``handle_position``,
``ato_running``, ``phase_latches``, ``state``, ``received_path_plan``)
is reached through the controller back-reference ``self._c`` so this
class stays a single-purpose owner of EB-specific state and behavior.

The ATC overspeed escalation path (in ``produce_physics_command``) writes
``self.eb.active = True`` directly alongside its other handle / ATO /
latch writes; the escalation semantics differ from operator EB (no
idempotency check, no ``RUNNING_AT_STATION`` guard) so it does not call
``activate()``. That inline escalation is intentional and the rule
"EB is the only writer of ``active``" still holds because both writers
land on the same field on the same class.

External callers access EB state and behavior as:

  - ``controller.eb.active``
  - ``controller.eb.activate()``
  - ``controller.eb.release()``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from enums import CommandResult, HandlePosition, TrainState

if TYPE_CHECKING:
    from train_controller import TrainController


class EBWorkflow:
    """Owns the emergency-brake latch and operator activate / release."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        self.active: bool = False

    def activate(self) -> CommandResult:
        """Operator-issued emergency brake.

        Per design_new.md: only acceptance gate is ``active == False``
        (idempotent rejection of a duplicate activation). Valid in any motion
        state, any operational state including OFF, with or without a cab,
        with or without an active path. On acceptance: set ``active = True``,
        force handle to EB, clear ``ato_running``, and (when an active path
        exists) write ``pending_path_cancel_request(EB)``. Exception while
        ``state == RUNNING_AT_STATION``: the controller is dwelling and has
        no active path, so no ``pending_path_cancel_request`` is written;
        state and dwell timer are preserved.
        """
        if self.active:
            return CommandResult(False, "Emergency brake already active")

        c = self._c

        self.active = True
        c.ops.handle_position = HandlePosition.EB
        c.ato.running = False

        if (
            c.ops.state != TrainState.RUNNING_AT_STATION
            and c.path_projection.received_path_plan is not None
        ):
            c.route_hooks.phase_latches.set_path_cancel_request("eb", "operator EB")

        from notification_center import EventKind, Severity

        c._emit_event(EventKind.EB_ACTIVATED, severity=Severity.WARNING)
        return CommandResult(True, "Emergency stop activated")

    def release(self) -> CommandResult:
        """Release emergency stop.

        Per design_new.md line 1385: accepts only when ``state != OFF``,
        ``active == True``, ``is_stopped() == True``, the controller's own
        ``pending_path_cancel_request`` is None (the EB cancel latch has
        been consumed by Phase 3), and the controller's own
        ``received_path_plan`` is None (the pre-EB authority has been
        cleared by ``PathDispatcher``). Otherwise return success=False with
        controller state unchanged. On acceptance from
        ``RUNNING_AT_STATION`` (dwell EB cycle), state stays
        ``RUNNING_AT_STATION`` and only ``active`` is cleared; dwell timing
        is unaffected. Otherwise lands in the ``READY`` state (powered,
        stopped, not dwelling — cab selection is preserved as an orthogonal
        field).
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if c.ops.state == TrainState.OFF:
            return CommandResult(False, "Power is off")
        if not self.active:
            return CommandResult(False, "Emergency brake not active")
        if not c.is_stopped():
            return CommandResult(False, "Cannot release emergency while moving")
        if c.route_hooks.phase_latches.path_cancel_request is not None:
            return CommandResult(
                False, "EB cancellation has not committed yet"
            )
        if c.path_projection.received_path_plan is not None:
            return CommandResult(
                False, "Pre-EB path authority has not cleared yet"
            )

        self.active = False
        c.ops.handle_position = HandlePosition.N

        # Land in READY (powered, stopped, not dwelling), except during dwell
        # (RUNNING_AT_STATION) where the dwell is preserved.
        if c.ops.state != TrainState.RUNNING_AT_STATION:
            c.ops.state = TrainState.READY
        from notification_center import EventKind, Severity

        c._emit_event(EventKind.EB_RELEASED, severity=Severity.WARNING)
        return CommandResult(True, "Emergency stop released")

    def clear(self) -> None:
        """Ungated EB-latch clear (design_new.md:1767, 1895).

        The EB Workflow is the single writer of ``active`` (module-boundary
        rule), so OFF-cleanup (``TrainController.complete_power_off()``) clears
        the latch through this method rather than assigning ``eb.active``
        directly. Unlike ``release()`` this has no preconditions — it is the
        unconditional teardown clear used when the controller powers off."""
        self.active = False
