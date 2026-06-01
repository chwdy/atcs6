"""
Platform Management Routes - Platform CRUD and tile management.

Thin HTTP layer over `services.platform_service`: parse JSON, call the
service, translate typed exceptions (NotFoundError -> 404,
ValidationError -> 400), and serialize. Read-only endpoints keep their
serialization here.
"""

from flask import jsonify, request

from app_state import AppState
from services import NotFoundError, ValidationError
from services import platform_service


def register(app, state: AppState) -> None:
    """Register platform routes."""

    @app.route("/api/platform", methods=["POST"])
    def create_platform():
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        try:
            platform = platform_service.create_platform(
                state,
                x=x,
                y=y,
                platform_id=data.get("platform_id"),
                name=data.get("name", ""),
                length_cm=data.get("length_cm", 0.0),
                platform_side=data.get("platform_side", "both"),
                top_speed_kmh=data.get("top_speed_kmh"),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(platform.to_dict_with_orientation(state.network))

    @app.route("/api/platform/<platform_id>", methods=["GET"])
    def get_platform(platform_id: str):
        platform = platform_service.get_platform(state, platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404
        return jsonify(platform.to_dict_with_orientation(state.network))

    @app.route("/api/platform/<platform_id>", methods=["PATCH"])
    def update_platform(platform_id: str):
        data = request.json or {}
        try:
            platform = platform_service.update_platform(
                state, platform_id, data=data
            )
        except NotFoundError:
            return jsonify({"error": "Platform not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(platform.to_dict_with_orientation(state.network))

    @app.route("/api/platform/<platform_id>", methods=["DELETE"])
    def delete_platform(platform_id: str):
        try:
            platform_service.delete_platform(state, platform_id)
        except NotFoundError:
            return jsonify({"error": "Platform not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/platform/validate/<sint:x>/<sint:y>", methods=["GET"])
    def validate_platform_at(x: int, y: int):
        return jsonify(platform_service.validate_placement(state, x, y))

    @app.route("/api/platform/<platform_id>/extend", methods=["POST"])
    def extend_platform(platform_id: str):
        data = request.json or {}
        positions = data.get("positions", [])
        if not positions:
            return jsonify({"error": "Missing positions"}), 400
        try:
            platform = platform_service.extend_platform(
                state, platform_id, positions=positions
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(platform.to_dict_with_orientation(state.network))

    @app.route("/api/platform/<platform_id>/remove_tile", methods=["POST"])
    def remove_platform_tile(platform_id: str):
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        try:
            result = platform_service.remove_tile(state, platform_id, x=x, y=y)
        except NotFoundError:
            return jsonify({"error": "Platform not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        if result["status"] == "continuity_error":
            return jsonify(
                {
                    "error": "Cannot remove tile: would break continuity",
                    "validation_errors": result["errors"],
                }
            ), 400
        if result["status"] == "deleted":
            return jsonify({"success": True, "platform_deleted": True})
        return jsonify(
            {
                "success": True,
                "platform": result["platform"].to_dict_with_orientation(
                    state.network
                ),
            }
        )

    @app.route("/api/platform/<platform_id>/can_extend", methods=["POST"])
    def can_extend_platform(platform_id: str):
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400
        try:
            result = platform_service.can_extend(state, platform_id, x=x, y=y)
        except NotFoundError:
            return jsonify({"error": "Platform not found"}), 404
        return jsonify(result)

    @app.route("/api/platform/<platform_id>/assign_tile", methods=["POST"])
    def assign_platform_tile_to_block(platform_id: str):
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        block_id = data.get("block_id")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400
        if not block_id:
            return jsonify({"error": "Missing block_id"}), 400

        try:
            platform = platform_service.assign_tile(
                state, platform_id, x=x, y=y, block_id=block_id
            )
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success", "platform": platform.to_dict()})

    @app.route("/api/platform/<platform_id>/unassign_tile", methods=["POST"])
    def unassign_platform_tile_from_block(platform_id: str):
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        try:
            platform = platform_service.unassign_tile(state, platform_id, x=x, y=y)
        except NotFoundError:
            return jsonify({"error": "Platform not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success", "platform": platform.to_dict()})

    @app.route("/api/platform/<platform_id>/block_assignments", methods=["GET"])
    def get_platform_block_assignments(platform_id: str):
        platform = platform_service.get_platform(state, platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404

        assignments = [
            {"x": x, "y": y, "block_id": bid}
            for (x, y), bid in platform.block_assignments.items()
        ]
        blocks_summary = {}
        for block_id in platform.get_all_assigned_blocks():
            tiles = platform.get_tiles_for_block(block_id)
            blocks_summary[block_id] = {
                "tile_count": len(tiles),
                "tiles": [{"x": x, "y": y} for x, y in tiles],
            }

        return jsonify(
            {
                "platform_id": platform_id,
                "assignments": assignments,
                "blocks_summary": blocks_summary,
            }
        )
