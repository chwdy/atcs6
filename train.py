"""
Train and Schedule Management

Definition:
This module defines train entities and their scheduling system. It includes train properties,
operational status, and time-based scheduling with stops and routing information.

TODO List:
- [ ] Add train performance metrics and monitoring
- [ ] Implement dynamic schedule adjustment based on delays
- [ ] Add train consist management (multiple cars)
- [ ] Implement energy consumption modeling
- [ ] Add maintenance scheduling integration
- [ ] Implement real-time train tracking and positioning
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime
from enums import TrainStatus, Direction


@dataclass
class ScheduleStop:
    """
    Individual stop in a train's schedule.
    
    Represents a planned stop at a station with arrival and departure times.
    """
    station_pos: Tuple[int, int]
    station_name: str
    arrival_time: datetime
    departure_time: datetime
    platform: Optional[str] = None
    
    def get_dwell_time(self) -> float:
        """Get dwell time in minutes"""
        return (self.departure_time - self.arrival_time).total_seconds() / 60
    
    def is_arrival_time_passed(self, current_time: datetime) -> bool:
        """Check if scheduled arrival time has passed"""
        return current_time > self.arrival_time
    
    def is_departure_time_passed(self, current_time: datetime) -> bool:
        """Check if scheduled departure time has passed"""
        return current_time > self.departure_time


@dataclass
class TrainSchedule:
    """
    Complete schedule for a train including all stops and timing.
    
    Manages the planned journey of a train with multiple stops,
    timing requirements, and priority levels.
    """
    train_id: str
    schedule_id: str
    stops: List[ScheduleStop] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    priority: int = 0  # Higher number = higher priority
    
    def get_current_target(self, current_time: datetime) -> Optional[ScheduleStop]:
        """Get the next scheduled stop based on current time"""
        for stop in self.stops:
            if current_time < stop.arrival_time:
                return stop
        return None
    
    def get_next_stop_after(self, station_pos: Tuple[int, int]) -> Optional[ScheduleStop]:
        """Get the next stop after a given station position"""
        found_current = False
        for stop in self.stops:
            if found_current:
                return stop
            if stop.station_pos == station_pos:
                found_current = True
        return None
    
    def is_delayed(self, current_time: datetime, current_pos: Tuple[int, int]) -> bool:
        """Check if train is currently delayed"""
        target = self.get_current_target(current_time)
        if target and target.station_pos == current_pos:
            return current_time > target.arrival_time
        return False
    
    def get_delay_minutes(self, current_time: datetime, target_stop: ScheduleStop) -> float:
        """Calculate delay in minutes for a specific stop"""
        if current_time > target_stop.arrival_time:
            delay = current_time - target_stop.arrival_time
            return delay.total_seconds() / 60
        return 0
    
    def add_stop(self, stop: ScheduleStop):
        """Add a new stop to the schedule"""
        self.stops.append(stop)
        self.stops.sort(key=lambda s: s.arrival_time)
    
    def get_total_journey_time(self) -> float:
        """Get total journey time in minutes"""
        if self.stops:
            first_stop = min(self.stops, key=lambda s: s.arrival_time)
            last_stop = max(self.stops, key=lambda s: s.departure_time)
            return (last_stop.departure_time - first_stop.arrival_time).total_seconds() / 60
        return 0


@dataclass
class Train:
    """
    Railway train with physical properties and operational state.
    
    Represents a complete train with its current position, movement capabilities,
    operational status, and assigned schedule.
    """
    train_id: str
    length: float  # Train length in track units
    min_curve_radius: float  # Minimum curve radius the train can navigate
    max_speed: float = 100.0  # Maximum speed in km/h
    current_speed: float = 0.0  # Current speed in km/h
    acceleration: float = 2.0  # Acceleration in m/s²
    
    # Position and movement
    current_pos: Optional[Tuple[int, int]] = None
    current_direction: Optional[Direction] = None
    occupied_tracks: List[Tuple[int, int]] = field(default_factory=list)
    
    # Operational status
    status: TrainStatus = TrainStatus.IDLE
    
    # Schedule management
    schedule: Optional[TrainSchedule] = None
    
    # Pathfinding and movement
    current_path: Optional[List[Tuple[int, int, Direction]]] = None
    path_index: int = 0
    
    # Block system integration
    reserved_blocks: List[str] = field(default_factory=list)
    
    def is_at_station(self, station_pos: Tuple[int, int]) -> bool:
        """Check if train is currently at a specific station"""
        return self.current_pos == station_pos and self.status == TrainStatus.AT_STATION
    
    def is_moving(self) -> bool:
        """Check if train is currently in motion"""
        return self.status == TrainStatus.RUNNING and self.current_speed > 0
    
    def is_delayed(self, current_time: datetime) -> bool:
        """Check if train is delayed according to its schedule"""
        if not self.schedule or not self.current_pos:
            return False
        return self.schedule.is_delayed(current_time, self.current_pos)
    
    def get_next_scheduled_stop(self, current_time: datetime) -> Optional[ScheduleStop]:
        """Get the next stop in the train's schedule"""
        if self.schedule:
            return self.schedule.get_current_target(current_time)
        return None
    
    def set_position(self, position: Tuple[int, int], direction: Direction):
        """Update train's current position and direction"""
        self.current_pos = position
        self.current_direction = direction
    
    def add_occupied_track(self, track_pos: Tuple[int, int]):
        """Add a track to the list of tracks occupied by this train"""
        if track_pos not in self.occupied_tracks:
            self.occupied_tracks.append(track_pos)
    
    def remove_occupied_track(self, track_pos: Tuple[int, int]):
        """Remove a track from the list of occupied tracks"""
        if track_pos in self.occupied_tracks:
            self.occupied_tracks.remove(track_pos)
    
    def clear_occupied_tracks(self):
        """Clear all occupied tracks"""
        self.occupied_tracks.clear()
    
    def has_path(self) -> bool:
        """Check if train has a planned path"""
        return self.current_path is not None and len(self.current_path) > 0
    
    def get_remaining_path_length(self) -> int:
        """Get number of remaining steps in current path"""
        if not self.has_path():
            return 0
        return len(self.current_path) - self.path_index