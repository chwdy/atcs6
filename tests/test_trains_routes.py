"""
Route execution tests - Routes, path assignment, stations

Run with: pytest tests/test_trains_routes.py -v
"""

import logging
import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, LeadingEnd, TrainState, HandlePosition
from train import Train, TrainCar, TrainPosition
from tests.train_test_helpers import make_train_position
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode, RouteTask
from switch import Switch


def Waypoint(platform_id=None, dwell_time_sec=0.0, **kwargs):
    """v1 shim: legacy Waypoint constructor → RouteTask.

    Tests that called ``Waypoint(platform_id=..., dwell_time_sec=...)`` are
    integration tests for the v0 route executor's per-waypoint dispatch loop.
    Most are skipped at runtime because the v0 dispatch loop, the
    ``Path.end_block_id`` cursor, the ``route.waypoints`` accessor, and
    ``execution_states[*].current_waypoint_index`` are all gone in v1. This
    shim exists only so the module collects and the ``Waypoint`` symbol
    remains a name for the few tests that still ask for it.
    """
    return RouteTask(
        task_kind="waypoint",
        task_location=platform_id or kwargs.get("block_id") or "",
        dwell_time_sec=dwell_time_sec,
    )


def _route_add_waypoint(self, task):
    """v1 shim: legacy Route.add_waypoint → Route.add_task."""
    self.add_task(task)


# Monkey-patch Route to accept the legacy add_waypoint name. Real v1 code
# uses add_task; tests written for v0 still call add_waypoint and should
# behave identically since the Waypoint shim above already returns a
# RouteTask.
Route.add_waypoint = _route_add_waypoint

# Route.waypoints alias for legacy v0 reads (route.waypoints[i].platform_id).
# RouteTask carries task_location (a block_id, platform_id, or station_id);
# we expose a ``platform_id`` property that re-reads task_location.
def _route_waypoints(self):
    return self.tasks


Route.waypoints = property(_route_waypoints)


def _routetask_platform_id(self):
    return self.task_location


RouteTask.platform_id = property(_routetask_platform_id)


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network


class TestBlockTileSyncValidation:
    """
    Tests for block-tile position sync validation.

    These tests verify that the sync validation detects and corrects
    desyncs between tiles and blocks.
    """

    @pytest.fixture
    def physics_setup(self):
        """Create network, train, and physics."""
        from network import RailwayNetwork
        from train import Train, TrainPosition
        from train_physics import TrainPhysics
        from train_controller import TrainController
        from block import Block
        from enums import Direction, BlockState

        network = RailwayNetwork()

        # Create track with blocks
        from track import TrackSegment, TRACK_EW

        # v1: Block.state is read-only; new blocks are FREE by default.
        block = Block(block_id="B1", length_cm=300.0)
        network.blocks["B1"] = block

        for x in range(6):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            track.block_section_id = "B1"
            network.add_track(track)

        train = Train(train_id="T1", max_speed=60.0)
        train.position = TrainPosition(
            end_a_tile=(2, 0),
            end_a_distance_cm=25.0,
            end_a_entry_edge="W",
            end_b_tile=(0, 0),
            end_b_distance_cm=10.0,
            end_b_entry_edge="W",
            leading_end=LeadingEnd.A,
            occupied_tiles=[(0, 0), (1, 0), (2, 0)],
            occupied_blocks=["B1"],
        )
        network.add_train(train)

        controller = TrainController(train_id="T1", network=network)
        physics = TrainPhysics(train_id="T1", network=network, controller=controller)

        return network, train, physics

    def test_sync_validation_returns_true_when_synced(self, physics_setup):
        """Validation should return True when tiles and the local tracker match.

        v1: physics._validate_position_sync now compares tile-derived blocks
        against the physics-local last-occupied-blocks tracker (not against
        train.position.occupied_blocks).
        """
        _, train, physics = physics_setup
        # Seed the physics internal tracker so tiles match the tracker.
        physics._last_occupied_blocks = {"B1"}
        result = physics._validate_position_sync(train)
        assert result is True, "Should return True when synced"

    def test_sync_validation_detects_desync(self, physics_setup):
        """Validation should detect when local tracker doesn't match tiles."""
        _, train, physics = physics_setup
        # Force a tracker mismatch.
        physics._last_occupied_blocks = set()
        result = physics._validate_position_sync(train)
        assert result is False, "Should return False when desynced"

    # v1: removed test_sync_validation_auto_corrects — auto-correction was
    # removed; corrections live in the dispatcher progress flow now.


