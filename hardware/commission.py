"""Commissioning session — a controller device held open across requests so the
Hardware Setup UI can watch inputs live and run an output marquee.

The one-shot bus scan / port-test open a transient device per call. The live
input view and the output sweep instead need the device held between requests:
the client opens a session, then drives many fast ops (set one output, read all
inputs) on a JS timer, and closes it. This module holds one such session behind
a lock, with an idle timeout so an abandoned session is reclaimed by the next
open, and an atexit close so shutdown always de-energizes.

Only one session at a time. While it holds the controller, the transient
scan/port-test report the device busy — commissioning is one activity at a
time. A real operation session (which holds its own device) likewise blocks a
commissioning open; going live needs a restart, which frees this handle anyway.
"""

import atexit
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from constants import COMMISSION_MAX_PULSE_MS, COMMISSION_MAX_SESSION_SECONDS
from hardware.catalog import SWITCH_THROW_PULSE_S
from hardware.chips.hc595 import sleep_holding
from hardware.interfaces import build_chip

# Reclaim a session left open by an abandoned client after this long idle. This
# is the default window; `open(timeout_seconds=...)` overrides it per session,
# bounded by COMMISSION_MAX_SESSION_SECONDS (see open / _validate_timeout).
IDLE_TIMEOUT_S = 30.0


