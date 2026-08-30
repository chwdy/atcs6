"""
Segment Geometry Routes - the geometry axis of the track model.

Thin HTTP layer over `services.segment_service`: parse JSON, call the service,
translate typed exceptions (ValidationError -> 400, NotFoundError -> 404), and
serialize. A `Segment` owns geometry only (`ordered_tiles`, `length_cm`,
`min_radius_cm`, `grade_percent`, `speed_limit_kmh`); occupancy / reservation /
power / grouping live on its owning `Block` under `/api/blocks`.
"""

from flask import jsonify, request

from app_state import AppState
from routes._positions import sort_positions_traversal
from services import NotFoundError, ValidationError
from services import segment_service


def register(app, state: AppState) -> None:
    """Register segment geometry routes."""

    def _serialize(segment) -> dict:
        """Segment geometry dict + owning block id + tile positions."""
        seg_dict = segment.to_dict()
        segment_id = segment.segment_id
        seg_dict["block_id"] = state.network.segment_to_block_id(segment_id)
        positions = state.network.get_segment_positions(segment_id)
        seg_dict["track_count"] = len(positions)
        seg_dict["track_positions"] = positions
        return seg_dict

    @app.route("/api/segments", methods=["GET"])
    def get_segments():
        """List all segments with their tile positions and owning block."""
        return jsonify(
            [_serialize(segment) for segment in segment_service.list_segments(state)]
        )

    @app.route("/api/segments/<segment_id>", methods=["GET"])
    def get_segment(segment_id: str):
        """Get one segment's geometry, continuity status and owning block."""
        segment = segment_service.get_segment(state, segment_id)
        if segment is None:
            return jsonify({"error": "Segment not found"}), 404
        result = _serialize(segment)
        is_continuous, errors = state.network.validate_segment_continuity(segment_id)
        result["is_continuous"] = is_continuous
        result["validation_errors"] = errors
        return jsonify(result)

    @app.route("/api/segments", methods=["POST"])
    def create_segment():
        """Create a segment holding the given track positions.

        Request body: { "segment_id": str?, "positions": [[x,y], ...],
        "name": str?, "length_cm": float?, "speed_limit_kmh": float? }
        """
        data = request.json or {}
        positions = data.get("positions", [])
        if not positions:
            return jsonify({"error": "positions required"}), 400
        try:
            block = segment_service.create_segment_with_tracks(
                state,
                segment_id=data.get("segment_id"),
                positions=positions,
                name=data.get("name", ""),
                length_cm=data.get("length_cm", 0.0),
                speed_limit_kmh=data.get("speed_limit_kmh", data.get("max_speed_kmh")),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        # A fresh segment gets its own block; the new segment is that block's
        # sole member.
        return jsonify(_serialize(block.segments[0]))

    @app.route("/api/segments/<segment_id>", methods=["PATCH"])
    def update_segment(segment_id: str):
        """Update a segment's geometry (length / radius / grade / speed limit).

        Mutations happen under `state.simulation_lock` so the per-tick phase
        loop cannot observe a torn read.
        """
        data = request.json or {}
        try:
            segment = segment_service.update_segment(state, segment_id, data=data)
        except NotFoundError:
            return jsonify({"error": "Segment not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(_serialize(segment))

    @app.route("/api/segments/<segment_id>", methods=["DELETE"])
    def delete_segment(segment_id: str):
        """Delete a segment's geometry (and its owning block if last member)."""
        try:
            segment_service.delete_segment(state, segment_id)
        except NotFoundError:
            return jsonify({"error": "Segment not found"}), 404
        return jsonify(
            {"status": "success", "message": f"Segment {segment_id} deleted"}
        )

    @app.route("/api/segments/<segment_id>/reverse-orientation", methods=["POST"])
    def reverse_segment(segment_id: str):
        """Flip a segment's stored tile order end-for-end.

        Geometry lives on the segment, so this is the segment-axis write that
        aligns a member stored the opposite way round a run. Grouping
        (`POST /api/blocks`) deliberately will not do this for you: it edits
        block membership only and rejects a misoriented member instead of
        rewriting its geometry.
        """
        try:
            changed = segment_service.reverse_segment(state, segment_id)
        except NotFoundError:
            return jsonify({"error": "Segment not found"}), 404
        return jsonify(
            {
                "status": "success",
                "segment_id": segment_id,
                "reversed": changed,
            }
        )

    @app.route("/api/segments/<segment_id>/extend", methods=["POST"])
    def extend_segment_endpoint(segment_id: str):
        """Extend a segment with new track positions.

        Response includes 'invalidated_signals' when force=true cleared any
        signals in the path.
        """
        data = request.json or {}
        positions = data.get("positions", [])
        if not positions:
            return jsonify({"error": "positions required"}), 400
        try:
            block = segment_service.extend_segment(
                state,
                segment_id,
                positions=positions,
                force=data.get("force", False),
                force_overwrite=data.get("force_overwrite", False),
            )
        except NotFoundError:
            return jsonify({"error": "Segment not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        segment = state.network.get_segment(segment_id)
        response = _serialize(segment)
        if hasattr(block, "_invalidated_signals") and block._invalidated_signals:
            response["invalidated_signals"] = block._invalidated_signals
            del block._invalidated_signals
        return jsonify(response)

    @app.route("/api/segments/<segment_id>/tracks", methods=["POST"])
    def add_tracks_to_segment(segment_id: str):
        """Add tracks to a segment by positions."""
        data = request.json or {}
        try:
            result = segment_service.add_tracks_to_segment(
                state,
                segment_id,
                positions=data.get("positions", []),
                force=data.get("force", False),
            )
        except NotFoundError:
            return jsonify({"error": "Segment not found"}), 404
        return jsonify(result)

    @app.route("/api/segments/<segment_id>/tracks", methods=["DELETE"])
    def remove_tracks_from_segment(segment_id: str):
        """Remove tracks from a segment (clear their segment_id) with edge
        validation."""
        data = request.json or {}
        try:
            result = segment_service.remove_tracks_from_segment(
                state, segment_id, positions=data.get("positions", [])
            )
        except NotFoundError:
            return jsonify({"error": "Segment not found"}), 404
        return jsonify(result)

    @app.route("/api/segments/<segment_id>/end_tiles", methods=["GET"])
    def get_segment_end_tiles(segment_id: str):
        """Get the start and end tiles of a segment (sorted in traversal
        order)."""
        segment = segment_service.get_segment(state, segment_id)
        if segment is None:
            return jsonify({"error": "Segment not found"}), 404

        positions = state.network.get_segment_positions(segment_id)
        if len(positions) == 0:
            return jsonify({"start_tile": None, "end_tile": None})

        sorted_positions = sort_positions_traversal(positions, state.network)
        start_tile = {"x": sorted_positions[0][0], "y": sorted_positions[0][1]}
        end_tile = {"x": sorted_positions[-1][0], "y": sorted_positions[-1][1]}
        return jsonify({"start_tile": start_tile, "end_tile": end_tile})

    @app.route("/api/segments/<segment_id>/can_extend", methods=["POST"])
    def can_extend_segment(segment_id: str):
        """Check if a position can be added to the segment (validation without
        mutation)."""
        data = request.json or {}
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400
        try:
            result = segment_service.can_extend_segment(state, segment_id, x=x, y=y)
        except NotFoundError:
            return jsonify({"error": "Segment not found"}), 404
        return jsonify(result)
