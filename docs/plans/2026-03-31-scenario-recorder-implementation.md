# Scenario Recorder Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a system to record user interactions as replayable test scenarios with assertions.

**Architecture:** Frontend interceptor captures raw API calls during recording sessions. Backend ScenarioRunner replays them via Flask test client, advancing simulation between actions, and evaluating assertions at user-defined checkpoints.

**Tech Stack:** Vanilla JS (frontend recorder), Python 3 + Flask test client (backend runner), pytest (integration)

**Design doc:** `docs/plans/2026-03-30-scenario-recorder-design.md`

---

### Task 1: Scenario Runner - Data Classes and Path Resolution

**Files:**
- Create: `scenario_runner.py`
- Test: `tests/test_scenario_runner.py`

**Step 1: Write failing tests for data classes and path resolution**

Create `tests/test_scenario_runner.py`:

```python
"""
Tests for ScenarioRunner - the backend scenario replay and assertion engine.

Run with: conda run -n general pytest tests/test_scenario_runner.py -v
"""
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
        """Create a ScenarioRunner-like object for testing path resolution."""
        from scenario_runner import ScenarioRunner
        return ScenarioRunner.__new__(ScenarioRunner)

    def test_resolve_dict_path(self):
        runner = self._make_runner()
        data = {"blocks": {"B1": {"state": "free"}}}
        assert runner._resolve_path(data, "blocks.B1.state") == "free"

    def test_resolve_list_with_id_lookup(self):
        """trains is a list - path 'trains.T1.speed_kmh' should find train by id."""
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
        # Within tolerance (0.5 default)
        assert runner._compare(0.3, "==", 0.0) is True
        # Outside tolerance
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
```

**Step 2: Run tests to verify they fail**

```bash
conda run -n general pytest tests/test_scenario_runner.py -v
```
Expected: ImportError - `scenario_runner` module doesn't exist.

**Step 3: Implement scenario_runner.py with data classes and path resolution**

Create `scenario_runner.py`:

```python
"""
Scenario Runner - Replay recorded scenarios and evaluate assertions.

Definition:
This module provides the ScenarioRunner class which loads scenario JSON files,
rebuilds the layout using StepPlayer, replays recorded API calls via Flask test
client, advances simulation between actions, and evaluates assertions at
user-defined checkpoints.

TODO List:
- [x] Data classes for assertion/checkpoint/scenario results
- [x] Dot-path resolution for nested payloads
- [x] Comparison operators with float tolerance
- [ ] Full scenario replay with Flask test client
- [ ] Pytest integration via test_scenarios.py
"""

from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict
import json


@dataclass
class AssertionResult:
    """Result of evaluating a single assertion."""
    path: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    message: str = ""


@dataclass
class CheckpointResult:
    """Result of evaluating all assertions at a checkpoint."""
    timestamp: float
    assertions: List[AssertionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(a.passed for a in self.assertions)


@dataclass
class ScenarioResult:
    """Overall result of running a scenario."""
    name: str
    checkpoints: List[CheckpointResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors and all(c.passed for c in self.checkpoints)


class ScenarioRunner:
    """Replay recorded scenarios and evaluate checkpoint assertions.

    The runner loads a scenario JSON file, rebuilds the network layout,
    replays recorded API calls, advances simulation time, and checks
    assertions at checkpoint boundaries.
    """

    TICK_SIZE: float = 0.05
    FLOAT_TOLERANCE: float = 0.5

    def __init__(self, scenario_path: str) -> None:
        with open(scenario_path) as f:
            self.data: Dict[str, Any] = json.load(f)

    def _resolve_path(self, data: Any, path: str) -> Any:
        """Resolve dot-notation path into nested dict/list structure.

        Handles two patterns:
        - Dict access: 'blocks.B1.state' -> data["blocks"]["B1"]["state"]
        - List-by-id: 'trains.T1.speed_kmh' -> find item with id=="T1" in list

        Args:
            data: The root data structure (from build_network_payload).
            path: Dot-separated path string.

        Returns:
            The resolved value, or None if path doesn't exist.
        """
        parts = path.split(".")
        current = data
        for part in parts:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                # Try to find item by 'id' field (e.g., trains list)
                found = None
                for item in current:
                    if isinstance(item, dict) and item.get("id") == part:
                        found = item
                        break
                if found is not None:
                    current = found
                else:
                    # Fall back to integer index
                    try:
                        current = current[int(part)]
                    except (ValueError, IndexError):
                        return None
            else:
                return None
        return current

    def _compare(self, actual: Any, op: str, expected: Any) -> bool:
        """Compare actual vs expected using the given operator.

        Float comparisons with '==' use FLOAT_TOLERANCE for approximate matching.

        Args:
            actual: The actual value from the simulation.
            op: Comparison operator string.
            expected: The expected value from the assertion.

        Returns:
            True if the comparison passes.
        """
        if actual is None:
            return False
        # Float tolerance for equality
        if op == "==" and (isinstance(actual, float) or isinstance(expected, float)):
            try:
                return abs(float(actual) - float(expected)) <= self.FLOAT_TOLERANCE
            except (TypeError, ValueError):
                return False
        ops = {
            "==": lambda a, e: a == e,
            "!=": lambda a, e: a != e,
            "<": lambda a, e: a < e,
            ">": lambda a, e: a > e,
            "<=": lambda a, e: a <= e,
            ">=": lambda a, e: a >= e,
        }
        comparator = ops.get(op)
        if comparator is None:
            return False
        try:
            return comparator(actual, expected)
        except TypeError:
            return False
```

