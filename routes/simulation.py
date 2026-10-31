"""
Simulation Control Routes - Server lifecycle, simulation start/stop, network
payload, network stats/clear, and model-scale configuration.
"""

import os
import time

from flask import Response, jsonify, request

from app_state import AppState
from services import ValidationError, train_service
from network_poll import build_network_poll_reply
from network_stream import STATE_CHANGING_METHODS, NetworkStreamHub, stream_messages
from simulation import build_network_payload


def register(app, state: AppState) -> None:
    """Register simulation-control routes."""

    def network_payload():
        # Built under the lock, between ticks: a layout load or network clear
        # replaces the network's dicts under the same lock, so a snapshot read
        # outside it could iterate a dict while it changes size.
        with state.simulation_lock:
            payload = build_network_payload(
                state.network,
                state.sim_controller,
                session_state=state.session_state(),
            )
        payload["simulation_running"] = state.simulation_running
        payload["simulation_speed"] = state.simulation_speed
        return payload

    @app.route("/api/network", methods=["GET"])
    def get_network():
        # The poller passes the layout_version it holds; when it still
        # matches, the reply leaves the layout out (design_new.md "Data Flow
        # To UI", Layout version).
        return jsonify(
            build_network_poll_reply(
                network_payload(), request.args.get("layout_version")
            )
        )

    # design_new.md "Data Flow To UI", Network stream: one hub per app, so
    # every open tab sends the same snapshot and it is built once.
    stream_hub = NetworkStreamHub(
        network_payload,
        lambda reply: app.json.dumps(reply, separators=(",", ":"), sort_keys=False),
    )
    app.extensions["network_stream"] = stream_hub

    @app.route("/api/network/stream", methods=["GET"])
    def stream_network():
        messages = stream_messages(stream_hub, request.args.get("layout_version"))
        return Response(
            messages,
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.after_request
    def push_after_state_change(response):
        # Any request that can change state sends the next stream message at
        # once, so every tab sees a command or an edit without waiting for
        # the interval.
        if request.method in STATE_CHANGING_METHODS:
            stream_hub.note_state_changed()
        return response

    def _matches_query(event, needle: str) -> bool:
        """Free-text match over the two human-readable fields plus the origin
        label — `message` says what was attempted, `error.text` says what went
        wrong, and `origin` is how a reader groups errors when `operation`
        alone is too coarse. Display-only: this narrows a view, and no runtime
        branch reads any of the three (design_new.md "Error events")."""
        if needle in event.message.lower() or needle in event.operation.lower():
            return True
        if needle in event.source.lower():
            return True
        if event.error is not None:
            if needle in event.error.text.lower() or needle in event.error.origin.lower():
                return True
        return any(needle in e.entity_id.lower() for e in event.entities)

    @app.route("/api/event-log", methods=["GET"])
    def get_event_log():
        """Read the operator-facing event log (design_new.md "Event log").

        Returns the `NotificationCenter` stream as records the read-only viewer
        and replay layers consume. Optional filters:
        ``?kind=&operation=&source=&origin=&q=&entity_type=&entity_id=&train_id=``
        ``&since=&until=&since_notification_id=&limit=``
        (level / emitting function / emitting class / `ErrorRecord.origin` /
        free text over message+error / entity ref / time range / id watermark /
        most-recent count). ``train_id`` is shorthand for
        ``entity_type=train&entity_id=<id>``: per-entity views are membership
        filters over the event's `entities` list, which is the only entity
        routing key (design_new.md "Runtime notifications").

        ``since_notification_id`` lets a poller ask only for what it has not
        seen, the same watermark the per-train endpoint takes. Ids increase in
        emit order and keep counting past a retention drop, so a watermark is
        never re-served an event it already saw.

        ``?persisted=true`` reads the on-disk stream instead of the in-memory
        archive, so an earlier run's incidents can be reviewed after a restart —
        the persistence the doc's viewer is built on. It falls back to the
        in-memory archive when no log path is configured.
        """
        from notification_center import NotificationCenter

        sim = getattr(state, "sim_controller", None)
        nc = getattr(sim, "notification_center", None) if sim else None
        if nc is None:
            return jsonify({"events": []})
        try:
            limit = int(request.args.get("limit", 500))
        except (TypeError, ValueError):
            limit = 500
        kind = request.args.get("kind")
        operation = request.args.get("operation")
        source_name = request.args.get("source")
        origin = request.args.get("origin")
        query = (request.args.get("q") or "").strip().lower()
        entity_type = request.args.get("entity_type")
        entity_id = request.args.get("entity_id")
        train_id = request.args.get("train_id")
        if train_id and not entity_type:
            entity_type, entity_id = "train", train_id

        try:
            since_id = int(request.args["since_notification_id"])
        except (KeyError, TypeError, ValueError):
            since_id = None

        def _time_arg(name):
            raw = request.args.get(name)
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        since = _time_arg("since")
        until = _time_arg("until")

        persisted = request.args.get("persisted", "").lower() in ("1", "true", "yes")
        if persisted and nc.log_path:
            # The persisted stream reads back in `notification_id` order and
            # survives a restart. Apply `limit` as most-recent after filtering.
            source = NotificationCenter.read_event_log(nc.log_path)
        else:
            source = nc.peek_recent(limit if limit > 0 else 500)

        records = []
        for e in source:
            if kind and e.kind != kind:
                continue
            if operation and e.operation != operation:
                continue
            if source_name and e.source != source_name:
                continue
            if origin and (e.error is None or e.error.origin != origin):
                continue
            if entity_type and entity_id and not e.has_entity(entity_type, entity_id):
                continue
            if since is not None and e.event_time < since:
                continue
            if until is not None and e.event_time > until:
                continue
            if since_id is not None and e.notification_id <= since_id:
                continue
            if query and not _matches_query(e, query):
                continue
            records.append(e.to_dict())

        if limit > 0 and len(records) > limit:
            records = records[-limit:]
        return jsonify({"events": records})

    @app.route("/api/simulation/emergency-cut-out", methods=["POST"])
    def emergency_cut_out():
        """Emergency stop: the layout emergency cut-out, in every environment
        (design_new.md "Emergency cut-out", Layout emergency cut-out).

        Runs between ticks under the lock. While Live or Paused it cuts every
        output and every train out and the session reads Halted for
        `layout_emergency_cut_out`. In Setup it closes whichever Setup tool
        holds the outputs, and the session stays in Setup. While Halted it is
        accepted and changes nothing, because nothing drives the outputs.
        """
        from enums import SessionStateValue
        from hardware.setup_tools import close_setup_tools, report_close_failures

        sim = getattr(state, "sim_controller", None)
        if sim is None:
            return jsonify({"error": "No simulation running"}), 400
        with state.simulation_lock:
            session = state.session_state()
            if session.value is SessionStateValue.SETUP:
                report_close_failures(sim, close_setup_tools())
                message = "Setup tools closed; the outputs are in their safe state"
            elif session.value is SessionStateValue.HALTED:
                message = "The session is halted; nothing drives the outputs"
            else:
                message = sim.layout_emergency_cut_out().message
            body = {
                "status": "success",
                "message": message,
                "session_state": state.session_state().to_dict(),
            }
        return jsonify(body)

    @app.route("/api/simulation/reset", methods=["POST"])
    def reset_layout_cut_out():
        """Reset: release every train after Emergency stop (design_new.md
        "Emergency cut-out", Reset). Refused with 409 unless the session is
        Halted for `layout_emergency_cut_out`."""
        from enums import HaltReason

        sim = getattr(state, "sim_controller", None)
        if sim is None:
            return jsonify({"error": "No simulation running"}), 400
        with state.simulation_lock:
            session = state.session_state()
            if session.halt_reason is not HaltReason.LAYOUT_EMERGENCY_CUT_OUT:
                return jsonify({
                    "error": "Reset applies only after Emergency stop",
                    "session_state": session.to_dict(),
                }), 409
            result = sim.reset_layout_cut_out()
            body = {
                "status": "success",
                "message": result.message,
                "session_state": state.session_state().to_dict(),
            }
        return jsonify(body)

    @app.route("/api/simulation/hold-all", methods=["POST"])
    def hold_all():
        """Hold all: ATO stop on every ATO train, in every session state and
        environment (design_new.md "Hold all"). Returns the trains it stopped."""
        sim = getattr(state, "sim_controller", None)
        if sim is None:
            return jsonify({"error": "No simulation running"}), 400
        with state.simulation_lock:
            stopped = sim.hold_all()
        return jsonify({"status": "success", "stopped_train_ids": stopped})

    @app.route("/api/simulation/control", methods=["POST"])
    def control_simulation():
        """Play, Pause and playback speed, in Simulation only (design_new.md
        "Session state"; "Running and stopped").

        - `start` (Play) is accepted only while the session is Paused and
          writes `simulation_running = True`.
        - `stop` (Pause) is accepted only while it is Live: it enters the safe
          state, which ends every open calibration run, and writes
          `simulation_running = False`.
        - `set_speed` sets `simulation_speed`, the playback multiplier.

        Every other case is refused with 409 and changes nothing, including
        every request in Virtual Hardware and Real Hardware: those go Live
        through activation, run at wall-clock speed, and have no Pause.
        """
        from enums import OperationMode, SessionStateValue

        data = request.json or {}
        action = data.get("action")
        sim = getattr(state, "sim_controller", None)

        def refuse(message):
            return jsonify({"status": "error", "message": message}), 409

        with state.simulation_lock:
            if sim is None or sim.operation_mode is not OperationMode.SIMULATION:
                return refuse(
                    "Play, Pause and playback speed exist only in Simulation"
                )
            session = state.session_state()
            if action == "start":
                if session.value is not SessionStateValue.PAUSED:
                    return refuse(f"Play needs a paused session; it is {session.value.value}")
                state.simulation_running = True
            elif action == "stop":
                if not session.is_live:
                    return refuse(f"Pause needs a live session; it is {session.value.value}")
                # Leaving Live enters the safe state (design_new.md "Running
                # and stopped"): every block voltage goes to 0, every point
                # motor de-energizes, and every open calibration run ends.
                sim.enter_output_safe_state()
                state.simulation_running = False
            elif action == "set_speed":
                try:
                    speed = float(data.get("speed", 1.0))
                except (TypeError, ValueError):
                    return jsonify({"status": "error", "message": "speed must be a number"}), 400
                if speed <= 0:
                    return jsonify({"status": "error", "message": "speed must be positive"}), 400
                state.simulation_speed = speed
            else:
                return refuse(f"unknown action {action!r}")

        return jsonify(
            {
                "status": "success",
                "running": state.simulation_running,
                "speed": state.simulation_speed,
                "session_state": state.session_state().to_dict(),
            }
        )

    @app.route("/api/runtime/clear-fatal", methods=["POST"])
    def clear_fatal_conflict():
        """Release a fatal-conflict halt, leaving every train off.

        The operator has gone and sorted the conflict out on the layout; this
        reopens the gate so the session carries on instead of demanding a
        process restart that would throw the whole layout state away.
        """
        try:
            cleared = train_service.clear_failed_runtime(state)
        except ValidationError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        return jsonify({"status": "success", "cleared_conflict": cleared})

    @app.route("/api/health", methods=["GET"])
    def health():
        """Liveness probe, plus enough identity for the client to tell a
        reconnect apart from a restart.

        `boot_id` changes on every new process. The connection monitor compares
        it across a dropped connection: same id means the socket blipped and the
        page is still valid; a different id means a new server is answering and
        the page must reload (and the previous run's shutdown is worth
        reporting).
        """
        import shutdown

        return jsonify(
            {
                "ok": True,
                "boot_id": shutdown.BOOT_ID,
                "pid": os.getpid(),
                "uptime_seconds": round(time.time() - shutdown._started_at, 1),
            }
        )

    @app.route("/api/server/shutdown-report", methods=["GET"])
    def shutdown_report():
        """How the previous server process ended (shutdown.py).

        `null` when this is the first run on this machine. Otherwise a record
        whose `clean` flag is the operationally important part: a false value
        means the process was killed outright and never drove its outputs to
        the safe state, so the layout may have been left energized.
        """
        import shutdown

        return jsonify({"previous": shutdown.read_last_shutdown()})

    @app.route("/api/server/restart", methods=["POST"])
    def restart_server():
        """Replace the server process with a fresh invocation of ``sys.argv``.

        Flushes autosave first so editor changes from the last <5 minutes are
        not lost, then schedules ``os.execv`` on a short timer so the HTTP
        response can reach the client before the process image is replaced.
        """
        import os
        import sys
        import threading as _threading

        state.simulation_running = False
        # A restart stops the session, so it enters the safe state like a stop
        # does: every block voltage goes to 0 and every open calibration run ends
        # (design_new.md "What ends a calibration run").
        sim = getattr(state, "sim_controller", None)
        if sim is not None:
            with state.simulation_lock:
                sim.enter_output_safe_state()
        try:
            with state.simulation_lock:
                state.persistence_manager.save_merged_layout(
                    "_autosave", state.network, "Pre-restart autosave"
                )
                state.last_autosave_time = time.time()
            flushed = True
            flush_error = None
        except Exception as e:
            flushed = False
            flush_error = str(e)
            print(f"[Restart] Pre-restart autosave failed: {e}")

        def _do_restart() -> None:
            print("[Restart] Exec'ing fresh process image...")
            # Werkzeug marks its listening socket inheritable and puts its fd in
            # WERKZEUG_SERVER_FD, so the socket survives the exec. Under the
            # reloader (WERKZEUG_RUN_MAIN=true) the new image serves on that fd.
            # Without it the new image binds the port itself, so the old socket
            # must close at exec: otherwise server.py's port check finds the port
            # held by our own pid and the new image exits.
            if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
                server_fd = os.environ.pop("WERKZEUG_SERVER_FD", None)
                if server_fd is not None:
                    try:
                        os.set_inheritable(int(server_fd), False)
                    except (OSError, ValueError):
                        pass  # not an open fd: nothing would be inherited
            os.execv(sys.executable, [sys.executable] + sys.argv)

        _threading.Timer(0.2, _do_restart).start()

        return jsonify(
            {
                "status": "restarting",
                "flushed": flushed,
                "flush_error": flush_error,
            }
        )

    @app.route("/api/network/steps", methods=["GET"])
    def get_network_steps():
        """Return current network layout as StepRecorder steps.

        Used by the frontend scenario recorder to embed the layout
        in scenario files for self-contained replay.
        """
        from step_recorder import StepRecorder

        recorder = StepRecorder()
        steps = recorder.generate_steps_list(state.network)
        return jsonify({"steps": steps})

    @app.route("/api/network/clear", methods=["POST"])
    def clear_network():
        """Clear the current network, giving a fresh canvas.

        Returns counts of what was cleared for confirmation display.
        """
        was_running = state.simulation_running
        state.simulation_running = False

        try:
            with state.simulation_lock:
                # Count what we're clearing
                counts = {
                    "tracks": len(state.network.tracks),
                    "blocks": len(state.network.blocks),
                    "signals": len(state.network.signals),
                    "platforms": len(getattr(state.network, "platforms", {})),
                    "stations": len(state.network.station_groups),
                    "trains": len(state.network.trains),
                    "switches": len(state.network.switches),
                    "tunnels": len(state.network.tunnels_to_list()),
                }

                # The trains are about to be replaced, so every calibration run
                # ends and every held result is discarded first (design_new.md
                # "What ends a calibration run").
                state.sim_controller.speed_calibration.discard_all(
                    "calibration ended: the network was cleared"
                )

                # Clear everything — entity dicts plus the derived tile->owner
                # reverse indices and caches (see RailwayNetwork.clear). Without
                # the index reset, a new track drawn on a tile the old layout
                # used keeps reporting its old block, so block creation fails
                # with "already assigned to block(s)".
                state.network.clear()

                # Update simulation controller reference
                state.sim_controller.network = state.network

            return jsonify(
                {"status": "success", "message": "Network cleared", "cleared": counts}
            )
        finally:
            state.simulation_running = was_running

    @app.route("/api/network/stats", methods=["GET"])
    def get_network_stats():
        """Get counts of items in the network (for checking if empty)."""
        with state.simulation_lock:
            return jsonify(
                {
                    "tracks": len(state.network.tracks),
                    "blocks": len(state.network.blocks),
                    "signals": len(state.network.signals),
                    "platforms": len(getattr(state.network, "platforms", {})),
                    "stations": len(state.network.station_groups),
                    "trains": len(state.network.trains),
                    "switches": len(state.network.switches),
                    "tunnels": len(state.network.tunnels_to_list()),
                    "is_empty": len(state.network.tracks) == 0,
                }
            )

    @app.route("/api/scale", methods=["GET"])
    def get_scale():
        """Get current model scale configuration.

        Returns:
            JSON with current scale value and available presets
        """
        from model_scale import get_model_scale, SCALE_PRESETS

        return jsonify({"scale": get_model_scale(), "presets": SCALE_PRESETS})

    @app.route("/api/scale", methods=["PUT"])
    def set_scale():
        """Set model scale.

        Request Body:
            {"scale": 160.0}  - Direct scale value
            OR
            {"preset": "1:160"}  - Use a preset

        Returns:
            JSON with new scale value
        """
        from model_scale import get_model_scale, set_model_scale, SCALE_PRESETS

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        with state.simulation_lock:
            try:
                if "preset" in data:
                    preset_name = data["preset"]
                    if preset_name not in SCALE_PRESETS:
                        return jsonify(
                            {
                                "error": f"Unknown preset '{preset_name}'",
                                "available_presets": list(SCALE_PRESETS.keys()),
                            }
                        ), 400
                    set_model_scale(SCALE_PRESETS[preset_name])
                elif "scale" in data:
                    scale = float(data["scale"])
                    set_model_scale(scale)
                else:
                    return (
                        jsonify({"error": "Must provide 'scale' or 'preset'"}),
                        400,
                    )

                return jsonify({"scale": get_model_scale(), "status": "success"})
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
