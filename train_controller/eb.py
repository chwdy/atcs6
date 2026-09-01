"""
EB Workflow sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the EB
Workflow owns the ``active`` latch and the operator-EB activation /
release contract. Cross-sub-module state (``handle_position``,
``ato_running``, ``phase_latches``, ``state``, ``received_path_plan``)
is reached through the controller back-reference ``self._c`` so this
class stays a single-purpose owner of EB-specific state and behavior.

The ATC overspeed escalation path calls ``activate()`` from inside
``TrainController.tick(...)`` rather than writing ``self.eb.active``
directly, so it runs the same synchronous activation effects — including
``consume_service_stop(...)`` — as an operator-issued EB. ``activate()``,
``release()``, and ``clear()`` are the only three sites that assign
``eb.active``; the ATC escalation call is the one caller of ``activate()``
outside the operator-command drain.

External callers access EB state and behavior as:

  - ``controller.eb.active``
  - ``controller.eb.activate()``
  - ``controller.eb.release()``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from enums import CommandResult, HandlePosition, TrainState
from runtime_payloads import ErrorRecord

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
        c.ato.stop()
        # Force-clear through the service-stop teardown path so a run
        # interrupted by EB never leaves braking armed on a later route
        # (design_new.md "TrainController.begin_service_stop() /
        # consume_service_stop()").
        c.consume_service_stop(force=True)

        if (
            c.ops.state != TrainState.RUNNING_AT_STATION
            and c.path_projection.received_path_plan is not None
        ):
            # A safety action that recovers, so `"warning"` — never `"error"`,
            # which design_new.md reserves for the two fatal session entries.
            c.route_hooks.phase_latches.set_path_cancel_request(
                ErrorRecord("operator EB", "eb"), "warning"
            )

        from notification_center import Kind

        c._emit_event(
            "activate", f"emergency stop activated for {c.train_id}",
            kind=Kind.WARNING,
        )
        return CommandResult(True, "Emergency stop activated")

    def release(self) -> CommandResult:
        """Release emergency stop.

        Per design_new.md line 1385: accepts only when ``state != OFF``,
        ``active == True``, ``is_stopped() == True``, the controller's own
        ``pending_path_cancel_request`` is None (the EB cancel latch has
        been consumed by Action), and the controller's own
        ``received_path_plan`` is None (the pre-EB authority has been
        cleared by ``PathDispatcher``). Otherwise return success=False with
        controller state unchanged.

        This method clears only ``active`` (and resets the handle to
        neutral); it does NOT write the operational ``state``. Per
        design_new.md (**Reactive condition evaluation**, the ``READY`` state
        row, and the EB-interruption / EB-release notes at lines 1959 and
        2215), the ``RUNNING_MOVING -> READY`` move is performed by
        ``OperationalStateMachine.evaluate_reactive_conditions()`` once this
        method has cleared ``active`` — the evaluator's transition 2 fires
        because the train is stopped, has no live authority, and ``eb.active``
        is now ``False``. Both of the evaluator's call sites pick a release up:
        the tick's step-``71ee`` call (when a release is drained as an operator
        command) and the ``release-emergency`` API route, which runs the
        evaluator synchronously right after this method so the POST stays
        synchronous. A release from ``RUNNING_AT_STATION`` (dwell EB cycle)
        leaves the dwell state untouched, because ``RUNNING_AT_STATION`` is not
        one of the evaluator's source states — so only ``active`` clears there.
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
        # Driver-control interlock (design_new.md "EB workflow state machine",
        # row `ad04`): a manual `power_percent` driver holding a retained
        # percentage must zero it before the release is accepted. EB caps the
        # tick to zero but leaves the retained field alone, so without this gate
        # clearing `eb.active` would hand the train straight back to a
        # percentage the driver never withdrew. `set_manual_power_percent(...)`
        # runs before `eb.release()` in the drain order, so a driver can satisfy
        # this and release in one batch.
        if (
            c.commands.control_mode == "manual"
            and c.manual.manual_mode_type == "power_percent"
            and c.manual.manual_power_percent != 0
        ):
            return CommandResult(
                False,
                "Set power to 0 before releasing the emergency brake",
            )

        self.active = False
        c.ops.handle_position = HandlePosition.N

        # The READY move is intentionally not written here: the reactive
        # evaluator owns the RUNNING_MOVING -> READY transition (see the
        # docstring). The tick's step-`71ee` call and the release-emergency API
        # route each run it right after this method.
        from notification_center import Kind

        c._emit_event(
            "release", f"emergency stop released for {c.train_id}",
            kind=Kind.WARNING,
        )
        return CommandResult(True, "Emergency stop released")

    def clear(self) -> None:
        """Ungated EB-latch clear (design_new.md:1767, 1895).

        The EB Workflow is the single writer of ``active`` (module-boundary
        rule), so OFF-cleanup (``TrainController.complete_power_off()``) clears
        the latch through this method rather than assigning ``eb.active``
        directly. Unlike ``release()`` this has no preconditions — it is the
        unconditional teardown clear used when the controller powers off."""
        self.active = False
