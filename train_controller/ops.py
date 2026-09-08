"""
Operational State Machine sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: this
sub-module owns the canonical operational state (``state``: ``OFF`` /
``READY`` / ``RUNNING_MOVING``), the current
handle position, and the diagnostic ``handle_reason`` string.

External callers access operational state as:

  - ``controller.ops.state`` (TrainState)
  - ``controller.ops.handle_position`` (HandlePosition)
  - ``controller.ops.handle_reason`` (str — diagnostic)

The state-transition methods, predicates (``is_stopped``, ``active_conditions``, ``power_on``), and
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

    # ------------------------------------------------------------------
    # State transitions into / out of RUNNING_MOVING
    # ------------------------------------------------------------------

    def enter_running_moving(self) -> None:
        """Move the operational state from READY to RUNNING_MOVING (the
        "depart accepted / motion observed" state), leaving a controller that
        is already RUNNING_MOVING unchanged.

        This is the single state-writer for entering RUNNING_MOVING. Every
        path that enters that state goes through it. It writes only ``state``;
        ATO intent (``ato.engaged``) is a separate flag owned by the mode
        selector (design_new.md "Controller state machine" and "ato.engaged")
        and is not touched here.

        Idempotent from RUNNING_MOVING (setting the same value is a no-op). Its
        callers only reach it from READY or RUNNING_MOVING, per the Controller
        state machine.
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

        Transition 1 — READY to RUNNING_MOVING: the controller is READY and the
        train is moving. Motion out of a READY controller is a real departure (a
        manual driver pre-rolling, or ATC clearance returning and physics
        resuming before the next plan tick). There is no cab guard — the head
        end is derived from the authority (design_new.md "Authority-side head
        derivation"). This writes only ``state``; ATO intent (``ato.engaged``)
        is set separately by the mode selector and is not touched here.

        Transition 2 — RUNNING_MOVING to READY: the train has stopped, has no
        live authority (``received_path_plan is None``), and EB is not
        latched. This is braking completion with nothing left to move into.
        While live authority still exists the controller stays RUNNING_MOVING
        even at a stand (clearance may return and motion resume on its own),
        and while EB is latched this transition does not fire —
        ``TrainController.eb.release()`` clears that latch and the post-command
        call here then performs the move. This writes only ``state``; ATO
        intent is unaffected by movement or authority loss (design_new.md
        "ato.engaged"). A dwelling train is READY, held there by the planner
        withholding onward authority.
        """
        c = self._c
        if self.state == TrainState.READY and not c.is_stopped():
            self.enter_running_moving()
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
