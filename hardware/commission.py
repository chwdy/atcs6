"""Commissioning session — the raw-port Setup tool, held open across requests
so the Hardware Setup UI can watch inputs live and run an output marquee.

The client opens a session, then drives many fast ops (set one output, read all
inputs) on a JS timer, and closes it. This module holds one such session behind
a lock, with an idle timeout so an abandoned session is reclaimed, and an
atexit close so shutdown always de-energizes.

It is an open and close tool (design_v2.md "Hardware interface", Two kinds of
Setup tool take the outputs): it owns the outputs from its open until its close
completes, and it reaches the rig only through the session's device handle,
under its own writer name. It opens no device of its own (design_v2.md "Setup
tools", Setup always has one device handle).
"""

import atexit
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from constants import COMMISSION_MAX_SESSION_SECONDS
from hardware.catalog import MAX_COIL_PULSE_S, SWITCH_THROW_PULSE_S
from hardware.chips.hc595 import sleep_holding
from hardware.device_handle import COMMISSIONING_SESSION, OutputNotOwned
from hardware.interfaces import SwitchDriver, build_chip, safe_state_writes

# Reclaim a session left open by an abandoned client after this long idle. This
# is the default window; `open(timeout_seconds=...)` overrides it per session,
# bounded by COMMISSION_MAX_SESSION_SECONDS (see open / _validate_timeout).
IDLE_TIMEOUT_S = 30.0


