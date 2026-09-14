"""
Train-Route Routes - CRUD on Route definitions, activation/deactivation,
per-train status, run history, and target-pair viability checks.

Route *runs* (the live RouteRun list) live in `route_runs.py` -- this module
deals with the route *definitions* and their immediate lifecycle.

The CRUD endpoints delegate to `services.train_route_service`; this module
parses JSON, calls the service, and formats the response. Service
exceptions are translated to HTTP status codes here (ValidationError -> 400,
NotFoundError -> 404).
"""

from typing import Optional

from flask import jsonify, request

from app_state import AppState
from services import NotFoundError, ValidationError
from services import train_route_service
from train_route import RouteTask


def register(app, state: AppState) -> None:
    """Register train-route routes."""

    @app.route("/api/train-routes", methods=["GET"])
    def list_train_routes():
        routes = [r.to_dict() for r in train_route_service.list_routes(state)]
        return jsonify({"routes": routes})

    @app.route("/api/train-routes", methods=["POST"])
    def create_train_route():
        data = request.get_json() or {}

        # Per design_new.md "Route edit timestamps": bump last_edited_at if any
        # of the edit fields (name/tasks/mode/priority) was actually supplied.
        # A bare create with only route_id leaves it None.
        edit_fields_present = any(
            k in data for k in train_route_service.EDIT_FIELDS
        )

        try:
            route = train_route_service.create_route(
                state,
                route_id=data.get("route_id"),
                name=data.get("name", ""),
                mode=data.get("mode", "one_off"),
                tasks=data.get("tasks", []),
                assigned_train_ids=data.get("assigned_train_ids", []),
                priority=data.get("priority"),
                manual_override=data.get("manual_override"),
                start_mode=data.get("start_mode"),
                start_task_index=data.get("start_task_index"),
                edit_fields_present=edit_fields_present,
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>", methods=["GET"])
    def get_train_route(route_id):
        route = train_route_service.get_route(state, route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404
        return jsonify(route.to_dict())

    @app.route("/api/train-routes/<route_id>", methods=["PUT"])
    def update_train_route(route_id: str):
        """Update an existing train route.

        Request body:
        {
            "name": str (optional),
            "mode": str (optional, "one_off"|"circular"|"round_trip"),
            "tasks": [
                {
                    "task_kind": "waypoint"|"stop"|"dwell",
                    "task_location": {"block_id"|"platform_id"|"station_id": "..."},
                    "task_direction": "forward"|"reverse"|"either",
                    "task_time": float (optional),
                    "target_time": float (optional)
                },
                ...
            ] (optional, replaces all tasks)
        }

        Returns:
            200 OK with updated route data
            400 Bad Request if validation fails
            404 Not Found if route doesn't exist
        """
        data = request.json or {}

        # Per design_new.md "RouteRun overrides": each allowlisted field may
        # arrive in two shapes — bare `{<field>: value}` (defaults to soft
        # push) or `{<field>: {"value": ..., "push": "force"|"soft"}}`.
        # Unpack the value and the optional per-field push policy.
        def _unpack(field_name: str):
            if field_name not in data:
                return None, None
            raw = data[field_name]
            if (
                isinstance(raw, dict)
                and "value" in raw
                and field_name in train_route_service.OVERRIDE_ALLOWLIST
            ):
                return raw.get("value"), raw.get("push", "soft")
            return raw, "soft"

        priority_value, priority_push = _unpack("priority")
        push_policies = {}
        if "priority" in data:
            push_policies["priority"] = priority_push

        with state.simulation_lock:
            try:
                route = train_route_service.update_route(
                    state,
                    route_id,
                    name=data["name"] if "name" in data else None,
                    mode=data["mode"] if "mode" in data else None,
                    tasks=data["tasks"] if "tasks" in data else None,
                    priority=priority_value if "priority" in data else None,
                    manual_override=(
                        data["manual_override"] if "manual_override" in data else None
                    ),
                    start_mode=data["start_mode"] if "start_mode" in data else None,
                    start_task_index=data.get("start_task_index"),
                    start_task_index_present=("start_task_index" in data),
                    assigned_train_ids=(
                        data["assigned_train_ids"]
                        if "assigned_train_ids" in data
                        else None
                    ),
                    push_policies=push_policies,
                )
            except NotFoundError as exc:
                return jsonify({"error": str(exc)}), 404
            except ValidationError as exc:
                return jsonify({"error": str(exc)}), 400

        # Per design_new.md "Route Edit And Reconciliation", structural route edits should
        # tear down active `RouteRun`s and let Plan allocate fresh
        # runs. The legacy in-place cursor clamp here was an admin
        # shortcut that is incompatible with the derived
        # `current_waypoint_index`. Operator/admin tooling that
        # actually needs to retarget mid-flight should call
        # `controller.route_hooks.phase_latches.set_route_abort(...)` for each
        # affected train (see `PUT /api/trains/<id>/assign-route`).

        return jsonify(route.to_dict())

    @app.route("/api/train-routes/<route_id>", methods=["DELETE"])
    def delete_train_route(route_id):
        try:
            train_route_service.delete_route(state, route_id)
        except NotFoundError:
            return jsonify({"error": "Route not found"}), 404
        return jsonify({"status": "success", "message": f"Route {route_id} deleted"})

    @app.route("/api/train-routes/<route_id>/activate", methods=["POST"])
    def activate_train_route(route_id):
        try:
            route = train_route_service.activate_route(state, route_id)
        except NotFoundError:
            return jsonify({"error": "Route not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>/deactivate", methods=["POST"])
    def deactivate_train_route(route_id):
        try:
            route = train_route_service.deactivate_route(state, route_id)
        except NotFoundError:
            return jsonify({"error": "Route not found"}), 404
        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>/status", methods=["GET"])
    def get_train_route_status(route_id):
        """Get per-train execution status for a route."""
        route = train_route_service.get_route(state, route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        route_executor = (
            state.sim_controller.route_executor if state.sim_controller else None
        )
        trains_status = []

        for train_id in route.assigned_train_ids:
            train = state.network.trains.get(train_id)
            if not train:
                continue

            status = {
                "train_id": train_id,
                "current_waypoint_index": None,
                "direction": "forward",
                "current_task_kind": None,
                "current_task_location": None,
                "at_platform": False,
                "dwell_remaining_sec": None,
            }

            if route_executor:
                exec_state = route_executor.execution_states.get(train_id)
                if exec_state and exec_state.route_id == route.route_id:
                    # Per design, `current_waypoint_index` is Optional[int]
                    # (largest finished walk-position, or None). For the
                    # status read we want the next planning index (the task
                    # the train is currently working on).
                    next_idx = exec_state.next_planning_index
                    status["current_waypoint_index"] = exec_state.current_waypoint_index
                    status["direction"] = exec_state.direction

                    controller = state.sim_controller.controllers.get(train_id)
                    if controller is not None:
                        # A dwelling train is READY (dwell is not a controller
                        # state): it is "at platform" when its current task is a
                        # dwell and it is stopped there.
                        _dwell_task = route.get_task_at(next_idx)
                        status["at_platform"] = bool(
                            _dwell_task is not None
                            and _dwell_task.task_kind == "dwell"
                            and controller.is_stopped()
                        )

                    task = route.get_task_at(next_idx)
                    if task:
                        from train_route import task_location_to_dict

                        status["current_task_kind"] = task.task_kind
                        status["current_task_location"] = task_location_to_dict(
                            task.task_location
                        )
                        if (
                            task.task_kind == "dwell"
                            and next_idx < len(exec_state.task_runs)
                        ):
                            status["dwell_remaining_sec"] = (
                                state.sim_controller.route_executor
                                .dwell_remaining_seconds(
                                    exec_state, route, next_idx
                                )
                            )

            trains_status.append(status)

        return jsonify(
            {
                "route_id": route.route_id,
                "is_active": route.is_active,
                "mode": route.mode.value,
                "trains": trains_status,
            }
        )

    @app.route("/api/train-routes/<route_id>/history", methods=["GET"])
    def get_train_route_history(route_id):
        """Read-only run history for a route.

        Aggregates every `RouteRun` for the route (running + archived, across
        all trains) into a flat timeline of run-start and per-task completion
        timestamps. Reads existing run state only -- no new persisted log, no
        mutation.
        """
        route = train_route_service.get_route(state, route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        route_executor = state.sim_controller.route_executor
        runs_out = []
        for run in route_executor.history_for_route(route_id):
            tasks_out = []
            for i, task_run in enumerate(run.task_runs):
                tasks_out.append(
                    {
                        "task_index": task_run.task_index,
                        "started_at": run.task_started_at(i),
                        "finished_at": task_run.finished_at,
                        "delay_class": task_run.delay_class,
                    }
                )
            runs_out.append(
                {
                    "train_id": run.train_id,
                    "run_number": run.run_number,
                    "started_at": run.started_at,
                    "status": run.current_route_status.value,
                    "tasks": tasks_out,
                }
            )

        return jsonify(
            {
                "route_id": route.route_id,
                "activated_at": route.started_at,
                "created_at": route.created_at,
                "last_edited_at": route.last_edited_at,
                "is_active": route.is_active,
                "runs": runs_out,
            }
        )

    @app.route("/api/train-routes/reachable-from", methods=["GET"])
    def reachable_from_target():
        """Blocks/stations reachable from a route-task target.

        Query params: ``type`` (``block``/``platform``/``station``) and
        ``id``. Resolves the target to a block via the same network-only
        resolver the runtime uses, then returns the topological
        reachable-endpoints set (blocks + stations) for that block. Used by
        the route editor to highlight feasible next-waypoint tiles. Read-only.
        """
        target_type = (request.args.get("type") or "").strip()
        target_id = (request.args.get("id") or "").strip()
        key_by_type = {
            "block": "block_id",
            "platform": "platform_id",
            "station": "station_id",
        }
        if target_type not in key_by_type or not target_id:
            return jsonify(
                {"error": "type must be block|platform|station and id is required"}
            ), 400

        route_executor = getattr(state.sim_controller, "route_executor", None)
        if route_executor is None:
            return jsonify({"error": "Route executor unavailable"}), 503

        try:
            task = RouteTask(
                task_kind="waypoint",
                task_location={key_by_type[target_type]: target_id},
            )
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid target"}), 400

        block_id = route_executor.resolve_task_target_block(task)
        if not block_id:
            return jsonify({"error": "Could not resolve target to a block"}), 404

        with state.simulation_lock:
            reachable = state.path_dispatcher.get_reachable_endpoints(
                block_id, include_stations=True
            )

        return jsonify(
            {
                "type": target_type,
                "id": target_id,
                "block_id": block_id,
                "reachable_endpoints": reachable,
                "count": len(reachable),
            }
        )

    @app.route("/api/train-routes/check-viability", methods=["POST"])
    def check_train_route_viability():
        """Check viability between route-task target pairs.

        Each pair element is one of:

          - ``[from_target, to_target]`` -- legacy list form. Treated as
            ``direction == "either"``.
          - ``{"from": ..., "to": ..., "direction": ...}`` -- dict form.
            ``direction`` is one of ``"forward" | "reverse" | "either"``;
            it constrains the *terminal* traversal at ``to``. Default
            ``"either"``.

        Targets are canonical task-location dicts (exactly one of
        ``block_id`` / ``platform_id`` / ``station_id``) and are resolved
        to a block via the same runtime resolver the route executor uses.

        Viability delegates to ``PathDispatcher.find_paths`` so it
        respects ``Block.allowed_travel_directions`` along the whole walk
        and the destination's required terminal direction. The starting
        cab direction is left free, which matches the route-task
        semantics where the train may turn around at any stop/waypoint.
        """
        data = request.get_json() or {}
        pairs = data.get("pairs", [])

        route_executor = getattr(
            getattr(state, "sim_controller", None), "route_executor", None
        )

        def resolve_target_block(elem) -> Optional[str]:
            """Resolve a canonical target (dict or bare platform-id string) to
            a block id via the runtime resolver, or None when unresolvable."""
            if route_executor is None:
                return None
            try:
                task = RouteTask(task_kind="waypoint", task_location=elem)
            except (ValueError, TypeError):
                return None
            return route_executor.resolve_task_target_block(task)

        def parse_pair(pair):
            """Accept both legacy list form and new dict form."""
            if isinstance(pair, list) and len(pair) == 2:
                return pair[0], pair[1], "either"
            if isinstance(pair, dict):
                direction = pair.get("direction") or "either"
                if direction not in ("forward", "reverse", "either"):
                    direction = "either"
                return pair.get("from"), pair.get("to"), direction
            return None, None, None

        def best_candidate(start_block, end_block, direction):
            """Probe ``find_paths`` and return ``(winning_segment,
            resolved_direction)`` or ``(None, None)`` when no candidate
            survives the direction filter.

            For an explicit direction, only that direction is probed.
            For ``"either"``, both directions are probed and the shorter
            top candidate wins, matching the canonical candidate
            comparison ``(tile_count, walk_length_cm)``.
            """
            if start_block == end_block:
                # Degenerate same-block pair: viable with no walk.
                return None, "self"

            if direction in ("forward", "reverse"):
                segs = state.path_dispatcher.find_paths(
                    start_block, end_block,
                    train_id=None, direction_constraint=direction,
                )
                return (segs[0], direction) if segs else (None, None)

            fwd = state.path_dispatcher.find_paths(
                start_block, end_block,
                train_id=None, direction_constraint="forward",
            )
            rev = state.path_dispatcher.find_paths(
                start_block, end_block,
                train_id=None, direction_constraint="reverse",
            )
            fwd_best = fwd[0] if fwd else None
            rev_best = rev[0] if rev else None
            if fwd_best and rev_best:
                if (len(fwd_best.tiles), fwd_best.distance_cm) <= (
                    len(rev_best.tiles), rev_best.distance_cm
                ):
                    return fwd_best, "forward"
                return rev_best, "reverse"
            if fwd_best:
                return fwd_best, "forward"
            if rev_best:
                return rev_best, "reverse"
            return None, None

        results = []
        for pair in pairs:
            from_target, to_target, direction = parse_pair(pair)
            if from_target is None or to_target is None or direction is None:
                results.append({
                    "from": None,
                    "to": None,
                    "viable": False,
                    "error": "Invalid pair format",
                })
                continue

            from_block = resolve_target_block(from_target)
            to_block = resolve_target_block(to_target)
            if not from_block or not to_block:
                results.append({
                    "from": from_target,
                    "to": to_target,
                    "viable": False,
                    "error": "Could not resolve target to a block",
                })
                continue

            segment, resolved_dir = best_candidate(
                from_block, to_block, direction
            )

            if resolved_dir == "self":
                results.append({
                    "from": from_target,
                    "to": to_target,
                    "viable": True,
                    "hop_count": 0,
                    "distance_cm": 0.0,
                    "direction": direction,
                })
                continue

            if segment is None:
                results.append({
                    "from": from_target,
                    "to": to_target,
                    "viable": False,
                    "error": "No path found",
                    "requested_direction": direction,
                })
                continue

            results.append({
                "from": from_target,
                "to": to_target,
                "viable": True,
                "hop_count": len(segment.tiles),
                "distance_cm": float(segment.distance_cm),
                "direction": resolved_dir,
            })

        return jsonify({"results": results})
