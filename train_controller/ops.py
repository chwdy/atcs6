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
        # Route-owned timed-run stop override (design_new.md
        # "TrainController.begin_service_stop() / consume_service_stop()").
        # Writers: TrainController.begin_service_stop() / consume_service_stop()
        # only; no other component assigns this field directly.
        self.service_stop_active: bool = False

    # ------------------------------------------------------------------
    # State transitions into / out of RUNNING_MOVING
    # ------------------------------------------------------------------

    def enter_running_moving(self) -> None:
        """Move the operational state from READY to RUNNING_MOVING (the
        "depart accepted / motion observed" state), leaving a controller that
        is already RUNNING_MOVING unchanged.

        This is the single state-writer for entering RUNNING_MOVING. Every
        path that enters that state goes through it: the reactive evaluation
        below, the ATO departure helper ``TrainController.ato.go()``, and the
        manual branch of ``TrainController.depart()`` (design_new.md
        "Controller state machine" and "TrainController.depart() canonical
        contract"). It writes only ``state``; arming an ATO run
        (``ato.running``) is a separate write owned by ``TrainController.ato.go()``.

        Idempotent from RUNNING_MOVING (setting the same value is a no-op),
        which is what lets ``depart()`` re-accept a train that is already
        moving. Its callers only reach it from READY or RUNNING_MOVING, per
        the Controller state machine.
        """
        self.state = TrainState.RUNNING_MOVING

    def evaluate_reactive_conditions(self) -> None:
        """Apply the two automatic operational-state transitions that depend
        on observed motion and live authority, not on an operator command.

        This is the one implementation of both transitions (design_new.md
        "Reactive condition evaluation"). It runs at two points in a tick:
        inside ``TrainController.on_physics_updated(...)`` right after the new
        speed is written (so a motion-caused change commits in the same
        Observe pass that produced it), and again inside
        ``TrainController.tick(...)`` right after the operator-command drain
        and the early departure/cancellation consumers (Action step ``71ee``,
        so a just-released EB or a just-cleared authority is acted on without
        waiting for the next movement calculation). Both calls are idempotent
        and read the canonical stopped predicate ``TrainController.is_stopped()``.

        Transition 1 — READY to RUNNING_MOVING: the controller is READY, a cab
        is selected, and the train is moving. Motion out of a cab-selected
        READY controller is a real departure (a manual driver pre-rolling
        before the route's ``depart()`` lands, or ATC clearance returning and
        physics resuming before the next plan tick). The cab guard is
        mandatory: every legal RUNNING_MOVING has a selected cab, so motion
        from a cabless READY controller is an invariant violation and must not
        be turned into an illegal state. After moving the state, call
        ``TrainController.ato.go()`` unconditionally — that helper checks its
        own preconditions and only sets ``ato.running`` when the controller is
        in ATO mode with live authority, so the same call is a no-op in manual
        mode (design_new.md "ato.running meaning and lifecycle").

        Transition 2 — RUNNING_MOVING to READY: the train has stopped, has no
        live authority (``received_path_plan is None``), and EB is not
        latched. This is braking completion with nothing left to move into.
        While live authority still exists the controller stays RUNNING_MOVING
        even at a stand (clearance may return and motion resume without a
        fresh ``depart()``), and while EB is latched this transition does not
        fire — ``TrainController.eb.release()`` clears that latch and the
        post-command call here then performs the move. This writes only
        ``state``; clearing ``ato.running`` on authority loss is the separate
        authority-clear ATO-stop bookkeeping (tick substep ``1b7a``), not this
        transition's job. RUNNING_AT_STATION is not a source state here and is
        left untouched.
        """
        c = self._c
        if (
            self.state == TrainState.READY
            and c.commands.cab in ("A", "B")
            and not c.is_stopped()
        ):
            self.enter_running_moving()
            c.ato.go()
        elif (
            self.state == TrainState.RUNNING_MOVING
            and c.is_stopped()
            and c.path_projection.received_path_plan is None
            and c.eb.active is False
        ):
            self.state = TrainState.READY

    # ------------------------------------------------------------------
    # Handle-position reset helpers (design_new.md "Module boundaries",
    # "Two reset helpers, no overlap")
    # ------------------------------------------------------------------

    def reset_handle_to_neutral(self) -> None:
        """Write the neutral notch — the non-driving neutral reset helper
        (design_new.md line 2319). Called by `TrainController._apply_power_off_request()`,
        `TrainController.commands.set_manual_mode_type()` when entering
        `power_percent`, and `TrainController.complete_power_off()`.
        """
        self.handle_position = HandlePosition.N

    def set_service_brake_to_zero(self) -> None:
        """Write the ordinary service-brake-to-zero notch — the braking notch
        that brings the current speed to a stop. The non-driving service-brake
        reset helper, called only by tick step `b41e`'s route-owned service-stop
        override (design_new.md line 2320).
        """
        c = self._c
        self.handle_position = c.ato.calculate_braking_notch_for_speed(
            c.physics_sync.current_speed_kmh, 0.0
        )
