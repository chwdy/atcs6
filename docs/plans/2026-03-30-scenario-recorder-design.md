# Scenario Recorder & Test Runner Design

**Date:** 2026-03-30  
**Status:** Design approved, pending implementation

## Problem

Testing train behavior (e.g., "train stops at end of platform") requires writing programmatic Python tests that manually construct networks, run simulation loops, and assert outcomes. This is time-consuming and requires backend knowledge. There is no way to capture real user interactions and convert them into replayable regression tests.

## Solution

A three-component system:

1. **Activity Recorder** (frontend) -- captures raw API calls during a user session
2. **Scenario File** (JSON) -- stores the layout, recorded actions, and user-defined assertions
3. **Scenario Runner** (backend) -- replays scenarios and evaluates assertions, integrated with pytest

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (Frontend)                                         │
│                                                             │
│  ┌──────────────┐    intercepts    ┌─────────────────────┐  │
│  │  API class    │───────────────→│  ActivityRecorder     │  │
│  │  (api.js)     │                │  (activity-recorder.js)│  │
│  └──────────────┘                └─────────────────────────┘  │
│                                          │                    │
│                                          ▼                    │
│                                   scenario.json               │
│                                   (downloaded)                │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Backend (Testing)                                          │
│                                                             │
│  tests/scenarios/my-scenario.json                           │
│         │                                                   │
│         ▼                                                   │
│  ┌──────────────────┐                                       │
│  │  ScenarioRunner   │                                      │
│  │  (scenario_runner) │                                     │
│  │                    │                                     │
│  │  1. StepPlayer     │──→ rebuild layout                   │
│  │  2. Flask test     │──→ replay API calls                 │
│  │     client         │                                     │
│  │  3. SimController  │──→ advance simulation time          │
│  │  4. Assertions     │──→ evaluate checkpoints             │
│  └──────────────────┘                                       │
│         │                                                   │
│         ▼                                                   │
│  tests/test_scenarios.py (pytest parametrize)               │
└─────────────────────────────────────────────────────────────┘
```

## Scenario File Format

```json
{
  "version": "1.0",
  "format": "scenario",
  "name": "Train stops at platform end",
  "description": "Verify train decelerates and stops at platform boundary",
  "created": "2026-03-30T12:00:00Z",
  "layout": [
    // StepRecorder-format steps to rebuild the network
    // Embedded via GET /api/network/steps at recording start
  ],
  "actions": [
    {
      "t": 0.0,
      "method": "POST",
      "url": "/api/simulation/control",
      "body": {"action": "start"}
    },
    {
      "t": 0.5,
      "method": "POST",
      "url": "/api/trains/T1/ato",
      "body": {"state": "running"}
    },
    {
      "t": 3.1,
      "method": "PUT",
      "url": "/api/tracks/2/3",
      "body": {"switch_state": 1}
    },
    {
      "t": 8.2,
      "action": "checkpoint",
      "snapshot": { /* full network state at capture time (reference) */ },
      "assertions": [
        {"path": "trains.T1.speed_kmh", "op": "==", "expected": 0},
        {"path": "trains.T1.current_block_id", "op": "==", "expected": "B3"},
        {"path": "blocks.B2.state", "op": "==", "expected": "free"}
      ]
    },
    {
      "t": 12.0,
      "action": "checkpoint",
      "snapshot": {},
      "assertions": [
        {"path": "trains.T1.speed_kmh", "op": ">", "expected": 0}
      ]
    }
  ]
}
```

### Action Types

**API calls** (recorded directly from frontend):
- `method`: HTTP method (POST, PUT, DELETE)
- `url`: API endpoint path
- `body`: Request body (JSON)
- `t`: Relative timestamp in seconds from recording start

**Checkpoints** (user-triggered snapshots):
- `action`: "checkpoint"
- `snapshot`: Full network state at capture time (for reference/debugging)
- `assertions`: List of assertions to evaluate during replay
- `t`: Relative timestamp

### Assertion Format

```json
{"path": "<dot-path>", "op": "<operator>", "expected": <value>}
```

- **path**: Dot-notation path into `build_network_payload()` output
  - `trains.<id>.speed_kmh`, `trains.<id>.current_block_id`
  - `blocks.<id>.state`, `signals.<id>.aspect`
- **op**: `==`, `!=`, `<`, `>`, `<=`, `>=`
- **expected**: The expected value
- Float comparison uses tolerance (~0.1 for speed, ~1.0 for distance)

## Component 1: Activity Recorder (Frontend)

**File:** `static/js/activity-recorder.js`

### Responsibilities

- Intercept API class method calls during recording
- Log mutations as `{t, method, url, body}` entries
- Filter out read-only operations (GET requests, polling)
- Capture checkpoints (fetch `/api/network` snapshot)
- Embed layout steps at recording start (via `GET /api/network/steps`)
- Export scenario as downloadable JSON

### Recording Logic

```javascript
class ActivityRecorder {
    constructor(api) {
        this.api = api;
        this.recording = false;
        this.actions = [];
        this.layout = null;
        this.startTime = null;
    }

