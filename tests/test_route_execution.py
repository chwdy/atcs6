"""
Tests for Phase 4 - Route Execution

This module tests:
- Route and RouteTask functionality (v1 schema)
- RouteExecutor class
- Route mode execution (ONE_OFF, CIRCULAR, ROUND_TRIP)
- Dwell time logic
- Task arrival detection
"""

import pytest
from train_route import (
    PlatformTarget,
    Route,
    RouteMode,
    RouteRunStatus,
    RouteTask,
)
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
                RouteTask(task_kind="dwell", task_location="STA_A", task_time=10.0),
                RouteTask(task_kind="dwell", task_location="STA_B", task_time=15.0),
                RouteTask(task_kind="dwell", task_location="STA_C", task_time=5.0),
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

    def test_dwell_task_should_depart_relative(self):
        """Test dwell task depart logic with task_time only."""
        task = RouteTask(
            task_kind="dwell",
            task_location="STA_A",
            task_time=30.0,
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
            task_time=10.0,
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
        """Test RouteTask serialization. v1 emits task_location as the
        canonical structured form (a dict with one of block_id/platform_id/
        station_id)."""
        task = RouteTask(
            task_kind="dwell",
            task_location=PlatformTarget(platform_id="STA_A"),
            task_time=45.0,
            target_time=120.0,
        )

        data = task.to_dict()
        assert data["task_kind"] == "dwell"
        assert data["task_location"] == {"platform_id": "STA_A"}
        assert data["task_time"] == 45.0
        assert data["target_time"] == 120.0

    def test_task_from_dict(self):
        """Test RouteTask deserialization from the canonical v1 shape:
        structured `task_location` plus `task_time`."""
        data = {
            "task_kind": "dwell",
            "task_location": {"platform_id": "STA_B"},
            "task_time": 60.0,
            "target_time": 500.0,
        }

        task = RouteTask.from_dict(data)
        assert task.task_kind == "dwell"
        assert task.task_location == PlatformTarget(platform_id="STA_B")
        assert task.task_time == 60.0
        assert task.target_time == 500.0

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
                RouteTask(task_kind="dwell", task_location="STA_A", task_time=10.0),
                RouteTask(task_kind="dwell", task_location="STA_B", task_time=15.0),
                RouteTask(task_kind="dwell", task_location="STA_C", task_time=5.0),
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

    def test_route_executor_is_train_at_platform(self, simple_network):
        """Test checking if train is at platform."""
        executor = RouteExecutor(simple_network)
        train = Train(train_id="T001")
        train.position = make_train_position((0, 0), Direction.EAST)

        # Train is at STA_A
        assert executor._is_train_at_platform(train, "STA_A") is True

        # Train is not at STA_B
        assert executor._is_train_at_platform(train, "STA_B") is False

        # Move train to STA_B
        train.position = make_train_position((1, 0), Direction.EAST)
        assert executor._is_train_at_platform(train, "STA_B") is True

    def test_execution_state_dataclass(self):
        """Test RouteExecutionState dataclass."""
        state = RouteExecutionState(
            train_id="T001", route_id="R001", started_at=0.0
        )

        assert state.train_id == "T001"
        assert state.route_id == "R001"
        assert state.current_waypoint_index is None
        assert state.path_cancelled is False
        assert state.task_runs == []


class TestRouteExecutorCompatibilityBugFixes:
    """
    Regression tests for route assignment behaviour.

    BUG 1 (historical): `_get_active_route_train_pairs()` returned early when
    it found any `route.assigned_train_ids` pair, which skipped the legacy
    `train.assigned_route_id` fallback in mixed deployments. After the
    `Train` persistent-only migration there is only one canonical store
    (`Route.assigned_train_ids`); this test verifies that every assigned
    train still surfaces.

    BUG 2: `deactivate_route()` deleted owned controllers and then called
    `_get_controller()`, which recreated a controller entry immediately.
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

    def test_get_active_pairs_lists_every_assigned_train(self, simple_network):
        """Every train assigned to an active route surfaces as a (train, route) pair."""
        executor = RouteExecutor(simple_network)

        route_a = Route(
            route_id="ROUTE_A",
            name="Route A",
            tasks=[RouteTask(task_kind="dwell", task_location="STA_A")],
        )
        route_b = Route(
            route_id="ROUTE_B",
            name="Route B",
            tasks=[RouteTask(task_kind="dwell", task_location="STA_B")],
        )
        route_a.activate(0.0)
        route_b.activate(0.0)
        route_a.assign_train("T_A")
        route_b.assign_train("T_B")

        train_a = Train(train_id="T_A")
        train_b = Train(train_id="T_B")

        executor.register_route(route_a)
        executor.register_route(route_b)
        simple_network.trains[train_a.train_id] = train_a
        simple_network.trains[train_b.train_id] = train_b

        pairs = executor._get_active_route_train_pairs()
        pair_ids = {(train.train_id, route.route_id) for train, route in pairs}

        assert ("T_A", "ROUTE_A") in pair_ids
        assert ("T_B", "ROUTE_B") in pair_ids
