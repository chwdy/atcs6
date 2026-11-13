"""Requests about v2 trains (design_v2.md "Running a train", "Trains").

Each handler holds `AppState.simulation_lock`, so it runs between two runtime
steps, and answers `{ success, reason }`. A refused request answers 400 with
the reason and changes nothing.

A request that names a train the runtime does not hold is refused with
`unknown_train`.
"""

import time

from flask import jsonify, request

from app_state import AppState


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

    def _refuse_without_runtime():
        return jsonify({"success": False, "reason": "unknown_train"}), 400

    @app.route("/api/trains/<train_id>/knob", methods=["PUT"])
    def set_train_knob(train_id):
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime()
        data = request.get_json(silent=True) or {}
        with state.simulation_lock:
            live = state.session_state().is_live
            return _answer(runtime.set_knob(train_id, data.get("volts"), time.time(), live))

    @app.route("/api/trains/<train_id>/reverse", methods=["POST"])
    def reverse_train(train_id):
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime()
        with state.simulation_lock:
            live = state.session_state().is_live
            return _answer(runtime.reverse(train_id, live))

    @app.route("/api/trains/<train_id>/stop", methods=["POST"])
    def stop_train(train_id):
        """Set the knob to 0 V; always accepted for a train the runtime holds
        (design_v2.md "Running a train", Stop)."""
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime()
        with state.simulation_lock:
            return _answer(runtime.stop(train_id, time.time()))

    @app.route("/api/trains/<train_id>", methods=["DELETE"])
    def remove_train(train_id):
        """Remove a train (design_v2.md "Trains", Removing a train)."""
        runtime = _runtime(state)
        if runtime is None:
            return _refuse_without_runtime()
        with state.simulation_lock:
            return _answer(runtime.remove(train_id))

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
                return jsonify({"success": True, "reason": None})
            return _answer(runtime.stop_all(time.time(), live))
