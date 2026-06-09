"""Advanced settings routes — read and update runtime-tunable operational
configuration (see `runtime_config.RuntimeConfig`)."""

from flask import jsonify, request

from app_state import AppState
import runtime_config


def register(app, state: AppState) -> None:
    """Register the Advanced settings GET/PUT routes."""

    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        return jsonify({"settings": runtime_config.as_dict()})

    @app.route("/api/settings", methods=["PUT"])
    def update_settings():
        data = request.get_json(silent=True) or {}
        # Accept either {"settings": {...}} or a flat field->value dict.
        updates = data.get("settings", data) if isinstance(data, dict) else {}
        with state.simulation_lock:
            runtime_config.apply_updates(updates)
        return jsonify(
            {"status": "success", "settings": runtime_config.as_dict()}
        )
