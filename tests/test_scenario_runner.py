"""
Tests for ScenarioRunner - scenario recording and replay engine.

Tests cover:
- Data class creation and properties
- Path resolution (dict paths, list-by-id lookup, missing keys)
- Comparison operators (==, !=, <, >, <=, >=, float tolerance, None handling)
- Full replay: layout setup, checkpoint assertions pass/fail, API action replay,
  simulation time advancement
"""

import threading
from datetime import datetime

import pytest


# ---------------------------------------------------------------------------
# Data class tests
# ---------------------------------------------------------------------------


class TestAssertionResult:
    """Tests for the AssertionResult dataclass."""

    def test_creation(self):
        """AssertionResult stores all fields correctly."""
        from scenario_runner import AssertionResult

        r = AssertionResult(
            path="trains.T1.speed_kmh",
            op="==",
            expected=0.0,
            actual=0.0,
            passed=True,
            message="OK",
        )
        assert r.path == "trains.T1.speed_kmh"
        assert r.op == "=="
        assert r.expected == 0.0
        assert r.actual == 0.0
        assert r.passed is True
        assert r.message == "OK"

    def test_failed_assertion(self):
        """AssertionResult can represent a failed assertion."""
        from scenario_runner import AssertionResult

        r = AssertionResult(
            path="blocks.B1.state",
            op="==",
            expected="occupied",
            actual="free",
            passed=False,
            message="blocks.B1.state: expected 'occupied' == 'free'",
        )
        assert r.passed is False
        assert "expected" in r.message


class TestCheckpointResult:
    """Tests for the CheckpointResult dataclass."""

    def test_all_pass(self):
        """Checkpoint passes when all assertions pass."""
        from scenario_runner import AssertionResult, CheckpointResult

        c = CheckpointResult(
            timestamp=1.0,
            assertions=[
                AssertionResult("a", "==", 1, 1, True, "ok"),
                AssertionResult("b", "==", 2, 2, True, "ok"),
            ],
        )
        assert c.passed is True

    def test_any_fail(self):
        """Checkpoint fails when any assertion fails."""
        from scenario_runner import AssertionResult, CheckpointResult

        c = CheckpointResult(
            timestamp=2.5,
            assertions=[
                AssertionResult("a", "==", 1, 1, True, "ok"),
                AssertionResult("b", "==", 2, 3, False, "mismatch"),
            ],
        )
        assert c.passed is False

    def test_empty_assertions(self):
        """Checkpoint with no assertions passes."""
        from scenario_runner import CheckpointResult

        c = CheckpointResult(timestamp=0.0, assertions=[])
        assert c.passed is True


class TestScenarioResult:
    """Tests for the ScenarioResult dataclass."""

    def test_passed_when_all_checkpoints_pass(self):
        """Scenario passes when all checkpoints pass and no errors."""
        from scenario_runner import (
            AssertionResult,
            CheckpointResult,
            ScenarioResult,
        )

        sr = ScenarioResult(
            name="test",
            checkpoints=[
                CheckpointResult(1.0, [AssertionResult("a", "==", 1, 1, True, "ok")]),
            ],
            errors=[],
            simulation_time=1.0,
        )
        assert sr.passed is True

    def test_fails_when_checkpoint_fails(self):
        """Scenario fails when any checkpoint fails."""
        from scenario_runner import (
            AssertionResult,
            CheckpointResult,
            ScenarioResult,
        )

        sr = ScenarioResult(
            name="test",
            checkpoints=[
                CheckpointResult(1.0, [AssertionResult("a", "==", 1, 2, False, "bad")]),
            ],
            errors=[],
            simulation_time=1.0,
        )
        assert sr.passed is False

    def test_fails_when_errors_present(self):
        """Scenario fails when errors list is non-empty."""
        from scenario_runner import ScenarioResult

        sr = ScenarioResult(
            name="test",
            checkpoints=[],
            errors=["something broke"],
            simulation_time=0.0,
        )
        assert sr.passed is False

    def test_passes_with_no_checkpoints_and_no_errors(self):
        """Scenario with no checkpoints and no errors passes."""
        from scenario_runner import ScenarioResult

        sr = ScenarioResult(
            name="empty",
            checkpoints=[],
            errors=[],
            simulation_time=0.0,
        )
        assert sr.passed is True


