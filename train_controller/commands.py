"""
Operator Command Surface sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the
Operator command surface owns the operator-side configuration state
(``control_mode``), the per-tick command inbox / canonical drain order,
and the operator-facing setter / queue methods themselves. There is no
operator cab: the head end is derived from the live authority
(design_new.md "Reservation-side head derivation").

External callers access operator-command state and behavior as:

  - ``controller.commands.control_mode`` (``'ato'`` / ``'manual'``)
  - ``controller.commands.command_inbox`` (list of pending ``Command``s)
  - ``controller.commands.drain_order`` (canonical FIFO drain order)
  - ``controller.commands.power_on_cmd()``
  - ``controller.commands.set_mode(mode)``
  - ``controller.commands.set_manual_mode_type(mode_type)``
  - ``controller.commands.set_handle(position)``
  - ``controller.commands.set_manual_power_percent(percent)``
  - ``controller.commands.power_off_cmd()``
  - ``controller.commands.enqueue_command(kind, payload)``

The setters orchestrate writes across multiple sub-modules
(``eb``, ``ato``, ``manual``, ``route_hooks``, ``physics_sync``,
``ops``) through the controller back-reference ``self._c``.
"""

from __future__ import annotations

from runtime_payloads import ErrorRecord as _ErrorRecord

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from enums import CommandResult, HandlePosition
from runtime_payloads import Command

if TYPE_CHECKING:
    from train_controller import TrainController


logger = logging.getLogger(__name__)


