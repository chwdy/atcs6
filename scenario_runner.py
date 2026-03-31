"""
Scenario Runner - Replay engine for recorded railway scenarios.

Definition:
This module provides data classes and a replay engine for scenario testing.
A scenario captures a layout, a sequence of API actions, and checkpoint
assertions.  The runner rebuilds the layout, replays the actions against a
Flask test client, advances the simulation clock, and evaluates assertions
at each checkpoint.

TODO List:
- [ ] Support "wait_for" actions that poll until a condition is met
- [ ] Add parallel scenario execution for test suites
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController, build_network_payload
from speed_profile import SpeedProfileRegistry
from step_player import StepPlayer


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AssertionResult:
    """Result of evaluating a single assertion against the network payload.

    Attributes:
        path: Dot-notation path into the payload (e.g. ``trains.T1.speed_kmh``).
        op: Comparison operator string (``==``, ``!=``, ``<``, ``>``, ``<=``, ``>=``).
        expected: The expected value from the scenario definition.
        actual: The actual value resolved from the network payload.
        passed: Whether the assertion passed.
        message: Human-readable description of the result.
    """

    path: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    message: str


@dataclass
class CheckpointResult:
    """Result of evaluating all assertions at a single checkpoint.

    Attributes:
        timestamp: Simulation time (seconds) at which the checkpoint was evaluated.
        assertions: Individual assertion results.
    """

    timestamp: float
    assertions: List[AssertionResult]

    @property
    def passed(self) -> bool:
        """Checkpoint passes when every assertion passes."""
        return all(a.passed for a in self.assertions)


@dataclass
class ScenarioResult:
    """Aggregate result of running an entire scenario.

    Attributes:
        name: Scenario name.
        checkpoints: Results from each checkpoint encountered.
        errors: Errors encountered during replay (e.g. layout build failures).
        simulation_time: Final simulation clock value in seconds.
    """

    name: str
    checkpoints: List[CheckpointResult]
    errors: List[str]
    simulation_time: float

    @property
    def passed(self) -> bool:
        """Scenario passes when all checkpoints pass and no errors occurred."""
        return all(c.passed for c in self.checkpoints) and len(self.errors) == 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ScenarioRunner:
    """Replays a recorded scenario and evaluates checkpoint assertions.

    Usage::

        runner = ScenarioRunner()
        result = runner.run(scenario_dict)
        assert result.passed
    """

    # Tolerance for float equality comparisons.
    FLOAT_TOLERANCE: float = 0.5

    # Simulation tick size used when advancing the clock.
    TICK_DELTA: float = 0.05

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve_path(self, data: Any, path: str) -> Any:
        """Resolve a dot-notation path against a nested data structure.

        The method walks the path segments left-to-right.  At each step it
        handles three cases:

        1. **dict** -- look up the segment as a key.
        2. **list** -- first try to find an element whose ``"id"`` field
           matches the segment; fall back to treating the segment as a
           numeric index.
        3. **other** -- return ``None`` (path cannot be resolved further).

        Args:
            data: The root data structure (typically the network payload dict).
            path: Dot-separated path string (e.g. ``"trains.T1.speed_kmh"``).

        Returns:
            The resolved value, or ``None`` if any segment cannot be resolved.
        """
        if not path:
            return data

        parts = path.split(".")
        current = data
        for part in parts:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                # Try id-based lookup first.
                found = None
                for item in current:
                    if isinstance(item, dict) and item.get("id") == part:
                        found = item
                        break
                if found is not None:
                    current = found
                else:
                    # Fall back to numeric index.
                    try:
                        current = current[int(part)]
                    except (ValueError, IndexError):
                        return None
            else:
                return None
        return current

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def _compare(self, actual: Any, op: str, expected: Any) -> bool:
        """Compare *actual* against *expected* using the given operator.

        For ``==`` and ``!=`` on float operands, a tolerance of
        :pyattr:`FLOAT_TOLERANCE` is applied.

        Args:
            actual: The value obtained from the network payload.
            op: Comparison operator string.
            expected: The value from the scenario assertion.

        Returns:
            ``True`` if the comparison holds, ``False`` otherwise.
        """
        # Float-tolerant equality.
        if (
            op == "=="
            and isinstance(actual, (int, float))
            and isinstance(expected, (int, float))
        ):
            return abs(float(actual) - float(expected)) <= self.FLOAT_TOLERANCE
        if (
            op == "!="
            and isinstance(actual, (int, float))
            and isinstance(expected, (int, float))
        ):
            return abs(float(actual) - float(expected)) > self.FLOAT_TOLERANCE

        ops = {
            "==": lambda a, e: a == e,
            "!=": lambda a, e: a != e,
            "<": lambda a, e: a < e,
            ">": lambda a, e: a > e,
            "<=": lambda a, e: a <= e,
            ">=": lambda a, e: a >= e,
        }
        fn = ops.get(op)
        if fn is None:
            return False
        try:
            return fn(actual, expected)
        except TypeError:
            # Incomparable types (e.g. None < 5).
            return False

    # ------------------------------------------------------------------
    # Assertion evaluation
    # ------------------------------------------------------------------

    def _evaluate_assertions(
        self,
        payload: Dict[str, Any],
        assertions: List[Dict[str, Any]],
    ) -> List[AssertionResult]:
        """Evaluate a list of assertion dicts against a network payload.

        Args:
            payload: Network payload dictionary.
            assertions: List of assertion definitions, each with keys
                ``path``, ``op``, and ``expected``.

        Returns:
            List of :class:`AssertionResult` instances.
        """
        results: List[AssertionResult] = []
        for assertion in assertions:
            path = assertion["path"]
            op = assertion["op"]
            expected = assertion["expected"]
            actual = self._resolve_path(payload, path)
            passed = self._compare(actual, op, expected)
            message = f"{path}: {actual!r} {op} {expected!r} -> {'PASS' if passed else 'FAIL'}"
            results.append(
                AssertionResult(
                    path=path,
                    op=op,
                    expected=expected,
                    actual=actual,
                    passed=passed,
                    message=message,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Replay engine
    # ------------------------------------------------------------------

    def run(self, scenario: Dict[str, Any]) -> ScenarioResult:
        """Run a complete scenario and return the result.

        Steps:
            1. Create an empty ``RailwayNetwork`` and supporting runtime
               objects.
            2. Replay layout steps via :class:`StepPlayer`.
            3. Create a Flask app + test client.
            4. Iterate actions in timestamp order, advancing the simulation
               clock as needed.  Checkpoint actions evaluate assertions;
               API actions are forwarded to the test client.
            5. Return a :class:`ScenarioResult`.

        Args:
            scenario: Scenario dictionary with keys ``name``, ``layout``,
                and ``actions``.

        Returns:
            :class:`ScenarioResult` summarising the replay.
        """
        name: str = scenario.get("name", "unnamed")
        layout_steps: List[Dict[str, Any]] = scenario.get("layout", [])
        actions: List[Dict[str, Any]] = scenario.get("actions", [])

        errors: List[str] = []
        checkpoints: List[CheckpointResult] = []

        # --- 1. Build runtime ---
        network = RailwayNetwork()
        path_dispatcher = PathDispatcher(network)
        sim_controller = SimulationController(network, path_dispatcher)
        persistence_manager = PersistenceManager()
        speed_profile_registry = SpeedProfileRegistry()

        state = AppState(
            network=network,
            path_dispatcher=path_dispatcher,
            sim_controller=sim_controller,
            persistence_manager=persistence_manager,
            speed_profile_registry=speed_profile_registry,
            simulation_lock=threading.Lock(),
            simulation_running=False,
            simulation_speed=1.0,
            last_update_time=datetime.now(),
        )

        app = create_app(state)

        # --- 2. Replay layout ---
        if layout_steps:
            player = StepPlayer()
            replay_result = player.play_steps(layout_steps, network, clear_first=True)
            if not replay_result["success"]:
                for err in replay_result.get("errors", []):
                    errors.append(
                        f"Layout step {err.get('step_id')}: {err.get('error')}"
                    )

        # --- 3. Replay actions ---
        with app.app_context():
            client = app.test_client()

            for action in actions:
                is_checkpoint = action.get("action") == "checkpoint"
                t = float(action.get("t", 0.0))

                # Advance simulation clock to the action's timestamp.
                while sim_controller.simulation_time < t:
                    remaining = t - sim_controller.simulation_time
                    tick = min(self.TICK_DELTA, remaining)
                    sim_controller.update(tick)

                if is_checkpoint:
                    payload = build_network_payload(network, sim_controller)
                    assertion_defs = action.get("assertions", [])
                    assertion_results = self._evaluate_assertions(
                        payload, assertion_defs
                    )
                    checkpoints.append(
                        CheckpointResult(
                            timestamp=t,
                            assertions=assertion_results,
                        )
                    )

                elif "method" in action:
                    method = action.get("method", "GET")
                    url = action.get("url", "/")
                    body = action.get("body")
                    try:
                        if body is not None:
                            client.open(url, method=method, json=body)
                        else:
                            client.open(url, method=method)
                    except Exception as exc:
                        errors.append(f"API {method} {url}: {exc}")

        return ScenarioResult(
            name=name,
            checkpoints=checkpoints,
            errors=errors,
            simulation_time=sim_controller.simulation_time,
        )
