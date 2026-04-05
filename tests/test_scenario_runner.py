"""
Tests for ScenarioRunner - the backend scenario replay and assertion engine.

Run with: conda run -n general pytest tests/test_scenario_runner.py -v
"""
import json
import os
import tempfile

import pytest


class TestAssertionResult:
    """Tests for AssertionResult dataclass."""

    def test_create_passing_assertion(self):
        from scenario_runner import AssertionResult
        r = AssertionResult(path="trains.T1.speed_kmh", op="==", expected=0, actual=0, passed=True)
        assert r.passed is True
        assert r.message == ""

    def test_create_failing_assertion(self):
        from scenario_runner import AssertionResult
        r = AssertionResult(path="trains.T1.speed_kmh", op="==", expected=0, actual=5.2, passed=False, message="got 5.2")
        assert r.passed is False
        assert "5.2" in r.message


class TestCheckpointResult:
    """Tests for CheckpointResult dataclass."""

    def test_checkpoint_all_pass(self):
        from scenario_runner import AssertionResult, CheckpointResult
        cp = CheckpointResult(timestamp=5.0, assertions=[
            AssertionResult(path="a", op="==", expected=1, actual=1, passed=True),
            AssertionResult(path="b", op="==", expected=2, actual=2, passed=True),
        ])
        assert cp.passed is True

    def test_checkpoint_one_fail(self):
        from scenario_runner import AssertionResult, CheckpointResult
        cp = CheckpointResult(timestamp=5.0, assertions=[
            AssertionResult(path="a", op="==", expected=1, actual=1, passed=True),
            AssertionResult(path="b", op="==", expected=2, actual=3, passed=False),
        ])
        assert cp.passed is False

    def test_checkpoint_empty_assertions(self):
        from scenario_runner import CheckpointResult
        cp = CheckpointResult(timestamp=0.0)
        assert cp.passed is True


class TestScenarioResult:
    """Tests for ScenarioResult dataclass."""

    def test_scenario_passes_with_no_errors_and_passing_checkpoints(self):
        from scenario_runner import ScenarioResult, CheckpointResult
        r = ScenarioResult(name="test", checkpoints=[CheckpointResult(timestamp=1.0)])
        assert r.passed is True

    def test_scenario_fails_with_errors(self):
        from scenario_runner import ScenarioResult
        r = ScenarioResult(name="test", errors=["layout failed"])
        assert r.passed is False


class TestPathResolution:
    """Tests for dot-path resolution into nested dicts/lists."""

    def _make_runner(self):
        from scenario_runner import ScenarioRunner
        return ScenarioRunner.__new__(ScenarioRunner)

    def test_resolve_dict_path(self):
        runner = self._make_runner()
        data = {"blocks": {"B1": {"state": "free"}}}
        assert runner._resolve_path(data, "blocks.B1.state") == "free"

    def test_resolve_list_with_id_lookup(self):
        runner = self._make_runner()
        data = {"trains": [{"id": "T1", "speed_kmh": 42.0}, {"id": "T2", "speed_kmh": 10.0}]}
        assert runner._resolve_path(data, "trains.T1.speed_kmh") == 42.0

    def test_resolve_missing_key_returns_none(self):
        runner = self._make_runner()
        data = {"blocks": {}}
        assert runner._resolve_path(data, "blocks.B99.state") is None

    def test_resolve_nested_dict(self):
        runner = self._make_runner()
        data = {"signals": {"S1": {"aspect": "red"}}}
        assert runner._resolve_path(data, "signals.S1.aspect") == "red"


class TestComparison:
    """Tests for assertion comparison logic."""

    def _make_runner(self):
        from scenario_runner import ScenarioRunner
        return ScenarioRunner.__new__(ScenarioRunner)

    def test_equal_int(self):
        runner = self._make_runner()
        assert runner._compare(0, "==", 0) is True
        assert runner._compare(1, "==", 0) is False

    def test_equal_float_with_tolerance(self):
        runner = self._make_runner()
        assert runner._compare(0.3, "==", 0.0) is True
        assert runner._compare(1.0, "==", 0.0) is False

    def test_not_equal(self):
        runner = self._make_runner()
        assert runner._compare("free", "!=", "occupied") is True
        assert runner._compare("free", "!=", "free") is False

    def test_less_than(self):
        runner = self._make_runner()
        assert runner._compare(3, "<", 5) is True
        assert runner._compare(5, "<", 5) is False

    def test_greater_than(self):
        runner = self._make_runner()
        assert runner._compare(10, ">", 5) is True
        assert runner._compare(5, ">", 5) is False

    def test_less_equal(self):
        runner = self._make_runner()
        assert runner._compare(5, "<=", 5) is True

    def test_greater_equal(self):
        runner = self._make_runner()
        assert runner._compare(5, ">=", 5) is True

    def test_none_actual_always_fails(self):
        runner = self._make_runner()
        assert runner._compare(None, "==", 0) is False


