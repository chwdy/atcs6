"""
Miscellaneous Routes - Static files, network, simulation control, persistence.
"""

from datetime import datetime
from flask import jsonify, request, send_from_directory

from app_state import AppState
from simulation import build_network_payload


def register(app, state: AppState) -> None:
    """Register miscellaneous routes."""

    @app.route("/")
    def index():
        return send_from_directory("static", "index.html")

    @app.route("/<path:path>")
    def static_files(path):
        return send_from_directory("static", path)

    @app.route("/api/network", methods=["GET"])
    def get_network():
        return jsonify(build_network_payload(state.network, state.sim_controller))

    @app.route("/api/simulation/start", methods=["POST"])
    def start_simulation():
        import server

        server.simulation_running = True
        return jsonify({"status": "success", "running": True})

    @app.route("/api/simulation/stop", methods=["POST"])
    def stop_simulation():
        import server

        server.simulation_running = False
        return jsonify({"status": "success", "running": False})

    @app.route("/api/simulation/speed", methods=["POST"])
    def set_simulation_speed():
        import server

        data = request.json or {}
        speed = data.get("speed", 1.0)
        server.simulation_speed = float(speed)
        return jsonify({"status": "success", "speed": server.simulation_speed})

    @app.route("/api/simulation/status", methods=["GET"])
    def get_simulation_status():
        import server

        return jsonify(
            {
                "running": server.simulation_running,
                "speed": server.simulation_speed,
            }
        )

    @app.route("/api/layouts", methods=["GET"])
    def list_layouts():
        layouts = state.persistence_manager.list_layouts()
        return jsonify(layouts)

    @app.route("/api/layouts/<name>", methods=["GET"])
    def load_layout(name):
        import server

        with state.simulation_lock:
            result = state.persistence_manager.load_merged_layout(name, state.network)
            if "error" in result:
                return jsonify(result), 404
            server.network = result["network"]
            state.path_dispatcher.network = server.network
            state.sim_controller.network = server.network
        return jsonify(result)

    @app.route("/api/layouts/<name>", methods=["POST"])
    def save_layout(name):
        data = request.json or {}
        description = data.get("description", "")
        with state.simulation_lock:
            result = state.persistence_manager.save_merged_layout(
                name, state.network, description
            )
        return jsonify(result)

    @app.route("/api/layouts/<name>", methods=["DELETE"])
    def delete_layout(name):
        result = state.persistence_manager.delete_layout(name)
        if "error" in result:
            return jsonify(result), 404
        return jsonify(result)
