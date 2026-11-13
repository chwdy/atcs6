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
import threading

from session_state import SessionState, derive_session_state

from network import RailwayNetwork
from persistence import PersistenceManager
from simulation import SimulationController


@dataclass
class AppState:
    """Shared runtime container for the server and simulation."""

    network: RailwayNetwork
    sim_controller: SimulationController
    persistence_manager: PersistenceManager
    simulation_lock: threading.Lock
    last_autosave_time: float = 0.0
    # Monotonic seconds of operation since process start (design_v2.md
    # "Operating environment and activation", `operating_time`). It lives here
    # rather than on `SimulationController` because loading a layout builds a
    # fresh controller and must not reset it; only a process restart does.
    operating_time: float = 0.0
    # Set while the session is in Setup: the operator is building and testing
    # the rig, and the Setup tools own the outputs (design_v2.md "Session
    # state").
    setup_active: bool = False

    def session_state(self) -> SessionState:
        """The session state, derived from the stored flags on every read
        (design_v2.md "Session state"). Every reader asks here rather than
        combining the flags itself."""
        return derive_session_state(
            self.sim_controller, setup_active=self.setup_active
        )
