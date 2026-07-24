"""Hardware configuration routes (design_new.md "Power -> Hardware configuration").

- `GET/PUT /api/hardware/config` reads / writes the `HardwareConfig` store (chip
  inventory + port bindings). A `PUT` validates the merged configuration and, on
  success, writes it to the app-managed config file; the change takes effect on
  the next session build (a restart), not the running session.
- `GET /api/hardware/catalog` returns the selectable controller / chip metadata
  the Hardware Setup dropdowns read.
- `POST /api/hardware/scan` opens the controller and reports the I2C addresses
  answering right now (Detect / Rescan). Shift registers cannot be detected, so
  they are proven by the interactive port test instead.
- `POST /api/hardware/port-test` pulses one output line or reads one input line,
  so a single wire can be confirmed before binding (the port picker's per-channel
  double-click).
- `POST /api/hardware/track-test` sweeps a block's two bound track channels
  through both polarities so a loco runs, stops, and reverses.
- `POST /api/hardware/occupancy-test` reads both of one block's occupancy
  detectors and reports which one decides and what it says.
- `POST /api/hardware/commission/pulse` drives one shift-out bit for the
  switch-coil pulse width on the held commissioning session, so the switch
  editor's throw test energizes a coil for exactly as long as the runtime does.

Scan / port-test open a transient device at commissioning time (sim mode,
device free); if a real session already holds the CH347 they report it as
unreachable rather than fighting for the handle, so they run outside the
simulation lock.
"""

from flask import jsonify, request

from app_state import AppState
import hardware_config


def _develop_opener(cfg):
    """When the persisted configuration has the develop flag set, an
    `open_device` callable that creates a transient seeded `FakeCh347Device`;
    None means "open the real CH347". Gated on the persisted `develop` flag
    alone — NOT `is_develop`, because commissioning (Detect / port test) runs
    on the fake BEFORE the user can enable hardware (enabling requires every
    chip proven), so it must not also require `enabled`. A PUT commits the flag
    live, so the fake is used right after the first Save, no restart. Seeded
    from the payload `cfg` under test (the edit-in-progress chip list is what
    the scan is compared against)."""
    if not hardware_config.get_hardware_config().develop:
        return None
    from hardware.fake import FakeCh347Device

    def _open():
        dev = FakeCh347Device(
            present_addresses={
                c.i2c_address for c in cfg.chips if c.i2c_address is not None
            }
        )
        dev.open()
        return dev

    return _open


