"""
Schedule Routes - Train-schedule CRUD (list / create / get / delete).
"""

import uuid
from datetime import datetime

from flask import jsonify, request

from app_state import AppState


def register(app, state: AppState) -> None:
    """Register schedule routes."""

    @app.route("/api/schedules", methods=["GET"])
    def list_schedules():
        """List all train schedules."""
        from train import TrainSchedule  # noqa: F401  (kept for parity)

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
