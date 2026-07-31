"""CH347 transport — the one module that talks to `ch347api`.

Wraps a `CH347HIDDev` and exposes the small duck-typed *device* surface the chip
classes use: `i2c_write` / `i2c_read` / `i2c_scan` for the PWM chip, and
`latch_out` / `load_in` for the shift registers. All access is serialized behind
one lock because I2C (track) and SPI (occupancy/switch) ops interleave every tick
on the same physical bridge.

Two wiring quirks are contained here so nothing above the interface sees them:

- **Pad vs library name.** `ch347api.set_CS1()` drives the physical **CS0** pad
  and `set_CS2()` drives **CS1** — off by one. Callers name lines by pad
  (`"cs0"`, `"cs1"`, `"gpio0".."gpio7"`); the map to the library call lives in
  `_PAD_TO_CS` / `_gpio`.
- **Active polarity.** The '595 latch is active-low and the '165 load is
  active-high; the correct edge for each is applied inside `latch_out` /
  `load_in`, driven by the operation, not the caller.

**Dropped links.** A bridge sharing a supply with solenoid coils and motor loads
gets reset by the noise sooner or later, and a re-enumerated device leaves the
session holding a handle that can never work again. Every bus operation runs
through `_run`, which reopens the bridge and replays the transaction once; see
`Ch347LinkLost` and `add_reconnect_hook`.

`ch347api` itself is only imported inside `Ch347Device.open()`, not at module
scope, so this module — and anything that only needs to monkeypatch
`open_ch347_device` — imports cleanly on a build without the USB driver
installed. `hardware_config.build_session_interfaces` relies on that: it turns
the ImportError raised on `open()` into `HardwareBackendUnavailable` so a build
without the USB driver still boots in sim mode.
"""

import logging
import struct
import sys
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Tuple

from hardware.catalog import BUS_I2C, BUS_SPI

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ch347api import CH347HIDDev

# If the Phase-0 coexistence probe shows I2C does NOT survive `init_SPI()`, set
# this True: the device then re-inits the bus before each bus-class op. Default
# assumes coexistence (Strategy A: init both once at open).
REINIT_EACH_OP = False

SPI_CLOCK_LEVEL = 5   # 1.875 MHz — matches the demos; raise once validated
CS1_HIGH = False      # pad cs0 (lib CS1): active-low '595 latch
CS2_HIGH = True       # pad cs1 (lib CS2): active-high '165 load

# '595 latch pulse width when the latch rides a GPIO pin (gpio.ipynb uses 100 µs).
GPIO_LATCH_PULSE_S = 100e-6

# Physical pad name -> ch347api CS selector (1 => set_CS1, 2 => set_CS2).
_PAD_TO_CS = {"cs0": 1, "cs1": 2}

# CH347 0xCC "set GPIO" command bytes (documented, not vendor-verified; from
# gpio.ipynb). v1 uses GPIO only as an output latch line.
_GPIO_OUT_LOW = 0xF0
_GPIO_OUT_HIGH = 0xF8
_GPIO_UNCHANGED = 0x00

# Serializes the one call that touches hidapi's process-global state
# (`CH347HIDDev()` enumerates before it opens). Nothing in hidapi's macOS
# backend locks that manager, and this process opens transient devices from
# several threads — every Hardware Setup request runs on its own Flask thread.
_OPEN_LOCK = threading.Lock()

# Set once `prime_hid_runloop` has pinned hidapi's manager (see below).
_hid_runloop_primed = False

# --- dropped-link recovery -------------------------------------------------
# The bridge re-enumerates on its own schedule after a reset, so reopening the
# instant the old handle died lands in the middle of that and fails on a device
# that is a fraction of a second from being fine. One short settle buys the
# common case; the cooldown is what keeps a bridge that is genuinely gone from
# turning every tick into a USB enumeration.
LINK_SETTLE_S = 0.25
LINK_RETRY_COOLDOWN_S = 2.0


