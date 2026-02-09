"""
Platform Management Routes - Platform CRUD and tile management.

This module provides the new /api/platform endpoints while preserving
behavior currently implemented for legacy /api/station endpoints.
"""

from flask import jsonify, request

from app_state import AppState


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
            platform = state.network.add_platform(
                x=x,
                y=y,
                platform_id=data.get("platform_id"),
                name=data.get("name", ""),
                length_cm=data.get("length_cm", 0.0),
                platform_side=data.get("platform_side", "both"),
                top_speed_kmh=data.get("top_speed_kmh"),
            )
            return jsonify(platform.to_dict_with_orientation(state.network))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/platform/<platform_id>", methods=["GET"])
    def get_platform(platform_id: str):
        platform = state.network.get_platform(platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404
        return jsonify(platform.to_dict_with_orientation(state.network))

    @app.route("/api/platform/<platform_id>", methods=["PATCH"])
    def update_platform(platform_id: str):
        platform = state.network.get_platform(platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404

        data = request.json or {}
        if "name" in data:
            platform.name = data["name"]
        if "length_cm" in data:
            platform.length_cm = data["length_cm"]
        if "platform_side" in data:
            from enums import PlatformSide

            platform.platform_side = PlatformSide(data["platform_side"])
        if "top_speed_kmh" in data:
            platform.top_speed_kmh = data["top_speed_kmh"]

        return jsonify(platform.to_dict_with_orientation(state.network))

    @app.route("/api/platform/<platform_id>", methods=["DELETE"])
    def delete_platform(platform_id: str):
        success = state.network.delete_platform(platform_id)
        if not success:
            return jsonify({"error": "Platform not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/platform/validate/<sint:x>/<sint:y>", methods=["GET"])
    def validate_platform_at(x: int, y: int):
        track = state.network.get_track(x, y)
        if not track:
            return jsonify(
                {"can_place": False, "error": f"No track at position ({x}, {y})"}
            )
        if track.is_switch:
            return jsonify(
                {"can_place": False, "error": "Cannot place platform on switch"}
            )
        if track.signal_ids:
            return jsonify(
                {
                    "can_place": False,
                    "error": "Cannot place platform on tile with signal",
                }
            )
        if track.station_id is not None:
            return jsonify(
                {
                    "can_place": False,
                    "error": f"Tile already belongs to platform {track.station_id}",
                }
            )
        return jsonify({"can_place": True, "existing_block": track.block_section_id})

    @app.route("/api/platform/<platform_id>/extend", methods=["POST"])
    def extend_platform(platform_id: str):
        data = request.json or {}
        positions = data.get("positions", [])
        if not positions:
            return jsonify({"error": "Missing positions"}), 400
        try:
            platform = state.network.extend_platform(
                platform_id, [tuple(pos) for pos in positions]
            )
            return jsonify(platform.to_dict_with_orientation(state.network))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/platform/<platform_id>/remove_tile", methods=["POST"])
    def remove_platform_tile(platform_id: str):
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        # Reuse existing legacy station endpoint behavior through network methods.
        platform = state.network.get_platform(platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404

        if (x, y) not in platform.positions:
            return jsonify(
                {"error": f"Position ({x}, {y}) is not part of platform {platform_id}"}
            ), 400

        new_positions = [p for p in platform.positions if p != (x, y)]
        if len(new_positions) > 1:
            is_continuous, errors = state.network._validate_station_continuity(
                new_positions
            )
            if not is_continuous:
                return jsonify(
                    {
                        "error": "Cannot remove tile: would break continuity",
                        "validation_errors": errors,
                    }
                ), 400

        platform.remove_position(x, y)
        track = state.network.get_track(x, y)
        if track and track.station_id == platform_id:
            track.station_id = None
            if track.block_section_id:
                state.network.remove_tile_from_block(x, y)

        if len(platform.positions) == 0:
            state.network.delete_platform(platform_id)
            return jsonify({"success": True, "platform_deleted": True})

        return jsonify(
            {
                "success": True,
                "platform": platform.to_dict_with_orientation(state.network),
            }
        )

    @app.route("/api/platform/<platform_id>/can_extend", methods=["POST"])
    def can_extend_platform(platform_id: str):
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        platform = state.network.get_platform(platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404

        track = state.network.get_track(x, y)
        if not track:
            return jsonify({"can_extend": False, "reason": "No track at position"})
        if track.is_switch:
            return jsonify(
                {"can_extend": False, "reason": "Cannot add switch to platform"}
            )
        if track.signal_ids:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Cannot add tile with signal to platform",
                }
            )
        if track.station_id is not None and track.station_id != platform_id:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": f"Tile already belongs to platform {track.station_id}",
                }
            )

        is_adjacent = any(
            abs(x - px) + abs(y - py) == 1 for px, py in platform.positions
        )
        if not is_adjacent and len(platform.positions) > 0:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Position must be adjacent to existing platform",
                }
            )

        all_positions = platform.positions + [(x, y)]
        is_continuous, errors = state.network._validate_station_continuity(
            all_positions
        )
        if not is_continuous:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": f"Would break continuity: {', '.join(errors)}",
                }
            )

        return jsonify({"can_extend": True})

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

        platform = state.network.get_platform(platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404
        if not state.network.get_block(block_id):
            return jsonify({"error": "Block not found"}), 404
        if not platform.assign_tile_to_block(x, y, block_id):
            return jsonify({"error": "Tile not in platform"}), 400

        return jsonify({"status": "success", "platform": platform.to_dict()})

    @app.route("/api/platform/<platform_id>/unassign_tile", methods=["POST"])
    def unassign_platform_tile_from_block(platform_id: str):
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        platform = state.network.get_platform(platform_id)
        if not platform:
            return jsonify({"error": "Platform not found"}), 404
        if not platform.unassign_tile_from_block(x, y):
            return jsonify(
                {"error": "Tile has no block assignment or not in platform"}
            ), 400

        return jsonify({"status": "success", "platform": platform.to_dict()})

    @app.route("/api/platform/<platform_id>/block_assignments", methods=["GET"])
    def get_platform_block_assignments(platform_id: str):
        platform = state.network.get_platform(platform_id)
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
