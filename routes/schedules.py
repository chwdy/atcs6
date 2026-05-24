"""
Schedule Routes - Train-schedule CRUD (list / create / get / delete).

Thin HTTP layer over `services.schedule_service`.
"""

from flask import jsonify, request

from app_state import AppState
from services import NotFoundError, ValidationError
from services import schedule_service


def _schedule_to_dict(schedule) -> dict:
    """Serialize a TrainSchedule into the legacy wire shape used by the API."""
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
    return {
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


def register(app, state: AppState) -> None:
    """Register schedule routes."""

    @app.route("/api/schedules", methods=["GET"])
    def list_schedules():
        """List all train schedules."""
        schedules = [
            _schedule_to_dict(s) for s in schedule_service.list_schedules(state)
        ]
        return jsonify({"schedules": schedules})

    @app.route("/api/schedules", methods=["POST"])
    def create_schedule():
        """Create a new train schedule.

        Request body:
            train_id: Train to assign schedule to (required)
            stops: List of stops with station_name, arrival_time, departure_time
            priority: Schedule priority (optional, default 0)
        """
        data = request.get_json() or {}

        try:
            schedule = schedule_service.create_schedule(
                state,
                train_id=data.get("train_id"),
                stops=data.get("stops", []),
                schedule_id=data.get("schedule_id"),
                priority=data.get("priority", 0),
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

        return jsonify(
            {
                "status": "success",
                "schedule_id": schedule.schedule_id,
                "message": (
                    f"Schedule created and assigned to train {schedule.train_id}"
                ),
            }
        )

    @app.route("/api/schedules/<schedule_id>", methods=["GET"])
    def get_schedule(schedule_id):
        """Get a specific schedule by ID."""
        schedule = schedule_service.get_schedule(state, schedule_id)
        if schedule is None:
            return jsonify({"error": "Schedule not found"}), 404
        return jsonify(_schedule_to_dict(schedule))

    @app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
    def delete_schedule(schedule_id):
        try:
            schedule_service.delete_schedule(state, schedule_id)
        except NotFoundError:
            return jsonify({"error": "Schedule not found"}), 404
        return jsonify(
            {"status": "success", "message": f"Schedule {schedule_id} deleted"}
        )
