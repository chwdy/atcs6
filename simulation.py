import logging
from datetime import datetime
from typing import Dict, Optional, TYPE_CHECKING

from network import RailwayNetwork
from train import Train
from enums import TrainState, SignalState, Direction, OPPOSITE_DIRECTIONS
from block_power import BlockPowerController
from train_atc import TrainATC
from route_executor import RouteExecutor
from speed_profile import SpeedProfile, SpeedProfileRegistry

if TYPE_CHECKING:
    from path_dispatcher import PathDispatcher

logger = logging.getLogger(__name__)


class SimulationController:
    """
    Controls train simulation and movement using the DC analog control model.

    Integrates:
    - TrainState machine for train operational states
    - TrainATC for automatic speed limiting based on clearance
    - BlockPowerController for voltage output to track blocks
    - SpeedProfile for speed-to-voltage mapping and acceleration
    - RouteExecutor for route/waypoint execution
    """

    def __init__(
        self,
        network: RailwayNetwork,
        path_dispatcher: Optional["PathDispatcher"] = None,
    ):
        self.network = network
        self.path_dispatcher = path_dispatcher
        self.last_update_time = datetime.now()

        self.power_controller = BlockPowerController(network)
        self.route_executor = RouteExecutor(network, path_dispatcher)
        self.speed_profile_registry = SpeedProfileRegistry()

        self.atc_instances: Dict[str, TrainATC] = {}
        self.simulation_time: float = 0.0

    def update(self, delta_seconds: float) -> None:
        self.simulation_time += delta_seconds

        self.route_executor.update(delta_seconds)

        for train in self.network.trains.values():
            self._update_train(train, delta_seconds)

    def _get_or_create_atc(self, train: Train) -> TrainATC:
        if train.train_id not in self.atc_instances:
            self.atc_instances[train.train_id] = TrainATC(
                train=train,
                network=self.network,
            )
        return self.atc_instances[train.train_id]

    def _get_speed_profile(self, train: Train) -> SpeedProfile:
        if train.speed_profile_id:
            profile = self.speed_profile_registry.get(train.speed_profile_id)
            if profile:
                return profile
        return self.speed_profile_registry.get_default()

    def _update_train(self, train: Train, delta_seconds: float) -> None:
        if train.state == TrainState.OFF:
            train.current_speed = 0.0
            train.target_speed = 0.0
            return

        if train.state in (
            TrainState.IDLE,
            TrainState.STANDBY,
            TrainState.RUNNING_WAITING,
            TrainState.RUNNING_AT_STATION,
        ):
            self._decelerate_to_stop(train, delta_seconds)
            self.power_controller.update_for_train(train)
            return

        if train.state == TrainState.RUNNING_MOVING:
            self._update_moving_train(train, delta_seconds)
            return

        if train.state == TrainState.MANUAL:
            self._update_manual_train(train, delta_seconds)
            return

    def _update_moving_train(self, train: Train, delta_seconds: float) -> None:
        if not train.current_path or train.path_index >= len(train.current_path):
            train.target_speed = 0.0
            self._decelerate_to_stop(train, delta_seconds)
            if train.is_stopped():
                train.state = TrainState.RUNNING_WAITING
            self.power_controller.update_for_train(train)
            return

        atc = self._get_or_create_atc(train)
        atc_limit = atc.get_speed_limit()

        base_target = train.max_speed
        limit_speed = self._check_path_limits(train)

        train.target_speed = min(base_target, atc_limit, limit_speed)

        profile = self._get_speed_profile(train)
        train.current_speed = profile.calculate_new_speed(
            train.current_speed, train.target_speed, delta_seconds
        )

        self.power_controller.update_for_train(train)

        self._update_train_position(train, delta_seconds)

    def _update_manual_train(self, train: Train, delta_seconds: float) -> None:
        atc = self._get_or_create_atc(train)
        atc_limit = atc.get_speed_limit()

        train.target_speed = min(train.manual_target_speed, atc_limit, train.max_speed)

        profile = self._get_speed_profile(train)
        train.current_speed = profile.calculate_new_speed(
            train.current_speed, train.target_speed, delta_seconds
        )

        self.power_controller.update_for_train(train)

        if train.current_speed > 0:
            if train.current_path:
                self._update_train_position(train, delta_seconds)
            else:
                self._update_manual_position_no_path(train, delta_seconds)

    def _check_path_limits(self, train: Train) -> float:
        if not train.current_pos:
            return 0.0

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return 0.0

        limit_speed = train.max_speed

        if train.path_index + 1 < len(train.current_path):
            next_x, next_y, _ = train.current_path[train.path_index + 1]
            next_track = self.network.get_track(next_x, next_y)

            if next_track:
                signals = self.network.get_signals_at(*train.current_pos)
                if signals:
                    aligned = None
                    if train.current_direction:
                        aligned = next(
                            (
                                s
                                for s in signals
                                if s.detection_direction.upper()
                                == train.current_direction.name
                            ),
                            None,
                        )
                    sig = aligned or signals[0]
                    if sig.state == SignalState.RED:
                        limit_speed = 0
                    elif sig.state == SignalState.YELLOW:
                        limit_speed = min(limit_speed, 40)

                if (
                    next_track.is_occupied()
                    and next_track.occupied_by != train.train_id
                ):
                    limit_speed = 0
        else:
            limit_speed = 0

        remaining_dist = current_track.length - train.distance_on_track
        dist_to_limit_m = remaining_dist * 100
        braking_dist = train.braking_distance

        if limit_speed < train.current_speed:
            if dist_to_limit_m <= braking_dist * 1.2:
                return limit_speed

        return train.max_speed

    def _decelerate_to_stop(self, train: Train, delta_seconds: float) -> None:
        if train.current_speed <= 0:
            train.current_speed = 0.0
            return

        profile = self._get_speed_profile(train)
        train.current_speed = profile.calculate_new_speed(
            train.current_speed, 0.0, delta_seconds
        )

        if train.current_speed < 0.1:
            train.current_speed = 0.0

    def _update_train_position(self, train: Train, delta_seconds: float) -> None:
        if not train.current_pos:
            return

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        speed_ms = train.current_speed / 3.6
        dist_m = speed_ms * delta_seconds
        dist_units = dist_m / 100.0

        train.distance_on_track += dist_units

        if train.distance_on_track >= current_track.length:
            self._advance_train_track(train)

    def _advance_train_track(self, train: Train) -> None:
        if not train.current_path:
            return

        if train.path_index + 1 >= len(train.current_path):
            current_track = self.network.get_track(*train.current_pos)
            if current_track:
                train.distance_on_track = current_track.length
            train.current_speed = 0
            train.target_speed = 0
            train.state = TrainState.RUNNING_AT_STATION
            return

        old_pos = train.current_pos
        next_x, next_y, next_dir = train.current_path[train.path_index + 1]

        next_track = self.network.get_track(next_x, next_y)
        if not next_track:
            train.current_speed = 0
            return

        current_track = self.network.get_track(*old_pos) if old_pos else None
        if current_track:
            signals = self.network.get_signals_at(*old_pos)
            if signals:
                aligned = None
                if train.current_direction:
                    aligned = next(
                        (
                            s
                            for s in signals
                            if s.detection_direction.upper()
                            == train.current_direction.name
                        ),
                        None,
                    )
                sig = aligned or signals[0]
                if sig.state == SignalState.RED:
                    train.distance_on_track = current_track.length
                    train.current_speed = 0
                    train.state = TrainState.RUNNING_WAITING
                    return

        block = (
            self.network.blocks.get(next_track.block_section_id)
            if next_track.block_section_id
            else None
        )
        if block:
            block.occupy(train.train_id)

        if old_pos and current_track and current_track.block_section_id:
            old_block = self.network.blocks.get(current_track.block_section_id)
            if old_block and old_block.block_id != (block.block_id if block else None):
                old_block.release()

        train.current_pos = (next_x, next_y)
        train.current_direction = next_dir
        train.path_index += 1
        train.distance_on_track = 0.0

    def _update_manual_position_no_path(
        self, train: Train, delta_seconds: float
    ) -> None:
        """Move train in manual mode without a pre-computed path.

        Uses facing direction and track connections to determine movement.
        """
        if not train.current_pos or not train.current_direction:
            return

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        speed_ms = train.current_speed / 3.6
        dist_m = speed_ms * delta_seconds
        dist_units = dist_m / 100.0

        train.distance_on_track += dist_units

        if train.distance_on_track >= current_track.length:
            self._advance_manual_train_no_path(train)

    def _advance_manual_train_no_path(self, train: Train) -> None:
        """Advance train to next tile when moving manually without a path."""
        if not train.current_pos or not train.current_direction:
            return

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        entry_dir = OPPOSITE_DIRECTIONS.get(train.current_direction)
        if not entry_dir:
            train.current_speed = 0
            return

        exit_dirs = current_track.get_exit_directions(entry_dir)
        if not exit_dirs:
            train.distance_on_track = current_track.length
            train.current_speed = 0
            return

        exit_dir = exit_dirs[0]

        dx, dy = 0, 0
        if exit_dir == Direction.NORTH:
            dy = 1
        elif exit_dir == Direction.SOUTH:
            dy = -1
        elif exit_dir == Direction.EAST:
            dx = 1
        elif exit_dir == Direction.WEST:
            dx = -1

        next_x = train.current_pos[0] + dx
        next_y = train.current_pos[1] + dy

        next_track = self.network.get_track(next_x, next_y)
        if not next_track:
            train.distance_on_track = current_track.length
            train.current_speed = 0
            return

        next_entry_dir = OPPOSITE_DIRECTIONS.get(exit_dir)
        if not next_entry_dir:
            train.current_speed = 0
            return

        next_exits = next_track.get_exit_directions(next_entry_dir)
        if not next_exits:
            train.distance_on_track = current_track.length
            train.current_speed = 0
            return

        signals = self.network.get_signals_at(*train.current_pos)
        if signals:
            aligned = None
            if train.current_direction:
                aligned = next(
                    (
                        s
                        for s in signals
                        if s.detection_direction.upper() == train.current_direction.name
                    ),
                    None,
                )
            sig = aligned or signals[0]
            if sig.state == SignalState.RED:
                train.distance_on_track = current_track.length
                train.current_speed = 0
                return

        if next_track.is_occupied() and next_track.occupied_by != train.train_id:
            train.distance_on_track = current_track.length
            train.current_speed = 0
            return

        if current_track.block_section_id:
            old_block = self.network.blocks.get(current_track.block_section_id)
            if old_block:
                old_block.release()

        if next_track.block_section_id:
            new_block = self.network.blocks.get(next_track.block_section_id)
            if new_block:
                new_block.occupy(train.train_id)

        train.current_pos = (next_x, next_y)
        train.current_direction = next_exits[0]
        train.distance_on_track = 0.0

    def remove_train_atc(self, train_id: str) -> None:
        if train_id in self.atc_instances:
            del self.atc_instances[train_id]
