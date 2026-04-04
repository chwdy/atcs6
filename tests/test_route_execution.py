"""
Tests for Phase 4 - Route Execution

This module tests:
- Route and Waypoint functionality
- RouteExecutor class
- Route mode execution (ONE_WAY, CIRCULAR, ROUND_TRIP)
- Dwell time logic with timing modes
- Waypoint arrival detection
"""

import pytest
from train_route import Route, Waypoint, TimingMode, RouteMode
from route_executor import RouteExecutor, RouteExecutionState
from train import Train
from tests.train_test_helpers import make_train_position
from network import RailwayNetwork
from railway_platform import Platform
from enums import Direction, TrainState


class TestRouteAndWaypoint:
    """Tests for Route and Waypoint classes."""

    @pytest.fixture
    def simple_route(self):
        """Create a simple route."""
        route = Route(
            route_id="ROUTE_001",
            name="Test Route",
            mode=RouteMode.ONE_OFF,
            waypoints=[
                Waypoint(platform_id="STA_A", dwell_time_sec=10.0),
                Waypoint(platform_id="STA_B", dwell_time_sec=15.0),
                Waypoint(platform_id="STA_C", dwell_time_sec=5.0),
            ],
        )
        return route

    def test_route_creation(self, simple_route):
        """Test creating a route."""
        assert simple_route.route_id == "ROUTE_001"
        assert simple_route.mode.value == "one_off"
        assert len(simple_route.waypoints) == 3
        assert simple_route.is_active is False

    def test_route_activation(self, simple_route):
        """Test activating a route."""
        simple_route.activate(100.0)
        assert simple_route.is_active is True
        assert simple_route.started_at == 100.0
        assert simple_route.current_waypoint_index == 0
        assert simple_route.direction == 1

    def test_get_current_waypoint(self, simple_route):
        """Test getting current waypoint."""
        waypoint = simple_route.get_current_waypoint()
        assert waypoint is not None
        assert waypoint.platform_id == "STA_A"

    def test_advance_to_next_waypoint_one_way(self, simple_route):
        """Test advancing waypoints in ONE_WAY mode."""
        # Start at waypoint 0 (STA_A)
        assert simple_route.current_waypoint_index == 0

        # Advance to waypoint 1 (STA_B)
        next_wp = simple_route.advance_to_next_waypoint()
        assert next_wp is not None
        assert next_wp.platform_id == "STA_B"
        assert simple_route.current_waypoint_index == 1

        # Advance to waypoint 2 (STA_C)
        next_wp = simple_route.advance_to_next_waypoint()
        assert next_wp is not None
        assert next_wp.platform_id == "STA_C"
        assert simple_route.current_waypoint_index == 2

        # Advance past end (route complete)
        next_wp = simple_route.advance_to_next_waypoint()
        assert next_wp is None
        assert simple_route.current_waypoint_index == 3

    def test_advance_to_next_waypoint_loop(self):
        """Test advancing waypoints in LOOP mode."""
        route = Route(
            route_id="ROUTE_LOOP",
            mode=RouteMode.CIRCULAR,
            waypoints=[
                Waypoint(platform_id="STA_A"),
                Waypoint(platform_id="STA_B"),
                Waypoint(platform_id="STA_C"),
            ],
        )

        # Advance through waypoints
        assert route.current_waypoint_index == 0

        route.advance_to_next_waypoint()  # -> STA_B (index 1)
        assert route.current_waypoint_index == 1

        route.advance_to_next_waypoint()  # -> STA_C (index 2)
        assert route.current_waypoint_index == 2

        # Loop back to start
        next_wp = route.advance_to_next_waypoint()  # -> STA_A (index 0)
        assert next_wp is not None
        assert next_wp.platform_id == "STA_A"
        assert route.current_waypoint_index == 0

    def test_advance_to_next_waypoint_round_trip(self):
        """Test advancing waypoints in ROUND_TRIP mode."""
        route = Route(
            route_id="ROUTE_RT",
            mode=RouteMode.ROUND_TRIP,
            waypoints=[
                Waypoint(platform_id="STA_A"),
                Waypoint(platform_id="STA_B"),
                Waypoint(platform_id="STA_C"),
            ],
        )

        # Forward: A -> B -> C
        assert route.current_waypoint_index == 0
        assert route.direction == 1

        route.advance_to_next_waypoint()  # -> STA_B (index 1)
        assert route.current_waypoint_index == 1

        route.advance_to_next_waypoint()  # -> STA_C (index 2)
        assert route.current_waypoint_index == 2

        # Bounce back: C -> B
        route.advance_to_next_waypoint()
        assert route.current_waypoint_index == 1
        assert route.direction == -1  # Now going reverse

        # Continue reverse: B -> A
        route.advance_to_next_waypoint()
        assert route.current_waypoint_index == 0
        assert route.direction == -1

        # Bounce forward again: A -> B
        route.advance_to_next_waypoint()
        assert route.current_waypoint_index == 1
        assert route.direction == 1  # Back to forward

    def test_waypoint_dwell_time_none_mode(self):
        """Test waypoint dwell time with NONE timing mode."""
        waypoint = Waypoint(
            platform_id="STA_A", dwell_time_sec=30.0, timing_mode=TimingMode.NONE
        )

        # Arrived 10 seconds ago - should not depart yet
        should_depart = waypoint.should_depart(
            time_at_platform=10.0, route_start_time=0.0, simulation_time=10.0
        )
        assert should_depart is False

        # Arrived 30 seconds ago - should depart
        should_depart = waypoint.should_depart(
            time_at_platform=30.0, route_start_time=0.0, simulation_time=30.0
        )
        assert should_depart is True

    def test_waypoint_dwell_time_relative_mode(self):
        """Test waypoint dwell time with RELATIVE timing mode."""
        waypoint = Waypoint(
            platform_id="STA_A",
            dwell_time_sec=10.0,
            timing_mode=TimingMode.RELATIVE,
            target_time=60.0,  # Depart 60 seconds after route start
        )

        # Route started at time 100, now at time 140 (40 sec elapsed)
        should_depart = waypoint.should_depart(
            time_at_platform=5.0, route_start_time=100.0, simulation_time=140.0
        )
        assert should_depart is False  # Only 40 sec since route start

        # Now at time 160 (60 sec elapsed)
        should_depart = waypoint.should_depart(
            time_at_platform=25.0, route_start_time=100.0, simulation_time=160.0
        )
        assert should_depart is True

    def test_waypoint_dwell_time_absolute_mode(self):
        """Test waypoint dwell time with ABSOLUTE timing mode."""
        waypoint = Waypoint(
            platform_id="STA_A",
            dwell_time_sec=10.0,
            timing_mode=TimingMode.ABSOLUTE,
            target_time=1000.0,  # Depart at simulation time 1000
        )

        # Current simulation time 900 - too early
        should_depart = waypoint.should_depart(
            time_at_platform=20.0, route_start_time=0.0, simulation_time=900.0
        )
        assert should_depart is False

        # Current simulation time 1000 - time to depart
        should_depart = waypoint.should_depart(
            time_at_platform=120.0, route_start_time=0.0, simulation_time=1000.0
        )
        assert should_depart is True

    def test_route_to_dict(self, simple_route):
        """Test route serialization."""
        data = simple_route.to_dict()

        assert data["route_id"] == "ROUTE_001"
        assert data["name"] == "Test Route"
        assert data["mode"] == "one_off"
        assert len(data["waypoints"]) == 3
        assert data["assigned_train_ids"] == []
        assert data["is_active"] is False

    def test_route_from_dict(self, simple_route):
        """Test route deserialization."""
        data = simple_route.to_dict()
        restored = Route.from_dict(data)

        assert restored.route_id == simple_route.route_id
        assert restored.name == simple_route.name
        assert restored.mode == simple_route.mode
        assert len(restored.waypoints) == len(simple_route.waypoints)

    def test_waypoint_to_dict(self):
        """Test waypoint serialization."""
        waypoint = Waypoint(
            platform_id="STA_A",
            dwell_time_sec=45.0,
            timing_mode=TimingMode.RELATIVE,
            target_time=120.0,
        )

        data = waypoint.to_dict()
        assert data["platform_id"] == "STA_A"
        assert data["dwell_time_sec"] == 45.0
        assert data["timing_mode"] == "relative"
        assert data["target_time"] == 120.0

    def test_waypoint_from_dict(self):
        """Test waypoint deserialization."""
        data = {
            "platform_id": "STA_B",
            "dwell_time_sec": 60.0,
            "timing_mode": "absolute",
            "target_time": 500.0,
        }

        waypoint = Waypoint.from_dict(data)
        assert waypoint.platform_id == "STA_B"
        assert waypoint.dwell_time_sec == 60.0
        assert waypoint.timing_mode == TimingMode.ABSOLUTE
        assert waypoint.target_time == 500.0

    def test_route_error_handling(self, simple_route):
        """Test route error tracking."""
        # No error initially
        assert simple_route.last_error is None
        assert simple_route.error_time is None

        # Set error
        simple_route.set_error("Path not found", 123.45)
        assert simple_route.last_error == "Path not found"
        assert simple_route.error_time == 123.45

        # Clear error
        simple_route.clear_error()
        assert simple_route.last_error is None
        assert simple_route.error_time is None


