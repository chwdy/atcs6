"""
Route Executor

Definition:
This module provides the RouteExecutor class which manages route execution for trains.
It supports multi-train routes, platform-first waypoints, and per-train execution state.
"""

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from enums import Direction, TrainState
from train_controller import TrainController, create_controller_for_train
from enums import PlatformSide
from train_route import RouteMode, TravelDirectionPolicy

if TYPE_CHECKING:
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from train import Train
    from train_route import Route, Waypoint

logger = logging.getLogger(__name__)


@dataclass
class RouteExecutionState:
    """Tracks execution state for a single train's route."""

    train_id: str
    route_id: str
    current_waypoint_index: int = 0
    direction: int = 1
    route_start_time: Optional[float] = None
    arrived_at_platform_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_platform_id: Optional[str] = None
    resolved_platform_id: Optional[str] = None


class RouteExecutor:
    """Manages route execution for all trains."""

    def __init__(
        self,
        network: "RailwayNetwork",
        path_dispatcher: Optional["PathDispatcher"] = None,
        shared_controllers: Optional[Dict[str, TrainController]] = None,
    ) -> None:
        self.network = network
        self.path_dispatcher = path_dispatcher
        self.execution_states: Dict[str, RouteExecutionState] = {}
        self.controllers = shared_controllers if shared_controllers is not None else {}
        self._owns_controllers = shared_controllers is None
        self.simulation_time: float = 0.0

    def _get_controller(self, train: "Train") -> TrainController:
        """Get or create TrainController for a train."""
        if train.train_id not in self.controllers:
            self.controllers[train.train_id] = create_controller_for_train(
                train_id=train.train_id,
                network=self.network,
            )
        return self.controllers[train.train_id]

    def update(self, delta_time: float) -> None:
        """Update route execution for active routes and assigned trains."""
        self.simulation_time += delta_time

        for train, route in self._get_active_route_train_pairs():
            self._execute_route_for_train(train, route)

    def _get_active_route_train_pairs(self) -> List[Tuple["Train", "Route"]]:
        """Collect active (train, route) pairs with compatibility fallback."""
        pairs: List[Tuple["Train", "Route"]] = []
        seen_pairs = set()
        routes = getattr(self.network, "routes", {})

        for route in routes.values():
            if not route.is_active:
                continue
            for train_id in route.assigned_train_ids:
                train = self.network.trains.get(train_id)
                if train:
                    pair_key = (train.train_id, route.route_id)
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        pairs.append((train, route))

        # Backward-compatible fallback: single train assignment on Train.
        for train in self.network.trains.values():
            if train.assigned_route_id:
                route = routes.get(train.assigned_route_id)
                if route and route.is_active:
                    if train.train_id not in route.assigned_train_ids:
                        route.assign_train(train.train_id)
                    pair_key = (train.train_id, route.route_id)
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        pairs.append((train, route))

        return pairs

    def handle_path_completion(
        self,
        train_id: str,
        path_id: str,
        _end_tile: Tuple[int, int],
        _event_time: Optional[datetime],
    ) -> bool:
        """Handle a path completion event from PathDispatcher."""
        train = self.network.trains.get(train_id)
        if not train or train.assigned_path_id != path_id:
            return False

        train.assigned_path_id = None
        train.current_path = None
        train.path_index = 0
        train.reserved_blocks = []
        train.ato_destination_block_id = None

        exec_state = self.execution_states.get(train_id)
        if exec_state:
            exec_state.waiting_for_path = False

        route = None
        if train.assigned_route_id:
            route = getattr(self.network, "routes", {}).get(train.assigned_route_id)

        if route and route.is_active:
            self._check_waypoint_arrival(train, route, exec_state)

        # Clear controller's destination — path is done
        controller = self._get_controller(train)
        controller.ato_destination_block_id = None
        controller.ato_path_id = None
        controller.transition_to_standby()
        return True

    def _execute_route_for_train(self, train: "Train", route: "Route") -> None:
        """Execute route logic for a single train."""
        exec_state = self.execution_states.get(train.train_id)
        if not exec_state or exec_state.route_id != route.route_id:
            exec_state = RouteExecutionState(
                train_id=train.train_id,
                route_id=route.route_id,
                route_start_time=self.simulation_time,
            )
            self.execution_states[train.train_id] = exec_state

        waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
        if not waypoint:
            self._handle_route_complete_for_train(train, route, exec_state)
            return

        target_platform_id = self._resolve_target_platform(train, waypoint, exec_state)
        if not target_platform_id:
            target_platform_id = waypoint.platform_id
        exec_state.resolved_platform_id = target_platform_id

        if train.state in (TrainState.STANDBY, TrainState.IDLE):
            # STANDBY: normal pre-departure state.
            # IDLE: a new route was assigned after a previous route completed.
            # In both cases, _try_depart_to_waypoint requests a path and calls
            # depart(), which infers direction and cab from the path (enabling
            # auto-reversal when the new route goes the opposite way).
            self._try_depart_to_waypoint(train, route, exec_state)
        elif train.state == TrainState.RUNNING_MOVING:
            self._check_waypoint_arrival(train, route, exec_state)
        elif train.state == TrainState.RUNNING_AT_STATION:
            self._check_dwell_complete(train, route, exec_state)
        elif train.state == TrainState.RUNNING_WAITING:
            if exec_state.waiting_for_path:
                self._try_depart_to_waypoint(train, route, exec_state)
        elif train.state == TrainState.MANUAL:
            self._check_waypoint_arrival(train, route, exec_state)

    def _try_depart_to_waypoint(
        self, train: "Train", route: "Route", exec_state: RouteExecutionState
    ) -> None:
        """Request a path and depart the train toward its current waypoint."""
        controller = self._get_controller(train)
        waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
        if not waypoint:
            return

        target_platform_id = exec_state.resolved_platform_id or waypoint.platform_id
        platform = self._get_platform(target_platform_id)
        if not platform:
            error_msg = f"Platform {target_platform_id} not found"
            route.set_error(error_msg, self.simulation_time)
            controller.wait_for_path(error_msg)
            exec_state.waiting_for_path = True
            return

        target_block_id = self._get_platform_block_id(target_platform_id)
        if not target_block_id:
            error_msg = f"Platform {target_platform_id} has no associated block"
            route.set_error(error_msg, self.simulation_time)
            controller.wait_for_path(error_msg)
            exec_state.waiting_for_path = True
            return

        current_block_id = self._get_current_block_id(train)
        if not current_block_id:
            controller.wait_for_path("Train is not in a block")
            exec_state.waiting_for_path = True
            return

        if current_block_id == target_block_id:
            exec_state.waiting_for_path = False
            route.clear_error()
            if train.state in (TrainState.STANDBY, TrainState.IDLE):
                controller.depart()
            return

        if self.path_dispatcher:
            path_result = self.path_dispatcher.create_path(
                start_block_id=current_block_id,
                end_block_id=target_block_id,
                train_id=train.train_id,
            )

            if path_result.success and path_result.path:
                exec_state.waiting_for_path = False
                route.clear_error()
                train.current_path = self._convert_path_to_train_path(path_result.path)
                train.path_index = 0
                train.assigned_path_id = path_result.path.path_id
                # Set ato_destination_block_id for proper ATO stopping
                if path_result.path.end_block_id:
                    controller.ato_destination_block_id = path_result.path.end_block_id
                    train.ato_destination_block_id = path_result.path.end_block_id
                if train.state in (TrainState.STANDBY, TrainState.IDLE):
                    controller.depart()
            else:
                error_msg = path_result.error or "Path unavailable"
                route.set_error(error_msg, self.simulation_time)
                controller.wait_for_path(error_msg)
                exec_state.waiting_for_path = True
        else:
            if train.state in (TrainState.STANDBY, TrainState.IDLE):
                controller.depart()

    def _check_waypoint_arrival(
        self,
        train: "Train",
        route: "Route",
        exec_state: Optional[RouteExecutionState],
    ) -> None:
        """Check if train has arrived at current waypoint platform."""
        if not exec_state:
            return
        waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
        if not waypoint:
            return

        target_platform_id = exec_state.resolved_platform_id or waypoint.platform_id
        if self._is_train_at_platform(train, target_platform_id):
            if exec_state.arrived_at_platform_time is None:
                exec_state.arrived_at_platform_time = self.simulation_time
                exec_state.last_waypoint_platform_id = target_platform_id
                if train.state != TrainState.MANUAL:
                    self._get_controller(train).arrive_at_station()
            self._check_dwell_complete(train, route, exec_state)

    def _check_dwell_complete(
        self, train: "Train", route: "Route", exec_state: RouteExecutionState
    ) -> None:
        """Advance route when dwell/timing conditions are met."""
        waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
        if not waypoint:
            return
        if exec_state.arrived_at_platform_time is None:
            return

        time_at_platform = self.simulation_time - exec_state.arrived_at_platform_time
        route_start_time = (
            exec_state.route_start_time or route.started_at or self.simulation_time
        )

        if not waypoint.should_depart(
            time_at_platform,
            route_start_time,
            self.simulation_time,
        ):
            return

        has_more = self._advance_to_next_waypoint(route, exec_state)
        if has_more:
            exec_state.arrived_at_platform_time = None
            exec_state.waiting_for_path = False
            if train.state != TrainState.MANUAL:
                self._get_controller(train).depart_from_station(has_more_waypoints=True)
            return

        self._handle_route_complete_for_train(train, route, exec_state)

    def _advance_to_next_waypoint(
        self, route: "Route", exec_state: RouteExecutionState
    ) -> bool:
        """Advance per-train waypoint index based on route mode."""
        waypoint_count = route.get_waypoint_count()
        if waypoint_count == 0:
            return False

        if route.mode == RouteMode.ONE_OFF:
            exec_state.current_waypoint_index += 1
            exec_state.resolved_platform_id = None
            route.current_waypoint_index = exec_state.current_waypoint_index
            return exec_state.current_waypoint_index < waypoint_count

        if route.mode == RouteMode.CIRCULAR:
            exec_state.current_waypoint_index += 1
            if exec_state.current_waypoint_index >= waypoint_count:
                exec_state.current_waypoint_index = 0
            exec_state.resolved_platform_id = None
            route.current_waypoint_index = exec_state.current_waypoint_index
            return True

        # ROUND_TRIP
        exec_state.current_waypoint_index += exec_state.direction
        if exec_state.current_waypoint_index >= waypoint_count:
            exec_state.direction = -1
            exec_state.current_waypoint_index = waypoint_count - 2
            if exec_state.current_waypoint_index < 0:
                exec_state.current_waypoint_index = 0
        elif exec_state.current_waypoint_index < 0:
            exec_state.direction = 1
            exec_state.current_waypoint_index = 1
            if exec_state.current_waypoint_index >= waypoint_count:
                exec_state.current_waypoint_index = 0

        exec_state.resolved_platform_id = None

        # Sync backward-compatible route-level fields from per-train state.
        route.current_waypoint_index = exec_state.current_waypoint_index
        route.direction = exec_state.direction
        return True

    def _handle_route_complete_for_train(
        self, train: "Train", route: "Route", exec_state: RouteExecutionState
    ) -> None:
        """Handle completion for a single train on a route."""
        controller = self._get_controller(train)

        route.unassign_train(train.train_id)
        if train.assigned_route_id == route.route_id:
            train.assigned_route_id = None

        if train.train_id in self.execution_states:
            del self.execution_states[train.train_id]

        if train.state != TrainState.MANUAL:
            controller.depart_from_station(has_more_waypoints=False)

        if route.mode == RouteMode.ONE_OFF and not route.assigned_train_ids:
            route.deactivate()

    def _resolve_target_platform(
        self,
        train: "Train",
        waypoint: "Waypoint",
        exec_state: RouteExecutionState,
    ) -> Optional[str]:
        """Resolve flexible platform target for current waypoint."""
        if not waypoint.flexible_platform:
            return waypoint.platform_id

        if exec_state.resolved_platform_id and self._is_platform_available(
            exec_state.resolved_platform_id, exec_state.train_id
        ):
            return exec_state.resolved_platform_id

        target_platform = self._get_platform(waypoint.platform_id)
        if not target_platform:
            return waypoint.platform_id

        station_group = self._find_station_group_for_platform(waypoint.platform_id)
        if not station_group:
            return waypoint.platform_id

        compatible = self._find_compatible_platforms(
            station_group,
            waypoint,
            train,
            exec_state,
            exec_state.train_id,
            target_platform,
        )
        if not compatible:
            return waypoint.platform_id
        if waypoint.platform_id in compatible:
            return waypoint.platform_id
        return compatible[0]

    def _find_station_group_for_platform(self, platform_id: str) -> Optional[object]:
        """Find logical station group containing platform_id, if present."""
        for station in getattr(self.network, "station_groups", {}).values():
            if hasattr(station, "platform_ids") and platform_id in station.platform_ids:
                return station
        return None

    def _find_compatible_platforms(
        self,
        station_group: object,
        waypoint: "Waypoint",
        train: "Train",
        exec_state: RouteExecutionState,
        train_id: str,
        target_platform: object,
    ) -> List[str]:
        """Find compatible platforms in a station group for flexible routing."""
        compatible: List[str] = []
        preserve_direction: Optional[Direction] = None
        if waypoint.travel_direction_policy == TravelDirectionPolicy.PRESERVE:
            preserve_direction = self._get_preserved_travel_direction(
                train,
                exec_state,
                target_platform,
            )

        for platform_id in getattr(station_group, "platform_ids", []):
            platform = self._get_platform(platform_id)
            if not platform:
                continue
            if not self._is_platform_available(platform_id, train_id):
                continue
            if not self._is_side_compatible(
                platform,
                target_platform,
                waypoint.required_platform_side,
            ):
                continue
            if waypoint.travel_direction_policy == TravelDirectionPolicy.PRESERVE:
                if not self._is_platform_direction_compatible(
                    train,
                    exec_state,
                    platform,
                    preserve_direction,
                ):
                    continue
            compatible.append(platform_id)
        return compatible

    def _get_platform_center(self, platform: object) -> Optional[Tuple[float, float]]:
        """Get center tile/position for platform-like object."""
        get_center_position = getattr(platform, "get_center_position", None)
        if callable(get_center_position):
            center = get_center_position()
            if (
                isinstance(center, tuple)
                and len(center) == 2
                and isinstance(center[0], (int, float))
                and isinstance(center[1], (int, float))
            ):
                return (float(center[0]), float(center[1]))

        positions = list(getattr(platform, "positions", []))
        if not positions:
            return None
        avg_x = sum(x for x, _ in positions) / len(positions)
        avg_y = sum(y for _, y in positions) / len(positions)
        return (avg_x, avg_y)

    def _direction_from_points(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
    ) -> Optional[Direction]:
        """Convert a 2D vector into dominant cardinal direction."""
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if dx == 0 and dy == 0:
            return None
        if abs(dx) > abs(dy):
            return Direction.EAST if dx > 0 else Direction.WEST
        return Direction.NORTH if dy > 0 else Direction.SOUTH

    def _direction_between_platforms(
        self, from_platform: object, to_platform: object
    ) -> Optional[Direction]:
        """Get dominant travel direction from one platform center to another."""
        from_center = self._get_platform_center(from_platform)
        to_center = self._get_platform_center(to_platform)
        if not from_center or not to_center:
            return None
        return self._direction_from_points(from_center, to_center)

    def _get_preserved_travel_direction(
        self,
        train: "Train",
        exec_state: RouteExecutionState,
        target_platform: object,
    ) -> Optional[Direction]:
        """Get preferred direction for PRESERVE policy."""
        if exec_state.last_waypoint_platform_id:
            previous_platform = self._get_platform(exec_state.last_waypoint_platform_id)
            if previous_platform:
                direction = self._direction_between_platforms(
                    previous_platform,
                    target_platform,
                )
                if direction is not None:
                    return direction

        return train.get_effective_direction()

    def _is_platform_direction_compatible(
        self,
        train: "Train",
        exec_state: RouteExecutionState,
        candidate_platform: object,
        preserve_direction: Optional[Direction],
    ) -> bool:
        """Check candidate against preserve-direction requirement."""
        if preserve_direction is None:
            return True

        candidate_direction: Optional[Direction] = None
        if exec_state.last_waypoint_platform_id:
            previous_platform = self._get_platform(exec_state.last_waypoint_platform_id)
            if previous_platform:
                candidate_direction = self._direction_between_platforms(
                    previous_platform,
                    candidate_platform,
                )
        elif train.position:
            candidate_tile = self._get_target_platform_tile(train, candidate_platform)
            if candidate_tile:
                candidate_direction = self._direction_from_points(
                    (
                        float(train.position.head_tile[0]),
                        float(train.position.head_tile[1]),
                    ),
                    (float(candidate_tile[0]), float(candidate_tile[1])),
                )

        if candidate_direction is None:
            return False
        return candidate_direction == preserve_direction

    def _is_platform_available(self, platform_id: str, train_id: str) -> bool:
        """Check if at least one block in the platform is available."""
        platform = self._get_platform(platform_id)
        if not platform:
            return False

        block_ids = set()
        get_all_assigned_blocks = getattr(platform, "get_all_assigned_blocks", None)
        if callable(get_all_assigned_blocks):
            assigned_blocks = get_all_assigned_blocks()
            if isinstance(assigned_blocks, (list, set, tuple)):
                block_ids.update(assigned_blocks)
        for x, y in getattr(platform, "positions", []):
            track = self.network.get_track(x, y)
            if track and track.block_section_id:
                block_ids.add(track.block_section_id)

        if not block_ids:
            return True

        for block_id in block_ids:
            block = self.network.blocks.get(block_id)
            if not block:
                continue
            if block.is_free() or (
                block.is_reserved() and block.occupied_by == train_id
            ):
                return True
        return False

    def _is_side_compatible(
        self,
        candidate: object,
        target: object,
        required_side: PlatformSide,
    ) -> bool:
        """Check platform side compatibility for flexible selection."""
        candidate_side = getattr(candidate, "platform_side", None)
        target_side = getattr(target, "platform_side", None)
        if candidate_side is None or target_side is None:
            return True

        candidate_side_value = (
            candidate_side.value
            if hasattr(candidate_side, "value")
            else str(candidate_side)
        )
        target_side_value = (
            target_side.value if hasattr(target_side, "value") else str(target_side)
        )

        def to_side_set(side_value: str) -> set:
            if side_value == "both":
                return {"left", "right"}
            if side_value == "left":
                return {"left"}
            if side_value == "right":
                return {"right"}
            return {"left", "right"}

        candidate_sides = to_side_set(candidate_side_value)
        target_sides = to_side_set(target_side_value)

        if required_side == PlatformSide.EITHER:
            return candidate_sides.issubset(target_sides)
        if required_side == PlatformSide.BOTH:
            return candidate_sides == {"left", "right"} and candidate_sides.issubset(
                target_sides
            )
        if required_side == PlatformSide.LEFT:
            return "left" in candidate_sides and candidate_sides.issubset(target_sides)
        if required_side == PlatformSide.RIGHT:
            return "right" in candidate_sides and candidate_sides.issubset(target_sides)
        return True

    def _is_train_at_platform(self, train: "Train", platform_id: str) -> bool:
        """Check if train head is at target platform tile."""
        if not train.position:
            return False

        platform = self._get_platform(platform_id)
        if not platform:
            return False

        target_tile = self._get_target_platform_tile(train, platform)
        if not target_tile:
            return False
        return train.position.head_tile == target_tile

    def _get_target_platform_tile(
        self, train: "Train", platform: object
    ) -> Optional[Tuple[int, int]]:
        """Pick stopping tile for platform based on travel direction."""
        positions = list(getattr(platform, "positions", []))
        if not positions:
            return None
        if len(positions) == 1:
            return positions[0]

        direction = train.get_effective_direction()
        if not direction:
            return positions[0]

        def sort_key(pos: Tuple[int, int]) -> float:
            x, y = pos
            if direction.name == "NORTH":
                return -y
            if direction.name == "SOUTH":
                return y
            if direction.name == "EAST":
                return -x
            if direction.name == "WEST":
                return x
            return 0

        return sorted(positions, key=sort_key)[0]

    def _get_target_station_tile(
        self, train: "Train", station: object
    ) -> Optional[Tuple[int, int]]:
        """Compatibility helper for station-oriented tests/callers."""
        return self._get_target_platform_tile(train, station)

    def _is_train_at_station(self, train: "Train", station_id: str) -> bool:
        """Compatibility helper for station-oriented tests/callers."""
        return self._is_train_at_platform(train, station_id)

    def _get_platform(self, platform_id: str) -> Optional[object]:
        """Get platform by ID."""
        platforms = getattr(self.network, "platforms", None)
        if isinstance(platforms, dict) and platform_id in platforms:
            return platforms[platform_id]
        return None

    def _get_platform_block_id(self, platform_id: str) -> Optional[str]:
        """Get primary block for a platform."""
        platform = self._get_platform(platform_id)
        if not platform:
            return None

        get_all_assigned_blocks = getattr(platform, "get_all_assigned_blocks", None)
        if callable(get_all_assigned_blocks):
            assigned_blocks = get_all_assigned_blocks()
            block_ids = (
                list(assigned_blocks)
                if isinstance(assigned_blocks, (list, set, tuple))
                else []
            )
            if block_ids:
                return block_ids[0]

        for x, y in getattr(platform, "positions", []):
            track = self.network.get_track(x, y)
            if track and track.block_section_id:
                return track.block_section_id
        return None

    def _get_current_block_id(self, train: "Train") -> Optional[str]:
        """Get train's current block based on head tile."""
        if not train.position:
            return None
        track = self.network.get_track(*train.position.head_tile)
        if not track:
            return None
        return track.block_section_id

    def _convert_path_to_train_path(
        self, path: object
    ) -> List[Tuple[int, int, Direction]]:
        """Convert dispatcher path tiles into train path tuples."""
        from path_utils import convert_tiles_to_train_path

        tiles = getattr(path, "path_tiles", [])
        if len(tiles) == 0:
            return []
        if len(tiles) == 1:
            raise ValueError("Cannot determine train direction for single-tile path")
        return convert_tiles_to_train_path(tiles)

    def activate_route(self, train: "Train", route: "Route") -> bool:
        """Activate route and assign train (compatibility helper)."""
        route.activate(self.simulation_time)
        route.assign_train(train.train_id)
        train.assigned_route_id = route.route_id

        self.execution_states[train.train_id] = RouteExecutionState(
            train_id=train.train_id,
            route_id=route.route_id,
            route_start_time=self.simulation_time,
        )

        controller = self._get_controller(train)
        if train.state == TrainState.IDLE and train.facing_direction:
            controller.set_direction_and_standby(train.facing_direction)
        return True

    def deactivate_route(self, train: "Train", route: "Route") -> bool:
        """Deactivate route for a single train (compatibility helper)."""
        route.unassign_train(train.train_id)
        if route.mode == RouteMode.ONE_OFF and not route.assigned_train_ids:
            route.deactivate()

        if train.assigned_route_id == route.route_id:
            train.assigned_route_id = None

        if train.train_id in self.execution_states:
            del self.execution_states[train.train_id]

        controller = self.controllers.get(train.train_id)
        if controller and train.state != TrainState.OFF and train.is_stopped():
            controller.power_off_cmd()

        if self._owns_controllers and train.train_id in self.controllers:
            del self.controllers[train.train_id]
        return True

    def set_train_waypoint_index(self, train_id: str, waypoint_index: int) -> bool:
        """Override current waypoint index for an assigned train."""
        train = self.network.trains.get(train_id)
        if not train or not train.assigned_route_id:
            return False

        route = getattr(self.network, "routes", {}).get(train.assigned_route_id)
        if not route:
            return False
        if waypoint_index < 0 or waypoint_index >= route.get_waypoint_count():
            return False

        exec_state = self.execution_states.get(train_id)
        if not exec_state or exec_state.route_id != route.route_id:
            exec_state = RouteExecutionState(
                train_id=train_id,
                route_id=route.route_id,
                route_start_time=self.simulation_time,
            )
            self.execution_states[train_id] = exec_state

        exec_state.current_waypoint_index = waypoint_index
        exec_state.arrived_at_platform_time = None
        exec_state.resolved_platform_id = None
        exec_state.waiting_for_path = False
        return True

    def get_execution_status(self, train_id: str) -> Optional[Dict]:
        """Get execution status for a train."""
        exec_state = self.execution_states.get(train_id)
        if not exec_state:
            return None

        route = getattr(self.network, "routes", {}).get(exec_state.route_id)
        status = {
            "train_id": train_id,
            "route_id": exec_state.route_id,
            "at_station": exec_state.arrived_at_platform_time is not None,
            "waiting_for_path": exec_state.waiting_for_path,
            "last_station": exec_state.last_waypoint_platform_id,
            "waypoint_index": exec_state.current_waypoint_index,
            "direction": exec_state.direction,
            "resolved_platform_id": exec_state.resolved_platform_id,
        }

        if route:
            waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
            status["current_waypoint"] = waypoint.platform_id if waypoint else None
            status["route_mode"] = route.mode.value
            status["route_active"] = route.is_active

            if exec_state.arrived_at_platform_time and waypoint:
                time_at_platform = (
                    self.simulation_time - exec_state.arrived_at_platform_time
                )
                route_start_time = (
                    exec_state.route_start_time
                    or route.started_at
                    or self.simulation_time
                )
                status["dwell_remaining_sec"] = waypoint.get_remaining_dwell_time(
                    time_at_platform,
                    route_start_time,
                    self.simulation_time,
                )

        return status
