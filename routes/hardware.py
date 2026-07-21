"""Hardware configuration routes (design_new.md "Power -> Hardware configuration").

- `GET/PUT /api/hardware/config` reads / writes the `HardwareConfig` store (chip
  inventory + port bindings). A `PUT` validates the merged configuration and, on
  success, writes it to the app-managed config file; the change takes effect on
  the next session build (a restart), not the running session.
- `GET /api/hardware/catalog` returns the selectable controller / chip metadata
  the Hardware Setup dropdowns read.
- `POST /api/hardware/self-test` probes the controller + each configured chip so
  every reachable port can join the bindable pool.
- `POST /api/hardware/port-test` pulses one output port, sweeps a track port
  through both polarities, or reads one input port, so a single wire can be
  confirmed before binding.
- `POST /api/hardware/occupancy-test` reads both of one block's occupancy
  detectors and reports which one decides and what it says.
- `POST /api/hardware/commission/pulse` drives one shift-out bit for the
  switch-coil pulse width on the held commissioning session, so the switch
  editor's throw test energizes a coil for exactly as long as the runtime does.

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

        # Optional: limit the probe to one chip (the per-chip Self-test button).
        return jsonify(self_test.run_self_test(cfg, chip_id=data.get("chip_id")))

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
        actions = ("pulse_output", "read_input", "drive_sequence")
        if not chip_id or port is None or action not in actions:
            return (
                jsonify({
                    "status": "error",
                    "error": f"chip_id, port, and action ({'|'.join(actions)}) are required",
                }),
                400,
            )
        from hardware import self_test

        return jsonify(self_test.run_port_test(cfg, chip_id, int(port), action))

    @app.route("/api/hardware/occupancy-test", methods=["POST"])
    def hardware_occupancy_test():
        """Read both of one block's occupancy detectors and report the verdict.

        Body: {config, block_id}. Reads the '165 bit and the current-sense
        channel, reads the block's drive state back off the PWM chip, and applies
        the runtime's rule for which source decides. Drives nothing."""
        data = request.get_json(silent=True) or {}
        try:
            cfg = hardware_config.config_from_payload(data.get("config", {}))
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        block_id = data.get("block_id")
        if not block_id:
            return jsonify({"status": "error", "error": "block_id is required"}), 400
        from hardware import self_test

        return jsonify(self_test.run_occupancy_test(cfg, str(block_id)))

    @app.route("/api/hardware/binding", methods=["PUT"])
    def update_hardware_binding():
        """Set or clear one port binding (from the per-entity block/switch
        editors). Body: {map, logical_id, chip_id?, port?} — chip_id absent
        clears it. The `switch_output` and `block_occupancy_current` maps carry
        more than one field per entry, so each has its own setter."""
        data = request.get_json(silent=True) or {}
        map_name = data.get("map")
        logical_id = data.get("logical_id")
        if not map_name or not logical_id:
            return jsonify({"status": "error", "error": "map and logical_id are required"}), 400
        try:
            with state.simulation_lock:
                if map_name == "switch_output":
                    # A switch binds two coil ports plus an optional standby bit
                    # (design_new.md Power, Hardware configuration).
                    hardware_config.set_switch_binding(
                        str(logical_id),
                        data.get("chip_id"),
                        data.get("port_main"),
                        data.get("port_diverge"),
                        port_standby=data.get("port_standby"),
                    )
                elif map_name == "block_occupancy_current":
                    # The current map carries per-channel calibration, so it has
                    # its own setter (design_new.md Power, Block occupancy detection).
                    hardware_config.set_current_binding(
                        str(logical_id),
                        data.get("chip_id"),
                        data.get("port"),
                        baseline_ma=data.get("baseline_ma"),
                        threshold_ma=data.get("threshold_ma"),
                    )
                else:
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

    @app.route("/api/hardware/commission/pulse", methods=["POST"])
    def hardware_commission_pulse():
        """Drive one shift-out bit for the switch-coil pulse width, optionally
        raising a driver standby bit for the pulse and holding the motor
        driver's other input low. Body: {chip_id, port, standby_port?,
        off_port?}."""
        data = request.get_json(silent=True) or {}
        from hardware import commission

        standby = data.get("standby_port")
        off_port = data.get("off_port")
        try:
            commission.get_session().pulse_output(
                data.get("chip_id"),
                int(data.get("port")),
                standby_port=None if standby is None else int(standby),
                off_port=None if off_port is None else int(off_port),
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

    @app.route("/api/hardware/commission/currents", methods=["POST"])
    def hardware_commission_currents():
        data = request.get_json(silent=True) or {}
        from hardware import commission

        try:
            currents = commission.get_session().read_currents(data.get("chip_id"))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({
            "ok": True,
            "currents": [{"port": i, "current_ma": v} for i, v in enumerate(currents)],
        })

    @app.route("/api/hardware/commission/close", methods=["POST"])
    def hardware_commission_close():
        from hardware import commission

        commission.get_session().close()
        return jsonify({"ok": True})
