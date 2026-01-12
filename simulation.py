import logging
from datetime import datetime
from typing import Dict, Optional, TYPE_CHECKING

from network import RailwayNetwork
from train import Train
from enums import TrainState, SignalState, Direction, OPPOSITE_DIRECTIONS, BlockState
from block_power import BlockPowerController
from train_atc import TrainATC
from route_executor import RouteExecutor
from speed_profile import SpeedProfile, SpeedProfileRegistry
from train_config import kmh_to_cm_per_sec

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
            profile = self._get_speed_profile(train)
            self.power_controller.update_for_train(train, profile)
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
                train.wait_reason = (
                    "No path assigned" if not train.current_path else "End of path"
                )
            profile = self._get_speed_profile(train)
            self.power_controller.update_for_train(train, profile)
            return

        atc = self._get_or_create_atc(train)
        atc_limit, speed_limit_reason = atc.get_speed_limit()
        train.speed_limit_reason = speed_limit_reason

        base_target = train.max_speed
        limit_speed = self._check_path_limits(train)

        train.target_speed = min(base_target, atc_limit, limit_speed)

        profile = self._get_speed_profile(train)
        train.current_speed = profile.calculate_new_speed(
            train.current_speed,
            train.target_speed,
            delta_seconds,
            train.emergency_brake,
        )

        self.power_controller.update_for_train(train, profile)

        self._update_train_position(train, delta_seconds)

        if train.target_speed <= 0 and train.is_stopped():
            if train.state != TrainState.RUNNING_WAITING:
                train.state = TrainState.RUNNING_WAITING
            if not train.wait_reason:
                train.wait_reason = self._get_wait_reason(train)
        elif train.current_speed > 0 and train.wait_reason:
            train.wait_reason = None

    def _update_manual_train(self, train: Train, delta_seconds: float) -> None:
        atc = self._get_or_create_atc(train)
        atc_limit, speed_limit_reason = atc.get_speed_limit()
        train.speed_limit_reason = speed_limit_reason

        train.target_speed = min(train.manual_target_speed, atc_limit, train.max_speed)

        profile = self._get_speed_profile(train)
        train.current_speed = profile.calculate_new_speed(
            train.current_speed,
            train.target_speed,
            delta_seconds,
            train.emergency_brake,
        )

        self.power_controller.update_for_train(train, profile)

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
                    direction = (
                        train.current_direction or train.get_effective_direction()
                    )
                    aligned = None
                    if direction:
                        aligned = next(
                            (
                                s
                                for s in signals
                                if s.detection_direction.upper() == direction.name
                            ),
                            None,
                        )
                    if aligned:
                        if aligned.state == SignalState.RED:
                            limit_speed = 0
                        elif aligned.state == SignalState.YELLOW:
                            limit_speed = min(limit_speed, 40)

                if (
                    next_track.is_occupied()
                    and next_track.occupied_by != train.train_id
                ):
                    limit_speed = 0
        else:
            limit_speed = 0

        remaining_dist_cm = (
            self.network._get_tile_length_cm(*train.current_pos)
            - train.distance_on_track
        )
        dist_to_limit_m = remaining_dist_cm / 100.0
        braking_dist = train.braking_distance

        if limit_speed < train.current_speed:
            if dist_to_limit_m <= braking_dist * 1.2:
                return limit_speed

        if limit_speed == 0 and train.is_stopped():
            return 0.0

        return train.max_speed

    def _get_wait_reason(self, train: Train) -> Optional[str]:
        if train.is_emergency_braking:
            return "Emergency stop"
        if not train.current_path:
            return "No path assigned"
        if train.path_index >= len(train.current_path):
            return "End of path"
        if not train.current_pos:
            return "Train has no position"

        current_track = self.network.get_track(*train.current_pos)
        if current_track:
            signals = self.network.get_signals_at(*train.current_pos)
            if signals:
                direction = train.current_direction or train.get_effective_direction()
                aligned = None
                if direction:
                    aligned = next(
                        (
                            s
                            for s in signals
                            if s.detection_direction.upper() == direction.name
                        ),
                        None,
                    )
                if aligned and aligned.state == SignalState.RED:
                    return f"Signal {aligned.signal_id} is red"

        next_tile = (
            train.current_path[train.path_index + 1]
            if train.path_index + 1 < len(train.current_path)
            else None
        )
        if next_tile:
            next_x, next_y, _ = next_tile
            next_track = self.network.get_track(next_x, next_y)
            if next_track and next_track.block_section_id:
                block = self.network.blocks.get(next_track.block_section_id)
                if block:
                    if block.disabled:
                        return f"Block {block.block_id} is disabled"
                    if (
                        block.state == BlockState.OCCUPIED
                        and block.occupied_by != train.train_id
                    ):
                        if block.occupied_by:
                            return f"Block {block.block_id} occupied by {block.occupied_by}"
                        return f"Block {block.block_id} is occupied"
                    if (
                        block.state == BlockState.RESERVED
                        and block.reserved_by_path != train.assigned_path_id
                    ):
                        if block.reserved_by_path:
                            return f"Block {block.block_id} reserved by path {block.reserved_by_path}"
                        return f"Block {block.block_id} is reserved"

        return "Waiting for clearance"

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

        tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
        speed_cm_per_sec = kmh_to_cm_per_sec(train.current_speed)
        dist_cm = speed_cm_per_sec * delta_seconds

        train.distance_on_track += dist_cm

        if train.distance_on_track >= tile_length_cm:
            overflow_cm = train.distance_on_track - tile_length_cm
            self._advance_train_track(train, overflow_cm)

    def _advance_train_track(self, train: Train, overflow_cm: float = 0.0) -> None:
        if not train.current_pos:
            return

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        effective_dir = train.current_direction or train.get_effective_direction()
        if not effective_dir:
            return

        on_path = self._is_train_on_path(train)

        if on_path:
            self._advance_along_path(train, overflow_cm)
        else:
            self._advance_through_block(train, effective_dir, overflow_cm)

    def _is_train_on_path(self, train: Train) -> bool:
        if not train.current_path or train.path_index >= len(train.current_path):
            return False
        path_tile = train.current_path[train.path_index]
        return train.current_pos == (path_tile[0], path_tile[1])

    def _advance_along_path(self, train: Train, overflow_cm: float) -> None:
        if train.path_index + 1 >= len(train.current_path):
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
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
                direction = train.current_direction or train.get_effective_direction()
                aligned = None
                if direction:
                    aligned = next(
                        (
                            s
                            for s in signals
                            if s.detection_direction.upper() == direction.name
                        ),
                        None,
                    )
                if aligned and aligned.state == SignalState.RED:
                    tile_length_cm = self.network._get_tile_length_cm(*old_pos)
                    train.distance_on_track = tile_length_cm
                    train.current_speed = 0
                    train.state = TrainState.RUNNING_WAITING
                    train.wait_reason = f"Signal {aligned.signal_id} is red"
                    return

        old_block_id = current_track.block_section_id if current_track else None
        new_block_id = next_track.block_section_id if next_track else None

        if train.assigned_path_id and self.path_dispatcher and new_block_id:
            self.path_dispatcher.on_train_advances(train.assigned_path_id, new_block_id)
        else:
            if new_block_id:
                block = self.network.blocks.get(new_block_id)
                if block:
                    if self.network.block_manager:
                        self.network.block_manager.occupy_block(
                            new_block_id, train.train_id
                        )
                    else:
                        block.occupy(train.train_id)

            if old_block_id and old_block_id != new_block_id:
                if self.network.block_manager:
                    self.network.block_manager.clear_block(old_block_id)
                else:
                    old_block = self.network.blocks.get(old_block_id)
                    if old_block:
                        old_block.release()

        train.current_pos = (next_x, next_y)
        train.current_direction = next_dir
        train.path_index += 1
        train.distance_on_track = overflow_cm

    def _advance_through_block(
        self, train: Train, effective_dir: Direction, overflow_cm: float
    ) -> None:
        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        entry_dir = effective_dir.opposite()
        exit_dirs = current_track.get_exit_directions(entry_dir)
        if not exit_dirs:
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
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
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
            train.current_speed = 0
            return

        next_entry_dir = exit_dir.opposite()
        next_exits = next_track.get_exit_directions(next_entry_dir)
        if not next_exits:
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
            train.current_speed = 0
            return

        signals = self.network.get_signals_at(*train.current_pos)
        if signals:
            aligned = next(
                (
                    s
                    for s in signals
                    if s.detection_direction.upper() == effective_dir.name
                ),
                None,
            )
            if aligned and aligned.state == SignalState.RED:
                tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
                train.distance_on_track = tile_length_cm
                train.current_speed = 0
                return

        if next_track.is_occupied() and next_track.occupied_by != train.train_id:
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
            train.current_speed = 0
            return

        old_block_id = current_track.block_section_id
        new_block_id = next_track.block_section_id

        if old_block_id != new_block_id:
            if train.assigned_path_id and self.path_dispatcher and new_block_id:
                self.path_dispatcher.on_train_advances(
                    train.assigned_path_id, new_block_id
                )
            else:
                if self.network.block_manager:
                    if old_block_id:
                        self.network.block_manager.clear_block(old_block_id)
                    if new_block_id:
                        self.network.block_manager.occupy_block(
                            new_block_id, train.train_id
                        )
                else:
                    if old_block_id:
                        old_block = self.network.blocks.get(old_block_id)
                        if old_block:
                            old_block.release()
                    if new_block_id:
                        new_block = self.network.blocks.get(new_block_id)
                        if new_block:
                            new_block.occupy(train.train_id)

        train.current_pos = (next_x, next_y)
        train.current_direction = exit_dir
        train.distance_on_track = overflow_cm

        if train.current_path and not self._is_train_on_path(train):
            for i, (px, py, pdir) in enumerate(train.current_path):
                if (px, py) == train.current_pos:
                    train.path_index = i
                    break

    def _update_manual_position_no_path(
        self, train: Train, delta_seconds: float
    ) -> None:
        """Move train in manual mode without a pre-computed path.

        Uses facing direction and track connections to determine movement.
        """
        if not train.current_pos:
            return

        effective_dir = train.get_effective_direction()
        if not effective_dir:
            return

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
        speed_cm_per_sec = kmh_to_cm_per_sec(train.current_speed)
        dist_cm = speed_cm_per_sec * delta_seconds

        train.distance_on_track += dist_cm

        if train.distance_on_track >= tile_length_cm:
            overflow_cm = train.distance_on_track - tile_length_cm
            self._advance_manual_train_no_path(train, effective_dir, overflow_cm)

    def _advance_manual_train_no_path(
        self, train: Train, effective_dir: Direction, overflow_cm: float = 0.0
    ) -> None:
        """Advance train to next tile when moving manually without a path."""
        if not train.current_pos:
            return

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        entry_dir = effective_dir.opposite()

        exit_dirs = current_track.get_exit_directions(entry_dir)
        if not exit_dirs:
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
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
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
            train.current_speed = 0
            return

        next_entry_dir = exit_dir.opposite()

        next_exits = next_track.get_exit_directions(next_entry_dir)
        if not next_exits:
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
            train.current_speed = 0
            return

        signals = self.network.get_signals_at(*train.current_pos)
        if signals:
            aligned = None
            if effective_dir:
                aligned = next(
                    (
                        s
                        for s in signals
                        if s.detection_direction.upper() == effective_dir.name
                    ),
                    None,
                )
            sig = aligned or signals[0]
            if sig.state == SignalState.RED:
                tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
                train.distance_on_track = tile_length_cm
                train.current_speed = 0
                return

        if next_track.is_occupied() and next_track.occupied_by != train.train_id:
            tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
            train.distance_on_track = tile_length_cm
            train.current_speed = 0
            return

        if current_track.block_section_id:
            old_block_id = current_track.block_section_id
            new_block_id = next_track.block_section_id if next_track else None

            if train.assigned_path_id and self.path_dispatcher and new_block_id:
                self.path_dispatcher.on_train_advances(
                    train.assigned_path_id, new_block_id
                )
            else:
                if self.network.block_manager:
                    if old_block_id:
                        self.network.block_manager.clear_block(old_block_id)
                    if new_block_id:
                        self.network.block_manager.occupy_block(
                            new_block_id, train.train_id
                        )
                else:
                    old_block = self.network.blocks.get(old_block_id)
                    if old_block:
                        old_block.release()

                    if new_block_id:
                        new_block = self.network.blocks.get(new_block_id)
                        if new_block:
                            new_block.occupy(train.train_id)
        elif next_track.block_section_id:
            if self.network.block_manager:
                self.network.block_manager.occupy_block(
                    next_track.block_section_id, train.train_id
                )
            else:
                new_block = self.network.blocks.get(next_track.block_section_id)
                if new_block:
                    new_block.occupy(train.train_id)

        train.current_pos = (next_x, next_y)
        train.current_direction = next_exits[0]
        train.distance_on_track = overflow_cm

    def remove_train_atc(self, train_id: str) -> None:
        if train_id in self.atc_instances:
            del self.atc_instances[train_id]
