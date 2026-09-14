"""
EB Workflow sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the EB
Workflow owns the ``active`` latch and the operator-EB activation /
release contract. Cross-sub-module state (``handle_position``,
``ato.engaged``, ``phase_latches``, ``state``, ``received_path_plan``)
is reached through the controller back-reference ``self._c`` so this
class stays a single-purpose owner of EB-specific state and behavior.

The ATC overspeed escalation path calls ``activate()`` from inside
``TrainController.tick(...)`` rather than writing ``self.eb.active``
directly, so it runs the same synchronous activation effects as an
operator-issued EB. ``activate()``, ``release()``, and ``clear()`` are the
only three sites that assign ``eb.active``; the ATC escalation call is the
one caller of ``activate()`` outside the operator-command drain.

External callers access EB state and behavior as:

  - ``controller.eb.active``
  - ``controller.eb.activate()``
  - ``controller.eb.release()``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from enums import CommandResult, HandlePosition
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
        force handle to EB, disengage ATO (``ato.engaged = False``), and (when
        an active path exists) write ``pending_path_cancel_request(EB)``. A
        dwelling train is READY with no active path, so it writes no cancel and
        its dwell timer keeps counting.
        """
        if self.active:
            return CommandResult(False, "Emergency brake already active")

        c = self._c

        self.active = True
        c.ops.handle_position = HandlePosition.EB
        c.ato.disengage()
        # EB springs both driver inputs to their safe rest (design_new.md
        # "ato.engaged"): the handle is pinned to EB above and the power notch
        # is zeroed here, symmetric with the per-tick EB hold. Nothing drives
        # under EB, and no retained percentage survives the release.
        c.manual.reset_power_percent_for_safety()

        if c.path_projection.received_path_plan is not None:
            # A safety action that recovers, so `"warning"` — never `"error"`,
            # which design_new.md reserves for the two fatal session entries.
            # A dwelling train is READY with no live authority, so this is never
            # taken while dwelling.
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
        synchronous. A dwelling train is READY, so a release while dwelling
        clears only ``active`` and the train keeps holding at the stop.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not c.powered:
            return CommandResult(False, "Power is off")
        if not self.active:
            return CommandResult(False, "Emergency brake not active")
        if not c.is_stopped():
            return CommandResult(False, "Cannot release emergency while moving")
        if c.path_projection.received_path_plan is not None:
            # A pending EB cancel keeps the path ACTIVE (completion is skipped
            # while `path_cancel_request` is set) and the cancel clears the plan
            # and the latch together, so `received_path_plan is None` already
            # implies no pending cancel — this one check covers both. Release
            # reads exactly the four physical-safety conditions (design_new.md
            # "eb.release()").
            return CommandResult(
                False, "Pre-EB path authority has not cleared yet"
            )
        # No power-notch check at release: EB springs both driver inputs to
        # their safe rest for as long as the latch is set (the per-tick EB hold
        # re-zeros `manual_power_percent`), so there is no retained percentage to
        # cross-check here (design_new.md "ato.engaged"). Release reads only the
        # physical-safety conditions above.
        self.active = False
        # Non-driving reset goes through the Operational-state helper, not a raw
        # handle_position write (design_new.md Module boundaries rule 5).
        c.ops.reset_handle_to_neutral()

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
