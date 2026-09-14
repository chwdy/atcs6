"""
Block Routes - the power + occupancy + reservation + grouping surface.

Thin HTTP layer over `services.block_service` for mutations: parse JSON, call
the service, translate `ServiceError` subclasses to status codes
(ValidationError -> 400, NotFoundError -> 404, ConflictError -> 409,
GoneError -> 410). Read-only endpoints keep their serialization here.

A `Block` groups one or more `Segment`s (its geometry) and owns occupancy,
reservation, availability, power and the static travel policy. Grouping —
creating a block from a `segment_ids` list, dissolving it, and adding / removing
a member segment — is the four-method `BlockGroupingManager` surface, the
replacement for the old power-district grouping API. Geometry (length / radius /
grade / speed / tiles) is edited on the segments under `/api/segments`.
"""

from datetime import datetime

from flask import jsonify, request

from app_state import AppState
from routes._positions import sort_positions_traversal
from services import ConflictError, GoneError, NotFoundError, ValidationError
from services import block_service
from services.notifications import emit_command_result


def register(app, state: AppState) -> None:
    """Register block routes."""

    def _read_block_dict(block) -> dict:
        """Block runtime dict + track counts, positions and platform names."""
        block_dict = block.to_dict()
        tracks = state.network.get_block_tracks(block.block_id)
        block_dict["track_count"] = len(tracks)
        block_dict["track_positions"] = state.network.get_block_positions(
            block.block_id
        )
        platform_names = set()
        for track in tracks:
            if track.station_id:
                platform = state.network.platforms.get(track.station_id)
                if platform:
                    platform_names.add(platform.name)
        if platform_names:
            block_dict["platform_names"] = sorted(list(platform_names))
        return block_dict

    @app.route("/api/blocks", methods=["GET"])
    def get_blocks():
        """List all blocks with their track counts, positions and member
        segments."""
        result = [
            _read_block_dict(block) for block in block_service.list_blocks(state)
        ]
        return jsonify(result)

    @app.route("/api/blocks", methods=["POST"])
    def create_block():
        """Group existing segments into one block.

        Request body: { "block_id": str?, "segment_ids": [str, ...],
        "name": str? }. Creating track geometry is a segment operation
        (POST /api/segments).
        """
        data = request.json or {}
        try:
            block = block_service.create_block(
                state,
                block_id=data.get("block_id"),
                segment_ids=data.get("segment_ids", []),
                name=data.get("name", ""),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(block.to_dict())

    @app.route("/api/blocks/<block_id>", methods=["GET"])
    def get_block(block_id):
        """Get block details including member segments, continuity status and
        connected signals."""
        block = block_service.get_block(state, block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404
        result = _read_block_dict(block)

        positions = state.network.get_block_positions(block_id)
        result["sorted_positions"] = sort_positions_traversal(
            positions, state.network
        )

        # Continuity is a per-segment property, so a block is continuous when
        # every member segment is.
        errors: list = []
        for segment_id in block.segment_ids:
            _seg_valid, seg_errors = state.network.validate_segment_continuity(
                segment_id
            )
            errors.extend(seg_errors)
        result["is_continuous"] = not errors
        result["validation_errors"] = errors

        # Find signals monitoring this block
        monitoring_signals = [
            signal.to_dict()
            for signal in state.network.signals.values()
            if signal.monitored_block_id == block_id
        ]
        result["monitoring_signals"] = monitoring_signals

        return jsonify(result)

    @app.route("/api/blocks/<block_id>", methods=["PATCH"])
    def update_block(block_id):
        """Update block config / availability flags (NOT geometry).

        Mutations to block runtime state happen between simulation ticks
        per design_new.md "The runtime uses one thread" — the service holds
        `state.simulation_lock` while writing entity fields so the per-tick
        phase loop cannot observe a torn read. Geometry lives on the member
        segments (PATCH /api/segments/<segment_id>).
        """
        data = request.json or {}
        try:
            block = block_service.update_block(state, block_id, data=data)
        except NotFoundError:
            return jsonify({"error": "Block not found"}), 404
        except GoneError as exc:
            return jsonify({"error": str(exc)}), 410
        except ConflictError as exc:
            # design_new.md "Availability flag REST API surface": the flag PATCH emits a command_result even
            # on the 409 admin-reject, carrying the rejection reason.
            emit_command_result(
                state, source="Block", entity_id_key="block_id",
                entity_id=block_id, command_name="update_flags",
                success=False, reason=str(exc),
            )
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        emit_command_result(
            state, source="Block", entity_id_key="block_id",
            entity_id=block_id, command_name="update_flags", success=True,
        )
        return jsonify(block.to_dict())

    @app.route("/api/blocks/<block_id>", methods=["DELETE"])
    def delete_block(block_id):
        """Dissolve a block grouping (ungroup); member segments return to their
        own single-segment blocks. Geometry survives."""
        try:
            block_service.delete_block(state, block_id)
        except NotFoundError:
            return jsonify({"error": "Block not found"}), 404
        return jsonify(
            {"status": "success", "message": f"Block {block_id} ungrouped"}
        )

    @app.route("/api/blocks/<block_id>/segments", methods=["POST"])
    def add_segment_to_block(block_id):
        """Add a member segment to either end of a block's run.

        Request body: { "segment_id": str }.
        """
        data = request.json or {}
        segment_id = data.get("segment_id")
        if not segment_id:
            return jsonify({"error": "segment_id required"}), 400
        try:
            block = block_service.add_segment_to_block(state, block_id, segment_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(block.to_dict())

    @app.route(
        "/api/blocks/<block_id>/segments/<segment_id>", methods=["DELETE"]
    )
    def remove_segment_from_block(block_id, segment_id):
        """Remove a member segment from either end of a block's run.

        The segment returns to its own single-segment block. Removing the
        block's last member dissolves the block.
        """
        try:
            block_service.remove_segment_from_block(state, block_id, segment_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        block = block_service.get_block(state, block_id)
        if block is None:
            # Removing the last member dissolved the block.
            return jsonify(
                {
                    "status": "success",
                    "block_dissolved": True,
                    "message": f"Block {block_id} dissolved (last member removed)",
                }
            )
        return jsonify(block.to_dict())

    @app.route("/api/blocks/<block_id>/voltage", methods=["GET"])
    def get_block_voltage(block_id: str):
        """Get current voltage for a block.

        Returns:
            200 OK with voltage data:
            {
                "block_id": str,
                "voltage": float,
                "timestamp": str (ISO format)
            }
            404 Not Found if block doesn't exist
        """
        with state.simulation_lock:
            block = block_service.get_block(state, block_id)
            if not block:
                return jsonify({"error": f"Block '{block_id}' not found"}), 404

            voltage = state.sim_controller.power_controller.get_block_voltage(block_id)

        return jsonify(
            {
                "block_id": block_id,
                "voltage": voltage,
                "timestamp": datetime.now().isoformat(),
            }
        )
