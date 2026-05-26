"""
Switch Routes - CRUD on Switch records and state changes routed through
SwitchManager so BlockManager notifications stay paired with position writes.
"""

import uuid

from flask import jsonify, request

from app_state import AppState


def register(app, state: AppState) -> None:
    """Register switch routes."""

    @app.route("/api/switches", methods=["GET"])
    def get_switches():
        """Get all switch configurations."""
        return jsonify(
            [s.to_dict(state.network) for s in state.network.switches.values()]
        )

    @app.route("/api/switches/<switch_id>", methods=["GET"])
    def get_switch(switch_id: str):
        """Get specific switch configuration."""
        switch = state.network.get_switch(switch_id)
        if not switch:
            return jsonify({"error": "Switch not found"}), 404
        return jsonify(switch.to_dict(state.network))

    @app.route("/api/tracks/<int:x>/<int:y>/switch", methods=["POST"])
    def create_switch_at_track(x: int, y: int):
        """
        Create a switch configuration for a track tile.

        User requirement: Auto-created when switch track is placed.
        This endpoint is called automatically by the frontend.
        """
        try:
            track = state.network.get_track(x, y)
            if not track:
                return jsonify({"error": "No track at position"}), 404
            if not track.is_switch:
                return jsonify({"error": "Track is not a switch"}), 400

            # Check if switch already exists
            existing = state.network.get_switch_at(x, y)
            if existing:
                # Return existing switch (idempotent)
                return jsonify(existing.to_dict(state.network)), 200

            # Generate switch ID
            switch_id = f"SW_{x}_{y}"

            from switch import Switch

            data = request.json if request.json else {}

            # User requirement: Default to 0.0 cm for both branches
            switch = Switch(
                switch_id=switch_id,
                x=x,
                y=y,
                main_length_cm=data.get("main_length_cm", 0.0),
                diverge_length_cm=data.get("diverge_length_cm", 0.0),
                main_radius_cm=data.get("main_radius_cm"),
                diverge_radius_cm=data.get("diverge_radius_cm"),
                name=data.get("name", ""),
                switch_type=data.get("switch_type", "standard"),
                notes=data.get("notes", ""),
            )

            state.network.add_switch(switch)

            return jsonify(switch.to_dict(state.network)), 201
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>", methods=["PUT"])
    def update_switch(switch_id: str):
        """Update switch configuration."""
        try:
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400

            switch = state.network.update_switch(switch_id, **data)
            if not switch:
                return jsonify({"error": "Switch not found"}), 404

            return jsonify(switch.to_dict(state.network))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>", methods=["DELETE"])
    def delete_switch(switch_id: str):
        """Remove switch configuration."""
        try:
            success = state.network.remove_switch(switch_id)
            if not success:
                return jsonify({"error": "Switch not found"}), 404

            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>/state", methods=["PUT"])
    def change_switch_state(switch_id: str):
        """
        Change switch state (MAIN=0, DIVERGE=1).

        Validation errors (unknown switch, invalid state) return 400/404
        synchronously. The actual command result surfaces through
        NotificationCenter as a `command_result` event with
        source="Switch".

        Request body:
            {"state": 0 or 1, "command_id": "<optional uuid>"}

        Returns:
            202: {"command_id": "...", "status": "queued"}
            404: {"error": "Switch not found"}
            400: {"error": "..."}
            503: {"error": "SwitchManager not available"}
        """
        try:
            data = request.json
            if not data or "state" not in data:
                return jsonify({"error": "Missing 'state' in request body"}), 400

            new_state = data["state"]
            if new_state not in (0, 1):
                return jsonify(
                    {"error": "Invalid state value. Must be 0 (MAIN) or 1 (DIVERGE)"}
                ), 400

            switch = state.network.switches.get(switch_id)
            if not switch:
                return jsonify({"error": f"Switch {switch_id} not found"}), 404

            switch_manager = state.network.switch_manager
            if switch_manager is None:
                return (
                    jsonify(
                        {"error": "SwitchManager not available — runtime not initialised"}
                    ),
                    503,
                )

            command_id = data.get("command_id") or str(uuid.uuid4())

            # Route through SwitchManager so the BlockManager notification is
            # paired with the position write.
            success, message = switch_manager.request_position_change(
                switch_id, new_state
            )

            # Emit command_result through NotificationCenter so callers observe
            # the outcome via the event stream rather than a sync HTTP payload.
            try:
                from notification_center import Severity as _Severity

                sc = getattr(state, "sim_controller", None)
                nc = getattr(sc, "notification_center", None)
                event_time = getattr(sc, "simulation_time", 0.0) or 0.0
                if nc is not None:
                    severity = _Severity.INFO if success else _Severity.WARNING
                    nc.emit(
                        source="Switch",
                        kind="command_result",
                        severity=severity,
                        event_time=event_time,
                        payload={
                            "command_id": command_id,
                            "command_name": "change_state",
                            "switch_id": switch_id,
                            "success": success,
                            "reason": message,
                        },
                    )
            except Exception:
                pass  # notification failure must not affect the HTTP response

            return jsonify({"command_id": command_id, "status": "queued"}), 202

        except Exception as e:
            return jsonify({"error": str(e)}), 500