**Step 4: Run tests to verify they pass**

```bash
conda run -n general pytest tests/test_scenario_runner.py -v
```
Expected: All tests PASS.

**Step 5: Commit**

```bash
git add scenario_runner.py tests/test_scenario_runner.py
git commit -m "feat: add scenario runner data classes, path resolution, and comparison logic"
```

---

### Task 2: Scenario Runner - Full Replay Engine

**Files:**
- Modify: `scenario_runner.py`
- Modify: `tests/test_scenario_runner.py`

**Context needed:**
- `app_factory.py` uses `create_app(state)` where `state` is `AppState`
- `AppState` requires: network, path_dispatcher, sim_controller, persistence_manager, speed_profile_registry, simulation_lock, simulation_running, simulation_speed, last_update_time
- `bootstrap.py:bootstrap_runtime()` returns (network, path_dispatcher, sim_controller, persistence_manager)
- `app.extensions["state"]` stores the AppState
- `build_network_payload(network, sim_controller)` returns the full payload

**Step 1: Write failing tests for full replay**

Add to `tests/test_scenario_runner.py`:

```python
import json
import os
import tempfile


class TestScenarioRunnerReplay:
    """Tests for full scenario replay including layout setup and simulation."""

    def _write_scenario(self, scenario_data: dict) -> str:
        """Write scenario dict to a temp file, return path."""
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, 'w') as f:
            json.dump(scenario_data, f)
        return path

    def _make_minimal_layout_steps(self) -> list:
        """Create minimal layout steps: 3 EW tracks with a block."""
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
        """Runner should rebuild layout from steps without errors."""
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
        """Checkpoint assertion on block state should pass after layout setup."""
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
        """Checkpoint assertion with wrong expected value should fail."""
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
        """API actions should be replayed via Flask test client."""
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
        """Simulation should advance time between actions based on timestamps."""
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
            # Should have advanced ~1 second of simulation time
            assert result.simulation_time >= 0.9
        finally:
            os.unlink(path)
```

**Step 2: Run tests to verify they fail**

```bash
conda run -n general pytest tests/test_scenario_runner.py::TestScenarioRunnerReplay -v
```
Expected: FAIL - `run()` method not implemented.

**Step 3: Implement the full replay engine**

Add the `run()` method and helper methods to `scenario_runner.py`. The `run()` method needs to:

1. Create an AppState + Flask app with empty network
2. Replay layout via StepPlayer
3. Create SimulationController
4. Iterate actions in order, advancing simulation time between them
5. At checkpoints, evaluate assertions using `build_network_payload`
6. For API actions, replay via Flask test client

Add these imports at top of `scenario_runner.py`:
```python
import threading
from datetime import datetime

from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController, build_network_payload
from persistence import PersistenceManager
from speed_profile import SpeedProfileRegistry
from step_player import StepPlayer
from app_state import AppState
from app_factory import create_app
```

