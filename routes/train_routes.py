"""
Train-Route Routes - CRUD on Route definitions, activation/deactivation,
per-train status, run history, and target-pair viability checks.

Route *runs* (the live RouteRun list) live in `route_runs.py` — this module
deals with the route *definitions* and their immediate lifecycle.
"""

import time
import uuid
from typing import Optional

from flask import jsonify, request

from app_state import AppState
from enums import TrainState
from train_route import Route, RouteTask


def _coerce_route_task(item: dict) -> RouteTask:
    """Build a RouteTask from a JSON request item.

    Requires the v1 shape: ``task_kind`` (``"waypoint"|"stop"|"dwell"``) and
    ``task_location`` as the structured dict form
    ``{"block_id"|"platform_id"|"station_id": "..."}``. Other fields
    (``task_direction``, ``task_time``, ``target_time``) follow the
    ``RouteTask`` schema.
    """
    return RouteTask.from_dict(dict(item or {}))


def register(app, state: AppState) -> None:
    """Register train-route routes."""

    @app.route("/api/train-routes", methods=["GET"])
    def list_train_routes():
        routes = []
        for route in state.sim_controller.route_executor.route_registry.values():
            routes.append(route.to_dict())
        return jsonify({"routes": routes})

    @app.route("/api/train-routes", methods=["POST"])
    def create_train_route():
        from train_route import RouteMode

        data = request.get_json() or {}

        route_id = data.get("route_id") or f"ROUTE-{uuid.uuid4().hex[:6].upper()}"
        name = data.get("name", "")
        mode_str = data.get("mode", "one_off")

        try:
            mode = RouteMode(mode_str)
        except ValueError:
            mode_map = {
                "one_way": RouteMode.ONE_OFF,
                "loop": RouteMode.CIRCULAR,
            }
            mode = mode_map.get(mode_str, RouteMode.ONE_OFF)

        route = Route(route_id=route_id, name=name, mode=mode)

        # Per design_new.md "Route edit timestamps": bump last_edited_at if any
        # of the five edit fields (name/tasks/mode/priority/acquisition_mode)
        # was actually supplied. A bare create with only route_id leaves it None.
        edit_fields_present = any(
            k in data for k in ("name", "tasks", "mode", "priority", "acquisition_mode")
        )

        for task_data in data.get("tasks", []):
            route.add_task(_coerce_route_task(task_data))

        assigned_train_ids = data.get("assigned_train_ids", [])
        for train_id in assigned_train_ids:
            if train_id in state.network.trains:
                route.assign_train(train_id)

        if edit_fields_present:
            route.last_edited_at = time.time()

        state.sim_controller.route_executor.register_route(route)

        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>", methods=["GET"])
    def get_train_route(route_id):
        route = state.sim_controller.route_executor.route_registry.get(route_id)
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
        from train_route import RouteMode

        with state.simulation_lock:
            route = state.sim_controller.route_executor.route_registry.get(route_id)
            if not route:
                return jsonify({"error": f"Route '{route_id}' not found"}), 404

            data = request.json or {}

            # Per design_new.md "Route edit timestamps": bump last_edited_at iff
            # at least one of the five definition-edit fields is actually
            # written. assigned_train_ids changes are explicitly excluded.
            definition_edit_touched = False

            if "name" in data:
                route.name = data["name"]
                definition_edit_touched = True

            if "mode" in data:
                mode_str = data["mode"]
                try:
                    route.mode = RouteMode(mode_str)
                except ValueError:
                    mode_map = {
                        "one_way": RouteMode.ONE_OFF,
                        "loop": RouteMode.CIRCULAR,
                    }
                    if mode_str in mode_map:
                        route.mode = mode_map[mode_str]
                    else:
                        return jsonify({"error": f"Invalid mode '{mode_str}'"}), 400
                definition_edit_touched = True

            if "tasks" in data:
                new_tasks = []
                for task_data in data["tasks"]:
                    if not isinstance(task_data, dict):
                        return jsonify({"error": "task entry must be an object"}), 400
                    if not task_data.get("task_location"):
                        return jsonify(
                            {"error": "task_location required for each task"}
                        ), 400
                    new_tasks.append(_coerce_route_task(task_data))

                route.tasks.clear()
                for task in new_tasks:
                    route.add_task(task)
                definition_edit_touched = True

                # Per design_new.md line 1074, structural route edits should
                # tear down active `RouteRun`s and let Phase 1 allocate fresh
                # runs. The legacy in-place cursor clamp here was an admin
                # shortcut that is incompatible with the derived
                # `current_waypoint_index`. Operator/admin tooling that
                # actually needs to retarget mid-flight should call
                # `controller.route_hooks.phase_latches.set_route_abort(...)` for each
                # affected train (see `PUT /api/trains/<id>/assign-route`).

            if "priority" in data:
                try:
                    route.priority = int(data["priority"])
                except (TypeError, ValueError):
                    return jsonify({"error": "priority must be an integer"}), 400
                definition_edit_touched = True

            if "acquisition_mode" in data:
                from runtime_payloads import AcquisitionMode
                try:
                    route.acquisition_mode = AcquisitionMode(data["acquisition_mode"])
                except ValueError:
                    return jsonify(
                        {"error": f"Invalid acquisition_mode '{data['acquisition_mode']}'"}
                    ), 400
                definition_edit_touched = True

            if "assigned_train_ids" in data:
                requested_train_ids = set(data.get("assigned_train_ids", []))
                current_train_ids = set(route.assigned_train_ids)

                for train_id in current_train_ids - requested_train_ids:
                    route.unassign_train(train_id)

                for train_id in requested_train_ids - current_train_ids:
                    if train_id in state.network.trains:
                        route.assign_train(train_id)

            if definition_edit_touched:
                route.last_edited_at = time.time()

        return jsonify(route.to_dict())

    @app.route("/api/train-routes/<route_id>", methods=["DELETE"])
    def delete_train_route(route_id):
        registry = state.sim_controller.route_executor.route_registry
        if route_id not in registry:
            return jsonify({"error": "Route not found"}), 404

        route = registry[route_id]
        previously_assigned = list(route.assigned_train_ids)
        for train_id in previously_assigned:
            route.unassign_train(train_id)

        if state.sim_controller and state.sim_controller.route_executor:
            for train_id in previously_assigned:
                state.sim_controller.route_executor.execution_states.pop(train_id, None)

        state.sim_controller.route_executor.unregister_route(route_id)
        return jsonify({"status": "success", "message": f"Route {route_id} deleted"})

    @app.route("/api/train-routes/<route_id>/activate", methods=["POST"])
    def activate_train_route(route_id):
        route = state.sim_controller.route_executor.route_registry.get(route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        route.activate(state.sim_controller.simulation_time)
        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>/deactivate", methods=["POST"])
    def deactivate_train_route(route_id):
        route = state.sim_controller.route_executor.route_registry.get(route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        route.deactivate(state.sim_controller)
        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>/status", methods=["GET"])
    def get_train_route_status(route_id):
        """Get per-train execution status for a route."""
        route = state.sim_controller.route_executor.route_registry.get(route_id)
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

                    controller = route_executor.controllers.get(train_id)
                    if controller is not None:
                        status["at_platform"] = (
                            controller.ops.state == TrainState.RUNNING_AT_STATION
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
                            dwell_started_at = exec_state.task_started_at(
                                next_idx
                            )
                            if dwell_started_at is not None:
                                status["dwell_remaining_sec"] = (
                                    task.get_remaining_dwell_time(
                                        state.sim_controller.simulation_time,
                                        dwell_started_at,
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
        timestamps. Reads existing run state only — no new persisted log, no
        mutation.
        """
        route = state.sim_controller.route_executor.route_registry.get(route_id)
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

        block_id = route_executor._resolve_task_target_block(task)
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

          - ``[from_target, to_target]`` — legacy list form. Treated as
            ``direction == "either"``.
          - ``{"from": ..., "to": ..., "direction": ...}`` — dict form.
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
            return route_executor._resolve_task_target_block(task)

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
