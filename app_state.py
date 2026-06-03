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
    simulation_lock: threading.Lock
    simulation_running: bool
    simulation_speed: float
    last_update_time: datetime
    last_autosave_time: float = 0.0

    @property
    def speed_profile_registry(self) -> SpeedProfileRegistry:
        """The single speed-profile store, owned by the network.

        Returns ``network.speed_profiles`` so the REST API, the services
        (``speed_profile_service`` / ``train_service``), and the running
        simulation (``TrainPhysics`` / ``TrainController``) all read and
        write one object. There is no separate copy to keep in sync.
        """
        return self.network.speed_profiles