@pytest.mark.skip(
    reason=(
        "Stale skip (PR 6 audit): the skip cites a "
        "`PathDispatcher._compute_block_traversals` bug that no longer "
        "reproduces — multi-block path acquisition works in current code. "
        "The actual blocker for these tests is API drift in the test code "
        "itself (e.g. `Waypoint(platform_id=..., dwell_time_sec=...)` uses "
        "the pre-v1 Waypoint shape; v1 uses `RouteTask(...)`). Re-enabling "
        "these tests requires porting the test fixtures to the v1 RouteTask "
        "/ RouteRun API; the dispatcher itself is not the blocker."
    )
)
class TestRouteExecutorThreeStationLoopSimulation:
    """End-to-end route execution on a 3-station eastbound line."""

    def _build_three_station_line(self) -> RailwayNetwork:
        """Build A-B-C eastbound line with requested geometry and limits."""
        network = RailwayNetwork()

        for x in range(9):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        network.add_station(
            0,
            0,
            station_id="STA_A",
            name="A",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )
        network.add_station(
            4,
            0,
            station_id="STA_B",
            name="B",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )
        network.add_station(
            8,
            0,
            station_id="STA_C",
            name="C",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        network.create_block_with_tracks(
            block_id="BLK_AB",
            positions=[(1, 0), (2, 0), (3, 0)],
            name="A-B",
            length_cm=150.0,
        )
        network.create_block_with_tracks(
            block_id="BLK_BC",
            positions=[(5, 0), (6, 0), (7, 0)],
            name="B-C",
            length_cm=150.0,
        )

        for block in network.blocks.values():
            block.speed_limit_kmh = 60.0

        return network

    def _run_until(
        self,
        simulation: SimulationController,
        condition,
        failure_message: str,
        max_steps: int = 20000,
        dt: float = 0.02,
    ) -> None:
        for _ in range(max_steps):
            simulation.update(dt)
            if condition():
                return
        pytest.fail(failure_message)

    def _force_arrival_at_platform(
        self,
        simulation: SimulationController,
        route: Route,
        train: Train,
        controller: TrainController,
        platform_id: str,
    ) -> None:
        """Force train at waypoint platform and trigger arrival logic deterministically."""
        platform = simulation.network.platforms[platform_id]
        tile = platform.positions[0]
        direction = train.facing_direction or Direction.EAST

        train.position = make_train_position(tile, direction)
        train.current_speed = 0.0
        train.assigned_path_id = None
        train.current_path = None
        train.reserved_blocks = []

        if simulation.path_dispatcher:
            for path_id in list(simulation.path_dispatcher.active_paths.keys()):
                simulation.path_dispatcher.cancel_path(path_id)

        train.state = TrainState.RUNNING_MOVING
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.running = True

        exec_state = simulation.route_executor.execution_states.get(train.train_id)
        if exec_state is None:
            simulation.update(0.02)
            exec_state = simulation.route_executor.execution_states.get(train.train_id)
        assert exec_state is not None
        simulation.route_executor._check_waypoint_arrival(train, route, exec_state)
        assert exec_state.last_waypoint_platform_id == platform_id
        assert train.state in (TrainState.RUNNING_AT_STATION, TrainState.READY)

    def _create_sim_and_train(self, train_id: str) -> tuple:
        """Create baseline 3-station simulation with one 45cm train at A."""
        network = self._build_three_station_line()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(car_id=f"CAR_{train_id}", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id=train_id, consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.state = TrainState.READY
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"
        controller.ops.state = TrainState.READY
        assert controller.commands.set_cab("A").success is True
        assert controller.commands.cab == "A"

        return network, dispatcher, simulation, train, controller

    def test_ato_route_a_b_c_loop_with_cab_switching(self):
        """Validate A->B->C->A loop execution and cab changes in ATO mode."""
        network = self._build_three_station_line()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(car_id="CAR_45CM", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id="T_LOOP", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.state = TrainState.READY
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"
        controller.ops.state = TrainState.READY
        assert controller.commands.set_cab("A").success is True
        assert controller.commands.cab == "A"

        route = Route(
            route_id="ROUTE_ABC_LOOP", name="A-B-C Loop", mode=RouteMode.CIRCULAR
        )
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.8))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.8))
        route.add_waypoint(Waypoint(platform_id="STA_A", dwell_time_sec=0.8))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        assert simulation.route_executor._is_train_at_platform(train, "STA_A") is True

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Train did not auto-dispatch from A to B",
        )

        self._force_arrival_at_platform(simulation, route, train, controller, "STA_B")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "Route executor did not dispatch B to C after dwell",
        )

        self._force_arrival_at_platform(simulation, route, train, controller, "STA_C")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_A_BLOCK"
                and train.facing_direction == Direction.WEST
                and controller.commands.cab == "B"
            ),
            "Train did not switch cab and dispatch from C back to A",
            max_steps=5000,
        )

        # Return leg should target A directly (no B waypoint between C and A)
        exec_state = simulation.route_executor.execution_states[train.train_id]
        assert route.waypoints[exec_state.current_waypoint_index].platform_id == "STA_A"
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_A")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Train did not switch cab back at A for next loop",
            max_steps=5000,
        )

    def test_one_off_route_a_b_c_completes_and_unassigns(self):
        """ONE_OFF route should run A->B->C then clear assignment."""
        network, dispatcher, simulation, train, controller = self._create_sim_and_train(
            "T_ONE_OFF"
        )

        route = Route(
            route_id="ROUTE_ABC_ONE_OFF", name="A-B-C", mode=RouteMode.ONE_OFF
        )
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.6))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.6))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "ONE_OFF did not dispatch A->B",
        )
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_B")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "ONE_OFF did not dispatch B->C",
        )
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_C")

        self._run_until(
            simulation,
            lambda: train.assigned_route_id is None,
            "Train was not unassigned after ONE_OFF completion",
            max_steps=3000,
        )
        assert train.state in (
            TrainState.READY,
            TrainState.READY,
            TrainState.RUNNING_AT_STATION,
        )

    def test_round_trip_route_runs_a_b_c_b_a(self):
        """ROUND_TRIP route should follow A->B->C->B->A sequence."""
        network, dispatcher, simulation, train, controller = self._create_sim_and_train(
            "T_ROUND_TRIP"
        )

        route = Route(
            route_id="ROUTE_ABC_ROUND_TRIP",
            name="A-B-C-B-A",
            mode=RouteMode.ROUND_TRIP,
        )
        route.add_waypoint(Waypoint(platform_id="STA_A", dwell_time_sec=0.4))
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.4))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.4))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        visited = []
        for target in ["STA_A", "STA_B", "STA_C", "STA_B", "STA_A"]:
            self._run_until(
                simulation,
                lambda: (
                    simulation.route_executor.execution_states.get(train.train_id)
                    is not None
                    and route.waypoints[
                        simulation.route_executor.execution_states[
                            train.train_id
                        ].current_waypoint_index
                    ].platform_id
                    == target
                ),
                f"ROUND_TRIP did not advance current waypoint to {target}",
                max_steps=5000,
            )
            self._force_arrival_at_platform(
                simulation, route, train, controller, target
            )
            visited.append(target)

        assert visited == ["STA_A", "STA_B", "STA_C", "STA_B", "STA_A"]

    def test_unassign_running_train_between_b_and_c_stops_at_b_or_c(self):
        """Unassigning while running should safely stop at nearby station block endpoint."""
        network, dispatcher, simulation, train, controller = self._create_sim_and_train(
            "T_UNASSIGN_RUN"
        )

        route = Route(route_id="ROUTE_UNASSIGN", name="A-B-C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=0.6))
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.6))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Did not dispatch A->B",
        )
        self._force_arrival_at_platform(simulation, route, train, controller, "STA_B")

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "Did not dispatch B->C",
        )

        # Move to in-between block tile and keep running, then unassign route mid-run.
        train.position = make_train_position((6, 0), Direction.EAST)
        train.current_speed = 20.0
        train.state = TrainState.RUNNING_MOVING
        controller.ops.state = TrainState.RUNNING_MOVING
        controller.ato.running = True

        route.unassign_train(train.train_id)
        train.assigned_route_id = None
        simulation.route_executor.execution_states.pop(train.train_id, None)

        if train.assigned_path_id:
            dispatcher.cancel_path(train.assigned_path_id)
        train.assigned_path_id = None
        train.current_path = None
        train.reserved_blocks = []

        auto_park_route = Route(
            route_id="TEMP_UNASSIGN_AUTO_PARK",
            name="AutoPark",
            mode=RouteMode.ONE_OFF,
        )
        auto_park_route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.4))
        auto_park_route.assign_train(train.train_id)
        auto_park_route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(auto_park_route)
        train.assigned_route_id = auto_park_route.route_id
        train.current_speed = 0.0
        train.state = TrainState.READY
        controller.ops.state = TrainState.READY

        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "Auto-park route did not dispatch from mid-block to C",
            max_steps=6000,
        )
        self._force_arrival_at_platform(
            simulation,
            auto_park_route,
            train,
            controller,
            "STA_C",
        )

        stop_tile = train.position.head_tile if train.position else None
        assert stop_tile in {(4, 0), (8, 0)}, (
            f"Expected final stop at B or C station tile, got {stop_tile}"
        )


