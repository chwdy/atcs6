"""
Train Dispatcher System

Definition:
This module implements the train dispatching and coordination system. It manages train
movements, schedule adherence, route planning, block reservation, and conflict detection
to ensure safe and efficient railway operations.

Note: This is a legacy dispatcher. The new path-based dispatcher is in path_dispatcher.py.
This module is kept for backward compatibility with existing simulation code.

TODO List:
- [ ] Implement priority-based train scheduling
- [ ] Add automatic conflict resolution strategies
- [ ] Implement predictive delay propagation analysis
- [ ] Add support for emergency stops and diversions
- [ ] Implement automatic rescheduling for delays
- [x] Add performance metrics and KPI tracking
"""

import logging
from typing import List, Optional, Tuple
from datetime import datetime
from collections import defaultdict
from train import Train, TrainSchedule
from pathfinder import RailwayPathfinder
from enums import TrainState, SignalState, Direction

logger = logging.getLogger(__name__)


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

        logger.info("Train %s Schedule:", train.train_id)
        for i, stop in enumerate(schedule.stops):
            logger.info("  Stop %d: %s", i + 1, stop.station_name)
            logger.info("    Arrival: %s", stop.arrival_time.strftime("%H:%M:%S"))
            logger.info("    Departure: %s", stop.departure_time.strftime("%H:%M:%S"))

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
            logger.warning("Train %s has no current position", train.train_id)
            return False

        logger.info("Dispatching train %s", train.train_id)
        logger.info("  Current position: %s", train.current_pos)
        logger.info("  Target position: %s", target_pos)

        # Find path
        path = self.pathfinder.find_path(
            train,
            start_pos=train.current_pos,
            goal_pos=target_pos,
            start_direction=train.current_direction or Direction.EAST,
        )

        if not path:
            logger.warning("  No path found")
            return False

        logger.info("  Path found, length: %d segments", len(path))

        # Reserve block sections
        if not self._reserve_blocks_for_path(train, path):
            logger.warning("  Cannot reserve required block sections")
            return False

        # Lock interlocking routes
        if not self._lock_interlocking_routes(train, path):
            logger.warning("  Cannot lock interlocking routes")
            self._release_blocks(train)
            return False

        train.current_path = path
        train.path_index = 0
        if train.state == TrainState.STANDBY:
            train.depart()
        elif train.state == TrainState.IDLE:
            if train.facing_direction:
                train.state = TrainState.RUNNING_MOVING
            else:
                train.set_direction_and_standby(
                    train.current_direction or Direction.EAST
                )
                train.depart()

        logger.info("  Train dispatch successful")
        return True

    def _reserve_blocks_for_path(
        self, train: Train, path: List[Tuple[int, int, Direction]]
    ) -> bool:
        """Reserve block sections for train's path"""
        required_blocks = set()

        # Collect all block sections along path
        for x, y, _ in path:
            track = self.network.get_track(x, y)
            if track and track.block_section_id:
                required_blocks.add(track.block_section_id)

        logger.debug("  Required block sections: %s", required_blocks)

        # Attempt to reserve all blocks
        reserved = []
        for block_id in required_blocks:
            block = self.network.blocks.get(block_id)
            if not block:
                continue
            if not block.reserve(train.train_id):
                logger.warning(
                    "    Block section %s reservation failed (state: %s)",
                    block_id,
                    block.state.value,
                )
                # Rollback reservations
                for prev_block_id in reserved:
                    prev_block = self.network.blocks.get(prev_block_id)
                    if prev_block:
                        prev_block.release()
                return False
            reserved.append(block_id)
            logger.debug("    Block section %s reserved", block_id)

        train.reserved_blocks = reserved
        return True

    def _release_blocks(self, train: Train):
        """Release train's reserved block sections"""
        for block_id in train.reserved_blocks:
            block = self.network.blocks.get(block_id)
            if block:
                block.release()
        train.reserved_blocks.clear()

    def _lock_interlocking_routes(
        self, train: Train, path: List[Tuple[int, int, Direction]]
    ) -> bool:
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
        if not train.current_path or train.state not in (
            TrainState.RUNNING_MOVING,
            TrainState.MANUAL,
        ):
            return False

        if train.path_index >= len(train.current_path):
            train.state = TrainState.RUNNING_AT_STATION
            self._release_blocks(train)
            logger.info("Train %s reached destination", train.train_id)
            return False

        x, y, exit_dir = train.current_path[train.path_index]

        # Check signal
        track = self.network.get_track(x, y)
        if track and track.track_type.value == "signal":
            signals = self.network.get_signals_at(x, y)
            blocking = None
            if signals:
                # Prefer signal where detection_direction matches train's travel direction
                blocking = next(
                    (
                        s
                        for s in signals
                        if s.detection_direction.upper() == exit_dir.name
                    ),
                    signals[0],
                )
            if blocking and blocking.state == SignalState.RED:
                logger.debug(
                    "Train %s waiting at red signal (%d,%d)", train.train_id, x, y
                )
                train.state = TrainState.RUNNING_WAITING
                return False

        # Occupy new track
        track = self.network.get_track(x, y)
        if track and track.block_section_id:
            block = self.network.blocks.get(track.block_section_id)
            if block:
                block.occupy(train.train_id)

        # Release old track
        if train.current_pos:
            old_track = self.network.get_track(*train.current_pos)
            if old_track and old_track.block_section_id:
                old_block = self.network.blocks.get(old_track.block_section_id)
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

    def _schedules_conflict(
        self, schedule1: TrainSchedule, schedule2: TrainSchedule
    ) -> bool:
        """Check if two schedules conflict"""
        for stop1 in schedule1.stops:
            for stop2 in schedule2.stops:
                if stop1.station_pos == stop2.station_pos:
                    # Check for time overlap
                    if (
                        stop1.arrival_time <= stop2.departure_time
                        and stop1.departure_time >= stop2.arrival_time
                    ):
                        return True
        return False

    def get_train_status_report(self) -> str:
        """Generate status report for all trains"""
        report = ["\n" + "=" * 70]
        report.append("Train Status Report")
        report.append("=" * 70)

        for train in self.network.trains.values():
            report.append(f"\nTrain {train.train_id}:")
            report.append(f"  State: {train.state.value}")
            report.append(f"  Position: {train.current_pos}")
            report.append(
                f"  Direction: {train.current_direction.name if train.current_direction else 'N/A'}"
            )

            if train.schedule:
                target = train.schedule.get_current_target(self.current_time)
                if target:
                    report.append(f"  Next stop: {target.station_name}")
                    report.append(
                        f"  Scheduled arrival: {target.arrival_time.strftime('%H:%M:%S')}"
                    )

                    if train.schedule.is_delayed(self.current_time, train.current_pos):
                        delay = train.schedule.get_delay_minutes(
                            self.current_time, target
                        )
                        report.append(f"  ⚠ Delayed: {delay:.1f} minutes")

        report.append("\n" + "=" * 70)
        return "\n".join(report)

    def get_block_status_report(self) -> str:
        """Generate status report for all block sections"""
        report = ["\n" + "=" * 70]
        report.append("Block Section Status")
        report.append("=" * 70)

        for block_id, block in self.network.blocks.items():
            report.append(f"\n{block_id}:")
            report.append(f"  State: {block.state.value}")
            if block.occupied_by:
                report.append(f"  Occupied by: {block.occupied_by}")

        report.append("\n" + "=" * 70)
        return "\n".join(report)
