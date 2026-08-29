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
from notification_center import Kind
from runtime_payloads import (
    AcquisitionMode,
    ErrorRecord,
)
from services import train_route_service, train_service
from services.exceptions import NotFoundError, ValidationError, ConflictError


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
            return AcquisitionMode.BEST_EFFORT
        if isinstance(raw, AcquisitionMode):
            return raw
        try:
            return AcquisitionMode(raw)
        except ValueError:
            return AcquisitionMode.BEST_EFFORT

    def _assign_one_off_route(
        train_id,
        destination_block_id,
        destination_direction,
        acquisition_mode,
        manual_override,
    ):
        """Build a one-off operator route toward `destination_block_id` and
        assign the train to it (design_new.md:655-656, Quick Route / operator
        retarget).

        The handler never calls PathDispatcher.create_path / extend_path
        directly. It creates a RouteMode.ONE_OFF route with a single stop task,
        activates it, and assigns the train through
        `train_service.assign_route(force=True)` — which writes the Action
        route-abort latch for any current run so Action tears it down, and the
        next eligible Plan builds the path, passing the route's
        `manual_override` down the acquisition chain. Returns the new route_id.
        """
        route = train_route_service.create_route(
            state,
            name=f"Quick: {train_id} -> {destination_block_id}",
            mode="one_off",
            tasks=[
                {
                    "task_kind": "stop",
                    "task_location": {"block_id": destination_block_id},
                    "task_direction": destination_direction,
                }
            ],
            acquisition_mode=acquisition_mode.value,
            manual_override=manual_override,
        )
        train_route_service.activate_route(state, route.route_id)
        train_service.assign_route(
            state, train_id, route_id=route.route_id, force=True
        )
        return route.route_id

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
        # Operator override flag per design_new.md "Availability flag REST API
        # surface" canonical home (under TrackResource). When True, manual
        # blocks/switches participate in candidate search and acquisition.
        # Disabled resources are always excluded regardless.
        manual_override = bool(data.get("manual_override", False))

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

                # design_new.md:655 (Quick Route): construct a one-off route and
                # assign the train; Plan builds the path. The handler must NOT
                # call PathDispatcher.create_path inline (request handlers do not
                # change dispatcher path records — see "Layered responsibilities").
                route_id = _assign_one_off_route(
                    train_id,
                    requested_destination_block_id,
                    requested_destination_direction,
                    acquisition_mode,
                    manual_override,
                )
            return (
                jsonify(
                    {
                        "success": True,
                        "status": "accepted",
                        "route_id": route_id,
                        "train_id": train_id,
                    }
                ),
                202,
            )
        except (ValidationError, ConflictError) as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except NotFoundError as e:
            return jsonify({"success": False, "error": str(e)}), 404
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    def _enrich_path_with_direction(payload: dict) -> dict:
        """Add `terminal_tile` + `terminal_heading` to a path payload.

        The block-relative `requested_destination_direction` is resolved
        through `BlockManager.direction_info` (per `design_new.md`
        "Block model" — `Block.ordered_tiles` is the canonical geometry)
        so the UI can render the decorated `F-(x,y)-E` label without a
        second fetch. Read-only enrichment.
        """
        block_id = payload.get("authority_end_block_id") or payload.get(
            "requested_destination_block_id"
        )
        direction = payload.get("requested_destination_direction")
        if not block_id or not direction:
            payload["terminal_tile"] = None
            payload["terminal_heading"] = None
            return payload
        info = state.network.block_manager.direction_info(block_id, direction)
        payload["terminal_tile"] = info["terminal_tile"] if info else None
        payload["terminal_heading"] = info["terminal_heading"] if info else None
        return payload

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
            # Per design_new.md ~line 80, terminal (COMPLETED/CANCELLED)
            # rows are retained in `paths_by_id` for history. Read the
            # full registry here so the `status` filter can surface
            # historical paths as well.
            paths = list(state.path_dispatcher.paths_by_id.values())

        if train_id:
            paths = [p for p in paths if p.train_id == train_id]
        if block_id:
            paths = [p for p in paths if block_id in p.get_all_blocks()]
        if status:
            paths = [p for p in paths if p.status.value == status]

        out = [_enrich_path_with_direction(p.to_dict()) for p in paths]
        return jsonify({"paths": out, "count": len(out)})

    @app.route("/api/paths/<path_id>", methods=["GET"])
    def get_path(path_id):
        """Get details of a specific path."""
        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
        if not path:
            return jsonify({"error": f"Path {path_id} not found"}), 404
        return jsonify(_enrich_path_with_direction(path.to_dict()))

    @app.route("/api/paths/<path_id>", methods=["DELETE"])
    def cancel_path(path_id):
        """Request cancellation of a path.

        v1 semantics (per `design_new.md` lines 270, 351-352, 1003): API
        callers must not invoke `PathDispatcher.cancel_path(...)` inline.
        The handler writes `controller.route_hooks.phase_latches.path_cancel_request`
        and returns immediately with ``status: "queued"``. Action of the
        simulation tick consumes the latch and calls
        ``PathDispatcher.cancel_path(...)`` once the train is fully stopped.
        Block reservations and switch locks are released by the same Action
        consumer; clients should refresh path state on a subsequent poll
        instead of mutating local UI state.

        v1 paths always carry a `train_id` (per `design_new.md` line 870)
        and every train has a `TrainController`. There is no inline fallback.

        Returns:
            200 OK with ``{"status": "queued", ...}`` when latched.
        """
        with state.simulation_lock:
            path = state.path_dispatcher.get_path(path_id)
            if not path:
                return jsonify({"error": f"Path {path_id} not found"}), 404

            train_id = path.train_id
            if not train_id:
                return jsonify(
                    {
                        "error": "Path is not bound to a train; v1 paths require train_id",
                        "path_id": path_id,
                    }
                ), 400

            if not state.sim_controller:
                return jsonify(
                    {"error": "Simulation runtime is not active", "path_id": path_id}
                ), 503

            train = state.network.trains.get(train_id)
            if not train:
                return jsonify(
                    {
                        "error": f"Train '{train_id}' not found for path",
                        "path_id": path_id,
                    }
                ), 404

            controller = state.sim_controller._get_or_create_controller(train)
            if controller is None:
                return jsonify(
                    {
                        "error": f"Controller unavailable for train '{train_id}'",
                        "path_id": path_id,
                    }
                ), 503

            controller.route_hooks.phase_latches.set_path_cancel_request(
                ErrorRecord("api cancel", "operator_cancelled"), Kind.INFO
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
            PathAcquisitionResult JSON (appended_resource_count / reached_target
            describe the outcome on success).
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
        # Operator override flag per design_new.md "Availability flag REST API
        # surface" canonical home (under TrackResource).
        manual_override = bool(data.get("manual_override", False))

        if not requested_destination_block_id:
            return (
                jsonify({"error": "requested_destination_block_id required"}),
                400,
            )

        try:
            with state.simulation_lock:
                path = state.path_dispatcher.get_path(path_id)
                if not path:
                    return jsonify({"error": f"Path {path_id} not found"}), 404

                # design_new.md:656 — operator retarget: abort the current run and
                # assign a fresh one-off route toward the new destination. The
                # handler must NOT call PathDispatcher.extend_path inline;
                # `assign_route(force=True)` writes the route-abort latch and the
                # next eligible Plan grows the path toward the new target.
                train_id = path.train_id
                if not train_id:
                    return (
                        jsonify({"error": f"Path {path_id} has no train"}),
                        400,
                    )
                route_id = _assign_one_off_route(
                    train_id,
                    requested_destination_block_id,
                    requested_destination_direction,
                    acquisition_mode,
                    manual_override,
                )
            return (
                jsonify(
                    {
                        "success": True,
                        "status": "accepted",
                        "route_id": route_id,
                        "train_id": train_id,
                    }
                ),
                202,
            )
        except (ValidationError, ConflictError) as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except NotFoundError as e:
            return jsonify({"success": False, "error": str(e)}), 404
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

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

    @app.route("/api/dispatcher/reachable-for-train/<train_id>", methods=["GET"])
    def get_reachable_endpoints_for_train(train_id):
        """Span-aware reachable endpoints for a placed train.

        Unlike the block-keyed endpoint above (topological, any direction),
        this answers where the train can actually go from its current span:
        forward out of its head or in reverse out of its tail, never routing
        back through its own body. Used by the Quick Route tool.
        """
        include_stations = (
            request.args.get("include_stations", "true").lower() != "false"
        )

        if train_id not in state.network.trains:
            return jsonify({"error": f"Train {train_id} not found"}), 404

        with state.simulation_lock:
            reachable = state.path_dispatcher.get_reachable_endpoints_for_train(
                train_id, include_stations=include_stations
            )

        return jsonify(
            {
                "train_id": train_id,
                "reachable_endpoints": reachable,
                "count": len(reachable),
            }
        )