Add `run()` method to `ScenarioRunner`:
```python
    def run(self) -> ScenarioResult:
        """Run the scenario: setup layout, replay actions, check assertions.

        Returns:
            ScenarioResult with checkpoint outcomes and any errors.
        """
        result = ScenarioResult(name=self.data.get("name", "unnamed"))

        # 1. Bootstrap runtime
        network = RailwayNetwork()
        path_dispatcher = PathDispatcher(network)
        sim_controller = SimulationController(network, path_dispatcher)
        persistence_manager = PersistenceManager()
        speed_registry = SpeedProfileRegistry()

        state = AppState(
            network=network,
            path_dispatcher=path_dispatcher,
            sim_controller=sim_controller,
            persistence_manager=persistence_manager,
            speed_profile_registry=speed_registry,
            simulation_lock=threading.Lock(),
            simulation_running=False,
            simulation_speed=1.0,
            last_update_time=datetime.now(),
        )

        app = create_app(state)
        client = app.test_client()

        # 2. Rebuild layout
        layout_steps = self.data.get("layout", [])
        if layout_steps:
            player = StepPlayer()
            play_result = player.play_steps(layout_steps, network)
            if not play_result.get("success"):
                result.errors.append(f"Layout setup failed: {play_result.get('errors', [])}")
                return result

        # 3. Replay actions
        sim_time = 0.0
        actions = self.data.get("actions", [])

        with app.app_context():
            for action in actions:
                target_time = action.get("t", 0.0)

                # Advance simulation to target time
                while sim_time < target_time - self.TICK_SIZE / 2:
                    sim_controller.update(self.TICK_SIZE)
                    sim_time += self.TICK_SIZE

                if action.get("action") == "checkpoint":
                    checkpoint = self._evaluate_checkpoint(
                        action, network, sim_controller
                    )
                    result.checkpoints.append(checkpoint)
                else:
                    # Replay raw API call
                    method = action.get("method", "POST")
                    url = action.get("url", "")
                    body = action.get("body")
                    try:
                        client.open(url, method=method, json=body)
                    except Exception as e:
                        result.errors.append(
                            f"Action at t={target_time}s failed: {e}"
                        )

        result.simulation_time = sim_time
        return result

    def _evaluate_checkpoint(
        self,
        action: Dict[str, Any],
        network: "RailwayNetwork",
        sim_controller: "SimulationController",
    ) -> CheckpointResult:
        """Evaluate all assertions at a checkpoint.

        Args:
            action: The checkpoint action dict with assertions.
            network: Current network state.
            sim_controller: Current simulation controller.

        Returns:
            CheckpointResult with individual assertion outcomes.
        """
        payload = build_network_payload(network, sim_controller)
        checkpoint = CheckpointResult(timestamp=action.get("t", 0.0))

        for assertion in action.get("assertions", []):
            path = assertion.get("path", "")
            op = assertion.get("op", "==")
            expected = assertion.get("expected")
            actual = self._resolve_path(payload, path)
            passed = self._compare(actual, op, expected)

            checkpoint.assertions.append(AssertionResult(
                path=path,
                op=op,
                expected=expected,
                actual=actual,
                passed=passed,
                message="" if passed else f"{path} {op} {expected}, got {actual}",
            ))

        return checkpoint
```

Also add `simulation_time` field to `ScenarioResult`:
```python
@dataclass
class ScenarioResult:
    name: str
    checkpoints: List[CheckpointResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    simulation_time: float = 0.0
```

**Step 4: Run tests to verify they pass**

```bash
conda run -n general pytest tests/test_scenario_runner.py -v
```
Expected: All tests PASS.

**Step 5: Commit**

```bash
git add scenario_runner.py tests/test_scenario_runner.py
git commit -m "feat: implement scenario runner replay engine with Flask test client"
```

---

### Task 3: API Endpoint - GET /api/network/steps

**Files:**
- Modify: `routes/misc.py` (add endpoint after line 56)
- Modify: `static/js/api.js` (add `getNetworkSteps()` method)
- Modify: `tests/test_scenario_runner.py` (add API test)

**Step 1: Write failing test for the endpoint**

Add to `tests/test_scenario_runner.py`:

