"""PCA9685 16-channel PWM driver (I2C), ported from i2c.py.

Drives track power: each block's bipolar track port is an H-bridge channel pair,
so this class exposes per-channel duty control and the interface layer
(`TrackDriver`) turns a signed voltage into the two channel writes.
"""

import time
from typing import Any


class PCA9685:
    MODE1 = 0x00
    MODE2 = 0x01
    PRESCALE = 0xFE
    LED0_ON_L = 0x06   # per-channel register group, 4 registers apart

    RESTART = 0x80
    SLEEP = 0x10
    AI = 0x20          # auto-increment

    NUM_CHANNELS = 16

    def __init__(self, device: Any, address: int = 0x40, freq_hz: int = 1000) -> None:
        self.device = device
        self.address = address
        self.freq_hz = freq_hz
        self._configured = False

    # -- low-level register access ---------------------------------------
    def _write8(self, reg: int, value: int) -> None:
        ok = self.device.i2c_write(self.address, bytes([reg, value & 0xFF]))
        if not ok:
            raise IOError(f"PCA9685@{hex(self.address)} write reg {hex(reg)} failed (no ACK)")

    def _read8(self, reg: int) -> int:
        if not self.device.i2c_write(self.address, bytes([reg])):
            raise IOError(f"PCA9685@{hex(self.address)} set reg ptr {hex(reg)} failed (no ACK)")
        status, raw = self.device.i2c_read(self.address, 1)
        if not status:
            raise IOError(f"PCA9685@{hex(self.address)} read reg {hex(reg)} failed (no ACK)")
        return raw[0]

    # -- configuration ---------------------------------------------------
    def configure(self) -> None:
        """Reset and set the shared PWM frequency. Call once before driving."""
        self.reset()
        self.set_pwm_freq(self.freq_hz)
        self._configured = True

    def reset(self) -> None:
        self._write8(self.MODE1, 0x00)
        time.sleep(0.005)

    def set_pwm_freq(self, freq_hz: int) -> None:
        prescale = round(25_000_000.0 / (4096 * freq_hz)) - 1
        prescale = max(3, min(255, prescale))
        old_mode = self._read8(self.MODE1)
        self._write8(self.MODE1, (old_mode & 0x7F) | self.SLEEP)  # sleep to set prescale
        self._write8(self.PRESCALE, prescale)
        self._write8(self.MODE1, old_mode)
        time.sleep(0.005)
        self._write8(self.MODE1, old_mode | self.RESTART | self.AI)

    # -- output ----------------------------------------------------------
    def set_pwm(self, channel: int, on: int, off: int) -> None:
        reg = self.LED0_ON_L + 4 * channel
        ok = self.device.i2c_write(self.address, bytes([
            reg,
            on & 0xFF, (on >> 8) & 0xFF,
            off & 0xFF, (off >> 8) & 0xFF,
        ]))
        if not ok:
            raise IOError(f"PCA9685@{hex(self.address)} set channel {channel} failed (no ACK)")

    def set_duty_cycle(self, channel: int, duty: float) -> None:
        """Set channel output by duty cycle 0.0..1.0 (0 count -> off count)."""
        duty = max(0.0, min(1.0, duty))
        self.set_pwm(channel, 0, int(duty * 4095))

    def read_duty_cycle(self, channel: int) -> float:
        """Duty the chip is currently outputting on a channel, 0.0..1.0, read
        back from its own registers.

        This is what the block occupancy test uses to tell whether a block is
        being driven: the chip itself is the truth, so the answer holds even when
        the drive was commanded by some other process.
        """
        reg = self.LED0_ON_L + 4 * channel
        if not self.device.i2c_write(self.address, bytes([reg])):
            raise IOError(f"PCA9685@{hex(self.address)} set reg ptr {hex(reg)} failed (no ACK)")
        status, raw = self.device.i2c_read(self.address, 4)
        if not status or len(raw) < 4:
            raise IOError(f"PCA9685@{hex(self.address)} read channel {channel} failed (no ACK)")
        on = raw[0] | (raw[1] << 8)
        off = raw[2] | (raw[3] << 8)
        # Bit 12 of either word is the full-off / full-on flag, which overrides
        # the 12-bit count. Full-off wins when both are set.
        if off & 0x1000:
            return 0.0
        if on & 0x1000:
            return 1.0
        return min(1.0, (off & 0x0FFF) / 4095.0)

    def all_off(self) -> None:
        for channel in range(self.NUM_CHANNELS):
            self.set_duty_cycle(channel, 0.0)

    def probe(self) -> bool:
        """True if the chip ACKs a MODE1 read (used by self-test)."""
        try:
            self._read8(self.MODE1)
            return True
        except (IOError, Exception):
            return False