# ---------------------------------------------------------------------------
# Path resolution tests
# ---------------------------------------------------------------------------


class TestResolvePathDict:
    """Tests for _resolve_path with dict-style paths."""

    def _runner(self):
        from scenario_runner import ScenarioRunner

        return ScenarioRunner.__new__(ScenarioRunner)

    def test_simple_key(self):
        """Resolve a single-level dict key."""
        runner = self._runner()
        data = {"blocks": {"B1": {"state": "free"}}}
        assert runner._resolve_path(data, "blocks") == {"B1": {"state": "free"}}

    def test_nested_dict(self):
        """Resolve a nested dict path."""
        runner = self._runner()
        data = {"blocks": {"B1": {"state": "free"}}}
        assert runner._resolve_path(data, "blocks.B1.state") == "free"

    def test_missing_key_returns_none(self):
        """Missing key in dict returns None."""
        runner = self._runner()
        data = {"blocks": {}}
        assert runner._resolve_path(data, "blocks.B1.state") is None

    def test_deeply_nested(self):
        """Deep nesting works correctly."""
        runner = self._runner()
        data = {"a": {"b": {"c": {"d": 42}}}}
        assert runner._resolve_path(data, "a.b.c.d") == 42


class TestResolvePathList:
    """Tests for _resolve_path with list-by-id lookups (trains)."""

    def _runner(self):
        from scenario_runner import ScenarioRunner

        return ScenarioRunner.__new__(ScenarioRunner)

    def test_list_by_id(self):
        """Resolve train from list by id field."""
        runner = self._runner()
        data = {
            "trains": [
                {"id": "T1", "speed_kmh": 50.0},
                {"id": "T2", "speed_kmh": 80.0},
            ]
        }
        assert runner._resolve_path(data, "trains.T1.speed_kmh") == 50.0

    def test_list_by_id_second_item(self):
        """Resolve second item from a list by id."""
        runner = self._runner()
        data = {
            "trains": [
                {"id": "T1", "speed_kmh": 50.0},
                {"id": "T2", "speed_kmh": 80.0},
            ]
        }
        assert runner._resolve_path(data, "trains.T2.speed_kmh") == 80.0

    def test_list_missing_id_returns_none(self):
        """Non-existent id in list returns None."""
        runner = self._runner()
        data = {"trains": [{"id": "T1", "speed_kmh": 50.0}]}
        assert runner._resolve_path(data, "trains.T99.speed_kmh") is None

    def test_list_by_numeric_index(self):
        """Numeric string can be used as a list index."""
        runner = self._runner()
        data = {"items": [10, 20, 30]}
        assert runner._resolve_path(data, "items.1") == 20

    def test_empty_path(self):
        """Empty path returns the data itself."""
        runner = self._runner()
        data = {"x": 1}
        assert runner._resolve_path(data, "") == data


# ---------------------------------------------------------------------------
# Comparison tests
# ---------------------------------------------------------------------------


