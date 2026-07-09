"""74HC595 shift-register output chain (SPI), ported from i2c.py / gpio.ipynb.

Drives switch throws: one output bit per switch. Cascaded chips give 8×N bits.
The device's `latch_out` clocks the bytes and pulses the configured select/latch
line; this class owns only the bit packing and the cascade byte order.
"""

from typing import Any


class HC595Chain:
    def __init__(self, device: Any, select: str = "cs0", chain_length: int = 1) -> None:
        self.device = device
        self.select = select
        self.chain_length = max(1, int(chain_length))
        self._state = bytearray(self.chain_length)

    @property
    def num_outputs(self) -> int:
        return self.chain_length * 8

    def write_all(self) -> None:
        # Shift registers are first-in-last-out: the farthest cascaded chip must
        # be clocked out first, so send the state reversed.
        self.device.latch_out(bytes(reversed(self._state)), self.select)

    def set_output(self, index: int, level: bool) -> None:
        """Set output `index` (0-based, continuous across the chain)."""
        byte_idx, bit_idx = divmod(index, 8)
        if level:
            self._state[byte_idx] |= (1 << bit_idx)
        else:
            self._state[byte_idx] &= ~(1 << bit_idx)
        self.write_all()

    def all_off(self) -> None:
        self._state = bytearray(self.chain_length)
        self.write_all()

    def probe(self) -> bool:
        """Write-only part: success == the SPI transaction issued without error."""
        try:
            self.write_all()
            return True
        except Exception:
            return False
