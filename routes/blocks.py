"""
Block Management Routes - Block CRUD, state cycling, and validation.
"""

from collections import Counter
from datetime import datetime
import uuid

from flask import jsonify, request

from app_state import AppState
from block_validation import is_crossing
from enums import BlockState as BlockStateEnum
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
        for block in state.network.blocks.values():
            block_dict = block.to_dict()
            tracks = state.network.get_block_tracks(block.block_id)
            block_dict["track_count"] = len(tracks)
            block_dict["track_positions"] = state.network.get_block_positions(
                block.block_id
            )

            # Find associated stations
            station_names = set()
            for track in tracks:
                if track.station_id:
                    station = state.network.stations.get(track.station_id)
                    if station:
                        station_names.add(station.name)

            if station_names:
                block_dict["station_names"] = sorted(list(station_names))

            result.append(block_dict)
        return jsonify(result)

    @app.route("/api/blocks", methods=["POST"])
    def create_block():
        """Create a new block"""
        data = request.json or {}
        block_id = data.get("block_id") or f"BLK_{uuid.uuid4().hex[:8].upper()}"
        name = data.get("name", "")
        try:
            block = state.network.create_block(block_id, name)
            # Set optional properties
            if "length_cm" in data:
                block.length_cm = float(data["length_cm"])
            if "min_radius_cm" in data:
                block.min_radius_cm = data["min_radius_cm"]
            if "max_speed_kmh" in data:
                block.max_speed_kmh = data["max_speed_kmh"]
            return jsonify(block.to_dict())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/blocks/<block_id>", methods=["GET"])
    def get_block(block_id):
        """Get block details including validation status and connected signals"""
        block = state.network.get_block(block_id)
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

        # Find associated stations
        station_names = set()
        for track in tracks:
            if track.station_id:
                station = state.network.stations.get(track.station_id)
                if station:
                    station_names.add(station.name)

        if station_names:
            result["station_names"] = sorted(list(station_names))
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
        """Update block properties"""
        block = state.network.get_block(block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404

        data = request.json or {}
        if "name" in data:
            block.name = data["name"] if data["name"] is not None else ""
        if "length_cm" in data:
            block.length_cm = float(data["length_cm"])
        if "min_radius_cm" in data:
            block.min_radius_cm = data["min_radius_cm"]
        if "max_speed_kmh" in data:
            block.max_speed_kmh = data["max_speed_kmh"]
        if "state" in data:
            # Convert string to BlockState enum and use set_block_state for proper signal updates
            state_str = data["state"].lower()
            try:
                new_state = BlockStateEnum(state_str)
                state.network.set_block_state(block_id, new_state)
            except ValueError:
                return jsonify(
                    {
                        "error": f"Invalid state: {state_str}. Valid: free, reserved, occupied, caution"
                    }
                ), 400

        return jsonify(block.to_dict())

    @app.route("/api/blocks/<block_id>", methods=["DELETE"])
    def delete_block(block_id):
        """Delete block and clear track references"""
        if state.network.delete_block(block_id):
            return jsonify(
                {"status": "success", "message": f"Block {block_id} deleted"}
            )
        return jsonify({"error": "Block not found"}), 404

    @app.route("/api/blocks/<block_id>/cycle-state", methods=["POST"])
    def cycle_block_state(block_id):
        """
        Cycle block state: FREE -> RESERVED -> OCCUPIED -> CAUTION -> FREE

        Used by UI right-click to manually cycle through states for testing.
        Rejects changes if block is reserved by an active route.
        """
        # Check if block is reserved by a route - reject manual changes
        block = state.network.get_block(block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404

        if block.reserved_by_path:
            return jsonify(
                {
                    "error": f"Block is reserved by path {block.reserved_by_path}",
                    "reserved_by_path": block.reserved_by_path,
                    "current_state": block.state.value,
                }
            ), 409  # 409 Conflict

        new_state = state.network.cycle_block_state(block_id)
        if new_state is None:
            return jsonify({"error": "Block not found"}), 404

        block = state.network.get_block(block_id)
        return jsonify(
            {
                "status": "success",
                "block_id": block_id,
                "new_state": new_state.value,
                "block": block.to_dict(),
            }
        )

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
            "max_speed_kmh": 80.0
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
            # Convert positions to tuples
            positions = [tuple(pos) for pos in positions]

            block = state.network.create_block_with_tracks(
                block_id=block_id,
                positions=positions,
                name=data.get("name", ""),
                length_cm=data.get("length_cm", 0.0),
                max_speed_kmh=data.get("max_speed_kmh"),
            )
            return jsonify(block.to_dict())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

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
        force = data.get("force", False)
        force_overwrite = data.get("force_overwrite", False)

        if not positions:
            return jsonify({"error": "positions required"}), 400

        try:
            positions = [tuple(pos) for pos in positions]
            block = state.network.extend_block(
                block_id, positions, force=force, force_overwrite=force_overwrite
            )

            # Build response with invalidated signals info
            response = block.to_dict()
            if hasattr(block, "_invalidated_signals") and block._invalidated_signals:
                response["invalidated_signals"] = block._invalidated_signals
                # Clean up temporary attribute
                del block._invalidated_signals

            return jsonify(response)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/blocks/<block_id>/tracks", methods=["POST"])
    def add_tracks_to_block(block_id):
        """Add tracks to a block by positions"""
        block = state.network.get_block(block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404

        data = request.json or {}
        positions = data.get("positions", [])
        force = data.get("force", False)  # Allow reassignment if force=True
        added = []
        reassigned = []

        for pos in positions:
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            x, y = pos[0], pos[1]

            # Check if track already assigned to a different block
            track = state.network.get_track(x, y)
            if track and track.block_section_id and track.block_section_id != block_id:
                if force:
                    # Unassign from old block first
                    old_block_id = track.block_section_id
                    track.block_section_id = None
                    reassigned.append(
                        {"position": [x, y], "from": old_block_id, "to": block_id}
                    )
                else:
                    # Skip if not forcing reassignment
                    continue

            # Now assign to new block
            try:
                state.network.assign_track_to_block(x, y, block_id)
                added.append([x, y])
            except ValueError:
                pass

        return jsonify(
            {
                "status": "success",
                "added": len(added),
                "reassigned": len(reassigned),
                "positions": added,
                "reassignment_details": reassigned,
                "block_id": block_id,
            }
        )

    @app.route("/api/blocks/<block_id>/tracks", methods=["DELETE"])
    def remove_tracks_from_block(block_id):
        """Remove tracks from a block (clear their block_section_id) with edge validation"""
        block = state.network.get_block(block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404

        data = request.json or {}
        positions = data.get("positions", [])
        removed = []
        errors = []

        # Get all current block positions
        current_positions = state.network.get_block_positions(block_id)
        position_set = set((x, y) for x, y in current_positions)

        for pos in positions:
            if not isinstance(pos, (list, tuple)) or len(pos) < 2:
                continue
            x, y = pos[0], pos[1]

            track = state.network.get_track(x, y)
            if not track or track.block_section_id != block_id:
                errors.append(f"Position ({x}, {y}) is not part of block {block_id}")
                continue

            # Check if this is an edge tile
            # Edge tiles have at least one exit that either:
            # 1. Has no neighbor track, OR
            # 2. Has a neighbor track that's not in the block

            # Get all connected edges from this track (excludes dead-ends)
            exits = []
            for bit in range(6):
                if track.track_bits & (1 << bit):
                    from track import BIT_TO_EDGES

                    e1, e2 = BIT_TO_EDGES[bit]
                    exits.extend([e1, e2])
            exits = list(set(exits))  # Remove duplicates

            is_edge = False
            for exit_edge in exits:
                # Get neighbor position based on exit edge
                neighbor_pos = state.network._get_neighbor_coords(x, y, exit_edge)
                if not neighbor_pos:
                    # No neighbor (edge of map) -> definitely an edge tile
                    is_edge = True
                    break

                nx, ny = neighbor_pos
                neighbor_track = state.network.get_track(nx, ny)

                if not neighbor_track or (nx, ny) not in position_set:
                    # Exit leads outside block -> edge tile
                    is_edge = True
                    break

            # If no exit leads outside the block, this is not an edge tile
            if not is_edge:
                errors.append(f"Cannot remove tile ({x}, {y}): not an edge tile")
                continue

            # Temporarily remove track to check continuity
            original_block_id = track.block_section_id
            track.block_section_id = None

            # Check if remaining block is still continuous (only if block has more than 1 track left)
            remaining_tracks = state.network.get_block_tracks(block_id)
            if len(remaining_tracks) > 1:
                is_continuous, validation_errors = (
                    state.network.validate_block_continuity(block_id)
                )
                if not is_continuous:
                    # Restore the block_section_id
                    track.block_section_id = original_block_id
                    errors.append(
                        f"Cannot remove tile ({x}, {y}): would break block continuity"
                    )
                    continue

            # Keep the removal (track.block_section_id is already None)
            removed.append([x, y])
            position_set.discard((x, y))

        # Check if block is now empty and delete it
        remaining_tracks = state.network.get_block_tracks(block_id)
        block_deleted = False
        if len(remaining_tracks) == 0:
            state.network.delete_block(block_id)
            block_deleted = True

        return jsonify(
            {
                "status": "success"
                if removed
                else "partial"
                if errors
                else "no_changes",
                "removed": len(removed),
                "positions": removed,
                "errors": errors,
                "block_deleted": block_deleted,
            }
        )

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
            block = state.network.get_block(block_id)
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
        block = state.network.get_block(block_id)
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

        block = state.network.get_block(block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404

        # Check if track exists
        track = state.network.get_track(x, y)
        if not track:
            return jsonify({"can_extend": False, "reason": "No track at position"})

        # Check for crossings (never allowed)
        if is_crossing(track.track_bits):
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Cannot add crossing to block - crossings isolate paths",
                }
            )

        # Check for switches (never allowed)
        if track.is_switch:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Cannot add switch to block - switches are block boundaries",
                }
            )

        # Check for signals (requires force flag in actual extend operation)
        if track.signal_ids:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Tile has signal(s) - use force=true to extend past signal",
                    "requires_force": True,
                }
            )

        # Check if tile belongs to another block (requires force_overwrite flag)
        if track.block_section_id and track.block_section_id != block_id:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": f"Tile already in block {track.block_section_id} - use force_overwrite=true",
                    "requires_force_overwrite": True,
                }
            )

        # Check adjacency
        existing_positions = state.network.get_block_positions(block_id)
        is_adjacent = False
        for old_x, old_y in existing_positions:
            if abs(x - old_x) + abs(y - old_y) == 1:
                is_adjacent = True
                break

        if not is_adjacent and len(existing_positions) > 0:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Position must be adjacent to existing block",
                }
            )

        return jsonify({"can_extend": True})
