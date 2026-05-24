"""
Train Management Routes - Train CRUD, control, and state management.
"""

import time
from typing import Dict

from flask import jsonify, request

from app_state import AppState
from enums import Direction
from train import Train, TrainCar


def register(app, state: AppState) -> None:
    """Register train routes."""

    def _get_train_controller(train):
        """Get or create TrainController for a train."""
        return state.sim_controller._get_or_create_controller(train)

    def _train_to_dict(train, network=None):
        """Convert train to the canonical sibling-payload shape.

        Per `design_new.md` "Data Flow To UI", live runtime state belongs to
        the TrainController and PathDispatcher rather than to Train itself.
        This helper returns three sibling sub-payloads:

          - ``train``: persistent/configuration state from `Train.to_dict()`
          - ``controller``: live runtime state from `TrainController.to_dict()`
            (span, lookahead, mode/handle/eb/ato, current_speed, is_stopped...)
          - ``dispatcher_progress``: dispatcher-owned path projection from
            `PathDispatcher.get_active_path_progress(train_id)` (or None
            when no active path exists)

        Args:
            train: Train instance to serialize.
            network: RailwayNetwork (currently unused; reserved).

        Returns:
            Sibling-payload dict.
        """
        controller_dict = None
        dispatcher_dict = None

        if state.sim_controller:
            try:
                controller = _get_train_controller(train)
                if controller is not None:
                    controller_dict = controller.to_dict()
            except Exception as e:
                import logging

                logging.getLogger(__name__).debug(
                    "Failed to serialize controller for train %s: %s",
                    train.train_id,
                    e,
                )

            dispatcher = getattr(state.sim_controller, "path_dispatcher", None)
            if dispatcher is not None:
                try:
                    dispatcher_dict = dispatcher.get_active_path_progress(
                        train.train_id
                    )
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).debug(
                        "Failed to read dispatcher progress for train %s: %s",
                        train.train_id,
                        e,
                    )

        return {
            "train": train.to_dict(network=network),
            "controller": controller_dict,
            "dispatcher_progress": dispatcher_dict,
        }

    # ============================================================================
    # Train CRUD and Basic Operations
    # ============================================================================

    @app.route("/api/trains", methods=["POST"])
    def create_train():
        """Create a new train.

        When a starting position is provided and a SimulationController is
        available, the request is routed through
        ``sim_controller.add_train(...)`` so that the controller, physics,
        and notification center are wired in one synchronous step. Otherwise
        the train is created without placement (editor scenario).
        """
        data = request.json
        train_id = data.get("id")
        if not train_id:
            return jsonify({"error": "Missing train ID"}), 400

        # API provides length in track units (approx 100m)
        length_units = data.get("length", 1.0)
        length_meters = length_units * 100.0
        max_speed_kmh = float(data.get("max_speed", 100.0))
        car_number = data.get("car_number", "")

        # Position-aware path: delegate to SimulationController.add_train.
        if "position" in data and state.sim_controller is not None:
            pos = data["position"]
            direction_name = data.get("direction", "EAST")
            try:
                pos_x = int(pos["x"])
                pos_y = int(pos["y"])
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "position must include x and y"}), 400
            track = state.network.get_track(pos_x, pos_y)
            if track is None or not track.block_section_id:
                return jsonify(
                    {"error": f"No block at position ({pos_x}, {pos_y})"}
                ), 400
            try:
                train = state.sim_controller.add_train(
                    train_id=train_id,
                    starting_block_id=track.block_section_id,
                    initial_direction=direction_name,
                    max_speed_kmh=max_speed_kmh,
                    length_cm=length_meters,
                    name=car_number,
                )
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            # The block returned by add_train may not be the requested tile
            # (block has multiple tiles). If the caller requested a specific
            # (x, y), re-place there for backwards compatibility.
            try:
                if state.network.block_manager and train.position:
                    for block_id in train.position.occupied_blocks:
                        state.network.block_manager.clear_block(block_id)
                state.network.place_train(
                    train, pos_x, pos_y, Direction[direction_name.upper()]
                )
            except (KeyError, ValueError) as e:
                # Tile-precise placement failed; the train is still on a
                # tile of the requested block, which is acceptable.
                import logging
                logging.getLogger(__name__).debug(
                    "Tile-precise placement failed for %s: %s",
                    train_id,
                    e,
                )
            state.network.update_signals_from_blocks()
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )

        # No-position / editor path: keep direct construction; this mirrors
        # the previous behaviour where a train can exist without placement.
        car = TrainCar(
            car_id=f"{train_id}_loco",
            car_type="LOCOMOTIVE",
            length_cm=length_meters,
            max_speed=max_speed_kmh,
        )
        train = Train(
            train_id=train_id,
            consist=[car],
            max_speed=max_speed_kmh,
            car_number=car_number,
        )
        state.network.add_train(train)
        state.network.update_signals_from_blocks()
        return jsonify(
            {"status": "success", **_train_to_dict(train, state.network)}
        )

    @app.route("/api/trains", methods=["GET"])
    def list_trains():
        """List all trains in the canonical sibling-payload shape.

        Per design_new.md "Data Flow To UI": expose `trains`,
        `controllers_by_train_id`, `dispatcher_progress_by_train_id`, and
        `assigned_routes_by_train_id` as sibling collections.
        """
        trains_list = []
        controllers_by_train_id: Dict[str, object] = {}
        dispatcher_by_train_id: Dict[str, object] = {}
        assigned_routes_by_train_id: Dict[str, object] = {}
        route_executor = (
            state.sim_controller.route_executor if state.sim_controller else None
        )
        for train in state.network.trains.values():
            payload = _train_to_dict(train, state.network)
            trains_list.append(payload["train"])
            controllers_by_train_id[train.train_id] = payload["controller"]
            dispatcher_by_train_id[train.train_id] = payload["dispatcher_progress"]
            assigned_routes_by_train_id[train.train_id] = (
                route_executor.assigned_route_id_for(train.train_id)
                if route_executor
                else None
            )
        return jsonify(
            {
                "trains": trains_list,
                "controllers_by_train_id": controllers_by_train_id,
                "dispatcher_progress_by_train_id": dispatcher_by_train_id,
                "assigned_routes_by_train_id": assigned_routes_by_train_id,
            }
        )

    @app.route("/api/trains/<train_id>", methods=["GET"])
    def get_train(train_id):
        """Get details for a single train."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        return jsonify(_train_to_dict(train, state.network))

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
                            length_cm=new_length,
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

        return jsonify(
            {"status": "success", **_train_to_dict(train, state.network)}
        )

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
        """Power on a train (OFF -> READY)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.commands.power_on_cmd()
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/power-off", methods=["POST"])
    def train_power_off(train_id):
        """Power off a train (any state -> OFF). Must be stopped."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.commands.power_off_cmd()
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
        return jsonify({"error": result.message}), 400

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
        result = controller.commands.set_cab(cab)
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/depart", methods=["POST"])
    def train_depart(train_id):
        """Depart-acceptance command for the controller.

        Per design_new.md, dwell exit is route-driven via
        ``RouteExecutor.tick_train_phase1(...) -> finish_dwell()``; the API
        path only invokes the canonical ``TrainController.depart()`` for the
        ``READY`` -> ``RUNNING_MOVING`` transition.
        """
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        if controller.depart():
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
        return jsonify({"error": "Cannot depart"}), 400

    # ============================================================================
    # Emergency and Stop Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/stop", methods=["POST"])
    def train_stop(train_id):
        """Controlled stop - bring train to standby state (ATO stop)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.ato.stop()
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
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
            payload = controller.get_lookahead()

            route_executor = state.sim_controller.route_executor
            if route_executor:
                route_id = route_executor.assigned_route_id_for(train_id)
                if route_id:
                    route = route_executor.route_registry.get(route_id)
                    exec_state = route_executor.execution_states.get(train_id)
                    if route and exec_state and exec_state.route_id == route_id:
                        next_idx = exec_state.next_planning_index
                        task = route.get_task_at(next_idx)
                        if (
                            task
                            and task.task_kind == "dwell"
                            and next_idx < len(exec_state.task_runs)
                        ):
                            # `target_time` and `dwell_started_at` are stored
                            # as wall-clock Unix seconds (RouteExecutor._now()
                            # uses time.time()), so the remaining duration
                            # must be computed in wall-clock units too.
                            # The finish time is then shifted onto the
                            # sim-clock the HMI already shows.
                            wall_now = time.time()
                            sim_now = state.sim_controller.simulation_time
                            dwell_started_at = exec_state.task_started_at(next_idx)
                            remaining = task.get_remaining_dwell_time(
                                wall_now, dwell_started_at
                            )
                            finish_sim_sec = sim_now + remaining
                            payload["current_task_kind"] = "dwell"
                            payload["dwell_remaining_sec"] = remaining
                            payload["dwell_finish_sim_sec"] = finish_sim_sec

            return jsonify(payload)

    @app.route("/api/trains/<train_id>/notifications", methods=["GET"])
    def get_train_notifications(train_id):
        """Get and clear pending notifications for a train (for toast messages)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        notifications = controller.notifications.get_and_clear()
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
        result = controller.commands.set_handle(position)
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
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
        result = controller.commands.set_mode(mode)
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/manual-mode-type", methods=["POST"])
    def set_manual_mode_type(train_id):
        """Set manual-mode submode ('simulation' = handle, or 'power_percent')."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        mode_type = data.get("mode_type")
        if mode_type not in ("simulation", "power_percent"):
            return jsonify(
                {"error": f"Invalid mode_type: {mode_type}. Must be 'simulation' or 'power_percent'"}
            ), 400

        controller = _get_train_controller(train)
        result = controller.commands.set_manual_mode_type(mode_type)
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/manual-power-percent", methods=["POST"])
    def set_manual_power_percent(train_id):
        """Set manual power-percent command (0..100) when manual_mode_type == power_percent."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        if "percent" not in data:
            return jsonify({"error": "Missing 'percent' field"}), 400

        controller = _get_train_controller(train)
        result = controller.commands.set_manual_power_percent(data.get("percent"))
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
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
        result = controller.ato.go()
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/ato-stop", methods=["POST"])
    def ato_stop(train_id):
        """Stop ATO movement (service stop)."""
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        controller = _get_train_controller(train)
        result = controller.ato.stop()
        if result.success:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )
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

        return jsonify(_train_to_dict(train, state.network))

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

        return jsonify(_train_to_dict(train, state.network))

    # ============================================================================
    # Train Routes and Assignment
    # ============================================================================

    @app.route("/api/trains/<train_id>/assign-route", methods=["PUT"])
    def assign_route_to_train(train_id):
        """Assign a route to a train.

        Per `design_new.md` line 272 (Operator retargeting / route swap), the
        handler atomically:
          (a) writes `controller.route_hooks.phase_latches.set_route_abort(...)` so Phase 3
              of the next eligible per-train transaction tears down the live
              authority and the existing `RouteRun`, and
          (b) calls `Route.unassign_train(old)` / `Route.assign_train(new)`.

        It does NOT mutate `RouteExecutor.execution_states` directly and does
        NOT call `PathDispatcher.cancel_path(...)` or `controller.ato.stop()`
        inline — those flow through Phase 3 cleanup. Phase 1 of the next
        eligible tick allocates a fresh `RouteRun` against the newly assigned
        route (per `design_new.md` line 1068).
        """
        train = state.network.trains.get(train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.get_json() or {}
        route_id = data.get("route_id")
        force = bool(data.get("force", False))

        route_executor = (
            state.sim_controller.route_executor if state.sim_controller else None
        )
        route_registry = route_executor.route_registry if route_executor else {}
        current_route_id = (
            route_executor.assigned_route_id_for(train_id)
            if route_executor
            else None
        )

        controller = (
            _get_train_controller(train) if state.sim_controller else None
        )

        def _has_active_path() -> bool:
            return (
                controller is not None
                and controller.path_projection.received_path_plan is not None
            )

        # ── Branch 1: unassign (route_id is None) ────────────────────────────
        if route_id is None:
            if current_route_id:
                if controller is not None:
                    controller.route_hooks.phase_latches.set_route_abort(
                        reason="unassign",
                        has_active_path=_has_active_path(),
                    )
                current_route = route_registry.get(current_route_id)
                if current_route:
                    current_route.unassign_train(train_id)

            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )

        # ── Branch 2: assign / retarget (route_id provided) ──────────────────
        route = route_registry.get(route_id)
        if not route:
            return jsonify({"error": "Route not found"}), 404

        # Same-route assign: idempotent no-op success.
        if current_route_id == route_id:
            return jsonify(
                {"status": "success", **_train_to_dict(train, state.network)}
            )

        if current_route_id and not force:
            return (
                jsonify(
                    {
                        "error": f"Train already assigned to route {current_route_id}. Use force=true to reassign."
                    }
                ),
                409,
            )

        if current_route_id:
            # Retargeting: write the abort latch first so Phase 3 commits the
            # cancellation through the canonical workflow.
            if controller is not None:
                controller.route_hooks.phase_latches.set_route_abort(
                    reason="retarget",
                    has_active_path=_has_active_path(),
                )
            previous_route = route_registry.get(current_route_id)
            if previous_route:
                previous_route.unassign_train(train_id)

        route.assign_train(train_id)

        return jsonify(
            {"status": "success", **_train_to_dict(train, state.network)}
        )

