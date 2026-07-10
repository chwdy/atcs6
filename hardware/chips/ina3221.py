"""INA3221 3-channel current/voltage monitor (I2C), ported from the hybrid demo.

Reads block occupancy by current draw. The chip sits high-side on the 12 V feed
into a block's H-bridge supply (DRV8873 VM), one channel per block, and reports
the shunt voltage across an external sense resistor. Unlike the INA219 it has no
current/power calibration registers — current is computed directly from the
shunt reading:

    current = shunt_voltage / shunt_ohms

so this class owns only the register math; the sense resistor value lives in
config. This is the *driving* half of hybrid occupancy: while a block is powered
its rail voltage is a poor occupancy signal, so `abs(current)` above a
per-channel threshold is what marks it occupied (regeneration/back-EMF counts as
occupancy because the magnitude is used). The idle half stays on the digital
detector (`HC165Chain`).

Wiring that matters — a reversed or in-series INA3221 can be destroyed: each
channel must be high-side on the supply feed (IN+ to 12 V, IN- to the H-bridge
VM), never in series with Rail A / Rail B, whose polarity switches under PWM.
"""

import time
from typing import Any


class INA3221:
    NUM_CHANNELS = 3

    REG_CONFIG = 0x00
    REG_SHUNT_V = {1: 0x01, 2: 0x03, 3: 0x05}
    REG_BUS_V = {1: 0x02, 2: 0x04, 3: 0x06}
    REG_MANUF_ID = 0xFE            # reads 0x5449 ("TI") on a genuine INA3221

    MANUFACTURER_ID = 0x5449

    RESET = 0x8000
    CH_ENABLE = {1: 0x4000, 2: 0x2000, 3: 0x1000}

    # Config bits: no averaging, 1.1 ms bus + 1.1 ms shunt conversion, continuous
    # shunt+bus mode. Combined with the three enable bits this is the standard
    # power-up-style config for reading all channels continuously.
    AVG_1 = 0x0000
    VBUS_CT_1100US = 0x0100
    VSH_CT_1100US = 0x0020
    MODE_CONT_SHUNT_BUS = 0x0007

    SHUNT_LSB_V = 40e-6           # 40 µV per shunt-register count
    BUS_LSB_V = 8e-3              # 8 mV per bus-register count

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
            raise IOError(f"INA3221@{hex(self.address)} write reg {hex(reg)} failed (no ACK)")

    def _read_reg16(self, reg: int) -> int:
        # CH347-safe pattern (same as the PCA9685 driver): write the register
        # pointer with a STOP, then read the two data bytes back big-endian.
        if not self.device.i2c_write(self.address, bytes([reg])):
            raise IOError(f"INA3221@{hex(self.address)} set reg ptr {hex(reg)} failed (no ACK)")
        status, raw = self.device.i2c_read(self.address, 2)
        if not status or len(raw) < 2:
            raise IOError(f"INA3221@{hex(self.address)} read reg {hex(reg)} failed (no ACK)")
        return (raw[0] << 8) | raw[1]

    @staticmethod
    def _sign_extend(value: int, bits: int) -> int:
        sign = 1 << (bits - 1)
        return (value ^ sign) - sign

    @staticmethod
    def _check_channel(channel: int) -> int:
        if channel not in (1, 2, 3):
            raise ValueError(f"INA3221 channel must be 1, 2, or 3, got {channel}")
        return channel

    # -- configuration ---------------------------------------------------
    def configure(self) -> None:
        """Reset, enable all three channels, and start continuous shunt+bus
        conversion. Call once before reading current; one chip serves up to three
        blocks, so every channel is enabled rather than a single selected one."""
        self._write_reg16(self.REG_CONFIG, self.RESET)
        time.sleep(0.01)
        config = (
            self.CH_ENABLE[1] | self.CH_ENABLE[2] | self.CH_ENABLE[3]
            | self.AVG_1 | self.VBUS_CT_1100US | self.VSH_CT_1100US
            | self.MODE_CONT_SHUNT_BUS
        )
        self._write_reg16(self.REG_CONFIG, config)
        self._configured = True

    # -- reads -----------------------------------------------------------
    def read_shunt_voltage(self, channel: int) -> float:
        """Shunt voltage in mV, signed (sign follows the IN+ -> IN- direction)."""
        raw = self._read_reg16(self.REG_SHUNT_V[self._check_channel(channel)])
        counts = self._sign_extend((raw >> 3) & 0x1FFF, 13)
        return counts * self.SHUNT_LSB_V * 1000.0

    def read_bus_voltage(self, channel: int) -> float:
        """Bus voltage in V (this block's supply rail into the H-bridge)."""
        raw = self._read_reg16(self.REG_BUS_V[self._check_channel(channel)])
        return ((raw >> 3) & 0x1FFF) * self.BUS_LSB_V

    def read_current(self, channel: int) -> float:
        """Current in A, signed. `abs(current)` above threshold marks occupancy."""
        return (self.read_shunt_voltage(channel) / 1000.0) / self.shunt_ohms

    def read_manufacturer_id(self) -> int:
        """Identity register; reads `MANUFACTURER_ID` (0x5449) on a genuine part.
        Lets self-test tell a real INA3221 from some other chip at the address."""
        return self._read_reg16(self.REG_MANUF_ID)

    def probe(self) -> bool:
        """True if the chip ACKs a config-register read (used by self-test)."""
        try:
            self._read_reg16(self.REG_CONFIG)
            return True
        except Exception:
            return False