class Ch347LinkLost(RuntimeError):
    """The USB link to the bridge dropped and could not be reopened.

    Raised instead of the bare `OSError: read error` that hidapi surfaces, which
    reads like a bad transfer when it actually means the handle is dead. hidapi
    returns -1 from `hid_read_timeout` only when its per-device read thread has
    stopped — the device was removed from the bus, or the handle was closed. A
    transfer that merely timed out returns 0 and never reaches here.
    """


def prime_hid_runloop() -> bool:
    """Pin hidapi's process-global IOHID manager to the **main** thread's run
    loop. Call once, from the main thread, before any worker thread can reach
    the USB driver. Returns True when the manager is pinned (or there is
    nothing to pin), False when the caller is too late or `hid` is missing.

    macOS hidapi creates one process-global `IOHIDManager` on its first call
    and schedules it on `CFRunLoopGetCurrent()` — the run loop of whichever
    thread got there first (`init_hid_manager`). IOKit stores that
    `CFRunLoopRef` *without retaining it*: the run loop's retain count stays 1,
    held by the thread alone, so it is deallocated when that thread exits.
    Every later `hid.enumerate()` re-runs `IOHIDManagerSetDeviceMatching`,
    whose device applier calls `IOHIDDeviceScheduleWithRunLoop` on the stored
    pointer — freed memory once that thread is gone. The process then dies
    inside `CFRunLoopAddSource` with an uncaught Objective-C exception
    (`-[NSIndirectTaggedPointerString member:]: unrecognized selector`), which
    is an `abort()`, not something a Python `except` can catch.

    Flask answers every request on its own short-lived thread, so without this
    the first Hardware Setup action that opens the CH347 (Detect / Rescan, a
    port test, a commission session) pins the manager to a thread that is gone
    by the time the response is sent — and the *next* hardware action kills the
    server. The main thread's run loop lives as long as the process, so priming
    it costs one USB enumeration at boot and closes the window for good.

    Priming runs even in sim mode: the operator can switch a running session to
    real mode from the Hardware Setup screen, and by then the main thread is no
    longer the one asking.
    """
    global _hid_runloop_primed
    if _hid_runloop_primed:
        return True
    if sys.platform != "darwin":
        # Only the macOS backend keeps a run-loop-bound global manager; the
        # hidraw / libusb backends have nothing to pin.
        _hid_runloop_primed = True
        return True
    if threading.current_thread() is not threading.main_thread():
        # Priming here would pin the manager to *this* thread's run loop, which
        # is the bug. Report it instead: the boot order is what needs fixing.
        return False
    try:
        import hid
    except ImportError:
        return False   # no USB driver in this build; nothing can reach hidapi
    try:
        with _OPEN_LOCK:
            hid.enumerate()
    except Exception:
        return False
    _hid_runloop_primed = True
    return True


