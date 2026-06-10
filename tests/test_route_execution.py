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
    BlockTarget,
    PlatformTarget,
    Route,
    RouteMode,
    RouteRunStatus,
    RouteTask,
    RouteTaskRun,
)
from route_executor import RouteExecutor, RouteExecutionState
from runtime_payloads import ResolvedRouteTarget, ResourceEvent
from train import Train
from tests.train_test_helpers import set_train_span
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
        """Both target_time and task_time set: hold task_time past the LATER of
        actual start and the scheduled start (design_new.md "Task time
        semantics"). Scheduled start 1000 + 10s hold => depart at 1010; the
        earlier arrival (800) does not shorten the hold."""
        task = RouteTask(
            task_kind="dwell",
            task_location="STA_A",
            task_time=10.0,
            target_time=1000.0,
        )

        # Before the scheduled start: too early.
        assert task.should_depart(now=900.0, dwell_started_at=800.0) is False
        # At/after the scheduled start but before the hold elapses: still holding.
        assert task.should_depart(now=1000.0, dwell_started_at=800.0) is False
        assert task.should_depart(now=1009.0, dwell_started_at=800.0) is False
        # Full hold served past the scheduled start.
        assert task.should_depart(now=1010.0, dwell_started_at=800.0) is True

    def test_dwell_late_arrival_serves_full_hold(self):
        """A late arrival (after target_time) still serves the full task_time
        hold from its actual start (design_new.md "Task time semantics")."""
        task = RouteTask(
            task_kind="dwell",
            task_location="STA_A",
            task_time=30.0,
            target_time=1000.0,
        )
        # Arrived late at 1050; hold 30s from max(1050, 1000) = 1050 -> 1080.
        assert task.should_depart(now=1070.0, dwell_started_at=1050.0) is False
        assert task.should_depart(now=1080.0, dwell_started_at=1050.0) is True

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
        simple_network.trains[train.train_id] = train
        # The predicate reads the head tile from the train's controller
        # (cab + current_span), so register a controller and seed its span.
        controller = executor._get_controller(train)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)

        # Train is at STA_A
        assert executor._is_train_at_platform(train, "STA_A") is True

        # Train is not at STA_B
        assert executor._is_train_at_platform(train, "STA_B") is False

        # Move train to STA_B
        set_train_span(controller, (1, 0), facing_direction=Direction.EAST)
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


