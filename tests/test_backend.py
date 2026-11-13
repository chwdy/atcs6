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
    "tests.test_train_runtime",
    "tests.test_running_trains_api",
]


@pytest.mark.parametrize("module_name", BACKEND_TEST_MODULES)
def test_backend_module_imports(module_name):
    """Backend test modules should stay importable for direct execution."""
    importlib.import_module(module_name)


if __name__ == "__main__":
    print("Run the whole backend suite with:  pytest")