class CommissionSession:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._device: Optional[Any] = None
        self._chips: Dict[str, Any] = {}
        self._kinds: Dict[str, str] = {}
        self._configured: set = set()
        # Per PWM chip, the channel `set_output` last drove. Zeroed before the
        # next channel is raised — see set_output.
        self._driven: Dict[str, int] = {}
        # (chip_id, port) pairs carrying a switch coil / standby bit, from the
        # config the session was opened with.
        self._switch_ports: set = set()
        # True when `_device` belongs to a running operation session rather than
        # to this one: it is neither cleared on open nor closed on close.
        self._borrowed = False
        self._last = 0.0
        # Idle window for this session; `open` resets it from its `timeout_seconds`
        # argument, or to IDLE_TIMEOUT_S when the caller names none.
        self._timeout_s = IDLE_TIMEOUT_S
        # Failed zeroing writes from a close the idle timeout ran, kept until a
        # caller that can report them takes them (`take_close_failures`).
        self._unreported_close_failures: List[str] = []

    def open(
        self,
        cfg: Any,
        open_device: Optional[Callable[[], Any]] = None,
        device: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        """Open (or reclaim) the session for `cfg` and build its chip drivers.

        Clears every shift-out chain on the way in, the same as a real session
        build: a reclaimed session inherits whatever bits the abandoned client
        left latched, and this screen exists to be poked at with coils wired up.

        `device` borrows an already-open device — the running operation
        session's — instead of opening one, because the controller is a single
        USB endpoint and a second open is refused. A borrowed device is NOT
        cleared on the way in and NOT closed on the way out: those chains are
        driving a live layout, so dropping every bit would zero the track and
        release the PCA output-enable hold mid-session. The caller is
        responsible for serializing use against the tick.

        `timeout_seconds` is the commissioning `enter` body's idle window
        (design_new.md "Hardware REST payload schemas"), the time after which an
        abandoned session is reclaimed and its outputs dropped. The doc bounds it
        at `0 < timeout_seconds <= COMMISSION_MAX_SESSION_SECONDS`; a request
        outside that is rejected here (a session too long is not silently
        shortened). `None` keeps the default IDLE_TIMEOUT_S.
        """
        with self._lock:
            # Reject a bad window before any side effect, then reclaim a prior
            # abandoned session by its own (still-current) timeout, then adopt
            # the new window for this session onward.
            new_timeout = self._validate_timeout(timeout_seconds)
            self._expire_locked()
            self._timeout_s = new_timeout
            if device is not None:
                self._device = device
                self._borrowed = True
            elif self._device is None:
                opener = open_device or (lambda: self._open_real(cfg))
                self._device = opener()
                self._borrowed = False
            self._chips = {c.chip_id: build_chip(self._device, c) for c in cfg.chips}
            self._kinds = {c.chip_id: c.kind for c in cfg.chips}
            self._configured = set()
            self._driven = {}
            # Which (chip_id, port) pairs a switch coil or its driver standby
            # sits on. `set_output` refuses to hold these — see there.
            self._switch_ports = {
                (b.chip_id, port)
                for b in getattr(cfg, "switch_output", {}).values()
                for port in b.ports
            }
            if not self._borrowed:
                for chip in self._chips.values():
                    if hasattr(chip, "all_off"):
                        chip.all_off()
            self._touch()

    def set_output(self, chip_id: str, line: int, duty: float) -> None:
        """Drive one output line. `line` is the raw hardware line the port grid
        addresses — a single PWM channel (0..15 on a PCA9685, not a pair), a
        single output bit on a shift register. PWM takes a duty 0..1; a shift-out
        bit takes on/off.

        Raising one PWM channel first drops whichever channel this chip was last
        driving. Two channels of one H-bridge pair high together is a shoot-
        through, and the caller walking channels one at a time must not be able
        to cause that by skipping its own zeroing step.

        A shift-out bit bound to a switch coil (or its driver standby) is refused
        outright: this call holds a bit until someone lowers it, and a held coil
        burns out. The UI already sends those ports to `pulse_output` — the
        refusal is for anything driving the API directly, and for a UI change
        that forgets. Lowering such a bit is always allowed; it is the one
        direction that cannot hurt."""
        with self._lock:
            self._require_open()
            chip = self._chips.get(chip_id)
            if chip is None:
                raise KeyError(f"chip {chip_id!r} not in session")
            if float(duty) > 0.0 and (chip_id, line) in self._switch_ports:
                raise ValueError(
                    f"port {line} on chip {chip_id!r} drives a switch coil and "
                    f"cannot be held on — pulse it instead"
                )
            if self._kinds.get(chip_id) == "pca9685":
                if chip_id not in self._configured:
                    chip.configure()
                    self._configured.add(chip_id)
                level = max(0.0, min(1.0, float(duty)))
                previous = self._driven.get(chip_id)
                if previous is not None and previous != line:
                    chip.set_duty_cycle(previous, 0.0)
                chip.set_duty_cycle(line, level)
                if level > 0.0:
                    self._driven[chip_id] = line
                else:
                    self._driven.pop(chip_id, None)
            else:
                chip.set_output(line, float(duty) > 0.0)
            self._touch()

    def pulse_output(
        self,
        chip_id: str,
        port: int,
        standby_port: Optional[int] = None,
        off_port: Optional[int] = None,
        pulse_s: float = SWITCH_THROW_PULSE_S,
    ) -> None:
        """Drive one shift-out bit for the switch-coil pulse width.

        This is how a switch coil must be driven — a solenoid point motor burns
        out if a coil is held — so this runs the same write order as
        `SwitchDriver.set_throw` (documented there): standby high,
        target bit high, `off_port` (the bridge's other input) low, wait, then
        standby low before the bit. What the user confirms at the bench is
        therefore the drive the runtime issues.

        `pulse_s` therefore defaults to the runtime throw width and should stay
        there for a "watch the switch throw" test. A caller measuring the bit
        rather than watching the points (a meter on the pin) passes a longer
        width; the sleep re-latches inside the guard window so the hold survives,
        and it is the caller's business that a coil can be on the other end. That
        longer width may not cross COMMISSION_MAX_PULSE_MS (design_new.md: the
        commissioning pulse "must be no greater than COMMISSION_MAX_PULSE_MS");
        a request past the ceiling is rejected rather than driven.

        The Hardware Setup output sweep also runs through here rather than
        `set_output`, because a bit it walks across may be wired to a coil; it
        names no `off_port`, having no notion of a coil pair. De-energizing runs
        in a `finally` so a failure leaves nothing driven."""
        with self._lock:
            self._require_open()
            pulse_ms = float(pulse_s) * 1000.0
            if pulse_ms > COMMISSION_MAX_PULSE_MS:
                raise ValueError(
                    f"pulse width {pulse_ms:g} ms exceeds "
                    f"COMMISSION_MAX_PULSE_MS ({COMMISSION_MAX_PULSE_MS:g} ms)"
                )
            chip = self._chips.get(chip_id)
            if chip is None:
                raise KeyError(f"chip {chip_id!r} not in session")
            if not hasattr(chip, "set_output"):
                raise KeyError(f"chip {chip_id!r} is not a shift-out chip")
            try:
                if standby_port is not None:
                    chip.set_output(standby_port, True)
                chip.set_output(port, True)
                if off_port is not None:
                    chip.set_output(off_port, False)
                sleep_holding(chip, pulse_s)
            finally:
                if standby_port is not None:
                    chip.set_output(standby_port, False)
                chip.set_output(port, False)
            self._touch()

    def read_inputs(self, chip_id: str) -> List[bool]:
        """Read every input bit on a shift-in chip (for the live view)."""
        with self._lock:
            self._require_open()
            chip = self._chips.get(chip_id)
            if chip is None or not hasattr(chip, "read_all"):
                raise KeyError(f"chip {chip_id!r} is not a readable input chip")
            data = chip.read_all()
            bits: List[bool] = []
            for port in range(chip.num_inputs):
                byte_idx, bit_idx = divmod(port, 8)
                bits.append(
                    bool(data[byte_idx] & (1 << bit_idx)) if byte_idx < len(data) else False
                )
            self._touch()
            return bits

    def read_currents(self, chip_id: str) -> List[float]:
        """Read every channel's current in mA on an INA3221 (for the live
        calibration view). Configures the chip once so its channels are enabled."""
        with self._lock:
            self._require_open()
            chip = self._chips.get(chip_id)
            if chip is None or not hasattr(chip, "read_current"):
                raise KeyError(f"chip {chip_id!r} is not a current-sense chip")
            if chip_id not in self._configured:
                if hasattr(chip, "configure"):
                    chip.configure()
                self._configured.add(chip_id)
            channels = getattr(chip, "NUM_CHANNELS", 3)
            readings: List[float] = []
            for channel in range(1, channels + 1):
                try:
                    readings.append(chip.read_current(channel) * 1000.0)
                except Exception:
                    readings.append(0.0)
            self._touch()
            return readings

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._expire_locked()
            return {"open": self._device is not None, "chips": list(self._chips.keys())}

    def close(self) -> List[str]:
        """Close the session, zeroing every output of the chips it built.
        Returns one message per chip whose zeroing failed, for the caller to
        report as a hardware I/O failure — including any a timed-out close left
        unreported."""
        with self._lock:
            return self._take_close_failures_locked() + self._close_locked()

    def take_close_failures(self) -> List[str]:
        """The failed zeroing writes of a close the idle timeout ran, once.
        The caller reports them as a hardware I/O failure."""
        with self._lock:
            return self._take_close_failures_locked()

    # -- internals -------------------------------------------------------
    @staticmethod
    def _open_real(cfg: Any) -> Any:
        from hardware.ch347_device import open_ch347_device

        return open_ch347_device(getattr(cfg.controller, "device_port", None))

    def _require_open(self) -> None:
        if self._device is None:
            raise RuntimeError("commission session is not open")

    def _touch(self) -> None:
        self._last = time.monotonic()

    @staticmethod
    def _validate_timeout(timeout_seconds: Optional[float]) -> float:
        """Coerce and bound a requested session idle window.

        `None` means the caller named no window, so the default IDLE_TIMEOUT_S
        stands. Any named value must satisfy the doc's bound
        `0 < timeout_seconds <= COMMISSION_MAX_SESSION_SECONDS`
        (design_new.md "Hardware REST payload schemas"); anything else — a
        non-number, zero-or-negative, or a value past the ceiling — is rejected
        so the session is not opened with a nonsense or unbounded window."""
        if timeout_seconds is None:
            return IDLE_TIMEOUT_S
        try:
            seconds = float(timeout_seconds)
        except (TypeError, ValueError):
            raise ValueError(f"timeout_seconds must be a number, got {timeout_seconds!r}")
        if seconds != seconds:  # NaN
            raise ValueError("timeout_seconds must be a number, got NaN")
        if seconds <= 0.0 or seconds > COMMISSION_MAX_SESSION_SECONDS:
            raise ValueError(
                f"timeout_seconds must be > 0 and <= "
                f"{COMMISSION_MAX_SESSION_SECONDS} s, got {seconds}"
            )
        return seconds

    def _expire_locked(self) -> None:
        if self._device is not None and (time.monotonic() - self._last) > self._timeout_s:
            # A timed-out close is a close like any other (design_new.md "Power",
            # Hardware setup, Closing a Setup tool restores the safe state): a
            # failed zeroing is a hardware I/O failure. The expiry runs inside
            # whatever call noticed it, which cannot report it, so the failures
            # wait for `take_close_failures()`.
            self._unreported_close_failures.extend(self._close_locked())

    def _take_close_failures_locked(self) -> List[str]:
        failures = self._unreported_close_failures
        self._unreported_close_failures = []
        return failures

    def _close_locked(self) -> List[str]:
        if self._device is None:
            return []
        # Closing a Setup tool restores the safe state (design_new.md "Power",
        # Hardware setup): the session runs only in Setup, where no environment
        # drives the outputs, so it zeroes every output of every chip it built
        # itself — not through `OutputSafety`, and without advancing
        # `safe_state_generation` — whether or not the device is borrowed. Every
        # chip is attempted even when one fails; the failures go back to the
        # caller, which reports them as a hardware I/O failure. A borrowed
        # device is the session's handle, kept open for the next tool and for
        # activation, so only an owned one is closed.
        errors: List[str] = []
        for chip_id, chip in self._chips.items():
            if not hasattr(chip, "all_off"):
                continue
            try:
                chip.all_off()
            except Exception as exc:  # noqa: BLE001 - zero the remaining chips
                errors.append(f"{chip_id}: {exc}")
        if not self._borrowed:
            try:
                self._device.close()
            except Exception:
                pass
        self._device = None
        self._borrowed = False
        self._chips = {}
        self._kinds = {}
        self._configured = set()
        self._driven = {}
        return errors


_session = CommissionSession()


def get_session() -> CommissionSession:
    return _session


atexit.register(_session.close)
