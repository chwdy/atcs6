"""
Simulation Control Routes - Server lifecycle, simulation start/stop, network
payload, network stats/clear, and model-scale configuration.
"""

import time

from flask import jsonify, request

from app_state import AppState
from simulation import build_network_payload


def register(app, state: AppState) -> None:
    """Register simulation-control routes."""

    @app.route("/api/network", methods=["GET"])
    def get_network():
        payload = build_network_payload(state.network, state.sim_controller)
        payload["simulation_running"] = state.simulation_running
        payload["simulation_speed"] = state.simulation_speed
        return jsonify(payload)

    @app.route("/api/simulation/control", methods=["POST"])
    def control_simulation():
        data = request.json or {}
        action = data.get("action")

        if action == "start":
            state.simulation_running = True
        elif action == "stop":
            state.simulation_running = False
        elif action == "reset":
            state.simulation_running = False
        elif action == "set_speed":
            state.simulation_speed = data.get("speed", 1.0)

        if action in ("stop", "reset"):
            # design_new.md:2235 — issue enter_safe_state when the runtime stops
            # driving outputs (block voltages 0, switch motors de-energized).
            sim = getattr(state, "sim_controller", None)
            if sim is not None:
                sim.enter_safe_state()

        return jsonify(
            {
                "status": "success",
                "running": state.simulation_running,
                "speed": state.simulation_speed,
            }
        )

    @app.route("/api/simulation/release-failed-runtime", methods=["POST"])
    def release_failed_runtime():
        """Clear a fatal physical-entry conflict halt after the operator has
        resolved the conflict.

        Delegates to
        ``SimulationController.release_failed_runtime_after_manual_cleanup()``,
        which re-verifies that the conflicting occupancy/reservation recorded
        in the stored fatal payload is actually gone before clearing the flag.
        Always returns HTTP 200; ``released`` reports whether the halt was
        actually lifted (``False`` means the conflict still stands and the
        operator must resolve it first).
        """
        sim = state.sim_controller
        if sim is None:
            return jsonify({"error": "Simulation not initialised"}), 503
        with state.simulation_lock:
            released = sim.release_failed_runtime_after_manual_cleanup()
        return jsonify(
            {
                "released": released,
                "failed_runtime": sim.failed_runtime,
            }
        )

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"ok": True})

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
