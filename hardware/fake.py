"""In-memory CH347 stand-in for tests and hardware-free UI dev.

Implements the same duck-typed device surface as `Ch347Device`
(`i2c_write` / `i2c_read` / `i2c_scan` / `latch_out` / `load_in`) over plain
dicts, plus test helpers to declare which I2C addresses are present and to set
'165 input bits. The chip classes, interface adapters, and self-test run against
this unchanged, so the whole real-mode stack is exercised without USB hardware.
"""

from typing import Dict, Iterable, List, Optional, Tuple


class FakeCh347Device:
    def __init__(
        self,
        present_addresses: Iterable[int] = (),
        inputs: Optional[Dict[str, bytes]] = None,
    ) -> None:
        self.present = set(present_addresses)
        self.opened = False
        self.closed = False
        # PWM state: address -> {channel: (on, off)}
        self.pwm: Dict[int, Dict[int, Tuple[int, int]]] = {}
        # '595 last latched bytes per select line.
        self.latched: Dict[str, bytes] = {}
        # '165 input bytes per select line (settable by tests).
        self.inputs: Dict[str, bytearray] = {
            k: bytearray(v) for k, v in (inputs or {}).items()
        }
        self._reg_ptr: Dict[int, int] = {}
        # 16-bit register file per I2C address (INA3221 shunt/bus/config/id).
        self.registers: Dict[int, Dict[int, int]] = {}

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    # -- I2C -------------------------------------------------------------
    def i2c_write(self, addr: int, data: bytes) -> bool:
        if addr not in self.present:
            return False
        data = bytes(data)
        if data:
            self._reg_ptr[addr] = data[0]
        # Emulate a PCA9685 LEDn_ON_L block write (reg, on_l, on_h, off_l, off_h).
        if len(data) == 5 and data[0] >= 0x06:
            channel = (data[0] - 0x06) // 4
            on = data[1] | (data[2] << 8)
            off = data[3] | (data[4] << 8)
            self.pwm.setdefault(addr, {})[channel] = (on, off)
        # Emulate a 16-bit register write [reg, high, low] (INA3221 config etc.).
        elif len(data) == 3:
            self.registers.setdefault(addr, {})[data[0]] = (data[1] << 8) | data[2]
        return True

    def i2c_read(self, addr: int, length: int) -> Tuple[bool, bytes]:
        if addr not in self.present:
            return False, b""
        reg = self._reg_ptr.get(addr)
        regs = self.registers.get(addr, {})
        if reg is not None and reg in regs:
            value = regs[reg]
            raw = bytes([(value >> 8) & 0xFF, value & 0xFF])
            return True, (raw + bytes(length))[:length]
        # Serve a PCA9685 LEDn_ON_L group back from the recorded PWM state, so a
        # driver can read its own commanded duty back (the occupancy test's
        # drive-state check).
        if reg is not None and reg >= 0x06:
            channel, offset = divmod(reg - 0x06, 4)
            if offset == 0 and channel in self.pwm.get(addr, {}):
                on, off = self.pwm[addr][channel]
                raw = bytes([on & 0xFF, (on >> 8) & 0xFF, off & 0xFF, (off >> 8) & 0xFF])
                return True, (raw + bytes(length))[:length]
        return True, bytes(length)   # benign zeros; enough for a MODE1 probe

    def i2c_scan(self) -> List[int]:
        return sorted(self.present)

    # -- SPI shift registers --------------------------------------------
    def latch_out(self, data: bytes, select: str) -> None:
        self.latched[select] = bytes(data)

    def load_in(self, count: int, select: str) -> bytes:
        raw = bytes(self.inputs.get(select, bytearray(count)))
        return (raw + bytes(count))[:count]

    # -- test helpers ----------------------------------------------------
    def duty_cycle(self, addr: int, channel: int) -> float:
        """Duty last commanded on a PCA9685 channel (0.0 when never set)."""
        on, off = self.pwm.get(addr, {}).get(channel, (0, 0))
        return off / 4095.0

    def latched_bit(self, select: str, index: int) -> bool:
        """State of a '595 output bit (accounts for the cascade byte reversal)."""
        data = self.latched.get(select)
        if not data:
            return False
        # latch_out receives the chain reversed; undo it to index logically.
        logical = bytes(reversed(data))
        byte_idx, bit_idx = divmod(index, 8)
        if byte_idx >= len(logical):
            return False
        return bool(logical[byte_idx] & (1 << bit_idx))

    def set_input_bit(self, select: str, index: int, value: bool, width: int = 1) -> None:
        """Set a '165 input bit that a later `load_in(width, select)` returns."""
        buf = self.inputs.setdefault(select, bytearray(width))
        byte_idx, bit_idx = divmod(index, 8)
        while len(buf) <= byte_idx:
            buf.append(0)
        if value:
            buf[byte_idx] |= (1 << bit_idx)
        else:
            buf[byte_idx] &= ~(1 << bit_idx)

    # -- INA3221 helpers -------------------------------------------------
    _INA_SHUNT_REG = {1: 0x01, 2: 0x03, 3: 0x05}
    _INA_BUS_REG = {1: 0x02, 2: 0x04, 3: 0x06}

    def set_register(self, addr: int, reg: int, value: int) -> None:
        """Set a raw 16-bit register a later `i2c_read(addr, 2)` returns (used
        for the INA3221 identity register and other direct-register reads)."""
        self.registers.setdefault(addr, {})[reg] = value & 0xFFFF

    def set_ina3221_current(
        self, addr: int, channel: int, amps: float, shunt_ohms: float = 0.1
    ) -> None:
        """Set an INA3221 channel's shunt register so `read_current(channel)`
        returns ~`amps` for a driver reading the same `shunt_ohms` (40 µV LSB,
        signed 13-bit)."""
        counts = int(round(amps * shunt_ohms / 40e-6))
        counts = max(-4096, min(4095, counts))
        self.set_register(addr, self._INA_SHUNT_REG[channel], (counts & 0x1FFF) << 3)

    def set_ina3221_bus_voltage(self, addr: int, channel: int, volts: float) -> None:
        """Set an INA3221 channel's bus register so `read_bus_voltage(channel)`
        returns ~`volts` (8 mV LSB, unsigned 13-bit)."""
        counts = max(0, min(0x1FFF, int(round(volts / 8e-3))))
        self.set_register(addr, self._INA_BUS_REG[channel], counts << 3)

    # -- INA219 helpers --------------------------------------------------
    def set_ina219_current(self, addr: int, amps: float, shunt_ohms: float = 0.1) -> None:
        """Set the INA219's shunt register so `read_current()` returns ~`amps`
        for a driver reading the same `shunt_ohms` (10 uV LSB, signed 16-bit)."""
        counts = int(round(amps * shunt_ohms / 10e-6))
        counts = max(-32768, min(32767, counts))
        self.set_register(addr, 0x01, counts & 0xFFFF)

    def set_ina219_bus_voltage(self, addr: int, volts: float) -> None:
        """Set the INA219's bus register so `read_bus_voltage()` returns
        ~`volts` (4 mV LSB, unsigned 13-bit)."""
        counts = max(0, min(0x1FFF, int(round(volts / 4e-3))))
        self.set_register(addr, 0x02, counts << 3)
