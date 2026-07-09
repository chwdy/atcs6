"""74HC165 shift-register input chain (SPI), ported from i2c.py.

Reads block occupancy: one input bit per block. Cascaded chips give 8×N bits.
The device's `load_in` asserts the configured load line and clocks the bits in;
this class owns only the bit unpacking.
"""

from typing import Any


class HC165Chain:
    def __init__(self, device: Any, select: str = "cs1", chain_length: int = 1) -> None:
        self.device = device
        self.select = select
        self.chain_length = max(1, int(chain_length))

    @property
    def num_inputs(self) -> int:
        return self.chain_length * 8

    def read_all(self) -> bytes:
        return bytes(self.device.load_in(self.chain_length, self.select))

    def read_bit(self, index: int) -> bool:
        data = self.read_all()
        byte_idx, bit_idx = divmod(index, 8)
        if byte_idx >= len(data):
            return False
        return bool(data[byte_idx] & (1 << bit_idx))

    def probe(self) -> bool:
        """Success == a read returned the expected number of bytes."""
        try:
            return len(self.read_all()) == self.chain_length
        except Exception:
            return False
