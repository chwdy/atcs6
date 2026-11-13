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

# Configuration
AUTOSAVE_INTERVAL = 300  # 5 minutes in seconds
AUTOSAVE_FILENAME = "_autosave"


# Cadence of the loop, and how often a stuck fault is re-reported. A fault that
# survives its own recovery (a USB bridge that stays unplugged, say) fails every
# single tick; printing its traceback ten times a second buries the one line
# that says when it started, so repeats are counted and summarized instead.
TICK_INTERVAL_SECONDS = 0.1
ERROR_REPEAT_SUMMARY_EVERY = 100   # ~10s at the tick interval


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


# The passes a runtime-loop iteration can run (design_v2.md "Runtime loop",
# Which pass runs).
PASS_TICK = "tick"  # the whole runtime step
PASS_STOPPED = "stopped"  # the stopped pass: read the detectors, update trains
PASS_WATCHDOG = "watchdog"  # no pass; only consume a loss-of-control watchdog trip


def runtime_pass(state: AppState) -> Optional[str]:
    """Which pass this iteration runs, read from the session state
    (design_v2.md "Runtime loop", Which pass runs).

    - Live: the whole runtime step.
    - Setup: the stopped pass, because the detectors go on detecting. Setup
      runs no pass while a hardware failure is latched, or while no detector
      interface is open.
    - Halted for `hardware_failure` or `not_activated`: no pass.
    - Halted for the v1 `runtime_failure`: no pass. Halted for the v1 layout
      emergency cut-out: the stopped pass, as in Setup.

    Every iteration consumes a watchdog trip whatever this returns.
    """
    from enums import HaltReason, SessionStateValue

    session = state.session_state()
    if session.is_live:
        return PASS_TICK
    if session.halt_reason in (
        HaltReason.RUNTIME_FAILURE,
        HaltReason.HARDWARE_FAILURE,
        HaltReason.NOT_ACTIVATED,
    ):
        return PASS_WATCHDOG
    controller = state.sim_controller
    if session.value is SessionStateValue.SETUP and getattr(
        controller, "hardware_failure", False
    ):
        return PASS_WATCHDOG
    if not getattr(controller, "occupancy_is_real", False):
        return PASS_WATCHDOG
    return PASS_STOPPED


def close_idle_setup_tools(state: AppState) -> None:
    """Close any Setup tool whose idle timeout has passed, and report a failed
    zeroing as a hardware I/O failure (design_new.md "Power", Hardware setup,
    Closing a Setup tool restores the safe state; "Running and stopped", Which
    pass runs).

    The runtime loop calls this every iteration, whatever pass it runs, so a
    tool the operator walked away from closes on time instead of waiting for
    the next request to notice. It takes `simulation_lock` only while a tool is
    open, and never waits on a tool's own lock (a busy tool is not idle).
    """
    from hardware import setup_tools

    if not setup_tools.any_setup_tool_open():
        return
    with state.simulation_lock:
        setup_tools.report_close_failures(
            state.sim_controller, setup_tools.close_idle_setup_tools()
        )


def run_loop_iteration(state: AppState) -> None:
    """One iteration of the runtime loop: the pass `runtime_pass(state)` picks,
    run under the simulation lock. Every iteration consumes a loss-of-control
    watchdog trip record, even when no pass runs (design_v2.md "Runtime
    loop", Every time round consumes a watchdog trip)."""
    which = runtime_pass(state)
    controller = state.sim_controller
    if controller is None:
        return
    with state.simulation_lock:
        controller.consume_watchdog_trip()
        controller.note_session_live(which == PASS_TICK)
        if which == PASS_TICK:
            controller.run_train_step(live=True)
        elif which == PASS_STOPPED:
            controller.run_train_step(live=False)


def create_simulation_loop(state: AppState) -> Callable[[], None]:
    """Create a simulation loop function bound to the given state.

    Args:
        state: AppState containing simulation controller and lock.

    Returns:
        A function that runs the simulation loop indefinitely.
    """

    def simulation_loop() -> None:
        faults = FaultLog("SimulationLoop")
        tool_faults = FaultLog("SetupToolTimeout")
        while True:
            try:
                run_loop_iteration(state)
            except Exception as e:
                faults.report(e)
            else:
                faults.clear()
            # In its own try, so a pass that fails every iteration cannot keep
            # an abandoned Setup tool driving its outputs.
            try:
                close_idle_setup_tools(state)
            except Exception as e:
                tool_faults.report(e)
            else:
                tool_faults.clear()
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
