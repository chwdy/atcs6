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

from session_state import SessionState, derive_session_state

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
    # Monotonic seconds of *operation* since process start — the clock a
    # schedule holds against (design_new.md "Clocks"). Each running pass of the
    # runtime loop adds that pass's wall delta multiplied by
    # `simulation_speed`; a stopped pass adds nothing, so an operator's stop
    # neither consumes a dwell nor makes a train late. It lives here rather
    # than on `SimulationController` because loading a layout builds a fresh
    # controller and must not reset it; only a process restart does.
    operating_time: float = 0.0
    # Set while the session is in Setup: the operator is building and testing
    # the rig, and the Setup tools own the outputs (design_new.md "Session
    # state", "Hardware setup", Setup).
    setup_active: bool = False

    def session_state(self) -> SessionState:
        """The session state, derived from the six stored flags on every read
        (design_new.md "Session state"). Every reader asks here rather than
        combining the flags itself."""
        return derive_session_state(
            self.sim_controller,
            setup_active=self.setup_active,
            simulation_running=self.simulation_running,
        )

    def __post_init__(self) -> None:
        """Hand the runtime the seams it reaches `AppState` through.

        The `operating_time` accumulator stays here — a layout load builds a
        fresh `SimulationController` and must not reset it — so the controller
        only ever reads the value, never owns it.
        """
        def _advance(delta_seconds: float) -> None:
            self.operating_time += max(delta_seconds, 0.0)

        try:
            self.sim_controller.operating_clock = lambda: self.operating_time
            self.sim_controller.operating_clock_advance = _advance
        except AttributeError:
            # A stand-in controller that accepts no attributes (a smoke-test
            # double) keeps its own fallback clock; see
            # `SimulationController.operating_clock`.
            pass

    @property
    def speed_profile_registry(self) -> SpeedProfileRegistry:
        """The single speed-profile store, owned by the network.

        Returns ``network.speed_profiles`` so the REST API, the services
        (``speed_profile_service`` / ``train_service``), and the running
        simulation (``TrainPhysics`` / ``TrainController``) all read and
        write one object. There is no separate copy to keep in sync.
        """
        return self.network.speed_profiles
