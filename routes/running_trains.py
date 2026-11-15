"""Requests about v2 trains (design_v2.md "Running a train", "Trains").

Each handler holds `AppState.simulation_lock`, so it runs between two runtime
steps, and answers `{ success, reason }`. A refused request answers 400 with
the reason and changes nothing.

A request that names a train the runtime does not hold is refused with
`unknown_train`.

Each request also emits one command-result event, so the event log holds what
the operator asked for beside what the runtime did (design_v2.md "Event log",
Operator requests about trains report a command result).
"""

import time

from flask import jsonify, request

from app_state import AppState
from services.notifications import emit_command_result


def _answer(result):
    success, reason = result
    return jsonify({"success": success, "reason": reason}), (200 if success else 400)


def _runtime(state: AppState):
    """The session's train runtime, or None when no runtime is built."""
    controller = getattr(state, "sim_controller", None)
    if controller is None:
        return None
    return getattr(controller, "train_runtime", None)


def register(app, state: AppState) -> None:
    """Register the v2 train routes."""

    def _report(operation, result, train_id=None, asked=""):
        """Emit the request's command-result event and answer the request.
        `asked` says what was asked for, for example "knob to 4.5 V"."""
        success, reason = result
        runtime = _runtime(state)
        train = runtime.trains.get(train_id) if runtime and train_id else None
        who = train.name if train is not None else (train_id or "every train")
        what = asked or operation
        emit_command_result(
            state,
            source="SimulationController",
            command_name=operation,
            success=success,
            entity_id_key="train_id" if train_id else "",
            entity_id=train_id or "",
            reason=reason or "",
            message=(
                f"{who}: {what}." if success else f"{who}: {what} refused ({reason})."
            ),
        )
        return _answer(result)

    def _refuse_without_runtime(operation, train_id):
        return _report(operation, (False, "unknown_train"), train_id)

    @app.route("/api/trains/<train_id>/knob", methods=["PUT"])
    def set_train_knob(train_id):
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime("set_knob", train_id)
        data = request.get_json(silent=True) or {}
        with state.simulation_lock:
            live = state.session_state().is_live
            return _report(
                "set_knob",
                runtime.set_knob(train_id, data.get("volts"), time.time(), live),
                train_id,
                asked=f"knob to {data.get('volts')} V",
            )

    @app.route("/api/trains/<train_id>/reverse", methods=["POST"])
    def reverse_train(train_id):
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime("reverse", train_id)
        with state.simulation_lock:
            live = state.session_state().is_live
            return _report("reverse", runtime.reverse(train_id, live), train_id)

    @app.route("/api/trains/<train_id>/assigned-leg", methods=["PUT"])
    def set_train_assigned_leg(train_id):
        """Give the train an assigned leg at a switch, or end it (design_v2.md
        "Running a train", Assign a leg)."""
        runtime = _runtime(state)
        if runtime is None:
            return jsonify({"success": False, "reason": "unknown_train"}), 400
        data = request.get_json(silent=True) or {}
        with state.simulation_lock:
            live = state.session_state().is_live
            return _answer(
                runtime.set_assigned_leg(train_id, data.get("switch_id"), data.get("leg"), live)
            )

    @app.route("/api/trains/<train_id>/stop", methods=["POST"])
    def stop_train(train_id):
        """Set the knob to 0 V; always accepted for a train the runtime holds
        (design_v2.md "Running a train", Stop)."""
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime("stop", train_id)
        with state.simulation_lock:
            return _report("stop", runtime.stop(train_id, time.time()), train_id)

    @app.route("/api/trains/<train_id>", methods=["DELETE"])
    def remove_train(train_id):
        """Remove a train (design_v2.md "Trains", Removing a train)."""
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime("remove_train", train_id)
        with state.simulation_lock:
            # The name is read before the train is gone.
            train = runtime.trains.get(train_id)
            name = train.name if train is not None else train_id
            result = runtime.remove(train_id)
            return _report("remove_train", result, train_id, asked=f"remove {name}")

    @app.route("/api/trains/<train_id>/notifications", methods=["GET"])
    def get_train_notifications(train_id):
        """Events naming this train, most recent first: a filter over the one
        event stream that acknowledges nothing (design_v2.md "Notifications
        and errors", Per-entity reads are filters). Optional `kind` and
        `since_notification_id` narrow it."""
        sim = getattr(state, "sim_controller", None)
        nc = getattr(sim, "notification_center", None) if sim else None
        if nc is None:
            return jsonify({"notifications": []})
        try:
            since = request.args.get("since_notification_id")
            since_id = int(since) if since is not None else None
        except (TypeError, ValueError):
            since_id = None
        events = nc.events_for(
            "train",
            train_id,
            kind=request.args.get("kind"),
            since_notification_id=since_id,
        )
        return jsonify({"notifications": [e.to_dict() for e in events]})

    @app.route("/api/trains/stop-all", methods=["POST"])
    def stop_all_trains():
        """Stop every train at once; always accepted (design_v2.md "Running a
        train", Stop all). While Live it sets every knob and every block to
        0 V; while not Live it writes nothing to the outputs and closes any
        Setup tool that holds them."""
        from hardware.setup_tools import close_setup_tools, report_close_failures

        runtime = _runtime(state)
        with state.simulation_lock:
            live = state.session_state().is_live
            if not live:
                report_close_failures(state.sim_controller, close_setup_tools())
            if runtime is None:
                return _report("stop_all", (True, None))
            return _report("stop_all", runtime.stop_all(time.time(), live))