```python
class TestNetworkStepsEndpoint:
    """Tests for GET /api/network/steps API endpoint."""

    def _create_test_app(self):
        """Create a Flask test app with a simple network."""
        import threading
        from datetime import datetime
        from network import RailwayNetwork
        from track import TrackSegment, TRACK_EW
        from path_dispatcher import PathDispatcher
        from simulation import SimulationController
        from persistence import PersistenceManager
        from speed_profile import SpeedProfileRegistry
        from app_state import AppState
        from app_factory import create_app

        network = RailwayNetwork()
        network.add_track(TrackSegment(0, 0, TRACK_EW))
        network.add_track(TrackSegment(1, 0, TRACK_EW))

        pd = PathDispatcher(network)
        sim = SimulationController(network, pd)
        pm = PersistenceManager()
        sr = SpeedProfileRegistry()

        state = AppState(
            network=network, path_dispatcher=pd, sim_controller=sim,
            persistence_manager=pm, speed_profile_registry=sr,
            simulation_lock=threading.Lock(), simulation_running=False,
            simulation_speed=1.0, last_update_time=datetime.now(),
        )
        app = create_app(state)
        return app

    def test_returns_steps_list(self):
        app = self._create_test_app()
        client = app.test_client()
        resp = client.get("/api/network/steps")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "steps" in data
        assert isinstance(data["steps"], list)
        assert len(data["steps"]) > 0  # Should have at least track steps

    def test_steps_contain_track_operations(self):
        app = self._create_test_app()
        client = app.test_client()
        resp = client.get("/api/network/steps")
        data = resp.get_json()
        steps = data["steps"]
        # First step should be a track_group
        assert steps[0]["type"] == "track_group"
        ops = steps[0]["operations"]
        assert any(op["op"] == "add_track" for op in ops)
```

**Step 2: Run tests to verify they fail**

```bash
conda run -n general pytest tests/test_scenario_runner.py::TestNetworkStepsEndpoint -v
```
Expected: 404 - endpoint doesn't exist.

**Step 3: Add endpoint to routes/misc.py**

In `routes/misc.py`, after the simulation control endpoint (line 56), add:

```python
    @app.route("/api/network/steps", methods=["GET"])
    def get_network_steps():
        """Return current network layout as StepRecorder steps.

        Used by the frontend scenario recorder to embed the layout
        in scenario files for self-contained replay.
        """
        from step_recorder import StepRecorder
        recorder = StepRecorder()
        steps = recorder.generate_steps_list(state.network)
        return jsonify({"steps": steps})
```

**Step 4: Add `getNetworkSteps()` to api.js**

In `static/js/api.js`, after the `getNetwork()` method (around line 67), add:

```javascript
    /**
     * Get current network layout as StepRecorder steps.
     * Used by the scenario recorder to embed layout in scenario files.
     */
    async getNetworkSteps() {
        const data = await this._request('/network/steps', {}, 'getNetworkSteps');
        return data?.steps || [];
    }
```

**Step 5: Run tests to verify they pass**

```bash
conda run -n general pytest tests/test_scenario_runner.py::TestNetworkStepsEndpoint -v
```
Expected: PASS.

**Step 6: Commit**

```bash
git add routes/misc.py static/js/api.js tests/test_scenario_runner.py
git commit -m "feat: add GET /api/network/steps endpoint for scenario recorder"
```

---

### Task 4: Frontend Activity Recorder

**Files:**
- Create: `static/js/scenario-recorder.js`

**Step 1: Create the ScenarioRecorder class**

Create `static/js/scenario-recorder.js`:

