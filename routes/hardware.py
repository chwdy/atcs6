"""Hardware configuration routes (design_new.md "Power -> Hardware configuration").

- `GET/PUT /api/hardware/config` reads / writes the `HardwareConfig` store (chip
  inventory + port bindings). A `PUT` validates the merged configuration and, on
  success, writes it to the app-managed config file; the change takes effect on
  the next session build (a restart), not the running session.
- `GET /api/hardware/catalog` returns the selectable controller / chip metadata
  the Hardware Setup dropdowns read.
- `GET /api/hardware/coverage` reports which blocks / switches in the loaded
  layout have no binding, and which bindings name entities the layout no longer
  has — the gap the config validator cannot see, because it checks the bindings
  that exist and says nothing about the ones nobody made.
- `POST /api/hardware/scan` opens the controller and reports the I2C addresses
  answering right now (Detect / Rescan). Shift registers cannot be detected, so
  they are proven by the interactive port test instead.
- `POST /api/hardware/port-test` pulses one output line or reads one input line,
  so a single wire can be confirmed before binding (the port picker's per-channel
  double-click). The pulse is held long enough to read on a meter, and the
  request may name its own width (`pulse_s`).
- `POST /api/hardware/track-test` sweeps a block's two bound track channels
  through both polarities so a loco runs, stops, and reverses. Each leg is
  mirrored onto `Block.current_voltage` while it is held, so the layout shows the
  block energized instead of sitting at 0 V through a test driving real rails.
- `POST /api/hardware/occupancy-test` reads both of one block's occupancy
  detectors and reports which one decides and what it says.
- `POST /api/hardware/switch-test` throws one switch to a named position — the
  switch editor's Test throw. It is a real throw, not a coil poke: the model's
  `Switch.position` follows the direction being tested, so watching the points
  move never leaves the app believing something the layout stopped doing.
- `POST /api/hardware/commission/pulse` drives one shift-out bit for the
  switch-coil pulse width on the held commissioning session (the Hardware Setup
  port grid's output sweep), for exactly as long as the runtime does — unless
  the request widens it with `pulse_s`, to measure rather than watch.

Every one of these tools needs the controller, and the controller is a single
USB endpoint. With no session holding it they open a transient device of their
own. With one — a real rig, or the develop-mode fake — they BORROW the session's
device and run under `simulation_lock`, so the test serializes with the tick
instead of losing a race to open a second handle. That fight used to be reported
as "the controller is unreachable", on a bridge that was plugged in and working.
"""

from contextlib import contextmanager

from flask import jsonify, request

from app_state import AppState
import hardware_config