    async startRecording() {
        // Capture current layout as steps
        this.layout = await this.api.getNetworkSteps();
        this.startTime = Date.now();
        this.actions = [];
        this.recording = true;
        // Wrap API to intercept calls
        this._wrapApi();
    }

    recordAction(method, url, body) {
        if (!this.recording) return;
        const t = (Date.now() - this.startTime) / 1000;
        this.actions.push({ t, method, url, body });
    }

    async checkpoint() {
        const t = (Date.now() - this.startTime) / 1000;
        const snapshot = await this.api.getNetwork();
        this.actions.push({
            t,
            action: "checkpoint",
            snapshot,
            assertions: []  // User fills in later
        });
    }

    stopRecording() {
        this.recording = false;
        this._unwrapApi();
    }

    exportScenario(name, description) {
        return {
            version: "1.0",
            format: "scenario",
            name,
            description,
            created: new Date().toISOString(),
            layout: this.layout,
            actions: this.actions
        };
    }
}
```

### API Interception

Wrap existing API methods to call `recordAction()` before/after the real call. Only intercept methods that mutate state (POST, PUT, DELETE endpoints).

### UI Elements

Three buttons added to the simulation toolbar:

| Button | Visibility | Action |
|--------|-----------|--------|
| **Record** (toggle) | Always | Start/stop recording. Red indicator when active. |
| **Checkpoint** | During recording | Capture state snapshot at current moment. |
| **Save Scenario** | After recording stops | Prompt for name/description, download JSON. |

## Component 2: Scenario Runner (Backend)

**File:** `scenario_runner.py`

### Responsibilities

- Load scenario JSON files
- Rebuild layout using existing `StepPlayer`
- Replay API calls using Flask test client
- Advance simulation between actions
- Evaluate assertions at checkpoints
- Report pass/fail results

### Core Logic

```python
from dataclasses import dataclass, field
from typing import List, Any, Optional
import json

from app_factory import create_app
from step_player import StepPlayer
from simulation import SimulationController


@dataclass
class AssertionResult:
    path: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    message: str = ""


