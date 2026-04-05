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

    assert not result.errors, f"Scenario '{result.name}' errors: {result.errors}"

    for i, checkpoint in enumerate(result.checkpoints):
        for assertion in checkpoint.assertions:
            assert assertion.passed, (
                f"Scenario '{result.name}' checkpoint {i + 1} "
                f"(t={checkpoint.timestamp}s): "
                f"{assertion.message}"
            )