```javascript
/**
 * ScenarioRecorder - Records user API interactions for test scenario generation.
 *
 * Intercepts mutation API calls (POST/PUT/DELETE/PATCH) made through the API class,
 * records them with relative timestamps, and allows exporting as scenario JSON files
 * that can be replayed by the backend ScenarioRunner.
 *
 * Usage:
 *   const recorder = new ScenarioRecorder(window.api);
 *   await recorder.startRecording();
 *   // ... user interacts with the app ...
 *   await recorder.checkpoint();  // capture state snapshot
 *   recorder.stopRecording();
 *   recorder.saveScenario('my-test', 'Description of what this tests');
 */
class ScenarioRecorder {
    constructor(api) {
        this.api = api;
        this.recording = false;
        this.actions = [];
        this.layout = null;
        this.startTime = null;
        this.checkpointCount = 0;
        this._originalRequest = null;
    }

    /**
     * Start recording. Captures the current layout and begins intercepting API calls.
     */
    async startRecording() {
        // Capture current layout as steps
        try {
            this.layout = await this.api.getNetworkSteps();
        } catch (e) {
            console.error('[ScenarioRecorder] Failed to capture layout steps:', e);
            this.layout = [];
        }

        this.startTime = Date.now();
        this.actions = [];
        this.checkpointCount = 0;
        this.recording = true;
        this._wrapApi();
        console.log('[ScenarioRecorder] Recording started');
    }

    /**
     * Stop recording. Restores the original API request method.
     */
    stopRecording() {
        this.recording = false;
        this._unwrapApi();
        console.log(`[ScenarioRecorder] Recording stopped. ${this.actions.length} actions captured.`);
    }

    /**
     * Capture a checkpoint - fetches current network state as a snapshot.
     * User adds assertions to the checkpoint later by editing the JSON.
     */
    async checkpoint() {
        if (!this.recording) {
            console.warn('[ScenarioRecorder] Not recording, cannot create checkpoint');
            return;
        }
        const t = (Date.now() - this.startTime) / 1000;
        this.checkpointCount++;
        try {
            const snapshot = await this.api._originalGetNetwork
                ? await this.api._originalGetNetwork()
                : await this._fetchNetworkDirect();
            this.actions.push({
                t,
                action: 'checkpoint',
                label: `Checkpoint ${this.checkpointCount}`,
                snapshot,
                assertions: []
            });
            console.log(`[ScenarioRecorder] Checkpoint ${this.checkpointCount} at t=${t.toFixed(1)}s`);
        } catch (e) {
            console.error('[ScenarioRecorder] Checkpoint failed:', e);
        }
    }

    /**
     * Fetch network state directly (bypasses interception).
     */
    async _fetchNetworkDirect() {
        const resp = await fetch('/api/network');
        return await resp.json();
    }

    /**
     * Export the recorded scenario as a JSON object.
     */
    exportScenario(name, description) {
        return {
            version: '1.0',
            format: 'scenario',
            name: name || 'Untitled Scenario',
            description: description || '',
            created: new Date().toISOString(),
            layout: this.layout || [],
            actions: this.actions
        };
    }

    /**
     * Save the scenario as a downloadable JSON file.
     */
    saveScenario(name, description) {
        const scenario = this.exportScenario(name, description);
        const json = JSON.stringify(scenario, null, 2);
        const blob = new Blob([json], { type: 'application/json' });
        const url = URL.createObjectURL(blob);

        const a = document.createElement('a');
        const filename = (name || 'scenario').toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '');
        a.href = url;
        a.download = `${filename}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        console.log(`[ScenarioRecorder] Scenario saved as ${filename}.json`);
    }

    /**
     * Wrap the API._request method to intercept mutation calls.
     */
    _wrapApi() {
        if (this._originalRequest) return; // Already wrapped
        this._originalRequest = this.api._request.bind(this.api);

        const recorder = this;
        this.api._request = async function(endpoint, options = {}, caller = 'request') {
            const method = (options.method || 'GET').toUpperCase();

            // Call the original
            const result = await recorder._originalRequest(endpoint, options, caller);

            // Record mutations only (not GET polling)
            if (recorder.recording && method !== 'GET') {
                const t = (Date.now() - recorder.startTime) / 1000;
                let body = options.body;
                // Parse body back from string if needed
                if (typeof body === 'string') {
                    try { body = JSON.parse(body); } catch (e) { /* keep as string */ }
                }
                recorder.actions.push({
                    t,
                    method,
                    url: `/api${endpoint}`,
                    body: body || null,
                    caller
                });
            }

            return result;
        };
    }

    /**
     * Restore the original API._request method.
     */
    _unwrapApi() {
        if (this._originalRequest) {
            this.api._request = this._originalRequest;
            this._originalRequest = null;
        }
    }

    /**
     * Get recording status info for UI display.
     */
    getStatus() {
        if (!this.recording) {
            return { recording: false, duration: 0, actionCount: 0, checkpointCount: 0 };
        }
        return {
            recording: true,
            duration: (Date.now() - this.startTime) / 1000,
            actionCount: this.actions.filter(a => !a.action).length,
            checkpointCount: this.checkpointCount
        };
    }

    /**
     * Whether there is a completed (stopped) recording available to save.
     */
    get hasPendingRecording() {
        return !this.recording && this.actions.length > 0;
    }
}
```

**Step 2: Run JavaScript syntax validation**

```bash
conda run -n general pytest tests/test_javascript_syntax.py -v
```
Expected: PASS (new file should have valid syntax).

**Step 3: Commit**

```bash
git add static/js/scenario-recorder.js
git commit -m "feat: add frontend ScenarioRecorder class with API interception"
```

---

### Task 5: UI Integration - HTML Buttons, CSS, App Wiring

**Files:**
- Modify: `static/index.html` (add script tag + recorder buttons)
- Modify: `static/js/app.js` (instantiate recorder, wire buttons)
- Modify: `static/css/components/common.css` (add recorder button styles)

**Step 1: Add recorder buttons to index.html**

In `static/index.html`, after the `simulation-controls` div (after line 31, before the `mode-switch` div), add a new `recorder-controls` div:

```html
                <div class="recorder-controls">
                    <button id="btn-record" class="btn btn-outline btn-record" title="Record Scenario">
                        <span class="record-dot"></span> Record
                    </button>
                    <button id="btn-checkpoint" class="btn btn-outline" title="Add Checkpoint" disabled>
                        ⚑ Checkpoint
                    </button>
                    <button id="btn-save-scenario" class="btn btn-outline" title="Save Scenario" disabled>
                        💾 Save Scenario
                    </button>
                </div>
