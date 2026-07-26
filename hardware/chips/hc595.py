"""74HC595 shift-register output chain (SPI), ported from i2c.py / gpio.ipynb.

Drives switch throws: one output bit per switch. Cascaded chips give 8×N bits.
The device's `latch_out` clocks the bytes and pulses the configured select/latch
line; this class owns only the bit packing and the cascade byte order.

**Hold guard.** These bits drive solenoid point motors, which burn out if a coil
is left energized, so this class enforces a ceiling on how long any bit may stay
high (`MAX_OUTPUT_HIGH_S`). Every write re-arms a timer; if the deadline passes
with bits still high, the guard drives the whole chain low and latches it. That
covers what a `finally` cannot: a caller that raises a bit and never lowers it, a
process that stalls mid-pulse, a REST client driving a raw bit, an interpreter
interrupted between the two writes of a pulse. It cannot cover process death —
the chip holds its last latched bits with nothing running to clear them (see
`watchdog.py` for why that needs external hardware).

The guard is a backstop, not the mechanism: correct code still pulses and drops
its own bits. Reaching the guard means something went wrong, so it reports
through `on_auto_release`.
"""

import threading
from typing import Any, Callable, Optional

from hardware.catalog import MAX_OUTPUT_HIGH_S


class HC595Chain:
    def __init__(
        self,
        device: Any,
        select: str = "cs0",
        chain_length: int = 1,
        max_high_s: Optional[float] = MAX_OUTPUT_HIGH_S,
        on_auto_release: Optional[Callable[[int], None]] = None,
    ) -> None:
        """`max_high_s` None disables the hold guard — only for a chain wired to
        something that is *meant* to stay on. Nothing in v1 is, so it defaults on.
        `on_auto_release` is called with the released bit pattern when the guard
        fires, off the timer thread."""
        self.device = device
        self.select = select
        self.chain_length = max(1, int(chain_length))
        self.max_high_s = None if max_high_s is None else float(max_high_s)
        self._on_auto_release = on_auto_release
        self._state = bytearray(self.chain_length)
        # Re-entrant: the guard writes through the same locked path a caller does.
        self._lock = threading.RLock()
        self._guard: Optional[threading.Timer] = None

    @property
    def num_outputs(self) -> int:
        return self.chain_length * 8

    def write_all(self) -> None:
        with self._lock:
            # Shift registers are first-in-last-out: the farthest cascaded chip
            # must be clocked out first, so send the state reversed.
            self.device.latch_out(bytes(reversed(self._state)), self.select)
            self._rearm_guard()

    def set_output(self, index: int, level: bool) -> None:
        """Set output `index` (0-based, continuous across the chain)."""
        with self._lock:
            byte_idx, bit_idx = divmod(index, 8)
            if level:
                self._state[byte_idx] |= (1 << bit_idx)
            else:
                self._state[byte_idx] &= ~(1 << bit_idx)
            self.write_all()

    def all_off(self) -> None:
        with self._lock:
            self._state = bytearray(self.chain_length)
            self.write_all()

    def probe(self) -> bool:
        """Write-only part: success == the SPI transaction issued without error."""
        try:
            self.write_all()
            return True
        except Exception:
            return False

    def is_high(self, index: int) -> bool:
        """Is output `index` currently driven high (per the last latch)?"""
        byte_idx, bit_idx = divmod(index, 8)
        with self._lock:
            if byte_idx >= len(self._state):
                return False
            return bool(self._state[byte_idx] & (1 << bit_idx))

    # -- hold guard ---------------------------------------------------------
    @property
    def any_high(self) -> bool:
        with self._lock:
            return any(self._state)

    def _rearm_guard(self) -> None:
        """Re-time the guard against the state just latched. Called under lock,
        after every write: bits high start a fresh countdown, an all-low chain
        cancels it. Timing from the last write, not the first, means a caller
        that keeps refreshing a bit keeps it — which is why the deadline is 100x
        a throw pulse rather than something a healthy path could ever reach."""
        if self._guard is not None:
            self._guard.cancel()
            self._guard = None
        if self.max_high_s is None or not any(self._state):
            return
        self._guard = threading.Timer(self.max_high_s, self._release)
        # Daemon: a pending guard must never hold up interpreter shutdown.
        self._guard.daemon = True
        self._guard.start()

    def _release(self) -> None:
        """Deadline reached with bits still high — drive the chain low."""
        with self._lock:
            released = int.from_bytes(bytes(self._state), "big")
            if not released:
                return
            self._state = bytearray(self.chain_length)
            try:
                self.device.latch_out(bytes(self.chain_length), self.select)
            except Exception:
                # A failed release is the worst case (a coil may still be hot),
                # but raising off a timer thread would only lose the report.
                pass
            self._guard = None
        if self._on_auto_release is not None:
            try:
                self._on_auto_release(released)
            except Exception:  # noqa: BLE001 - reporting must not kill the thread
                pass

    def cancel_guard(self) -> None:
        """Stop the timer thread. For teardown; the chain must already be low."""
        with self._lock:
            if self._guard is not None:
                self._guard.cancel()
                self._guard = None
