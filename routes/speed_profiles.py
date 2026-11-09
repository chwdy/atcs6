"""
Speed-profile routes: the library of models and sets, and hand edits.

Thin HTTP layer over `services.speed_profile_service`: parse JSON, call the
service, translate `ServiceError` subclasses to status codes (ValidationError
-> 400, NotFoundError -> 404, ConflictError -> 409). Profile and model ids are
numbers (design_new.md "SpeedProfile", Profile and model ids are numbers).

There is no request that creates a profile or a model: a set, and a new model
for it, come only from a finished speed pass's Promote (design_new.md
"SpeedProfile", Promoting an instance profile), and a train's instance profile
only from placing or adopting the train.
"""

from flask import jsonify, request

from app_state import AppState
from services import ConflictError, NotFoundError, ValidationError
from services import speed_profile_service


def register(app, state: AppState) -> None:
    """Register speed-profile and locomotive-model routes."""

    @app.route("/api/speed-profiles", methods=["GET"])
    def get_speed_profiles():
        """List every profile as ``{profiles: [...], models: [...], count}``."""
        profiles = [p.to_dict() for p in speed_profile_service.list_profiles(state)]
        models = [m.to_dict() for m in speed_profile_service.list_models(state)]
        return jsonify({"profiles": profiles, "models": models, "count": len(profiles)})

    @app.route("/api/speed-profiles/<int:profile_id>", methods=["GET"])
    def get_speed_profile(profile_id: int):
        """One profile as the Speed Profile panel shows it (404 if absent)."""
        view = speed_profile_service.profile_view(state, profile_id)
        if view is None:
            return jsonify({"error": f"Speed profile {profile_id} not found"}), 404
        return jsonify(view)

    @app.route("/api/speed-profiles/<int:profile_id>", methods=["PUT"])
    def update_speed_profile(profile_id: int):
        """Hand-edit a set or a train's instance profile."""
        try:
            profile = speed_profile_service.update_profile(
                state, profile_id, data=request.get_json(silent=True) or {}
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(profile.to_dict())

    @app.route("/api/speed-profiles/<int:profile_id>", methods=["DELETE"])
    def delete_speed_profile(profile_id: int):
        """Delete a set. 204 on success, 404 if absent, 409 for the Default
        profile or a train's instance profile."""
        try:
            speed_profile_service.delete_profile(state, profile_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        return "", 204

    @app.route("/api/locomotive-models", methods=["GET"])
    def get_locomotive_models():
        models = [m.to_dict() for m in speed_profile_service.list_models(state)]
        return jsonify({"models": models})

    @app.route("/api/locomotive-models/<int:model_id>", methods=["PUT"])
    def rename_locomotive_model(model_id: int):
        """Rename a model: body ``{"name": str}``."""
        body = request.get_json(silent=True) or {}
        try:
            model = speed_profile_service.rename_model(
                state, model_id, name=body.get("name")
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(model.to_dict())

    @app.route("/api/locomotive-models/<int:model_id>", methods=["DELETE"])
    def delete_locomotive_model(model_id: int):
        """Delete a model and every set naming it."""
        try:
            removed = speed_profile_service.delete_model(state, model_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"status": "success", "removed_profile_ids": removed})
