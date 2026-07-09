"""Hardware configuration routes (design_new.md "Power -> Hardware configuration").

- `GET/PUT /api/hardware/config` reads / writes the `HardwareConfig` store (chip
  inventory + port bindings). A `PUT` validates the merged configuration and, on
  success, writes it to the app-managed config file; the change takes effect on
  the next session build (a restart), not the running session.
- `GET /api/hardware/catalog` returns the selectable controller / chip metadata
  the Hardware Setup dropdowns read.
- `POST /api/hardware/self-test` probes the controller + each configured chip so
  every reachable port can join the bindable pool.
- `POST /api/hardware/port-test` pulses one output port or reads one input port
  so a single wire can be confirmed before binding.

Self-test / port-test open a transient device at commissioning time (sim mode,
device free); if a real session already holds the CH347 they report it as
unreachable rather than fighting for the handle, so they run outside the
simulation lock.
"""

from flask import jsonify, request

from app_state import AppState
import hardware_config


def register(app, state: AppState) -> None:
    """Register the hardware-configuration routes."""

    @app.route("/api/hardware/config", methods=["GET"])
    def get_hardware_configuration():
        return jsonify({"config": hardware_config.as_dict()})

    @app.route("/api/hardware/config", methods=["PUT"])
    def update_hardware_configuration():
        data = request.get_json(silent=True) or {}
        # Accept either {"config": {...}} or a flat section->value dict.
        updates = data.get("config", data) if isinstance(data, dict) else {}
        try:
            with state.simulation_lock:
                hardware_config.apply_updates(updates)
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        return jsonify({"status": "success", "config": hardware_config.as_dict()})

    @app.route("/api/hardware/catalog", methods=["GET"])
    def get_hardware_catalog():
        from hardware.catalog import catalog_payload

        return jsonify(catalog_payload())

    @app.route("/api/hardware/self-test", methods=["POST"])
    def hardware_self_test():
        data = request.get_json(silent=True) or {}
        payload = data.get("config", data) if isinstance(data, dict) else {}
        try:
            cfg = hardware_config.config_from_payload(payload)
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        from hardware import self_test

        return jsonify(self_test.run_self_test(cfg))

    @app.route("/api/hardware/port-test", methods=["POST"])
    def hardware_port_test():
        data = request.get_json(silent=True) or {}
        payload = data.get("config", {}) if isinstance(data, dict) else {}
        try:
            cfg = hardware_config.config_from_payload(payload)
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        chip_id = data.get("chip_id")
        port = data.get("port")
        action = data.get("action")
        if not chip_id or port is None or action not in ("pulse_output", "read_input"):
            return (
                jsonify({
                    "status": "error",
                    "error": "chip_id, port, and action (pulse_output|read_input) are required",
                }),
                400,
            )
        from hardware import self_test

        return jsonify(self_test.run_port_test(cfg, chip_id, int(port), action))

    @app.route("/api/hardware/binding", methods=["PUT"])
    def update_hardware_binding():
        """Set or clear one port binding (from the per-entity block/switch
        editors). Body: {map, logical_id, chip_id?, port?} — chip_id absent
        clears it."""
        data = request.get_json(silent=True) or {}
        map_name = data.get("map")
        logical_id = data.get("logical_id")
        if not map_name or not logical_id:
            return jsonify({"status": "error", "error": "map and logical_id are required"}), 400
        try:
            with state.simulation_lock:
                hardware_config.set_binding(
                    map_name, str(logical_id), data.get("chip_id"), data.get("port")
                )
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        return jsonify({"status": "success", "config": hardware_config.as_dict()})

    @app.route("/api/hardware/commission/open", methods=["POST"])
    def hardware_commission_open():
        data = request.get_json(silent=True) or {}
        try:
            cfg = hardware_config.config_from_payload(data.get("config", {}))
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        from hardware import commission

        try:
            commission.get_session().open(cfg)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True})

    @app.route("/api/hardware/commission/output", methods=["POST"])
    def hardware_commission_output():
        data = request.get_json(silent=True) or {}
        from hardware import commission

        try:
            commission.get_session().set_output(
                data.get("chip_id"), int(data.get("port")), float(data.get("duty", 1.0))
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True})

    @app.route("/api/hardware/commission/inputs", methods=["POST"])
    def hardware_commission_inputs():
        data = request.get_json(silent=True) or {}
        from hardware import commission

        try:
            bits = commission.get_session().read_inputs(data.get("chip_id"))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True, "ports": [{"port": i, "value": v} for i, v in enumerate(bits)]})

    @app.route("/api/hardware/commission/close", methods=["POST"])
    def hardware_commission_close():
        from hardware import commission

        commission.get_session().close()
        return jsonify({"ok": True})
