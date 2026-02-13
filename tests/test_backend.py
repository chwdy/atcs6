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
- tests/test_trains_core.py - Core train tests (SimulationController, placement, position)
- tests/test_trains_occupancy.py - Train occupancy and validation
- tests/test_trains_cab.py - Cab switching tests
- tests/test_trains_physics.py - Train physics and movement
- tests/test_trains_controller.py - Train controller and ATC
- tests/test_trains_ato.py - ATO-specific tests
- tests/test_trains_lookahead.py - Monitor lookahead and UI data
- tests/test_trains_routes.py - Route execution and path assignment
"""

import pytest

# Import all test modules - pytest will discover and run all tests from these
from tests.test_track import *
from tests.test_blocks import *
from tests.test_signals import *
from tests.test_stations import *
from tests.test_pathfinding import *

# New modular train test files
from tests.test_trains_core import *
from tests.test_trains_occupancy import *
from tests.test_trains_cab import *
from tests.test_trains_physics import *
from tests.test_trains_controller import *
from tests.test_trains_ato import *
from tests.test_trains_lookahead import *
from tests.test_trains_routes import *


if __name__ == "__main__":
    print("Backend Test Suite - Modular Test Files")
    print("=" * 50)
    print("\nRun individual test categories:")
    print("  pytest tests/test_track.py -v")
    print("  pytest tests/test_blocks.py -v")
    print("  pytest tests/test_signals.py -v")
    print("  pytest tests/test_stations.py -v")
    print("  pytest tests/test_pathfinding.py -v")
    print("\nTrain tests (modular):")
    print("  pytest tests/test_trains_core.py -v")
    print("  pytest tests/test_trains_occupancy.py -v")
    print("  pytest tests/test_trains_cab.py -v")
    print("  pytest tests/test_trains_physics.py -v")
    print("  pytest tests/test_trains_controller.py -v")
    print("  pytest tests/test_trains_ato.py -v")
    print("  pytest tests/test_trains_lookahead.py -v")
    print("  pytest tests/test_trains_routes.py -v")
    print("\nOr run all tests:")
    print("  pytest tests/test_backend.py -v")
    print("  conda run -n general pytest tests/test_backend.py -v")