class TestCompare:
    """Tests for _compare method."""

    def _runner(self):
        from scenario_runner import ScenarioRunner

        return ScenarioRunner.__new__(ScenarioRunner)

    def test_eq_exact(self):
        assert self._runner()._compare(5, "==", 5) is True

    def test_eq_fail(self):
        assert self._runner()._compare(5, "==", 6) is False

    def test_neq(self):
        assert self._runner()._compare(5, "!=", 6) is True

    def test_neq_fail(self):
        assert self._runner()._compare(5, "!=", 5) is False

    def test_lt(self):
        assert self._runner()._compare(3, "<", 5) is True

    def test_lt_fail(self):
        assert self._runner()._compare(5, "<", 5) is False

    def test_gt(self):
        assert self._runner()._compare(10, ">", 5) is True

    def test_gt_fail(self):
        assert self._runner()._compare(5, ">", 5) is False

    def test_le(self):
        assert self._runner()._compare(5, "<=", 5) is True

    def test_le_less(self):
        assert self._runner()._compare(4, "<=", 5) is True

    def test_ge(self):
        assert self._runner()._compare(5, ">=", 5) is True

    def test_ge_greater(self):
        assert self._runner()._compare(6, ">=", 5) is True

    def test_float_tolerance_eq(self):
        """Floats within 0.5 tolerance should be equal."""
        assert self._runner()._compare(5.3, "==", 5.0) is True

    def test_float_tolerance_boundary(self):
        """Floats exactly at 0.5 boundary should still pass."""
        assert self._runner()._compare(5.5, "==", 5.0) is True

    def test_float_tolerance_fail(self):
        """Floats outside 0.5 tolerance should fail."""
        assert self._runner()._compare(5.6, "==", 5.0) is False

    def test_string_eq(self):
        """String comparison works."""
        assert self._runner()._compare("free", "==", "free") is True

    def test_string_neq(self):
        assert self._runner()._compare("free", "==", "occupied") is False

    def test_none_actual_eq(self):
        """None actual equals None expected."""
        assert self._runner()._compare(None, "==", None) is True

    def test_none_actual_neq_value(self):
        """None actual does not equal a real value."""
        assert self._runner()._compare(None, "==", 5) is False

    def test_none_neq_operator(self):
        """None != a real value is True."""
        assert self._runner()._compare(None, "!=", 5) is True


# ---------------------------------------------------------------------------
# Full replay engine tests
# ---------------------------------------------------------------------------


