"""CH347 transport unit tests — the CS vs GPIO latch paths.

`Ch347Device` is the one module that talks to `ch347api`, so these tests stand a
fake `CH347HIDDev` behind it and assert the exact call sequence. The fake mirrors
the one library rule that shapes the GPIO path: `spi_write` / `spi_read` refuse to
run unless a chip-select is enabled ("no CS enabled yet").

Hardware facts these tests encode (verify on the rig before trusting the wiring):
- A '595 latched by a CS pad latches on the CS release edge.
- A '595 latched by a GPIO pin still clocks its data over SPI, so it must borrow
  a CS pad — one no other '595 latches on, or that chip would latch stray bytes.
- A '165 freezes its inputs while its load line is high and reloads when it drops.
"""

import pytest

from hardware.ch347_device import Ch347Device
from hardware.fake import FakeCh347Device
from hardware.interfaces import build_real_interfaces, gpio_transfer_cs
from hardware_config import ChipConfig


class FakeHidDev:
    """Stands in for `ch347api.CH347HIDDev`: records calls and enforces the
    library's CS requirement for SPI transfers."""

    def __init__(self, gpio_response: bytes = b"\x00" * 8, spi_in: bytes = b"") -> None:
        self.calls: list = []
        self.cs1_enabled = False
        self.cs2_enabled = False
        self.gpio_response = gpio_response
        self.spi_in = spi_in

    def init_I2C(self) -> None:
        self.calls.append(("init_i2c", None))

    def init_SPI(self, **kwargs) -> None:
        self.calls.append(("init_spi", None))

    # Enabling one CS disables the other; disabling clears both (as ch347api does).
    def set_CS1(self, enable: bool = True) -> None:
        self.cs1_enabled = bool(enable)
        self.cs2_enabled = False
        self.calls.append(("cs1", bool(enable)))

    def set_CS2(self, enable: bool = True) -> None:
        self.cs2_enabled = bool(enable)
        self.cs1_enabled = False
        self.calls.append(("cs2", bool(enable)))

    def _require_cs(self) -> None:
        if not (self.cs1_enabled or self.cs2_enabled):
            raise Exception("no CS enabled yet")

    def spi_write(self, data: bytes) -> int:
        self._require_cs()
        self.calls.append(("spi_write", bytes(data)))
        return len(data)

    def spi_read(self, length: int) -> list:
        self._require_cs()
        self.calls.append(("spi_read", length))
        return list((self.spi_in + bytes(length))[:length])

    def write(self, payload: bytes) -> None:
        self.calls.append(("hid_write", bytes(payload)))

    def read(self, size: int, timeout_ms: int = 0) -> bytes:
        return self.gpio_response

    def close(self) -> None:
        self.calls.append(("close", None))


def _device(gpio_transfer_cs: str = "cs0", **fake_kwargs):
    device = Ch347Device(gpio_transfer_cs=gpio_transfer_cs)
    hid = FakeHidDev(**fake_kwargs)
    device._dev = hid   # bypass open(); this module is the only ch347api caller
    return device, hid


def _names(hid: FakeHidDev) -> list:
    return [name for name, _ in hid.calls]


def _gpio_ops(hid: FakeHidDev) -> list:
    """(pin, level) for each 0xCC GPIO command issued, in order."""
    ops = []
    for name, payload in hid.calls:
        if name != "hid_write":
            continue
        for pin, byte in enumerate(payload[6:14]):   # 6-byte header, then 8 pin bytes
            if byte == 0xF8:
                ops.append((pin, True))
            elif byte == 0xF0:
                ops.append((pin, False))
    return ops


# --- the library rule the GPIO path has to satisfy --------------------------
def test_spi_transfer_without_a_cs_is_refused():
    # Sentinel: this is why a GPIO-latched chip must borrow a CS pad.
    with pytest.raises(Exception, match="no CS enabled"):
        FakeHidDev().spi_write(b"\x00")


# --- CS-latched path (unchanged behavior) ----------------------------------
def test_cs_latched_595_enables_then_releases_its_own_cs():
    device, hid = _device()
    device.latch_out(b"\x01\x02", "cs0")
    assert _names(hid) == ["cs1", "spi_write", "cs1"]
    assert hid.calls[0][1] is True and hid.calls[2][1] is False
    assert hid.calls[1][1] == b"\x01\x02"