class TestMultiTilePlatformStopping:
    """
    Tests for train stopping at multi-tile platforms.

    BUG: When a path ends at a multi-tile station platform, the train should stop
    at the END of the platform (last tile), not at the first tile. This test
    ensures the train controller correctly calculates clearance to the end of
    the platform regardless of block position ordering.

    Key issue: _calculate_clearance_to_end_of_current_block was checking if there
    were more station tiles after the current one in block_positions, but
    block_positions is in arbitrary insertion order, not travel direction order.
    """

    @pytest.fixture
    def multi_tile_station_network(self, network):
        """Create a network with a 2-tile station on the east branch."""
        from track import TRACK_NS, TRACK_EW, TRACK_NW

        # Create vertical track layout: (9,0) to (9,4)
        # Train will travel NORTH from (9,0) to station at (9,3)-(9,4)
        for y in range(5):
            track = TrackSegment(9, y, track_bits=TRACK_NS)
            network.add_track(track)

        # Create blocks
        network.create_block_with_tracks(
            block_id="BLOCK_START",
            positions=[(9, 0)],
            name="Start Block",
            length_cm=50.0,
        )
        network.create_block_with_tracks(
            block_id="BLOCK_MID",
            positions=[(9, 1), (9, 2)],
            name="Middle Block",
            length_cm=100.0,
        )
        # Multi-tile station block spanning (9,3) and (9,4)
        # Add tracks in reverse order to test insertion order issue
        network.create_block_with_tracks(
            block_id="STATION_BLOCK",
            positions=[(9, 4), (9, 3)],  # Note: reversed order!
            name="East Terminal",
            length_cm=100.0,
        )

        # Create station platform spanning both tiles
        from railway_platform import Platform

        station = Platform(
            platform_id="EAST_TERMINAL",
            name="East Terminal",
            positions=[(9, 3), (9, 4)],
            length_cm=100.0,
            platform_side="both",
        )
        network.stations["EAST_TERMINAL"] = station

        # Mark tracks as having station
        network.get_track(9, 3).station_id = "EAST_TERMINAL"
        network.get_track(9, 4).station_id = "EAST_TERMINAL"

        return network

    def test_clearance_includes_full_multi_tile_station_length(
        self, multi_tile_station_network
    ):
        """
        _calculate_clearance_to_end_of_current_block() should calculate distance
        to the end of the platform, even when block_positions are in arbitrary order.

        This ensures ATO allows the train to travel the full platform length
        before stopping, rather than stopping at the first tile.
        """
        network = multi_tile_station_network

        # Create train at first station tile (9,3), traveling NORTH
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="CLEARANCE_TEST", consist=[car])
        network.add_train(train)

        # Place train at first station tile (9,3) with path to last tile (9,4)
        train.position = TrainPosition(
            end_a_tile=(9, 3),
            end_a_distance_cm=0.0,
            end_a_entry_edge="S",
            end_b_tile=(9, 3),
            end_b_distance_cm=0.0,
            end_b_entry_edge="S",
            leading_end=LeadingEnd.A,
            occupied_tiles=[(9, 3)],
            occupied_blocks=["STATION_BLOCK"],
        )

        controller = TrainController(train_id=train.train_id, network=network)
        controller.power_on = True
        # `_calculate_clearance_to_end_of_current_block` reads `train.position`
        # and the network, not current_speed or current_path — both of those
        # live on TrainController / PathDispatcher in v1.

        # Calculate clearance - should include distance to end of platform
        clearance = controller._calculate_clearance_to_end_of_current_block()

        # Station block is 2 tiles (9,3) and (9,4), each 50cm = 100cm total
        # Train is at start of tile (9,3), so clearance should be ~100cm
        # (remaining in current tile + full next tile)
        assert clearance >= 75.0, (
            f"Clearance should include full platform length (~100cm), "
            f"but got {clearance}cm. "
            f"The train will stop prematurely at the first tile instead of "
            f"continuing to the end of the multi-tile platform. "
            f"Block positions may be in reverse insertion order: "
            f"{network.get_block_positions('STATION_BLOCK')}"
        )

    def test_clearance_calculation_with_reversed_block_positions(
        self, multi_tile_station_network
    ):
        """
        _calculate_clearance_to_end_of_current_block should work correctly
        even when block positions are in reverse insertion order.

        The bug was that the code checked for 'more station tiles after' using
        block_positions index order, but block_positions can be in any order
        depending on insertion sequence.
        """
        network = multi_tile_station_network

        # Create train already in the station block at first tile (9,3)
        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=30.0)
        train = Train(train_id="CLEARANCE_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 9, 3, Direction.NORTH)

        # Manually set position to be at start of tile (9,3)
        train.position = TrainPosition(
            end_a_tile=(9, 3),
            end_a_distance_cm=0.0,
            end_a_entry_edge="S",
            end_b_tile=(9, 3),
            end_b_distance_cm=0.0,
            end_b_entry_edge="S",
            leading_end=LeadingEnd.A,
            occupied_tiles=[(9, 3)],
            occupied_blocks=["STATION_BLOCK"],
        )

        # Create controller
        controller = TrainController(train_id=train.train_id, network=network)

        # Calculate clearance to end of current block
        clearance = controller._calculate_clearance_to_end_of_current_block()

        # Clearance should be approximately 100cm (two 50cm tiles: (9,3) and (9,4))
        # Not just 50cm for the current tile
        assert clearance >= 75.0, (
            f"Clearance should include distance to end of platform (~100cm), "
            f"but got {clearance}cm. "
            f"Block positions were inserted in reverse order [(9,4), (9,3)], "
            f"which may cause the clearance calculation to stop early "
            f"if not properly sorting by travel direction."
        )


@pytest.mark.skip(
    reason=(
        "Stale skip (PR 6 audit): the skip cites a "
        "`PathDispatcher._compute_block_traversals` bug that no longer "
        "reproduces — `create_path` returns success for multi-block "
        "destinations in current code. The actual blocker for these tests "
        "is pre-v1 API usage in the fixtures (e.g. `Waypoint(...)`); the "
        "dispatcher itself is not the blocker. Re-enabling requires "
        "porting fixtures to the v1 RouteTask shape."
    )
)
class TestManualPathAssignmentAfterArrival:
    """
    Comprehensive tests for manual path assignment bug.

    BUG: When a train arrives at a terminal (B) and user manually assigns
    a one-off path to another terminal (C), the ATO GO button flashes
    briefly and returns to STANDBY without the train moving.

    The path appears to be established correctly (debug info and lookahead
    JSON look correct, distance to stop is correct), but the train doesn't move.

    These tests ensure that manual path assignment from a stopped train works
    correctly and the train actually proceeds to the destination.
    """

    def _build_terminal_line_with_switch(self) -> RailwayNetwork:
        """Build A-B-C line with switch at B connecting to branch C.

        Layout:
        A(0,0) --- B(3,0)[Station] --- (4,0)[SWITCH] --- (continue main line)
                                        |
                                      C(4,4)
        """
        from track import TRACK_EW, TRACK_NS, TRACK_NW, TRACK_SE
        from switch import Switch

        network = RailwayNetwork()

        # Create main line: A to switch (0,0 to 4,0)
        for x in range(5):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        # Create switch at (4,0) - connects east-west to north-south
        # Replace the straight EW track at (4,0) with a switch track
        network.remove_track(4, 0)
        switch_track = TrackSegment(4, 0, track_bits=TRACK_EW | TRACK_NW)
        switch_track.switch_state = 0  # Start with EW (main)
        network.add_track(switch_track)

        # Create switch entity
        switch = Switch(switch_id="SW_B", x=4, y=0)
        network.add_switch(switch)
        switch_track.switch_id = switch.switch_id

        # Create branch line from switch northward (4,1) to (4,4)
        for y in range(1, 5):
            network.add_track(TrackSegment(x=4, y=y, track_bits=TRACK_NS))

        # Station A at west end (0,0)
        network.add_station(
            0,
            0,
            station_id="STA_A",
            name="West Terminal",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        # Station B at (3,0) - just before the switch (main terminal)
        network.add_station(
            3,
            0,
            station_id="STA_B",
            name="East Terminal",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        # Station C at end of branch (4,4) - the branch terminal
        network.add_station(
            4,
            4,
            station_id="STA_C",
            name="East Branch Terminal",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        # Create blocks
        # Block A-B (main line from A to before switch)
        # Note: stations create their own blocks automatically (STA_B_BLOCK, STA_C_BLOCK)
        network.create_block_with_tracks(
            block_id="BLK_A_B",
            positions=[(1, 0), (2, 0)],
            name="A to B",
            length_cm=100.0,
        )

        # Block B-C (the branch from switch to C)
        network.create_block_with_tracks(
            block_id="BLK_B_C",
            positions=[(4, 1), (4, 2), (4, 3)],
            name="B to C Branch",
            length_cm=150.0,
        )

        for block in network.blocks.values():
            block.speed_limit_kmh = 60.0

        return network

    def _build_bidirectional_line(self, network: RailwayNetwork) -> None:
        """Build simple A-B-C bidirectional line for route/path testing.

        Layout:
        A(0,0) --- (1,0) --- (2,0) --- B(3,0) --- (4,0) --- (5,0) --- C(6,0)

        All stations and blocks are aligned on a single east-west line.
        Trains can go A→C facing EAST, then turn around and go C→A facing WEST.
        """
        from track import TRACK_EW

        # Create straight line from (0,0) to (6,0)
        for x in range(7):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        # Station A at (0,0) - west terminal
        network.add_station(
            0,
            0,
            station_id="STA_A",
            name="Station A",
            length_cm=50.0,
            top_speed_kmh=80.0,
        )

        # Station B at (3,0) - middle station
        network.add_station(
            3,
            0,
            station_id="STA_B",
            name="Station B",
            length_cm=50.0,
            top_speed_kmh=80.0,
        )

        # Station C at (6,0) - east terminal
        network.add_station(
            6,
            0,
            station_id="STA_C",
            name="Station C",
            length_cm=50.0,
            top_speed_kmh=80.0,
        )

        # Create blocks between stations
        # Block A-B: (1,0), (2,0)
        network.create_block_with_tracks(
            block_id="BLK_A",
            positions=[(1, 0), (2, 0)],
            name="A to B",
            length_cm=100.0,
        )

        # Block B-C: (4,0), (5,0)
        network.create_block_with_tracks(
            block_id="BLK_C",
            positions=[(4, 0), (5, 0)],
            name="B to C",
            length_cm=100.0,
        )

        for block in network.blocks.values():
            block.speed_limit_kmh = 80.0

    def _run_until(
        self,
        simulation: SimulationController,
        condition,
        failure_message: str,
        max_steps: int = 20000,
        dt: float = 0.02,
    ) -> None:
        for _ in range(max_steps):
            simulation.update(dt)
            if condition():
                return
        pytest.fail(failure_message)

    def test_train_arrives_at_b_manual_path_to_c_should_move(self):
        """
        Test the core bug scenario:
        1. Train starts at A, assigned ONE_OFF route to B
        2. Train arrives at B and stops
        3. Manually create a one-off path from B to C
        4. Train should move to C (not flash ATO and return to standby)
        """
        # Setup
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at A
        car = TrainCar(car_id="CAR_BUG_TEST", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id="BUG_TEST_TRAIN", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Setup controller
        train.state = TrainState.READY
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"
        controller.ops.state = TrainState.READY
        assert controller.commands.set_cab("A").success is True

        # Step 1: Assign ONE_OFF route from A to B
        route = Route(route_id="ROUTE_A_TO_B", name="A to B", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_B", dwell_time_sec=1.0))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # Wait for train to get path to B
        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_B_BLOCK"
            ),
            "Train did not get path from A to B",
        )

        # Step 2: Force arrival at B (simulating the train arriving and stopping)
        # Place train at B station (now at 3,0)
        train.position = make_train_position((3, 0), Direction.EAST)
        train.current_speed = 0.0
        train.state = TrainState.READY
        controller.ops.state = TrainState.READY
        controller.ato.running = False

        # Cancel the path from A to B
        if train.assigned_path_id:
            dispatcher.cancel_path(train.assigned_path_id)
        train.assigned_path_id = None
        train.current_path = None
        train.reserved_blocks = []

        # Mark route as completed at B
        exec_state = simulation.route_executor.execution_states.get(train.train_id)
        if exec_state:
            exec_state.current_waypoint_index = 0  # Completed the only waypoint

        # Verify train is at B
        assert simulation.route_executor._is_train_at_platform(train, "STA_B") is True
        assert train.state == TrainState.READY
        assert train.current_speed == 0.0

        # Step 3: Clear the A->B route and manually assign a new path from B to C
        # This simulates the user action in the UI
        old_route = simulation.route_executor.route_registry.get("ROUTE_A_TO_B")
        if old_route:
            old_route.unassign_train(train.train_id)
        train.assigned_route_id = None
        if exec_state:
            simulation.route_executor.execution_states.pop(train.train_id, None)

        # Create new ONE_OFF route from B to C (the bug scenario)
        new_route = Route(
            route_id="ROUTE_B_TO_C", name="B to C", mode=RouteMode.ONE_OFF
        )
        new_route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=1.0))
        new_route.assign_train(train.train_id)
        new_route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(new_route)
        train.assigned_route_id = new_route.route_id

        # IMPORTANT: This is where the bug manifests
        # The train should now get a path from B to C and start moving
        # Instead of ATO flashing and returning to standby

        # Step 4: Verify train gets path to C and actually moves
        self._run_until(
            simulation,
            lambda: (
                train.assigned_path_id is not None
                and dispatcher.get_path(train.assigned_path_id) is not None
                and dispatcher.get_path(train.assigned_path_id).end_block_id
                == "STA_C_BLOCK"
            ),
            "BUG: Train did not get path from B to C after manual assignment. "
            "ATO likely flashed and returned to standby without moving.",
        )

        # Verify the path is valid
        path = dispatcher.get_path(train.assigned_path_id)
        assert path is not None
        assert path.start_block_id in ["STA_B_BLOCK", "BLK_B_C"]
        assert path.end_block_id == "STA_C_BLOCK"

        # Verify train has a valid state after path assignment
        # The train may be RUNNING_AT_STATION (just arrived) or transitioning
        assert train.state in [
            TrainState.RUNNING_MOVING,
            TrainState.READY,
            TrainState.RUNNING_AT_STATION,
        ], (
            f"BUG: Train state is {train.state}, expected valid state after path assignment. "
            f"Path was assigned but train is in unexpected state."
        )

        # Verify ATO is running or ready
        # Note: ATO may not be running yet if train is still in station state
        assert controller.ops.state in [
            TrainState.RUNNING_MOVING,
            TrainState.READY,
            TrainState.RUNNING_AT_STATION,
        ], (
            "BUG: Controller in unexpected state after path assignment. "
            f"State: {controller.ops.state}"
        )

    def test_manual_path_b_to_c_train_actually_arrives_at_c(self):
        """
        Extended test: Actually run the simulation and verify train arrives at C.

        This ensures the full end-to-end flow works:
        1. Train at B with manual B->C route
        2. Route executor acquires path and calls depart()
        3. Train departs B (ATO doesn't falsely stop at departure station)
        4. Train arrives at C

        BUG: _is_at_station() returned True at the departure station on the
        first tick after depart() because the train had speed=0 and no route-
        based waypoint filtering applied when ato_destination_block_id was set.

        FIX: _is_at_station() now checks ato_destination_block_id and only
        returns True when the train is at the destination block.
        """
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train already at B (simulating arrival at terminal)
        car = TrainCar(car_id="CAR_AT_B", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id="TRAIN_AT_B", consist=[car], max_speed=60.0)
        network.add_train(train)

        # Place train at B station (3,0), facing east
        network.place_train(train, 3, 0, Direction.EAST)
        train.current_speed = 0.0
        train.state = TrainState.READY

        # Setup controller
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"
        controller.ops.state = TrainState.READY
        assert controller.commands.set_cab("A").success is True

        # Assign B->C route (simulating UI action)
        route = Route(
            route_id="MANUAL_B_TO_C", name="Manual B to C", mode=RouteMode.ONE_OFF
        )
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=1.0))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # Run simulation - route executor should:
        # 1. See STANDBY train with assigned route
        # 2. Create path B->C via dispatcher
        # 3. Set ato_destination_block_id
        # 4. Call depart()
        # 5. Train should start moving (not falsely stop at departure station)

        # Wait for train to get path and start moving
        self._run_until(
            simulation,
            lambda: train.state == TrainState.RUNNING_MOVING and controller.ato.running,
            "BUG: Train did not start moving after path B->C assignment. "
            f"State: {train.state}, ato_running: {controller.ato.running}, "
            f"ato_destination_block_id: {controller.path_projection.ato_destination_block_id}",
            max_steps=500,
        )

        # Verify ato_destination_block_id is set
        assert controller.path_projection.ato_destination_block_id == "STA_C_BLOCK", (
            f"ato_destination_block_id should be STA_C_BLOCK, "
            f"got {controller.path_projection.ato_destination_block_id}"
        )

        # Continue until train arrives at C
        self._run_until(
            simulation,
            lambda: simulation.route_executor._is_train_at_platform(train, "STA_C"),
            "BUG: Train did not arrive at C after manual path assignment. "
            f"State: {train.state}, speed: {train.current_speed}, "
            f"position: {getattr(train.position, 'head_tile', None)}",
            max_steps=20000,
        )

        # Verify train is at C
        assert simulation.route_executor._is_train_at_platform(train, "STA_C") is True

    def test_path_assignment_preserves_correct_lookahead(self):
        """
        Test that manual path assignment results in correct lookahead info.

        The bug description mentioned that debug info and lookahead JSON
        looked correct, but train still didn't move. This test verifies
        the lookahead calculation is correct after manual path assignment.
        """
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at B (now at 3,0)
        car = TrainCar(car_id="CAR_LOOKAHEAD", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id="LOOKAHEAD_TEST", consist=[car], max_speed=60.0)
        network.add_train(train)
        train.position = make_train_position((3, 0), Direction.EAST)
        train.current_speed = 0.0
        train.state = TrainState.READY

        # Setup controller
        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"
        controller.ops.state = TrainState.READY

        # Manually assign B->C route
        route = Route(
            route_id="LOOKAHEAD_ROUTE", name="Lookahead Test", mode=RouteMode.ONE_OFF
        )
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=1.0))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # Run a few updates to let route executor process
        for _ in range(100):
            simulation.update(0.02)

        # Check if path was assigned - if not, the bug is reproduced
        if train.assigned_path_id is None:
            pytest.fail(
                "BUG: Lookahead test failed - path was not assigned. "
                "Train at B with route to C did not get a path."
            )

        # Get lookahead info - use the controller's lookahead attribute
        lookahead = controller.lookahead

        # Verify lookahead shows correct destination
        assert lookahead is not None, (
            "Lookahead should not be None after path assignment"
        )
        assert len(lookahead) > 0, "Lookahead should contain lookahead data"

        # The lookahead should show the path to C
        # Check that we have blocks in the lookahead
        block_ids_in_lookahead = [pt.block_id for pt in lookahead if pt.block_id]
        assert len(block_ids_in_lookahead) > 0, (
            "BUG: Lookahead contains no block IDs. "
            "Path may not be properly established."
        )

        # Verify the destination block is in lookahead
        assert (
            "STA_C_BLOCK" in block_ids_in_lookahead
            or "BLK_B_C" in block_ids_in_lookahead
        ), (
            f"BUG: Lookahead should contain path to C. Got blocks: {block_ids_in_lookahead}"
        )

        print(
            f"✓ Lookahead correct: contains {len(lookahead)} points, blocks: {block_ids_in_lookahead}"
        )

    def test_consecutive_manual_paths_a_to_b_then_b_to_c(self):
        """
        Test two consecutive manual path assignments:
        1. Assign A->B route, train departs A and arrives at B
        2. Assign B->C route, train departs B and arrives at C

        This simulates the exact workflow that triggers the bug:
        Train runs ONE_OFF A->B, arrives at B, user assigns B->C.

        BUG: On the B->C leg, _is_at_station() falsely detected arrival at B
        (the departure station) on the first tick because speed=0 and no
        ato_destination_block_id check was applied.

        FIX: _is_at_station() now returns False at non-destination stations
        when ato_destination_block_id is set.
        """
        pytest.skip("v1: RUNNING_WAITING/MANUAL state removed")
        network = self._build_terminal_line_with_switch()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at A (short train to fit on track)
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

        # Create and activate route A->C
        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # Run until train is moving
        for _ in range(500):
            simulation.update(0.1)
            if train.state == TrainState.RUNNING_MOVING and controller.ato.running:
                break

        assert controller.ato.running is True, "Train should be running with ATO"
        assert train.current_speed > 0, "Train should be moving"
        old_destination = controller.path_projection.ato_destination_block_id
        assert old_destination is not None, "Train should have a destination"

        # Cancel route (simulate cancel button)
        # This mimics the API call: PUT /api/trains/<train_id>/assign-route with route_id=None
        if train.assigned_route_id:
            old_route = simulation.route_executor.route_registry.get(train.assigned_route_id)
            if old_route:
                old_route.unassign_train(train.train_id)
        train.assigned_route_id = None

        # Apply the fix: Stop ATO and clear destination
        if controller.ato.running:
            controller.ato.stop()
        train.ato_destination_block_id = None

        # Clear route executor state
        if simulation.route_executor:
            simulation.route_executor.execution_states.pop(train.train_id, None)

        # Verify fix
        assert controller.ato.running is False, (
            "BUG: ATO should stop after route cancellation"
        )
        assert controller.path_projection.ato_destination_block_id is None, (
            "BUG: Destination should be cleared"
        )
        assert train.ato_destination_block_id is None, (
            "BUG: Train destination should be cleared"
        )

        # Run a bit more - train should brake to stop
        for _ in range(1000):
            simulation.update(0.1)
            if train.current_speed <= 0.5:
                break

        # Train should eventually transition to STANDBY after stopping
        for _ in range(100):
            simulation.update(0.1)
            if train.state == TrainState.READY:
                break

        assert train.state in (TrainState.READY, TrainState.RUNNING_WAITING), (
            f"Train should stop after ATO cancel, got {train.state}"
        )

    def test_route_cancellation_edge_case_at_station(self):
        """Edge case: Cancel route when train is at station."""
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

        # Create route
        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # Train is at station A, cancel immediately
        train.assigned_route_id = None
        if controller.ato.running:
            controller.ato.stop()
        train.ato_destination_block_id = None

        assert controller.ato.running is False
        assert controller.path_projection.ato_destination_block_id is None

    def test_route_cancellation_clears_path_id(self):
        """Ensure cancellation clears both destination AND path ID."""
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

        # Set some path state
        train.ato_destination_block_id = "BLK_C"

        # Cancel
        if controller.ato.running:
            controller.ato.stop()
        train.ato_destination_block_id = None

        assert controller.path_projection.ato_path_id is None, "Path ID should be cleared"
        assert controller.path_projection.ato_destination_block_id is None

    def test_backward_path_assignment_auto_turns_train(self):
        """
        BUG 2 test: Assigning backward path should auto-turn train.

        Steps:
        1. Train at C facing EAST (reached terminal)
        2. User assigns path C->A (requires WEST direction)
        3. System should auto-turn train to face WEST
        4. Train should then be able to follow the path

        Expected:
        - Train facing direction changes to WEST
        - Active cab switches A <-> B
        - Train can depart toward A
        """
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Create train at C facing EAST (as if it just arrived from the west)
        # But we need to place it so there's track ahead, so place at (5,0) facing EAST
        # This puts the train approaching/at station C
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 5, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")
        controller.commands.set_cab("A")

        assert train.facing_direction == Direction.EAST, (
            "Train should face EAST initially"
        )
        assert controller.commands.cab == "A", "Cab A should be active"

        # Manually create backward path C->A (this simulates path tool usage)
        # Path should go from (6,0) to (0,0) - heading WEST
        path_result = dispatcher.create_path(
            train_id=train.train_id,
            requested_destination_block_id="STA_A_BLOCK",
        )

        assert path_result.success, f"Path creation failed: {path_result.error}"

        # Convert path to train format
        from path_utils import convert_tiles_to_train_path

        raw_path = convert_tiles_to_train_path(path_result.path.get_path_tiles())
        train.current_path = raw_path
        train.assigned_path_id = path_result.path.path_id

        # Set destination
        train.ato_destination_block_id = "STA_A_BLOCK"

        # Apply the fix: Auto-turn if path requires opposite direction
        if train.position and train.current_path and len(train.current_path) >= 2:
            first_tile = train.current_path[0]
            second_tile = train.current_path[1]
            dx = second_tile[0] - first_tile[0]
            dy = second_tile[1] - first_tile[1]

            # Determine path direction
            path_direction = None
            if dx > 0:
                path_direction = Direction.EAST
            elif dx < 0:
                path_direction = Direction.WEST
            elif dy > 0:
                path_direction = Direction.NORTH
            elif dy < 0:
                path_direction = Direction.SOUTH

            # If path goes opposite to facing, turn around
            if (
                path_direction is not None
                and path_direction == train.facing_direction.opposite()
                and train.is_stopped()
            ):
                opposite = "B" if controller.commands.cab == "A" else "A"
                result = controller.commands.set_cab(opposite)
                assert result.success, (
                    f"set_cab({opposite}) failed! "
                    f"train.state={train.state}, controller.ops.state={controller.ops.state}, "
                    f"active_cab={controller.commands.cab}, ato_running={controller.ato.running}, "
                    f"speed={train.current_speed}"
                )

        # Verify fix
        assert train.facing_direction == Direction.WEST, (
            f"BUG: Train should auto-turn to WEST for backward path, got {train.facing_direction}"
        )
        assert controller.commands.cab == "B", (
            f"BUG: Cab should switch to B after turn, got {controller.commands.cab}"
        )

    def test_backward_path_no_turn_if_already_correct(self):
        """Don't turn if train already faces the right direction."""
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Train at A facing EAST (correct for A->C path)
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")
        controller.commands.set_cab("A")

        # Create forward path A->C
        path_result = dispatcher.create_path(
            train_id=train.train_id,
            requested_destination_block_id="BLK_C",
        )

        from path_utils import convert_tiles_to_train_path

        raw_path = convert_tiles_to_train_path(path_result.path.get_path_tiles())
        train.current_path = raw_path

        # Check if turn is needed (should NOT turn)
        if train.position and train.current_path and len(train.current_path) >= 2:
            first_tile = train.current_path[0]
            second_tile = train.current_path[1]
            dx = second_tile[0] - first_tile[0]

            path_direction = None
            if dx > 0:
                path_direction = Direction.EAST
            elif dx < 0:
                path_direction = Direction.WEST

            # Should NOT turn (path and facing both EAST)
            if (
                path_direction is not None
                and path_direction == train.facing_direction.opposite()
                and train.is_stopped()
            ):
                opposite = "B" if controller.commands.cab == "A" else "A"
                controller.commands.set_cab(opposite)

        # Should still face EAST
        assert train.facing_direction == Direction.EAST, (
            "Train should not turn for forward path"
        )
        assert controller.commands.cab == "A", "Cab should remain A"

    def test_backward_path_real_world_scenario(self):
        """
        Real-world scenario: Train reaches terminal C, user assigns path back to A.

        This is the exact scenario the user described:
        - Train finishes route to branch terminal
        - User uses path tool to assign return path
        - Train should automatically turn around and depart
        """
        network = RailwayNetwork()
        self._build_bidirectional_line(network)

        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # 1. Train starts at A with route to C
        car = TrainCar(
            car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=40.0, max_speed=80.0
        )
        train = Train(train_id="TRAIN_1", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.commands.power_on_cmd()
        controller.commands.set_cab("A")

        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # 2. Run until train reaches C
        for _ in range(10000):
            simulation.update(0.1)
            if simulation.route_executor._is_train_at_platform(train, "STA_C"):
                break

        assert simulation.route_executor._is_train_at_platform(train, "STA_C"), (
            "Train should reach C"
        )
        assert train.facing_direction == Direction.EAST, (
            "Train should still face EAST at terminal"
        )

        # 3. User cancels route and assigns return path
        train.assigned_route_id = None
        if controller.ato.running:
            controller.ato.stop()
        train.ato_destination_block_id = None

        # Wait for train to stop
        for _ in range(500):
            simulation.update(0.1)
            if train.is_stopped():
                break

        assert train.is_stopped(), "Train should be stopped"

        # Assign backward path C->A
        path_result = dispatcher.create_path(
            train_id=train.train_id,
            requested_destination_block_id="STA_A_BLOCK",
        )

        from path_utils import convert_tiles_to_train_path

        raw_path = convert_tiles_to_train_path(path_result.path.get_path_tiles())
        train.current_path = raw_path
        train.assigned_path_id = path_result.path.path_id
        train.ato_destination_block_id = "STA_A_BLOCK"

        # Auto-turn logic
        if train.position and train.current_path and len(train.current_path) >= 2:
            first_tile = train.current_path[0]
            second_tile = train.current_path[1]
            dx = second_tile[0] - first_tile[0]
            dy = second_tile[1] - first_tile[1]

            path_direction = None
            if dx < 0:
                path_direction = Direction.WEST
            elif dx > 0:
                path_direction = Direction.EAST
            elif dy > 0:
                path_direction = Direction.NORTH
            elif dy < 0:
                path_direction = Direction.SOUTH

            if (
                path_direction is not None
                and path_direction == train.facing_direction.opposite()
                and train.is_stopped()
            ):
                opposite = "B" if controller.commands.cab == "A" else "A"
                result = controller.commands.set_cab(opposite)
                assert result.success, (
                    f"set_cab({opposite}) failed! "
                    f"train.state={train.state}, controller.ops.state={controller.ops.state}, "
                    f"active_cab={controller.commands.cab}, ato_running={controller.ato.running}, "
                    f"speed={train.current_speed}, facing={train.facing_direction}"
                )

        # 4. Verify train turned and can depart
        assert train.facing_direction == Direction.WEST, (
            "BUG: Train should turn WEST for return path"
        )

        # After turning, ensure cab A is selected to match the new facing.
        controller.commands.set_cab("A")

        # Start ATO
        controller.ato.go()

        # Run a bit - train should start moving
        for _ in range(500):
            simulation.update(0.1)
            if train.current_speed > 0:
                break

        assert train.current_speed > 0, "BUG: Train should be able to move after turn"
        assert controller.ato.running is True, "BUG: ATO should be running"


        # OLD: Button disabled (!active_cab) - BUG
        # NEW: Button enabled (power_on) - CORRECT, backend auto-selects cab

        # This test documents the data contract for the frontend


@pytest.mark.skip(
    reason=(
        "Stale skip (PR 6 audit): the skip cites a "
        "`PathDispatcher._compute_block_traversals` bug that no longer "
        "reproduces — `create_path` succeeds for the destination blocks "
        "these tests target. The actual blocker is pre-v1 API usage in the "
        "fixtures; re-enabling requires porting them to the v1 RouteTask "
        "shape and the new auto-reversal contract on `Route.mode == "
        "ROUND_TRIP`."
    )
)
class TestAutoReversalOnNewRouteAssignment:
    """
    Tests for auto-reversal when assigning a new route after a train has
    completed its previous route at a terminal.

    Expected behaviour: when a train completes a ONE_OFF route to a terminal
    (e.g. eastbound to station C) and the user assigns a new route going the
    other way (e.g. westbound to station A), the system should automatically:
      1. Handle IDLE state in _execute_route_for_train() (same as STANDBY)
      2. Request a path and infer the required direction
      3. Switch cabs (auto-reverse) via ato_go() to match the path direction
      4. Depart and move toward the new destination

    Fix: route_executor.py now dispatches IDLE to _try_depart_to_waypoint(),
    and depart() delegates to ato_go() which handles cab selection for both
    IDLE and STANDBY states.

    Note: test_backward_path_assignment_auto_turns_train and related tests in
    TestManualPathAssignmentAfterArrival call controller.turn_around() manually
    in the test body — those tests cover a different workflow (user-initiated
    reversal) and do not test the automatic reversal path exercised here.
    """

    def _build_three_station_line(self) -> RailwayNetwork:
        """Build A(0,0)--B(4,0)--C(8,0) line with blocks and stations."""
        network = RailwayNetwork()
        for x in range(9):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        network.add_station(
            0,
            0,
            station_id="STA_A",
            name="A",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )
        network.add_station(
            4,
            0,
            station_id="STA_B",
            name="B",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )
        network.add_station(
            8,
            0,
            station_id="STA_C",
            name="C",
            length_cm=50.0,
            top_speed_kmh=60.0,
        )

        network.create_block_with_tracks(
            block_id="BLK_AB",
            positions=[(1, 0), (2, 0), (3, 0)],
            name="A-B",
            length_cm=150.0,
        )
        network.create_block_with_tracks(
            block_id="BLK_BC",
            positions=[(5, 0), (6, 0), (7, 0)],
            name="B-C",
            length_cm=150.0,
        )

        for block in network.blocks.values():
            block.speed_limit_kmh = 60.0
        return network

    def _run_until(
        self,
        simulation: SimulationController,
        condition,
        failure_message: str,
        max_steps: int = 20000,
        dt: float = 0.02,
    ) -> None:
        for _ in range(max_steps):
            simulation.update(dt)
            if condition():
                return
        pytest.fail(failure_message)

    def test_idle_train_with_facing_gets_backward_route_should_reverse_and_depart(self):
        """
        Isolated IDLE state test: Train at C, facing EAST, in IDLE with cab A.
        Assign new backward route C->A. System should auto-reverse and depart.

        This directly tests the state machine gap: _execute_route_for_train()
        does not handle IDLE state, so the train stays stuck.
        """
        network = self._build_three_station_line()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Place train at C(8,0) facing WEST (place_train needs valid neighbor)
        car = TrainCar(car_id="CAR_REV", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id="T_REV", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 7, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"

        # Simulate post-route-completion: IDLE, facing EAST, cab A.
        # depart_from_station(has_more_waypoints=False) sets state=IDLE
        # but does NOT clear active_cab or leading_end, so the train
        # retains its cab and facing direction.
        controller.commands.cab = "A"
        train.active_cab = "A"
        train.state = TrainState.READY
        controller.ops.state = TrainState.READY

        # Verify preconditions
        assert train.facing_direction == Direction.EAST
        assert train.state == TrainState.READY
        assert controller.commands.cab == "A"

        # Assign new backward route to A
        route = Route(route_id="ROUTE_CA", name="C to A", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_A", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # System should: IDLE->STANDBY, create path, auto-reverse cab, depart
        self._run_until(
            simulation,
            lambda: (
                train.state == TrainState.RUNNING_MOVING
                and train.facing_direction == Direction.WEST
            ),
            f"BUG: Train did not auto-reverse and depart westbound from IDLE. "
            f"State={train.state}, facing={train.facing_direction}, "
            f"cab={controller.commands.cab}, path={train.assigned_path_id}",
            max_steps=5000,
        )

        assert controller.commands.cab == "B", (
            f"Expected cab B for westbound travel, got {controller.commands.cab}"
        )
        assert controller.ato.running is True, "ATO should be running"

    def test_idle_train_no_cab_gets_forward_route_should_depart(self):
        """
        Train in IDLE with no cab and no facing_direction (transition_to_idle
        clears these). Assign forward route. System should auto-select cab
        and depart.
        """
        network = self._build_three_station_line()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        car = TrainCar(car_id="CAR_FWD", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id="T_FWD", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"

        # Simulate transition_to_idle(): clears cab and leading_end
        train.state = TrainState.READY
        controller.ops.state = TrainState.READY
        controller.commands.cab = None
        train.active_cab = None
        if train.position:
            train.position.leading_end = None

        assert train.facing_direction is None
        assert train.state == TrainState.READY

        # Assign forward route A->C
        route = Route(route_id="ROUTE_AC", name="A to C", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_C", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # System should auto-select cab from path direction and depart
        self._run_until(
            simulation,
            lambda: (
                train.state == TrainState.RUNNING_MOVING
                and train.facing_direction is not None
            ),
            f"BUG: Train did not depart from IDLE with no cab. "
            f"State={train.state}, facing={train.facing_direction}, "
            f"cab={controller.commands.cab}",
            max_steps=5000,
        )

        assert train.facing_direction == Direction.EAST, (
            f"Expected EAST, got {train.facing_direction}"
        )

    def test_idle_train_no_cab_gets_backward_route_at_terminal(self):
        """
        Train at terminal C(7,0), IDLE, no cab, no facing_direction.
        Assign route C->A. System should auto-determine WEST direction
        from path and depart.
        """
        network = self._build_three_station_line()
        dispatcher = PathDispatcher(network)
        simulation = SimulationController(network, dispatcher)

        # Place at (7,0) facing WEST so there's a track to connect
        car = TrainCar(car_id="CAR_NF", car_type="LOCOMOTIVE", length_cm=45.0)
        train = Train(train_id="T_NF", consist=[car], max_speed=60.0)
        network.add_train(train)
        network.place_train(train, 7, 0, Direction.WEST)

        controller = simulation._get_or_create_controller(train)
        controller.power_on = True
        controller.commands.control_mode = "ato"

        # Simulate transition_to_idle(): clears everything
        train.state = TrainState.READY
        controller.ops.state = TrainState.READY
        controller.commands.cab = None
        train.active_cab = None
        if train.position:
            train.position.leading_end = None

        assert train.facing_direction is None
        assert train.state == TrainState.READY

        # Assign route to A (westbound)
        route = Route(route_id="ROUTE_CA", name="C to A", mode=RouteMode.ONE_OFF)
        route.add_waypoint(Waypoint(platform_id="STA_A", dwell_time_sec=0.5))
        route.assign_train(train.train_id)
        route.activate(simulation.simulation_time)
        simulation.route_executor.register_route(route)
        train.assigned_route_id = route.route_id

        # System should auto-determine direction from path and depart
        self._run_until(
            simulation,
            lambda: (
                train.state == TrainState.RUNNING_MOVING
                and train.facing_direction is not None
            ),
            f"BUG: Train with no facing_direction did not depart. "
            f"State={train.state}, facing={train.facing_direction}, "
            f"cab={controller.commands.cab}",
            max_steps=5000,
        )

        assert train.facing_direction == Direction.WEST, (
            f"Expected WEST, got {train.facing_direction}"
        )


if __name__ == "__main__":
    print("Run tests with: pytest tests/test_trains.py -v")