class TestRouteExecutor:
    """Tests for RouteExecutor class."""

    @pytest.fixture
    def simple_network(self):
        """Create a simple network for testing."""
        network = RailwayNetwork()

        # Add stations
        station_a = Platform(platform_id="STA_A", name="Station A", positions=[(0, 0)])
        station_b = Platform(platform_id="STA_B", name="Station B", positions=[(1, 0)])
        station_c = Platform(platform_id="STA_C", name="Station C", positions=[(2, 0)])

        network.stations["STA_A"] = station_a
        network.stations["STA_B"] = station_b
        network.stations["STA_C"] = station_c

        return network

    @pytest.fixture
    def simple_route(self):
        """Create a simple route."""
        route = Route(
            route_id="ROUTE_001",
            name="Test Route",
            mode=RouteMode.ONE_OFF,
            waypoints=[
                Waypoint(platform_id="STA_A", dwell_time_sec=10.0),
                Waypoint(platform_id="STA_B", dwell_time_sec=15.0),
                Waypoint(platform_id="STA_C", dwell_time_sec=5.0),
            ],
        )
        return route

    def test_route_executor_creation(self, simple_network):
        """Test creating a route executor."""
        executor = RouteExecutor(simple_network)
        assert executor.network == simple_network
        assert len(executor.execution_states) == 0
        assert executor.simulation_time == 0.0

    def test_route_executor_is_train_at_station(self, simple_network):
        """Test checking if train is at station."""
        executor = RouteExecutor(simple_network)
        train = Train(train_id="T001")
        train.position = make_train_position((0, 0), Direction.EAST)

        # Train is at STA_A
        assert executor._is_train_at_station(train, "STA_A") is True

        # Train is not at STA_B
        assert executor._is_train_at_station(train, "STA_B") is False

        # Move train to STA_B
        train.position = make_train_position((1, 0), Direction.EAST)
        assert executor._is_train_at_station(train, "STA_B") is True

    def test_execution_state_dataclass(self):
        """Test RouteExecutionState dataclass."""
        state = RouteExecutionState(train_id="T001", route_id="R001")

        assert state.train_id == "T001"
        assert state.route_id == "R001"
        assert state.arrived_at_platform_time is None
        assert state.waiting_for_path is False
        assert state.last_waypoint_platform_id is None