class Ch347Device:
    """One shared transport instance for all three real interfaces + self-test."""

    def __init__(
        self, device_port: Optional[str] = None, gpio_transfer_cs: str = "cs0"
    ) -> None:
        self.device_port = device_port
        if gpio_transfer_cs not in _PAD_TO_CS:
            raise ValueError(f"gpio_transfer_cs must be one of {list(_PAD_TO_CS)}")
        # The CS pad the SPI engine asserts while a GPIO-latched chip is clocked.
        # `build_real_interfaces` overrides it with a pad no '595 latches on.
        self.gpio_transfer_cs = gpio_transfer_cs
        self._dev: "Optional[CH347HIDDev]" = None
        self._lock = threading.RLock()
        # Dropped-link state. `_recovering` suppresses recovery *during* a
        # recovery, so a reconnect hook whose own writes fail cannot recurse.
        self._on_reconnect: List[Callable[[], None]] = []
        self._recovering = False
        self._link_down = False
        self._reconnect_count = 0
        self._last_link_error: Optional[str] = None
        self._next_retry_at = 0.0

    # -- dropped-link recovery -------------------------------------------
    def add_reconnect_hook(self, hook: Callable[[], None]) -> None:
        """Register work to redo on the far side of a reconnect.

        A reopened bridge is a *blank* bridge: `open()` re-inits its I2C and SPI
        engines, but every chip downstream kept whatever state it had — and if
        the bridge browned out, so did they, which leaves a '595 holding
        undefined register contents on a solenoid. `build_real_interfaces`
        registers the same live-init it runs at session build, so the board
        comes back de-energized rather than in whatever state the glitch left.

        A hook that raises fails the reconnect: a bridge that cannot be put back
        into a known-safe state is not a usable link.
        """
        self._on_reconnect.append(hook)

    def link_status(self) -> Dict[str, Any]:
        """Diagnostic read-back of the USB link. Deliberately takes no lock: a
        status probe must answer while the bus is stalled mid-recovery, which is
        exactly when someone is asking."""
        return {
            "open": self._dev is not None,
            "link_down": self._link_down,
            "reconnects": self._reconnect_count,
            "last_error": self._last_link_error,
        }

    def _run(self, op: Callable[[], Any], what: str) -> Any:
        """Run one bus operation, recovering once from a dropped USB link.

        Every public bus method funnels through here, so the retry replays a
        whole transaction — select line asserted, bytes clocked, latch pulsed —
        rather than resuming a half-finished one against a fresh handle.
        """
        with self._lock:
            if self._dev is None:
                raise Ch347LinkLost(
                    f"{what}: the CH347 is not open"
                    + (f" ({self._last_link_error})" if self._last_link_error else "")
                )
            try:
                return op()
            except OSError as exc:
                # hidapi's dead-handle signal (see `Ch347LinkLost`). Anything
                # else is a real protocol/usage error and is left alone.
                if self._recovering:
                    raise
                self._note_link_lost(exc, what)
                if not self._recover():
                    raise Ch347LinkLost(
                        f"{what}: USB link to the CH347 is down ({exc})"
                    ) from exc
                try:
                    result = op()
                except OSError as retry_exc:
                    # Reopened, still not working: a bridge that enumerates and
                    # then fails every transfer. The cooldown `_recover` set on
                    # the way in is deliberately left standing — clearing it here
                    # is what would turn this into a reopen every single tick.
                    self._last_link_error = str(retry_exc)
                    raise Ch347LinkLost(
                        f"{what}: the CH347 reopened but the retry still failed "
                        f"({retry_exc})"
                    ) from retry_exc
                # A transaction actually landed: only now is the link back.
                self._link_down = False
                self._next_retry_at = 0.0
                _LOG.warning(
                    "CH347 link is carrying traffic again after reconnect #%d",
                    self._reconnect_count,
                )
                return result

    def _note_link_lost(self, exc: OSError, what: str) -> None:
        """Record the drop, logging only the transition. A down link fails every
        operation of every tick; logging each one buries the one line that says
        when it started."""
        self._last_link_error = str(exc)
        if not self._link_down:
            self._link_down = True
            _LOG.error(
                "CH347 USB link dropped during %s (%s) — the bridge fell off the "
                "bus; reopening", what, exc,
            )

    def _recover(self) -> bool:
        """Close the stale handle and reopen the bridge. True when the link is
        usable again. Caller holds `self._lock`.

        Reopening is by VID/PID, not by the path the session opened, because a
        bridge that re-enumerated comes back on a different path — the stale one
        can never be revived.
        """
        now = time.monotonic()
        if now < self._next_retry_at:
            return False
        self._next_retry_at = now + LINK_RETRY_COOLDOWN_S
        self._recovering = True
        try:
            try:
                if self._dev is not None:
                    self._dev.close()
            except Exception:
                pass                   # already dead; closing is best-effort
            self._dev = None
            time.sleep(LINK_SETTLE_S)
            self.open()                # re-inits I2C + SPI on the new handle
            for hook in list(self._on_reconnect):
                hook()
        except Exception as exc:
            self._last_link_error = str(exc)
            try:
                if self._dev is not None:
                    self._dev.close()
            except Exception:
                pass
            self._dev = None
            _LOG.warning("CH347 reconnect attempt failed: %s", exc)
            return False
        finally:
            self._recovering = False
        self._reconnect_count += 1
        _LOG.warning(
            "CH347 reopened (reconnect #%d); outputs re-initialized to the safe "
            "state", self._reconnect_count,
        )
        # `_link_down` and the cooldown stay as they are: a handle is not a
        # working link. `_run` clears both once a transaction actually lands.
        return True

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        from ch347api import CH347HIDDev

        try:
            # `CH347HIDDev()` calls `hid.enumerate()` before it opens the path,
            # and that walks hidapi's unlocked process-global manager. Two
            # threads opening at once (a Hardware Setup request while a session
            # is coming up) would race inside IOKit, so opens are serialized —
            # see `_OPEN_LOCK` / `prime_hid_runloop`.
            with _OPEN_LOCK:
                self._dev = CH347HIDDev()  # opens by VID/PID, interface 1
        except TypeError as exc:
            # `ch347api` looks the bridge up by VID/PID and hands the result
            # straight to `hid.open_path`, so "nothing plugged in" arrives as
            # `TypeError: expected bytes, NoneType found` — a message about the
            # library's internals that reads, on the Hardware Setup screen, as
            # if the app were broken. Say what actually happened instead. If the
            # library ever raises a real not-found error, it lands in the branch
            # below, which still reads correctly.
            raise RuntimeError(
                "no CH347 found on USB — check the bridge is plugged in"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"could not open the CH347 ({exc}) — it may already be open in "
                "another program"
            ) from exc
        self._init_i2c()
        self._init_spi()

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                finally:
                    self._dev = None

    def _init_i2c(self) -> None:
        self._dev.init_I2C()

    def _init_spi(self) -> None:
        self._dev.init_SPI(
            clock_freq_level=SPI_CLOCK_LEVEL, is_MSB=True, mode=0,
            CS1_high=CS1_HIGH, CS2_high=CS2_HIGH,
        )

    def _ensure(self, bus: str) -> None:
        if not REINIT_EACH_OP:
            return
        if bus == BUS_I2C:
            self._init_i2c()
        else:
            self._init_spi()

    # -- I2C -------------------------------------------------------------
    def i2c_write(self, addr: int, data: bytes) -> bool:
        def op() -> bool:
            self._ensure(BUS_I2C)
            return bool(self._dev.i2c_write(addr, bytes(data)))

        return self._run(op, "i2c_write")

    def i2c_read(self, addr: int, length: int) -> Tuple[bool, bytes]:
        def op() -> Tuple[bool, bytes]:
            self._ensure(BUS_I2C)
            status, raw = self._dev.i2c_read(addr, length)
            return bool(status), bytes(raw) if raw else b""

        return self._run(op, "i2c_read")

    def i2c_scan(self) -> List[int]:
        """Probe every 7-bit address and report the ones that ACK.

        `ch347api` has no address-existence call, so the probe is a one-byte
        write — on nearly every chip that is a register-pointer set, which
        changes no state a later read does not set again anyway.

        The byte is load-bearing, not padding. `__i2c_read_write_raw` skips the
        address ACK when the frame carries nothing but the address
        (`if len(data) == 1: ack_stops -= 1`), so its ACK check reduces to
        `sum(b"") == len(b"")` and an address-only write reports success for
        every address on the bus. Sending one data byte keeps the ACK bytes in
        the window it actually inspects.
        """
        def probe(addr: int) -> bool:
            self._ensure(BUS_I2C)
            return bool(self._dev.i2c_write(addr, b"\x00"))

        with self._lock:
            first = 0x03
            found: List[int] = []
            # The first probe runs through `_run` rather than the swallowing
            # loop below, so a dropped link surfaces as itself (and gets its one
            # reconnect attempt). Otherwise the `except` swallows 117 OSErrors
            # in a row and a bridge that is gone reports as a bus with nothing
            # on it — on the Hardware Setup screen, "no chips detected".
            if self._run(lambda: probe(first), "i2c_scan"):
                found.append(first)
            for addr in range(first + 1, 0x78):
                try:
                    if probe(addr):
                        found.append(addr)
                except Exception:
                    pass
            return found

    # -- SPI shift registers --------------------------------------------
    def latch_out(self, data: bytes, select: str) -> None:
        """Clock `data` out and pulse the '595 latch on `select`."""

        def op() -> None:
            self._ensure(BUS_SPI)
            if select in _PAD_TO_CS:
                self._cs(select, True)         # assert (drives CS0 low)
                self._dev.spi_write(bytes(data))
                self._cs(select, False)        # deassert -> rising edge latches
            else:
                self._gpio(select, False)      # RCLK idle low
                with self._transfer_cs():
                    self._dev.spi_write(bytes(data))
                self._gpio(select, True)       # rising edge latches
                time.sleep(GPIO_LATCH_PULSE_S)
                self._gpio(select, False)

        self._run(op, "latch_out")

    def load_in(self, count: int, select: str) -> bytes:
        """Assert the '165 load on `select` and clock `count` bytes in."""

        def op() -> bytes:
            self._ensure(BUS_SPI)
            if select in _PAD_TO_CS:
                self._cs(select, True)         # assert (drives CS1 high) -> shift
                rx = self._dev.spi_read(count)
                self._cs(select, False)
            else:
                self._gpio(select, True)       # high -> freeze inputs, shift mode
                with self._transfer_cs():
                    rx = self._dev.spi_read(count)
                self._gpio(select, False)      # low -> transparent parallel load
            return bytes(rx)

        return self._run(op, "load_in")

    @contextmanager
    def _transfer_cs(self) -> Iterator[None]:
        """Hold a chip-select enabled for the length of a GPIO-latched transfer.

        A GPIO-latched shift register still clocks its data over the SPI
        peripheral, and `ch347api` refuses `spi_write` / `spi_read` unless some
        CS is enabled ("no CS enabled yet"). The CH347 asserts that CS around the
        transfer and releases it after, so the pad must be one no '595 latches
        on — `build_real_interfaces` picks it.
        """
        self._cs(self.gpio_transfer_cs, True)
        try:
            yield
        finally:
            self._cs(self.gpio_transfer_cs, False)

    # -- select-line primitives -----------------------------------------
    def _cs(self, select: str, assert_: bool) -> None:
        if _PAD_TO_CS[select] == 1:
            self._dev.set_CS1(assert_)
        else:
            self._dev.set_CS2(assert_)

    def _gpio(self, select: str, level: bool) -> None:
        pin = int(select[len("gpio"):])
        self._gpio_set_output(pin, level)

    def _gpio_set_output(self, pin: int, level: bool) -> None:
        if not 0 <= pin <= 7:
            raise ValueError(f"gpio pin must be 0-7, got {pin}")
        data = [_GPIO_UNCHANGED] * 8
        data[pin] = _GPIO_OUT_HIGH if level else _GPIO_OUT_LOW
        payload = struct.pack("<BHBH", 0x00, 11, 0xCC, 8) + bytes(data)
        self._dev.write(payload)
        # The 0xCC command echoes the eight pin states. An empty frame means the
        # bridge did not accept it; without this check a chip whose latch never
        # moved would still self-test as reachable.
        if not self._dev.read(512, timeout_ms=200):
            raise RuntimeError(f"no response from CH347 on GPIO command (pin {pin})")


def open_ch347_device(device_port: Optional[str] = None) -> Ch347Device:
    """Construct and open a `Ch347Device` (raises on missing/busy hardware)."""
    device = Ch347Device(device_port)
    device.open()
    return device