class TestRouteCallbacksWrapAround:
    """Regression tests for the `task_runs` list-index vs `Route.tasks`
    index conflation in Phase-3 callbacks.

    Per design_new.md line 1104 + line 1267, `walk_position` (the list
    index in `RouteRun.task_runs`) and `task_index` (the index into
    `Route.tasks`) decouple after `CIRCULAR` / `ROUND_TRIP` wrap-around
    once `len(task_runs) > len(Route.tasks)`. The callbacks must index
    `task_runs` by list position, never by `task_index`, or they will
    silently target an already-finished early row from a prior walk.
    """

    @pytest.fixture
    def wrap_around_scenario(self):
        """Build a 2-stop CIRCULAR route mid-second-wrap.

        Route.tasks: [stop_A (BLK_A), stop_B (BLK_B)], CIRCULAR.
        exec_state.task_runs after the first wrap and starting the second:
          [0] task_index=0 finished (visit 1 to A) — already done
          [1] task_index=1 finished (visit 1 to B) — already done
          [2] task_index=0 in-flight (visit 2 to A) — current target

        The currently in-flight stop_A is at list position 2 but shares
        `task_index = 0` with the already-finished row at list position 0.
        """
        network = RailwayNetwork()
        network.blocks = {"BLK_A": object(), "BLK_B": object()}

        route = Route(
            route_id="ROUTE_WRAP",
            name="Wrap-around",
            mode=RouteMode.CIRCULAR,
            tasks=[
                RouteTask(
                    task_kind="stop",
                    task_location=BlockTarget(block_id="BLK_A"),
                    task_direction="forward",
                ),
                RouteTask(
                    task_kind="stop",
                    task_location=BlockTarget(block_id="BLK_B"),
                    task_direction="forward",
                ),
            ],
        )
        route.activate(0.0)
        route.assign_train("T_WRAP")

        train = Train(train_id="T_WRAP")
        network.trains[train.train_id] = train

        executor = RouteExecutor(network)
        executor.register_route(route)

        controller = executor._get_controller(train)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)

        exec_state = RouteExecutionState(
            train_id=train.train_id,
            route_id=route.route_id,
            started_at=0.0,
            task_runs=[
                RouteTaskRun(
                    task_index=0,
                    finished_at=10.0,
                    resolved_target=ResolvedRouteTarget(
                        requested_destination_block_id="BLK_A",
                        requested_destination_direction="forward",
                    ),
                ),
                RouteTaskRun(
                    task_index=1,
                    finished_at=20.0,
                    resolved_target=ResolvedRouteTarget(
                        requested_destination_block_id="BLK_B",
                        requested_destination_direction="forward",
                    ),
                ),
                RouteTaskRun(
                    task_index=0,
                    finished_at=None,
                    resolved_target=ResolvedRouteTarget(
                        requested_destination_block_id="BLK_A",
                        requested_destination_direction="forward",
                    ),
                ),
            ],
        )
        executor.execution_states[train.train_id] = exec_state

        return executor, exec_state, train, route

    def test_next_planning_walk_position_decouples_after_wrap(
        self, wrap_around_scenario
    ):
        """The list-index and task-index cursors decouple after wrap."""
        _executor, exec_state, _train, _route = wrap_around_scenario
        assert exec_state.next_planning_index == 0
        assert exec_state.next_planning_walk_position == 2

    def test_handle_path_completion_writes_to_current_walk_row(
        self, wrap_around_scenario
    ):
        """Path completion must update list position 2 (the current
        in-flight stop_A), not list position 0 (the already-finished
        first-wrap visit that shares `task_index = 0`)."""
        executor, exec_state, train, _route = wrap_around_scenario

        ok = executor.handle_path_completion(
            train_id=train.train_id,
            path_id="P_X",
            authority_end_block_id="BLK_A",
            requested_destination_direction="forward",
            event_time=30.0,
        )

        assert ok is True
        assert exec_state.task_runs[2].finished_at == 30.0
        # Already-finished rows must NOT have been touched.
        assert exec_state.task_runs[0].finished_at == 10.0
        assert exec_state.task_runs[1].finished_at == 20.0
        # Wrap-append: the next walk-position for CIRCULAR is appended
        # at list position 3 with task_index=1 (back to stop_B).
        assert len(exec_state.task_runs) == 4
        assert exec_state.task_runs[3].task_index == 1
        assert exec_state.task_runs[3].finished_at is None

    def test_handle_train_advanced_writes_to_current_walk_row(self):
        """The head-advance callback also indexes `task_runs` by list
        position via `projection.walk_position`. Lock that in with a
        wrap-around exec_state whose in-flight waypoint shares
        `task_index` with an already-finished early row."""
        network = RailwayNetwork()
        network.blocks = {"BLK_A": object(), "BLK_B": object()}

        route = Route(
            route_id="ROUTE_WRAP_WP",
            name="Wrap-around waypoint",
            mode=RouteMode.CIRCULAR,
            tasks=[
                RouteTask(
                    task_kind="waypoint",
                    task_location=BlockTarget(block_id="BLK_A"),
                    task_direction="forward",
                ),
                RouteTask(
                    task_kind="stop",
                    task_location=BlockTarget(block_id="BLK_B"),
                    task_direction="forward",
                ),
            ],
        )
        route.activate(0.0)
        route.assign_train("T_WP")

        train = Train(train_id="T_WP")
        network.trains[train.train_id] = train

        executor = RouteExecutor(network)
        executor.register_route(route)

        controller = executor._get_controller(train)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)

        exec_state = RouteExecutionState(
            train_id=train.train_id,
            route_id=route.route_id,
            started_at=0.0,
            task_runs=[
                RouteTaskRun(
                    task_index=0,
                    finished_at=10.0,
                    resolved_target=ResolvedRouteTarget(
                        requested_destination_block_id="BLK_A",
                        requested_destination_direction="forward",
                    ),
                ),
                RouteTaskRun(
                    task_index=1,
                    finished_at=20.0,
                    resolved_target=ResolvedRouteTarget(
                        requested_destination_block_id="BLK_B",
                        requested_destination_direction="forward",
                    ),
                ),
                RouteTaskRun(
                    task_index=0,
                    finished_at=None,
                    resolved_target=ResolvedRouteTarget(
                        requested_destination_block_id="BLK_A",
                        requested_destination_direction="forward",
                    ),
                ),
            ],
        )
        executor.execution_states[train.train_id] = exec_state

        assert exec_state.next_planning_walk_position == 2

        executor.handle_train_advanced(
            train_id=train.train_id,
            path_id="P_X",
            resource_event=ResourceEvent(
                sequence_index=None,
                resource_type="block",
                resource_id="BLK_A",
                traversal_direction="forward",
            ),
        )

        assert exec_state.task_runs[2].finished_at is not None
        assert exec_state.task_runs[0].finished_at == 10.0
        assert exec_state.task_runs[1].finished_at == 20.0

    def test_handle_train_advanced_skips_wrong_direction_waypoint(self):
        """A head-entry into the waypoint's block in the WRONG block-relative
        direction must NOT consume the waypoint (design_new.md:465; gap #4).
        Direction is matched on `traversal_direction`, not the compass
        `exit_edge`, and a known mismatch is a no-op."""
        network = RailwayNetwork()
        network.blocks = {"BLK_A": object()}

        route = Route(
            route_id="ROUTE_DIR_WP",
            name="Direction waypoint",
            mode=RouteMode.CIRCULAR,
            tasks=[
                RouteTask(
                    task_kind="waypoint",
                    task_location=BlockTarget(block_id="BLK_A"),
                    task_direction="forward",
                ),
            ],
        )
        route.activate(0.0)
        route.assign_train("T_DIR")

        train = Train(train_id="T_DIR")
        network.trains[train.train_id] = train

        executor = RouteExecutor(network)
        executor.register_route(route)
        controller = executor._get_controller(train)
        controller.commands.cab = "A"
        set_train_span(controller, (0, 0), facing_direction=Direction.EAST)

        exec_state = RouteExecutionState(
            train_id=train.train_id,
            route_id=route.route_id,
            started_at=0.0,
            task_runs=[
                RouteTaskRun(
                    task_index=0,
                    finished_at=None,
                    resolved_target=ResolvedRouteTarget(
                        requested_destination_block_id="BLK_A",
                        requested_destination_direction="forward",
                    ),
                ),
            ],
        )
        executor.execution_states[train.train_id] = exec_state

        # Reported entry is REVERSE — opposite the waypoint's forward target.
        executor.handle_train_advanced(
            train_id=train.train_id,
            path_id="P_X",
            resource_event=ResourceEvent(
                sequence_index=None,
                resource_type="block",
                resource_id="BLK_A",
                traversal_direction="reverse",
            ),
        )
        assert exec_state.task_runs[0].finished_at is None, (
            "wrong-direction entry must not consume the forward waypoint"
        )

        # A forward entry DOES consume it.
        executor.handle_train_advanced(
            train_id=train.train_id,
            path_id="P_X",
            resource_event=ResourceEvent(
                sequence_index=None,
                resource_type="block",
                resource_id="BLK_A",
                traversal_direction="forward",
            ),
        )
        assert exec_state.task_runs[0].finished_at is not None, (
            "matching-direction entry must consume the waypoint"
        )
