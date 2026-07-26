"""INA219 single-channel current/voltage monitor (I2C).

The one-block sibling of `INA3221`: same job (occupancy by current draw), same
wiring rule, one channel instead of three. The chip sits high-side on the 12 V
feed into a block's H-bridge supply (DRV8871 VM) and reports the voltage across
an external sense resistor.

The INA219 *does* have current/power calibration registers, but this driver does
not use them: current is computed from the shunt reading exactly as the INA3221
driver does,

    current = shunt_voltage / shunt_ohms

so both current-sense chips share one code shape and one config field (the sense
resistor), and there is no calibration value to get wrong. The chip's own CURRENT
and POWER registers stay unread.

To keep the two interchangeable behind the occupancy interface, the read methods
take the same 1-based `channel` argument; the INA219 accepts only channel 1.

Wiring that matters — a reversed or in-series part can be destroyed: the channel
must be high-side on the supply feed (IN+ to 12 V, IN- to the H-bridge VM), never
in series with Rail A / Rail B, whose polarity switches under PWM.
"""

import time
from typing import Any


class INA219:
    NUM_CHANNELS = 1

    REG_CONFIG = 0x00
    REG_SHUNT_V = 0x01
    REG_BUS_V = 0x02

    RESET = 0x8000

    # 32 V bus range, PGA /8 (+/-320 mV shunt), 12-bit bus + shunt conversion,
    # continuous shunt+bus mode. This is the part's own power-up configuration,
    # written explicitly so a soft reset lands somewhere known.
    BRNG_32V = 0x2000
    PGA_8 = 0x1800
    BADC_12BIT = 0x0180
    SADC_12BIT = 0x0018
    MODE_CONT_SHUNT_BUS = 0x0007

    SHUNT_LSB_V = 10e-6           # 10 uV per shunt-register count
    BUS_LSB_V = 4e-3              # 4 mV per bus-register count

    def __init__(self, device: Any, address: int = 0x40, shunt_ohms: float = 0.1) -> None:
        if shunt_ohms <= 0:
            raise ValueError("shunt_ohms must be positive")
        self.device = device
        self.address = address
        self.shunt_ohms = shunt_ohms
        self._configured = False

    # -- low-level register access ---------------------------------------
    def _write_reg16(self, reg: int, value: int) -> None:
        ok = self.device.i2c_write(
            self.address, bytes([reg, (value >> 8) & 0xFF, value & 0xFF])
        )
        if not ok:
            raise IOError(f"INA219@{hex(self.address)} write reg {hex(reg)} failed (no ACK)")

    def _read_reg16(self, reg: int) -> int:
        # CH347-safe pattern (same as the PCA9685 / INA3221 drivers): write the
        # register pointer with a STOP, then read the two data bytes big-endian.
        if not self.device.i2c_write(self.address, bytes([reg])):
            raise IOError(f"INA219@{hex(self.address)} set reg ptr {hex(reg)} failed (no ACK)")
        status, raw = self.device.i2c_read(self.address, 2)
        if not status or len(raw) < 2:
            raise IOError(f"INA219@{hex(self.address)} read reg {hex(reg)} failed (no ACK)")
        return (raw[0] << 8) | raw[1]

    @staticmethod
    def _sign_extend(value: int, bits: int) -> int:
        sign = 1 << (bits - 1)
        return (value ^ sign) - sign

    @staticmethod
    def _check_channel(channel: int) -> int:
        if channel != 1:
            raise ValueError(f"INA219 has one channel; channel must be 1, got {channel}")
        return channel

    # -- configuration ---------------------------------------------------
    def configure(self) -> None:
        """Reset and start continuous shunt+bus conversion. Call once before
        reading current."""
        self._write_reg16(self.REG_CONFIG, self.RESET)
        time.sleep(0.01)
        config = (
            self.BRNG_32V | self.PGA_8 | self.BADC_12BIT
            | self.SADC_12BIT | self.MODE_CONT_SHUNT_BUS
        )
        self._write_reg16(self.REG_CONFIG, config)
        self._configured = True

    # -- reads -----------------------------------------------------------
    def read_shunt_voltage(self, channel: int = 1) -> float:
        """Shunt voltage in mV, signed (sign follows the IN+ -> IN- direction)."""
        self._check_channel(channel)
        raw = self._read_reg16(self.REG_SHUNT_V)
        return self._sign_extend(raw, 16) * self.SHUNT_LSB_V * 1000.0

    def read_bus_voltage(self, channel: int = 1) -> float:
        """Bus voltage in V (this block's supply rail into the H-bridge)."""
        self._check_channel(channel)
        raw = self._read_reg16(self.REG_BUS_V)
        return ((raw >> 3) & 0x1FFF) * self.BUS_LSB_V

    def read_current(self, channel: int = 1) -> float:
        """Current in A, signed. `abs(current)` above threshold marks occupancy."""
        return (self.read_shunt_voltage(channel) / 1000.0) / self.shunt_ohms

    def probe(self) -> bool:
        """True if the chip ACKs a config-register read (used by self-test).

        The INA219 has no manufacturer-id register, so unlike the INA3221 there
        is no identity check to distinguish it from another part at the address.
        """
        try:
            self._read_reg16(self.REG_CONFIG)
            return True
        except Exception:
            return False
