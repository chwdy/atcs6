"""
Route Executor

Definition:
This module provides the RouteExecutor class which manages route execution for trains.
It handles waypoint arrival detection, dwell time management, automatic turn-around,
and route mode execution (ONE_WAY, LOOP, ROUND_TRIP).

The executor integrates with:
- TrainController for state transitions (not Train methods directly)
- PathDispatcher for infrastructure reservations
- Station system for position matching
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, TYPE_CHECKING
from enums import TrainState, Direction, OPPOSITE_DIRECTIONS
from train_controller import TrainController, create_controller_for_train

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from train_route import Route, Waypoint
    from path_dispatcher import PathDispatcher

logger = logging.getLogger(__name__)


@dataclass
class RouteExecutionState:
    """
    Tracks execution state for a single train's route.

    Attributes:
        train_id: Train being tracked
        route_id: Route being executed
        arrived_at_station_time: When train arrived at current station (None if not at station)
        waiting_for_path: Whether train is waiting for path to next waypoint
        last_waypoint_station_id: Last station we visited
    """

    train_id: str
    route_id: str
    arrived_at_station_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_station_id: Optional[str] = None


class RouteExecutor:
    """
    Manages route execution for all trains.

    Handles:
    - Waypoint arrival detection
    - Dwell time management with timing modes
    - Automatic turn-around at waypoints
    - Route mode execution (ONE_WAY, LOOP, ROUND_TRIP)
    - Path requests via dispatcher
    """

    def __init__(
        self,
        network: "RailwayNetwork",
        path_dispatcher: Optional["PathDispatcher"] = None,
        shared_controllers: Optional[Dict[str, TrainController]] = None,
    ):
        """
        Initialize route executor.

        Args:
            network: Railway network
            path_dispatcher: Path dispatcher for infrastructure reservations
            shared_controllers: Shared controller dict from SimulationController (optional)
        """
        self.network = network
        self.path_dispatcher = path_dispatcher

        # Track execution state for each train
        self.execution_states: Dict[str, RouteExecutionState] = {}

        # Use shared controllers if provided, otherwise create our own
        self.controllers = shared_controllers if shared_controllers is not None else {}
        self._owns_controllers = shared_controllers is None

        # Simulation time
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
        """
        Update route execution for all trains.

        Args:
            delta_time: Time elapsed since last update (seconds)
        """
        self.simulation_time += delta_time

        # Get routes from network
        routes = getattr(self.network, "routes", {})

        for train in self.network.trains.values():
            if train.assigned_route_id:
                route = routes.get(train.assigned_route_id)
                if route and route.is_active:
                    self._execute_route_for_train(train, route)

    def _execute_route_for_train(self, train: "Train", route: "Route") -> None:
        """
        Execute route logic for a single train.

        Args:
            train: Train to execute route for
            route: Route being executed
        """
        # Get or create execution state
        exec_state = self.execution_states.get(train.train_id)
        if not exec_state:
            exec_state = RouteExecutionState(
                train_id=train.train_id, route_id=route.route_id
            )
            self.execution_states[train.train_id] = exec_state

        # Handle different train states
        if train.state == TrainState.STANDBY:
            # Train is ready - try to depart to next waypoint
            self._try_depart_to_waypoint(train, route, exec_state)

        elif train.state == TrainState.RUNNING_MOVING:
            # Check if arrived at current waypoint
            self._check_waypoint_arrival(train, route, exec_state)

        elif train.state == TrainState.RUNNING_AT_STATION:
            # Check if dwell time is complete
            self._check_dwell_complete(train, route, exec_state)

        elif train.state == TrainState.RUNNING_WAITING:
            # Waiting for path - retry periodically
            if exec_state.waiting_for_path:
                self._try_depart_to_waypoint(train, route, exec_state)

        elif train.state == TrainState.MANUAL:
            # MANUAL mode: route continues in background, check waypoint arrival
            # The user controls speed, but waypoints auto-advance when reached
            self._check_waypoint_arrival(train, route, exec_state)

    def _try_depart_to_waypoint(
        self, train: "Train", route: "Route", exec_state: RouteExecutionState
    ) -> None:
        controller = self._get_controller(train)
        waypoint = route.get_current_waypoint()
        if not waypoint:
            logger.info(
                f"Train {train.train_id}: Route {route.route_id} has no more waypoints"
            )
            return

        station = self.network.stations.get(waypoint.station_id)
        if not station:
            error_msg = f"Station {waypoint.station_id} not found"
            logger.error(f"Train {train.train_id}: {error_msg}")
            route.set_error(error_msg, self.simulation_time)
            controller.wait_for_path(error_msg)
            exec_state.waiting_for_path = True
            return

        if self._should_turn_around(train, station):
            logger.info(
                f"Train {train.train_id}: Turning around for waypoint {waypoint.station_id}"
            )
            controller.turn_around()

        if self.path_dispatcher and train.position:
            current_track = self.network.get_track(*train.position.head_tile)
            if not current_track or not current_track.block_section_id:
                if train.state == TrainState.RUNNING_WAITING:
                    train.wait_reason = "Train is not in a block"
                exec_state.waiting_for_path = True
                return

            target_block_id = self._get_station_block_id(station)
            if not target_block_id:
                error_msg = f"Station {waypoint.station_id} has no associated block"
                logger.error(f"Train {train.train_id}: {error_msg}")
                route.set_error(error_msg, self.simulation_time)
                controller.wait_for_path(error_msg)
                exec_state.waiting_for_path = True
                return

            start_block_id = current_track.block_section_id

            if start_block_id == target_block_id:
                exec_state.waiting_for_path = False
                route.clear_error()
                if train.state == TrainState.STANDBY:
                    controller.depart()
                    logger.info(
                        f"Train {train.train_id}: Already at target block, departed"
                    )
                return

            path_result = self.path_dispatcher.create_path(
                start_block_id=start_block_id,
                end_block_id=target_block_id,
                train_id=train.train_id,
            )

            if path_result.success and path_result.path:
                exec_state.waiting_for_path = False
                route.clear_error()

                train.current_path = self._convert_path_to_train_path(path_result.path)
                train.path_index = 0
                train.assigned_path_id = path_result.path.path_id

                if train.state == TrainState.STANDBY:
                    controller.depart()
                    logger.info(
                        f"Train {train.train_id}: Path acquired, departed to {waypoint.station_id}"
                    )
            else:
                error_msg = path_result.error or "Path unavailable"
                logger.info(f"Train {train.train_id}: {error_msg}")
                route.set_error(error_msg, self.simulation_time)

                if train.state == TrainState.RUNNING_WAITING:
                    train.wait_reason = error_msg
                elif train.state == TrainState.STANDBY:
                    train.wait_reason = error_msg
                else:
                    controller.wait_for_path(error_msg)
                exec_state.waiting_for_path = True
        else:
            if train.state == TrainState.STANDBY:
                controller.depart()
                logger.info(
                    f"Train {train.train_id}: No dispatcher, departed to {waypoint.station_id}"
                )

    def _should_turn_around(self, train: "Train", target_station) -> bool:
        """
        Check if train should turn around to reach target station.

        Args:
            train: Train to check
            target_station: Target station

        Returns:
            True if turn-around needed
        """
        if not train.position or not train.facing_direction:
            return False

        # Get station position
        if not hasattr(target_station, "positions") or not target_station.positions:
            return False

        target_pos = target_station.positions[0]

        # Calculate direction to target
        dx = target_pos[0] - train.position.head_tile[0]
        dy = target_pos[1] - train.position.head_tile[1]

        # Determine required direction (simplified - just use primary axis)
        if abs(dx) > abs(dy):
            required_dir = Direction.EAST if dx > 0 else Direction.WEST
        else:
            required_dir = Direction.NORTH if dy > 0 else Direction.SOUTH

        # Get train's effective direction
        effective_dir = train.get_effective_direction()

        # Check if opposite
        if (
            effective_dir
            and OPPOSITE_DIRECTIONS.get(effective_dir.name[0]) == required_dir.name[0]
        ):
            return True

        return False

    def _check_waypoint_arrival(
        self, train: "Train", route: "Route", exec_state: RouteExecutionState
    ) -> None:
        """
        Check if train has arrived at current waypoint.

        Args:
            train: Train to check
            route: Route being executed
            exec_state: Execution state
        """
        controller = self._get_controller(train)
        waypoint = route.get_current_waypoint()
        if not waypoint:
            return

        # Check if at station
        if self._is_train_at_station(train, waypoint.station_id):
            # Already at station and dwelling - check if dwell complete
            if exec_state.arrived_at_station_time is not None:
                self._check_dwell_complete(train, route, exec_state)
                return

            # First arrival at this station
            logger.info(f"Train {train.train_id}: Arrived at {waypoint.station_id}")
            exec_state.arrived_at_station_time = self.simulation_time
            exec_state.last_waypoint_station_id = waypoint.station_id

            # Handle based on train state
            if train.state == TrainState.MANUAL:
                # In MANUAL mode, the train stays in MANUAL state
                # but we track arrival for dwell time
                logger.info(
                    f"Train {train.train_id}: Dwell time started in MANUAL mode"
                )
            else:
                # Normal running states - transition to station state
                controller.arrive_at_station()

    def _check_dwell_complete(
        self, train: "Train", route: "Route", exec_state: RouteExecutionState
    ) -> None:
        """
        Check if dwell time is complete and train should depart.

        Args:
            train: Train at station
            route: Route being executed
            exec_state: Execution state
        """
        controller = self._get_controller(train)
        waypoint = route.get_current_waypoint()
        if not waypoint:
            return

        if exec_state.arrived_at_station_time is None:
            return

        time_at_station = self.simulation_time - exec_state.arrived_at_station_time

        # Check if should depart
        route_start_time = (
            route.started_at if route.started_at else self.simulation_time
        )

        if waypoint.should_depart(
            time_at_station, route_start_time, self.simulation_time
        ):
            # Dwell complete - advance to next waypoint
            logger.info(
                f"Train {train.train_id}: Dwell complete at {waypoint.station_id}"
            )

            # Advance route
            next_waypoint = route.advance_to_next_waypoint()

            if next_waypoint:
                # More waypoints - handle based on train state
                if train.state == TrainState.MANUAL:
                    # In MANUAL mode, stay in MANUAL and clear arrival tracking
                    # The user can continue driving to the next waypoint
                    logger.info(
                        f"Train {train.train_id}: Next waypoint is {next_waypoint.station_id} (MANUAL mode)"
                    )
                else:
                    # Normal running states - transition to STANDBY
                    controller.depart_from_station(has_more_waypoints=True)
                exec_state.arrived_at_station_time = None
            else:
                # Route complete (ONE_WAY only)
                logger.info(f"Train {train.train_id}: Route {route.route_id} complete")
                if train.state == TrainState.MANUAL:
                    # In MANUAL mode, stay in MANUAL but clear route
                    train.assigned_route_id = None
                else:
                    controller.depart_from_station(has_more_waypoints=False)
                route.deactivate()
                exec_state.arrived_at_station_time = None

                # Clean up execution state
                del self.execution_states[train.train_id]

    def _get_target_station_tile(
        self, train: "Train", station: "Station"
    ) -> Optional[Tuple[int, int]]:
        """
        Determine which tile the train should stop at within the station.

        The target tile is the furthest tile in the train's travel direction.
        For example, if train is traveling east, it should stop at the easternmost tile.

        Args:
            train: Train approaching/at the station
            station: Target station

        Returns:
            Target (x, y) position, or first position if direction unknown
        """
        if not hasattr(station, "positions") or not station.positions:
            # Fallback for old-style single-position stations
            return getattr(station, "position", None)

        if len(station.positions) == 1:
            # Single-tile station - only one option
            return station.positions[0]

        # Get train's effective travel direction
        travel_direction = train.get_effective_direction()
        if not travel_direction:
            # No direction set - use first position as fallback
            return station.positions[0]

        # Sort positions based on travel direction to find the "last" tile
        # The train should aim for the furthest tile in its direction of travel
        def sort_key(pos: Tuple[int, int]) -> float:
            x, y = pos
            # Return negative to get furthest (max) tile as first element when sorting
            if travel_direction.name == "NORTH":
                return -y  # Highest Y
            elif travel_direction.name == "SOUTH":
                return y  # Lowest Y
            elif travel_direction.name == "EAST":
                return -x  # Highest X
            elif travel_direction.name == "WEST":
                return x  # Lowest X
            else:
                return 0

        sorted_positions = sorted(station.positions, key=sort_key)
        return sorted_positions[
            0
        ]  # First element is the target (furthest in direction)

    def _is_train_at_station(self, train: "Train", station_id: str) -> bool:
        """
        Check if train has arrived at the target tile of the station.

        The train is considered "at station" only when it reaches the last tile
        in its direction of travel within the station.

        Args:
            train: Train to check
            station_id: Station ID

        Returns:
            True if train is at the correct target tile
        """
        if not train.position:
            return False

        station = self.network.stations.get(station_id)
        if not station:
            return False

        target_tile = self._get_target_station_tile(train, station)
        if not target_tile:
            return False

        return train.position.head_tile == target_tile

    def _get_station_block_id(self, station) -> Optional[str]:
        if hasattr(station, "positions") and station.positions:
            track = self.network.get_track(*station.positions[0])
            if track:
                return track.block_section_id
        elif hasattr(station, "position"):
            track = self.network.get_track(*station.position)
            if track:
                return track.block_section_id
        return None

    def _convert_path_to_train_path(self, path) -> list:
        from path_utils import convert_tiles_to_train_path

        tiles = path.path_tiles if hasattr(path, "path_tiles") else []

        if len(tiles) == 0:
            return []
        if len(tiles) == 1:
            raise ValueError("Cannot determine train direction for single-tile path")

        return convert_tiles_to_train_path(tiles)

    def activate_route(self, train: "Train", route: "Route") -> bool:
        """
        Activate a route for a train.

        Args:
            train: Train to activate route for
            route: Route to activate

        Returns:
            True if successful
        """
        controller = self._get_controller(train)

        # Activate route
        route.activate(self.simulation_time)

        # Assign to train
        train.assigned_route_id = route.route_id

        # Create execution state
        self.execution_states[train.train_id] = RouteExecutionState(
            train_id=train.train_id, route_id=route.route_id
        )

        # If train is IDLE, transition to STANDBY
        if train.state == TrainState.IDLE and train.facing_direction:
            controller.set_direction_and_standby(train.facing_direction)

        logger.info(f"Train {train.train_id}: Activated route {route.route_id}")
        return True

    def deactivate_route(self, train: "Train", route: "Route") -> bool:
        """
        Deactivate a route for a train.

        Args:
            train: Train to deactivate route for
            route: Route to deactivate

        Returns:
            True if successful
        """
        controller = self._get_controller(train)

        route.deactivate()
        train.assigned_route_id = None

        # Clean up execution state
        if train.train_id in self.execution_states:
            del self.execution_states[train.train_id]

        # Clean up controller instance only if we own the controllers dict
        # (don't delete from shared controllers - that's managed by SimulationController)
        if self._owns_controllers and train.train_id in self.controllers:
            del self.controllers[train.train_id]

        # Transition train to IDLE if not already
        if train.state != TrainState.OFF and train.is_stopped():
            controller.power_off_cmd()

        logger.info(f"Train {train.train_id}: Deactivated route {route.route_id}")
        return True

    def get_execution_status(self, train_id: str) -> Optional[Dict]:
        """
        Get execution status for a train.

        Args:
            train_id: Train ID

        Returns:
            Status dictionary or None
        """
        exec_state = self.execution_states.get(train_id)
        if not exec_state:
            return None

        routes = getattr(self.network, "routes", {})
        route = routes.get(exec_state.route_id)

        status = {
            "train_id": train_id,
            "route_id": exec_state.route_id,
            "at_station": exec_state.arrived_at_station_time is not None,
            "waiting_for_path": exec_state.waiting_for_path,
            "last_station": exec_state.last_waypoint_station_id,
        }

        if route:
            waypoint = route.get_current_waypoint()
            status["current_waypoint"] = waypoint.station_id if waypoint else None
            status["waypoint_index"] = route.current_waypoint_index
            status["route_mode"] = route.mode.value
            status["route_active"] = route.is_active

            if exec_state.arrived_at_station_time and waypoint:
                time_at_station = (
                    self.simulation_time - exec_state.arrived_at_station_time
                )
                route_start_time = (
                    route.started_at if route.started_at else self.simulation_time
                )
                remaining = waypoint.get_remaining_dwell_time(
                    time_at_station, route_start_time, self.simulation_time
                )
                status["dwell_remaining_sec"] = remaining

        return status
