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

Importing this module requires `ch347api`. `hardware_config.build_session_interfaces`
imports it lazily and turns an ImportError into `HardwareBackendUnavailable` so a
build without the USB driver still boots in sim mode.
"""

import struct
import threading
from typing import List, Optional, Tuple

from ch347api import CH347HIDDev

from hardware.catalog import BUS_I2C, BUS_SPI

# If the Phase-0 coexistence probe shows I2C does NOT survive `init_SPI()`, set
# this True: the device then re-inits the bus before each bus-class op. Default
# assumes coexistence (Strategy A: init both once at open).
REINIT_EACH_OP = False

SPI_CLOCK_LEVEL = 5   # 1.875 MHz — matches the demos; raise once validated
CS1_HIGH = False      # pad cs0 (lib CS1): active-low '595 latch
CS2_HIGH = True       # pad cs1 (lib CS2): active-high '165 load

# Physical pad name -> ch347api CS selector (1 => set_CS1, 2 => set_CS2).
_PAD_TO_CS = {"cs0": 1, "cs1": 2}

# CH347 0xCC "set GPIO" command bytes (documented, not vendor-verified; from
# gpio.ipynb). v1 uses GPIO only as an output latch line.
_GPIO_OUT_LOW = 0xF0
_GPIO_OUT_HIGH = 0xF8
_GPIO_UNCHANGED = 0x00


class Ch347Device:
    """One shared transport instance for all three real interfaces + self-test."""

    def __init__(self, device_port: Optional[str] = None) -> None:
        self.device_port = device_port
        self._dev: Optional[CH347HIDDev] = None
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._dev = CH347HIDDev()          # opens by VID/PID, interface 1
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
        with self._lock:
            self._ensure(BUS_I2C)
            return bool(self._dev.i2c_write(addr, bytes(data)))

    def i2c_read(self, addr: int, length: int) -> Tuple[bool, bytes]:
        with self._lock:
            self._ensure(BUS_I2C)
            status, raw = self._dev.i2c_read(addr, length)
            return bool(status), bytes(raw) if raw else b""

    def i2c_scan(self) -> List[int]:
        with self._lock:
            self._ensure(BUS_I2C)
            found: List[int] = []
            for addr in range(0x03, 0x78):
                try:
                    if self._dev.i2c_exists(addr):
                        found.append(addr)
                except Exception:
                    pass
            return found

    # -- SPI shift registers --------------------------------------------
    def latch_out(self, data: bytes, select: str) -> None:
        """Clock `data` out and pulse the '595 latch on `select`."""
        with self._lock:
            self._ensure(BUS_SPI)
            if select in _PAD_TO_CS:
                self._cs(select, True)         # assert (drives CS0 low)
                self._dev.spi_write(bytes(data))
                self._cs(select, False)        # deassert -> rising edge latches
            else:
                self._gpio(select, False)      # idle low
                self._dev.spi_write(bytes(data))
                self._gpio(select, True)       # rising edge latches
                self._gpio(select, False)

    def load_in(self, count: int, select: str) -> bytes:
        """Assert the '165 load on `select` and clock `count` bytes in."""
        with self._lock:
            self._ensure(BUS_SPI)
            if select in _PAD_TO_CS:
                self._cs(select, True)         # assert (drives CS1 high) -> shift
                rx = self._dev.spi_read(count)
                self._cs(select, False)
            else:
                self._gpio(select, True)       # high -> shift mode
                rx = self._dev.spi_read(count)
                self._gpio(select, False)      # low -> transparent load
            return bytes(rx)

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
        self._dev.read(512, timeout_ms=200)


def open_ch347_device(device_port: Optional[str] = None) -> Ch347Device:
    """Construct and open a `Ch347Device` (raises on missing/busy hardware)."""
    device = Ch347Device(device_port)
    device.open()
    return device
