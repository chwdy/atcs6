"""
Backend API Tests - Test index file

Run with: pytest tests/test_backend.py -v

This file imports all test modules so running test_backend.py runs everything.
Individual test categories are in:
- tests/test_track.py - Track geometry and switches
- tests/test_blocks.py - Block state management and validation
- tests/test_signals.py - Signal creation and behavior
- tests/test_stations.py - Station creation and block integration
- tests/test_pathfinding.py - Pathfinding algorithms and persistence
- tests/test_trains.py - Train placement and movement
- tests/test_interlocking.py - Switch locking and path operations
"""

import pytest

# Import all test modules - pytest will discover and run all tests from these
from tests.test_track import *
from tests.test_blocks import *
from tests.test_signals import *
from tests.test_stations import *
from tests.test_pathfinding import *
from tests.test_trains import *
from tests.test_interlocking import *


if __name__ == "__main__":
    print("Backend Test Suite - Modular Test Files")
    print("=" * 50)
    print("\nRun individual test categories:")
    print("  pytest tests/test_track.py -v")
    print("  pytest tests/test_blocks.py -v")
    print("  pytest tests/test_signals.py -v")
    print("  pytest tests/test_stations.py -v")
    print("  pytest tests/test_pathfinding.py -v")
    print("  pytest tests/test_trains.py -v")
    print("  pytest tests/test_interlocking.py -v")
    print("\nOr run all tests:")
    print("  pytest tests/test_backend.py -v")
    print("  conda run -n general pytest tests/test_backend.py -v")
