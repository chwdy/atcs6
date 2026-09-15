"""
Train Management Routes - Train CRUD, control, and state management.

Thin HTTP layer over `services.train_service`: parse JSON, call the
service, translate `ServiceError` subclasses to status codes
(ValidationError -> 400, NotFoundError -> 404, ConflictError -> 409), and
shape the sibling-payload response. Operator commands return the
controller's `CommandResult`; a non-success result maps to 400.
"""

import time
from typing import Dict

from flask import jsonify, request

from app_state import AppState
from constants import LIGHTING_VOLTAGE_STEP_V
from services import ConflictError, NotFoundError, ValidationError
from services import train_service


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
                # design_new.md "Data Flow To UI": `waiting_for_block_id` is
                # composed here from the route-supplied
                # `get_waiting_for_block_id(...)` call, not inside
                # `get_active_path_progress(...)`.
                if dispatcher_dict is not None:
                    route_executor = getattr(
                        state.sim_controller, "route_executor", None
                    )
                    try:
                        dispatcher_dict["waiting_for_block_id"] = (
                            route_executor.waiting_for_block_id_for(train.train_id)
                            if route_executor is not None
                            else None
                        )
                    except Exception as e:
                        import logging

                        logging.getLogger(__name__).debug(
                            "Failed to compose waiting_for_block_id for train %s: %s",
                            train.train_id,
                            e,
                        )
                        dispatcher_dict["waiting_for_block_id"] = None

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
        `assigned_routes_by_train_id`, and `route_runs_by_train_id` as sibling
        collections.
        """
        trains_list = []
        controllers_by_train_id: Dict[str, object] = {}
        dispatcher_by_train_id: Dict[str, object] = {}
        assigned_routes_by_train_id: Dict[str, object] = {}
        route_runs_by_train_id: Dict[str, object] = {}
        route_executor = (
            state.sim_controller.route_executor if state.sim_controller else None
        )
        for train in train_service.list_trains(state):
            payload = _train_to_dict(train, state.network)
            trains_list.append(payload["train"])
            controllers_by_train_id[train.train_id] = payload["controller"]
            dispatcher_by_train_id[train.train_id] = payload["dispatcher_progress"]
            assigned_routes_by_train_id[train.train_id] = (
                route_executor.assigned_route_id_for(train.train_id)
                if route_executor
                else None
            )
            route_runs_by_train_id[train.train_id] = (
                route_executor.route_run_summary_for(train.train_id)
                if route_executor
                else None
            )
        return jsonify(
            {
                "trains": trains_list,
                "controllers_by_train_id": controllers_by_train_id,
                "dispatcher_progress_by_train_id": dispatcher_by_train_id,
                "assigned_routes_by_train_id": assigned_routes_by_train_id,
                "route_runs_by_train_id": route_runs_by_train_id,
            }
        )

    @app.route("/api/trains/<train_id>", methods=["GET"])
    def get_train(train_id):
        """Get details for a single train."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        return jsonify(_train_to_dict(train, state.network))

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
        """Delete a train from the network."""
        try:
            train_service.delete_train(state, train_id)
        except NotFoundError:
            return jsonify({"error": "Train not found"}), 404
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

    @app.route("/api/trains/<train_id>/why-stopped", methods=["GET"])
    def why_stopped(train_id):
        """Explain why a train is not moving, one gate at a time.

        The gates are ordered the way they actually bite; `blocking_gate` names
        the first one that is shut, which is the only one worth fixing.
        """
        try:
            return jsonify(train_service.diagnose_stopped_train(state, train_id))
        except NotFoundError:
            return jsonify({"error": "Train not found"}), 404

    # ============================================================================
    # Train Power and Direction Control
    # ============================================================================

    @app.route("/api/trains/<train_id>/power-on", methods=["POST"])
    def train_power_on(train_id):
        """Power on a train (OFF -> READY)."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.power_on(state, train_id)
        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/power-off", methods=["POST"])
    def train_power_off(train_id):
        """Power off a train (any state -> OFF). Must be stopped."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.power_off(state, train_id)
        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

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
        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

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
        return jsonify({"status": "success", **run.to_dict()})

    @app.route("/api/trains/<train_id>/calibrate", methods=["GET"])
    def calibrate_status(train_id):
        """Progress of the open pass, for the panel to poll."""
        run = train_service.calibrate_status(state, train_id)
        if run is None:
            return jsonify({"open": False})
        return jsonify({"open": True, **run.to_dict()})

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
            }
        )

    # ---------------------------------------------------------------- #
    # Lighting-voltage calibration
    # (design_new.md "Lighting-voltage calibration")
    # ---------------------------------------------------------------- #

    def _lighting_payload(run) -> dict:
        return {
            "status": "success",
            "candidate_voltage": run.candidate_voltage,
            "ceiling_voltage": run.ceiling_voltage,
            "step_v": LIGHTING_VOLTAGE_STEP_V,
        }

    @app.route("/api/trains/<train_id>/calibrate/lighting", methods=["POST"])
    def calibrate_lighting_start(train_id):
        """Open a lighting-voltage run: the candidate goes onto the rails.

        The operator steps it up until the locomotive creeps, steps back down
        one, and saves. There is no measurement step — the operator watching
        the locomotive is the instrument.
        """
        try:
            run = train_service.start_lighting_calibration(state, train_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(_lighting_payload(run))

    @app.route("/api/trains/<train_id>/calibrate/lighting/step", methods=["POST"])
    def calibrate_lighting_step(train_id):
        """Move the candidate one step. Body: ``{ "direction": 1 | -1 }``."""
        data = request.get_json(silent=True) or {}
        try:
            direction = int(data.get("direction", 1))
        except (TypeError, ValueError):
            return jsonify({"error": "direction must be 1 or -1"}), 400
        try:
            run = train_service.step_lighting_calibration(state, train_id, direction)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(_lighting_payload(run))

    @app.route("/api/trains/<train_id>/calibrate/lighting/save", methods=["POST"])
    def calibrate_lighting_save(train_id):
        """Write the candidate to the profile's `lighting_voltage` and close."""
        try:
            value = train_service.save_lighting_calibration(state, train_id)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"status": "success", "lighting_voltage": value})

    @app.route("/api/trains/<train_id>/calibrate/lighting", methods=["DELETE"])
    def calibrate_lighting_cancel(train_id):
        """Close the run without writing; the candidate is discarded."""
        train_service.cancel_lighting_calibration(state, train_id)
        return jsonify({"status": "success"})

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

        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

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
        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

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

        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

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

        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

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
        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

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
        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
        return jsonify({"error": result.message}), 400

    @app.route("/api/trains/<train_id>/ato-stop", methods=["POST"])
    def ato_stop(train_id):
        """Stop ATO movement (service stop)."""
        train = train_service.get_train(state, train_id)
        if not train:
            return jsonify({"error": "Train not found"}), 404
        result = train_service.ato_stop(state, train_id)
        if result.success:
            return jsonify({"status": "success", **_train_to_dict(train, state.network)})
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
                force=bool(data.get("force", False)),
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