```

Add the script tag before `app.js` (before line 362):

```html
    <script src="js/scenario-recorder.js?v=15"></script>
```

**Step 2: Add CSS for recorder buttons**

Add to `static/css/components/common.css` (at end of file):

```css
/* Scenario Recorder Controls */
.recorder-controls {
    display: flex;
    align-items: center;
    gap: 8px;
    padding-left: 16px;
    border-left: 1px solid var(--border-color);
}

.recorder-controls .btn {
    font-size: 0.8rem;
    padding: 4px 12px;
}

.record-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-secondary);
    margin-right: 4px;
    vertical-align: middle;
}

.btn-record.recording .record-dot {
    background: #ff4444;
    animation: record-pulse 1s ease-in-out infinite;
}

.btn-record.recording {
    border-color: #ff4444;
    color: #ff4444;
}

@keyframes record-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
}
```

**Step 3: Wire up in app.js**

In `static/js/app.js`, in the constructor (after line 5, after `this.trainMonitor = new TrainMonitor();`), add:

```javascript
        this.scenarioRecorder = new ScenarioRecorder(window.api);
```

In the constructor (after line 16, after `this.setupControls();`), add:

```javascript
        this.setupRecorderControls();
```

Add a new method `setupRecorderControls()` to the App class:

```javascript
    setupRecorderControls() {
        const recordBtn = document.getElementById('btn-record');
        const checkpointBtn = document.getElementById('btn-checkpoint');
        const saveBtn = document.getElementById('btn-save-scenario');

        if (!recordBtn) return; // Buttons not in DOM

        recordBtn.addEventListener('click', async () => {
            if (this.scenarioRecorder.recording) {
                // Stop recording
                this.scenarioRecorder.stopRecording();
                recordBtn.classList.remove('recording');
                recordBtn.querySelector('.record-dot').nextSibling.textContent = ' Record';
                checkpointBtn.disabled = true;
                saveBtn.disabled = !this.scenarioRecorder.hasPendingRecording;
                window.showToast?.('Recording stopped', 'info', 2000);
            } else {
                // Start recording
                await this.scenarioRecorder.startRecording();
                recordBtn.classList.add('recording');
                recordBtn.querySelector('.record-dot').nextSibling.textContent = ' Stop';
                checkpointBtn.disabled = false;
                saveBtn.disabled = true;
                window.showToast?.('Recording started - actions are being captured', 'info', 2000);
            }
        });

        checkpointBtn.addEventListener('click', async () => {
            await this.scenarioRecorder.checkpoint();
            const status = this.scenarioRecorder.getStatus();
            window.showToast?.(`Checkpoint ${status.checkpointCount} added`, 'success', 1500);
        });

        saveBtn.addEventListener('click', () => {
            const name = prompt('Scenario name:', 'My Scenario');
            if (!name) return;
            const description = prompt('Description (optional):', '');
            this.scenarioRecorder.saveScenario(name, description || '');
            saveBtn.disabled = true;
            window.showToast?.('Scenario file downloaded', 'success', 2000);
        });
    }
```

**Step 4: Run all three mandatory test suites**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```
Expected: All PASS.

**Step 5: Commit**

```bash
git add static/index.html static/js/app.js static/css/components/common.css
git commit -m "feat: add scenario recorder UI with Record, Checkpoint, and Save buttons"
```

---

### Task 6: Pytest Scenario Discovery + Sample Scenario

**Files:**
- Create: `tests/test_scenarios.py`
- Create: `tests/scenarios/` (directory)
- Create: `tests/scenarios/sample-block-state.json` (sample scenario)
- Modify: `tests/test_backend.py` (add import)

