"""
Path Management Routes - v1 wire format.

JSON shape (request and response) uses the v1 Path field names directly:
- requested_destination_block_id
- requested_destination_direction ("forward" | "reverse")
- acquisition_mode ("all_or_nothing" | "best_effort")
- authority_end_block_id
- resource_sequence

The v0 keys `start_block_id`, `end_block_id`, `priority`, `dispatcher_id`,
`new_end_block_id`, `block_sequence` are no longer accepted on the request side.
"""

from flask import jsonify, request

from app_state import AppState
from path_utils import assign_complete_path_to_train, convert_tiles_to_train_path
from runtime_payloads import AcquisitionMode, PathCancellationKind


def register(app, state: AppState) -> None:
    """Register path routes."""

    def _path_tiles_to_coords(path_tiles):
        """Convert PathTileEntry list (v1) to plain (x, y) tuple list."""
        out = []
        for t in path_tiles or []:
            if hasattr(t, "x") and hasattr(t, "y"):
                out.append((t.x, t.y))
            elif isinstance(t, (tuple, list)) and len(t) >= 2:
                out.append((int(t[0]), int(t[1])))
        return out

    def _convert_path_tiles_to_train_path(tiles):
        """Convert path dispatcher tiles to train path format."""
        coords = _path_tiles_to_coords(tiles)
        return convert_tiles_to_train_path(coords)

    def _parse_acquisition_mode(raw):
        if not raw:
            return AcquisitionMode.ALL_OR_NOTHING
        if isinstance(raw, AcquisitionMode):
            return raw
        try:
            return AcquisitionMode(raw)
        except ValueError:
            return AcquisitionMode.ALL_OR_NOTHING

    @app.route("/api/paths", methods=["POST"])
    def create_path():
        """Create a new path for a train.

        Request body (v1):
        {
            "train_id": "TRAIN_001",
            "requested_destination_block_id": "BLK_002",
            "requested_destination_direction": "forward",
            "acquisition_mode": "all_or_nothing"
        }

        Returns:
            PathAcquisitionResult JSON.
        """
        data = request.json or {}

        # Reject legacy keys explicitly.
        if "start_block_id" in data or "end_block_id" in data:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            "v1 wire format: pass requested_destination_block_id "
                            "and requested_destination_direction; do not pass "
                            "start_block_id/end_block_id"
                        ),
                    }
                ),
                400,
            )

        train_id = data.get("train_id")
        requested_destination_block_id = data.get("requested_destination_block_id")
        requested_destination_direction = data.get(
            "requested_destination_direction", "forward"
        )
        acquisition_mode = _parse_acquisition_mode(data.get("acquisition_mode"))

        if not train_id:
            return jsonify({"error": "train_id required"}), 400
        if not requested_destination_block_id:
            return (
                jsonify({"error": "requested_destination_block_id required"}),
                400,
            )

        try:
            with state.simulation_lock:
                if train_id not in state.network.trains:
                    return (
                        jsonify({"error": f"Train {train_id} not found"}),
                        404,
                    )

                result = state.path_dispatcher.create_path(
                    train_id=train_id,
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    acquisition_mode=acquisition_mode,
                )

                if result.success and result.path_id:
                    state.network.update_signals_from_blocks()
                    path = state.path_dispatcher.get_path(result.path_id)
                    train = state.network.trains[train_id]
                    if path is not None:
                        raw_path = _convert_path_tiles_to_train_path(path.path_tiles)
                        success, error = assign_complete_path_to_train(
                            state.network, train, raw_path
                        )
                        if not success:
                            train.current_path = raw_path
                            train.path_index = 0
                        train.assigned_path_id = path.path_id

                        # Set destination on controller for ATO.
                        sim_controller = state.sim_controller
                        controller = None
                        if sim_controller:
                            getter = (
                                getattr(sim_controller, "get_or_create_controller", None)
                                or getattr(
                                    sim_controller, "_get_or_create_controller", None
                                )
                            )
                            if callable(getter):
                                try:
                                    controller = getter(train)
                                except TypeError:
                                    try:
                                        controller = getter(train.train_id)
                                    except Exception:
                                        controller = None
                        if controller and path.authority_end_block_id:
                            controller.ato_destination_block_id = (
                                path.authority_end_block_id
                            )
                            train.ato_destination_block_id = (
                                path.authority_end_block_id
                            )

            return (
                jsonify(result.to_dict()),
                200 if result.success else 400,
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/paths", methods=["GET"])
    def list_paths():
        """List all active paths.

        Query params:
            train_id: Filter by train
            block_id: Filter by block usage (in resource_sequence blocks)
            status: Filter by status
        """
        train_id = request.args.get("train_id")
        block_id = request.args.get("block_id")
        status = request.args.get("status")

        with state.simulation_lock:
            paths = state.path_dispatcher.get_active_paths()

        if train_id:
            paths = [p for p in paths if p.train_id == train_id]
        if block_id:
            paths = [p for p in paths if block_id in p.get_all_blocks()]
        if status:
            paths = [p for p in paths if p.status.value == status]

        return jsonify(
            {"paths": [p.to_dict() for p in paths], "count": len(paths)}
        )

    @app.route("/api/paths/<path_id>", methods=["GET"])
    def get_path(path_id):
        """Get details of a specific path."""
        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
        if not path:
            return jsonify({"error": f"Path {path_id} not found"}), 404
        return jsonify(path.to_dict())

    @app.route("/api/paths/<path_id>", methods=["DELETE"])
    def cancel_path(path_id):
        """Cancel a path and release resources.

        Returns CancellationResult JSON.
        """
        # `force` retained for legacy callers but no longer used in v1.
        _ = request.args.get("force", "").lower() == "true"

        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            # Stop train ATO before cancelling path so it brakes to a stop.
            train_id = path.train_id
            if train_id and state.sim_controller:
                train = state.network.trains.get(train_id)
                if train and train.assigned_path_id == path_id:
                    sim_controller = state.sim_controller
                    controller = None
                    getter = (
                        getattr(sim_controller, "get_or_create_controller", None)
                        or getattr(
                            sim_controller, "_get_or_create_controller", None
                        )
                    )
                    if callable(getter):
                        try:
                            controller = getter(train)
                        except TypeError:
                            try:
                                controller = getter(train.train_id)
                            except Exception:
                                controller = None
                    if controller is not None:
                        if getattr(controller, "ato_running", False):
                            stop_fn = getattr(controller, "ato_stop", None)
                            if callable(stop_fn):
                                stop_fn()
                        controller.ato_destination_block_id = None
                        controller.ato_path_id = None
                    train.ato_destination_block_id = None
                    train.assigned_path_id = None
                    train.current_path = None
                    train.path_index = 0
                    train.reserved_blocks = []

            result = state.path_dispatcher.cancel_path(
                path_id,
                cancellation_kind=PathCancellationKind.OPERATOR_CANCELLED,
                reason="operator_cancelled",
            )

            if result.success:
                state.network.update_signals_from_blocks()

        return jsonify(result.to_dict()), 200 if result.success else 400

    @app.route("/api/paths/<path_id>/extend", methods=["PUT"])
    def extend_path(path_id):
        """Extend an existing path.

        Request body (v1):
        {
            "requested_destination_block_id": "BLK_003",
            "requested_destination_direction": "forward",
            "acquisition_mode": "all_or_nothing"
        }

        Returns:
            PathAcquisitionResult JSON (with result_kind=EXTENDED on success).
        """
        data = request.json or {}

        # Reject legacy key.
        if "new_end_block_id" in data and "requested_destination_block_id" not in data:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            "v1 wire format: use requested_destination_block_id "
                            "instead of new_end_block_id"
                        ),
                    }
                ),
                400,
            )

        requested_destination_block_id = data.get("requested_destination_block_id")
        requested_destination_direction = data.get(
            "requested_destination_direction", "forward"
        )
        acquisition_mode = _parse_acquisition_mode(data.get("acquisition_mode"))

        if not requested_destination_block_id:
            return (
                jsonify({"error": "requested_destination_block_id required"}),
                400,
            )

        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            result = state.path_dispatcher.extend_path(
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                acquisition_mode=acquisition_mode,
            )

            if result.success and path.train_id:
                train = state.network.trains.get(path.train_id)
                if train:
                    train.ato_destination_block_id = (
                        result.authority_end_block_id
                        or requested_destination_block_id
                    )
                    train.current_path = _convert_path_tiles_to_train_path(
                        path.path_tiles
                    )
                    sim_controller = state.sim_controller
                    if sim_controller:
                        getter = (
                            getattr(sim_controller, "get_or_create_controller", None)
                            or getattr(
                                sim_controller, "_get_or_create_controller", None
                            )
                        )
                        controller = None
                        if callable(getter):
                            try:
                                controller = getter(train)
                            except TypeError:
                                try:
                                    controller = getter(train.train_id)
                                except Exception:
                                    controller = None
                        if controller is not None:
                            controller.ato_destination_block_id = (
                                result.authority_end_block_id
                                or requested_destination_block_id
                            )

            if result.success:
                state.network.update_signals_from_blocks()

        return jsonify(result.to_dict()), 200 if result.success else 400

    @app.route("/api/paths/<path_id>/reachable-extensions", methods=["GET"])
    def get_path_reachable_extensions(path_id):
        """Get all blocks reachable from a path's end in the FORWARD direction."""
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
            return (
                jsonify(
                    {
                        "path_id": path_id,
                        "reachable_endpoints": [],
                        "count": 0,
                        "exit_direction": None,
                        "error": error,
                    }
                ),
                400,
            )

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
        """Mark a path as completed and release all resources."""
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
        """Find all possible paths between two blocks (geometry only).

        Request body:
        {
            "start_block_id": "BLK_001",
            "end_block_id": "BLK_002",
            "train_id": "TRAIN_001"
        }
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

        # PathSegment to_dict — derive from fields directly since this is a
        # simple value type without a to_dict method.
        return jsonify(
            {
                "paths": [
                    {
                        "blocks": p.blocks,
                        "tiles": [list(t) for t in p.tiles],
                        "distance_cm": p.distance_cm,
                        "required_switches": [
                            {"position": list(pos), "state": int(state)}
                            for pos, state in p.required_switches.items()
                        ],
                    }
                    for p in paths
                ],
                "count": len(paths),
                "start_block_id": start_block_id,
                "end_block_id": end_block_id,
            }
        )

    @app.route("/api/dispatcher/reachable/<block_id>", methods=["GET"])
    def get_reachable_endpoints(block_id):
        """Get all blocks reachable from a starting block."""
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
