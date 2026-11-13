"""
Switch Routes - CRUD on Switch records and state changes.

Thin HTTP layer over `services.switch_service`: parse JSON, call the
service, translate `ServiceError` subclasses to status codes
(ValidationError -> 400, NotFoundError -> 404, ConflictError -> 409,
ServiceUnavailableError -> 503). The service routes position changes
through SwitchManager so BlockManager notifications stay paired with
position writes.
"""

from routes import running_trains
from flask import jsonify, request

from app_state import AppState
from services import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from services import switch_service
from services.notifications import emit_command_result


def register(app, state: AppState) -> None:
    """Register switch routes."""

    @app.route("/api/switches", methods=["GET"])
    def get_switches():
        """Get all switch configurations."""
        return jsonify(
            [s.to_dict(state.network) for s in switch_service.list_switches(state)]
        )

    @app.route("/api/switches/<switch_id>", methods=["GET"])
    def get_switch(switch_id: str):
        """Get specific switch configuration."""
        switch = switch_service.get_switch(state, switch_id)
        if not switch:
            return jsonify({"error": "Switch not found"}), 404
        return jsonify(switch.to_dict(state.network))

    @app.route("/api/tracks/<int:x>/<int:y>/switch", methods=["POST"])
    def create_switch_at_track(x: int, y: int):
        """Create a switch configuration for a switch-track tile.

        Auto-created by the frontend when a switch track is placed.
        Returns 201 when freshly created, 200 when one already exists
        (idempotent), 404 if there is no track, 400 if the track is not a
        switch.
        """
        data = request.get_json(silent=True) or {}
        try:
            already_existed = switch_service.get_switch_at(state, x, y) is not None
            switch = switch_service.create_switch_at_track(state, x, y, data=data)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        status = 200 if already_existed else 201
        return jsonify(switch.to_dict(state.network)), status

    @app.route("/api/switches/<switch_id>", methods=["PUT"])
    def update_switch(switch_id: str):
        """Update switch configuration.

        Per design_new.md "Availability flag REST API surface": setting
        `disabled = True` runs the admin-reject preconditions (occupancy /
        future locks / train-protection) and returns 409 on rejection;
        setting `manual` always succeeds. A real `disabled` change also
        fires `BlockManager.on_switch_position_changed(switch_id)`.
        """
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "No data provided"}), 400
        try:
            with state.simulation_lock:
                switch = switch_service.update_switch(state, switch_id, data=data)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            # design_new.md "Availability flag REST API surface": the flag PATCH emits a command_result even
            # on the 409 admin-reject, carrying the rejection reason.
            emit_command_result(
                state, source="Switch", entity_id_key="switch_id",
                entity_id=switch_id, command_name="update_flags",
                success=False, reason=str(exc),
            )
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        emit_command_result(
            state, source="Switch", entity_id_key="switch_id",
            entity_id=switch_id, command_name="update_flags", success=True,
        )
        return jsonify(switch.to_dict(state.network))

    @app.route("/api/switches/<switch_id>", methods=["PATCH"])
    def patch_switch_availability(switch_id: str):
        """PATCH mirror of PUT for the availability flags (`disabled`,
        `manual`), so the entity-editor UI can use a uniform PATCH endpoint
        per design_new.md "Availability flag REST API surface"."""
        return update_switch(switch_id)

    @app.route("/api/switches/<switch_id>", methods=["DELETE"])
    def delete_switch(switch_id: str):
        """Remove switch configuration."""
        try:
            switch_service.delete_switch(state, switch_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"success": True})

    @app.route("/api/switches/<switch_id>/alternate", methods=["PUT"])
    def set_alternate(switch_id: str):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            return jsonify({"error": "enabled must be true or false"}), 400
        with state.simulation_lock:
            runtime = running_trains._runtime(state)
            if runtime is None or not state.session_state().is_live:
                return jsonify({"error": "The session must be Live"}), 409
            switch = state.network.switches.get(switch_id)
            if switch is None:
                return jsonify({"error": "Switch not found"}), 404
            if data["enabled"]:
                if switch.disabled or switch.manual or switch.motor_unavailable:
                    return jsonify({"error": "This switch is unavailable for automatic throws"}), 409
                if not runtime.enable_alternation(switch_id):
                    return jsonify({"error": "Each leg of this switch must lead to a block of its own"}), 409
            else:
                runtime.disable_alternation(switch_id)
            return jsonify({"alternate_next": runtime.alternate_next.get(switch_id)})

    @app.route("/api/switches/<switch_id>/state", methods=["PUT"])
    def change_switch_state(switch_id: str):
        """Change switch state (MAIN=0, DIVERGE=1).

        Validation errors (unknown switch, invalid state) return 400/404
        synchronously. The actual command result surfaces through
        NotificationCenter as a `command_result` event with source="Switch".

        Request body: {"state": 0 or 1, "command_id": "<optional>",
        "manual_override": <optional bool>}

        Returns:
            202: {"command_id": "...", "status": "queued"}
            400: missing/invalid state
            404: switch not found
            503: SwitchManager not available
        """
        data = request.get_json(silent=True) or {}
        if "state" not in data:
            return jsonify({"error": "Missing 'state' in request body"}), 400
        try:
            with state.simulation_lock:
                runtime = running_trains._runtime(state)
                if runtime is not None and state.session_state().is_live and switch_id in runtime.held_switch_ids():
                    return jsonify({
                        "success": False,
                        "reason": "switch_held",
                        "error": "A train may be on this switch, so it is not thrown.",
                    }), 409
                command_id = switch_service.request_position_change(
                    state,
                    switch_id,
                    new_state=data["state"],
                    command_id=data.get("command_id"),
                    manual_override=bool(data.get("manual_override", False)),
                )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ServiceUnavailableError as exc:
            return jsonify({"error": str(exc)}), 503

        return jsonify({"command_id": command_id, "status": "queued"}), 202
