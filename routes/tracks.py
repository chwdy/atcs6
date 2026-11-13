"""
Track Routes - Track CRUD and bulk operations.

Thin HTTP layer over `services.track_service`: parse JSON, call the
service, translate typed exceptions (NotFoundError -> 404,
ValidationError -> 400) and the tagged update outcomes to their exact
responses.
"""

from flask import jsonify, request

from app_state import AppState
from services import NotFoundError, ValidationError
from services import track_service


def register(app, state: AppState) -> None:
    """Register track routes."""

    @app.route("/api/track/<sint:x>/<sint:y>", methods=["GET"])
    def get_track(x, y):
        track = track_service.get_track(state, x, y)
        if not track:
            return jsonify({"error": "Track not found"}), 404
        return jsonify(track.to_dict())

    @app.route("/api/track/<sint:x>/<sint:y>", methods=["PATCH"])
    def update_track(x, y):
        data = request.json or {}
        force = bool(data.get("force", False)) if isinstance(data, dict) else False

        try:
            result = track_service.update_track(state, x, y, data=data, force=force)
        except NotFoundError:
            return jsonify({"error": "Track not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        status = result["status"]
        if status == "bits_rejected":
            return jsonify({"success": False, "error": result["message"]}), 400
        if status == "needs_confirmation":
            return jsonify(
                {"requires_confirmation": True, "impact": result["impact"]}
            ), 409
        if status == "switch_change_rejected":
            return jsonify({"error": result["reason"]}), 409
        if status == "removed":
            return jsonify(
                {"status": "success", "message": f"Track at ({x}, {y}) removed (empty)"}
            )
        return jsonify({"status": "success", "track": result["track"].to_dict()})

    @app.route("/api/track/<sint:x>/<sint:y>", methods=["DELETE"])
    def delete_track(x, y):
        try:
            track_service.delete_track(state, x, y)
        except NotFoundError:
            return jsonify({"error": "Track not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {"status": "success", "message": f"Track at ({x}, {y}) removed"}
        )

    @app.route("/api/tracks/bulk", methods=["POST"])
    def add_tracks_bulk():
        data = request.json or {}
        try:
            result = track_service.add_tracks_bulk(
                state, tracks_data=data.get("tracks", [])
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        affected = result["added"] + result["updated"]
        return jsonify(
            {
                "status": "success",
                "added": len(result["added"]),
                "updated": len(result["updated"]),
                "tracks": [t.to_dict() for t in affected],
            }
        )

    @app.route("/api/tracks/bulk-delete", methods=["POST"])
    def delete_tracks_bulk():
        data = request.json or {}
        force = bool(data.get("force", False))
        try:
            result = track_service.delete_tracks_bulk(
                state, positions=data.get("positions", []), force=force
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        if result["status"] == "needs_confirmation":
            return jsonify(
                {"requires_confirmation": True, "impact": result["impact"]}
            ), 409
        return jsonify({"status": "success", "deleted": len(result["deleted"])})
