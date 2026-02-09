"""
Miscellaneous Routes - Static files, simulation control, persistence.
"""

import time
import uuid
from datetime import datetime
from typing import Optional

from flask import send_from_directory, jsonify, request

from app_state import AppState
from simulation import build_network_payload
from train_route import Route, Waypoint


def register(app, state: AppState) -> None:
    """Register miscellaneous routes including static files, network endpoint, and simulation control."""

    # ========================================================================
    # Static Files and Core Routes
    # ========================================================================

    @app.route("/")
    def index():
        return send_from_directory("static", "index.html")

    @app.route("/<path:path>")
    def static_files(path):
        return send_from_directory("static", path)

    @app.route("/api/network", methods=["GET"])
    def get_network():
        return jsonify(build_network_payload(state.network, state.sim_controller))

    @app.route("/api/simulation/control", methods=["POST"])
    def control_simulation():
        data = request.json or {}
        action = data.get("action")

        if action == "start":
            state.simulation_running = True
        elif action == "stop":
            state.simulation_running = False
        elif action == "reset":
            state.simulation_running = False
        elif action == "set_speed":
            state.simulation_speed = data.get("speed", 1.0)

        return jsonify(
            {
                "status": "success",
                "running": state.simulation_running,
                "speed": state.simulation_speed,
            }
        )

    # ========================================================================
    # Layout Persistence API
    # ========================================================================

    @app.route("/api/layouts", methods=["GET"])
    def list_layouts():
        """List all saved layouts."""
        layouts = state.persistence_manager.list_layouts()
        return jsonify({"layouts": layouts})

    @app.route("/api/layouts", methods=["POST"])
    def save_layout():
        """Save current network state as a layout.

        Request body:
            name: Layout name (required)
            description: Optional description

        Saves both snapshot and steps in a single merged file.
        """
        data = request.get_json() or {}
        name = data.get("name", "").strip()
        description = data.get("description", "")

        if not name:
            return jsonify({"error": "Layout name is required"}), 400

        was_running = state.simulation_running
        state.simulation_running = False

        try:
            with state.simulation_lock:
                result = state.persistence_manager.save_merged_layout(
                    name, state.network, description
                )
                state.last_autosave_time = time.time()

            response = {
                "status": "success",
                "message": f"Layout '{name}' saved successfully",
                "path": result["path"],
                "steps_generated": result["steps_generated"],
                "steps_count": result["steps_count"],
            }

            # Notify user if steps failed to generate
            if result["steps_error"]:
                response["warning"] = (
                    f"Steps could not be generated: {result['steps_error']}"
                )

            return jsonify(response)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except IOError as e:
            return jsonify({"error": str(e)}), 500
        finally:
            state.simulation_running = was_running

    @app.route("/api/layouts/<name>/load", methods=["POST"])
    def load_layout(name):
        """Load a layout, replacing the current network state.

        Uses snapshot by default, falls back to steps if snapshot fails.
        """
        was_running = state.simulation_running
        state.simulation_running = False

        try:
            with state.simulation_lock:
                result = state.persistence_manager.load_layout_with_fallback(
                    name, state.network
                )

                if not result.get("success"):
                    return jsonify(
                        {
                            "error": result.get("error", "Failed to load layout"),
                            "method": result.get("method"),
                        }
                    ), 400

                state.sim_controller.network = state.network
                state.last_autosave_time = time.time()

            response = {
                "status": "success",
                "message": f"Layout '{name}' loaded successfully",
                "method": result.get("method"),
            }
            if result.get("recovery"):
                response["recovery"] = True
                response["message"] = (
                    f"Layout '{name}' recovered from steps (snapshot was corrupted)"
                )

            return jsonify(response)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            state.simulation_running = was_running

    @app.route("/api/layouts/<name>", methods=["DELETE"])
    def delete_layout(name):
        """Delete a saved layout."""
        if state.persistence_manager.delete_layout(name):
            return jsonify({"status": "success", "message": f"Layout '{name}' deleted"})
        else:
            return jsonify({"error": f"Layout '{name}' not found"}), 404

    # ========================================================================
    # Network Management API
    # ========================================================================

    @app.route("/api/network/clear", methods=["POST"])
    def clear_network():
        """Clear the current network, giving a fresh canvas.

        Returns counts of what was cleared for confirmation display.
        """
        was_running = state.simulation_running
        state.simulation_running = False

        try:
            with state.simulation_lock:
                # Count what we're clearing
                counts = {
                    "tracks": len(state.network.tracks),
                    "blocks": len(state.network.blocks),
                    "signals": len(state.network.signals),
                    "platforms": len(getattr(state.network, "platforms", {})),
                    "stations": len(state.network.station_groups),
                    "trains": len(state.network.trains),
                }

                # Clear everything
                state.network.tracks.clear()
                state.network.blocks.clear()
                state.network.signals.clear()
                state.network.platforms.clear()
                if hasattr(state.network, "station_groups"):
                    state.network.station_groups.clear()
                state.network.trains.clear()
                state.network.schedules.clear()

                # Update simulation controller reference
                state.sim_controller.network = state.network

            return jsonify(
                {"status": "success", "message": "Network cleared", "cleared": counts}
            )
        finally:
            state.simulation_running = was_running

    @app.route("/api/network/stats", methods=["GET"])
    def get_network_stats():
        """Get counts of items in the network (for checking if empty)."""
        with state.simulation_lock:
            return jsonify(
                {
                    "tracks": len(state.network.tracks),
                    "blocks": len(state.network.blocks),
                    "signals": len(state.network.signals),
                    "platforms": len(getattr(state.network, "platforms", {})),
                    "stations": len(state.network.station_groups),
                    "trains": len(state.network.trains),
                    "is_empty": len(state.network.tracks) == 0,
                }
            )

    # ========================================================================
    # Train Routes API
    # ========================================================================

    @app.route("/api/train-routes", methods=["GET"])
    def list_train_routes():
        routes = []
        for route in state.network.routes.values():
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

        waypoints_data = data.get("waypoints", [])
        for wp_data in waypoints_data:
            waypoint = Waypoint.from_dict(wp_data)
            route.add_waypoint(waypoint)

        assigned_train_ids = data.get("assigned_train_ids", [])
        for train_id in assigned_train_ids:
            if train_id in state.network.trains:
                route.assign_train(train_id)
                state.network.trains[train_id].assigned_route_id = route.route_id

        state.network.routes[route_id] = route

        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>", methods=["GET"])
    def get_train_route(route_id):
        route = state.network.routes.get(route_id)
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
            "waypoints": [
                {
                    "platform_id": str,
                    "dwell_time_sec": float,
                    "timing_mode": str ("none"|"relative"|"absolute"),
                    "target_time": float (optional)
                },
                ...
            ] (optional, replaces all waypoints)
        }

        Returns:
            200 OK with updated route data
            400 Bad Request if validation fails
            404 Not Found if route doesn't exist
        """
        from train_route import RouteMode

        with state.simulation_lock:
            route = state.network.routes.get(route_id)
            if not route:
                return jsonify({"error": f"Route '{route_id}' not found"}), 404

            data = request.json or {}

            if "name" in data:
                route.name = data["name"]

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

            if "waypoints" in data:
                waypoints_data = data["waypoints"]
                route.waypoints.clear()

                for wp_data in waypoints_data:
                    platform_id = wp_data.get("platform_id")
                    if not platform_id:
                        return jsonify(
                            {"error": "platform_id required for each waypoint"}
                        ), 400

                    waypoint = Waypoint.from_dict(wp_data)
                    route.add_waypoint(waypoint)

                if state.sim_controller and state.sim_controller.route_executor:
                    for (
                        exec_state
                    ) in state.sim_controller.route_executor.execution_states.values():
                        if exec_state.route_id == route_id:
                            exec_state.current_waypoint_index = min(
                                exec_state.current_waypoint_index,
                                max(0, len(route.waypoints) - 1),
                            )

            if "assigned_train_ids" in data:
                requested_train_ids = set(data.get("assigned_train_ids", []))
                current_train_ids = set(route.assigned_train_ids)

                for train_id in current_train_ids - requested_train_ids:
                    route.unassign_train(train_id)
                    train = state.network.trains.get(train_id)
                    if train and train.assigned_route_id == route.route_id:
                        train.assigned_route_id = None

                for train_id in requested_train_ids - current_train_ids:
                    if train_id in state.network.trains:
                        route.assign_train(train_id)
                        state.network.trains[
                            train_id
                        ].assigned_route_id = route.route_id

        return jsonify(route.to_dict())

    @app.route("/api/train-routes/<route_id>", methods=["DELETE"])
    def delete_train_route(route_id):
        if route_id not in state.network.routes:
            return jsonify({"error": "Route not found"}), 404

        route = state.network.routes[route_id]
        for train in state.network.trains.values():
            if train.assigned_route_id == route_id:
                train.assigned_route_id = None

        if state.sim_controller and state.sim_controller.route_executor:
            for train_id in list(route.assigned_train_ids):
                state.sim_controller.route_executor.execution_states.pop(train_id, None)

        del state.network.routes[route_id]
        return jsonify({"status": "success", "message": f"Route {route_id} deleted"})

    @app.route("/api/train-routes/<route_id>/activate", methods=["POST"])
    def activate_train_route(route_id):
        route = state.network.routes.get(route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        route.activate(state.sim_controller.simulation_time)
        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>/deactivate", methods=["POST"])
    def deactivate_train_route(route_id):
        route = state.network.routes.get(route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        route.deactivate()
        return jsonify({"status": "success", "route": route.to_dict()})

    @app.route("/api/train-routes/<route_id>/status", methods=["GET"])
    def get_train_route_status(route_id):
        """Get per-train execution status for a route."""
        route = state.network.routes.get(route_id)
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
                "current_waypoint_index": 0,
                "direction": 1,
                "current_waypoint_platform_id": None,
                "resolved_platform_id": None,
                "at_platform": False,
                "waiting_for_path": False,
                "dwell_remaining_sec": None,
            }

            if route_executor:
                exec_state = route_executor.execution_states.get(train_id)
                if exec_state and exec_state.route_id == route.route_id:
                    status["current_waypoint_index"] = exec_state.current_waypoint_index
                    status["direction"] = exec_state.direction
                    status["resolved_platform_id"] = exec_state.resolved_platform_id
                    status["at_platform"] = (
                        exec_state.arrived_at_platform_time is not None
                    )
                    status["waiting_for_path"] = exec_state.waiting_for_path

                    waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
                    if waypoint:
                        status["current_waypoint_platform_id"] = waypoint.platform_id
                        if exec_state.arrived_at_platform_time is not None:
                            route_start = (
                                exec_state.route_start_time
                                or route.started_at
                                or state.sim_controller.simulation_time
                            )
                            time_at_platform = (
                                state.sim_controller.simulation_time
                                - exec_state.arrived_at_platform_time
                            )
                            status["dwell_remaining_sec"] = (
                                waypoint.get_remaining_dwell_time(
                                    time_at_platform,
                                    route_start,
                                    state.sim_controller.simulation_time,
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

    @app.route("/api/train-routes/check-viability", methods=["POST"])
    def check_train_route_viability():
        """Check connectivity viability between platform waypoint pairs."""
        data = request.get_json() or {}
        pairs = data.get("pairs", [])

        def get_platform(platform_id: str):
            platforms = getattr(state.network, "platforms", None)
            if isinstance(platforms, dict) and platform_id in platforms:
                return platforms[platform_id]
            return None

        def get_platform_block_id(platform_id: str) -> Optional[str]:
            platform = get_platform(platform_id)
            if not platform:
                return None

            get_all_assigned_blocks = getattr(platform, "get_all_assigned_blocks", None)
            if callable(get_all_assigned_blocks):
                block_ids = get_all_assigned_blocks()
                if isinstance(block_ids, (set, list, tuple)) and block_ids:
                    return list(block_ids)[0]

            for x, y in getattr(platform, "positions", []):
                track = state.network.get_track(x, y)
                if track and track.block_section_id:
                    return track.block_section_id
            return None

        def build_block_graph() -> dict:
            graph = {}
            for (x, y), track in state.network.tracks.items():
                block_id = track.block_section_id
                if not block_id:
                    continue
                graph.setdefault(block_id, set())
                for edge in track.edges:
                    neighbor = state.network.get_neighbor(x, y, edge)
                    if not neighbor or not neighbor.block_section_id:
                        continue
                    if neighbor.block_section_id != block_id:
                        graph[block_id].add(neighbor.block_section_id)
                        graph.setdefault(neighbor.block_section_id, set()).add(block_id)
            return graph

        def shortest_hops(
            graph: dict, start_block: str, end_block: str
        ) -> Optional[int]:
            if start_block == end_block:
                return 0
            if start_block not in graph or end_block not in graph:
                return None

            queue = [(start_block, 0)]
            visited = {start_block}
            while queue:
                current, hops = queue.pop(0)
                for nxt in graph.get(current, set()):
                    if nxt in visited:
                        continue
                    if nxt == end_block:
                        return hops + 1
                    visited.add(nxt)
                    queue.append((nxt, hops + 1))
            return None

        graph = build_block_graph()
        results = []

        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                results.append(
                    {
                        "from": None,
                        "to": None,
                        "viable": False,
                        "error": "Invalid pair format",
                    }
                )
                continue

            from_platform, to_platform = pair
            from_block = get_platform_block_id(from_platform)
            to_block = get_platform_block_id(to_platform)

            if not from_block or not to_block:
                results.append(
                    {
                        "from": from_platform,
                        "to": to_platform,
                        "viable": False,
                        "error": "Missing platform or block assignment",
                    }
                )
                continue

            hops = shortest_hops(graph, from_block, to_block)
            if hops is None:
                results.append(
                    {
                        "from": from_platform,
                        "to": to_platform,
                        "viable": False,
                        "error": "No path found",
                    }
                )
                continue

            results.append(
                {
                    "from": from_platform,
                    "to": to_platform,
                    "viable": True,
                    "hop_count": hops,
                    "distance_cm": float(hops * 50.0),
                }
            )

        return jsonify({"results": results})

    # ========================================================================
    # Schedules API
    # ========================================================================

    @app.route("/api/schedules", methods=["GET"])
    def list_schedules():
        """List all train schedules."""
        from train import TrainSchedule

        schedules = []
        for schedule in state.network.schedules.values():
            stops_data = []
            for stop in schedule.stops:
                stops_data.append(
                    {
                        "station_pos": list(stop.station_pos),
                        "station_name": stop.station_name,
                        "arrival_time": stop.arrival_time.isoformat()
                        if stop.arrival_time
                        else None,
                        "departure_time": stop.departure_time.isoformat()
                        if stop.departure_time
                        else None,
                        "platform": stop.platform,
                    }
                )
            schedules.append(
                {
                    "schedule_id": schedule.schedule_id,
                    "train_id": schedule.train_id,
                    "stops": stops_data,
                    "start_time": schedule.start_time.isoformat()
                    if schedule.start_time
                    else None,
                    "end_time": schedule.end_time.isoformat()
                    if schedule.end_time
                    else None,
                    "priority": schedule.priority,
                }
            )
        return jsonify({"schedules": schedules})

    @app.route("/api/schedules", methods=["POST"])
    def create_schedule():
        """Create a new train schedule.

        Request body:
            train_id: Train to assign schedule to (required)
            stops: List of stops with station_name, arrival_time, departure_time
            priority: Schedule priority (optional, default 0)
        """
        from train import TrainSchedule, ScheduleStop
        from datetime import timedelta

        data = request.get_json() or {}

        train_id = data.get("train_id")
        if not train_id:
            return jsonify({"error": "train_id is required"}), 400

        if train_id not in state.network.trains:
            return jsonify({"error": f"Train '{train_id}' not found"}), 404

        schedule_id = data.get("schedule_id") or f"SCH-{uuid.uuid4().hex[:6].upper()}"
        priority = data.get("priority", 0)

        schedule = TrainSchedule(
            train_id=train_id, schedule_id=schedule_id, priority=priority
        )

        stops_data = data.get("stops", [])
        for stop_data in stops_data:
            station_name = stop_data.get("station_name", "Unknown")
            station_pos = tuple(stop_data.get("station_pos", [0, 0]))

            arrival_str = stop_data.get("arrival_time")
            departure_str = stop_data.get("departure_time")

            try:
                arrival_time = (
                    datetime.fromisoformat(arrival_str)
                    if arrival_str
                    else datetime.now()
                )
                departure_time = (
                    datetime.fromisoformat(departure_str)
                    if departure_str
                    else arrival_time + timedelta(minutes=2)
                )
            except ValueError:
                arrival_time = datetime.now()
                departure_time = arrival_time + timedelta(minutes=2)

            stop = ScheduleStop(
                station_pos=station_pos,
                station_name=station_name,
                arrival_time=arrival_time,
                departure_time=departure_time,
                platform=stop_data.get("platform"),
            )
            schedule.add_stop(stop)

        if schedule.stops:
            schedule.start_time = schedule.stops[0].arrival_time
            schedule.end_time = schedule.stops[-1].departure_time

        state.network.add_schedule(schedule)

        train = state.network.trains[train_id]
        train.schedule = schedule

        return jsonify(
            {
                "status": "success",
                "schedule_id": schedule_id,
                "message": f"Schedule created and assigned to train {train_id}",
            }
        )

    @app.route("/api/schedules/<schedule_id>", methods=["GET"])
    def get_schedule(schedule_id):
        """Get a specific schedule by ID."""
        if schedule_id not in state.network.schedules:
            return jsonify({"error": "Schedule not found"}), 404

        schedule = state.network.schedules[schedule_id]
        stops_data = []
        for stop in schedule.stops:
            stops_data.append(
                {
                    "station_pos": list(stop.station_pos),
                    "station_name": stop.station_name,
                    "arrival_time": stop.arrival_time.isoformat()
                    if stop.arrival_time
                    else None,
                    "departure_time": stop.departure_time.isoformat()
                    if stop.departure_time
                    else None,
                    "platform": stop.platform,
                }
            )

        return jsonify(
            {
                "schedule_id": schedule.schedule_id,
                "train_id": schedule.train_id,
                "stops": stops_data,
                "start_time": schedule.start_time.isoformat()
                if schedule.start_time
                else None,
                "end_time": schedule.end_time.isoformat()
                if schedule.end_time
                else None,
                "priority": schedule.priority,
            }
        )

    @app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
    def delete_schedule(schedule_id):
        """Delete a schedule."""
        if schedule_id not in state.network.schedules:
            return jsonify({"error": "Schedule not found"}), 404

        schedule = state.network.schedules[schedule_id]

        if schedule.train_id in state.network.trains:
            train = state.network.trains[schedule.train_id]
            if train.schedule and train.schedule.schedule_id == schedule_id:
                train.schedule = None

        del state.network.schedules[schedule_id]

        return jsonify(
            {"status": "success", "message": f"Schedule {schedule_id} deleted"}
        )

    # ========================================================================
    # Speed Profiles API
    # ========================================================================

    @app.route("/api/speed-profiles", methods=["GET"])
    def get_speed_profiles():
        """List all speed profiles.

        Returns:
            {
                "profiles": [
                    {
                        "profile_id": str,
                        "name": str,
                        "speed_voltage_curve": [[speed, voltage], ...],
                        "min_start_voltage": float,
                        "lighting_voltage": float,
                        "acceleration_rate": float,
                        "deceleration_rate": float,
                        "emergency_brake_rate": float
                    },
                    ...
                ],
                "count": int
            }
        """
        with state.simulation_lock:
            profile_ids = state.speed_profile_registry.list_profiles()
            profiles = []
            for profile_id in profile_ids:
                profile = state.speed_profile_registry.get(profile_id)
                if profile:
                    profiles.append(profile.to_dict())

        return jsonify({"profiles": profiles, "count": len(profiles)})

    @app.route("/api/speed-profiles", methods=["POST"])
    def create_speed_profile():
        """Create a new speed profile.

        Request body:
        {
            "profile_id": str (required, unique),
            "name": str (required),
            "speed_voltage_curve": [[speed, voltage], ...] (optional),
            "min_start_voltage": float (optional, default 2.0),
            "lighting_voltage": float (optional, default 2.5),
            "acceleration_rate": float (optional, default 10.0),
            "deceleration_rate": float (optional, default 15.0),
            "emergency_brake_rate": float (optional, default 30.0)
        }

        Returns:
            201 Created with profile data
            400 Bad Request if validation fails
            409 Conflict if profile_id already exists
        """
        from speed_profile import SpeedProfile

        data = request.json or {}

        profile_id = data.get("profile_id")
        name = data.get("name")

        if not profile_id:
            return jsonify({"error": "profile_id is required"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400

        with state.simulation_lock:
            # Check for duplicate
            if state.speed_profile_registry.get(profile_id):
                return (
                    jsonify(
                        {
                            "error": f"Speed profile with profile_id '{profile_id}' already exists"
                        }
                    ),
                    409,
                )

            # Create profile from data
            try:
                profile = SpeedProfile.from_dict(data)
                state.speed_profile_registry.register(profile)
            except (ValueError, KeyError, TypeError) as e:
                return jsonify({"error": f"Invalid profile data: {str(e)}"}), 400

        return jsonify(profile.to_dict()), 201

    @app.route("/api/speed-profiles/<profile_id>", methods=["GET"])
    def get_speed_profile(profile_id: str):
        """Get a single speed profile by ID.

        Returns:
            200 OK with profile data
            404 Not Found if profile_id doesn't exist
        """
        with state.simulation_lock:
            profile = state.speed_profile_registry.get(profile_id)

        if not profile:
            return jsonify({"error": f"Speed profile '{profile_id}' not found"}), 404

        return jsonify(profile.to_dict())

    @app.route("/api/speed-profiles/<profile_id>", methods=["PUT"])
    def update_speed_profile(profile_id: str):
        """Update an existing speed profile.

        Request body: Same as POST (all fields optional except profile_id must match)

        Returns:
            200 OK with updated profile data
            400 Bad Request if validation fails
            404 Not Found if profile_id doesn't exist
        """
        from speed_profile import SpeedProfile

        data = request.json or {}

        # Ensure profile_id in body matches URL (if provided)
        if "profile_id" in data and data["profile_id"] != profile_id:
            return (
                jsonify(
                    {
                        "error": "profile_id in body must match URL parameter or be omitted"
                    }
                ),
                400,
            )

        with state.simulation_lock:
            # Check if exists
            existing_profile = state.speed_profile_registry.get(profile_id)
            if not existing_profile:
                return (
                    jsonify({"error": f"Speed profile '{profile_id}' not found"}),
                    404,
                )

            # Merge data with profile_id from URL
            data["profile_id"] = profile_id

            # If name not provided, keep existing
            if "name" not in data:
                data["name"] = existing_profile.name

            # Create updated profile
            try:
                updated_profile = SpeedProfile.from_dict(data)
                state.speed_profile_registry.register(updated_profile)
            except (ValueError, KeyError, TypeError) as e:
                return jsonify({"error": f"Invalid profile data: {str(e)}"}), 400

        return jsonify(updated_profile.to_dict())

    @app.route("/api/speed-profiles/<profile_id>", methods=["DELETE"])
    def delete_speed_profile(profile_id: str):
        """Delete a speed profile.

        Cannot delete if:
        - profile_id is "default"
        - Profile is currently assigned to any train

        Returns:
            204 No Content on success
            404 Not Found if profile_id doesn't exist
            409 Conflict if profile is in use or is default
        """
        with state.simulation_lock:
            # Check if profile exists
            profile = state.speed_profile_registry.get(profile_id)
            if not profile:
                return (
                    jsonify({"error": f"Speed profile '{profile_id}' not found"}),
                    404,
                )

            # Check if it's the default profile
            if profile_id == "default":
                return jsonify({"error": "Cannot delete default speed profile"}), 409

            # Check if any train is using this profile
            trains_using = []
            for train in state.network.trains.values():
                if (
                    hasattr(train, "speed_profile_id")
                    and train.speed_profile_id == profile_id
                ):
                    trains_using.append(train.train_id)

            if trains_using:
                return (
                    jsonify(
                        {
                            "error": f"Cannot delete speed profile in use by trains: {', '.join(trains_using)}"
                        }
                    ),
                    409,
                )

            # Delete profile
            success = state.speed_profile_registry.remove(profile_id)
            if not success:
                return (
                    jsonify(
                        {"error": f"Failed to delete speed profile '{profile_id}'"}
                    ),
                    500,
                )

        return "", 204

    # ========================================================================
    # Scale Configuration API
    # ========================================================================

    @app.route("/api/scale", methods=["GET"])
    def get_scale():
        """Get current model scale configuration.

        Returns:
            JSON with current scale value and available presets
        """
        from model_scale import get_model_scale, SCALE_PRESETS

        return jsonify({"scale": get_model_scale(), "presets": SCALE_PRESETS})

    @app.route("/api/scale", methods=["PUT"])
    def set_scale():
        """Set model scale.

        Request Body:
            {"scale": 160.0}  - Direct scale value
            OR
            {"preset": "1:160"}  - Use a preset

        Returns:
            JSON with new scale value
        """
        from model_scale import get_model_scale, set_model_scale, SCALE_PRESETS

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        with state.simulation_lock:
            try:
                if "preset" in data:
                    preset_name = data["preset"]
                    if preset_name not in SCALE_PRESETS:
                        return jsonify(
                            {
                                "error": f"Unknown preset '{preset_name}'",
                                "available_presets": list(SCALE_PRESETS.keys()),
                            }
                        ), 400
                    set_model_scale(SCALE_PRESETS[preset_name])
                elif "scale" in data:
                    scale = float(data["scale"])
                    set_model_scale(scale)
                else:
                    return (
                        jsonify({"error": "Must provide 'scale' or 'preset'"}),
                        400,
                    )

                return jsonify({"scale": get_model_scale(), "status": "success"})
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

    # ========================================================================
    # Switch Configuration API
    # ========================================================================

    @app.route("/api/switches", methods=["GET"])
    def get_switches():
        """Get all switch configurations."""
        return jsonify([s.to_dict() for s in state.network.switches.values()])

    @app.route("/api/switches/<switch_id>", methods=["GET"])
    def get_switch(switch_id: str):
        """Get specific switch configuration."""
        switch = state.network.get_switch(switch_id)
        if not switch:
            return jsonify({"error": "Switch not found"}), 404
        return jsonify(switch.to_dict())

    @app.route("/api/tracks/<int:x>/<int:y>/switch", methods=["POST"])
    def create_switch_at_track(x: int, y: int):
        """
        Create a switch configuration for a track tile.

        User requirement: Auto-created when switch track is placed.
        This endpoint is called automatically by the frontend.
        """
        try:
            track = state.network.get_track(x, y)
            if not track:
                return jsonify({"error": "No track at position"}), 404
            if not track.is_switch:
                return jsonify({"error": "Track is not a switch"}), 400

            # Check if switch already exists
            existing = state.network.get_switch_at(x, y)
            if existing:
                # Return existing switch (idempotent)
                return jsonify(existing.to_dict()), 200

            # Generate switch ID
            switch_id = f"SW_{x}_{y}"

            from switch import Switch

            data = request.json if request.json else {}

            # User requirement: Default to 0.0 cm for both branches
            switch = Switch(
                switch_id=switch_id,
                x=x,
                y=y,
                main_length_cm=data.get("main_length_cm", 0.0),
                diverge_length_cm=data.get("diverge_length_cm", 0.0),
                main_radius_cm=data.get("main_radius_cm"),
                diverge_radius_cm=data.get("diverge_radius_cm"),
                name=data.get("name", ""),
                switch_type=data.get("switch_type", "standard"),
                notes=data.get("notes", ""),
            )

            state.network.add_switch(switch)

            return jsonify(switch.to_dict()), 201
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>", methods=["PUT"])
    def update_switch(switch_id: str):
        """Update switch configuration."""
        try:
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400

            switch = state.network.update_switch(switch_id, **data)
            if not switch:
                return jsonify({"error": "Switch not found"}), 404

            return jsonify(switch.to_dict())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>", methods=["DELETE"])
    def delete_switch(switch_id: str):
        """Remove switch configuration."""
        try:
            success = state.network.remove_switch(switch_id)
            if not success:
                return jsonify({"error": "Switch not found"}), 404

            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>/state", methods=["PUT"])
    def change_switch_state(switch_id: str):
        """
        Change switch state (MAIN=0, DIVERGE=1).

        Fails with 409 Conflict if switch is locked by a path.
        Use lock field to check if switch is locked before calling.

        Request body:
            {"state": 0 or 1}

        Returns:
            200: {"success": true, "message": "..."}
            404: {"error": "Switch not found"}
            409: {"error": "Switch locked by path ..."}
            400: {"error": "Invalid state value"}
        """
        try:
            data = request.json
            if not data or "state" not in data:
                return jsonify({"error": "Missing 'state' in request body"}), 400

            new_state = data["state"]
            if new_state not in (0, 1):
                return jsonify(
                    {"error": "Invalid state value. Must be 0 (MAIN) or 1 (DIVERGE)"}
                ), 400

            switch = state.network.switches.get(switch_id)
            if not switch:
                return jsonify({"error": f"Switch {switch_id} not found"}), 404

            # Use request_state_change which checks lock status
            success, message = switch.request_state_change(new_state, state.network)

            if success:
                return jsonify(
                    {
                        "success": True,
                        "message": message,
                        "switch": switch.to_dict(),
                    }
                )
            else:
                # Switch is locked - return 409 Conflict
                return jsonify({"error": message}), 409

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>/lock", methods=["POST"])
    def lock_switch(switch_id: str):
        """
        Lock a switch for a path.

        Request body:
            {"path_id": "PATH_123", "state": 0 or 1}

        Returns:
            200: {"success": true, "switch": {...}}
            404: {"error": "Switch not found"}
            409: {"error": "Switch already locked by different path"}
        """
        try:
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400

            path_id = data.get("path_id")
            state_val = data.get("state")

            if not path_id:
                return jsonify({"error": "Missing 'path_id' in request body"}), 400
            if state_val not in (0, 1):
                return jsonify(
                    {"error": "Invalid state value. Must be 0 (MAIN) or 1 (DIVERGE)"}
                ), 400

            switch = state.network.switches.get(switch_id)
            if not switch:
                return jsonify({"error": f"Switch {switch_id} not found"}), 404

            # First change state, then lock
            track = state.network.get_track(switch.x, switch.y)
            if track:
                track.set_switch_state(state_val)

            success = switch.lock(path_id, state_val)

            if success:
                return jsonify({"success": True, "switch": switch.to_dict()})
            else:
                return jsonify(
                    {"error": f"Switch already locked by path {switch.locked_by_path}"}
                ), 409

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/switches/<switch_id>/unlock", methods=["POST"])
    def unlock_switch(switch_id: str):
        """
        Unlock a switch.

        Request body:
            {"path_id": "PATH_123"}

        Returns:
            200: {"success": true, "switch": {...}}
            404: {"error": "Switch not found"}
            403: {"error": "Switch not locked by this path"}
        """
        try:
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400

            path_id = data.get("path_id")
            if not path_id:
                return jsonify({"error": "Missing 'path_id' in request body"}), 400

            switch = state.network.switches.get(switch_id)
            if not switch:
                return jsonify({"error": f"Switch {switch_id} not found"}), 404

            success = switch.unlock(path_id)

            if success:
                return jsonify({"success": True, "switch": switch.to_dict()})
            else:
                if switch.is_locked():
                    return jsonify(
                        {
                            "error": f"Switch locked by different path: {switch.locked_by_path}"
                        }
                    ), 403
                else:
                    return jsonify({"error": "Switch is not locked"}), 400

        except Exception as e:
            return jsonify({"error": str(e)}), 500