**Step 1: Create scenario directory and sample file**

Create `tests/scenarios/sample-block-state.json`:

```json
{
    "version": "1.0",
    "format": "scenario",
    "name": "Block starts free",
    "description": "Verify that a freshly created block has 'free' state",
    "created": "2026-03-31T00:00:00Z",
    "layout": [
        {
            "id": 1,
            "type": "track_group",
            "description": "Tracks",
            "operations": [
                {"op": "add_track", "x": 0, "y": 0, "track_bits": 2},
                {"op": "add_track", "x": 1, "y": 0, "track_bits": 2},
                {"op": "add_track", "x": 2, "y": 0, "track_bits": 2}
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
                    "positions": [[0, 0], [1, 0], [2, 0]]
                }
            ]
        }
    ],
    "actions": [
        {
            "t": 0.0,
            "action": "checkpoint",
            "assertions": [
                {"path": "blocks.B1.state", "op": "==", "expected": "free"},
                {"path": "blocks.B1.name", "op": "==", "expected": "Block 1"}
            ]
        }
    ]
}
```

**Step 2: Create tests/test_scenarios.py**

```python
"""
Scenario-based Tests - Auto-discovers and runs recorded scenario files.

Scenario files are JSON files in tests/scenarios/ that contain:
- Layout setup steps (reuses StepRecorder format)
- Recorded API actions (raw HTTP calls)
- Checkpoint assertions (dot-path based)

Run with: conda run -n general pytest tests/test_scenarios.py -v

To add a new scenario:
1. Record using the UI (Record button) or write JSON manually
2. Add assertions to checkpoint entries
3. Place the .json file in tests/scenarios/
4. It will be auto-discovered on next test run
"""
import glob
import os
import pytest

from scenario_runner import ScenarioRunner

SCENARIO_DIR = os.path.join(os.path.dirname(__file__), "scenarios")


def discover_scenarios():
    """Find all scenario JSON files in the scenarios directory."""
    if not os.path.isdir(SCENARIO_DIR):
        return []
    return sorted(glob.glob(os.path.join(SCENARIO_DIR, "*.json")))


scenario_files = discover_scenarios()


@pytest.mark.parametrize(
    "scenario_file",
    scenario_files,
    ids=lambda f: os.path.basename(f).replace(".json", ""),
)
def test_scenario(scenario_file: str) -> None:
    """Run a recorded scenario and verify all checkpoint assertions pass."""
    runner = ScenarioRunner(scenario_file)
    result = runner.run()

    # No errors during setup or replay
    assert not result.errors, f"Scenario '{result.name}' errors: {result.errors}"

    # All checkpoint assertions pass
    for i, checkpoint in enumerate(result.checkpoints):
        for assertion in checkpoint.assertions:
            assert assertion.passed, (
                f"Scenario '{result.name}' checkpoint {i + 1} "
                f"(t={checkpoint.timestamp}s): "
                f"{assertion.message}"
            )
```

**Step 3: Add import to test_backend.py**

In `tests/test_backend.py`, add at the end of imports:
```python
from tests.test_scenario_runner import *
```

**Step 4: Run all tests**

```bash
conda run -n general pytest tests/test_scenarios.py tests/test_scenario_runner.py -v
```
Expected: All PASS including the sample scenario.

**Step 5: Run full mandatory test suites**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```
Expected: All PASS.

**Step 6: Commit**

```bash
git add tests/test_scenarios.py tests/scenarios/sample-block-state.json tests/test_backend.py tests/test_scenario_runner.py
git commit -m "feat: add pytest scenario discovery and sample scenario file"
```

---

### Task 7: Final Integration Test + Verification

**Step 1: Run the full test suite**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py tests/test_scenarios.py tests/test_scenario_runner.py -v
```

**Step 2: Verify the server starts**

```bash
python3 server.py &
# Wait for startup
sleep 5
curl -s http://localhost:5000/api/network/steps | python3 -m json.tool | head -20
kill %1
```

**Step 3: Verify JS loads in browser context (syntax check)**

```bash
source ~/.nvm/nvm.sh && nvm use 22 && node -e "
const fs = require('fs');
const code = fs.readFileSync('static/js/scenario-recorder.js', 'utf-8');
new Function(code); // Checks syntax
console.log('scenario-recorder.js: valid syntax');
"
```

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete scenario recorder system - record, replay, and test user interactions"
```
