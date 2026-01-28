"""
Station Management Routes - Station CRUD, tile management, and block assignments.
"""

from flask import jsonify, request

from app_state import AppState
from track import BIT_TO_GROUP


def register(app, state: AppState) -> None:
    """Register station routes."""

    @app.route("/api/station", methods=["POST"])
    def create_station():
        """Create a new station at specified position"""
        data = request.json
        x = data.get("x")
        y = data.get("y")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        try:
            station = state.network.add_station(
                x=x,
                y=y,
                station_id=data.get("station_id"),
                name=data.get("name", ""),
                length_cm=data.get("length_cm", 0.0),
                platform_side=data.get("platform_side", "both"),
                top_speed_kmh=data.get("top_speed_kmh"),
            )
            return jsonify(station.to_dict_with_orientation(state.network))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/station/<station_id>", methods=["GET"])
    def get_station(station_id):
        """Get station by ID"""
        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404
        return jsonify(station.to_dict_with_orientation(state.network))

    @app.route("/api/station/<station_id>", methods=["PATCH"])
    def update_station(station_id):
        """Update station properties"""
        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        data = request.json

        # Update allowed fields
        if "name" in data:
            station.name = data["name"]
        if "length_cm" in data:
            station.length_cm = data["length_cm"]
        if "platform_side" in data:
            from enums import PlatformSide

            station.platform_side = PlatformSide(data["platform_side"])
        if "top_speed_kmh" in data:
            station.top_speed_kmh = data["top_speed_kmh"]

        return jsonify(station.to_dict_with_orientation(state.network))

    @app.route("/api/station/<station_id>", methods=["DELETE"])
    def delete_station(station_id):
        """Delete station"""
        success = state.network.delete_station(station_id)
        if not success:
            return jsonify({"error": "Station not found"}), 404
        return jsonify({"success": True})

    @app.route("/api/station/validate/<sint:x>/<sint:y>", methods=["GET"])
    def validate_station_at(x, y):
        """Check if station can be placed at position"""
        try:
            track = state.network.get_track(x, y)

            if not track:
                return jsonify(
                    {"can_place": False, "error": f"No track at position ({x}, {y})"}
                )

            if track.is_switch:
                return jsonify(
                    {"can_place": False, "error": "Cannot place station on switch"}
                )

            if track.signal_ids:
                return jsonify(
                    {
                        "can_place": False,
                        "error": "Cannot place station on tile with signal",
                    }
                )

            if track.station_id is not None:
                return jsonify(
                    {
                        "can_place": False,
                        "error": f"Tile already belongs to station {track.station_id}",
                    }
                )

            return jsonify(
                {"can_place": True, "existing_block": track.block_section_id}
            )
        except Exception as e:
            import traceback

            traceback.print_exc()
            return jsonify({"error": str(e), "can_place": False}), 500

    @app.route("/api/station/<station_id>/extend", methods=["POST"])
    def extend_station(station_id):
        """Extend station platform with new tiles"""
        data = request.json
        positions = data.get("positions", [])

        if not positions:
            return jsonify({"error": "Missing positions"}), 400

        # Convert positions to tuples
        positions = [tuple(pos) for pos in positions]

        try:
            station = state.network.extend_station(station_id, positions)
            return jsonify(station.to_dict_with_orientation(state.network))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/station/<station_id>/remove_tile", methods=["POST"])
    def remove_station_tile(station_id):
        """Remove a single tile from a station with edge validation"""
        data = request.json
        x = data.get("x")
        y = data.get("y")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        pos = (x, y)
        if pos not in station.positions:
            return jsonify(
                {"error": f"Position ({x}, {y}) is not part of station {station_id}"}
            ), 400

        # Check if this is an edge tile
        # Edge tiles have at least one exit that either:
        # 1. Has no neighbor track, OR
        # 2. Has a neighbor track that's not in the station
        track = state.network.get_track(x, y)
        if not track:
            return jsonify({"error": f"No track at ({x}, {y})"}), 400

        # Get all connected edges from this track (excludes dead-ends)
        # We only care about actual track connections, not dead-ends
        exits = []
        for bit in range(6):
            if track.track_bits & (1 << bit):
                from track import BIT_TO_EDGES

                e1, e2 = BIT_TO_EDGES[bit]
                exits.extend([e1, e2])
        exits = list(set(exits))  # Remove duplicates

        position_set = set(station.positions)

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
                # Exit leads outside station -> edge tile
                is_edge = True
                break

        # If no exit leads outside the station, this is not an edge tile
        if not is_edge:
            return jsonify(
                {
                    "error": f"Cannot remove tile ({x}, {y}): not an edge tile. Only edge tiles can be removed.",
                    "is_edge": False,
                }
            ), 400

        # Remove the position from station
        new_positions = [p for p in station.positions if p != pos]

        # Check that remaining positions are still continuous
        if len(new_positions) > 1:
            is_continuous, errors = state.network._validate_station_continuity(
                new_positions
            )
            if not is_continuous:
                return jsonify(
                    {
                        "error": f"Cannot remove tile ({x}, {y}): would break station continuity",
                        "validation_errors": errors,
                        "is_edge": True,
                    }
                ), 400

        # Update station positions
        station.positions = new_positions

        # Clear station_id from the track
        track = state.network.get_track(x, y)
        if track and track.station_id == station_id:
            track.station_id = None

            # Auto-shorten block: Remove tile from ANY block it belongs to
            # Since this tile is no longer part of the station, remove it from its block
            if track.block_section_id:
                old_block_id = track.block_section_id

                # Use the network's remove_tile_from_block method which handles:
                # - Removing the tile
                # - Checking block connectivity
                # - Splitting blocks if needed
                # - Deleting empty blocks
                state.network.remove_tile_from_block(x, y)

                print(
                    f"[Auto-Shorten] Removed tile ({x}, {y}) from block {old_block_id} due to station shrink"
                )

        # If station is now empty, delete it
        if len(station.positions) == 0:
            state.network.delete_station(station_id)
            return jsonify({"success": True, "station_deleted": True})

        return jsonify(
            {
                "success": True,
                "station": station.to_dict_with_orientation(state.network),
            }
        )

    @app.route("/api/station/<station_id>/end_tiles", methods=["GET"])
    def get_station_end_tiles(station_id):
        """Get the start and end tiles of a station (sorted by orientation)"""
        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        sorted_positions = station._sort_positions_by_orientation(
            station.initial_orientation
        )

        if len(sorted_positions) == 0:
            return jsonify({"start_tile": None, "end_tile": None})

        start_tile = {"x": sorted_positions[0][0], "y": sorted_positions[0][1]}
        end_tile = {"x": sorted_positions[-1][0], "y": sorted_positions[-1][1]}

        return jsonify({"start_tile": start_tile, "end_tile": end_tile})

    @app.route("/api/station/<station_id>/can_extend", methods=["POST"])
    def can_extend_station(station_id):
        """Check if a position can be added to the station (validation without mutation)"""
        data = request.json
        x = data.get("x")
        y = data.get("y")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        # Check if track exists
        track = state.network.get_track(x, y)
        if not track:
            return jsonify({"can_extend": False, "reason": "No track at position"})

        # Check if it's a switch
        if track.is_switch:
            return jsonify(
                {"can_extend": False, "reason": "Cannot add switch to station"}
            )

        # Check if track is on straight path
        if not state.network._is_track_on_straight_path(x, y):
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Stations require tracks on straight paths (pure NS/EW or simple diagonal tracks)",
                }
            )

        # Check orientation match
        new_tile_orientation = None
        for bit_idx, group in BIT_TO_GROUP.items():
            if track.track_bits & (1 << bit_idx):
                new_tile_orientation = group
                break

        if (
            station.initial_orientation
            and new_tile_orientation != station.initial_orientation
        ):
            return jsonify(
                {
                    "can_extend": False,
                    "reason": f"Orientation mismatch: station is {station.initial_orientation}, tile is {new_tile_orientation}",
                }
            )

        # Check if tile has signals
        if track.signal_ids:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Cannot add tile with signal to station",
                }
            )

        # Check if tile already belongs to another station
        if track.station_id is not None and track.station_id != station_id:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": f"Tile already belongs to station {track.station_id}",
                }
            )

        # Check adjacency
        is_adjacent = False
        for old_x, old_y in station.positions:
            if abs(x - old_x) + abs(y - old_y) == 1:
                is_adjacent = True
                break

        if not is_adjacent and len(station.positions) > 0:
            return jsonify(
                {
                    "can_extend": False,
                    "reason": "Position must be adjacent to existing station",
                }
            )

        # Check continuity
        all_positions = station.positions + [(x, y)]
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

    @app.route("/api/station/<station_id>/assign_tile", methods=["POST"])
    def assign_tile_to_block(station_id):
        """Assign a station tile to a specific block"""
        data = request.json
        x = data.get("x")
        y = data.get("y")
        block_id = data.get("block_id")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        if not block_id:
            return jsonify({"error": "Missing block_id"}), 400

        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        # Verify block exists
        block = state.network.get_block(block_id)
        if not block:
            return jsonify({"error": "Block not found"}), 404

        # Assign tile to block
        success = station.assign_tile_to_block(x, y, block_id)
        if not success:
            return jsonify({"error": "Tile not in station"}), 400

        return jsonify({"status": "success", "station": station.to_dict()})

    @app.route("/api/station/<station_id>/unassign_tile", methods=["POST"])
    def unassign_tile_from_block(station_id):
        """Remove block assignment from a station tile"""
        data = request.json
        x = data.get("x")
        y = data.get("y")

        if x is None or y is None:
            return jsonify({"error": "Missing x or y coordinate"}), 400

        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        # Unassign tile from block
        success = station.unassign_tile_from_block(x, y)
        if not success:
            return (
                jsonify({"error": "Tile has no block assignment or not in station"}),
                400,
            )

        return jsonify({"status": "success", "station": station.to_dict()})

    @app.route("/api/station/<station_id>/block_assignments", methods=["GET"])
    def get_block_assignments(station_id):
        """Get all block assignments for a station"""
        station = state.network.get_station(station_id)
        if not station:
            return jsonify({"error": "Station not found"}), 404

        # Build response with detailed block assignment info
        assignments = []
        for (x, y), block_id in station.block_assignments.items():
            assignments.append({"x": x, "y": y, "block_id": block_id})

        # Also include summary by block
        blocks_summary = {}
        for block_id in station.get_all_assigned_blocks():
            tiles = station.get_tiles_for_block(block_id)
            blocks_summary[block_id] = {
                "tile_count": len(tiles),
                "tiles": [{"x": x, "y": y} for x, y in tiles],
            }

        return jsonify(
            {
                "station_id": station_id,
                "assignments": assignments,
                "blocks_summary": blocks_summary,
            }
        )
