"""Train-schedule lifecycle service.

CRUD over the per-train `TrainSchedule` records the network owns. The
HTTP layer in `routes/schedules.py` parses JSON; this layer enforces
referential integrity (train exists, schedule exists), assigns the
schedule to the train, and tears that link down on delete.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional
import uuid

from app_state import AppState
from services.exceptions import NotFoundError, ValidationError
from train import ScheduleStop, TrainSchedule


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string into datetime, or None when blank/invalid."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def list_schedules(state: AppState) -> List[TrainSchedule]:
    """Return every schedule registered on the network."""
    return list(state.network.schedules.values())


def get_schedule(state: AppState, schedule_id: str) -> Optional[TrainSchedule]:
    """Return the schedule, or None when not found."""
    return state.network.schedules.get(schedule_id)


def require_schedule(state: AppState, schedule_id: str) -> TrainSchedule:
    """Return the schedule or raise NotFoundError."""
    schedule = state.network.schedules.get(schedule_id)
    if schedule is None:
        raise NotFoundError(f"Schedule {schedule_id!r} not found")
    return schedule


def create_schedule(
    state: AppState,
    *,
    train_id: str,
    stops: Optional[Iterable[Any]] = None,
    schedule_id: Optional[str] = None,
    priority: int = 0,
) -> TrainSchedule:
    """Create a new schedule and attach it to the named train.

    Args:
        train_id: Train that owns this schedule. Required.
        stops: Iterable of stop dicts; each is parsed into a
            ScheduleStop. Optional (an empty schedule is valid).
        schedule_id: Caller-supplied id; auto-generated when blank/None.
        priority: Higher number = higher priority.

    Raises:
        ValidationError when train_id is empty.
        NotFoundError when train_id does not name a known train.
    """
    if not train_id:
        raise ValidationError("train_id is required")

    if train_id not in state.network.trains:
        raise NotFoundError(f"Train {train_id!r} not found")

    resolved_id = schedule_id or f"SCH-{uuid.uuid4().hex[:6].upper()}"

    schedule = TrainSchedule(
        train_id=train_id,
        schedule_id=resolved_id,
        priority=priority,
    )

    for stop_data in stops or ():
        if not isinstance(stop_data, dict):
            raise ValidationError("stop entry must be an object")

        station_name = stop_data.get("station_name", "Unknown")
        station_pos = tuple(stop_data.get("station_pos", [0, 0]))

        arrival_time = _parse_dt(stop_data.get("arrival_time")) or datetime.now()
        departure_time = (
            _parse_dt(stop_data.get("departure_time"))
            or arrival_time + timedelta(minutes=2)
        )

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

    return schedule


def delete_schedule(state: AppState, schedule_id: str) -> bool:
    """Remove the schedule from the network and detach it from the train.

    Raises:
        NotFoundError when the schedule does not exist.
    """
    if schedule_id not in state.network.schedules:
        raise NotFoundError(f"Schedule {schedule_id!r} not found")

    schedule = state.network.schedules[schedule_id]

    if schedule.train_id in state.network.trains:
        train = state.network.trains[schedule.train_id]
        if train.schedule and train.schedule.schedule_id == schedule_id:
            train.schedule = None

    del state.network.schedules[schedule_id]
    return True
