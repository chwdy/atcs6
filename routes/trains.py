"""
Train Management Routes - Train CRUD, control, and state management.

Thin HTTP layer over `services.train_service`: parse JSON, call the
service, translate `ServiceError` subclasses to status codes
(ValidationError -> 400, NotFoundError -> 404, ConflictError -> 409), and
shape the sibling-payload response. Operator commands return the
controller's `CommandResult`; a non-success result maps to 400.
"""

import time

from flask import jsonify, request

from app_state import AppState
from services import ConflictError, NotFoundError, ValidationError
from services import train_service
from simulation import build_train_runtime_maps, train_runtime_entry


def register(app, state: AppState) -> None:
    """Register train routes."""

    def _get_train_controller(train):
        """Get or create TrainController for a train."""
        return state.sim_controller._get_or_create_controller(train)

    def _train_to_dict(train, network=None):
        """Convert train to the canonical sibling-payload shape.

        Per `design_new.md` "Data Flow To UI", live runtime state belongs to
        the TrainController and PathDispatcher rather than to Train itself.
        This helper returns four sibling sub-payloads, built by the shared
        `simulation.train_runtime_entry(...)`:

          - ``train``: persistent/configuration state from `Train.to_dict()`
          - ``controller``: live runtime state from `TrainController.to_dict()`
            (span, lookahead, mode/handle/eb/ato, current_speed, is_stopped...)
          - ``dispatcher_progress``: dispatcher-owned path projection from
            `PathDispatcher.get_active_path_progress(train_id)` (or None
            when no active path exists)
          - ``operator_status``: `{ato_status, pending_actions, hold_reason}`,
            so a command reply, refused or not, carries the train's hold reason

        Args:
            train: Train instance to serialize.
            network: RailwayNetwork (currently unused; reserved).

        Returns:
            Sibling-payload dict.
        """
        if not state.sim_controller:
            return {
                "train": train.to_dict(network=network),
                "controller": None,
                "dispatcher_progress": None,
                "operator_status": None,
            }
        entry = train_runtime_entry(
            state.sim_controller,
            train,
            session_state=state.session_state(),
        )
        return {
            "train": train.to_dict(network=network),
            "controller": entry["controller"],
            "dispatcher_progress": entry["dispatcher_progress"],
            "operator_status": entry["operator_status"],
        }

    # ============================================================================
    # Train CRUD and Basic Operations
    # ============================================================================

    @app.route("/api/trains", methods=["POST"])
    def create_train():
        """Create a new train (placement-aware when a position is given)."""
        data = request.json
        # API provides length in track units (approx 100m).
        length_cm = data.get("length", 1.0) * 100.0
        max_speed_kmh = float(data.get("max_speed", 100.0))

        try:
            train = train_service.create_train(
                state,
                train_id=data.get("id"),
                length_cm=length_cm,
                max_speed_kmh=max_speed_kmh,
                car_number=data.get("car_number", ""),
                position=data.get("position"),
                direction=data.get("direction", "EAST"),
                speed_profile_id=data.get("speed_profile_id"),
                min_radius_cm=data.get("min_radius_cm"),
                max_slope=data.get("max_slope"),
                roster_entry_id=data.get("roster_entry_id"),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({"status": "success", **_train_to_dict(train, state.network)})

    @app.route("/api/trains", methods=["GET"])
    def list_trains():
        """List all trains in the canonical sibling-payload shape.

        Per design_new.md "Data Flow To UI": expose `trains`,
        `controllers_by_train_id`, `dispatcher_progress_by_train_id`,
        `assigned_routes_by_train_id`, `route_runs_by_train_id`, and
        `operator_status_by_train_id` as sibling collections, from the same
        builder `/api/network` uses.
        """
        # Read under the lock, between ticks, like `/api/network`: a layout
        # load replaces the trains under the same lock.
        with state.simulation_lock:
            maps = build_train_runtime_maps(
                state.network,
                state.sim_controller,
                session_state=state.session_state(),
            )
            trains_list = [
                train.to_dict(network=state.network)
                for train in train_service.list_trains(state)
            ]
        return jsonify({"trains": trains_list, **maps})

    @app.route("/api/trains/<train_id>", methods=["GET"])
    def get_train(train_id):
        """Get details for a single train."""
        with state.simulation_lock:
            train = train_service.get_train(state, train_id)
            if not train:
                return jsonify({"error": "Train not found"}), 404
            payload = _train_to_dict(train, state.network)
        return jsonify(payload)

    @app.route("/api/trains/<train_id>", methods=["PATCH"])
    def update_train(train_id):
        """Update train properties."""
        data = request.json or {}
        try:
            train = train_service.update_train(
                state,
                train_id,
                max_speed=data.get("max_speed"),
                car_number=data.get("car_number"),
                length_cm=data.get("length_cm"),
            )
        except NotFoundError:
            return jsonify({"error": "Train not found"}), 404
        except ValidationError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        return jsonify({"status": "success", **_train_to_dict(train, state.network)})

    @app.route("/api/trains/<train_id>", methods=["DELETE"])
    def delete_train(train_id):
        """Delete a train (design_new.md "Train Entity", Deleting a train).

        A refused deletion — an unidentified train, a train that is not OFF, one
        still holding a path or a running route run, or one still drawing
        current — returns 400 with the reason and changes nothing.
        """
        try:
            train_service.delete_train(state, train_id)
        except NotFoundError:
            return jsonify({"error": "Train not found"}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success", "message": f"Train {train_id} deleted"})

    @app.route("/api/trains/<train_id>/adopt", methods=["POST"])
    def adopt_unidentified_train(train_id):
        """Set an unidentified train up as a real, driveable train.

        The unidentified train keeps its blocks and its controller; the body supplies the
        identity, consist and facing the detector could never know
        (design_new.md "Unidentified trains", Adoption). The adopted train's new id is
        in the response, since it is usually not the one in the URL.
        """
        data = request.json or {}
        try:
            train = train_service.adopt_unidentified_train(
                state,
                train_id,
                new_train_id=data.get("new_train_id") or data.get("id"),
                length_cm=data.get("length_cm", 100.0),
                max_speed_kmh=data.get("max_speed", 100.0),
                direction=data.get("direction", "EAST"),
                head_tile=data.get("head_tile"),
                cab=data.get("cab"),
                car_number=data.get("car_number", ""),
                train_type=data.get("train_type"),
                min_radius_cm=data.get("min_radius_cm"),
                max_slope=data.get("max_slope"),
                speed_profile_id=data.get("speed_profile_id"),
                roster_entry_id=data.get("roster_entry_id"),
            )
        except NotFoundError:
            return jsonify({"error": "Train not found"}), 404
        except ValidationError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        return jsonify(
            {
                "status": "success",
                "adopted_from_unidentified_id": train_id,
                **_train_to_dict(train, state.network),
            }
        )

    # ============================================================================
    # Train Power and Direction Control
    # ============================================================================

    def _command_response(train, result):
        """Reply to an operator command (design_new.md "Operator-command
        processing slot", A command's result is its return value): the reply
        carries the command's own `{success, reason}` and the train's status
        payload, on acceptance and on refusal alike, so the panel updates
        without waiting for the next refresh."""
        body = {
            "success": bool(result.success),
            "reason": result.message,
            **_train_to_dict(train, state.network),
        }
        if result.success:
            return jsonify({"status": "success", **body})
        return jsonify({"error": result.message, **body}), 400

    @app.route("/api/trains/<train_id>/power-on", methods=["POST"])
    def train_power_on(train_id):
        """Power on a train (OFF -> READY)."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.power_on(state, train_id)
        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/power-off", methods=["POST"])
    def train_power_off(train_id):
        """Power off a train (any state -> OFF). Must be stopped."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.power_off(state, train_id)
        return _command_response(train, result)

    # ============================================================================
    # Emergency and Stop Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/stop", methods=["POST"])
    def train_stop(train_id):
        """Controlled stop - bring train to standby state (ATO stop)."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.ato_stop(state, train_id)
        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/calibrate", methods=["POST"])
    def calibrate_train(train_id):
        """Open a speed-calibration pass (design_new.md "Speed calibration").

        Body: ``{"measured_block_id": str}``. The pass resolves the measured
        block's two neighbours and drives itself. The pass writes nothing until
        ``/calibrate/commit``.
        """
        data = request.get_json(silent=True) or {}
        try:
            run = train_service.calibrate_train(
                state,
                train_id,
                measured_block_id=data.get("measured_block_id", ""),
            )
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "status": "success",
                **run.to_dict(),
                **_stretch_names(run.measured_block_id, run.outer_block_ids),
            }
        )

    def _stretch_names(measured_block_id, outer_block_ids):
        """The measured block's and outer blocks' names, so the panel can say
        which blocks the stretch runs between and which way each crossing ran."""
        return {
            "block_names": {
                block_id: train_service.block_name(state, block_id)
                for block_id in (measured_block_id, *outer_block_ids)
                if block_id
            }
        }

    @app.route("/api/trains/<train_id>/calibrate/readiness", methods=["GET"])
    def calibrate_readiness(train_id):
        """Every opening condition the train does not yet meet, for the panel's
        readiness line (design_new.md "Speed calibration", Opening a run takes
        control of the train).

        Query: ``measured_block_id``. Returns ``{"ready", "unmet"}``.
        """
        try:
            with state.simulation_lock:
                unmet = train_service.calibration_readiness(
                    state,
                    train_id,
                    measured_block_id=request.args.get("measured_block_id", ""),
                )
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ready": not unmet, "unmet": unmet})

    @app.route("/api/trains/<train_id>/calibrate", methods=["GET"])
    def calibrate_status(train_id):
        """Progress of the open pass, for the panel to poll. With no pass open,
        ``last_result`` says how the most recent one ended (read from the event
        stream), so the panel can show why a pass failed."""
        run = train_service.calibrate_status(state, train_id)
        if run is None:
            return jsonify({
                "open": False,
                "last_result": train_service.last_speed_calibration_result(
                    state, train_id
                ),
            })
        return jsonify(
            {
                "open": True,
                **run.to_dict(),
                **_stretch_names(run.measured_block_id, run.outer_block_ids),
            }
        )

    @app.route("/api/trains/<train_id>/calibrate", methods=["DELETE"])
    def calibrate_cancel(train_id):
        """Drop the open pass. Its samples go with it."""
        train_service.calibrate_cancel(state, train_id)
        return "", 204


    @app.route("/api/trains/<train_id>/calibrate/commit", methods=["POST"])
    def calibrate_commit(train_id):
        """Write a measured pass onto a profile.

        Body: ``{"target": "locomotive"|"model"}``. ``locomotive`` (Save to this
        locomotive) writes the two measured fields onto the instance profile this
        train names, so only this locomotive changes; ``model`` (Promote to
        model) writes them onto the model profile the instance derives from and
        clears them from the instance, so every locomotive of that model gets the
        measurement (design_new.md "What commit writes").
        """
        data = request.get_json(silent=True) or {}
        try:
            profile = train_service.calibrate_commit(
                state,
                train_id,
                target=data.get("target", "locomotive"),
            )
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success", "profile": profile.to_dict()})

    @app.route("/api/trains/<train_id>/calibrate/stretch/<block_id>", methods=["GET"])
    def calibrate_stretch(train_id, block_id):
        """The two outer blocks a measured block resolves to, for the panel to
        show before the operator starts the pass."""
        try:
            outer, length_cm = train_service.resolve_calibration_stretch(
                state, block_id
            )
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "measured_block_id": block_id,
                "outer_block_ids": list(outer),
                "length_cm": length_cm,
                **_stretch_names(block_id, outer),
            }
        )

    # ============================================================================
    # TrainController State and Monitoring
    # ============================================================================

    @app.route("/api/trains/<train_id>/onboard", methods=["GET"])
    def get_onboard_computer(train_id):
        """Get TrainController state for a train."""
        train = train_service.get_train(state, train_id)
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
            train = train_service.get_train(state, train_id)
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
                            # The remaining hold is computed by the route
                            # executor, which owns the wall/timing-basis split
                            # (design_new.md "Task time semantics"). The finish
                            # time is then shifted onto the sim-clock the HMI
                            # already shows.
                            sim_now = state.sim_controller.simulation_time
                            remaining = (
                                state.sim_controller.route_executor
                                .dwell_remaining_seconds(
                                    exec_state, route, next_idx
                                )
                            )
                            finish_sim_sec = sim_now + remaining
                            payload["current_task_kind"] = "dwell"
                            payload["dwell_remaining_sec"] = remaining
                            payload["dwell_finish_sim_sec"] = finish_sim_sec

            return jsonify(payload)

    @app.route("/api/trains/<train_id>/notifications", methods=["GET"])
    def get_train_notifications(train_id):
        """Events naming this train, most recent first.

        A filter over the one `NotificationCenter` stream, not a drain of a
        per-train queue: it acknowledges nothing and empties nothing
        (design_new.md "Runtime notifications"). Optional `kind` and
        `since_notification_id` filters let a poller ask only for what it has
        not seen.
        """
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        sim = getattr(state, "sim_controller", None)
        nc = getattr(sim, "notification_center", None) if sim else None
        if nc is None:
            return jsonify({"notifications": []})

        try:
            since = request.args.get("since_notification_id")
            since_id = int(since) if since is not None else None
        except (TypeError, ValueError):
            since_id = None

        events = nc.events_for(
            "train",
            train_id,
            kind=request.args.get("kind"),
            since_notification_id=since_id,
        )
        return jsonify({"notifications": [e.to_dict() for e in events]})

    # ============================================================================
    # Train Handle and Control Mode
    # ============================================================================

    @app.route("/api/trains/<train_id>/handle", methods=["POST"])
    def set_handle_position(train_id):
        """Set throttle/brake handle position."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        try:
            result = train_service.set_handle(
                state, train_id, position=data.get("position")
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/activate-cut-out", methods=["POST"])
    def activate_cut_out(train_id):
        """Raise an operator-initiated emergency cut-out (canonical
        commands.activate_cut_out_cmd()).

        Accepted only on a hardware environment; in Simulation it returns 400,
        because only the emergency brake exists there (design_new.md "Emergency
        cut-out", The operator-initiated path is gated to hardware environments).
        """
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        result = train_service.activate_cut_out(state, train_id)
        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/emergency-brake", methods=["POST"])
    def emergency_brake(train_id):
        """Raise the operator emergency brake (canonical eb.activate()).

        Valid for every train in any state (design_new.md "EB activation");
        `/handle` with `EB` is accepted only on a manual train.
        """
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.emergency_brake(state, train_id)
        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/release-emergency", methods=["POST"])
    def release_emergency(train_id):
        """Release the emergency brake (canonical eb.release()).

        The handle never releases EB; this is the only operator surface that
        clears the EB latch. eb.release() is latch-gated, so it fails (400)
        with a diagnostic until the train is stopped and the EB cancellation
        has committed.
        """
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        result = train_service.release_emergency(state, train_id)
        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/mode", methods=["POST"])
    def set_control_mode(train_id):
        """Set control mode (ATO or Manual)."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        try:
            result = train_service.set_mode(state, train_id, mode=data.get("mode"))
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/manual-mode-type", methods=["POST"])
    def set_manual_mode_type(train_id):
        """Set manual-mode submode ('simulation' = handle, or 'power_percent')."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        try:
            result = train_service.set_manual_mode_type(
                state, train_id, mode_type=data.get("mode_type")
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/manual-power-percent", methods=["POST"])
    def set_manual_power_percent(train_id):
        """Set manual power-percent command (0..100) when manual_mode_type == power_percent."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404

        data = request.json or {}
        if "percent" not in data:
            return jsonify({"error": "Missing 'percent' field"}), 400

        result = train_service.set_manual_power_percent(
            state, train_id, percent=data.get("percent")
        )
        return _command_response(train, result)

    # ============================================================================
    # ATO Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/ato-go", methods=["POST"])
    def ato_go(train_id):
        """Start ATO movement toward destination."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.ato_go(state, train_id)
        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/start-route", methods=["POST"])
    def start_route(train_id):
        """Start the train on its assigned route under ATO (design_new.md
        "Start route"): power up when OFF, select ATO, and engage."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.start_route(state, train_id)
        return _command_response(train, result)

    @app.route("/api/trains/<train_id>/ato-stop", methods=["POST"])
    def ato_stop(train_id):
        """Stop ATO movement (service stop)."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.ato_stop(state, train_id)
        return _command_response(train, result)

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
        try:
            train = train_service.reposition_train(
                state,
                train_id,
                position=data.get("position"),
                direction=data.get("direction"),
            )
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

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
        try:
            train = train_service.assign_speed_profile(
                state, train_id, profile_id=data.get("profile_id")
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

        return jsonify(_train_to_dict(train, state.network))

    # ============================================================================
    # Train Routes and Assignment
    # ============================================================================

    @app.route("/api/trains/<train_id>/assign-route", methods=["PUT"])
    def assign_route_to_train(train_id):
        """Assign a route to a train.

        Per `design_new.md` "Route Edit And Reconciliation" (operator retargeting / route swap), the
        service atomically writes the Action route-abort latch and updates
        `Route` assignment; it does NOT mutate `RouteExecutor.execution_states`
        or cancel the path inline -- those flow through Action cleanup, and
        Plan of the next eligible tick allocates a fresh `RouteRun`.
        """
        data = request.get_json() or {}
        try:
            train = train_service.assign_route(
                state,
                train_id,
                route_id=data.get("route_id"),
                start_mode=data.get("start_mode"),
                start_task_index=data.get("start_task_index"),
            )
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409

        return jsonify({"status": "success", **_train_to_dict(train, state.network)})
