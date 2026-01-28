"""
Application State Container

Definition:
This module provides the AppState dataclass which bundles shared runtime
objects for the Flask server and simulation.

TODO List:
- [ ] Move mutable runtime flags to AppState-backed access
- [ ] Add helpers for rebuilding demo vs empty state
"""

from dataclasses import dataclass
from datetime import datetime
import threading

from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from speed_profile import SpeedProfileRegistry


@dataclass
class AppState:
    """Shared runtime container for the server and simulation."""

    network: RailwayNetwork
    path_dispatcher: PathDispatcher
    sim_controller: SimulationController
    persistence_manager: PersistenceManager
    speed_profile_registry: SpeedProfileRegistry
    simulation_lock: threading.Lock
    simulation_running: bool
    simulation_speed: float
    last_update_time: datetime
    last_autosave_time: float = 0.0
