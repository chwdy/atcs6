"""Requests about v2 trains (design_v2.md "Running a train", "Trains").

Each handler holds `AppState.simulation_lock`, so it runs between two runtime
steps, and answers `{ success, reason }`. A refused request answers 400 with
the reason and changes nothing.

`stop` and remove-a-train share their addresses with the v1 handlers in
`routes/trains.py`; those call `stop_running_train` and `remove_running_train`
here first and fall through to v1 only for a train the v2 runtime does not
hold.
"""

import time

from flask import jsonify, request

from app_state import AppState
from runtime import runs_train_step


def _answer(result):
    success, reason = result
    return jsonify({"success": success, "reason": reason}), (200 if success else 400)


def _runtime(state: AppState):
    """The train runtime when this session runs the v2 runtime step, else
    None."""
    controller = getattr(state, "sim_controller", None)
    if controller is None or not runs_train_step(controller):
        return None
    return controller.train_runtime


def holds_train(state: AppState, train_id: str) -> bool:
    runtime = _runtime(state)
    return runtime is not None and train_id in runtime.trains


def stop_running_train(state: AppState, train_id: str):
    """`POST /api/trains/<train_id>/stop` for a v2 train."""
    with state.simulation_lock:
        return _answer(_runtime(state).stop(train_id, time.time()))


def remove_running_train(state: AppState, train_id: str):
    """`DELETE /api/trains/<train_id>` for a v2 train."""
    with state.simulation_lock:
        return _answer(_runtime(state).remove(train_id))


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

    @app.route("/api/trains/stop-all", methods=["POST"])
    def stop_all_trains():
        runtime = _runtime(state)
        if runtime is None:
            return jsonify({"success": True, "reason": None})
        with state.simulation_lock:
            live = state.session_state().is_live
            return _answer(runtime.stop_all(time.time(), live))
