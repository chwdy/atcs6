"""
Route-Run Routes - Access to live and historical ``RouteRun`` records
across all routes.

Route *definitions* live in `train_routes.py`; this module surfaces the
per-run execution records that `RouteExecutor` builds as routes run, plus
the override write endpoints per design_new.md "RouteRun overrides"
canonical home.
"""

from flask import jsonify, request

from app_state import AppState
from train_route import RouteRunStatus


def _route_run_summary(route, run) -> dict:
    """Wire-shape for one ``RouteRun`` row used by both the list and
    single-run endpoints.

    Covers the canonical schema in ``design_new.md`` "Route-run state":
    identifiers, stored state, derived ``display_state`` /
    ``current_waypoint_index`` / ``next_planning_index``, latch, and
    ``last_error``. The list endpoint also includes route name + mode so the
    panel can render them without a second fetch.
    """
    # Serialize per-run overrides as a plain dict — enums are flattened to
    # their string values so the frontend can render directly. The dict is
    # sparse: only fields with active overrides are present.
    overrides_payload = {}
    for key, value in (run.overrides or {}).items():
        overrides_payload[key] = getattr(value, "value", value)
    return {
        "run_number": run.run_number,
        "train_id": run.train_id,
        "route_id": run.route_id,
        "route_name": route.name if route else "",
        "route_mode": route.mode.value if route else None,
        "started_at": run.started_at,
        "direction": run.direction,
        "state": run.current_route_status.value,
        "display_state": run.display_state,
        "path_cancelled": run.path_cancelled,
        "path_cancel_was_deviation": run.path_cancel_was_deviation,
        "current_waypoint_index": run.current_waypoint_index,
        "next_planning_index": run.next_planning_index,
        "task_count": len(run.task_runs),
        "finished_task_count": sum(
            1 for t in run.task_runs if t.finished_at is not None
        ),
        "last_error": (
            run.last_error.to_dict() if run.last_error is not None else None
        ),
        # Per design_new.md "RouteRun overrides": expose the sparse override
        # dict plus the effective values for UI display. Effective values
        # fall back to the shared Route when no override is present.
        "overrides": overrides_payload,
        "overrides_updated_at": run.overrides_updated_at,
        "effective_priority": run.effective_priority(route=route),
        "effective_acquisition_mode": (
            run.effective_acquisition_mode(route=route).value
        ),
        # Per design_new.md "Run start-mode seeding": the resolved start mode
        # this run was seeded with (diagnostic; read-only — start mode is an
        # allocation-time input, not a live override).
        "start_mode": run.start_mode.value if run.start_mode is not None else None,
    }


