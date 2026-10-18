"""
EB Workflow sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the EB
Workflow owns the ``active`` latch and the operator-EB activation /
release contract. Cross-sub-module state (``handle_position``,
``control_mode`` / ``commands.engaged``, ``phase_latches``, ``powered``,
``received_path_plan``) is reached through the controller back-reference
``self._c`` so this class stays a single-purpose owner of EB-specific state
and behavior.

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
        # Whether the active latch is an emergency cut-out (traction voltage
        # driven to `0`, immediate stop in every submode) rather than an
        # emergency brake (deceleration at `SpeedProfile.emergency_brake_rate`).
        # Only meaningful while `active`. Set `True` by `activate_cut_out()`;
        # set `False` by `release()` and `clear()` alongside `active`. Serialized
        # under the key `"eb_cut_out"` (design_new.md "Emergency cut-out").
        self.cut_out: bool = False

    def _activate(self, *, cut_out: bool) -> CommandResult:
        """Canonical EB activation effects (design_new.md "Canonical EB
        activation effects"), shared by the emergency brake and the emergency
        cut-out.

        The only acceptance gate is ``active == False`` (idempotent rejection of
        a duplicate activation). Valid in any motion state, any status including
        OFF, with or without an active path. On acceptance: set ``active =
        True``, force handle to EB, clear ``commands.engaged`` while leaving
        ``control_mode`` unchanged, reset the power notch, and (when an active
        path exists) write ``pending_path_cancel_request(EB)``. A dwelling train holds
        no live path, so it writes no cancel and its dwell timer keeps counting.

        A cut-out (``cut_out=True``) performs every effect above and additionally
        sets ``cut_out = True`` — the one difference an emergency cut-out adds to
        an emergency brake (design_new.md "Emergency cut-out"). The method itself
        carries no environment gate, so the cut-out request Observe latches on an
        overrun is applied in every environment; the operator-initiated
        cut-out's hardware-only gate lives on the operator command path.
        """
        kind_label = "emergency cut-out" if cut_out else "emergency brake"
        if self.active:
            return CommandResult(False, f"{kind_label.capitalize()} already active")

        c = self._c

        self.active = True
        if cut_out:
            self.cut_out = True
        c.ops.handle_position = HandlePosition.EB
        # Canonical EB activation effect: EB stops ATO driving, so the engage
        # bit clears, but `control_mode` is left as it was (design_new.md
        # "Canonical EB activation effects"). A released ATO train reads
        # `ato_status == READY` and drives again only after `/ato/go`.
        c.commands.engaged = False
        # EB springs both driver inputs to their safe rest: the handle is pinned
        # to EB above and the power notch is zeroed here, symmetric with the
        # per-tick EB hold. Nothing drives under EB, and no retained percentage
        # survives the release.
        c.manual.reset_power_percent_for_safety()

        if c.path_projection.received_path_plan is not None:
            # A safety action that recovers, so `"warning"` — never `"error"`,
            # which design_new.md reserves for the two fatal session entries.
            # A dwelling train is READY with no live authority, so this is never
            # taken while dwelling.
            origin = "eb cut-out" if cut_out else "operator EB"
            c.route_hooks.phase_latches.set_path_cancel_request(
                ErrorRecord(origin, "eb"), "warning"
            )

        from notification_center import Kind

        c._emit_event(
            "activate", f"{kind_label} activated for {c.train_id}",
            kind=Kind.WARNING,
        )
        return CommandResult(True, f"{kind_label.capitalize()} activated")

    def activate(self) -> CommandResult:
        """Operator-issued or ATC-escalated emergency brake.

        Runs the **canonical EB activation effects** (design_new.md) with no
        cut-out — the train decelerates down the ``emergency_brake_rate`` curve.
        """
        return self._activate(cut_out=False)

    def activate_cut_out(self) -> CommandResult:
        """Emergency cut-out — the emergency-brake latch with ``cut_out`` set.

        Per design_new.md "Emergency cut-out": performs the **canonical EB
        activation effects** and additionally sets ``cut_out = True``, so the
        train's traction voltage is driven to ``0`` and it stops the same tick in
        every submode instead of braking down the ``emergency_brake_rate`` curve.
        The acceptance gate is ``active == False``, the same duplicate-rejection
        as ``activate()``; it has no motion-state or OFF gate, and the method
        carries no environment gate. Called by the operator command drain (slot
        7, hardware-only) and, at the end of the drain, for a cut-out request
        Observe latched on an ``overrun_entry`` attribution result (ungated).
        """
        return self._activate(cut_out=True)

    def release(self) -> CommandResult:
        """Release emergency stop.

        Per design_new.md "`TrainController.eb.release()`": accepts only when ``state != OFF``,
        ``active == True``, ``is_stopped() == True``, the controller's own
        ``pending_path_cancel_request`` is None (the EB cancel latch has
        been consumed by Action), and the controller's own
        ``received_path_plan`` is None (the pre-EB authority has been
        cleared by ``PathDispatcher``). Otherwise return success=False with
        controller state unchanged.

        This method clears ``active`` and ``cut_out`` together (a cut-out resets
        exactly as an emergency brake does, design_new.md "Emergency cut-out"),
        and resets the handle to neutral. It writes no ``train_status`` (that
        read is derived): once ``active`` clears, a powered train reads its
        stored ``control_mode`` again, which EB left unchanged — ``MANUAL``, or
        ``ATO`` with ``commands.engaged`` cleared, per design_new.md "EB
        release". It writes no ``control_mode``; an ATO train drives again after
        ``/ato/go``. A train holding at a stop keeps holding after the release.
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
        # A cut-out resets back to normal exactly as an emergency brake does
        # (design_new.md "Emergency cut-out").
        self.cut_out = False
        # Non-driving reset goes through the Operational-state helper, not a raw
        # handle_position write (design_new.md Module boundaries rule 5).
        c.ops.reset_handle_to_neutral()

        # `train_status` / `ato_status` are derived reads (design_new.md
        # "Controller state machine" / "ATO status"), so clearing `eb.active` is
        # all that is written here: a powered train then reads
        # `train_status == MANUAL` with no stored state transition to make.
        from notification_center import Kind

        c._emit_event(
            "release", f"emergency stop released for {c.train_id}",
            kind=Kind.WARNING,
        )
        return CommandResult(True, "Emergency stop released")

    def clear(self) -> None:
        """Ungated EB-latch clear (design_new.md, "EB workflow state machine").

        The EB Workflow is the single writer of ``active`` and ``cut_out``
        (module-boundary rule), so OFF-cleanup
        (``TrainController.complete_power_off()``) clears both latches through
        this method rather than assigning them directly. Unlike ``release()``
        this has no preconditions — it is the unconditional teardown clear used
        when the controller powers off."""
        self.active = False
        self.cut_out = False
