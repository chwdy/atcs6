"""
Train Management Routes - Train CRUD, control, and state management.
"""

from typing import Dict, Optional

from flask import jsonify, request

from app_state import AppState
from enums import Direction, TrainState
from train import Train, TrainCar


def register(app, state: AppState) -> None:
    """Register train routes."""

    def _get_train_controller(train):
        """Get or create TrainController for a train."""
        return state.sim_controller._get_or_create_controller(train)

    def _get_platforms() -> Dict[str, object]:
        """Get physical platforms map."""
        platforms = getattr(state.network, "platforms", None)
        if isinstance(platforms, dict):
            return platforms
        return {}

    def _get_platform_block_id(platform_id: str) -> Optional[str]:
        """Resolve a platform to one representative block ID."""
        platform = _get_platforms().get(platform_id)
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

    def _find_auto_park_platform_id(train: Train) -> Optional[str]:
        """Pick a platform destination for temporary auto-park route."""
        platforms = _get_platforms()
        if not platforms:
            return None

        if not train.position:
            return next(iter(platforms.keys()))

        head_x, head_y = train.position.head_tile
        current_track = state.network.get_track(head_x, head_y)
        current_block_id = current_track.block_section_id if current_track else None

        # Prefer platform on current block.
        for platform_id in platforms.keys():
            if _get_platform_block_id(platform_id) == current_block_id:
                return platform_id

        # Fall back to nearest platform center by Manhattan distance.
        best_id: Optional[str] = None
        best_distance: Optional[float] = None
        for platform_id, platform in platforms.items():
            positions = getattr(platform, "positions", [])
            if not positions:
                continue
            center_x = sum(pos[0] for pos in positions) / len(positions)
            center_y = sum(pos[1] for pos in positions) / len(positions)
            distance = abs(center_x - head_x) + abs(center_y - head_y)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_id = platform_id

        return best_id

    # ============================================================================
    # Train CRUD and Basic Operations
    # ============================================================================

    @app.route("/api/trains", methods=["POST"])
    def create_train():
        """Create a new train."""
        data = request.json
        train_id = data.get("id")
        if not train_id:
            return jsonify({"error": "Missing train ID"}), 400

        # Create a default locomotive car with the specified length
        # API provides length in track units (approx 100m)
        length_units = data.get("length", 1.0)
        length_meters = length_units * 100.0

        car = TrainCar(
            car_id=f"{train_id}_loco",
            car_type="LOCOMOTIVE",
            length=length_meters,
            max_speed=data.get("max_speed", 100.0),
        )

        train = Train(
            train_id=train_id,
            consist=[car],
            max_speed=data.get("max_speed", 100.0),
            car_number=data.get("car_number", ""),
        )

        if "position" in data:
            pos = data["position"]
            direction = Direction[data.get("direction", "EAST")]
            state.network.add_train(train)
            try:
                state.network.place_train(train, pos["x"], pos["y"], direction)
            except ValueError as e:
                state.network.remove_train(train_id)
                return jsonify({"error": str(e)}), 400
        else:
            state.network.add_train(train)

        state.network.update_signals_from_blocks()

        return jsonify({"status": "success", "train": train.to_dict(state.network)})

    @app.route("/api/trains", methods=["GET"])
    def list_trains():
        """List all trains in the network."""
        trains = []
        for train in state.network.trains.values():
            trains.append(train.to_dict(state.network))
        return jsonify({"trains": trains})

    @app.route("/api/trains/<train_id>", methods=["GET"])
    def get_train(train_id):
        """Get details for a single train."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        return jsonify(train.to_dict(state.network))

    @app.route("/api/trains/<train_id>", methods=["PATCH"])
    def update_train(train_id):
        """Update train properties."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}

        errors = []

        if "max_speed" in data:
            try:
                train.max_speed = float(data["max_speed"])
            except (ValueError, TypeError):
                errors.append("max_speed")

        if "car_number" in data:
            train.car_number = str(data["car_number"])

        if "length_cm" in data:
            try:
                new_length = float(data["length_cm"])
                if new_length > 0:
                    if train.consist:
                        train.consist[0].length = new_length
                    else:
                        # Create default car if consist is empty
                        car = TrainCar(
                            car_id=f"{train_id}_loco",
                            car_type="LOCOMOTIVE",
                            length=new_length,
                            max_speed=train.max_speed,
                        )
                        train.consist.append(car)
            except (ValueError, TypeError):
                errors.append("length_cm")

        if errors:
            return jsonify(
                {
                    "status": "error",
                    "message": f"Invalid values for fields: {', '.join(errors)}",
                }
            ), 400

        return jsonify({"status": "success", "train": train.to_dict(state.network)})

    @app.route("/api/trains/<train_id>", methods=["DELETE"])
    def delete_train(train_id):
        """Delete a train from the network."""
        if not state.network.remove_train(train_id):
            return jsonify({"error": "Train not found"}), 404

        return jsonify({"status": "success", "message": f"Train {train_id} deleted"})

    # ============================================================================
    # Train Power and Direction Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/power-on", methods=["POST"])
    def train_power_on(train_id):
        """Power on a train (OFF -> IDLE)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.power_on_cmd()
        if result.success:
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/power-off", methods=["POST"])
    def train_power_off(train_id):
        """Power off a train (any state -> OFF). Must be stopped."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.power_off_cmd()
        if result.success:
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/set-direction", methods=["POST"])
    def train_set_direction(train_id):
        """Set direction and transition to STANDBY (IDLE -> STANDBY)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        direction_str = data.get("direction")
        if not direction_str:
            return jsonify({"error": "Missing direction"}), 400

        try:
            direction = Direction[direction_str]
        except KeyError:
            return jsonify({"error": f"Invalid direction: {direction_str}"}), 400

        controller = _get_train_controller(train)
        if controller.set_direction_and_standby(direction):
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify({"error": "Cannot set direction (train not in IDLE state)"}), 400

    @app.route("/api/trains/<train_id>/clear-direction", methods=["POST"])
    def train_clear_direction(train_id):
        """Clear direction and transition back to IDLE (STANDBY -> IDLE)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        if controller.clear_direction():
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify(
            {"error": "Cannot clear direction (train not in STANDBY state)"}
        ), 400

    # ============================================================================
    # Train Cab and Movement Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/set-cab", methods=["POST"])
    def train_set_cab(train_id):
        """Set active cab (A, B, or None/off) and manage state transitions."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        cab = data.get("cab")

        # Validate cab value
        if cab not in ("A", "B", None, "off", "Off", "OFF"):
            return jsonify(
                {"error": f"Invalid cab value: {cab}. Must be 'A', 'B', or None"}
            ), 400

        # Normalize "off" strings to None
        if cab in ("off", "Off", "OFF"):
            cab = None

        controller = _get_train_controller(train)
        result = controller.set_cab(cab)
        if result.success:
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/arrive", methods=["POST"])
    def train_arrive(train_id):
        """Arrive at station (RUNNING_MOVING -> RUNNING_AT_STATION)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        if controller.arrive_at_station():
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify(
            {"error": "Cannot arrive (train not in RUNNING_MOVING state)"}
        ), 400

    @app.route("/api/trains/<train_id>/depart", methods=["POST"])
    def train_depart(train_id):
        """Depart from station."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)

        if train.state == TrainState.RUNNING_WAITING:
            if controller.clear_to_move():
                return jsonify(
                    {"status": "success", "train": train.to_dict(state.network)}
                )

        if train.state == TrainState.RUNNING_AT_STATION:
            if controller.depart_from_station(has_more_waypoints=True):
                return jsonify(
                    {"status": "success", "train": train.to_dict(state.network)}
                )

        if controller.depart():
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify({"error": "Cannot depart (train not in STANDBY state)"}), 400

    @app.route("/api/trains/<train_id>/turn-around", methods=["POST"])
    def train_turn_around(train_id):
        """Flip facing direction (cab switch). Only allowed when stopped."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        if controller.turn_around():
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify(
            {"error": "Cannot turn around (train is moving or no direction set)"}
        ), 400

    # ============================================================================
    # Manual Control Mode
    # ============================================================================

    @app.route("/api/trains/<train_id>/enable-manual", methods=["POST"])
    def train_enable_manual(train_id):
        """Enable manual control mode. Train must be stopped."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        if controller.enable_manual():
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify(
            {"error": "Cannot enable manual mode (train must be stopped and not OFF)"}
        ), 400

    @app.route("/api/trains/<train_id>/disable-manual", methods=["POST"])
    def train_disable_manual(train_id):
        """Disable manual control mode. Train must be stopped."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        if controller.disable_manual():
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify(
            {
                "error": "Cannot disable manual mode (train must be stopped and in MANUAL state)"
            }
        ), 400

    @app.route("/api/trains/<train_id>/manual-speed", methods=["POST"])
    def train_set_manual_speed(train_id):
        """Set manual target speed (only in MANUAL mode)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        speed = data.get("speed", 0)
        if not isinstance(speed, (int, float)):
            return jsonify({"error": "Invalid speed value"}), 400

        if train.set_manual_speed(float(speed)):
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify(
            {"error": "Cannot set manual speed (train not in MANUAL mode)"}
        ), 400

    @app.route("/api/trains/<train_id>/travel-direction", methods=["POST"])
    def train_set_travel_direction(train_id):
        """Set travel direction for MANUAL mode ('forward' or 'reverse')."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        direction = data.get("direction")
        if not direction or direction not in ("forward", "reverse"):
            return jsonify(
                {
                    "error": "Missing or invalid direction (must be 'forward' or 'reverse')"
                }
            ), 400

        if train.set_travel_direction(direction):
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify(
            {"error": "Cannot set travel direction (train not in MANUAL mode)"}
        ), 400

    # ============================================================================
    # Emergency and Stop Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/emergency-stop", methods=["POST"])
    def train_emergency_stop(train_id):
        """Emergency stop from any operational state."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.emergency_stop_cmd()
        if result.success:
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/stop", methods=["POST"])
    def train_stop(train_id):
        """Controlled stop - bring train to standby state (ATO stop)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.ato_stop()
        if result.success:
            return jsonify({"status": "success", "train": train.to_dict(state.network)})
        return jsonify({"error": result.message}), 400

    # ============================================================================
    # TrainController State and Monitoring
    # ============================================================================

    @app.route("/api/trains/<train_id>/onboard", methods=["GET"])
    def get_onboard_computer(train_id):
        """Get TrainController state for a train."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        return jsonify(controller.to_dict())

    @app.route("/api/trains/<train_id>/lookahead", methods=["GET"])
    def get_train_monitor_lookahead(train_id):
        """Get Train Monitor lookahead data from TrainController.

        Acquires simulation_lock to prevent reading partially-updated
        controller state mid-tick (e.g. current_path set but path_index
        stale), which would cause the monitor to fall back to a truncated
        single-block lookahead.
        """
        with state.simulation_lock:
            train = state.network.trains.get(train_id)
            if not train:
                return jsonify({"error": "Train not found"}), 404

            controller = _get_train_controller(train)
            return jsonify(controller.get_monitor_lookahead())

    @app.route("/api/trains/<train_id>/notifications", methods=["GET"])
    def get_train_notifications(train_id):
        """Get and clear pending notifications for a train (for toast messages)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        notifications = controller.get_and_clear_notifications()
        return jsonify({"notifications": notifications})

    # ============================================================================
    # Train Handle and Control Mode
    # ============================================================================

    @app.route("/api/trains/<train_id>/handle", methods=["POST"])
    def set_handle_position(train_id):
        """Set throttle/brake handle position."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        position_str = data.get("position")
        if not position_str:
            return jsonify({"error": "Missing position"}), 400

        from enums import HandlePosition

        try:
            position = HandlePosition(position_str)
        except ValueError:
            return jsonify(
                {
                    "error": f"Invalid handle position: {position_str}. Valid: P1-P5, N, B1-B8, EB"
                }
            ), 400

        controller = _get_train_controller(train)
        result = controller.set_handle(position)
        if result.success:
            return jsonify({"status": "success", "onboard": controller.to_dict()})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/mode", methods=["POST"])
    def set_control_mode(train_id):
        """Set control mode (ATO or Manual)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        mode = data.get("mode")
        if mode not in ("ato", "manual"):
            return jsonify(
                {"error": f"Invalid mode: {mode}. Must be 'ato' or 'manual'"}
            ), 400

        controller = _get_train_controller(train)
        result = controller.set_mode(mode)
        if result.success:
            return jsonify({"status": "success", "onboard": controller.to_dict()})
        return jsonify({"error": result.message}), 400

    # ============================================================================
    # ATO Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/ato-go", methods=["POST"])
    def ato_go(train_id):
        """Start ATO movement toward destination."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.ato_go()
        if result.success:
            return jsonify({"status": "success", "onboard": controller.to_dict()})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/ato-stop", methods=["POST"])
    def ato_stop(train_id):
        """Stop ATO movement (service stop)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.ato_stop()
        if result.success:
            return jsonify({"status": "success", "onboard": controller.to_dict()})
        return jsonify({"error": result.message}), 400

    # ============================================================================
    # Train Position and Properties
    # ============================================================================

    @app.route("/api/trains/<train_id>/position", methods=["PUT"])
    def update_train_position(train_id: str):
        """Update train position.

        Request body:
        {
            "position": {"x": int, "y": int} (required),
            "direction": "NORTH"|"EAST"|"SOUTH"|"WEST" (required)
        }

        Returns:
            200 OK with updated train data
            400 Bad Request if validation fails
            404 Not Found if train doesn't exist
        """
        data = request.json or {}

        with state.simulation_lock:
            train = state.network.trains.get(train_id)
            if not train:
                return jsonify({"error": f"Train '{train_id}' not found"}), 404

            position = data.get("position")
            if not isinstance(position, dict):
                return jsonify({"error": "position is required"}), 400

            if "x" not in position or "y" not in position:
                return jsonify({"error": "position must include x and y"}), 400

            if not data.get("direction"):
                return jsonify({"error": "direction is required"}), 400

            try:
                direction = Direction[data["direction"]]
            except KeyError:
                return jsonify(
                    {"error": f"Invalid direction: {data['direction']}"}
                ), 400

            x, y = int(position["x"]), int(position["y"])

            track = state.network.tracks.get((x, y))
            if not track:
                return jsonify({"error": f"No track at position ({x}, {y})"}), 400

            if train.position and state.network.block_manager:
                for block_id in train.position.occupied_blocks:
                    state.network.block_manager.clear_block(block_id)

            try:
                state.network.place_train(train, x, y, direction)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

        return jsonify(train.to_dict(state.network))

    @app.route("/api/trains/<train_id>/speed-profile", methods=["PATCH"])
    def assign_train_speed_profile(train_id: str):
        """Assign speed profile to a train.

        Request body:
        {
            "profile_id": str (required)
        }

        Returns:
            200 OK with updated train data
            400 Bad Request if validation fails
            404 Not Found if train or profile doesn't exist
        """
        data = request.json or {}

        profile_id = data.get("profile_id")
        if not profile_id:
            return jsonify({"error": "profile_id is required"}), 400

        with state.simulation_lock:
            train = state.network.trains.get(train_id)
            if not train:
                return jsonify({"error": f"Train '{train_id}' not found"}), 404

            profile = state.speed_profile_registry.get(profile_id)
            if not profile:
                return jsonify(
                    {"error": f"Speed profile '{profile_id}' not found"}
                ), 404

            train.speed_profile_id = profile_id

        return jsonify(train.to_dict(state.network))

    # ============================================================================
    # Train Routes and Assignment
    # ============================================================================

    @app.route("/api/trains/<train_id>/assign-route", methods=["PUT"])
    def assign_route_to_train(train_id):
        """Assign a route to a train."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.get_json() or {}
        route_id = data.get("route_id")
        force = bool(data.get("force", False))
        auto_park = bool(data.get("auto_park", True))
        next_waypoint_index = data.get("next_waypoint_index")

        current_route_id = train.assigned_route_id

        if route_id is None:
            if current_route_id:
                current_route = state.network.routes.get(current_route_id)
                if current_route:
                    current_route.unassign_train(train_id)
            train.assigned_route_id = None

            # Stop ATO and clear destination when canceling route
            controller = _get_train_controller(train)
            if controller.ato_running:
                controller.ato_stop()
            controller.ato_destination_block_id = None
            controller.ato_path_id = None
            train.ato_destination_block_id = None

            route_executor = (
                state.sim_controller.route_executor if state.sim_controller else None
            )
            if route_executor:
                route_executor.execution_states.pop(train_id, None)

            if auto_park and current_route_id:
                target_platform_id = _find_auto_park_platform_id(train)
                if target_platform_id:
                    from train_route import Route, RouteMode, Waypoint

                    simulation_time = (
                        state.sim_controller.simulation_time
                        if state.sim_controller
                        else 0.0
                    )
                    temp_route_id = f"TEMP_{train_id}_{int(simulation_time)}"
                    while temp_route_id in state.network.routes:
                        temp_route_id = f"{temp_route_id}_X"

                    temp_route = Route(
                        route_id=temp_route_id,
                        name=f"AutoPark {train_id}",
                        mode=RouteMode.ONE_OFF,
                    )
                    temp_route.add_waypoint(
                        Waypoint(
                            platform_id=target_platform_id,
                            dwell_time_sec=0.0,
                            flexible_platform=False,
                        )
                    )
                    temp_route.assign_train(train_id)
                    temp_route.activate(simulation_time)
                    state.network.routes[temp_route_id] = temp_route
                    train.assigned_route_id = temp_route_id

                    if route_executor:
                        from route_executor import RouteExecutionState

                        route_executor.execution_states[train_id] = RouteExecutionState(
                            train_id=train_id,
                            route_id=temp_route_id,
                            route_start_time=simulation_time,
                        )

                    return jsonify(
                        {
                            "status": "success",
                            "train": train.to_dict(state.network),
                            "temporary_route_id": temp_route_id,
                            "target_platform_id": target_platform_id,
                        }
                    )

            return jsonify({"status": "success", "train": train.to_dict(state.network)})

        route = state.network.routes.get(route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        if current_route_id and current_route_id != route_id and not force:
            return (
                jsonify(
                    {
                        "error": f"Train already assigned to route {current_route_id}. Use force=true to reassign."
                    }
                ),
                409,
            )

        if current_route_id and current_route_id != route_id:
            previous_route = state.network.routes.get(current_route_id)
            if previous_route:
                previous_route.unassign_train(train_id)

        route.assign_train(train_id)
        train.assigned_route_id = route_id

        route_executor = (
            state.sim_controller.route_executor if state.sim_controller else None
        )
        if route_executor:
            exec_state = route_executor.execution_states.get(train_id)
            if not exec_state or exec_state.route_id != route_id:
                from route_executor import RouteExecutionState

                exec_state = RouteExecutionState(
                    train_id=train_id,
                    route_id=route_id,
                    route_start_time=state.sim_controller.simulation_time,
                )
                route_executor.execution_states[train_id] = exec_state

            if next_waypoint_index is not None:
                try:
                    idx = int(next_waypoint_index)
                except (TypeError, ValueError):
                    return jsonify(
                        {"error": "next_waypoint_index must be an integer"}
                    ), 400

                if idx < 0 or idx >= route.get_waypoint_count():
                    return (
                        jsonify({"error": "next_waypoint_index out of range"}),
                        400,
                    )

                exec_state.current_waypoint_index = idx
                exec_state.arrived_at_platform_time = None
                exec_state.waiting_for_path = False
                exec_state.resolved_platform_id = None

        return jsonify({"status": "success", "train": train.to_dict(state.network)})

    @app.route("/api/trains/<train_id>/set-waypoint", methods=["POST"])
    def set_train_waypoint(train_id: str):
        """Override the current waypoint index for an assigned route."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        if not train.assigned_route_id:
            return jsonify({"error": "Train has no assigned route"}), 400

        route = state.network.routes.get(train.assigned_route_id)
        if not route:
            return jsonify({"error": "Assigned route not found"}), 404

        data = request.get_json() or {}
        if "waypoint_index" not in data:
            return jsonify({"error": "waypoint_index is required"}), 400

        try:
            waypoint_index = int(data["waypoint_index"])
        except (TypeError, ValueError):
            return jsonify({"error": "waypoint_index must be an integer"}), 400

        if waypoint_index < 0 or waypoint_index >= route.get_waypoint_count():
            return jsonify({"error": "waypoint_index out of range"}), 400

        route_executor = (
            state.sim_controller.route_executor if state.sim_controller else None
        )
        if not route_executor:
            return jsonify({"error": "Route executor unavailable"}), 500

        if not route_executor.set_train_waypoint_index(train_id, waypoint_index):
            return jsonify({"error": "Failed to set waypoint"}), 400

        return jsonify(
            {
                "status": "success",
                "train": train.to_dict(state.network),
                "waypoint_index": waypoint_index,
            }
        )