def register(app, state: AppState) -> None:
    """Register route-run routes."""

    @app.route("/api/route-runs", methods=["GET"])
    def list_route_runs():
        """List every `RouteRun` across every route (live + historical).

        Aggregates `route_executor.history_for_route(route_id)` for each
        registered route, returning a flat list ordered by `run_number`. The
        list panel uses this to show all runs in one place; per-task details
        and `resolved_target` come from the per-run getter below.
        """
        route_executor = state.sim_controller.route_executor
        runs_out = []
        for route_id, route in route_executor.route_registry.items():
            for run in route_executor.history_for_route(route_id):
                runs_out.append(_route_run_summary(route, run))
        runs_out.sort(key=lambda r: r["run_number"])
        return jsonify({"route_runs": runs_out})

    @app.route("/api/route-runs/<int:run_number>", methods=["GET"])
    def get_route_run(run_number: int):
        """Return one `RouteRun` by `run_number` with full per-task detail."""
        from train_route import task_location_to_dict

        route_executor = state.sim_controller.route_executor
        block_manager = state.network.block_manager
        target_run = None
        target_route = None
        for route_id, route in route_executor.route_registry.items():
            for run in route_executor.history_for_route(route_id):
                if run.run_number == run_number:
                    target_run = run
                    target_route = route
                    break
            if target_run is not None:
                break

        if target_run is None:
            return jsonify({"error": "RouteRun not found"}), 404

        tasks_out = []
        for i, task_run in enumerate(target_run.task_runs):
            task_index = task_run.task_index
            task = target_route.get_task_at(task_index) if target_route else None
            resolved = task_run.resolved_target

            # Enrich the resolved-target wire shape with terminal_tile +
            # terminal_heading so the UI can render the decorated
            # F-(x,y)-E label without re-fetching block geometry.
            resolved_payload = None
            if resolved is not None:
                geom = block_manager.direction_info(
                    resolved.requested_destination_block_id,
                    resolved.requested_destination_direction,
                )
                resolved_payload = {
                    "requested_destination_block_id": (
                        resolved.requested_destination_block_id
                    ),
                    "requested_destination_direction": (
                        resolved.requested_destination_direction
                    ),
                    "terminal_tile": geom["terminal_tile"] if geom else None,
                    "terminal_heading": geom["terminal_heading"] if geom else None,
                }

            tasks_out.append(
                {
                    "walk_position": i,
                    "task_index": task_index,
                    "task_kind": task.task_kind if task else None,
                    "task_location": (
                        task_location_to_dict(task.task_location) if task else None
                    ),
                    "task_direction": task.task_direction if task else None,
                    "task_time": task.task_time if task else None,
                    # target_time applies to every task kind (load-bearing on
                    # `dwell`, whose scheduled departure is target_time + task_time
                    # per RouteTask.should_depart) — not stop-only.
                    "target_time": task.target_time if task else None,
                    "started_at": target_run.task_started_at(i),
                    "finished_at": task_run.finished_at,
                    "delay_class": task_run.delay_class,
                    "resolved_target": resolved_payload,
                    "last_error": (
                        task_run.last_error.to_dict()
                        if task_run.last_error is not None
                        else None
                    ),
                }
            )

        payload = _route_run_summary(target_route, target_run)
        payload["route_name"] = target_route.name if target_route else ""
        payload["route_mode"] = (
            target_route.mode.value if target_route else None
        )
        payload["tasks"] = tasks_out
        return jsonify(payload)

    def _resolve_running_run_train_id(run_number: int):
        """Locate the train_id of a currently-RUNNING RouteRun by run_number.

        Returns ``None`` for any run that exists in the registry but is not
        currently RUNNING (FINISHED / DEACTIVATED). Caller maps
        ``None`` to HTTP 409 per design_new.md "RouteRun overrides" canonical
        home: historical runs reject override edits.
        """
        route_executor = state.sim_controller.route_executor
        for run in route_executor.execution_states.values():
            if run.run_number != run_number:
                continue
            if run.current_route_status != RouteRunStatus.RUNNING:
                continue
            return run.train_id
        return None

    @app.route("/api/route-runs/<int:run_number>/overrides", methods=["PATCH"])
    def patch_route_run_overrides(run_number: int):
        """Write one or more per-run overrides on a currently RUNNING run.

        Per design_new.md "RouteRun overrides" canonical home: accepts either
        a single-field body `{field: value}` (when `field` is one of the v1
        allowlist) or a batched body `{"overrides": {field: value, ...}}`.
        Historical (FINISHED / DEACTIVATED) runs reject override edits with
        HTTP 409.

        Per design_new.md "single-threaded runtime": the Flask worker holds
        `state.simulation_lock` for the duration of the iteration + per-field
        write so the simulation tick cannot insert/archive `execution_states`
        mid-request (which would otherwise raise RuntimeError on the dict
        iteration or produce a torn read of `run.overrides`).
        """
        with state.simulation_lock:
            route_executor = state.sim_controller.route_executor
            train_id = _resolve_running_run_train_id(run_number)
            if train_id is None:
                return jsonify({"error": "RouteRun is not RUNNING"}), 409

            data = request.get_json() or {}
            # Accept both wire shapes.
            if "overrides" in data and isinstance(data["overrides"], dict):
                entries = list(data["overrides"].items())
            else:
                entries = list(data.items())

            # Apply per-field. First error short-circuits and rolls back nothing
            # (per design_new.md the helper is per-field; callers should retry
            # bad fields individually).
            applied = []
            for field_name, value in entries:
                ok, msg = route_executor.set_run_override(train_id, field_name, value)
                if not ok:
                    return jsonify(
                        {
                            "error": msg,
                            "applied": applied,
                            "failed_field": field_name,
                        }
                    ), 400
                applied.append(field_name)

            # Re-read the updated run for the response. Because we hold the
            # lock, execution_state_for cannot return None for a run that
            # _resolve_running_run_train_id matched moments ago.
            run = route_executor.execution_state_for(train_id)
            route = route_executor.get_route(run.route_id) if run else None
            return jsonify(_route_run_summary(route, run))

    @app.route(
        "/api/route-runs/<int:run_number>/overrides/<field>",
        methods=["DELETE"],
    )
    def delete_route_run_override(run_number: int, field: str):
        """Clear one per-run override on a currently RUNNING run.

        Restores the shared-route value for that field via the effective_*
        helpers. Clearing a key that is not present is a no-op success.

        Holds `state.simulation_lock` for the same reason as the PATCH
        handler above.
        """
        with state.simulation_lock:
            route_executor = state.sim_controller.route_executor
            train_id = _resolve_running_run_train_id(run_number)
            if train_id is None:
                return jsonify({"error": "RouteRun is not RUNNING"}), 409
            ok, msg = route_executor.clear_run_override(train_id, field)
            if not ok:
                return jsonify({"error": msg}), 400
            run = route_executor.execution_state_for(train_id)
            route = route_executor.get_route(run.route_id) if run else None
            return jsonify(_route_run_summary(route, run))
