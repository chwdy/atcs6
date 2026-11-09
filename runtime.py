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


# The passes a runtime-loop iteration can run (design_new.md "Running and
# stopped", Which pass runs).
PASS_TICK = "tick"  # the whole tick
PASS_STOPPED = "stopped"  # the observing steps only
PASS_WATCHDOG = "watchdog"  # only consume a loss-of-control watchdog trip


def runtime_pass(state: AppState) -> Optional[str]:
    """Which pass this iteration runs, read from the session state.

    - Live: the whole tick.
    - Halted for a runtime or hardware failure: no pass, apart from consuming a
      watchdog trip record (step `ee19`).
    - Any other stopped state in Virtual Hardware or Real Hardware — Setup, or
      Halted for the layout emergency cut-out — the stopped pass, because the
      detectors go on detecting. Setup with a hardware failure latched runs no
      pass, since step `ee19` keeps the runtime off the failed device.
    - Everything else runs no pass: a Paused or cut-out Simulation session has
      nothing to observe, and a session Halted for `not_activated` has no
      interfaces built.
    """
    from enums import HaltReason, SessionStateValue

    session = state.session_state()
    if session.is_live:
        return PASS_TICK
    if session.halt_reason in (HaltReason.RUNTIME_FAILURE, HaltReason.HARDWARE_FAILURE):
        return PASS_WATCHDOG
    controller = state.sim_controller
    hardware = getattr(controller, "operation_mode", None) in (
        OperationMode.VIRTUAL_HARDWARE,
        OperationMode.REAL_HARDWARE,
    )
    if not hardware or session.halt_reason is HaltReason.NOT_ACTIVATED:
        return None
    if session.value is SessionStateValue.SETUP and getattr(
        controller, "hardware_failure", False
    ):
        return None
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


def run_loop_iteration(state: AppState, delta_seconds: float) -> None:
    """One iteration of the runtime loop: the pass `runtime_pass(state)` picks,
    run under the simulation lock. Every branch consumes a loss-of-control
    watchdog trip record (design_new.md "Trip-record consumption escalates a
    failed drop to hardware failure"): a running pass inside `update(...)`, the
    other branches directly."""
    which = runtime_pass(state)
    if which == PASS_TICK:
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
    elif which == PASS_STOPPED:
        # Stopped, but the detectors have not stopped detecting.
        # Occupancy is what the hardware says it is, so it is read
        # and committed on the same cadence whether or not trains
        # are being ticked; `poll_hardware_occupancy` runs the
        # Observe read and its commit alone, so nothing else about
        # "stopped" changes.
        with state.simulation_lock:
            # A watchdog may trip while the session is stopped; its
            # trip record is consumed on every iteration
            # (design_new.md "Trip-record consumption escalates a
            # failed drop to hardware failure").
            state.sim_controller.consume_watchdog_trip()
            state.sim_controller.poll_hardware_occupancy()
    elif which == PASS_WATCHDOG:
        # A failure halt runs no pass, but a watchdog trip is still
        # consumed so the recorded voltages follow the drop and a
        # failed drop escalates (design_new.md step `ee19`).
        with state.simulation_lock:
            state.sim_controller.consume_watchdog_trip()
            state.sim_controller.mark_no_pass()
    elif state.sim_controller is not None:
        # No pass this iteration: the time adds nothing to
        # `Block.occupied_seconds` (design_new.md "Maintenance
        # counters").
        with state.simulation_lock:
            state.sim_controller.consume_watchdog_trip()
            state.sim_controller.mark_no_pass()


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
        tool_faults = FaultLog("SetupToolTimeout")
        while True:
            try:
                current_time = datetime.now()
                delta_seconds = (current_time - last_update_time).total_seconds()
                last_update_time = current_time

                run_loop_iteration(state, delta_seconds)
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
