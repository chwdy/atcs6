"""Hardware configuration routes — read and update the hardware backend
selection (design_new.md "Power -> Hardware configuration").

`GET` / `PUT /api/hardware/config` is the only entry point that reads or writes
the `HardwareConfig` store. A `PUT` validates the merged configuration and, on
success, writes it to the app-managed config file; the change takes effect on
the next session build (a server restart), not the running session.
"""

from flask import jsonify, request

from app_state import AppState
import hardware_config


def register(app, state: AppState) -> None:
    """Register the hardware-configuration GET/PUT routes."""

    @app.route("/api/hardware/config", methods=["GET"])
    def get_hardware_configuration():
        return jsonify({"config": hardware_config.as_dict()})

    @app.route("/api/hardware/config", methods=["PUT"])
    def update_hardware_configuration():
        data = request.get_json(silent=True) or {}
        # Accept either {"config": {...}} or a flat field->value dict.
        updates = data.get("config", data) if isinstance(data, dict) else {}
        try:
            with state.simulation_lock:
                hardware_config.apply_updates(updates)
        except hardware_config.HardwareConfigError as exc:
            return (
                jsonify({"status": "error", "error": str(exc)}),
                400,
            )
        return jsonify({"status": "success", "config": hardware_config.as_dict()})
