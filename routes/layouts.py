"""
Layout Persistence Routes - Save, load, list, and delete saved layouts.

Thin HTTP layer over `services.layout_service`.
"""

from flask import jsonify, request

from app_state import AppState
from services import NotFoundError, ValidationError
from services import layout_service


def register(app, state: AppState) -> None:
    """Register layout-persistence routes."""

    @app.route("/api/layouts", methods=["GET"])
    def list_layouts():
        """List all saved layouts."""
        layouts = layout_service.list_layouts(state)
        return jsonify({"layouts": layouts})

    @app.route("/api/layouts", methods=["POST"])
    def save_layout():
        """Save current network state as a layout.

        Request body:
            name: Layout name (required)
            description: Optional description

        Saves both snapshot and steps in a single merged file.
        """
        data = request.get_json() or {}
        name = data.get("name", "")
        description = data.get("description", "")

        try:
            result = layout_service.save_layout(
                state, name=name, description=description
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except IOError as exc:
            return jsonify({"error": str(exc)}), 500

        response = {
            "status": "success",
            "message": f"Layout '{name.strip()}' saved successfully",
            "path": result["path"],
            "steps_generated": result["steps_generated"],
            "steps_count": result["steps_count"],
        }

        # Notify user if steps failed to generate
        if result["steps_error"]:
            response["warning"] = (
                f"Steps could not be generated: {result['steps_error']}"
            )

        return jsonify(response)

    @app.route("/api/layouts/<name>/load", methods=["POST"])
    def load_layout(name):
        """Load a layout, replacing the current network state.

        Uses snapshot by default, falls back to steps if snapshot fails.
        """
        try:
            result = layout_service.load_layout(state, name)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        response = {
            "status": "success",
            "message": f"Layout '{name}' loaded successfully",
            "method": result.get("method"),
        }
        if result.get("recovery"):
            response["recovery"] = True
            response["message"] = (
                f"Layout '{name}' recovered from steps (snapshot was corrupted)"
            )

        return jsonify(response)

    @app.route("/api/layouts/<name>", methods=["DELETE"])
    def delete_layout(name):
        """Delete a saved layout."""
        try:
            layout_service.delete_layout(state, name)
        except NotFoundError:
            return jsonify({"error": f"Layout '{name}' not found"}), 404
        return jsonify(
            {"status": "success", "message": f"Layout '{name}' deleted"}
        )