def register(app, state: AppState) -> None:
    """Register the hardware-configuration routes."""

    @app.route("/api/hardware/config", methods=["GET"])
    def get_hardware_configuration():
        return jsonify({
            "config": hardware_config.as_dict(),
            # What the RUNNING session actually is (the config above only says
            # what the next session will be): the operation mode the session
            # booted with (honest about a sim fallback) and whether it runs on
            # the fake device (the develop badge + dev section key off this).
            "session": {
                "operation_mode": state.sim_controller.operation_mode.value,
                "develop_active": hardware_config.get_session_device() is not None,
            },
        })

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

    @app.route("/api/hardware/scan", methods=["POST"])
    def hardware_scan():
        """Open the controller and report the I2C addresses answering right now
        (the Hardware Setup Detect / Rescan button)."""
        data = request.get_json(silent=True) or {}
        payload = data.get("config", data) if isinstance(data, dict) else {}
        try:
            cfg = hardware_config.config_from_payload(payload)
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        from hardware import self_test

        return jsonify(self_test.run_bus_scan(cfg, open_device=_develop_opener(cfg)))

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
        actions = ("pulse_output", "read_input")
        if not chip_id or port is None or action not in actions:
            return (
                jsonify({
                    "status": "error",
                    "error": f"chip_id, port, and action ({'|'.join(actions)}) are required",
                }),
                400,
            )
        from hardware import self_test

        return jsonify(
            self_test.run_port_test(
                cfg, chip_id, int(port), action, open_device=_develop_opener(cfg)
            )
        )

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

        # In develop mode prefer the live session's fake device, so the verdict
        # reflects state injected via /api/debug/hw (reads only, never drives;
        # a passed-in device is never closed by run_occupancy_test).
        return jsonify(
            self_test.run_occupancy_test(
                cfg,
                str(block_id),
                device=hardware_config.get_session_device(),
                open_device=_develop_opener(cfg),
            )
        )

    @app.route("/api/hardware/track-test", methods=["POST"])
    def hardware_track_test():
        """Sweep a block's track through both polarities (the block editor's
        track Test button). Body: {config, block_id}. Drives the block's two
        bound PWM channels forward / off / reverse so a loco runs, stops, and
        reverses, then leaves the track de-energized. Runs on a transient device
        (commissioning), so it never fights a live session."""
        data = request.get_json(silent=True) or {}
        try:
            cfg = hardware_config.config_from_payload(data.get("config", {}))
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        block_id = data.get("block_id")
        if not block_id:
            return jsonify({"status": "error", "error": "block_id is required"}), 400
        from hardware import self_test

        return jsonify(
            self_test.run_track_test(cfg, str(block_id), open_device=_develop_opener(cfg))
        )

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
                if map_name == "block_track":
                    # A block's track binds two PWM channels (forward + reverse),
                    # chosen independently (design_new.md Power, Hardware configuration).
                    hardware_config.set_track_binding(
                        str(logical_id),
                        data.get("chip_id"),
                        data.get("forward"),
                        data.get("reverse"),
                    )
                elif map_name == "switch_output":
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
            commission.get_session().open(cfg, open_device=_develop_opener(cfg))
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

    # -- develop-mode fake-input debug surface ------------------------------
    # These act on the LIVE session's FakeCh347Device (develop mode only): the
    # dev panel injects occupancy bits / currents here and the real occupancy
    # pipeline reads them back on its next tick.

    def _require_fake():
        """The live session's fake device, or None (caller answers 409)."""
        return hardware_config.get_session_device()

    def _chip_spec(cfg, chip_id):
        return next((c for c in cfg.chips if c.chip_id == chip_id), None)

    _NOT_DEVELOP = {"status": "error", "error": "session is not running in develop mode"}

    @app.route("/api/debug/hw", methods=["GET"])
    def debug_hw_state():
        """Read-back of the fake device's per-block / per-switch state for the
        dev panel poll. Fields are null where the entity has no binding of that
        kind."""
        device = _require_fake()
        if device is None:
            return jsonify(_NOT_DEVELOP), 409
        from block_power import MAX_TRACK_VOLTAGE
        from hardware.interfaces import build_chip

        cfg = hardware_config.get_hardware_config()
        with state.simulation_lock:
            chips = {}

            def chip_for(chip_id):
                if chip_id not in chips:
                    spec = _chip_spec(cfg, chip_id)
                    try:
                        chips[chip_id] = build_chip(device, spec) if spec else None
                    except Exception:
                        chips[chip_id] = None
                return chips[chip_id]

            try:
                occupied = state.sim_controller.input_interface.read_block_currents()
            except Exception:
                occupied = {}

            blocks = {}
            for block_id in (
                set(cfg.block_occupancy)
                | set(cfg.block_occupancy_current)
                | set(cfg.block_track)
            ):
                entry = {"occupancy_bit": None, "current_ma": None, "volts": None,
                         "occupied": occupied.get(block_id)}
                binding = cfg.block_occupancy.get(block_id)
                if binding is not None:
                    chip = chip_for(binding.chip_id)
                    if chip is not None:
                        entry["occupancy_bit"] = bool(chip.read_bit(binding.port))
                cbind = cfg.block_occupancy_current.get(block_id)
                if cbind is not None:
                    chip = chip_for(cbind.chip_id)
                    if chip is not None:
                        try:
                            entry["current_ma"] = round(
                                chip.read_current(cbind.port + 1) * 1000.0, 3
                            )
                        except Exception:
                            pass
                tbind = cfg.block_track.get(block_id)
                if tbind is not None:
                    chip = chip_for(tbind.chip_id)
                    if chip is not None:
                        fwd = device.duty_cycle(chip.address, tbind.forward)
                        rev = device.duty_cycle(chip.address, tbind.reverse)
                        entry["volts"] = round((fwd - rev) * MAX_TRACK_VOLTAGE, 2)
                blocks[block_id] = entry

            switches = {}
            for switch_id, sbind in cfg.switch_output.items():
                chip = chip_for(sbind.chip_id)
                if chip is None:
                    continue
                switches[switch_id] = {
                    "main": device.latched_bit(chip.select, sbind.port_main),
                    "diverge": device.latched_bit(chip.select, sbind.port_diverge),
                    "standby": (
                        device.latched_bit(chip.select, sbind.port_standby)
                        if sbind.port_standby is not None else None
                    ),
                }

        return jsonify({"develop_active": True, "blocks": blocks, "switches": switches})

    @app.route("/api/debug/hw/occupancy", methods=["POST"])
    def debug_hw_set_occupancy():
        """Set a block's fake '165 occupancy bit. Body: {block_id, occupied}."""
        device = _require_fake()
        if device is None:
            return jsonify(_NOT_DEVELOP), 409
        data = request.get_json(silent=True) or {}
        block_id = data.get("block_id")
        occupied = data.get("occupied")
        if not block_id or occupied is None:
            return jsonify({"status": "error", "error": "block_id and occupied are required"}), 400
        from hardware.interfaces import build_chip

        cfg = hardware_config.get_hardware_config()
        binding = cfg.block_occupancy.get(str(block_id))
        spec = _chip_spec(cfg, binding.chip_id) if binding else None
        if binding is None or spec is None:
            return jsonify({
                "status": "error",
                "error": f"block {block_id!r} has no occupancy detector binding",
            }), 404
        with state.simulation_lock:
            # Resolve the chip's latch line the same way the driver does, so the
            # bit lands where the runtime's read will look.
            chip = build_chip(device, spec)
            device.set_input_bit(
                chip.select, binding.port, bool(occupied), width=spec.chain_length
            )
        return jsonify({"ok": True, "block_id": block_id, "occupied": bool(occupied)})

    @app.route("/api/debug/hw/current", methods=["POST"])
    def debug_hw_set_current():
        """Set a block's fake current-sense reading in mA. Body: {block_id,
        current_ma}. Current decides occupancy only while the block is driven
        (see `OccupancyDriver`); an idle block reads its '165 bit."""
        device = _require_fake()
        if device is None:
            return jsonify(_NOT_DEVELOP), 409
        data = request.get_json(silent=True) or {}
        block_id = data.get("block_id")
        current_ma = data.get("current_ma")
        if not block_id or current_ma is None:
            return jsonify({"status": "error", "error": "block_id and current_ma are required"}), 400
        cfg = hardware_config.get_hardware_config()
        binding = cfg.block_occupancy_current.get(str(block_id))
        spec = _chip_spec(cfg, binding.chip_id) if binding else None
        if binding is None or spec is None:
            return jsonify({
                "status": "error",
                "error": f"block {block_id!r} has no current-sense binding",
            }), 404
        from hardware.interfaces import build_chip

        amps = float(current_ma) / 1000.0
        with state.simulation_lock:
            # Resolve the address through the driver so a spec without an
            # explicit i2c_address lands on the catalog default, the same
            # address the runtime reads.
            addr = build_chip(device, spec).address
            if spec.kind == "ina219":
                device.set_ina219_current(addr, amps)
            else:
                device.set_ina3221_current(addr, binding.port + 1, amps)
        return jsonify({"ok": True, "block_id": block_id, "current_ma": float(current_ma)})
