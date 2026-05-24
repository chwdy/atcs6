"""
Route-Run Routes - Read-only access to live and historical ``RouteRun``
records across all routes.

Route *definitions* live in `train_routes.py`; this module surfaces the
per-run execution records that `RouteExecutor` builds as routes run.
"""

from flask import jsonify

from app_state import AppState


def _route_run_summary(route, run) -> dict:
    """Wire-shape for one ``RouteRun`` row used by both the list and
    single-run endpoints.

    Covers the canonical schema in ``design_new.md`` "Route-run state":
    identifiers, stored state, derived ``display_state`` /
    ``current_waypoint_index`` / ``next_planning_index``, latch, and
    ``last_error``. The list endpoint also includes route name + mode so the
    panel can render them without a second fetch.
    """
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
        "current_waypoint_index": run.current_waypoint_index,
        "next_planning_index": run.next_planning_index,
        "task_count": len(run.task_runs),
        "finished_task_count": sum(
            1 for t in run.task_runs if t.finished_at is not None
        ),
        "last_error": (
            run.last_error.to_dict() if run.last_error is not None else None
        ),
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
                    "target_time": (
                        task.target_time if task and task.task_kind == "stop" else None
                    ),
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
