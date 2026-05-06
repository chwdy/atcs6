"""
Path Management Routes - Path creation, extension, completion, and dispatcher pathfinding.
"""

from flask import jsonify, request

from app_state import AppState
from path_utils import assign_complete_path_to_train, convert_tiles_to_train_path
from runtime_payloads import PathCancellationKind, PendingPathCancelRequest


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
                if train_id and train_id in state.network.trains:
                    train = state.network.trains[train_id]
                    existing_path_id = getattr(train, "assigned_path_id", None)
                    existing_path = (
                        state.path_dispatcher.get_path(existing_path_id)
                        if existing_path_id
                        else None
                    )
                    if existing_path:
                        return (
                            jsonify(
                                {
                                    "success": False,
                                    "error": (
                                        f"Train {train_id} already has active path "
                                        f"{existing_path_id}"
                                    ),
                                    "conflict": "train_has_active_path",
                                    "train_id": train_id,
                                    "existing_path_id": existing_path_id,
                                    "existing_path": existing_path.to_dict(),
                                }
                            ),
                            409,
                        )

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

                        # In v1 the controller derives ato_destination_block_id
                        # from received_path_plan (a property over the active
                        # path); no manual write is needed. Mirror it onto the
                        # Train for serialization and legacy callers.
                        if result.path.end_block_id:
                            train.ato_destination_block_id = result.path.end_block_id
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
        """Request cancellation of a path.

        v1 semantics: cancellation is latched. The handler writes
        `controller.pending_path_cancel_request` and returns immediately with
        ``status: "queued"``. Phase 3 of the simulation tick consumes the
        latch and calls ``PathDispatcher.cancel_path(...)`` once the train is
        fully stopped. Block reservations and switch locks are released by
        the same Phase-3 consumer; clients should refresh path state on a
        subsequent poll instead of mutating local UI state.

        Paths with no assigned train are cancelled synchronously (no train
        means nothing to brake first).

        Returns:
            200 OK with ``{"status": "queued", ...}`` when latched.
            200 OK with ``CancellationResult`` when cancelled synchronously.
        """
        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            train_id = path.train_id
            if train_id and state.sim_controller:
                train = state.network.trains.get(train_id)
                if train:
                    controller = state.sim_controller._get_or_create_controller(train)
                    if controller is not None:
                        controller.pending_path_cancel_request = (
                            PendingPathCancelRequest(
                                cancellation_kind=PathCancellationKind.OPERATOR_CANCELLED,
                                reason="api cancel",
                                expected_path_id=path_id,
                            )
                        )
                        return jsonify(
                            {
                                "success": True,
                                "status": "queued",
                                "path_id": path_id,
                                "train_id": train_id,
                                "note": "cancel committed when train is stopped",
                            }
                        ), 200

            # No train assigned (or controller unavailable): cancel synchronously.
            success = state.path_dispatcher.cancel_path(
                path_id,
                cancellation_kind=PathCancellationKind.OPERATOR_CANCELLED,
                reason="api cancel",
            )
            if success:
                state.network.update_signals_from_blocks()
                return jsonify(
                    {"status": "success", "path_id": path_id, "success": True}
                ), 200
            return jsonify(
                {"error": "Failed to cancel path", "path_id": path_id}
            ), 400

    @app.route("/api/paths/<path_id>/extend", methods=["PUT"])
    def extend_path(path_id):
        """Extend an existing path to a new destination.

        Request body:
        {
            "new_end_block_id": "BLK_003"
        }

        Also syncs ato_destination_block_id on both controller and train
        so the ATO knows the new terminal, and updates train.current_path
        cache for to_dict() serialization.

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

            # Sync train state after successful extension. The controller
            # derives ato_destination_block_id from received_path_plan
            # (now a property over Path.authority_end_block_id), so no
            # controller-side write is needed here.
            if result.success and path.train_id:
                train = state.network.trains.get(path.train_id)
                if train:
                    # Mirror destination on Train for serialization and
                    # legacy consumers.
                    train.ato_destination_block_id = new_end_block_id

                    # Update current_path cache for to_dict() serialization
                    train.current_path = _convert_path_tiles_to_train_path(
                        path.path_tiles
                    )

            if result.success:
                state.network.update_signals_from_blocks()

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
