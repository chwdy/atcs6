"""
Speed-Profile Routes - CRUD for ``SpeedProfile`` records that govern
acceleration/deceleration/voltage-curve behavior per train.

Thin HTTP layer over `services.speed_profile_service`: parse JSON, call
the service, translate `ServiceError` subclasses to status codes
(ValidationError -> 400, NotFoundError -> 404, ConflictError -> 409,
ServiceError -> 500).
"""

from flask import jsonify, request

from app_state import AppState
from services import ConflictError, NotFoundError, ServiceError, ValidationError
from services import speed_profile_service


def register(app, state: AppState) -> None:
    """Register speed-profile routes."""

    @app.route("/api/speed-profiles", methods=["GET"])
    def get_speed_profiles():
        """List all speed profiles as ``{profiles: [...], count: int}``."""
        profiles = [p.to_dict() for p in speed_profile_service.list_profiles(state)]
        return jsonify({"profiles": profiles, "count": len(profiles)})

    @app.route("/api/speed-profiles", methods=["POST"])
    def create_speed_profile():
        """Create a new speed profile.

        Returns 201 with the profile, 400 on bad input, 409 if the
        profile_id already exists.
        """
        try:
            profile = speed_profile_service.create_profile(
                state, data=request.get_json(silent=True) or {}
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409

        return jsonify(profile.to_dict()), 201

    @app.route("/api/speed-profiles/<profile_id>", methods=["GET"])
    def get_speed_profile(profile_id: str):
        """Get a single speed profile by ID (404 if absent).

        With ``?resolved=true`` it returns the profile as the Speed Profile
        panel shows it: resolved values plus the stored ``derived_from`` and
        the instance's ``own_fields`` (design_new.md "SpeedProfile", Editing a
        profile by hand). Without it, the stored record.
        """
        if request.args.get("resolved") in ("1", "true"):
            view = speed_profile_service.resolved_view(state, profile_id)
            if view is None:
                return jsonify({"error": f"Speed profile '{profile_id}' not found"}), 404
            return jsonify(view)
        profile = speed_profile_service.get_profile(state, profile_id)
        if not profile:
            return jsonify({"error": f"Speed profile '{profile_id}' not found"}), 404
        return jsonify(profile.to_dict())

    @app.route("/api/speed-profiles/<profile_id>", methods=["PUT"])
    def update_speed_profile(profile_id: str):
        """Update an existing speed profile.

        Returns 200 with the updated profile, 400 on bad input, 404 if the
        profile_id does not exist.
        """
        try:
            profile = speed_profile_service.update_profile(
                state, profile_id, data=request.get_json(silent=True) or {}
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

        return jsonify(profile.to_dict())

    @app.route("/api/speed-profiles/<profile_id>", methods=["DELETE"])
    def delete_speed_profile(profile_id: str):
        """Delete a speed profile.

        Returns 204 on success, 404 if absent, 409 if it is the default
        profile or is currently assigned to a train.
        """
        try:
            speed_profile_service.delete_profile(state, profile_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ServiceError as exc:
            return jsonify({"error": str(exc)}), 500

        return "", 204