def test_cs_loaded_165_asserts_its_load_line_around_the_read():
    device, hid = _device(spi_in=b"\xa5")
    assert device.load_in(1, "cs1") == b"\xa5"
    assert _names(hid) == ["cs2", "spi_read", "cs2"]


# --- GPIO-latched path ------------------------------------------------------
def test_gpio_latched_595_borrows_a_cs_and_pulses_the_latch():
    device, hid = _device(gpio_transfer_cs="cs1")
    device.latch_out(b"\x05", "gpio4")   # must not raise "no CS enabled yet"
    # idle low -> borrow CS -> clock -> release CS -> latch high -> latch low
    assert _names(hid) == ["hid_write", "cs2", "spi_write", "cs2", "hid_write", "hid_write"]
    assert hid.calls[1][1] is True and hid.calls[3][1] is False
    assert hid.calls[2][1] == b"\x05"
    assert _gpio_ops(hid) == [(4, False), (4, True), (4, False)]


def test_gpio_loaded_165_freezes_shifts_then_releases():
    device, hid = _device(gpio_transfer_cs="cs0", spi_in=b"\xa5\x5a")
    assert device.load_in(2, "gpio3") == b"\xa5\x5a"
    assert _names(hid) == ["hid_write", "cs1", "spi_read", "cs1", "hid_write"]
    assert _gpio_ops(hid) == [(3, True), (3, False)]   # high = freeze+shift, low = reload


def test_gpio_command_without_a_response_raises():
    # A bridge that ignores the 0xCC command must fail loudly, or a chip whose
    # latch never moved would still self-test as reachable.
    device, _hid = _device(gpio_response=b"")
    with pytest.raises(RuntimeError, match="no response from CH347"):
        device.latch_out(b"\x01", "gpio2")


def test_gpio_pin_out_of_range_is_rejected():
    device, _hid = _device()
    with pytest.raises(ValueError):
        device.latch_out(b"\x01", "gpio9")


def test_transfer_cs_must_be_a_real_pad():
    with pytest.raises(ValueError):
        Ch347Device(gpio_transfer_cs="gpio4")


# --- choosing the borrowed pad ---------------------------------------------
def test_gpio_transfer_cs_prefers_a_free_pad():
    specs = [ChipConfig("in1", "hc165", bus="spi", spi_select="cs1")]
    assert gpio_transfer_cs(specs) == "cs0"


def test_gpio_transfer_cs_avoids_a_595_latch_pad():
    # Borrowing cs0 would clock stray bytes into the '595 latched there.
    specs = [ChipConfig("sw1", "hc595", bus="spi", spi_select="cs0")]
    assert gpio_transfer_cs(specs) == "cs1"


def test_gpio_transfer_cs_may_borrow_a_165_load_pad():
    specs = [
        ChipConfig("sw1", "hc595", bus="spi", spi_select="cs0"),
        ChipConfig("in1", "hc165", bus="spi", spi_select="cs1"),
    ]
    assert gpio_transfer_cs(specs) == "cs1"


def test_gpio_transfer_cs_raises_when_both_pads_latch_595s():
    specs = [
        ChipConfig("a", "hc595", bus="spi", spi_select="cs0"),
        ChipConfig("b", "hc595", bus="spi", spi_select="cs1"),
    ]
    with pytest.raises(RuntimeError, match="chip-select pad"):
        gpio_transfer_cs(specs)


# --- the whole stack builds a GPIO-latched chip -----------------------------
def test_build_real_interfaces_wires_a_gpio_latched_595():
    device = FakeCh347Device()
    specs = [
        ChipConfig("sw_cs", "hc595", bus="spi", spi_select="cs0"),
        ChipConfig("sw_gpio", "hc595", bus="gpio", spi_select="gpio4"),
    ]
    build_real_interfaces(device, specs, {}, {}, {"SW_A": ("sw_gpio", 2)})
    # cs0 latches a '595, so the GPIO transfer borrows cs1 instead.
    assert device.gpio_transfer_cs == "cs1"
    # Both chips were cleared at build, each through its own latch line.
    assert "cs0" in device.latched and "gpio4" in device.latched
