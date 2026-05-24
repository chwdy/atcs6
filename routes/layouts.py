"""
Layout Persistence Routes - Save, load, list, and delete saved layouts.
"""

import time

from flask import jsonify, request

from app_state import AppState


def register(app, state: AppState) -> None:
    """Register layout-persistence routes."""

    @app.route("/api/layouts", methods=["GET"])
    def list_layouts():
        """List all saved layouts."""
        layouts = state.persistence_manager.list_layouts()
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
        name = data.get("name", "").strip()
        description = data.get("description", "")

        if not name:
            return jsonify({"error": "Layout name is required"}), 400

        was_running = state.simulation_running
        state.simulation_running = False

        try:
            with state.simulation_lock:
                result = state.persistence_manager.save_merged_layout(
                    name, state.network, description
                )
                state.last_autosave_time = time.time()

            response = {
                "status": "success",
                "message": f"Layout '{name}' saved successfully",
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
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except IOError as e:
            return jsonify({"error": str(e)}), 500
        finally:
            state.simulation_running = was_running

    @app.route("/api/layouts/<name>/load", methods=["POST"])
    def load_layout(name):
        """Load a layout, replacing the current network state.

        Uses snapshot by default, falls back to steps if snapshot fails.
        """
        was_running = state.simulation_running
        state.simulation_running = False

        try:
            with state.simulation_lock:
                result = state.persistence_manager.load_layout_with_fallback(
                    name, state.network
                )

                if not result.get("success"):
                    return jsonify(
                        {
                            "error": result.get("error", "Failed to load layout"),
                            "method": result.get("method"),
                        }
                    ), 400

                state.sim_controller.network = state.network
                state.last_autosave_time = time.time()

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
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            state.simulation_running = was_running

    @app.route("/api/layouts/<name>", methods=["DELETE"])
    def delete_layout(name):
        """Delete a saved layout."""
        if state.persistence_manager.delete_layout(name):
            return jsonify({"status": "success", "message": f"Layout '{name}' deleted"})
        else:
            return jsonify({"error": f"Layout '{name}' not found"}), 404
