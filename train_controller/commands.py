"""
Operator Command Surface sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the
Operator command surface owns the operator-side configuration state
(``cab`` selection, ``control_mode``), the per-tick command inbox /
canonical drain order, and the operator-facing setter / queue
methods themselves.

External callers access operator-command state and behavior as:

  - ``controller.commands.cab`` (``'A'`` / ``'B'`` / ``None``)
  - ``controller.commands.control_mode`` (``'ato'`` / ``'manual'``)
  - ``controller.commands.command_inbox`` (list of pending ``Command``s)
  - ``controller.commands.drain_order`` (canonical FIFO drain order)
  - ``controller.commands.power_on_cmd()``
  - ``controller.commands.set_cab(cab)`` / ``set_mode(mode)``
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

from enums import CommandResult, HandlePosition, TrainState
from runtime_payloads import Command

if TYPE_CHECKING:
    from train_controller import TrainController


logger = logging.getLogger(__name__)


class OperatorCommands:
    """Owns operator-side configuration state and the command inbox."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Operator cab/mode selection.
        self.cab: Any = None  # 'A', 'B', or None
        self.control_mode: str = "ato"  # 'ato' or 'manual'

        # Per design_new.md "Command queue + drain": operator commands
        # land here and the tick-entry drain processes them in canonical
        # ``drain_order``.
        self.command_inbox: List[Any] = []
        self.drain_order: tuple = (
            "power_on",
            "set_mode",
            "set_cab",
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

        Per design_new.md "Phase 2 — Execute" / "Driver/operator inputs",
        commands are drained in the order: power_on -> set_mode -> set_cab
        -> set_manual_mode_type -> set_handle / set_manual_power_percent ->
        activate_emergency_cmd -> release_emergency_cmd -> power_off.
        `ato_go` / `ato_stop` are internal helpers and are not part of the
        operator-command drain.

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
            elif kind == "set_cab":
                result = self._execute_set_cab(payload, event_time)
            elif kind == "set_mode":
                result = self._execute_set_mode(payload, event_time)
            elif kind == "set_manual_mode_type":
                result = self._execute_set_manual_mode_type(payload, event_time)
            elif kind == "set_handle":
                result = self._execute_set_handle(payload, event_time)
            elif kind == "set_manual_power_percent":
                result = self._execute_set_manual_power_percent(payload, event_time)
            # ato_go / ato_stop are not operator commands; they are internal
            # helpers invoked directly by depart() and by the reactive
            # transition in on_physics_updated. They do not appear in the
            # canonical drain order. Any leftover bucket of them gets caught
            # by the "unknown kind" notification in _drain_command_inbox.
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
                        "command_name": kind,
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

        if not result.success:
            c.notifications.emit(result.message, operation=kind)

    # Inline executors — these contain the actual command bodies. The public
    # methods below (`power_on_cmd` etc.) call them after enqueuing so the
    # behaviour is identical when no drain is needed (e.g. tests that call
    # the command directly without ever ticking).

    def _execute_power_on(self, payload, event_time) -> CommandResult:
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")
        if c.ops.state != TrainState.OFF:
            return CommandResult(False, "Power already on")
        c.ops.state = TrainState.READY
        c.ops.handle_position = HandlePosition.N
        return CommandResult(True, "Power on")

    def _execute_power_off(self, payload, event_time) -> CommandResult:
        # The drained-queue path delegates to the canonical command method so
        # the stopped-only check AND the already-OFF no-op guard
        # (design_new.md:1799) stay in one place. Otherwise an enqueued
        # `power_off` drained on an already-OFF train would arm a stale
        # `power_off` latch that tears a later freshly-powered train back to OFF.
        return self.power_off_cmd()

    def _execute_activate_emergency_cmd(self, payload, event_time) -> CommandResult:
        return self._c.eb.activate()

    def _execute_release_emergency_cmd(self, payload, event_time) -> CommandResult:
        return self._c.eb.release()

    def _execute_set_cab(self, payload, event_time) -> CommandResult:
        return self.set_cab(payload.get("cab"))

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

        Per design_new.md: valid only when state == OFF. On accept, transitions
        to READY regardless of cab selection (cab is orthogonal in the v1 state
        machine).
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if c.ops.state != TrainState.OFF:
            return CommandResult(False, "Power already on")

        c.ops.state = TrainState.READY
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
        Phase 3 cleanup.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not c.is_stopped():
            return CommandResult(False, "Cannot power off while moving")

        # power_off while already OFF is a no-op (design_new.md:1783): the train
        # is already off, so do NOT arm the `power_off` latch. A stale latch
        # would survive a later `power_on_cmd()` and tear the freshly-powered
        # train straight back to OFF in the next Phase-3 cleanup.
        if c.ops.state == TrainState.OFF:
            return CommandResult(True, "Power off (already off)")

        self._apply_power_off_request()

        return CommandResult(True, "Power off accepted; shutdown deferred to Phase 3")

    # -------------------------------------------------------------------------
    # Power-off request side effects
    # -------------------------------------------------------------------------
    #
    # The four Phase-3 latches themselves live on
    # `self._c.route_hooks.phase_latches` (see
    # `runtime_payloads.PhaseLatches`). Setting `power_off` also has the
    # cross-field side effect of clearing `ato_running`; that side effect is
    # bundled into `_apply_power_off_request(...)` so every power-off code
    # path stays consistent.

    def _apply_power_off_request(self, reason: str = "power off") -> None:
        """Set the `power_off` latch and disengage any active ATO run.

        Wraps `phase_latches.set_power_off(...)` together with the
        controller-local side effects of clearing `ato_running`, force-
        clearing any service-stop override, and neutralizing propulsion:
        `handle_position` goes to neutral and `manual.manual_power_percent`
        is zeroed, so the rest of this tick's `TrainController.tick(...)`
        cannot produce a non-neutral propulsion command while
        `pending_power_off` is set (design_new.md step `98d3`). The
        inclusive `path_cancel_request` fan-out is applied automatically
        when a live path exists, per design_new.md "Latch Table".
        """
        c = self._c
        c.ato.running = False
        c.ops.handle_position = HandlePosition.N
        c.manual.reset_power_percent_for_safety()
        # Force-clear through the service-stop teardown path so a run
        # interrupted by power-off never leaves braking armed on a later
        # route (design_new.md step `98d3`).
        c.complete_service_stop(force=True)
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

        # Per design_new.md line 1757: set_handle() accepts only when
        # control_mode == manual, manual_mode_type == simulation, and a cab is
        # selected; it is rejected in the power_percent submode (that submode is
        # driven by set_manual_power_percent()). The EB activation above is the
        # one always-available exception and returns before this gate.
        if self.control_mode != "manual":
            return CommandResult(False, "Handle input requires manual mode")
        if c.manual.manual_mode_type != "simulation":
            return CommandResult(
                False, "Handle input requires the manual 'simulation' submode"
            )
        if self.cab is None:
            return CommandResult(False, "Handle input requires a selected cab")

        c.ops.handle_position = position
        return CommandResult(True, f"Handle set to {position.value}")

    # -------------------------------------------------------------------------
    # Cab & Mode Commands
    # -------------------------------------------------------------------------

    def set_cab(self, cab: Optional[str], *, internal: bool = False) -> CommandResult:
        """
        Set active cab. Flips facing_direction when switching cabs.

        Rules:
        - Operator cab selection is only available in manual mode
          (``control_mode == "manual"``). In ATO the system aligns the cab
          itself, so an operator-issued ``set_cab`` is rejected.
        - Train must be stopped (speed = 0)
        - From any state other than RUNNING_AT_STATION, setting the cab (to
          'A', 'B', or None) transitions to READY. From RUNNING_AT_STATION the
          dwell state is preserved (and `set_cab(None)` is rejected outright).
        - Switching from A to B (or vice versa) flips facing_direction

        ``internal=True`` is the ATO auto-cab-selection path: the controller
        aligns the cab to the live path direction during ``depart()``. It
        bypasses the manual-mode gate (the system, not the operator, is
        selecting the cab) but keeps every other rule (stopped, frame flip).
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not c.is_stopped():
            return CommandResult(False, "Cannot switch cab while moving")

        # Operator cab selection is manual-only; ATO aligns the cab itself
        # via the internal=True path during depart().
        if not internal and self.control_mode != "manual":
            return CommandResult(
                False, "cab selection is only available in manual mode"
            )

        old_cab = self.cab

        # Per design_new.md: same-cab no-op success — does not change state,
        # does not disengage ato_running, no notification.
        if cab == old_cab:
            return CommandResult(True, f"Cab already {cab}")

        # Per design_new.md line 1630 + Allowed (state, cab) pairs table
        # (line 1589): `(RUNNING_AT_STATION, None)` is not a legal pair.
        # Reject `set_cab(None)` from RUNNING_AT_STATION; the operator must
        # finish dwell or pick a concrete cab letter instead.
        if cab is None and c.ops.state == TrainState.RUNNING_AT_STATION:
            return CommandResult(False, "cannot deselect cab while dwelling")

        old_head = "end_b" if old_cab == "B" else "end_a"
        new_head = "end_b" if cab == "B" else "end_a"
        reframe_error = c._reframe_span_for_head_change(old_head, new_head)
        if reframe_error is not None:
            return CommandResult(False, reframe_error)

        # Commit the cab only after both span locators have been successfully
        # re-expressed.  The head selector and its geometry therefore change as
        # one operation and can never disagree in the renderer or physics.
        self.cab = cab

        # Per design_new.md: changing cab is part of the depart-acceptance
        # contract; it disengages any active ATO run. set_cab from
        # RUNNING_AT_STATION preserves the dwell state.
        c.ato.running = False

        prev_state = c.ops.state
        if prev_state != TrainState.RUNNING_AT_STATION:
            c.ops.state = TrainState.READY

        return CommandResult(True, f"Cab set to {cab}")

    def set_mode(self, mode: str) -> CommandResult:
        """
        Set control mode (ATO or Manual).

        Per design_new.md, manual is a control mode, not a separate
        operational state. Stopped manual trains sit in READY just
        like ATO trains. From RUNNING_AT_STATION, the dwell state is
        preserved; only the side effects (ato_running cleared) apply.

        Rules:
        - Train must be stopped
        - 'ato' / 'manual' both leave a stopped train in READY, unless coming
          from RUNNING_AT_STATION which is preserved.
        """
        c = self._c
        train = c._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not c.is_stopped():
            return CommandResult(False, "Cannot switch mode while moving")

        if mode not in ("ato", "manual"):
            return CommandResult(False, f"Invalid mode: {mode}")

        self.control_mode = mode
        # Per design_new.md, set_mode disengages any active ATO run.
        c.ato.running = False

        # Preserve dwell state if currently dwelling; otherwise land in READY.
        if c.ops.state != TrainState.RUNNING_AT_STATION:
            c.ops.state = TrainState.READY
        return CommandResult(True, f"Mode set to {mode}")

    def set_manual_mode_type(self, mode_type: str) -> CommandResult:
        """
        Set manual-mode submode ('simulation' or 'power_percent').

        Per design_new.md, valid only while the train is stopped, powered,
        and control_mode == manual. Side effects: entering 'power_percent'
        resets handle_position to neutral; entering 'simulation' resets
        manual_power_percent to 0. Preserves cab selection and
        received_path_plan; does not start movement.
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
            c.ops.handle_position = HandlePosition.N
        else:
            c.manual.reset_power_percent_for_safety()

        return CommandResult(True, f"Manual mode type set to {mode_type}")

    def set_manual_power_percent(self, percent: int) -> CommandResult:
        """
        Set manual power-percent command (0..100).

        Per design_new.md: accepted only while manual mode is active,
        manual_mode_type == power_percent, the train is powered, and a cab is
        selected. It is accepted while EB is latched too, but ATC/EB override
        the stored value (forcing 0) and this command never clears
        ``eb.active``. The command does not start movement by itself; it just
        writes the controller-owned field.
        """
        c = self._c
        if not c.power_on:
            return CommandResult(False, "Power is off")
        if self.control_mode != "manual":
            return CommandResult(False, "Not in manual mode")
        if c.manual.manual_mode_type != "power_percent":
            return CommandResult(False, "Manual mode type is not power_percent")
        if self.cab not in ("A", "B"):
            return CommandResult(False, "No cab selected")

        try:
            value = int(percent)
        except (TypeError, ValueError):
            return CommandResult(False, f"Invalid power percent: {percent!r}")

        clamped = max(0, min(100, value))
        c.manual.set_power_percent(clamped)
        return CommandResult(True, f"Manual power percent set to {clamped}")
