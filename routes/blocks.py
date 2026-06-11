"""
Block Management Routes - Block CRUD, track membership, and validation.

Thin HTTP layer over `services.block_service` for mutations: parse JSON,
call the service, translate `ServiceError` subclasses to status codes
(ValidationError -> 400, NotFoundError -> 404, ConflictError -> 409,
GoneError -> 410). Read-only endpoints keep their serialization here.
"""

from collections import Counter
from datetime import datetime

from flask import jsonify, request

from app_state import AppState
from services import ConflictError, GoneError, NotFoundError, ValidationError
from services import block_service
from track import BIT_TO_GROUP


def register(app, state: AppState) -> None:
    """Register block routes."""

    def _sort_block_positions(positions, network):
        """
        Sort block positions in traversal order.

        Algorithm:
        1. Count track bit occurrences to detect dominant orientation
        2. For NS: sort by y (descending)
        3. For EW: sort by x (ascending)
        4. For NE_SW diagonal: sort by (x+y)
        5. For NW_SE diagonal: sort by (y-x)
        6. Fallback: BFS traversal for complex shapes

        Args:
            positions: List of (x, y) tuples
            network: RailwayNetwork instance

        Returns:
            List of (x, y) tuples sorted in traversal order
        """
        if len(positions) <= 1:
            return positions

        # Count track bit groups to detect dominant orientation
        orientation_counts = Counter()
        for x, y in positions:
            track = network.get_track(x, y)  # network is passed as parameter
            if track:
                for bit_idx, group in BIT_TO_GROUP.items():
                    if track.track_bits & (1 << bit_idx):
                        orientation_counts[group] += 1

        if not orientation_counts:
            return positions

        # Get dominant orientation
        dominant_orientation = orientation_counts.most_common(1)[0][0]

        # Sort based on orientation
        if dominant_orientation == "NS":
            # North-South: sort by y descending (north to south)
            return sorted(positions, key=lambda pos: -pos[1])
        elif dominant_orientation == "EW":
            # East-West: sort by x ascending (west to east)
            return sorted(positions, key=lambda pos: pos[0])
        elif dominant_orientation == "NE_SW":
            # Northeast-Southwest diagonal: sort by (x-y) ascending, then by x ascending
            return sorted(positions, key=lambda pos: (pos[0] - pos[1], pos[0]))
        elif dominant_orientation == "NW_SE":
            # Northwest-Southeast diagonal: sort by (x+y) ascending, then by x ascending
            return sorted(positions, key=lambda pos: (pos[0] + pos[1], pos[0]))
        else:
            # Fallback: return as-is
            return positions

    @app.route("/api/blocks", methods=["GET"])
    def get_blocks():
        """List all blocks with their track counts and positions"""
        result = []
        for block in block_service.list_blocks(state):
            block_dict = block.to_dict()
            tracks = state.network.get_block_tracks(block.block_id)
            block_dict["track_count"] = len(tracks)
            block_dict["track_positions"] = state.network.get_block_positions(
                block.block_id
            )

            # Find associated platforms
            platform_names = set()
            for track in tracks:
                if track.station_id:
                    platform = state.network.platforms.get(track.station_id)
                    if platform:
                        platform_names.add(platform.name)

            if platform_names:
                block_dict["platform_names"] = sorted(list(platform_names))

            result.append(block_dict)
        return jsonify(result)

    @app.route("/api/blocks", methods=["POST"])
    def create_block():
        data = request.json or {}
        try:
            block = block_service.create_block(
                state,
                block_id=data.get("block_id"),
                name=data.get("name", ""),
                length_cm=data.get("length_cm"),
                min_radius_cm=data.get("min_radius_cm"),
                grade_percent=data.get("grade_percent"),
                speed_limit_kmh=data.get("speed_limit_kmh"),
                max_speed_kmh=data.get("max_speed_kmh"),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(block.to_dict())

    @app.route("/api/blocks/<block_id>", methods=["GET"])
    def get_block(block_id):
        """Get block details including validation status and connected signals"""
        block = block_service.get_block(state, block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404
        result = block.to_dict()
        tracks = state.network.get_block_tracks(block_id)
        result["track_count"] = len(tracks)

        # Get positions (unsorted for backwards compatibility)
        positions = state.network.get_block_positions(block_id)
        result["track_positions"] = positions

        # Add sorted positions for UI display
        result["sorted_positions"] = _sort_block_positions(positions, state.network)

        # Find associated platforms
        platform_names = set()
        for track in tracks:
            if track.station_id:
                platform = state.network.platforms.get(track.station_id)
                if platform:
                    platform_names.add(platform.name)

        if platform_names:
            result["platform_names"] = sorted(list(platform_names))
        is_valid, errors = state.network.validate_block_continuity(block_id)
        result["is_continuous"] = is_valid
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
        """Update block properties.

        Mutations to block runtime state happen between simulation ticks
        per design_new.md "single-threaded runtime" — the service holds
        `state.simulation_lock` while writing entity fields so the per-tick
        phase loop cannot observe a torn read.
        """
        data = request.json or {}
        try:
            block = block_service.update_block(state, block_id, data=data)
        except NotFoundError:
            return jsonify({"error": "Block not found"}), 404
        except GoneError as exc:
            return jsonify({"error": str(exc)}), 410
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(block.to_dict())

    @app.route("/api/blocks/<block_id>", methods=["DELETE"])
    def delete_block(block_id):
        """Delete block and clear track references"""
        try:
            block_service.delete_block(state, block_id)
        except NotFoundError:
            return jsonify({"error": "Block not found"}), 404
        return jsonify({"status": "success", "message": f"Block {block_id} deleted"})

    @app.route("/api/blocks/create-with-tracks", methods=["POST"])
    def create_block_with_tracks():
        """
        Create a new block with validated track assignments.

        Request body:
        {
            "block_id": "BLK_001",
            "positions": [[0,0], [1,0], [2,0]],
            "name": "Main Line",
            "length_cm": 150.0,
            "speed_limit_kmh": 80.0
        }
        """
        data = request.json
        block_id = data.get("block_id")
        positions = data.get("positions", [])

        if not block_id:
            return jsonify({"error": "block_id required"}), 400
        if not positions:
            return jsonify({"error": "positions required"}), 400

        try:
            block = block_service.create_block_with_tracks(
                state,
                block_id=block_id,
                positions=positions,
                name=data.get("name", ""),
                length_cm=data.get("length_cm", 0.0),
                speed_limit_kmh=data.get("speed_limit_kmh", data.get("max_speed_kmh")),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(block.to_dict())

    @app.route("/api/blocks/<block_id>/extend", methods=["POST"])
    def extend_block_endpoint(block_id):
        """
        Extend an existing block with new track positions.

        Request body:
        {
            "positions": [[3,0], [4,0]],
            "force": false,  // Optional: if true, invalidates signals in the path
            "force_overwrite": false  // Optional: if true, allows overwriting tracks from other blocks
        }

        Response includes 'invalidated_signals' array if any signals were invalidated.
        Invalidated signals have their monitored_block cleared and show a warning triangle.
        """
        data = request.json
        positions = data.get("positions", [])
        if not positions:
            return jsonify({"error": "positions required"}), 400

        try:
            block = block_service.extend_block(
                state,
                block_id,
                positions=positions,
                force=data.get("force", False),
                force_overwrite=data.get("force_overwrite", False),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        # Build response with invalidated signals info
        response = block.to_dict()
        if hasattr(block, "_invalidated_signals") and block._invalidated_signals:
            response["invalidated_signals"] = block._invalidated_signals
            # Clean up temporary attribute
            del block._invalidated_signals
        return jsonify(response)

    @app.route("/api/blocks/<block_id>/tracks", methods=["POST"])
    def add_tracks_to_block(block_id):
        """Add tracks to a block by positions"""
        data = request.json or {}
        try:
            result = block_service.add_tracks_to_block(
                state,
                block_id,
                positions=data.get("positions", []),
                force=data.get("force", False),
            )
        except NotFoundError:
            return jsonify({"error": "Block not found"}), 404
        return jsonify(result)

    @app.route("/api/blocks/<block_id>/tracks", methods=["DELETE"])
    def remove_tracks_from_block(block_id):
        """Remove tracks from a block (clear their block_section_id) with edge validation"""
        data = request.json or {}
        try:
            result = block_service.remove_tracks_from_block(
                state, block_id, positions=data.get("positions", [])
            )
        except NotFoundError:
            return jsonify({"error": "Block not found"}), 404
        return jsonify(result)

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

    @app.route("/api/blocks/<block_id>/end_tiles", methods=["GET"])
    def get_block_end_tiles(block_id):
        """Get the start and end tiles of a block (sorted in traversal order)"""
        block = block_service.get_block(state, block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404

        positions = state.network.get_block_positions(block_id)
        if len(positions) == 0:
            return jsonify({"start_tile": None, "end_tile": None})

        # Sort positions using the helper function
        sorted_positions = _sort_block_positions(positions, state.network)

        start_tile = {"x": sorted_positions[0][0], "y": sorted_positions[0][1]}
        end_tile = {"x": sorted_positions[-1][0], "y": sorted_positions[-1][1]}

        return jsonify({"start_tile": start_tile, "end_tile": end_tile})

    @app.route("/api/blocks/<block_id>/can_extend", methods=["POST"])
    def can_extend_block(block_id):
        """Check if a position can be added to the block (validation without mutation)"""
        data = request.json
        x = data.get("x")
        y = data.get("y")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        try:
            result = block_service.can_extend_block(state, block_id, x=x, y=y)
        except NotFoundError:
            return jsonify({"error": "Block not found"}), 404
        return jsonify(result)
