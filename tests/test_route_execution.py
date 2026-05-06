"""
Tests for Phase 4 - Route Execution

This module tests:
- Route and RouteTask functionality (v1 schema)
- RouteExecutor class
- Route mode execution (ONE_OFF, CIRCULAR, ROUND_TRIP)
- Dwell time logic with timing modes
- Task arrival detection
"""

import pytest
from train_route import Route, RouteTask, TimingMode, RouteMode, RouteRunStatus
from route_executor import RouteExecutor, RouteExecutionState
from train import Train
from tests.train_test_helpers import make_train_position
from network import RailwayNetwork
from railway_platform import Platform
from enums import Direction, TrainState


class TestRouteAndTask:
    """Tests for Route and RouteTask classes (v1)."""

    @pytest.fixture
    def simple_route(self):
        """Create a simple route with three dwell tasks."""
        route = Route(
            route_id="ROUTE_001",
            name="Test Route",
            mode=RouteMode.ONE_OFF,
            tasks=[
                RouteTask(task_kind="dwell", task_location="STA_A", dwell_time_sec=10.0),
                RouteTask(task_kind="dwell", task_location="STA_B", dwell_time_sec=15.0),
                RouteTask(task_kind="dwell", task_location="STA_C", dwell_time_sec=5.0),
            ],
        )
        return route

    def test_route_creation(self, simple_route):
        """Test creating a route."""
        assert simple_route.route_id == "ROUTE_001"
        assert simple_route.mode.value == "one_off"
        assert len(simple_route.tasks) == 3
        assert simple_route.is_active is False

    def test_route_activation(self, simple_route):
        """Test activating a route."""
        simple_route.activate(100.0)
        assert simple_route.is_active is True
        assert simple_route.started_at == 100.0
        assert simple_route.current_task_index == 0
        assert simple_route.direction == 1

    def test_get_current_task(self, simple_route):
        """Test getting current task."""
        task = simple_route.get_current_task()
        assert task is not None
        assert task.task_location == "STA_A"

    def test_advance_to_next_task_one_off(self, simple_route):
        """Test advancing tasks in ONE_OFF mode."""
        # Start at task 0 (STA_A)
        assert simple_route.current_task_index == 0

        # Advance to task 1 (STA_B)
        next_task = simple_route.advance_to_next_task()
        assert next_task is not None
        assert next_task.task_location == "STA_B"
        assert simple_route.current_task_index == 1

        # Advance to task 2 (STA_C)
        next_task = simple_route.advance_to_next_task()
        assert next_task is not None
        assert next_task.task_location == "STA_C"
        assert simple_route.current_task_index == 2

        # Advance past end (route complete)
        next_task = simple_route.advance_to_next_task()
        assert next_task is None
        assert simple_route.current_task_index == 3

    def test_advance_to_next_task_loop(self):
        """Test advancing tasks in CIRCULAR mode."""
        route = Route(
            route_id="ROUTE_LOOP",
            mode=RouteMode.CIRCULAR,
            tasks=[
                RouteTask(task_kind="dwell", task_location="STA_A"),
                RouteTask(task_kind="dwell", task_location="STA_B"),
                RouteTask(task_kind="dwell", task_location="STA_C"),
            ],
        )

        # Advance through tasks
        assert route.current_task_index == 0

        route.advance_to_next_task()  # -> STA_B (index 1)
        assert route.current_task_index == 1

        route.advance_to_next_task()  # -> STA_C (index 2)
        assert route.current_task_index == 2

        # Loop back to start
        next_task = route.advance_to_next_task()  # -> STA_A (index 0)
        assert next_task is not None
        assert next_task.task_location == "STA_A"
        assert route.current_task_index == 0

    def test_advance_to_next_task_round_trip(self):
        """Test advancing tasks in ROUND_TRIP mode."""
        route = Route(
            route_id="ROUTE_RT",
            mode=RouteMode.ROUND_TRIP,
            tasks=[
                RouteTask(task_kind="dwell", task_location="STA_A"),
                RouteTask(task_kind="dwell", task_location="STA_B"),
                RouteTask(task_kind="dwell", task_location="STA_C"),
            ],
        )

        # Forward: A -> B -> C
        assert route.current_task_index == 0
        assert route.direction == 1

        route.advance_to_next_task()  # -> STA_B (index 1)
        assert route.current_task_index == 1

        route.advance_to_next_task()  # -> STA_C (index 2)
        assert route.current_task_index == 2

        # Bounce back: C -> B
        route.advance_to_next_task()
        assert route.current_task_index == 1
        assert route.direction == -1  # Now going reverse

        # Continue reverse: B -> A
        route.advance_to_next_task()
        assert route.current_task_index == 0
        assert route.direction == -1

        # Bounce forward again: A -> B
        route.advance_to_next_task()
        assert route.current_task_index == 1
        assert route.direction == 1  # Back to forward

    def test_dwell_task_should_depart_none_mode(self):
        """Test dwell task depart logic with NONE timing mode."""
        task = RouteTask(
            task_kind="dwell",
            task_location="STA_A",
            dwell_time_sec=30.0,
            timing_mode=TimingMode.NONE,
        )

        # Started dwelling at t=0, now at t=10 (10 sec elapsed) - should not depart
        should_depart = task.should_depart(now=10.0, dwell_started_at=0.0)
        assert should_depart is False

        # Started dwelling at t=0, now at t=30 - should depart
        should_depart = task.should_depart(now=30.0, dwell_started_at=0.0)
        assert should_depart is True

    def test_dwell_task_should_depart_absolute_target(self):
        """Test dwell task depart logic when target_time is set (absolute)."""
        task = RouteTask(
            task_kind="dwell",
            task_location="STA_A",
            dwell_time_sec=10.0,
            timing_mode=TimingMode.ABSOLUTE,
            target_time=1000.0,
        )

        # Current simulation time 900 - too early
        should_depart = task.should_depart(now=900.0, dwell_started_at=800.0)
        assert should_depart is False

        # Current simulation time 1000 - time to depart
        should_depart = task.should_depart(now=1000.0, dwell_started_at=800.0)
        assert should_depart is True

    def test_non_dwell_task_should_depart_immediately(self):
        """waypoint and stop tasks return True for should_depart (caller controls movement)."""
        wp_task = RouteTask(task_kind="waypoint", task_location="STA_A")
        stop_task = RouteTask(task_kind="stop", task_location="STA_B")

        assert wp_task.should_depart(now=0.0, dwell_started_at=None) is True
        assert stop_task.should_depart(now=0.0, dwell_started_at=None) is True

    def test_route_to_dict(self, simple_route):
        """Test route serialization."""
        data = simple_route.to_dict()

        assert data["route_id"] == "ROUTE_001"
        assert data["name"] == "Test Route"
        assert data["mode"] == "one_off"
        assert len(data["tasks"]) == 3
        assert data["assigned_train_ids"] == []
        assert data["is_active"] is False

    def test_route_from_dict(self, simple_route):
        """Test route deserialization."""
        data = simple_route.to_dict()
        restored = Route.from_dict(data)

        assert restored.route_id == simple_route.route_id
        assert restored.name == simple_route.name
        assert restored.mode == simple_route.mode
        assert len(restored.tasks) == len(simple_route.tasks)

    def test_task_to_dict(self):
        """Test RouteTask serialization."""
        task = RouteTask(
            task_kind="dwell",
            task_location="STA_A",
            dwell_time_sec=45.0,
            timing_mode=TimingMode.RELATIVE,
            target_time=120.0,
        )

        data = task.to_dict()
        assert data["task_kind"] == "dwell"
        assert data["task_location"] == "STA_A"
        assert data["dwell_time_sec"] == 45.0
        assert data["timing_mode"] == "relative"
        assert data["target_time"] == 120.0

    def test_task_from_dict(self):
        """Test RouteTask deserialization."""
        data = {
            "task_kind": "dwell",
            "task_location": "STA_B",
            "dwell_time_sec": 60.0,
            "timing_mode": "absolute",
            "target_time": 500.0,
        }

        task = RouteTask.from_dict(data)
        assert task.task_kind == "dwell"
        assert task.task_location == "STA_B"
        assert task.dwell_time_sec == 60.0
        assert task.timing_mode == TimingMode.ABSOLUTE
        assert task.target_time == 500.0

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
            tasks=[
                RouteTask(task_kind="dwell", task_location="STA_A", dwell_time_sec=10.0),
                RouteTask(task_kind="dwell", task_location="STA_B", dwell_time_sec=15.0),
                RouteTask(task_kind="dwell", task_location="STA_C", dwell_time_sec=5.0),
            ],
        )
        return route

    def test_route_executor_creation(self, simple_network):
        """Test creating a route executor.

        v1: simulation_time accumulator was removed; the executor uses
        time.time() as the canonical timestamp source.
        """
        executor = RouteExecutor(simple_network)
        assert executor.network == simple_network
        assert len(executor.execution_states) == 0

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
            tasks=[RouteTask(task_kind="dwell", task_location="STA_A")],
        )
        route_legacy = Route(
            route_id="ROUTE_LEGACY",
            name="Legacy-style",
            tasks=[RouteTask(task_kind="dwell", task_location="STA_B")],
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
