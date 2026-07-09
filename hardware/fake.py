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
        return True

    def i2c_read(self, addr: int, length: int) -> Tuple[bool, bytes]:
        if addr not in self.present:
            return False, b""
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
