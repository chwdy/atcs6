"""
Scenario Runner - Replay recorded scenarios and evaluate assertions.

Definition:
This module provides the ScenarioRunner class which loads scenario JSON files,
rebuilds the layout using StepPlayer, replays recorded API calls via Flask test
client, advances simulation between actions, and evaluates assertions at
user-defined checkpoints.
"""

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List


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
    simulation_time: float = 0.0

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
                found = None
                for item in current:
                    if isinstance(item, dict) and item.get("id") == part:
                        found = item
                        break
                if found is not None:
                    current = found
                else:
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
        """
        if actual is None:
            return False
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

    def run(self) -> ScenarioResult:
        """Run the scenario: setup layout, replay actions, check assertions.

        Returns:
            ScenarioResult with checkpoint outcomes and any errors.
        """
        from app_factory import create_app
        from app_state import AppState
        from network import RailwayNetwork
        from path_dispatcher import PathDispatcher
        from persistence import PersistenceManager
        from simulation import SimulationController, build_network_payload
        from step_player import StepPlayer

        result = ScenarioResult(name=self.data.get("name", "unnamed"))

        # 1. Bootstrap runtime
        network = RailwayNetwork()
        path_dispatcher = PathDispatcher(network)
        sim_controller = SimulationController(network, path_dispatcher)
        if sim_controller.power_controller:
            sim_controller.power_controller._sim_controller = sim_controller
        persistence_manager = PersistenceManager()

        state = AppState(
            network=network,
            path_dispatcher=path_dispatcher,
            sim_controller=sim_controller,
            persistence_manager=persistence_manager,
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
                result.errors.append(
                    f"Layout setup failed: {play_result.get('errors', [])}"
                )
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
                        action, network, sim_controller, build_network_payload
                    )
                    result.checkpoints.append(checkpoint)
                else:
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
        network: Any,
        sim_controller: Any,
        build_network_payload: Any,
    ) -> CheckpointResult:
        """Evaluate all assertions at a checkpoint."""
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
