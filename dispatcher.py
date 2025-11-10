"""
Train Dispatcher System

Definition:
This module implements the train dispatching and coordination system. It manages train
movements, schedule adherence, route planning, block reservation, and conflict detection
to ensure safe and efficient railway operations.

TODO List:
- [ ] Implement priority-based train scheduling
- [ ] Add automatic conflict resolution strategies
- [ ] Implement predictive delay propagation analysis
- [ ] Add support for emergency stops and diversions
- [ ] Implement automatic rescheduling for delays
- [ ] Add performance metrics and KPI tracking
"""

from typing import List, Optional, Tuple, Set
from datetime import datetime
from collections import defaultdict
from train import Train, TrainSchedule
from pathfinder import RailwayPathfinder
from enums import TrainStatus, SignalState, Direction


class TrainDispatcher:
    """
    Central train dispatching and coordination system.
    
    Manages train operations, schedule adherence, and safe movement
    coordination across the railway network.
    """
    
    def __init__(self, network):
        """
        Initialize dispatcher with railway network.
        
        Args:
            network: RailwayNetwork instance
        """
        self.network = network
        self.pathfinder = RailwayPathfinder(network)
        self.current_time = datetime.now()
        self.conflict_matrix: dict = defaultdict(set)
    
    def update_time(self, new_time: datetime):
        """Update system time"""
        self.current_time = new_time
    
    def schedule_train(self, train: Train, schedule: TrainSchedule) -> bool:
        """
        Assign schedule to train.
        
        Args:
            train: Train instance
            schedule: TrainSchedule instance
            
        Returns:
            bool: True if scheduling successful
        """
        train.schedule = schedule
        self.network.schedules[schedule.schedule_id] = schedule
        
        print(f"\nTrain {train.train_id} Schedule:")
        for i, stop in enumerate(schedule.stops):
            print(f"  Stop {i+1}: {stop.station_name}")
            print(f"    Arrival: {stop.arrival_time.strftime('%H:%M:%S')}")
            print(f"    Departure: {stop.departure_time.strftime('%H:%M:%S')}")
        
        return True
    
    def dispatch_train(self, train: Train, target_pos: Tuple[int, int]) -> bool:
        """
        Dispatch train to target position.
        
        Args:
            train: Train to dispatch
            target_pos: Target position (x, y)
            
        Returns:
            bool: True if dispatch successful
        """
        if not train.current_pos:
            print(f"✗ Train {train.train_id} has no current position")
            return False
        
        print(f"\nDispatching train {train.train_id}")
        print(f"  Current position: {train.current_pos}")
        print(f"  Target position: {target_pos}")
        
        # Find path
        path = self.pathfinder.find_path(
            train,
            start_pos=train.current_pos,
            goal_pos=target_pos,
            start_direction=train.current_direction or Direction.EAST
        )
        
        if not path:
            print(f"  ✗ No path found")
            return False
        
        print(f"  ✓ Path found, length: {len(path)} segments")
        
        # Reserve block sections
        if not self._reserve_blocks_for_path(train, path):
            print(f"  ✗ Cannot reserve required block sections")
            return False
        
        # Lock interlocking routes
        if not self._lock_interlocking_routes(train, path):
            print(f"  ✗ Cannot lock interlocking routes")
            self._release_blocks(train)
            return False
        
        train.current_path = path
        train.path_index = 0
        train.status = TrainStatus.RUNNING
        
        print(f"  ✓ Train dispatch successful")
        return True
    
    def _reserve_blocks_for_path(self, train: Train, 
                                 path: List[Tuple[int, int, Direction]]) -> bool:
        """Reserve block sections for train's path"""
        required_blocks = set()
        
        # Collect all block sections along path
        for x, y, _ in path:
            block = self.network.get_block_section((x, y))
            if block:
                required_blocks.add(block.block_id)
        
        print(f"  Required block sections: {required_blocks}")
        
        # Attempt to reserve all blocks
        reserved = []
        for block_id in required_blocks:
            block = self.network.block_sections[block_id]
            if not block.reserve(train.train_id):
                print(f"    ✗ Block section {block_id} reservation failed (state: {block.state.value})")
                # Rollback reservations
                for prev_block_id in reserved:
                    self.network.block_sections[prev_block_id].release()
                return False
            reserved.append(block_id)
            print(f"    ✓ Block section {block_id} reserved")
        
        train.reserved_blocks = reserved
        return True
    
    def _release_blocks(self, train: Train):
        """Release train's reserved block sections"""
        for block_id in train.reserved_blocks:
            block = self.network.block_sections.get(block_id)
            if block:
                block.release()
        train.reserved_blocks.clear()
    
    def _lock_interlocking_routes(self, train: Train,
                                  path: List[Tuple[int, int, Direction]]) -> bool:
        """Lock interlocking routes for path (simplified)"""
        # Simplified: just check signals and switches along path
        for x, y, exit_dir in path:
            track = self.network.get_track(x, y)
            if track and track.track_type.value == "switch":
                # Could set switch position here if needed
                pass
        return True
    
    def move_train_one_step(self, train: Train) -> bool:
        """
        Move train one step along its path.
        
        Returns:
            bool: True if movement successful, False if blocked or at destination
        """
        if not train.current_path or train.status != TrainStatus.RUNNING:
            return False
        
        if train.path_index >= len(train.current_path):
            # Reached destination
            train.status = TrainStatus.AT_STATION
            self._release_blocks(train)
            print(f"Train {train.train_id} reached destination")
            return False
        
        x, y, exit_dir = train.current_path[train.path_index]
        
        # Check signal
        track = self.network.get_track(x, y)
        if track and track.track_type.value == "signal":
            if track.signal_state == SignalState.RED:
                print(f"Train {train.train_id} waiting at red signal ({x},{y})")
                train.status = TrainStatus.WAITING
                return False
        
        # Occupy new track
        block = self.network.get_block_section((x, y))
        if block:
            block.occupy(train.train_id)
        
        # Release old track
        if train.current_pos:
            old_block = self.network.get_block_section(train.current_pos)
            if old_block and old_block.block_id not in train.reserved_blocks[1:]:
                old_block.release()
        
        train.current_pos = (x, y)
        train.current_direction = exit_dir
        train.path_index += 1
        
        return True
    
    def check_schedule_conflicts(self) -> List[Tuple[str, str]]:
        """Check for schedule conflicts between trains"""
        conflicts = []
        schedules = list(self.network.schedules.values())
        
        for i in range(len(schedules)):
            for j in range(i + 1, len(schedules)):
                if self._schedules_conflict(schedules[i], schedules[j]):
                    conflicts.append((schedules[i].train_id, schedules[j].train_id))
        
        return conflicts
    
    def _schedules_conflict(self, schedule1: TrainSchedule, 
                           schedule2: TrainSchedule) -> bool:
        """Check if two schedules conflict"""
        for stop1 in schedule1.stops:
            for stop2 in schedule2.stops:
                if stop1.station_pos == stop2.station_pos:
                    # Check for time overlap
                    if (stop1.arrival_time <= stop2.departure_time and
                        stop1.departure_time >= stop2.arrival_time):
                        return True
        return False
    
    def get_train_status_report(self) -> str:
        """Generate status report for all trains"""
        report = ["\n" + "=" * 70]
        report.append("Train Status Report")
        report.append("=" * 70)
        
        for train in self.network.trains.values():
            report.append(f"\nTrain {train.train_id}:")
            report.append(f"  Status: {train.status.value}")
            report.append(f"  Position: {train.current_pos}")
            report.append(f"  Direction: {train.current_direction.name if train.current_direction else 'N/A'}")
            
            if train.schedule:
                target = train.schedule.get_current_target(self.current_time)
                if target:
                    report.append(f"  Next stop: {target.station_name}")
                    report.append(f"  Scheduled arrival: {target.arrival_time.strftime('%H:%M:%S')}")
                    
                    if train.schedule.is_delayed(self.current_time, train.current_pos):
                        delay = train.schedule.get_delay_minutes(self.current_time, target)
                        report.append(f"  ⚠ Delayed: {delay:.1f} minutes")
        
        report.append("\n" + "=" * 70)
        return "\n".join(report)
    
    def get_block_status_report(self) -> str:
        """Generate status report for all block sections"""
        report = ["\n" + "=" * 70]
        report.append("Block Section Status")
        report.append("=" * 70)
        
        for block_id, block in self.network.block_sections.items():
            report.append(f"\n{block_id}:")
            report.append(f"  State: {block.state.value}")
            if block.occupied_by:
                report.append(f"  Occupied by: {block.occupied_by}")
        
        report.append("\n" + "=" * 70)
        return "\n".join(report)