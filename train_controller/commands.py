"""
Operator Command Surface sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the
Operator command surface owns the operator-side configuration state
(``control_mode``, ``engaged``) and the operator-facing command methods
themselves. There is no operator cab: the head end is derived from the live
authority (design_new.md "Reservation-side head derivation").

Operator commands run between ticks (design_new.md "Operator-command
processing slot"): the request handler calls the command while holding
``AppState.simulation_lock``, the command checks its own preconditions against
the controller state at that moment, and returns its ``CommandResult``. There
is no command queue. Action's first substep (step ``01f2``) only applies the
emergency request Observe latched (``apply_emergency_request``).

External callers access operator-command state and behavior as:

  - ``controller.commands.control_mode`` (``'ato'`` / ``'manual'``)
  - ``controller.commands.engaged``
  - ``controller.commands.power_on_cmd()``
  - ``controller.commands.set_mode(mode)``
  - ``controller.commands.ato_go()`` / ``controller.commands.ato_stop()``
  - ``controller.commands.set_manual_mode_type(mode_type)``
  - ``controller.commands.set_handle(position)``
  - ``controller.commands.set_manual_power_percent(percent)``
  - ``controller.commands.activate_cut_out_cmd()``
  - ``controller.commands.power_off_cmd()``
  - ``controller.commands.report_result(operation, result)``

The command methods orchestrate writes across multiple sub-modules
(``eb``, ``ato``, ``manual``, ``route_hooks``, ``physics_sync``,
``ops``) through the controller back-reference ``self._c``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from enums import CommandResult, HandlePosition

if TYPE_CHECKING:
    from train_controller import TrainController


logger = logging.getLogger(__name__)


class OperatorCommands:
    """Owns operator-side configuration state and the operator commands."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Operator mode selection. There is no operator cab: the head end is
        # derived from the live authority (design_new.md "Reservation-side head
        # derivation"), so no cab is stored or selected here. Defaults to
        # `"manual"`: a powered-on train reads `train_status == MANUAL`
        # (design_new.md "Controller state machine" / power-on contract) — the
        # operator selects ATO with `set_mode("ato")`. A never-powered train
        # that has run no OFF cleanup still starts here, so the default is the
        # same MANUAL an OFF cleanup would leave.
        self.control_mode: str = "manual"  # 'ato' or 'manual'

        # ATO engage bit — whether the operator has started ATO driving with
        # `/ato/go` (design_new.md "ATO status", `commands.engaged`). Written by
        # `ato_go()` / `ato_stop()` (accepted only when `train_status == ATO`;
        # `ato_go()` also needs a route assigned to the train),
        # cleared by any `control_mode` write (`_write_control_mode`), by the
        # canonical EB activation effects, and by a new route assignment. Read
        # only in ATO, by the derived `ato_status` and the ATO driving rule.
        # Serialized under the key `"engaged"`.
        self.engaged: bool = False

    # -------------------------------------------------------------------------
    # Action step 01f2 and command-result reporting
    # -------------------------------------------------------------------------

    def apply_emergency_request(self) -> None:
        """Action step `01f2`: apply the emergency request Observe latched this
        tick, then clear it (design_new.md "Operator-command processing slot",
        Action begins by applying any emergency request; Latch Table row
        `e9c7`).

        `"cut_out"` calls `eb.activate_cut_out()` and `"brake"` calls
        `eb.activate()`. The request is not an operator command, so it carries no
        operating-environment gate; both methods no-op on a train whose
        emergency brake is already latched.
        """
        c = self._c
        latches = c.route_hooks.phase_latches
        request = latches.emergency_request
        if request is None:
            return
        try:
            if request == "cut_out":
                c.eb.activate_cut_out()
            else:
                c.eb.activate()
        finally:
            latches.clear("emergency_request")

    def report_result(self, operation: str, result: CommandResult) -> None:
        """Emit one command-result event for an operator command
        (design_new.md "Operator-command processing slot", A command's result is
        its return value; "Runtime notifications").

        `operation` is the command's own name; the event is `info` on acceptance
        and `warning` on rejection, with `{success, reason}` in its payload. The
        request handler calls this once per operator command it ran, so internal
        callers (ATC escalation, step `01f2`) report nothing here.
        """
        from notification_center import Kind

        self._c.notifications.emit(
            result.message
            or (f"{operation} accepted" if result.success else f"{operation} rejected"),
            operation=operation,
            kind=Kind.INFO if result.success else Kind.WARNING,
            payload={"success": result.success, "reason": result.message},
        )

    # -------------------------------------------------------------------------
    # Power Commands
    # -------------------------------------------------------------------------

    def power_on_cmd(self) -> CommandResult:
        """
        Turn on train power.

        Per design_new.md "Power/mode command contracts": valid only when power
        is currently off. On accept it sets the stored `powered` flag, leaving
        the train `train_status == MANUAL` (the stored `control_mode` is manual
        from OFF cleanup) with no reservation. It does not touch `eb.active`.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if c.powered:
            return CommandResult(False, "Power already on")

        c.powered = True
        c.ops.handle_position = HandlePosition.N

        return CommandResult(True, "Power on")

    def power_off_cmd(self) -> CommandResult:
        """
        Turn off train power.

        Per design_new.md: stopped-only at acceptance, valid even when
        eb_active == True. On acceptance, immediately disengages any active
        ATO run, sets pending_power_off = True, and (when an active path
        exists) also writes pending_path_cancel_request with initiator =
        "power_off" (inclusive-latch rule). The actual dispatcher cancellation,
        route teardown, and OFF transition happen in the later eligible
        Action cleanup.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not c.is_stopped():
            return CommandResult(False, "Cannot power off while moving")

        # power_off while already OFF is a no-op (design_new.md "Power/mode
        # command contracts"): the train is already off, so do NOT arm the
        # `power_off` latch. A stale latch would survive a later
        # `power_on_cmd()` and tear the freshly-powered train straight back to
        # OFF in the next Action cleanup.
        if not c.powered:
            return CommandResult(True, "Power off (already off)")

        self._apply_power_off_request()

        return CommandResult(True, "Power off accepted; shutdown deferred to Action")

    def activate_cut_out_cmd(self) -> CommandResult:
        """Operator-initiated emergency cut-out (design_new.md "Emergency
        cut-out", The operator-initiated path is gated to hardware environments).

        The operator-initiated cut-out is accepted only while the selected
        operating environment is Virtual Hardware or Real Hardware; in Simulation
        it is not offered — only the emergency brake exists — because there is no
        physical locomotive to stop faster than the modelled brake curve and no
        output boundary to de-energize. When accepted it runs
        `eb.activate_cut_out()`. That method itself carries no environment gate,
        so the cut-out request Observe latches on an overrun is applied by the
        drain in every environment; this operator gate is the one place the
        hardware-only rule lives.
        """
        import operation_environment
        from enums import OperationMode

        if operation_environment.get_selected() is OperationMode.SIMULATION:
            return CommandResult(
                False, "Emergency cut-out is only available on a hardware environment"
            )
        return self._c.eb.activate_cut_out()

    # -------------------------------------------------------------------------
    # Control-mode writer (design_new.md "ATO status")
    # -------------------------------------------------------------------------

    def _write_control_mode(self, mode: str) -> None:
        """The one place `control_mode` is assigned.

        Any write of `control_mode` clears the ATO engage bit (design_new.md
        "ATO status": "Any write of `control_mode` sets it `False`"), so
        `set_mode(...)`, EB activation, and both power-off paths all route
        through here and a re-selected or released-into ATO train lands not
        engaged (`ato_status == READY`).
        """
        self.control_mode = mode
        self.engaged = False

    # -------------------------------------------------------------------------
    # Power-off request side effects
    # -------------------------------------------------------------------------
    #
    # The five Action latches themselves live on
    # `self._c.route_hooks.phase_latches` (see
    # `runtime_payloads.PhaseLatches`). Power-off acceptance also sets
    # `control_mode = "manual"` (which clears `commands.engaged`); that
    # cross-field write is bundled into `_apply_power_off_request(...)` so every
    # power-off code path stays consistent.

    def _apply_power_off_request(self, reason: str = "power off") -> None:
        """Set the `power_off` latch and take the train to manual.

        Wraps `phase_latches.set_power_off(...)` together with the
        controller-local side effects of setting `control_mode = "manual"`
        (which clears `commands.engaged`, per design_new.md "Driving mode",
        Power-off acceptance) and neutralizing propulsion: `handle_position`
        goes to neutral and `manual.manual_power_percent` is zeroed, so the rest
        of this tick's `TrainController.tick(...)` cannot produce a non-neutral
        propulsion command while `pending_power_off` is set (design_new.md step
        `98d3`). The inclusive `path_cancel_request` fan-out is applied
        automatically when a live path exists, per design_new.md "Latch Table".
        """
        c = self._c
        self._write_control_mode("manual")
        c.ops.reset_handle_to_neutral()
        c.manual.reset_power_percent_for_safety()
        c.route_hooks.phase_latches.set_power_off(
            reason=reason,
            has_active_path=c.path_projection.received_path_plan is not None,
        )

    # -------------------------------------------------------------------------
    # Handle Commands
    # -------------------------------------------------------------------------

    def set_handle(self, position: HandlePosition) -> CommandResult:
        """
        Set throttle/brake handle position.

        Rules:
        - Must be powered on
        - In manual mode: stores ``handle_position``; physics integrates it,
          but ATC and EB can override (EB emergency-brakes the train
          regardless of the handle)
        - In ATO mode while running: rejected (handle controlled by computer)
        - ``HandlePosition.EB`` activates the emergency brake
        - Accepted while EB is latched (the operator may move the handle to any
          position), but the handle never releases EB; EB is released only by
          ``eb.release()``
        """
        c = self._c
        # design_new.md "Driver input acceptance": the `powered` and
        # `control_mode == "manual"` checks run first, for every position.
        if not c.power_on:
            return CommandResult(False, "Power is off")
        if self.control_mode != "manual":
            return CommandResult(False, "Handle input requires manual mode")

        # The `set_handle(EB)` bypass: an EB handle activates the emergency brake
        # whatever the submode. Only the submode check is bypassed; an ATO
        # train reaches EB through the ungated `eb.activate()` instead. There is
        # no cab gate — the head end is derived from the live authority
        # (design_new.md "Reservation-side head derivation").
        if position == HandlePosition.EB:
            return c.eb.activate()

        if c.manual.manual_mode_type != "simulation":
            return CommandResult(
                False, "Handle input requires the manual 'simulation' submode"
            )

        c.ops.handle_position = position
        return CommandResult(True, f"Handle set to {position.value}")

    # -------------------------------------------------------------------------
    # Mode Commands
    # -------------------------------------------------------------------------

    def ato_go(self) -> CommandResult:
        """Start ATO driving (design_new.md "ATO go").

        Accepted only when `train_status == ATO` and the train has a route
        assigned (`RouteExecutor.assigned_route_id_for(train_id)` is not None);
        otherwise (not powered, under EB, in manual, or with no route to drive)
        it returns `success=False` and changes nothing. Like a real ATO start
        button, Go is offered only once there is a trip to drive: with no route
        the engage bit would drive nothing, and the next route assignment clears
        it anyway. On acceptance it sets `commands.engaged = True` (the
        canonical `/ato/go` writer). It creates no reservation itself: the next
        Plan pass acquires reservation once the ATO engage gate is open.
        Idempotent while already engaged.
        """
        from enums import TrainStatus

        c = self._c
        if c.ops.train_status != TrainStatus.ATO:
            return CommandResult(False, "ATO go requires the train in ATO mode")
        route_executor = c.route_executor
        if (
            route_executor is None
            or route_executor.assigned_route_id_for(c.train_id) is None
        ):
            return CommandResult(False, "ATO go requires a route assigned to the train")
        self.engaged = True
        return CommandResult(True, "ATO go")

    def ato_stop(self) -> CommandResult:
        """Pause ATO driving (design_new.md "ATO stop").

        Accepted only when `train_status == ATO`; otherwise it returns
        `success=False` and changes nothing. Unlike the stopped-only commands
        it is accepted while the train is moving — pausing a running ATO train
        is its purpose. On acceptance it sets `commands.engaged = False`, so the
        ATO driving rule commands `0` and the train brakes to a stand at
        `ato_status == READY`. The reservation it holds is retained, not
        released (the ATO engage gate). Idempotent while already not engaged.
        """
        from enums import TrainStatus

        c = self._c
        if c.ops.train_status != TrainStatus.ATO:
            return CommandResult(False, "ATO stop requires the train in ATO mode")
        self.engaged = False
        return CommandResult(True, "ATO stop")

    def set_mode(self, mode: str) -> CommandResult:
        """
        Set control mode (ATO or Manual).

        Per design_new.md, manual is a control mode, not a separate
        operational state.

        Rules:
        - Train must be stopped
        - set_mode from OFF also powers the controller up as a side effect.
        - Any control_mode write clears `commands.engaged`, so set_mode("ato")
          lands the train not engaged (`ato_status == READY`); driving waits for
          `/ato/go`.
        - The retained `manual.manual_power_percent` is cleared.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not c.is_stopped():
            return CommandResult(False, "Cannot switch mode while moving")

        if mode not in ("ato", "manual"):
            return CommandResult(False, f"Invalid mode: {mode}")

        # Rejected while a calibration leg or run is open for this train: a
        # calibration pass drives the train itself and outranks the ordinary
        # ATO/manual sources, so its driving mode must not change underneath it
        # — the operator ends the pass first (design_new.md "set_mode":
        # rejected while `SpeedCalibration.get_propulsion_override(train_id)` is
        # not None; "calibration outranks ATO"). Reached through the same
        # `simulation_controller` back-reference the controller's own
        # calibration-output read uses; it is None in unit-test controllers,
        # where no pass can be open. `get_propulsion_override(...) is not None`
        # (not the power-percent-only read) so an open lighting run is covered
        # too — a stopped auto pass returns value=0.0, still non-None.
        sc = getattr(c, "simulation_controller", None)
        calibration = getattr(sc, "speed_calibration", None) if sc is not None else None
        if (
            calibration is not None
            and calibration.get_propulsion_override(c.train_id) is not None
        ):
            return CommandResult(False, "Cannot switch mode during calibration")

        # Select the driving source. Any control_mode write clears the ATO
        # engage bit (design_new.md "ATO status"), so a fresh ATO selection
        # lands not engaged and driving waits for `/ato/go`.
        self._write_control_mode(mode)
        # A mode change also clears the retained manual power notch: it is
        # set-and-forget, so a percentage parked before the change must not
        # become the train's command after it with no fresh driver input.
        c.manual.reset_power_percent_for_safety()

        # A stopped train issuing set_mode is brought under control: from OFF
        # this powers the controller up as a side effect (design_new.md
        # "Controller state machine": set_mode from OFF sets `powered = True`).
        c.powered = True
        return CommandResult(True, f"Mode set to {mode}")

    def set_manual_mode_type(self, mode_type: str) -> CommandResult:
        """
        Set manual-mode submode ('simulation' or 'power_percent').

        Per design_new.md, valid only while the train is stopped, powered,
        and control_mode == manual. Side effects: entering 'power_percent'
        resets handle_position to neutral; entering 'simulation' resets
        manual_power_percent to 0. Preserves received_path_plan; does not
        start movement.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")
        if not c.power_on:
            return CommandResult(False, "Power is off")
        if self.control_mode != "manual":
            return CommandResult(False, "Not in manual mode")
        if not c.is_stopped():
            return CommandResult(False, "Cannot change manual mode type while moving")
        if mode_type not in ("simulation", "power_percent"):
            return CommandResult(False, f"Invalid manual mode type: {mode_type}")

        c.manual.manual_mode_type = mode_type
        if mode_type == "power_percent":
            c.ops.reset_handle_to_neutral()
        else:
            c.manual.reset_power_percent_for_safety()

        return CommandResult(True, f"Manual mode type set to {mode_type}")

    def set_manual_power_percent(self, percent: int) -> CommandResult:
        """
        Set manual power-percent command (0..100).

        Per design_new.md: accepted only while manual mode is active,
        manual_mode_type == power_percent, and the train is powered. It is
        accepted while EB is latched too, but ATC/EB override the stored value
        (forcing 0) and this command never clears ``eb.active``. The command
        does not start movement by itself; it just writes the controller-owned
        field. There is no cab gate — the head end is derived from the live
        authority (design_new.md "Reservation-side head derivation").
        """
        c = self._c
        if not c.power_on:
            return CommandResult(False, "Power is off")
        if self.control_mode != "manual":
            return CommandResult(False, "Not in manual mode")
        if c.manual.manual_mode_type != "power_percent":
            return CommandResult(False, "Manual mode type is not power_percent")

        try:
            value = int(percent)
        except (TypeError, ValueError):
            return CommandResult(False, f"Invalid power percent: {percent!r}")

        clamped = max(0, min(100, value))
        c.manual.set_power_percent(clamped)
        return CommandResult(True, f"Manual power percent set to {clamped}")