class TestRouteExecutorCompatibilityBugFixes:
    """
    Regression tests for route assignment compatibility fixes.

    BUG 1: _get_active_route_train_pairs() returned early when it found any
    route.assigned_train_ids pair, which skipped legacy train.assigned_route_id
    assignments in mixed deployments.

    BUG 2: deactivate_route() deleted owned controllers and then called
    _get_controller(), which recreated a controller entry immediately.
    """

    @pytest.fixture
    def simple_network(self):
        """Create a simple network for testing."""
        network = RailwayNetwork()
        network.stations["STA_A"] = Platform(
            platform_id="STA_A", name="Station A", positions=[(0, 0)]
        )
        network.stations["STA_B"] = Platform(
            platform_id="STA_B", name="Station B", positions=[(1, 0)]
        )
        return network

    def test_get_active_pairs_merges_new_and_legacy_assignments(self, simple_network):
        """Mixed assignment styles should both contribute active train/route pairs."""
        executor = RouteExecutor(simple_network)

        route_new = Route(
            route_id="ROUTE_NEW",
            name="New-style",
            waypoints=[Waypoint(platform_id="STA_A")],
        )
        route_legacy = Route(
            route_id="ROUTE_LEGACY",
            name="Legacy-style",
            waypoints=[Waypoint(platform_id="STA_B")],
        )
        route_new.activate(0.0)
        route_legacy.activate(0.0)
        route_new.assign_train("T_NEW")

        train_new = Train(train_id="T_NEW")
        train_legacy = Train(train_id="T_OLD")
        train_legacy.assigned_route_id = route_legacy.route_id

        simple_network.routes[route_new.route_id] = route_new
        simple_network.routes[route_legacy.route_id] = route_legacy
        simple_network.trains[train_new.train_id] = train_new
        simple_network.trains[train_legacy.train_id] = train_legacy

        pairs = executor._get_active_route_train_pairs()
        pair_ids = {(train.train_id, route.route_id) for train, route in pairs}

        assert ("T_NEW", "ROUTE_NEW") in pair_ids
        assert ("T_OLD", "ROUTE_LEGACY") in pair_ids
        assert "T_OLD" in route_legacy.assigned_train_ids