class TestRunReplay:
    """Integration tests for ScenarioRunner.run()."""

    def _make_simple_scenario(self, actions=None, layout_steps=None):
        """Build a minimal scenario dict for testing.

        Creates a layout with one EW track at (0,0) if no layout_steps
        are provided.
        """
        if layout_steps is None:
            layout_steps = [
                {
                    "id": 1,
                    "type": "add_track",
                    "x": 0,
                    "y": 0,
                    "track_bits": 2,  # TRACK_EW
                }
            ]
        return {
            "name": "test-scenario",
            "layout": layout_steps,
            "actions": actions or [],
        }

    def test_empty_scenario(self):
        """Run an empty scenario succeeds with zero simulation time."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario(actions=[])
        result = ScenarioRunner().run(scenario)
        assert result.passed is True
        assert result.simulation_time == 0.0
        assert result.errors == []
        assert result.checkpoints == []

    def test_layout_setup(self):
        """Layout steps are replayed to create the network."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario()
        result = ScenarioRunner().run(scenario)
        assert result.passed is True

    def test_checkpoint_pass(self):
        """Checkpoint assertion that passes."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario(
            actions=[
                {
                    "action": "checkpoint",
                    "t": 0.0,
                    "assertions": [
                        {
                            "path": "blocks",
                            "op": "!=",
                            "expected": None,
                        }
                    ],
                }
            ]
        )
        result = ScenarioRunner().run(scenario)
        assert result.passed is True
        assert len(result.checkpoints) == 1
        assert result.checkpoints[0].passed is True

    def test_checkpoint_fail(self):
        """Checkpoint assertion that fails."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario(
            actions=[
                {
                    "action": "checkpoint",
                    "t": 0.0,
                    "assertions": [
                        {
                            "path": "trains.NONEXIST.speed_kmh",
                            "op": "==",
                            "expected": 999.0,
                        }
                    ],
                }
            ]
        )
        result = ScenarioRunner().run(scenario)
        assert result.passed is False
        assert len(result.checkpoints) == 1
        assert result.checkpoints[0].passed is False

    def test_api_action_replay(self):
        """API actions are replayed via the Flask test client."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario(
            layout_steps=[
                {
                    "id": 1,
                    "type": "add_track",
                    "x": 0,
                    "y": 0,
                    "track_bits": 2,
                }
            ],
            actions=[
                {
                    "t": 0.0,
                    "method": "PUT",
                    "url": "/api/track/0/0",
                    "body": {"track_bits": 2},
                },
                {
                    "action": "checkpoint",
                    "t": 0.0,
                    "assertions": [
                        {
                            "path": "blocks",
                            "op": "!=",
                            "expected": None,
                        }
                    ],
                },
            ],
        )
        result = ScenarioRunner().run(scenario)
        # The API call should not cause errors
        assert len(result.errors) == 0

    def test_simulation_time_advances(self):
        """Simulation time advances to match action timestamps."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario(
            actions=[
                {
                    "action": "checkpoint",
                    "t": 3.0,
                    "assertions": [],
                },
            ]
        )
        result = ScenarioRunner().run(scenario)
        assert result.simulation_time >= 3.0

    def test_multiple_checkpoints(self):
        """Multiple checkpoints at different timestamps."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario(
            actions=[
                {
                    "action": "checkpoint",
                    "t": 1.0,
                    "assertions": [],
                },
                {
                    "action": "checkpoint",
                    "t": 2.0,
                    "assertions": [],
                },
            ]
        )
        result = ScenarioRunner().run(scenario)
        assert len(result.checkpoints) == 2
        assert result.checkpoints[0].timestamp == 1.0
        assert result.checkpoints[1].timestamp == 2.0
        assert result.simulation_time >= 2.0

    def test_scenario_result_name(self):
        """ScenarioResult name is taken from the scenario."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario()
        scenario["name"] = "my-test"
        result = ScenarioRunner().run(scenario)
        assert result.name == "my-test"

    def test_run_captures_errors(self):
        """Errors during replay are captured, not raised."""
        from scenario_runner import ScenarioRunner

        scenario = self._make_simple_scenario(
            layout_steps=[],
            actions=[
                {
                    "t": 0.0,
                    "method": "GET",
                    "url": "/api/nonexistent-endpoint-that-404s",
                    "body": None,
                },
                {
                    "action": "checkpoint",
                    "t": 0.0,
                    "assertions": [],
                },
            ],
        )
        result = ScenarioRunner().run(scenario)
        # Should not crash; the 404 is not an error in the runner itself
        # The checkpoint should still be recorded
        assert len(result.checkpoints) == 1


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestNetworkStepsEndpoint:
    """Tests for GET /api/network/steps API endpoint."""

    def _create_test_app(self):
        """Create a Flask test app with a simple network."""
        import threading
        from datetime import datetime

        from app_factory import create_app
        from app_state import AppState
        from network import RailwayNetwork
        from path_dispatcher import PathDispatcher
        from persistence import PersistenceManager
        from simulation import SimulationController
        from speed_profile import SpeedProfileRegistry
        from track import TRACK_EW, TrackSegment

        network = RailwayNetwork()
        network.add_track(TrackSegment(0, 0, TRACK_EW))
        network.add_track(TrackSegment(1, 0, TRACK_EW))

        pd = PathDispatcher(network)
        sim = SimulationController(network, pd)
        pm = PersistenceManager()
        sr = SpeedProfileRegistry()

        state = AppState(
            network=network,
            path_dispatcher=pd,
            sim_controller=sim,
            persistence_manager=pm,
            speed_profile_registry=sr,
            simulation_lock=threading.Lock(),
            simulation_running=False,
            simulation_speed=1.0,
            last_update_time=datetime.now(),
        )
        return create_app(state)

    def test_returns_steps_list(self):
        """Endpoint returns a list of steps."""
        app = self._create_test_app()
        client = app.test_client()
        resp = client.get("/api/network/steps")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "steps" in data
        assert isinstance(data["steps"], list)
        assert len(data["steps"]) > 0

    def test_steps_contain_track_operations(self):
        """Steps include track group with add_track operations."""
        app = self._create_test_app()
        client = app.test_client()
        resp = client.get("/api/network/steps")
        data = resp.get_json()
        steps = data["steps"]
        assert steps[0]["type"] == "track_group"
        ops = steps[0]["operations"]
        assert any(op["op"] == "add_track" for op in ops)
