"""Tunnel Routes — CRUD on the network's tunnel table.

Tunnels pair two dead-end edges so they behave as a single tile-to-tile
connection. See design_v2.md "Tunnel" for the invariants.

A request that changes the tunnel table holds `state.simulation_lock` while it
writes (design_v2.md "About this document").
"""

from flask import jsonify, request

from app_state import AppState


def register(app, state: AppState) -> None:
    """Register tunnel routes."""

    @app.route("/api/tunnels", methods=["GET"])
    def list_tunnels():
        return jsonify(state.network.tunnels_to_list())

    @app.route("/api/tunnels", methods=["POST"])
    def create_tunnel():
        data = request.get_json(silent=True) or {}
        try:
            a = data["a"]
            b = data["b"]
            with state.simulation_lock:
                state.network.add_tunnel(
                    (a["x"], a["y"], a["edge"]),
                    (b["x"], b["y"], b["edge"]),
                )
        except (KeyError, TypeError) as exc:
            return jsonify({"error": f"Malformed tunnel body: {exc}"}), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(state.network.tunnels_to_list()), 201

    @app.route("/api/tunnels", methods=["DELETE"])
    def delete_tunnel():
        data = request.get_json(silent=True) or {}
        try:
            x = int(data["x"])
            y = int(data["y"])
            edge = data["edge"]
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": f"Malformed delete body: {exc}"}), 400
        with state.simulation_lock:
            removed = state.network.remove_tunnel((x, y, edge))
        if not removed:
            return jsonify({"error": "No tunnel at that endpoint"}), 404
        return jsonify(state.network.tunnels_to_list()), 200