@dataclass
class CheckpointResult:
    timestamp: float
    assertions: List[AssertionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(a.passed for a in self.assertions)


@dataclass
class ScenarioResult:
    name: str
    checkpoints: List[CheckpointResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors and all(c.passed for c in self.checkpoints)


class ScenarioRunner:
    TICK_SIZE = 0.05  # 50ms simulation ticks (matches existing tests)
    FLOAT_TOLERANCE = 0.5  # Tolerance for float comparisons

    def __init__(self, scenario_path: str):
        with open(scenario_path) as f:
            self.data = json.load(f)
        self.app = create_app()
        self.client = self.app.test_client()

    def run(self) -> ScenarioResult:
        result = ScenarioResult(name=self.data.get("name", "unnamed"))

        with self.app.app_context():
            # 1. Rebuild layout
            network = self._get_network()
            player = StepPlayer()
            play_result = player.play_steps(self.data["layout"], network)
            if not play_result.get("success"):
                result.errors.append(f"Layout setup failed: {play_result}")
                return result

            # 2. Replay actions
            sim = self._get_simulation()
            sim_time = 0.0

            for action in self.data["actions"]:
                target_time = action["t"]

                # Advance simulation
                while sim_time < target_time - self.TICK_SIZE / 2:
                    sim.update(self.TICK_SIZE)
                    sim_time += self.TICK_SIZE

                if action.get("action") == "checkpoint":
                    checkpoint = self._evaluate_checkpoint(action, network)
                    result.checkpoints.append(checkpoint)
                else:
                    # Replay raw API call
                    self.client.open(
                        action["url"],
                        method=action["method"],
                        json=action.get("body"),
                        content_type="application/json"
                    )

        return result

    def _evaluate_checkpoint(
        self, action: dict, network
    ) -> CheckpointResult:
        from simulation import build_network_payload
        state = build_network_payload(network, ...)
        checkpoint = CheckpointResult(timestamp=action["t"])

        for assertion in action.get("assertions", []):
            actual = self._resolve_path(state, assertion["path"])
            passed = self._compare(actual, assertion["op"], assertion["expected"])
            checkpoint.assertions.append(AssertionResult(
                path=assertion["path"],
                op=assertion["op"],
                expected=assertion["expected"],
                actual=actual,
                passed=passed,
                message="" if passed else
                    f'{assertion["path"]} {assertion["op"]} {assertion["expected"]}, got {actual}'
            ))

        return checkpoint

    def _resolve_path(self, data: dict, path: str) -> Any:
        """Resolve dot-notation path: 'trains.T1.speed_kmh' -> value"""
        parts = path.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                current = current[int(part)]
            else:
                return None
        return current

    def _compare(self, actual: Any, op: str, expected: Any) -> bool:
        if actual is None:
            return False
        if isinstance(actual, float) or isinstance(expected, float):
            if op == "==":
                return abs(actual - expected) <= self.FLOAT_TOLERANCE
        ops = {
            "==": lambda a, e: a == e,
            "!=": lambda a, e: a != e,
            "<":  lambda a, e: a < e,
            ">":  lambda a, e: a > e,
            "<=": lambda a, e: a <= e,
            ">=": lambda a, e: a >= e,
        }
        return ops.get(op, lambda a, e: False)(actual, expected)
```

## Component 3: Pytest Integration

**File:** `tests/test_scenarios.py`

```python
import glob
import pytest
from scenario_runner import ScenarioRunner

SCENARIO_DIR = "tests/scenarios"

def discover_scenarios():
    return sorted(glob.glob(f"{SCENARIO_DIR}/*.json"))

@pytest.mark.parametrize("scenario_file", discover_scenarios(),
                         ids=lambda f: f.split("/")[-1].replace(".json", ""))
def test_scenario(scenario_file):
    """Run a recorded scenario and verify all checkpoint assertions."""
    runner = ScenarioRunner(scenario_file)
    result = runner.run()

    assert not result.errors, f"Scenario errors: {result.errors}"
    for checkpoint in result.checkpoints:
        for assertion in checkpoint.assertions:
            assert assertion.passed, (
                f"Checkpoint t={checkpoint.timestamp}s: "
                f"{assertion.message}"
            )
```

## New API Endpoint

**`GET /api/network/steps`** in `routes/misc.py`:

Returns the current network layout as StepRecorder steps. Used by the frontend recorder to embed the layout in the scenario file.

```python
@bp.route('/api/network/steps', methods=['GET'])
def get_network_steps():
    from step_recorder import StepRecorder
    recorder = StepRecorder()
    steps = recorder.generate_steps_list(state.network)
    return jsonify({"steps": steps})
```

## User Workflow

1. **Set up layout** in the editor (tracks, blocks, signals, trains)
2. **Switch to simulation mode**
3. **Click Record** -- layout is captured, recording starts
4. **Interact** -- start simulation, set ATO, change switches, etc.
5. **Click Checkpoint** at key moments (e.g., when train reaches a station)
6. **Click Stop Recording**
7. **Click Save Scenario** -- enter name and description, download JSON
8. **Edit JSON** -- add assertions to checkpoint entries
9. **Place JSON** in `tests/scenarios/`
10. **Run** `conda run -n general pytest tests/test_scenarios.py -v`

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `static/js/activity-recorder.js` | Create | Frontend recording logic |
| `scenario_runner.py` | Create | Backend replay + assertion engine |
| `tests/test_scenarios.py` | Create | Pytest auto-discovery integration |
| `tests/scenarios/` | Create dir | Directory for scenario JSON files |
| `routes/misc.py` | Modify | Add `GET /api/network/steps` endpoint |
| `static/js/api.js` | Modify | Add `getNetworkSteps()` method |
| `static/js/app.js` | Modify | Add recorder UI buttons + wiring |
| `static/index.html` | Modify | Add recorder buttons + load script |

## Design Decisions

1. **Raw API call recording** -- No action mapping layer. Records the actual HTTP method + URL + body. Replay uses Flask test client. Zero maintenance for new endpoints.

2. **Layout embedded in scenario** -- Each scenario is self-contained. Doesn't depend on external layout files. Can be replayed on any machine.

3. **Manual checkpoint + edit** -- User decides when to checkpoint. Assertions are added by editing JSON afterward. Keeps the recorder simple and the assertion format flexible.

4. **Frontend interceptor** -- Recording happens by wrapping the API class. No backend middleware needed. Clean separation.

5. **Flask test client for replay** -- No server needed. Fast, in-process. Same as existing test patterns.

6. **Float tolerance** -- Physics values use approximate comparison by default to avoid flaky tests from minor floating-point drift.
