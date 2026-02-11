"""
Path Management Routes - Path creation, extension, completion, and dispatcher pathfinding.
"""

from flask import jsonify, request

from app_state import AppState
from path_utils import assign_complete_path_to_train, convert_tiles_to_train_path


def register(app, state: AppState) -> None:
    """Register path routes."""

    def _convert_path_tiles_to_train_path(tiles):
        """Convert path dispatcher tiles to train path format."""
        return convert_tiles_to_train_path(tiles)

    @app.route("/api/paths", methods=["POST"])
    def create_path():
        """Create a new path between two blocks.

        Request body:
        {
            "start_block_id": "BLK_001",
            "end_block_id": "BLK_002",
            "train_id": "TRAIN_001",       // Optional: pre-assign a train
            "priority": 5,                 // Optional: 1-10, default 5
            "dispatcher_id": "user"        // Optional: who created this
        }

        Returns:
            PathResult with success flag and path details or error info
        """
        data = request.json or {}

        start_block_id = data.get("start_block_id")
        end_block_id = data.get("end_block_id")

        if not start_block_id:
            return jsonify({"error": "start_block_id required"}), 400
        if not end_block_id:
            return jsonify({"error": "end_block_id required"}), 400

        train_id = data.get("train_id")
        priority = data.get("priority", 5)
        dispatcher_id = data.get("dispatcher_id", "user")

        try:
            with state.simulation_lock:
                result = state.path_dispatcher.create_path(
                    start_block_id=start_block_id,
                    end_block_id=end_block_id,
                    dispatcher_id=dispatcher_id,
                    priority=priority,
                    train_id=train_id,
                )
                if result.success and result.path:
                    state.network.update_signals_from_blocks()
                    if train_id and train_id in state.network.trains:
                        train = state.network.trains[train_id]
                        raw_path = _convert_path_tiles_to_train_path(
                            result.path.path_tiles
                        )
                        success, error = assign_complete_path_to_train(
                            state.network, train, raw_path
                        )
                        if not success:
                            train.current_path = raw_path
                            train.path_index = 0
                        train.assigned_path_id = result.path.path_id

                        # Set ato_destination_block_id on controller for proper stopping
                        controller = state.sim_controller._get_or_create_controller(
                            train
                        )
                        if controller and result.path.end_block_id:
                            controller.ato_destination_block_id = (
                                result.path.end_block_id
                            )
                            train.ato_destination_block_id = result.path.end_block_id

                            # Check if train needs to turn around for this path.
                            # Use the controller's ato_info to determine the
                            # required cab — this handles diagonal and 90-degree
                            # cases that a simple "exact opposite" check misses.
                            if (
                                train.position
                                and train.current_path
                                and len(train.current_path) >= 2
                                and train.facing_direction is not None
                                and train.is_stopped()
                            ):
                                # Recalculate ato_info now that the path is assigned
                                controller.ato_info = controller._calculate_ato_info()
                                if (
                                    controller.ato_info
                                    and controller.ato_info.required_cab
                                    and controller.active_cab
                                    and controller.ato_info.required_cab
                                    != controller.active_cab
                                ):
                                    # Stop ATO before turning
                                    if controller.ato_running:
                                        controller.ato_stop()
                                    # Switch cab to match path direction
                                    controller.set_cab(controller.ato_info.required_cab)
            return jsonify(result.to_dict()), 200 if result.success else 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/paths", methods=["GET"])
    def list_paths():
        """List all active paths.

        Query params:
            train_id: Filter by train
            block_id: Filter by block usage
            status: Filter by status (set, active, etc.)

        Returns:
            List of path objects
        """
        train_id = request.args.get("train_id")
        block_id = request.args.get("block_id")
        status = request.args.get("status")

        with state.simulation_lock:
            paths = state.path_dispatcher.get_active_paths()

        # Apply filters
        if train_id:
            paths = [p for p in paths if p.train_id == train_id]
        if block_id:
            paths = [p for p in paths if block_id in p.block_sequence]
        if status:
            paths = [p for p in paths if p.status.value == status]

        return jsonify({"paths": [p.to_dict() for p in paths], "count": len(paths)})

    @app.route("/api/paths/<path_id>", methods=["GET"])
    def get_path(path_id):
        """Get details of a specific path.

        Returns:
            Path object or 404 error
        """
        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)

        if not path:
            return jsonify({"error": f"Path {path_id} not found"}), 404

        return jsonify(path.to_dict())

    @app.route("/api/paths/<path_id>", methods=["DELETE"])
    def cancel_path(path_id):
        """Cancel a path and release its resources.

        Also stops the assigned train's ATO and clears path-related fields
        so the train brakes to a stop instead of continuing to the destination.

        Query params:
            force: If "true", force cancellation even with approach locking

        Returns:
            CancellationResult with released resources info
        """
        force = request.args.get("force", "").lower() == "true"

        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            # Before cancelling the path, stop the assigned train's ATO
            # and clear path-related fields so it brakes to a stop
            if path.train_id and state.sim_controller:
                train = state.network.trains.get(path.train_id)
                if train and train.assigned_path_id == path_id:
                    controller = state.sim_controller._get_or_create_controller(train)
                    if controller:
                        if controller.ato_running:
                            controller.ato_stop()
                        controller.ato_destination_block_id = None
                        controller.ato_path_id = None
                    train.ato_destination_block_id = None
                    train.assigned_path_id = None
                    train.current_path = None
                    train.path_index = 0
                    train.reserved_blocks = []

            result = state.path_dispatcher.cancel_path(path_id, force=force)

            # Update signal states after path cancellation (blocks are now released)
            if result.success:
                state.network.update_signals_from_blocks()

        return jsonify(result.to_dict()), 200 if result.success else 400

    @app.route("/api/paths/<path_id>/extend", methods=["PUT"])
    def extend_path(path_id):
        """Extend an existing path to a new destination.

        Request body:
        {
            "new_end_block_id": "BLK_003"
        }

        Returns:
            ExtensionResult with extended blocks or error info
        """
        data = request.json or {}
        new_end_block_id = data.get("new_end_block_id")

        if not new_end_block_id:
            return jsonify({"error": "new_end_block_id required"}), 400

        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            result = state.path_dispatcher.extend_path(path_id, new_end_block_id)

        return jsonify(result.to_dict()), 200 if result.success else 400

    @app.route("/api/paths/<path_id>/reachable-extensions", methods=["GET"])
    def get_path_reachable_extensions(path_id):
        """Get all blocks reachable from a path's end in the FORWARD direction only.

        This is used for path extension to ensure users can only extend in the
        direction the path is already heading (no backward extension).

        Query params:
            include_stations: If "true", include station IDs (default: true)

        Returns:
            {
                "path_id": str,
                "reachable_endpoints": [str],
                "count": int,
                "exit_direction": str | null,  // N/E/S/W
                "error": str | null
            }
        """
        include_stations = (
            request.args.get("include_stations", "true").lower() != "false"
        )

        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            reachable, error = (
                state.path_dispatcher.get_reachable_endpoints_for_extension(
                    path_id, include_stations=include_stations
                )
            )

        if error:
            return jsonify(
                {
                    "path_id": path_id,
                    "reachable_endpoints": [],
                    "count": 0,
                    "exit_direction": None,
                    "error": error,
                }
            ), 400

        return jsonify(
            {
                "path_id": path_id,
                "reachable_endpoints": reachable,
                "count": len(reachable),
                "exit_direction": path.get_exit_direction() if path else None,
                "error": None,
            }
        )

    @app.route("/api/paths/<path_id>/complete", methods=["POST"])
    def complete_path(path_id):
        """Mark a path as completed and release all resources.

        Used when train reaches destination.

        Returns:
            Success message or error
        """
        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            success = state.path_dispatcher.complete_path(path_id)

        if success:
            return jsonify(
                {"status": "success", "message": f"Path {path_id} completed"}
            )
        return jsonify({"error": "Failed to complete path"}), 400

    @app.route("/api/dispatcher/paths", methods=["POST"])
    def find_dispatcher_paths():
        """Find all possible paths between two blocks.

        Request body:
        {
            "start_block_id": "BLK_001",
            "end_block_id": "BLK_002",
            "train_id": "TRAIN_001"  // Optional
        }

        Returns:
            List of possible paths sorted by distance
        """
        data = request.json or {}
        start_block_id = data.get("start_block_id")
        end_block_id = data.get("end_block_id")
        train_id = data.get("train_id")

        if not start_block_id:
            return jsonify({"error": "start_block_id required"}), 400
        if not end_block_id:
            return jsonify({"error": "end_block_id required"}), 400

        with state.simulation_lock:
            paths = state.path_dispatcher.find_paths(
                start_block_id, end_block_id, train_id
            )

        return jsonify(
            {
                "paths": [p.to_dict() for p in paths],
                "count": len(paths),
                "start_block_id": start_block_id,
                "end_block_id": end_block_id,
            }
        )

    @app.route("/api/dispatcher/reachable/<block_id>", methods=["GET"])
    def get_reachable_endpoints(block_id):
        """Get all blocks reachable from a starting block.

        Used by UI to populate destination dropdown.

        Query params:
            include_stations: If "true", include station IDs (default: true)

        Returns:
            List of reachable block IDs (and optionally station IDs)
        """
        include_stations = (
            request.args.get("include_stations", "true").lower() != "false"
        )

        if block_id not in state.network.blocks:
            return jsonify({"error": f"Block {block_id} not found"}), 404

        with state.simulation_lock:
            reachable = state.path_dispatcher.get_reachable_endpoints(
                block_id, include_stations=include_stations
            )

        return jsonify(
            {
                "start_block_id": block_id,
                "reachable_endpoints": reachable,
                "count": len(reachable),
            }
        )
