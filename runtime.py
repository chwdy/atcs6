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
import traceback
from datetime import datetime
from typing import Callable, Optional, Tuple

from app_state import AppState
from constants import MAX_SIM_DELTA_SECONDS
from enums import OperationMode

# Configuration
AUTOSAVE_INTERVAL = 300  # 5 minutes in seconds
AUTOSAVE_FILENAME = "_autosave"


# Cadence of the loop, and how often a stuck fault is re-reported. A fault that
# survives its own recovery (a USB bridge that stays unplugged, say) fails every
# single tick; printing its traceback ten times a second buries the one line
# that says when it started, so repeats are counted and summarized instead.
TICK_INTERVAL_SECONDS = 0.1
ERROR_REPEAT_SUMMARY_EVERY = 100   # ~10s at the tick interval


def _clamp_sim_delta(delta_seconds: float, simulation_speed: float) -> float:
    """Per-tick sim delta = wall-clock delta x speed, clamped to
    ``MAX_SIM_DELTA_SECONDS`` (and floored at 0 against clock skew)."""
    return min(
        max(delta_seconds, 0.0) * simulation_speed, MAX_SIM_DELTA_SECONDS
    )


class FaultLog:
    """Collapses consecutive identical faults from a loop that must not exit.

    A stuck fault fails every tick — a USB bridge that stayed off the bus is the
    one that prompted this — and at 10 Hz its traceback scrolls the line that
    says when the trouble started off the screen within seconds. The first
    occurrence prints in full; identical repeats are counted, summarized
    periodically, and totalled when the fault changes or clears.

    Sameness is `(type, str(exc))`, not the traceback: the same failure reached
    from two call sites is still the same failure to whoever is reading the log,
    and a message that varies (a changing device path, say) is worth seeing.
    """

    def __init__(
        self, tag: str, summary_every: int = ERROR_REPEAT_SUMMARY_EVERY
    ) -> None:
        self._tag = tag
        self._summary_every = max(1, int(summary_every))
        self._signature: Optional[Tuple[str, str]] = None
        self._repeats = 0

    def report(self, exc: BaseException) -> None:
        signature = (type(exc).__name__, str(exc))
        if signature == self._signature:
            self._repeats += 1
            if self._repeats % self._summary_every == 0:
                print(
                    f"[{self._tag}] STILL FAILING: {exc} "
                    f"({self._repeats + 1} consecutive ticks)"
                )
            return
        self._flush()
        self._signature = signature
        self._repeats = 0
        print(f"[{self._tag}] ERROR: {exc}")
        # Explicit rather than `print_exc()`: this is reachable outside an
        # `except` block, where the implicit form prints "NoneType: None".
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        print(f"[{self._tag}] Continuing despite error...")

    def clear(self) -> None:
        """Record a tick that succeeded, closing out any run of faults."""
        if self._signature is None:
            return
        print(
            f"[{self._tag}] recovered after {self._repeats + 1} failed tick(s): "
            f"{self._signature[1]}"
        )
        self._signature = None
        self._repeats = 0

    def _flush(self) -> None:
        if self._repeats:
            print(
                f"[{self._tag}] (previous error repeated {self._repeats} "
                f"more times)"
            )


def _reads_hardware(state: AppState) -> bool:
    """True when this session reads a setup-aware occupancy device.

    Read without the simulation lock: the backend is chosen once when the
    session is built and never changes under a running loop, so there is
    nothing here to race with — and taking the lock on every idle pass of a
    sim-mode session would contend with request handlers for no reason.
    """
    controller = getattr(state, "sim_controller", None)
    return getattr(controller, "operation_mode", None) in (
        OperationMode.VIRTUAL_HARDWARE,
        OperationMode.REAL_HARDWARE,
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
        faults = FaultLog("SimulationLoop")
        while True:
            try:
                current_time = datetime.now()
                delta_seconds = (current_time - last_update_time).total_seconds()
                last_update_time = current_time

                if state.simulation_running:
                    # The operating clock advances by the uncapped wall x speed
                    # delta (a faithful clock a schedule measures against); the
                    # physics integrator uses the same delta CLAMPED to
                    # MAX_SIM_DELTA_SECONDS so a multi-second hitch can't
                    # integrate one giant jump that spikes speed past max and
                    # trips a spurious overspeed EB (design_new.md "Clocks").
                    # Normal ticks (incl. a few-x speed multiplier) leave the two
                    # equal.
                    operating_delta = max(delta_seconds, 0.0) * state.simulation_speed
                    sim_delta = min(operating_delta, MAX_SIM_DELTA_SECONDS)
                    with state.simulation_lock:
                        state.sim_controller.update(
                            sim_delta, operating_delta_seconds=operating_delta
                        )
                elif _reads_hardware(state):
                    # Stopped, but the detectors have not stopped detecting.
                    # Occupancy is what the hardware says it is, so it is read
                    # and committed on the same cadence whether or not trains
                    # are being ticked; `poll_hardware_occupancy` runs the Observe read
                    # and its commit alone, so nothing else about "stopped"
                    # changes. The mode is checked before taking the lock so a
                    # sim-mode session never contends for it while idle.
                    with state.simulation_lock:
                        state.sim_controller.poll_hardware_occupancy()
            except Exception as e:
                faults.report(e)
            else:
                faults.clear()
            # Outside the try, so a failing tick still paces itself. Inside, a
            # stuck fault would skip its own sleep and spin the loop at full
            # speed — pinning a core and hammering whatever just failed.
            time.sleep(TICK_INTERVAL_SECONDS)

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
