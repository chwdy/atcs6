"""
Backend test index and import smoke checks.

Historically this file imported every test module with ``from ... import *``,
which caused pytest to collect the same tests twice. The real suite is already
discovered directly from the individual ``tests/test_*.py`` files.

This file now keeps the index documentation and adds a lightweight check that
the major backend test modules remain importable.
"""

import importlib

import pytest


BACKEND_TEST_MODULES = [
    "tests.test_track",
    "tests.test_blocks",
    "tests.test_signals",
    "tests.test_stations",
    "tests.test_trains_core",
    "tests.test_trains_occupancy",
    "tests.test_trains_cab",
    "tests.test_trains_physics",
    "tests.test_trains_controller",
    "tests.test_trains_ato",
    "tests.test_trains_lookahead",
    "tests.test_trains_routes",
]


@pytest.mark.parametrize("module_name", BACKEND_TEST_MODULES)
def test_backend_module_imports(module_name):
    """Backend test modules should stay importable for direct execution."""
    importlib.import_module(module_name)


if __name__ == "__main__":
    print("Backend Test Suite - Modular Test Files")
    print("=" * 50)
    print("\nRun individual test categories:")
    print("  pytest tests/test_track.py -v")
    print("  pytest tests/test_blocks.py -v")
    print("  pytest tests/test_signals.py -v")
    print("  pytest tests/test_stations.py -v")
    print("\nTrain tests (modular):")
    print("  pytest tests/test_trains_core.py -v")
    print("  pytest tests/test_trains_occupancy.py -v")
    print("  pytest tests/test_trains_cab.py -v")
    print("  pytest tests/test_trains_physics.py -v")
    print("  pytest tests/test_trains_controller.py -v")
    print("  pytest tests/test_trains_ato.py -v")
    print("  pytest tests/test_trains_lookahead.py -v")
    print("  pytest tests/test_trains_routes.py -v")
    print("\nOr run the whole backend suite directly:")
    print("  pytest")