def _develop_opener(cfg):
    """When `cfg` has the develop flag set, an `open_device` callable that
    creates a transient seeded `FakeCh347Device`; None means "open the real
    CH347".

    Gated on the flag of the config UNDER TEST — the same object the fake is
    seeded from — never on the persisted one. Commissioning (Detect, port test,
    track / occupancy test) always tests the edit in progress, so the toggle
    decides which device answers the moment it is flipped, with no Save and no
    restart. Reading the persisted flag here instead was a real bug: with
    `develop` saved as true, a scan carrying `develop: false` still opened the
    fake, and its addresses came back as a genuine "Connected". Every caller
    supplies the flag — the setup panel puts it on each payload, and `as_dict`
    carries it for the per-entity block / switch editors.

    NOT `is_develop`, because commissioning runs on the fake BEFORE the user can
    enable hardware (enabling requires every chip proven), so it must not also
    require `enabled`."""
    if not cfg.develop:
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

    @contextmanager
    def _bus_guard():
        """Serialize against the tick while a commissioning tool is using the
        running session's controller. A no-op when it opened its own device —
        there is then no tick sharing that bus to collide with."""
        if hardware_config.get_live_device() is None:
            yield
        else:
            with state.simulation_lock:
                yield

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

    @app.route("/api/hardware/coverage", methods=["GET"])
    def get_hardware_coverage():
        """Which blocks and switches in the LOADED layout are wired, and which
        saved bindings name entities the layout no longer has.

        The config PUT validates bindings that exist; it cannot know about the
        ones that were never made. In real mode a block with no occupancy
        binding reads permanently clear and a switch with no coil binding cannot
        be aligned, so half-wired is a state worth being able to see rather than
        discover as a train that stops committing progress. Reported, never
        enforced — wiring a layout up an entity at a time is how it gets wired.
        """
        return jsonify(
            hardware_config.coverage_report(
                hardware_config.get_hardware_config(),
                state.network.blocks.keys(),
                state.network.switches.keys(),
            )
        )

    @app.route("/api/hardware/occupancy/raw", methods=["GET"])
    def get_raw_occupancy():
        """Diagnostic read-back of the last tick's occupancy read, pre-debounce.

        Returns exactly what `BlockOccupancyDetector.step()` saw on its most
        recent read — the raw per-block boolean, the confirmed verdict, and the
        live debounce counters — plus, on the real driver, which source answered
        (digital '165 bit vs INA current) and what it measured. No hardware
        traffic happens here: everything is recorded state, so polling this
        endpoint fast cannot perturb the bus or the tick. `read_count` increments
        once per detector read; a poller dedupes on it.

        Carries the USB link state alongside, because "every block froze at once"
        and "the bridge dropped off the bus" look identical from the block rows
        alone. `link_status()` takes no lock and issues no traffic, so it answers
        even while the bus is stalled mid-reconnect — which is when it matters.
        """
        import time as _time

        with state.simulation_lock:
            snapshot = state.sim_controller.block_occupancy_detector.debug_snapshot()
            detail_fn = getattr(
                state.sim_controller.input_interface, "last_read_detail", None
            )
            if callable(detail_fn):
                for block_id, entry in (detail_fn() or {}).items():
                    snapshot["blocks"].setdefault(block_id, {}).update(entry)
        device = hardware_config.get_live_device()
        link_fn = getattr(device, "link_status", None)
        snapshot["link"] = link_fn() if callable(link_fn) else None
        snapshot["monotonic"] = _time.monotonic()
        return jsonify(snapshot)

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

        device = hardware_config.get_live_device()
        if device is not None:
            with state.simulation_lock:
                return jsonify(self_test.run_bus_scan(cfg, device=device))
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

        # `pulse_s` is optional: absent means the default measurable width.
        device = hardware_config.get_live_device()
        if device is not None:
            with state.simulation_lock:
                return jsonify(
                    self_test.run_port_test(
                        cfg, chip_id, int(port), action,
                        device=device, pulse_s=data.get("pulse_s"),
                    )
                )
        return jsonify(
            self_test.run_port_test(
                cfg,
                chip_id,
                int(port),
                action,
                open_device=_develop_opener(cfg),
                pulse_s=data.get("pulse_s"),
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

        # Borrow the running session's device when there is one — the physical
        # CH347 as readily as the develop-mode fake. A second open would be
        # refused by the driver, which is what used to make this test unusable
        # on exactly the rig it exists to check. Reads only, never drives, and a
        # passed-in device is never closed by run_occupancy_test.
        device = hardware_config.get_live_device()
        if device is not None:
            # Serialize against the tick: same bus, and the detector read must
            # not interleave with one.
            with state.simulation_lock:
                return jsonify(
                    self_test.run_occupancy_test(cfg, str(block_id), device=device)
                )
        return jsonify(
            self_test.run_occupancy_test(
                cfg, str(block_id), open_device=_develop_opener(cfg)
            )
        )

    @app.route("/api/hardware/track-test", methods=["POST"])
    def hardware_track_test():
        """Sweep a block's track through both polarities (the block editor's
        track Test button). Body: {config, block_id}. Drives the block's two
        bound PWM channels forward / off / reverse so a loco runs, stops, and
        reverses, then leaves the track de-energized.

        Borrows the running session's device when there is one, rather than
        opening a second handle the driver would refuse. That sweep drives real
        track, so it holds `simulation_lock` for its ~3 seconds: the runtime
        must not be writing block voltages into the middle of it, and it must
        not be writing over the runtime's. A stalled tick for the length of a
        test the operator deliberately started is the cheaper side of that.

        The sweep is mirrored onto `Block.current_voltage` leg by leg, for the
        same reason the switch throw test writes `Switch.position`: a test that
        energizes real rails while the model still reads 0 V leaves the operator
        watching a layout that disagrees with the track in front of them. It is
        what the block-voltage debug overlay draws from, and the poll runs at
        200 ms against legs held for a second, so the block visibly swings one
        way, drops to zero, and swings the other. The runtime is frozen on the
        lock while that happens and recomputes the block on its next tick."""
        data = request.get_json(silent=True) or {}
        try:
            cfg = hardware_config.config_from_payload(data.get("config", {}))
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        block_id = data.get("block_id")
        if not block_id:
            return jsonify({"status": "error", "error": "block_id is required"}), 400
        from hardware import self_test

        block = state.network.get_block(str(block_id)) if state.network else None

        def _mirror(volts: float) -> None:
            if block is not None:
                block.current_voltage = volts

        mirror = {
            "on_voltage": _mirror,
            # From the same config payload the sweep drives, so bench test and
            # runtime resolve the flag identically (design_new.md "Power", Rail
            # polarity lives on the rail binding).
            "polarity_reversed": (
                bool(cfg.block_track[str(block_id)].reversed)
                if str(block_id) in cfg.block_track
                else False
            ),
        }

        device = hardware_config.get_live_device()
        if device is not None:
            with state.simulation_lock:
                return jsonify(
                    self_test.run_track_test(cfg, str(block_id), device=device, **mirror)
                )
        return jsonify(
            self_test.run_track_test(
                cfg, str(block_id), open_device=_develop_opener(cfg), **mirror
            )
        )

    @app.route("/api/hardware/switch-test", methods=["POST"])
    def hardware_switch_test():
        """Throw one switch to a named position — the switch editor's Test
        throw. Body: {config, switch_id, position} where position is 0 (MAIN)
        or 1 (DIVERGE).

        A test that moves the points and leaves `Switch.position` untouched
        leaves the app believing something about the layout that stopped being
        true while the operator watched. So the test IS a throw: it commands the
        alignment it is testing, through the same `SwitchManager` path an
        operator's throw takes, and the model ends up where the points ended up.

        Which mechanism drives the coil depends on what is wired:

        - A session with a real switch output owns those coils. The command goes
          through `SwitchManager` and the driver pulses them, so the test is
          exactly an operating throw — and no second handle on the controller is
          needed.
        - With no such output (commissioning: hardware configured but not yet
          enabled, or being wired up in sim mode) the coils are pulsed through
          the commissioning session, using this switch's configured binding, and
          the model position is written after the pulse lands. That order
          matters: a pulse that never reached a coil must not leave the model
          claiming an alignment nothing moved to.
        """
        data = request.get_json(silent=True) or {}
        try:
            cfg = hardware_config.config_from_payload(data.get("config", {}))
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        switch_id = data.get("switch_id")
        position = data.get("position")
        if not switch_id or position not in (0, 1):
            return jsonify({
                "ok": False,
                "error": "switch_id and position (0 = MAIN, 1 = DIVERGE) are required",
            }), 400
        switch_id = str(switch_id)
        position = int(position)

        if state.network.switches.get(switch_id) is None:
            return jsonify({"ok": False, "error": f"unknown switch {switch_id!r}"}), 404
        switch_manager = getattr(state.network, "switch_manager", None)
        if switch_manager is None:
            return jsonify({"ok": False, "error": "runtime not initialised"}), 503

        # The session owns the coils when it opened a controller AND wired a
        # switch output onto it. Both halves matter: a real session with no
        # switch chip configured still falls back to the simulated driver, which
        # drives nothing, so the bench pulse is what the operator needs there.
        from switch_power import SimulatedSwitchDriver

        iface = getattr(state.sim_controller, "switch_output_interface", None)
        drives_coils = (
            hardware_config.get_live_device() is not None
            and iface is not None
            and not isinstance(iface, SimulatedSwitchDriver)
        )

        if not drives_coils:
            # Commissioning: pulse the coils ourselves so there is something to
            # watch, then let the throw below record it on the model.
            binding = cfg.switch_output.get(switch_id)
            if binding is None:
                return jsonify({
                    "ok": False,
                    "error": f"switch {switch_id!r} has no coil binding to test",
                }), 400
            from hardware import commission
            from hardware.catalog import SWITCH_THROW_PULSE_S
            from hardware import self_test

            coil = binding.port_main if position == 0 else binding.port_diverge
            other = binding.port_diverge if position == 0 else binding.port_main
            try:
                session = commission.get_session()
                session.open(
                    cfg,
                    open_device=_develop_opener(cfg),
                    device=hardware_config.get_live_device(),
                )
                with _bus_guard():
                    session.pulse_output(
                        binding.chip_id,
                        coil,
                        standby_port=binding.port_standby,
                        off_port=other,
                        pulse_s=self_test.clamp_pulse_s(
                            data.get("pulse_s"), default=SWITCH_THROW_PULSE_S
                        ),
                    )
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)})

        # The operator explicitly asked for this alignment, so a `manual` switch
        # participates — the same override the manual throw controls send.
        with state.simulation_lock:
            ok, message = switch_manager.request_position_change(
                switch_id, position, manual_override=True
            )
        return jsonify({
            "ok": bool(ok),
            "error": None if ok else message,
            "message": message,
            "switch_id": switch_id,
            "position": position,
            "driver": "runtime" if drives_coils else "commission",
        })

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
                    # chosen independently (design_new.md Power, Hardware
                    # configuration), plus the `reversed` rail-polarity flag —
                    # omitted, the flag keeps its stored value.
                    hardware_config.set_track_binding(
                        str(logical_id),
                        data.get("chip_id"),
                        data.get("forward"),
                        data.get("reverse"),
                        polarity_reversed=data.get("reversed"),
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
                        map_name, str(logical_id), data.get("chip_id"), data.get("port"),
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
            # Borrow the running session's controller when there is one; a
            # second open of the same USB endpoint is refused by the driver.
            commission.get_session().open(
                cfg,
                open_device=_develop_opener(cfg),
                device=hardware_config.get_live_device(),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True})

    @app.route("/api/hardware/commission/output", methods=["POST"])
    def hardware_commission_output():
        data = request.get_json(silent=True) or {}
        from hardware import commission

        try:
            with _bus_guard():
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
        off_port?, pulse_s?}.

        `pulse_s` widens the pulse for a user measuring the bit instead of
        watching the points; omitted, the pulse is the runtime throw width, so
        the bench test reproduces an operating throw exactly."""
        data = request.get_json(silent=True) or {}
        from hardware import commission
        from hardware import self_test
        from hardware.catalog import SWITCH_THROW_PULSE_S

        standby = data.get("standby_port")
        off_port = data.get("off_port")
        try:
            with _bus_guard():
                commission.get_session().pulse_output(
                    data.get("chip_id"),
                    int(data.get("port")),
                    standby_port=None if standby is None else int(standby),
                    off_port=None if off_port is None else int(off_port),
                    pulse_s=self_test.clamp_pulse_s(
                        data.get("pulse_s"), default=SWITCH_THROW_PULSE_S
                    ),
                )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True})

    @app.route("/api/hardware/commission/inputs", methods=["POST"])
    def hardware_commission_inputs():
        data = request.get_json(silent=True) or {}
        from hardware import commission

        try:
            with _bus_guard():
                bits = commission.get_session().read_inputs(data.get("chip_id"))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True, "ports": [{"port": i, "value": v} for i, v in enumerate(bits)]})

    @app.route("/api/hardware/commission/currents", methods=["POST"])
    def hardware_commission_currents():
        data = request.get_json(silent=True) or {}
        from hardware import commission

        try:
            with _bus_guard():
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
