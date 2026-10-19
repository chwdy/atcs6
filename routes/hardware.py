"""Hardware configuration routes (design_new.md "Hardware setup").

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
  mirrored onto the applied-output store (`BlockPowerController`) while it is
  held, so the layout shows the
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
- `POST /api/hardware/bench/{open,close,drive,threshold}` + `GET /api/hardware/bench/state`
  run the occupancy bench: hold every block at one voltage and read BOTH
  detectors, with the runtime's occupancy loop out of the way entirely. Unlike
  the tools above it does not borrow the session's device — it takes it, the same
  handover commissioning does, because the thing being diagnosed is the loop the
  runtime would otherwise be closing twenty times a second on top of it. It is
  also the one handover that undoes itself: `close` reactivates the environment
  `open` deactivated, because a diagnostic window is something an operator looks
  through, not a mode they switch into. See hardware/occupancy_bench.py.

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
import operation_environment
from enums import OperationMode
from runtime_payloads import HardwareFailure


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

    Reads the requested environment directly (not a gate that also demands
    `enabled`), because commissioning runs on the fake BEFORE the user can
    enable hardware (enabling requires every chip proven), so it must not also
    require `enabled`."""
    requested = getattr(cfg, "_requested_environment", None)
    if requested is not OperationMode.VIRTUAL_HARDWARE:
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
    # What the occupancy bench took from the runtime, so its close can put back
    # exactly that and no more. Both are only meaningful while a bench is open.
    if not hasattr(state, "bench_owns_commissioning"):
        state.bench_owns_commissioning = False
    if not hasattr(state, "bench_restore_environment"):
        state.bench_restore_environment = False
    from hardware.virtual_rig import VirtualRig
    virtual_rig = VirtualRig()
    virtual_rig_device = {"value": None}

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
                "operation_environment": state.sim_controller.operation_mode.value,
                "operation_mode": state.sim_controller.operation_mode.value,
                "environment_active": state.sim_controller.environment_active,
                "virtual_hardware_active": hardware_config.get_session_device() is not None,
                "develop_active": hardware_config.get_session_device() is not None,
                # Carried here as well as on `/api/operation/environment` so the
                # per-entity binding panels can say "saved, but not running"
                # without a second request on every render.
                "awaits_activation": hardware_config.setup_awaits_activation(),
                "session_state": state.session_state().to_dict(),
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

        The sweep is mirrored onto the applied-output store (`BlockPowerController`)
        leg by leg, for the
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
        power_controller = getattr(state.sim_controller, "power_controller", None)

        def _mirror(volts: float) -> None:
            # Mirror the sweep voltage onto the applied-output store so the layout
            # shows it while the leg is held (design_new.md "Power"). Voltage is
            # not a `Block` field; it is recorded on `BlockPowerController`.
            if block is not None and power_controller is not None:
                power_controller.set_recorded_voltage(str(block_id), volts)

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

        The two halves write the model through different `SwitchManager` entry
        points, because the second one runs while no operating environment is
        active — the ordinary throw is refused in that state, and the coil has
        been driven already. See `record_bench_alignment`.
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

        if drives_coils:
            # The session's own switch output owns these coils, so the ordinary
            # operating throw IS the test. The operator explicitly asked for
            # this alignment, so a `manual` switch participates — the same
            # override the manual throw controls send.
            with state.simulation_lock:
                ok, message = switch_manager.request_position_change(
                    switch_id, position, manual_override=True
                )
        else:
            # Commissioning: pulse the coils ourselves so there is something to
            # watch, then record that alignment on the model.
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

            # The coil is already pulsed, on chips commissioning has taken, so
            # this only records where the points now are. The operating throw
            # cannot do it: it would drive the coil again, and it refuses while
            # no environment is active — which is the state a bench throw runs
            # in, so routing this through it made every test fail on the throw
            # the operator had just watched land.
            with state.simulation_lock:
                ok, message = switch_manager.record_bench_alignment(
                    switch_id, position
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
                elif map_name == "switch_track_source":
                    # The one binding whose value is another logical id: the
                    # block whose feed powers this switch's rails (design_new.md
                    # Power, Switch power-controller role).
                    hardware_config.set_switch_track_source(
                        str(logical_id), data.get("block_id")
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

    # -- Occupancy bench ---------------------------------------------------
    # A device-owning diagnostic session: hold every block at one voltage and
    # read both detectors, with the runtime out of the loop entirely. See
    # hardware/occupancy_bench.py for why it takes the controller rather than
    # sharing the running session's.

    def _bench_session():
        from hardware import occupancy_bench

        return occupancy_bench.get_session()

    def _bench_opener(cfg, environment):
        """How the bench opens its controller, or None for "open the real CH347".

        Deliberately not `_develop_opener`: that one gates on the environment
        *requested by the payload under test*, because commissioning always tests
        the edit in progress. The bench has no edit in progress — it operates the
        persisted configuration under the environment that was selected — so it
        gates on the selection, the same input `build_session_interfaces` uses to
        choose between the fake device and the physical bridge. Gating it the
        commissioning way sent Virtual Hardware looking for a USB bridge.
        """
        if environment is not OperationMode.VIRTUAL_HARDWARE:
            return None
        from hardware.fake import FakeCh347Device

        def _open():
            device = FakeCh347Device(
                present_addresses={
                    c.i2c_address for c in cfg.chips if c.i2c_address is not None
                }
            )
            device.open()
            return device

        return _open

    @app.route("/api/hardware/bench/open", methods=["POST"])
    def hardware_bench_open():
        """Take the controller for the bench.

        Does the same handover `enter_commissioning` does, for the same reason:
        the rig is one USB endpoint, so the bench cannot have it while a session
        is driving through it. The environment is deactivated, the session device
        closed, and the runtime handed back the simulated boundary — leaving the
        dead chips bound would make the next safe state raise.

        `setup_active` is the flag that keeps the two bench-style screens
        (this and Hardware Setup's port grid) off the controller at the same time,
        and blocks activation until one of them gives it back.

        What this route takes, `close` gives back — including a live environment.
        Which of the two took it is recorded here, because opening a bench while
        Hardware Setup already holds the device takes nothing: closing must then
        leave the ownership it found exactly as it found it rather than handing
        the runtime a device another screen is still driving.
        """
        if state.simulation_running:
            return jsonify({"status": "error", "error": "stop operation first"}), 409
        selected = operation_environment.get_selected()
        if selected is OperationMode.SIMULATION:
            return jsonify({
                "status": "error",
                "error": "Simulation has no hardware to bench",
            }), 409
        try:
            cfg = hardware_config.get_hardware_config()
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        with state.simulation_lock:
            if not state.setup_active:
                # Only a session this bench actually takes something from is
                # worth restoring on the way out — otherwise closing a diagnostic
                # window would energize a rig on nobody's request. Both halves
                # are needed: a controller that never activated still reports the
                # environment it was constructed with as active, and a device
                # handle without an active environment belongs to a session that
                # is already blocked. Together they mean one thing, which is the
                # thing being taken away: a live, hardware-backed runtime.
                state.bench_restore_environment = bool(
                    state.sim_controller.environment_active
                    and hardware_config.get_live_device() is not None
                )
                state.sim_controller.deactivate_environment(
                    [{"code": "commissioning", "message": "the occupancy bench owns the device"}],
                    environment=selected,
                )
                hardware_config.close_live_device()
                state.sim_controller.release_interfaces()
                state.setup_active = True
                state.bench_owns_commissioning = True
            try:
                status = _bench_session().open(
                    cfg,
                    open_device=_bench_opener(cfg, selected),
                    track_polarity_of=hardware_config.track_polarity_reversed,
                )
            except Exception as exc:  # noqa: BLE001 - reported to the panel
                # The handover above already happened, so a failure here has
                # taken the controller and produced no bench to hold it: the
                # environment is down, `setup_active` is latched, and
                # the panel never starts polling `bench/state` because it has
                # nothing open to poll — so nothing else will notice. An
                # operator who opened the bench with the rig unplugged was left
                # with a silently dead railway and an error about the wrong
                # thing. Give back exactly what this call took, then report.
                _bench_session().close()
                restored = _restore_after_bench_locked()
                return jsonify({"ok": False, "error": str(exc), **restored}), 200
        return jsonify({"ok": True, **status})

    @app.route("/api/hardware/bench/close", methods=["POST"])
    def hardware_bench_close():
        """De-energize, release the controller, and put back what `open` took.

        Restoring the environment is this route's job rather than the panel's.
        The bench is a diagnostic window, not a mode: an operator opens it to see
        why a block reads wrong and closes it to carry on operating, and the
        device handover in between is a means, not something they asked for. It
        was the route that deactivated the environment to get the device, so it
        is the route that reactivates it — leaving that to the caller left a
        railway silently unpowered behind a window that had already gone away.

        Restores only what this bench took (see `open`), and reports the restore:
        a rebuild can still be blocked — by a layout edited while the bench was
        up, or a bridge that left the bus — and a close that swallowed that would
        be the same silence in a new place.
        """
        with state.simulation_lock:
            _bench_session().close()
            restored = _restore_after_bench_locked()
        return jsonify({"ok": True, **restored})

    def _restore_after_bench_locked():
        """Put back the ownership a bench open took. Caller holds the lock.

        Split out of the close route because closing is not the only way a bench
        ends. `IDLE_TIMEOUT_S` closes the session from the inside — dropping the
        device and de-energizing — but it can only reach the objects it owns, and
        `setup_active` and the environment are the app's, not the
        session's. An expiry alone therefore left the runtime locked out of a
        controller that nothing was holding any more: no detection, no drive, and
        no window on screen to explain it, until someone opened and closed a
        bench again. Every path that observes the session closed calls this, so
        the handover is undone by whichever one gets there first.

        Idempotent: with nothing of ours outstanding it reports the state and
        touches none of it, which is what makes it safe on a polling route.
        """
        if not state.bench_owns_commissioning:
            # Either there was nothing to give back, or Hardware Setup was
            # holding the device before this bench opened and is holding it
            # still. Neither is ours to undo.
            return {
                "commissioning": bool(state.setup_active),
                "reactivated": False,
                "issues": [],
            }
        state.setup_active = False
        state.bench_owns_commissioning = False
        restore = state.bench_restore_environment
        state.bench_restore_environment = False
        blocked = _activate_locked(operation_environment.get_selected()) if restore else None
        return {
            "commissioning": False,
            "reactivated": bool(restore and blocked is None),
            "issues": (blocked or {}).get("issues", []),
        }

    @app.route("/api/hardware/bench/state", methods=["GET"])
    def hardware_bench_state():
        """Both detectors for every bound block, plus what the bench is driving.

        Answers with `open: false` rather than an error when no bench is running,
        so the panel can poll one endpoint whether or not it holds the device.

        `is_open()` is also where an idle session is reaped, which makes this the
        route that most often first observes a bench that ended without anyone
        closing it. So it undoes the handover on the way out
        (`_restore_after_bench_locked`) instead of reporting `open: false` over a
        runtime still locked out of the controller.
        """
        session = _bench_session()
        if not session.is_open():
            with state.simulation_lock:
                restored = _restore_after_bench_locked()
            return jsonify({"ok": True, "open": False, "blocks": {}, **restored})
        with state.simulation_lock:
            try:
                payload = session.read()
            except Exception as exc:  # noqa: BLE001 - reported to the panel
                return jsonify({"ok": False, "open": True, "error": str(exc)}), 200
            payload["link"] = session.link_status()
        return jsonify({"ok": True, **payload})

    @app.route("/api/hardware/bench/drive", methods=["POST"])
    def hardware_bench_drive():
        """Start / stop the bench drive and set which blocks run reversed.

        Body: ``{running, percent, flipped: [block_id, ...]}``. `flipped`
        replaces the stored set outright — see `OccupancyBenchSession.drive`.
        """
        data = request.get_json(silent=True) or {}
        session = _bench_session()
        if not session.is_open():
            return jsonify({"ok": False, "error": "the occupancy bench is not open"}), 409
        with state.simulation_lock:
            try:
                status = session.drive(
                    bool(data.get("running")),
                    percent=data.get("percent"),
                    flipped=data.get("flipped"),
                )
            except Exception as exc:  # noqa: BLE001 - reported to the panel
                return jsonify({"ok": False, "error": str(exc)}), 200
        return jsonify({"ok": True, **status})

    @app.route("/api/hardware/bench/threshold", methods=["POST"])
    def hardware_bench_threshold():
        """Judge every current channel against one threshold.

        Body: ``{threshold_ma}`` — a number, or null to put each block back on
        its own configured threshold. Reads only: the rails are not re-commanded,
        so an operator can sweep the number and watch where each block crosses
        over without disturbing the drive it is crossing over under.

        Bench-local. The calibration this finds is saved per channel in Hardware
        Setup; nothing here writes the configuration.
        """
        data = request.get_json(silent=True) or {}
        session = _bench_session()
        if not session.is_open():
            return jsonify({"ok": False, "error": "the occupancy bench is not open"}), 409
        raw = data.get("threshold_ma")
        try:
            threshold = None if raw is None or raw == "" else float(raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": f"threshold_ma must be a number, got {raw!r}"}), 400
        with state.simulation_lock:
            try:
                status = session.set_current_threshold(threshold)
            except Exception as exc:  # noqa: BLE001 - reported to the panel
                return jsonify({"ok": False, "error": str(exc)}), 200
        return jsonify({"ok": True, **status})

    @app.route("/api/hardware/bench/peaks/reset", methods=["POST"])
    def hardware_bench_reset_peaks():
        """Start every current channel's running peak over.

        Takes no body: a peak is per channel but only ever reset all at once,
        because the point of the summary is comparing channels measured over the
        same window. The bench resets them itself whenever the drive changes;
        this is for when what changed was on the layout — a locomotive lifted
        off, a block rewired — which nothing on this side can observe.
        """
        session = _bench_session()
        if not session.is_open():
            return jsonify({"ok": False, "error": "the occupancy bench is not open"}), 409
        with state.simulation_lock:
            try:
                status = session.reset_peaks()
            except Exception as exc:  # noqa: BLE001 - reported to the panel
                return jsonify({"ok": False, "error": str(exc)}), 200
        return jsonify({"ok": True, **status})

    @app.route("/api/hardware/commission/open", methods=["POST"])
    def hardware_commission_open():
        if not state.setup_active:
            return jsonify({
                "ok": False,
                "error": "enter commissioning before opening the device",
            }), 409
        data = request.get_json(silent=True) or {}
        try:
            cfg = hardware_config.config_from_payload(data.get("config", {}))
        except hardware_config.HardwareConfigError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        from hardware import commission

        try:
            # Borrow the running session's controller when there is one; a
            # second open of the same USB endpoint is refused by the driver.
            # `timeout_seconds` is the commissioning `enter` body's idle window,
            # bounded by COMMISSION_MAX_SESSION_SECONDS; an out-of-bound request
            # raises and is reported below rather than opening the session.
            commission.get_session().open(
                cfg,
                open_device=_develop_opener(cfg),
                device=hardware_config.get_live_device(),
                timeout_seconds=data.get("timeout_seconds"),
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

    _NOT_DEVELOP = {
        "status": "error",
        "error": "session is not running in Virtual Hardware",
    }

    @app.route("/api/virtual-hardware", methods=["GET"])
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

            # The pipeline's VERDICT, taken off the detector rather than by
            # reading the device again here. Two reasons, and both bit:
            #
            #   - This is the column that answers "did the app react", so it has
            #     to be the debounced confirmed set `Block.occupied_by` follows.
            #     A raw read reports a clear OCCUPANCY_OFF_THRESHOLD ticks before
            #     the app acts on one, so the panel would say clear while the
            #     layout still showed the block held — which reads as the app
            #     being slow when it is the panel running ahead.
            #   - `read_block_currents()` MUTATES the driver: it advances the
            #     lighting-zone clear window and rewrites `_last_reported`
            #     (OccupancyDriver). A panel polling at 4 Hz would add its reads
            #     to a window sized for the 10 Hz tick, so opening the bench
            #     changed the release timing of the very block it was watching.
            #
            # The per-detector values below are direct chip-register reads, which
            # carry no such side effect and stay live at the poll rate.
            detector = getattr(state.sim_controller, "block_occupancy_detector", None)
            confirmed = detector.confirmed_occupied if detector is not None else set()
            in_layout = set(state.network.blocks)

            blocks = {}
            for block_id in (
                set(cfg.block_occupancy)
                | set(cfg.block_occupancy_current)
                | set(cfg.block_track)
            ):
                binding = cfg.block_occupancy.get(block_id)
                cbind = cfg.block_occupancy_current.get(block_id)
                tbind = cfg.block_track.get(block_id)
                # The `has_*` flags say which detectors and outputs this block is
                # WIRED to; the values below say what they currently read. The
                # panel needs both: a null value means "unwired" or "the chip
                # would not build", and only the flag tells those apart.
                entry = {
                    "has_digital": binding is not None,
                    "has_current": cbind is not None,
                    "has_track": tbind is not None,
                    # `occupancy_bit` is the RAW register bit; `detector_present`
                    # is that bit resolved through the chain's polarity, which is
                    # what the pipeline reads and therefore what "is there a
                    # train on it" means. On an active-low chain the two are
                    # opposites, so a panel that showed only the raw bit would
                    # report every idle block as clear and every occupied one as
                    # clear too.
                    "occupancy_bit": None, "detector_present": None,
                    "active_low": None,
                    "current_ma": None, "volts": None,
                    "duty_forward": None, "duty_reverse": None,
                    "track_reversed": bool(tbind.reversed) if tbind is not None else None,
                    "threshold_ma": float(cbind.threshold_ma) if cbind is not None else None,
                    "baseline_ma": float(cbind.baseline_ma or 0.0) if cbind is not None else None,
                    # Null where there is no verdict to report: a block with no
                    # detector has nothing to conclude from, and one bound to a
                    # block id the layout does not have is never read at all
                    # (the global detector read walks `network.blocks`). Reporting either as
                    # "clear" would hide a mis-wired binding behind a normal
                    # answer.
                    "occupied": (
                        (block_id in confirmed)
                        if (binding is not None or cbind is not None)
                        and block_id in in_layout
                        else None
                    ),
                }
                if binding is not None:
                    spec = _chip_spec(cfg, binding.chip_id)
                    active_low = bool(getattr(spec, "active_low", False)) if spec else False
                    entry["active_low"] = active_low
                    chip = chip_for(binding.chip_id)
                    if chip is not None:
                        from hardware.interfaces import resolve_detector_bit

                        raw = bool(chip.read_bit(binding.port))
                        entry["occupancy_bit"] = raw
                        entry["detector_present"] = resolve_detector_bit(
                            raw, active_low=active_low
                        )
                if cbind is not None:
                    chip = chip_for(cbind.chip_id)
                    if chip is not None:
                        try:
                            entry["current_ma"] = round(
                                chip.read_current(cbind.port + 1) * 1000.0, 3
                            )
                        except Exception:
                            pass
                if tbind is not None:
                    chip = chip_for(tbind.chip_id)
                    if chip is not None:
                        # The two PWM duties as commanded, before the polarity
                        # flag is applied — what the board is actually doing.
                        # `volts` is the same pair resolved the way the driver
                        # resolves it, which is what the layout should agree with.
                        fwd = device.duty_cycle(chip.address, tbind.forward)
                        rev = device.duty_cycle(chip.address, tbind.reverse)
                        entry["duty_forward"] = round(fwd, 4)
                        entry["duty_reverse"] = round(rev, 4)
                        signed = (rev - fwd) if tbind.reversed else (fwd - rev)
                        entry["volts"] = round(signed * MAX_TRACK_VOLTAGE, 2)
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

        return jsonify({
            "virtual_hardware_active": True,
            "develop_active": True,
            "blocks": blocks,
            "switches": switches,
            # Operator-facing injected faults (VirtualHardwareFaultRequest
            # records); `faults` is the low-level transport view below it.
            "injected_faults": device.virtual_fault_status(),
            "faults": device.fault_status(),
        })

    @app.route("/api/virtual-hardware/input/occupancy", methods=["PUT"])
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

    @app.route("/api/virtual-hardware/input/current", methods=["PUT"])
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

    @app.route("/api/virtual-hardware/input/presence", methods=["PUT"])
    def debug_hw_set_presence():
        """Put a virtual object on a block's detectors, or lift it off. Body:
        {block_id, present}.

        The one-block form of what the scenario rig does per step, and the
        control the bench places trains with. It writes every detector the block
        is bound to at once, because which one the runtime believes depends on
        how hard the block is being driven — setting the '165 bit alone leaves a
        driven block still reading its current channel, which is how "the app
        ignored my fake train" happens. The two raw endpoints above stay for
        deliberately making the sources disagree."""
        device = _require_fake()
        if device is None:
            return jsonify(_NOT_DEVELOP), 409
        data = request.get_json(silent=True) or {}
        block_id = data.get("block_id")
        present = data.get("present")
        if not block_id or present is None:
            return jsonify({
                "status": "error", "error": "block_id and present are required",
            }), 400
        from hardware.virtual_rig import set_block_presence

        cfg = hardware_config.get_hardware_config()
        try:
            with state.simulation_lock:
                set_block_presence(cfg, device, str(block_id), bool(present))
        except ValueError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404
        return jsonify({
            "ok": True, "block_id": str(block_id), "present": bool(present),
        })

    @app.route("/api/virtual-hardware/fault", methods=["PUT"])
    def virtual_hardware_set_fault():
        """Inject or clear one VirtualHardwareFaultRequest below the real HAL
        (design_new.md "Hardware REST payload schemas").

        Body: `{action, target_kind, target_id, operation, fault}`. `action` is
        `inject` or `clear`; `fault` is one of the 6 VirtualHardwareFaultKind
        values, required for `inject` and absent for `clear`; an incompatible
        (target_kind, fault) pair is rejected rather than translated.
        """
        from hardware.fake import validate_virtual_fault_request

        device = _require_fake()
        if device is None:
            return jsonify(_NOT_DEVELOP), 409
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        target_kind = data.get("target_kind")
        target_id = data.get("target_id")
        operation = data.get("operation")
        fault = data.get("fault")
        if not isinstance(target_id, str) or not target_id:
            return jsonify(
                {"status": "error", "error": "target_id is required"}
            ), 400
        err = validate_virtual_fault_request(action, target_kind, fault)
        if err is not None:
            return jsonify({"status": "error", "error": err}), 400
        try:
            with state.simulation_lock:
                if action == "inject":
                    device.inject_virtual_fault(
                        target_kind, target_id, operation, fault
                    )
                else:
                    device.clear_virtual_fault(
                        target_kind, target_id, operation, fault
                    )
        except ValueError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        return jsonify(
            {"ok": True, "injected_faults": device.virtual_fault_status()}
        )

    @app.route("/api/virtual-hardware/faults/clear", methods=["POST"])
    def virtual_hardware_clear_faults():
        device = _require_fake()
        if device is None:
            return jsonify(_NOT_DEVELOP), 409
        with state.simulation_lock:
            device.clear_all_virtual_faults()
            device.clear_faults()
        return jsonify(
            {"ok": True, "injected_faults": device.virtual_fault_status()}
        )

    @app.route("/api/virtual-hardware/scenario", methods=["GET", "POST"])
    def virtual_hardware_scenario():
        device = _require_fake()
        if device is None:
            return jsonify(_NOT_DEVELOP), 409
        if virtual_rig_device["value"] is not device:
            virtual_rig.blocks = []
            virtual_rig.index = None
            virtual_rig.stalled = False
            virtual_rig_device["value"] = device
        if request.method == "GET":
            return jsonify({"ok": True, "scenario": virtual_rig.status()})

        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "status"))
        cfg = hardware_config.get_hardware_config()
        try:
            with state.simulation_lock:
                if action == "start":
                    scenario = virtual_rig.start(cfg, device, data.get("blocks") or [])
                elif action == "advance":
                    scenario = virtual_rig.advance(cfg, device)
                elif action == "stall":
                    scenario = virtual_rig.set_stalled(bool(data.get("stalled", True)))
                elif action == "remove":
                    scenario = virtual_rig.remove(cfg, device)
                elif action == "status":
                    scenario = virtual_rig.status()
                else:
                    raise ValueError(f"unknown scenario action {action!r}")
        except ValueError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        return jsonify({"ok": True, "scenario": scenario})
    @app.route("/api/operation/environment", methods=["GET"])
    def get_operation_environment():
        return jsonify({
            "selected": operation_environment.get_selected().value,
            "active": state.sim_controller.operation_mode.value,
            "is_active": bool(state.sim_controller.environment_active),
            "activation_issues": list(state.sim_controller.activation_issues),
            "commissioning": bool(state.setup_active),
            # The one session truth the header reads (design_new.md "Session
            # state").
            "session_state": state.session_state().to_dict(),
            # Saved setup changes the running session is not using. Without it
            # an operator re-picks a chip or a channel, sees it save, and has
            # nothing telling them the rig is still running the old wiring.
            "awaits_activation": hardware_config.setup_awaits_activation(),
        })

    @app.route("/api/operation/environment", methods=["PUT"])
    def update_operation_environment():
        data = request.get_json(silent=True) or {}
        try:
            selected = operation_environment.select(data.get("selected"))
        except ValueError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        return jsonify({
            "status": "success",
            "selected": selected.value,
            "active": state.sim_controller.operation_mode.value,
            "is_active": bool(state.sim_controller.environment_active),
        })

    @app.route("/api/operation/commissioning/enter", methods=["POST"])
    def enter_commissioning():
        if state.simulation_running:
            return jsonify({"status": "error", "error": "stop operation first"}), 409
        selected = operation_environment.get_selected()
        if selected is OperationMode.SIMULATION:
            return jsonify({
                "status": "error",
                "error": "Simulation has no hardware to commission",
            }), 409
        with state.simulation_lock:
            safe_state_failures = state.sim_controller.deactivate_environment(
                [{"code": "commissioning", "message": "commissioning owns the device"}],
                environment=selected,
            )
            if safe_state_failures:
                # A bridge that has left the bus cannot be de-energized by
                # refusing this request either, and refusing it takes away the
                # one tool that reaches the rig: opening a commissioning session
                # clears every shift-out chain on the way in, so entering is how
                # those coils actually drop once the link is back. Hand the
                # device over, and latch the fault so the halt, the event, and
                # the banner all say the outputs were never proven safe.
                state.sim_controller.enter_hardware_failure(
                    HardwareFailure(
                        component="OutputSafety",
                        operation="enter_commissioning",
                        message="; ".join(safe_state_failures),
                    )
                )
            hardware_config.close_live_device()
            # The session's chips were built on the handle just closed, so they
            # can no longer reach the rig. Hand the runtime back the simulated
            # boundary: leaving the dead chips bound would make the next safe
            # state — this route on a second enter, or `activate` on the way
            # back out — raise instead.
            state.sim_controller.release_interfaces()
            state.setup_active = True
        return jsonify({
            "status": "success",
            "commissioning": True,
            "safe_state": {
                "ok": not safe_state_failures,
                "failures": safe_state_failures or [],
            },
        })

    @app.route("/api/operation/commissioning/exit", methods=["POST"])
    def exit_commissioning():
        from hardware import commission

        with state.simulation_lock:
            commission.get_session().close()
            state.setup_active = False
        return jsonify({
            "status": "success",
            "commissioning": False,
            "requires_activation": True,
        })

    def _readiness():
        # Resolve every placed train's lighting_voltage so the evaluator can
        # check it against current_sense_min_drive_v (design_new.md "Power":
        # the shared readiness evaluator enforces that bound at activation).
        lighting_voltages = {}
        registry = getattr(state.network, "speed_profiles", None)
        for train_id, train in state.network.trains.items():
            profile_id = getattr(train, "speed_profile_id", None)
            resolved = (
                registry.resolve(profile_id)
                if registry is not None and profile_id
                else None
            )
            if resolved is not None:
                lighting_voltages[train_id] = getattr(
                    resolved, "lighting_voltage", None
                )
        return hardware_config.activation_report(
            hardware_config.get_hardware_config(),
            operation_environment.get_selected(),
            state.network.blocks.keys(),
            state.network.switches.keys(),
            lighting_voltages=lighting_voltages,
            )

    def _activate_locked(selected):
        """Build the session's hardware boundary for `selected` and make it live.

        Returns None once the environment is active, or the blocked response body
        explaining why it is not — in which case the controller has already been
        told, so a caller that only wants to know whether it worked can test the
        return for None and drop the rest.

        Its own function rather than the body of `/api/operation/activate`
        because activation is also how a diagnostic session gives the runtime
        back. The bench takes the device by deactivating the environment, and
        undoing that is exactly this sequence — a second copy of it would be one
        more place for the safe-state ordering below to drift.

        Caller holds `simulation_lock` and has already released whatever else was
        holding the device.
        """
        report = _readiness()
        if not report["ready"]:
            state.sim_controller.deactivate_environment(
                report["issues"], environment=selected
            )
            return {"status": "blocked", **report}
        # A rig that cannot be safed must not stop the rebuild: the outgoing
        # chips are about to be dropped, and building the new ones re-inits
        # every chip — clearing each shift-out chain before anything else on
        # the bus — which is the de-energize this call could not land. What
        # it must not do is disappear, so a failure is carried down to the
        # blocked report for the case where no new session arrives.
        safe_state_failures = state.sim_controller.try_enter_safe_state()
        try:
            hardware_config.close_live_device()
            # Unbind the previous session's chips before building the next
            # ones: they hold the handle just closed, and both the failure
            # path below and `activate_interfaces` safe the outputs again.
            state.sim_controller.release_interfaces()
            interfaces = hardware_config.build_session_interfaces(
                hardware_config.get_hardware_config(),
                environment=selected,
                track_polarity_of=hardware_config.track_polarity_reversed,
            )
            state.sim_controller.activate_interfaces(selected, *interfaces)
        except hardware_config.HardwareBackendUnavailable as exc:
            issues = [{"code": "backend_unavailable", "message": str(exc)}]
            if safe_state_failures:
                issues.append({
                    "code": "safe_state_failed",
                    "message": (
                        "outputs could not be driven to their safe state: "
                        + "; ".join(safe_state_failures)
                    ),
                })
            state.sim_controller.deactivate_environment(issues, environment=selected)
            return {
                "status": "blocked",
                "ready": False,
                "environment": selected.value,
                "issues": issues,
            }
        return None

    @app.route("/api/operation/readiness", methods=["GET"])
    def get_operation_readiness():
        return jsonify(_readiness())

    @app.route("/api/operation/activate", methods=["POST"])
    def activate_operation_environment():
        if state.setup_active:
            return jsonify({
                "status": "error",
                "error": "exit commissioning before activation",
            }), 409
        if state.simulation_running:
            return jsonify({
                "status": "error",
                "error": "stop operation before activating an environment",
            }), 409
        selected = operation_environment.get_selected()
        with state.simulation_lock:
            blocked = _activate_locked(selected)
            if blocked is None:
                # design_new.md "Activation starts the railway over": before the
                # new environment's first tick, drop live paths, running runs,
                # and train power; keep the layout, setup, trains, and routes.
                state.sim_controller.start_railway_over()
        if blocked is not None:
            return jsonify(blocked), 409
        return jsonify({
            "status": "success",
            "environment": selected.value,
            "active": True,
        })