class TestScenarioRunnerReplay:
    """Tests for full scenario replay including layout setup and simulation."""

    def _write_scenario(self, scenario_data: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, 'w') as f:
            json.dump(scenario_data, f)
        return path

    def _make_minimal_layout_steps(self) -> list:
        from track import TRACK_EW
        return [
            {
                "id": 1,
                "type": "track_group",
                "description": "Tracks",
                "operations": [
                    {"op": "add_track", "x": 0, "y": 0, "track_bits": TRACK_EW},
                    {"op": "add_track", "x": 1, "y": 0, "track_bits": TRACK_EW},
                    {"op": "add_track", "x": 2, "y": 0, "track_bits": TRACK_EW},
                ]
            },
            {
                "id": 2,
                "type": "block",
                "description": "Block B1",
                "operations": [
                    {
                        "op": "create_block_with_tracks",
                        "block_id": "B1",
                        "name": "Block 1",
                        "positions": [[0, 0], [1, 0], [2, 0]],
                    }
                ]
            }
        ]

    def test_layout_setup_succeeds(self):
        scenario = {
            "version": "1.0",
            "format": "scenario",
            "name": "test layout",
            "layout": self._make_minimal_layout_steps(),
            "actions": []
        }
        path = self._write_scenario(scenario)
        try:
            from scenario_runner import ScenarioRunner
            runner = ScenarioRunner(path)
            result = runner.run()
            assert not result.errors, f"Errors: {result.errors}"
        finally:
            os.unlink(path)

    def test_checkpoint_assertion_passes(self):
        scenario = {
            "version": "1.0",
            "format": "scenario",
            "name": "test checkpoint",
            "layout": self._make_minimal_layout_steps(),
            "actions": [
                {
                    "t": 0.0,
                    "action": "checkpoint",
                    "assertions": [
                        {"path": "blocks.B1.state", "op": "==", "expected": "free"}
                    ]
                }
            ]
        }
        path = self._write_scenario(scenario)
        try:
            from scenario_runner import ScenarioRunner
            runner = ScenarioRunner(path)
            result = runner.run()
            assert not result.errors
            assert len(result.checkpoints) == 1
            assert result.checkpoints[0].passed
        finally:
            os.unlink(path)

    def test_checkpoint_assertion_fails(self):
        scenario = {
            "version": "1.0",
            "format": "scenario",
            "name": "test failing checkpoint",
            "layout": self._make_minimal_layout_steps(),
            "actions": [
                {
                    "t": 0.0,
                    "action": "checkpoint",
                    "assertions": [
                        {"path": "blocks.B1.state", "op": "==", "expected": "occupied"}
                    ]
                }
            ]
        }
        path = self._write_scenario(scenario)
        try:
            from scenario_runner import ScenarioRunner
            runner = ScenarioRunner(path)
            result = runner.run()
            assert not result.errors
            assert len(result.checkpoints) == 1
            assert not result.checkpoints[0].passed
            assert "occupied" in result.checkpoints[0].assertions[0].message
        finally:
            os.unlink(path)

    def test_api_action_replay(self):
        scenario = {
            "version": "1.0",
            "format": "scenario",
            "name": "test api replay",
            "layout": self._make_minimal_layout_steps(),
            "actions": [
                {
                    "t": 0.0,
                    "method": "POST",
                    "url": "/api/simulation/control",
                    "body": {"action": "start"}
                },
                {
                    "t": 0.1,
                    "method": "POST",
                    "url": "/api/simulation/control",
                    "body": {"action": "stop"}
                }
            ]
        }
        path = self._write_scenario(scenario)
        try:
            from scenario_runner import ScenarioRunner
            runner = ScenarioRunner(path)
            result = runner.run()
            assert not result.errors
        finally:
            os.unlink(path)

    def test_simulation_advances_between_actions(self):
        scenario = {
            "version": "1.0",
            "format": "scenario",
            "name": "test sim advance",
            "layout": self._make_minimal_layout_steps(),
            "actions": [
                {
                    "t": 0.0,
                    "method": "POST",
                    "url": "/api/simulation/control",
                    "body": {"action": "start"}
                },
                {
                    "t": 1.0,
                    "action": "checkpoint",
                    "assertions": []
                }
            ]
        }
        path = self._write_scenario(scenario)
        try:
            from scenario_runner import ScenarioRunner
            runner = ScenarioRunner(path)
            result = runner.run()
            assert not result.errors
            assert result.simulation_time >= 0.9
        finally:
            os.unlink(path)