class CommissionSession:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # The session's device handle while this session holds the outputs,
        # and the `DeviceWriter` this session's chips are built on.
        self._handle: Optional[Any] = None
        self._device: Optional[Any] = None
        self._chips: Dict[str, Any] = {}
        self._kinds: Dict[str, str] = {}
        self._configured: set = set()
        # Per PWM chip, the channel `set_output` last drove. Zeroed before the
        # next channel is raised — see set_output.
        self._driven: Dict[str, int] = {}
        # (chip_id, port) pairs carrying a switch coil / standby bit: every port
        # a `switch_output` binding names, from the posted config and the saved
        # one (design_v2.md "Setup tools", Switch bits).
        self._switch_ports: set = set()
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
        handle: Any,
        timeout_seconds: Optional[float] = None,
        saved_switch_output: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Open (or reopen) the session for `cfg` on the session's device
        handle and build its chip drivers.

        Opening takes the outputs (design_v2.md "Hardware interface", When the
        owner changes): `handle.take(...)` raises `OutputsOwned` while another
        owner holds them, and nothing else happens then. Opening again while
        this session is open rebuilds its chips from `cfg`.

        Clears every output of the chips it builds on the way in: a reclaimed
        session inherits whatever bits the abandoned client left latched, and
        this screen exists to be poked at with coils wired up.

        `timeout_seconds` is the commissioning `open` body's idle window
        (design_v2.md "Hardware REST API", Commissioning-session bodies
        address raw chips and ports, not entities), the time after which an
        abandoned session is reclaimed and its outputs dropped. The doc bounds it
        at `0 < timeout_seconds <= COMMISSION_MAX_SESSION_SECONDS`; a request
        outside that is rejected here (a session too long is not silently
        shortened). `None` keeps the default IDLE_TIMEOUT_S.

        `saved_switch_output` is the saved setup's `switch_output` map. The
        panel posts its chips without bindings, so the saved bindings are what
        tell this session which shift-out bits drive a coil (design_v2.md "Setup
        tools", Switch bits). A binding counts only for a chip
        this session builds.
        """
        with self._lock:
            # Reject a bad window before any side effect, then reclaim a prior
            # abandoned session by its own (still-current) timeout, then adopt
            # the new window for this session onward.
            new_timeout = self._validate_timeout(timeout_seconds)
            self._expire_locked()
            handle.take(COMMISSIONING_SESSION)
            if self._device is not None:
                # Reopening while open: the chips about to be dropped may still
                # be driving, and a chip the new setup lacks would never be
                # zeroed by a later close. Zero every one first; the session
                # keeps the outputs across the rebuild. A failed zeroing is a
                # hardware I/O failure, kept for the next caller that can
                # report it.
                self._unreported_close_failures.extend(self._zero_chips_locked())
            self._timeout_s = new_timeout
            self._handle = handle
            self._device = handle.writer(COMMISSIONING_SESSION)
            self._chips = {}
            self._kinds = {}
            self._switch_ports = set()
            self._configured = set()
            self._driven = {}
            try:
                self._add_chips_locked(cfg, saved_switch_output)
                for chip in self._chips.values():
                    if hasattr(chip, "all_off"):
                        chip.all_off()
            except Exception:
                # A failed open still completes the safe-state sequence and
                # gives the outputs back; a failed zeroing there is kept for
                # the next caller that can report it.
                self._unreported_close_failures.extend(self._close_locked())
                raise
            self._touch()

    def add_chips(
        self, cfg: Any, saved_switch_output: Optional[Dict[str, Any]] = None
    ) -> None:
        """Build the chips of `cfg` this open session does not have yet, and
        learn their switch bits. A chip already built keeps its driver and its
        state. The switch throw test uses it to pulse through a session the
        operator already opened (design_v2.md "Setup tools", The switch throw
        test pulses through the commissioning session)."""
        with self._lock:
            self._require_open()
            self._add_chips_locked(cfg, saved_switch_output)
            self._touch()

    def _add_chips_locked(
        self, cfg: Any, saved_switch_output: Optional[Dict[str, Any]]
    ) -> None:
        for spec in cfg.chips:
            if spec.chip_id not in self._chips:
                self._chips[spec.chip_id] = build_chip(self._device, spec)
                self._kinds[spec.chip_id] = spec.kind
        # Which (chip_id, port) pairs a switch coil or its driver standby
        # sits on. `set_output` refuses to hold these — see there.
        bindings = list(getattr(cfg, "switch_output", {}).values())
        bindings += list((saved_switch_output or {}).values())
        self._switch_ports |= {
            (b.chip_id, port)
            for b in bindings
            if b.chip_id in self._chips
            for port in b.ports
        }

    def is_open(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._device is not None

    def switch_ports_by_chip(self) -> Dict[str, List[int]]:
        """The switch bits per chip — what `open` reports as `switch_ports`, so
        a caller walking the bits pulses these only for the switch-coil width."""
        with self._lock:
            by_chip: Dict[str, List[int]] = {}
            for chip_id, port in self._switch_ports:
                by_chip.setdefault(chip_id, []).append(port)
            return {chip_id: sorted(ports) for chip_id, ports in by_chip.items()}

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
            try:
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
            except Exception as exc:
                self._end_on_write_failure_locked(exc)
                raise
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
        there for a "watch the switch throw" test. A caller may pass a longer
        width so an LED on the pin visibly lights, but never past
        MAX_COIL_PULSE_S: any '595 bit may be wired to a coil, bound or not, so a
        request past the ceiling is rejected rather than driven (design_v2.md
        "Hardware setup", No shift-out bit is driven for longer than 200 ms).

        The Hardware Setup output sweep also runs through here rather than
        `set_output`, because a bit it walks across may be wired to a coil; it
        names no `off_port`, having no notion of a coil pair. De-energizing runs
        in a `finally` so a failure leaves nothing driven."""
        with self._lock:
            self._require_open()
            if float(pulse_s) > MAX_COIL_PULSE_S:
                raise ValueError(
                    f"pulse width {float(pulse_s):g} s exceeds "
                    f"MAX_COIL_PULSE_S ({MAX_COIL_PULSE_S:g} s)"
                )
            chip = self._chips.get(chip_id)
            if chip is None:
                raise KeyError(f"chip {chip_id!r} not in session")
            if not hasattr(chip, "set_output"):
                raise KeyError(f"chip {chip_id!r} is not a shift-out chip")
            first = standby_port if standby_port is not None else port
            landed = False
            try:
                try:
                    chip.set_output(first, True)
                    landed = True
                    if standby_port is not None:
                        chip.set_output(port, True)
                    if off_port is not None:
                        chip.set_output(off_port, False)
                    sleep_holding(chip, pulse_s)
                except OutputNotOwned:
                    # As `SwitchDriver.set_throw`: a refused first write drove
                    # nothing; after a landed one, lowering is a move to the
                    # safe state and is accepted from anyone.
                    if landed:
                        with safe_state_writes(chip):
                            SwitchDriver._lower(chip, port, standby_port)
                    raise
                except BaseException:
                    SwitchDriver._lower(chip, port, standby_port)
                    raise
                SwitchDriver._lower(chip, port, standby_port)
            except Exception as exc:
                self._end_on_write_failure_locked(exc)
                raise
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
        """Read every channel's current in mA on a current monitor (for the live
        calibration view). Configures the chip once so its channels are enabled.

        A channel that cannot be read raises, failing the whole read: a missing
        reading is not a clear one, so it is never reported as 0 mA
        (design_v2.md "Setup tools", Every failure reaches the operator)."""
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
                readings.append(chip.read_current(channel) * 1000.0)
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

    def is_holding_outputs(self) -> bool:
        """Whether the tool is open. Read without the lock: the runtime loop
        checks it on every iteration before deciding to take any lock."""
        return self._device is not None

    def close_if_idle(self) -> List[str]:
        """Close the tool when its idle timeout has passed, for the runtime loop
        (design_v2.md "Setup tools", A timeout closes the tool on time: no
        request is needed). Never waits for the lock: a tool whose lock is held is in use,
        so it is not idle. Returns the failed zeroing writes of that close, plus
        any a request-noticed timeout left, for the caller to report."""
        if not self._lock.acquire(blocking=False):
            return []
        try:
            self._expire_locked()
            return self._take_close_failures_locked()
        finally:
            self._lock.release()

    def take_close_failures(self) -> List[str]:
        """The failed zeroing writes of a close the idle timeout ran, once.
        The caller reports them as a hardware I/O failure."""
        with self._lock:
            return self._take_close_failures_locked()

    # -- internals -------------------------------------------------------
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
        (design_v2.md "Hardware REST API", Commissioning-session bodies address
        raw chips and ports, not entities); anything else — a
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
            # A timed-out close is a close like any other (design_v2.md "Setup
            # tools", A failed closing write is a hardware I/O failure): a
            # failed zeroing is a hardware I/O failure. The expiry runs inside
            # whatever call noticed it, which cannot report it, so the failures
            # wait for `take_close_failures()`.
            self._unreported_close_failures.extend(self._close_locked())

    def _take_close_failures_locked(self) -> List[str]:
        failures = self._unreported_close_failures
        self._unreported_close_failures = []
        return failures

    def _end_on_write_failure_locked(self, exc: BaseException) -> None:
        """A failure of this session's own write ends it (design_v2.md
        "Hardware interface", A failure the tool did not cause leaves it open):
        close it through its own safe-state sequence and raise
        `SetupToolWriteFailed` for the caller to report. Any other exception
        (a refused write, a bad request) leaves the session open."""
        from hardware.setup_tools import SetupToolWriteFailed, is_write_failure

        if not is_write_failure(exc):
            return
        close_failures = self._close_locked()
        raise SetupToolWriteFailed("the commissioning session", exc, close_failures) from exc

    def _zero_chips_locked(self) -> List[str]:
        """Write `0` to every output of every chip this session built; returns
        one message per chip that failed."""
        errors: List[str] = []
        for chip_id, chip in self._chips.items():
            if not hasattr(chip, "all_off"):
                continue
            try:
                chip.all_off()
            except Exception as exc:  # noqa: BLE001 - zero the remaining chips
                errors.append(f"{chip_id}: {exc}")
        return errors

    def _close_locked(self) -> List[str]:
        if self._device is None:
            return []
        # Closing a Setup tool restores the safe state (design_v2.md "Setup
        # tools", The tool performs its safe-state sequence itself): the
        # session zeroes every output of every chip it built itself, through
        # the session's device handle — not through `OutputSafety`, and without
        # advancing `safe_state_generation` — then gives the outputs back.
        # Every chip is attempted even when one fails; the failures go back to
        # the caller, which reports them as a hardware I/O failure. The handle
        # stays open for the next tool and for activation.
        errors = self._zero_chips_locked()
        # The close is complete: the outputs go back to nobody (design_v2.md
        # "Hardware interface", When the owner changes).
        if self._handle is not None:
            self._handle.give_back(COMMISSIONING_SESSION)
        self._handle = None
        self._device = None
        self._chips = {}
        self._kinds = {}
        self._configured = set()
        self._driven = {}
        self._switch_ports = set()
        return errors


_session = CommissionSession()


def get_session() -> CommissionSession:
    return _session


atexit.register(_session.close)
