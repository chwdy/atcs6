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

from hardware.catalog import SWITCH_THROW_PULSE_S
from hardware.interfaces import build_chip

# Reclaim a session left open by an abandoned client after this long idle.
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
        self._last = 0.0

    def open(self, cfg: Any, open_device: Optional[Callable[[], Any]] = None) -> None:
        """Open (or reclaim) the session for `cfg` and build its chip drivers.

        Clears every shift-out chain on the way in, the same as a real session
        build: a reclaimed session inherits whatever bits the abandoned client
        left latched, and this screen exists to be poked at with coils wired up."""
        with self._lock:
            self._expire_locked()
            if self._device is None:
                opener = open_device or (lambda: self._open_real(cfg))
                self._device = opener()
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

        The Hardware Setup output sweep also runs through here rather than
        `set_output`, because a bit it walks across may be wired to a coil; it
        names no `off_port`, having no notion of a coil pair. De-energizing runs
        in a `finally` so a failure leaves nothing driven."""
        with self._lock:
            self._require_open()
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
                time.sleep(pulse_s)
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

    def close(self) -> None:
        with self._lock:
            self._close_locked()

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

    def _expire_locked(self) -> None:
        if self._device is not None and (time.monotonic() - self._last) > IDLE_TIMEOUT_S:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._device is None:
            return
        try:
            for chip in self._chips.values():
                if hasattr(chip, "all_off"):
                    chip.all_off()
        except Exception:
            pass
        try:
            self._device.close()
        except Exception:
            pass
        self._device = None
        self._chips = {}
        self._kinds = {}
        self._configured = set()
        self._driven = {}


_session = CommissionSession()


def get_session() -> CommissionSession:
    return _session


atexit.register(_session.close)
