"""The operator status read model: `ato_status`, `pending_actions`, `hold_reason`.

design_new.md "Data Flow To UI": `operator_status_by_train_id` maps each train
to `{ato_status, pending_actions, hold_reason}` — the three projections that
need both controller state and route state. They are assembled on every read
and stored nowhere, and no control logic reads them.

- **Pending actions** (canonical home in the doc): one entry per Action latch
  set on the train's `phase_latches`, plus one while a route assignment is held
  for the train, in the order `emergency_request`, `power_off`,
  `route_deactivate`, `route_abort`, the held assignment, `path_cancel_request`.
  Each entry is `{latch, label}`; the labels are the Latch Table's
  "Operator sees (pending action)" column.
- **Hold reason** (canonical home in the doc): the first row of the doc's
  17-row table whose condition holds, as `{code, question, detail}`. It is
  `None` while the session is Live and the train moves, and when no row holds.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from enums import AtcLimitReasonCode, HandlePosition, SessionStateValue, TrainStatus
from session_state import SessionState

# The Latch Table's "Operator sees (pending action)" column, in the order the
# doc lists pending actions. The held route assignment sits between
# `route_abort` and `path_cancel_request`.
_LATCH_LABELS_BEFORE_HELD = (
    ("emergency_request", "emergency stop requested"),
    ("power_off", "stopping to power off"),
    ("route_deactivate", "stopping to end its route run"),
    ("route_abort", "stopping to leave its route"),
)
_LATCH_LABELS_AFTER_HELD = (("path_cancel_request", "stopping to cancel its path"),)


def pending_actions_for(controller, route_executor, train_id: str) -> List[Dict[str, Any]]:
    """What the train has accepted but can only finish later (design_new.md
    "Pending actions")."""
    latches = controller.route_hooks.phase_latches
    entries: List[Dict[str, Any]] = []
    for name, label in _LATCH_LABELS_BEFORE_HELD:
        if getattr(latches, name, None):
            entries.append({"latch": name, "label": label})
    held = (
        route_executor.pending_route_assignment_for(train_id)
        if route_executor is not None
        else None
    )
    if held is not None:
        requested = held.get("requested_route_id")
        label = (
            f"switching to route {requested} once stopped"
            if requested is not None
            else "leaving its route once stopped"
        )
        entries.append({"latch": None, "label": label})
    for name, label in _LATCH_LABELS_AFTER_HELD:
        if getattr(latches, name, None):
            entries.append({"latch": name, "label": label})
    return entries


def _hold(code: str, question: int, detail: Optional[str] = None) -> Dict[str, Any]:
    return {"code": code, "question": question, "detail": detail}


def _latest_error_origin(sim_controller, train_id: str) -> Optional[str]:
    nc = getattr(sim_controller, "notification_center", None)
    if nc is None:
        return None
    found = nc.latest_error_for("train", train_id)
    error = getattr(found, "error", None) if found is not None else None
    return getattr(error, "origin", None) if error is not None else None


def _driver_idle(controller) -> bool:
    """The driver's input commands no traction (design_new.md "Hold reason",
    row `driver_idle`)."""
    if controller.manual.manual_mode_type == "power_percent":
        return float(controller.manual.manual_power_percent or 0) == 0.0
    handle = controller.ops.handle_position
    return handle is HandlePosition.N or handle.value.startswith("B") or handle is HandlePosition.EB


def hold_reason_for(
    sim_controller,
    train,
    controller,
    pending_actions: List[Dict[str, Any]],
    *,
    session_state: SessionState,
) -> Optional[Dict[str, Any]]:
    """Why a train is not moving: the first row of design_new.md "Hold reason"
    whose condition holds, or None.

    The rows follow the four questions in order — is the layout live, who is
    driving and is it asking to move, how far may it go, how fast may it go —
    with `driver_idle` last. Question 1 is answered by the session state; it
    holds whenever the session is not Live, even for a train a halt froze with
    speed still stored, because a stopped session moves no train.
    """
    # Question 1: is the layout live?
    if session_state.value is SessionStateValue.HALTED:
        return _hold("halted", 1, session_state.halt_reason.value)
    if session_state.value is SessionStateValue.SETUP:
        return _hold("setup", 1)
    if session_state.value is SessionStateValue.PAUSED:
        return _hold("paused", 1)
    if not controller.is_stopped():
        return None
    train_id = train.train_id
    route_executor = getattr(sim_controller, "route_executor", None)

    # Question 2: who is driving this train, and is it asking to move?
    if getattr(train, "is_unidentified", False):
        return _hold("unidentified", 2)
    if getattr(controller, "presence_unknown", False):
        return _hold("detection_lost", 2)
    status = controller.ops.train_status
    if status == TrainStatus.OFF:
        return _hold("powered_off", 2)
    if status == TrainStatus.EB:
        if controller.eb.cut_out:
            return _hold("emergency_cut_out", 2)
        return _hold("emergency_brake", 2)
    assigned_route_id = (
        route_executor.assigned_route_id_for(train_id) if route_executor is not None else None
    )
    if status == TrainStatus.CALIBRATING:
        calibration = getattr(sim_controller, "speed_calibration", None)
        run = calibration.runs.get(train_id) if calibration is not None else None
        measured_block = run.measured_block_id if run is not None else ""
        return _hold("calibrating", 2, measured_block)
    if (
        status == TrainStatus.ATO
        and not controller.commands.engaged
        and assigned_route_id is not None
    ):
        return _hold("ato_not_engaged", 2)

    # Question 3: how far may it go?
    if pending_actions:
        return _hold("pending_action", 3, pending_actions[0]["label"])
    if assigned_route_id is None:
        return _hold("no_route", 3)
    if route_executor is not None and route_executor.run_is_dwelling(train_id):
        return _hold("dwelling", 3)
    if controller.path_projection.received_path_plan is None:
        return _hold("no_reservation", 3, _latest_error_origin(sim_controller, train_id))
    limit, code = controller.atc_limit_at_distance_zero()
    if limit <= 0.0 and code == AtcLimitReasonCode.RESERVATION_STOP:
        detail = _latest_error_origin(sim_controller, train_id)
        if detail is None and route_executor is not None:
            detail = route_executor.waiting_for_block_id_for(train_id)
        return _hold("reservation_end", 3, detail)

    # Question 4: how fast may it go right now?
    if limit <= 0.0:
        return _hold("atc_limit", 4, code.value if code is not None else None)

    # The driver, last: idle driver input is often another row's result.
    if status == TrainStatus.MANUAL and _driver_idle(controller):
        return _hold("driver_idle", 2)
    return None


def operator_status_for(
    sim_controller,
    train,
    *,
    session_state: SessionState,
) -> Dict[str, Any]:
    """One train's `{ato_status, pending_actions, hold_reason}`."""
    controller = sim_controller._get_or_create_controller(train)
    route_executor = getattr(sim_controller, "route_executor", None)
    train_id = train.train_id
    ato_status = (
        route_executor.ato_status_for(train_id) if route_executor is not None else None
    )
    pending = pending_actions_for(controller, route_executor, train_id)
    hold = hold_reason_for(
        sim_controller,
        train,
        controller,
        pending,
        session_state=session_state,
    )
    return {"ato_status": ato_status, "pending_actions": pending, "hold_reason": hold}
