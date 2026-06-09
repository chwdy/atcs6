"""
Runtime Module - Background threads for simulation and autosave.

Definition:
This module provides functions to start and manage background threads
for simulation updates and periodic autosave.

TODO List:
- [ ] Add graceful shutdown support
- [ ] Add thread health monitoring
"""

import threading
import time
from datetime import datetime
from typing import Callable, Optional

from app_state import AppState

# Configuration
AUTOSAVE_INTERVAL = 300  # 5 minutes in seconds
AUTOSAVE_FILENAME = "_autosave"

# Hard ceiling on a single per-tick sim delta. Normal operation — including a
# few-times `simulation_speed` multiplier — stays well under this, so it is a
# pass-through in the common case and the loop runs ONE update() per tick. It
# only ever caps a multi-second hitch: the loop stalled (lock contention with
# request handlers, GC, a host that fell behind) and resumed with a huge
# accumulated wall-clock delta. Integrated in one step, such a delta overshoots
# the train's max speed and teleports the head past its authority, tripping a
# spurious ATC overspeed EB. Empirically the EB only appears around a ~3.5s
# step with a full demo trainset, so this sits below that with margin. When a
# hitch is clamped the surplus sim-time is dropped (the sim briefly runs slower
# than real time) rather than teleporting; sub-second steps are physically fine.
MAX_SIM_DELTA_SECONDS = 1.0


def _clamp_sim_delta(delta_seconds: float, simulation_speed: float) -> float:
    """Per-tick sim delta = wall-clock delta x speed, clamped to
    ``MAX_SIM_DELTA_SECONDS`` (and floored at 0 against clock skew)."""
    return min(
        max(delta_seconds, 0.0) * simulation_speed, MAX_SIM_DELTA_SECONDS
    )


def create_simulation_loop(state: AppState) -> Callable[[], None]:
    """Create a simulation loop function bound to the given state.

    Args:
        state: AppState containing simulation controller and lock.

    Returns:
        A function that runs the simulation loop indefinitely.
    """

    def simulation_loop() -> None:
        last_update_time = datetime.now()
        while True:
            try:
                current_time = datetime.now()
                delta_seconds = (current_time - last_update_time).total_seconds()
                last_update_time = current_time

                if state.simulation_running:
                    # Clamp the per-tick delta (see MAX_SIM_DELTA_SECONDS) so a
                    # multi-second hitch can't integrate one giant jump that
                    # spikes speed past max and trips a spurious overspeed EB.
                    # Normal ticks (incl. a few-x speed multiplier) pass through
                    # unchanged: one update() per tick.
                    sim_delta = _clamp_sim_delta(
                        delta_seconds, state.simulation_speed
                    )
                    with state.simulation_lock:
                        state.sim_controller.update(sim_delta)

                time.sleep(0.1)
            except Exception as e:
                print(f"[SimulationLoop] ERROR: {e}")
                import traceback

                traceback.print_exc()
                print("[SimulationLoop] Continuing despite error...")

    return simulation_loop


def create_autosave_loop(state: AppState) -> Callable[[], None]:
    """Create an autosave loop function bound to the given state.

    Reads and writes `state.last_autosave_time` so that manual saves
    (which also update that field) defer the next autosave by the
    full interval rather than racing the timer.

    Args:
        state: AppState containing persistence manager and lock.

    Returns:
        A function that runs the autosave loop indefinitely.
    """

    def autosave_loop() -> None:
        state.last_autosave_time = time.time()
        while True:
            time.sleep(60)  # Check every minute
            if time.time() - state.last_autosave_time >= AUTOSAVE_INTERVAL:
                with state.simulation_lock:
                    try:
                        result = state.persistence_manager.save_merged_layout(
                            AUTOSAVE_FILENAME, state.network, "Auto-saved"
                        )
                        state.last_autosave_time = time.time()
                        msg = f"[AutoSave] Saved at {datetime.now().isoformat()}"
                        if result.get("steps_error"):
                            msg += f" (steps failed: {result['steps_error']})"
                        print(msg)
                    except Exception as e:
                        print(f"[AutoSave] Failed: {e}")

    return autosave_loop


def start_simulation_thread(state: AppState) -> threading.Thread:
    """Start the simulation background thread.

    Args:
        state: AppState for simulation loop.

    Returns:
        The started thread.
    """
    loop = create_simulation_loop(state)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def start_autosave_thread(state: AppState) -> threading.Thread:
    """Start the autosave background thread.

    Args:
        state: AppState for autosave loop.

    Returns:
        The started thread.
    """
    loop = create_autosave_loop(state)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def start_all_threads(
    state: AppState,
    simulation: bool = True,
    autosave: bool = True,
) -> dict[str, Optional[threading.Thread]]:
    """Start all background threads based on flags.

    Args:
        state: AppState for thread loops.
        simulation: If True, start simulation thread.
        autosave: If True, start autosave thread.

    Returns:
        Dict mapping thread name to thread instance (or None if not started).
    """
    threads: dict[str, Optional[threading.Thread]] = {
        "simulation": None,
        "autosave": None,
    }

    if simulation:
        threads["simulation"] = start_simulation_thread(state)

    if autosave:
        threads["autosave"] = start_autosave_thread(state)

    return threads
