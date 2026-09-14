"""
Comprehensive tests for Speed Curves and ATO Behavior.

Tests the scenario:
- Block 1 (70 km/h): 3 tiles, 150cm at (0,0)-(2,0)
- Block 2 (40 km/h): 3 tiles, 150cm at (3,0)-(5,0)
- Block 3 (no limit): 3 tiles, 150cm at (6,0)-(8,0)
- Station (50 km/h): 100cm at (9,0)

Train: 80cm long, 60 km/h max speed

Expected behavior:
1. In block 1: ATC = 60 km/h (train max, since 70 > 60)
2. Before block 2: braking curve to 40 km/h
3. In block 2: ATC = 40 km/h
4. After block 2 (in block 3): ATC increases back to 60 km/h
5. Before station: braking curve to 50 km/h
6. In station: ATC = 50 km/h

Run with: conda run -n general pytest tests/test_speed_curve_debug.py -v -s
"""

import pytest
from typing import List, Tuple, Dict, Any, Optional

from track import Tile, TRACK_EW
from network import RailwayNetwork
from block import Block
from station import Station
from train import Train, TrainCar
from train_controller import TrainController, create_controller_for_train
from simulation import SimulationController
from train_physics import TrainPhysics
from enums import Direction, HandlePosition, BlockState




