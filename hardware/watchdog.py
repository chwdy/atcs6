"""Loss-of-control watchdog for the real output interfaces.

design_new.md "Power -> Hardware interface boundary": a real output
implementation arms a watchdog that drops the outputs to the safe state when
the control loop stops refreshing them. The timing lives here; the design
models only the safe-state command and the rule that loss of control reaches
the safe state.

What this covers, and what it does not
-------------------------------------
`OutputWatchdog` runs a daemon thread that fires `on_timeout` when no output
write has landed for `timeout_s`. That covers a control loop which stalls,
deadlocks, or returns without running its shutdown hook while the process is
still alive.

It cannot cover process death. The thread dies with the process, and a PCA9685
holds its last PWM duty while a '595 holds its last latched bits. Real Hardware
therefore also requires an external renewable output-safety lease whose gate
fails disabled when renewal stops. `OutputWatchdog` is only the faster
in-process backstop; it is never evidence that the independent gate exists.

Bus access is safe: `Ch347Device` serializes every I2C/SPI operation under its
own `RLock`, so a timeout firing concurrently with a control-loop write cannot
interleave two transfers.
"""

import threading
import time
from typing import Callable, Optional

# Long enough that a slow tick under load never trips it, short enough that a
# runaway train does not travel far. The runtime refreshes track voltage every
# tick, so a healthy loop pets this many times per second.
DEFAULT_WATCHDOG_TIMEOUT_S = 2.0

# Cap the poll interval so a long timeout still detects promptly.
_MAX_POLL_S = 0.25


class OutputWatchdog:
    """Fires `on_timeout` when the control loop stops refreshing the outputs.

    Starts disarmed. The first `pet()` arms it; each later `pet()` pushes the
    deadline out. After it fires it stays latched until the next `pet()`, so a
    stalled loop drives the outputs to the safe state exactly once rather than
    once per poll.
    """

    def __init__(
        self,
        on_timeout: Callable[[], None],
        timeout_s: float = DEFAULT_WATCHDOG_TIMEOUT_S,
        name: str = "hw-output-watchdog",
    ) -> None:
        self._on_timeout = on_timeout
        self._timeout_s = float(timeout_s)
        self._poll_s = min(self._timeout_s / 4.0, _MAX_POLL_S)
        self._lock = threading.Lock()
        self._deadline: Optional[float] = None
        self._fired = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def pet(self) -> None:
        """Record a successful output write; push the deadline out."""
        with self._lock:
            self._deadline = time.monotonic() + self._timeout_s
            self._fired = False

    def disarm(self) -> None:
        """Stop watching. Used when the runtime deliberately stops driving."""
        with self._lock:
            self._deadline = None
            self._fired = False

    def stop(self) -> None:
        """Disarm and join the thread. Idempotent."""
        self.disarm()
        self._stop.set()
        self._thread.join(timeout=1.0)

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    def _run(self) -> None:
        while not self._stop.wait(self._poll_s):
            with self._lock:
                deadline = self._deadline
                if deadline is None or self._fired:
                    continue
                if time.monotonic() < deadline:
                    continue
                # Latch before releasing the lock so a second poll cannot also
                # fire, and so a concurrent pet() re-arms cleanly.
                self._fired = True
                self._deadline = None
            try:
                self._on_timeout()
            except Exception:  # noqa: BLE001 - a failing safe state must not kill the thread
                pass