class OperatorCommands:
    """Owns operator-side configuration state and the command inbox."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Operator mode selection. There is no operator cab: the head end is
        # derived from the live authority (design_new.md "Authority-side head
        # derivation"), so no cab is stored or selected here.
        self.control_mode: str = "ato"  # 'ato' or 'manual'

        # ATO engage bit — whether the operator has started ATO driving with
        # `/ato/go` (design_new.md "ATO status", `commands.engaged`). Written by
        # `ato_go()` / `ato_stop()` (accepted only when `train_status == ATO`),
        # cleared by any `control_mode` write (`_write_control_mode`) and by a
        # new route assignment. Read only in ATO, by the derived `ato_status`
        # and the ATO driving rule. Serialized under the key `"engaged"`.
        self.engaged: bool = False

        # Per design_new.md "Operator-command processing slot": operator commands
        # land here and the tick-entry drain processes them in canonical
        # ``drain_order``. `set_mode` (slot 2) drains before `ato_go` / `ato_stop`
        # (slot 3), so a same-tick "switch to ATO, then go" batch is accepted in
        # one drain.
        self.command_inbox: List[Any] = []
        self.drain_order: tuple = (
            "power_on",
            "set_mode",
            "ato_go",
            "ato_stop",
            "set_manual_mode_type",
            "set_handle",
            "set_manual_power_percent",
            "activate_emergency_cmd",
            "release_emergency_cmd",
            "power_off",
        )

    # -------------------------------------------------------------------------
    # Command inbox plumbing
    # -------------------------------------------------------------------------

    def _estimate_event_time(self) -> float:
        """Best-effort current event time for drain bookkeeping."""
        c = self._c
        sc = getattr(c, "simulation_controller", None)
        getter = getattr(sc, "current_event_time", None) if sc is not None else None
        if callable(getter):
            try:
                return float(getter())
            except Exception:
                pass
        try:
            import time as _time
            return _time.time()
        except Exception:
            return 0.0

    def _enqueue_command(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> Command:
        """Push a Command onto command_inbox and return it."""
        import uuid as _uuid
        cmd = Command(
            kind=kind,
            payload=dict(payload or {}),
            command_id=_uuid.uuid4().hex,
            accepted_at=self._estimate_event_time(),
        )
        self.command_inbox.append(cmd)
        return cmd

    def enqueue_command(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> Command:
        """Public alias for `_enqueue_command` (used by bootstrap and external callers)."""
        return self._enqueue_command(kind, payload)

    def _drain_command_inbox(self, event_time: float) -> None:
        """Drain the command inbox in canonical kind order, FIFO within each.

        Per design_new.md "Per-train tick transaction" / "Driver/operator inputs",
        commands are drained in the order: power_on -> set_mode ->
        ato_go / ato_stop -> set_manual_mode_type -> set_handle /
        set_manual_power_percent -> activate_emergency_cmd ->
        release_emergency_cmd -> power_off. set_mode (slot 2) drains before
        ato_go / ato_stop (slot 3), so a same-tick "switch to ATO, then go"
        batch is accepted in one drain.

        Each drained command re-checks acceptance gates by delegating to the
        existing inline command method. Rejected commands are silently dropped
        (their inline method's CommandResult.success=False is observed via
        notifications, not by the caller).
        """
        if not self.command_inbox:
            return

        # Bucket commands by kind preserving FIFO within each bucket.
        buckets: Dict[str, List[Command]] = {}
        for cmd in self.command_inbox:
            buckets.setdefault(cmd.kind, []).append(cmd)
        self.command_inbox.clear()

        for kind in self.drain_order:
            cmds = buckets.pop(kind, [])
            for cmd in cmds:
                self._execute_drained_command(cmd, event_time)
        # Any unknown kinds left over: drop with a notification.
        for kind, cmds in buckets.items():
            for cmd in cmds:
                self._c.notifications.emit(
                    f"Unknown command kind: {kind}",
                    operation="_drain_command_inbox",
                )

    def _execute_drained_command(self, cmd: Command, event_time: float) -> None:
        """Dispatch one drained command to its inline executor.

        Re-checks acceptance gates inside the inline method. Notifies the
        operator on failure via pending_notifications. When wired, also emits
        a `command_result` notification through the simulation controller's
        notification center.
        """
        c = self._c
        kind = cmd.kind
        payload = cmd.payload or {}
        result: Optional[CommandResult] = None
        try:
            if kind == "power_on":
                result = self._execute_power_on(payload, event_time)
            elif kind == "power_off":
                result = self._execute_power_off(payload, event_time)
            elif kind == "activate_emergency_cmd":
                result = self._execute_activate_emergency_cmd(payload, event_time)
            elif kind == "release_emergency_cmd":
                result = self._execute_release_emergency_cmd(payload, event_time)
            elif kind == "set_mode":
                result = self._execute_set_mode(payload, event_time)
            elif kind == "ato_go":
                result = self.ato_go()
            elif kind == "ato_stop":
                result = self.ato_stop()
            elif kind == "set_manual_mode_type":
                result = self._execute_set_manual_mode_type(payload, event_time)
            elif kind == "set_handle":
                result = self._execute_set_handle(payload, event_time)
            elif kind == "set_manual_power_percent":
                result = self._execute_set_manual_power_percent(payload, event_time)
        except Exception as exc:
            c.notifications.emit(
                f"Command {kind} raised: {exc}",
                operation=kind,
                error=_ErrorRecord(str(exc), "command_raised"),
            )
            return

        if result is None:
            return

        # Emit a command_result notification through the simulation
        # controller's notification center.
        sc = getattr(c, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is not None:
            try:
                from notification_center import Kind as _Kind, train_ref as _tref

                # A command result is an ordinary event: `operation` is the
                # command's own function name, `kind` is `info` on acceptance
                # and `warning` on rejection (design_new.md "Runtime
                # notifications").
                nc.emit(
                    source="TrainController",
                    operation=kind,
                    kind=_Kind.INFO if result.success else _Kind.WARNING,
                    message=result.message or (
                        f"{kind} accepted" if result.success else f"{kind} rejected"
                    ),
                    entities=[_tref(c.train_id)],
                    event_time=event_time,
                    payload={
                        "command_id": cmd.command_id,
                        "success": result.success,
                        "reason": result.message,
                    },
                )
            except Exception:
                logger.warning(
                    "NotificationCenter.emit() failed for command_result "
                    "(train=%s, kind=%s); the command itself completed",
                    c.train_id,
                    kind,
                    exc_info=True,
                )
        # The single command_result event above (operation=kind, kind=warning on
        # rejection) is the canonical per-command notification; a rejected
        # command does not additionally emit a second bare warning
        # (design_new.md "Runtime notifications").

    # Inline executors — these contain the actual command bodies. The public
    # methods below (`power_on_cmd` etc.) call them after enqueuing so the
    # behaviour is identical when no drain is needed (e.g. tests that call
    # the command directly without ever ticking).

    def _execute_power_on(self, payload, event_time) -> CommandResult:
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")
        if c.powered:
            return CommandResult(False, "Power already on")
        c.powered = True
        c.ops.handle_position = HandlePosition.N
        return CommandResult(True, "Power on")

    def _execute_power_off(self, payload, event_time) -> CommandResult:
        # The drained-queue path delegates to the canonical command method so
        # the stopped-only check AND the already-OFF no-op guard
        # (design_new.md, "Power/mode command contracts") stay in one place. Otherwise an enqueued
        # `power_off` drained on an already-OFF train would arm a stale
        # `power_off` latch that tears a later freshly-powered train back to OFF.
        return self.power_off_cmd()

    def _execute_activate_emergency_cmd(self, payload, event_time) -> CommandResult:
        return self._c.eb.activate()

    def _execute_release_emergency_cmd(self, payload, event_time) -> CommandResult:
        return self._c.eb.release()

    def _execute_set_mode(self, payload, event_time) -> CommandResult:
        return self.set_mode(payload.get("mode"))

    def _execute_set_manual_mode_type(self, payload, event_time) -> CommandResult:
        return self.set_manual_mode_type(payload.get("mode_type"))

    def _execute_set_handle(self, payload, event_time) -> CommandResult:
        return self.set_handle(payload.get("position"))

    def _execute_set_manual_power_percent(self, payload, event_time) -> CommandResult:
        return self.set_manual_power_percent(payload.get("percent"))

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
    # The four Action latches themselves live on
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
        if not c.power_on:
            return CommandResult(False, "Power is off")

        if position == HandlePosition.EB:
            return c.eb.activate()

        # Per design_new.md "Driver input acceptance": set_handle() accepts only when
        # control_mode == manual and manual_mode_type == simulation; it is
        # rejected in the power_percent submode (that submode is driven by
        # set_manual_power_percent()). The EB activation above is the one
        # always-available exception and returns before this gate. There is no
        # cab gate — the head end is derived from the live authority
        # (design_new.md "Reservation-side head derivation").
        if self.control_mode != "manual":
            return CommandResult(False, "Handle input requires manual mode")
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

        Accepted only when `train_status == ATO`; otherwise (not powered, under
        EB, or in manual) it returns `success=False` and changes nothing. On
        acceptance it sets `commands.engaged = True` (the canonical `/ato/go`
        writer). It creates no reservation itself: the next Plan pass acquires
        reservation once the ATO engage gate is open. Idempotent while already
        engaged.
        """
        from enums import TrainStatus

        c = self._c
        if c.ops.train_status != TrainStatus.ATO:
            return CommandResult(False, "ATO go requires the train in ATO mode")
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
