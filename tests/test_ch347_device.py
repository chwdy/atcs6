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

import sys
import threading
import time
import types

import pytest

from hardware import ch347_device
from hardware.ch347_device import Ch347Device
from hardware.fake import FakeCh347Device
from hardware.interfaces import build_real_interfaces, gpio_transfer_cs
from hardware_config import ChipConfig


class FakeHidDev:
    """Stands in for `ch347api.CH347HIDDev`: records calls and enforces the
    library's CS requirement for SPI transfers."""

    def __init__(
        self,
        gpio_response: bytes = b"\x00" * 8,
        spi_in: bytes = b"",
        i2c_present: frozenset = frozenset(),
    ) -> None:
        self.calls: list = []
        self.cs1_enabled = False
        self.cs2_enabled = False
        self.gpio_response = gpio_response
        self.spi_in = spi_in
        self.i2c_present = i2c_present

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

    def i2c_write(self, addr: int, data: bytes) -> bool:
        self.calls.append(("i2c_write", (addr, bytes(data))))
        return addr in self.i2c_present

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


# --- I2C bus scan -----------------------------------------------------------
def test_i2c_scan_reports_the_addresses_that_ack():
    device, hid = _device(i2c_present=frozenset({0x41, 0x44}))
    assert device.i2c_scan() == [0x41, 0x44]


def test_i2c_scan_probes_with_a_call_the_library_actually_has():
    # The scan once called `i2c_exists`, which `ch347api.CH347HIDDev` does not
    # define; the per-address `except Exception` swallowed every AttributeError
    # and the bus always came back empty. Probe only with calls the fake — and
    # therefore the library — really exposes.
    device, hid = _device(i2c_present=frozenset({0x40}))
    device.i2c_scan()
    probes = [payload for name, payload in hid.calls if name == "i2c_write"]
    assert len(probes) == 0x78 - 0x03
    # One data byte, not an address-only frame: the library skips the address
    # ACK on a 1-byte payload, so an empty write would ACK at every address.
    assert all(data == b"\x00" for _addr, data in probes)


def test_i2c_scan_survives_an_address_that_raises():
    class Exploding(FakeHidDev):
        def i2c_write(self, addr: int, data: bytes) -> bool:
            if addr == 0x20:
                raise OSError("bus glitch")
            return super().i2c_write(addr, data)

    device = Ch347Device()
    device._dev = Exploding(i2c_present=frozenset({0x41}))
    assert device.i2c_scan() == [0x41]


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
    # (chip, main, diverge, standby) — the shape `_switch_binding_tuples` builds
    # and `SwitchDriver.set_throw` unpacks.
    build_real_interfaces(device, specs, {}, {}, {"SW_A": ("sw_gpio", 2, 3, None)})
    # cs0 latches a '595, so the GPIO transfer borrows cs1 instead.
    assert device.gpio_transfer_cs == "cs1"
    # Both chips were cleared at build, each through its own latch line.
    assert "cs0" in device.latched and "gpio4" in device.latched


# --- hidapi's run-loop trap -------------------------------------------------
# macOS hidapi pins one process-global IOHID manager to `CFRunLoopGetCurrent()`
# the first time anything calls it, and IOKit stores that run loop without
# retaining it. Pinned to a Flask request thread, the run loop is freed when the
# request ends, and the next `hid.enumerate()` schedules devices onto freed
# memory — an abort inside IOKit that no `except` can catch. Priming on the main
# thread at boot is what keeps the pinned run loop alive for the whole process.
@pytest.fixture
def unprimed(monkeypatch):
    """A darwin process that has not yet touched hidapi, with `hid` faked."""
    monkeypatch.setattr(ch347_device, "_hid_runloop_primed", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list = []
    monkeypatch.setitem(
        sys.modules, "hid",
        types.SimpleNamespace(enumerate=lambda: calls.append("enumerate") or []),
    )
    return calls


def test_prime_hid_runloop_pins_the_manager_once(unprimed):
    assert ch347_device.prime_hid_runloop() is True
    assert ch347_device.prime_hid_runloop() is True
    # Once is the whole point: hidapi creates and schedules the manager on its
    # first call, so a second enumeration pins nothing and just costs a USB scan.
    assert unprimed == ["enumerate"]


def test_prime_hid_runloop_refuses_to_run_off_the_main_thread(unprimed):
    result: list = []
    worker = threading.Thread(
        target=lambda: result.append(ch347_device.prime_hid_runloop())
    )
    worker.start()
    worker.join()
    # Priming here would pin the manager to a thread about to exit — exactly the
    # crash. Report the bad boot order instead of causing it.
    assert result == [False]
    assert unprimed == []
    assert ch347_device._hid_runloop_primed is False


def test_prime_hid_runloop_without_the_usb_driver_is_not_fatal(unprimed, monkeypatch):
    # `None` in sys.modules is how CPython spells "this import fails".
    monkeypatch.setitem(sys.modules, "hid", None)
    assert ch347_device.prime_hid_runloop() is False


def test_prime_hid_runloop_is_a_no_op_off_darwin(unprimed, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # hidraw / libusb keep no run-loop-bound global manager to pin.
    assert ch347_device.prime_hid_runloop() is True
    assert unprimed == []


def test_opens_are_serialized(monkeypatch):
    # `CH347HIDDev()` enumerates before it opens, and hidapi's global manager is
    # unlocked, so two threads must never be inside the constructor at once.
    guard = threading.Lock()
    counts = {"in_flight": 0, "peak": 0}

    class SlowHid(FakeHidDev):
        def __init__(self) -> None:
            super().__init__()
            with guard:
                counts["in_flight"] += 1
                counts["peak"] = max(counts["peak"], counts["in_flight"])
            time.sleep(0.02)
            with guard:
                counts["in_flight"] -= 1

    monkeypatch.setitem(
        sys.modules, "ch347api", types.SimpleNamespace(CH347HIDDev=SlowHid)
    )
    opened: list = []

    def opener():
        device = Ch347Device()
        device.open()
        opened.append(device._dev)

    threads = [threading.Thread(target=opener) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(opened) == 4
    assert counts["peak"] == 1


# --- dropped USB link -------------------------------------------------------
# `OSError: read error` out of hidapi is not a slow or noisy transfer: the macOS
# backend returns -1 from `hid_read_timeout` only when its per-device read thread
# has stopped, which means the bridge left the bus (a transfer that merely timed
# out returns 0 bytes). So the handle is dead, it stays dead — a re-enumerated
# bridge comes back on a different path — and the only cure is a reopen. On a
# layout that shares a supply with solenoid coils, that happens mid-session.
class FlakyHid(FakeHidDev):
    """A handle that dies after `alive_for` bus operations."""

    def __init__(self, alive_for: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.alive_for = alive_for
        self.ops = 0

    def _tick(self) -> None:
        self.ops += 1
        if self.ops > self.alive_for:
            raise OSError("read error")

    def spi_read(self, length: int) -> list:
        self._tick()
        return super().spi_read(length)

    def spi_write(self, data: bytes) -> int:
        self._tick()
        return super().spi_write(data)

    def i2c_write(self, addr: int, data: bytes) -> bool:
        self._tick()
        return super().i2c_write(addr, data)


@pytest.fixture
def instant_reconnect(monkeypatch):
    """Drop the re-enumeration settle delay so recovery runs at test speed."""
    monkeypatch.setattr(ch347_device, "LINK_SETTLE_S", 0.0)


def _reopenable(monkeypatch, *handles):
    """A `Ch347Device` whose successive `open()` calls hand back `handles`."""
    queue = list(handles)
    monkeypatch.setitem(
        sys.modules, "ch347api", types.SimpleNamespace(CH347HIDDev=lambda: queue.pop(0))
    )
    device = Ch347Device()
    device.open()
    return device


def _reopens_into(monkeypatch, factory):
    """Record every handle `open()` builds, so a test can count enumerations."""
    opened: list = []

    def build():
        handle = factory()
        opened.append(handle)
        return handle

    monkeypatch.setitem(
        sys.modules, "ch347api", types.SimpleNamespace(CH347HIDDev=build)
    )
    return opened


def _no_bridge(monkeypatch):
    """USB with nothing on it: `ch347api` hands `hid.open_path` a `None` path."""

    def missing():
        raise TypeError("expected bytes, NoneType found")

    monkeypatch.setitem(
        sys.modules, "ch347api", types.SimpleNamespace(CH347HIDDev=missing)
    )


def test_a_dropped_link_is_reopened_and_the_transaction_replayed(
    monkeypatch, instant_reconnect
):
    dead = FlakyHid(alive_for=0, spi_in=b"\xa5")
    fresh = FakeHidDev(spi_in=b"\xa5")
    device = _reopenable(monkeypatch, dead, fresh)

    assert device.load_in(1, "cs1") == b"\xa5"

    # The WHOLE transaction replayed on the new handle — load line asserted,
    # bytes clocked, load line released — not just the spi_read that failed.
    # Resuming a half-finished transfer against a fresh handle would clock the
    # '165 with its load line never asserted.
    assert _names(fresh) == ["init_i2c", "init_spi", "cs2", "spi_read", "cs2"]
    assert device._dev is fresh
    assert ("close", None) in dead.calls        # the stale handle was released
    assert device.link_status() == {
        "open": True, "link_down": False, "reconnects": 1, "last_error": "read error",
    }


def test_reconnect_hooks_run_before_the_transaction_is_replayed(
    monkeypatch, instant_reconnect
):
    device = _reopenable(monkeypatch, FlakyHid(alive_for=0), FakeHidDev(spi_in=b"\x00"))
    order: list = []
    device.add_reconnect_hook(lambda: order.append("resync"))

    device.load_in(1, "cs1")

    # Nothing may resume against a bridge whose chips have not been put back
    # into a known state — that is what the hook is for.
    assert order == ["resync"]


def test_a_hook_that_raises_fails_the_reconnect(monkeypatch, instant_reconnect):
    device = _reopenable(monkeypatch, FlakyHid(alive_for=0), FakeHidDev())

    def refuse() -> None:
        raise RuntimeError("a coil bit came back high")

    device.add_reconnect_hook(refuse)
    with pytest.raises(ch347_device.Ch347LinkLost):
        device.load_in(1, "cs1")
    # A bridge that cannot be put back into a safe state is not a usable link.
    # Leaving it closed beats letting the session drive a board in unknown state.
    assert device._dev is None
    assert device.link_status()["link_down"] is True


def test_a_hook_whose_own_write_fails_does_not_recurse(monkeypatch, instant_reconnect):
    # The hooks drive the bus — clearing the '595 chains is the whole point — so
    # a bridge that dies again mid-resync must fail this reconnect, not start
    # another one from inside it.
    opened = _reopens_into(monkeypatch, lambda: FlakyHid(alive_for=0))
    device = Ch347Device()
    device.open()
    device.add_reconnect_hook(lambda: device.latch_out(b"\x00", "cs0"))

    with pytest.raises(ch347_device.Ch347LinkLost):
        device.load_in(1, "cs1")

    assert len(opened) == 2            # the original, plus exactly one reconnect


def test_a_bridge_that_stays_gone_is_not_reopened_every_tick(
    monkeypatch, instant_reconnect
):
    opened = _reopens_into(monkeypatch, lambda: FlakyHid(alive_for=0))
    device = Ch347Device()
    device.open()

    for _ in range(20):
        with pytest.raises(ch347_device.Ch347LinkLost):
            device.load_in(1, "cs1")

    # The detector reads at 10 Hz. One USB enumeration per read is how a dropped
    # link stops being a dropped link and starts being a hung host.
    assert len(opened) == 2            # original + one attempt; cooldown holds


def test_a_protocol_error_is_not_treated_as_a_dropped_link(monkeypatch):
    class Refusing(FakeHidDev):
        def spi_read(self, length: int) -> list:
            raise ValueError("bad length")

    device = Ch347Device()
    device._dev = Refusing()
    with pytest.raises(ValueError):
        device.load_in(1, "cs1")
    # Reopening cannot fix a usage error, and retrying through one would hide it.
    assert device.link_status()["reconnects"] == 0
    assert device.link_status()["link_down"] is False


def test_operating_a_closed_device_names_the_link(monkeypatch):
    device = Ch347Device()
    with pytest.raises(ch347_device.Ch347LinkLost, match="not open"):
        device.load_in(1, "cs1")


def test_a_scan_on_a_dead_link_reports_the_link_not_an_empty_bus(
    monkeypatch, instant_reconnect
):
    _no_bridge(monkeypatch)
    device = Ch347Device()
    device._dev = FlakyHid(alive_for=0)
    # The per-address `except` exists so a NAK is not an error. Before the first
    # probe was lifted out of it, that same `except` swallowed 117 consecutive
    # OSErrors and an absent bridge scanned as a bus with nothing on it — which
    # reads, on the Hardware Setup screen, as "no chips detected".
    with pytest.raises(ch347_device.Ch347LinkLost):
        device.i2c_scan()


def test_build_registers_a_resync_that_clears_the_switch_chains(monkeypatch):
    device, hid = _device()
    specs = [ChipConfig("sw", "hc595", bus="spi", spi_select="cs0")]
    build_real_interfaces(device, specs, {}, {}, {"SW_A": ("sw", 0, 1, None)})
    assert device._on_reconnect, "build must register the post-reconnect resync"

    hid.calls.clear()
    device._on_reconnect[0]()
    # A '595 that browned out with the bridge powers up with undefined register
    # contents; on a coil bit that is a point motor cooking. Chains go low first.
    assert ("spi_write", b"\x00") in hid.calls


class DiesOnCommand(FakeHidDev):
    """A handle that stays healthy until the test pulls the plug on it."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.dead = False

    def _check(self) -> None:
        if self.dead:
            raise OSError("read error")

    def spi_read(self, length: int) -> list:
        self._check()
        return super().spi_read(length)

    def spi_write(self, data: bytes) -> int:
        self._check()
        return super().spi_write(data)


def test_occupancy_reads_survive_a_link_drop_end_to_end(monkeypatch, instant_reconnect):
    # The reported failure, whole: read_block_currents -> HC165Chain.read_all ->
    # load_in -> spi_read -> OSError. Every occupancy read of every tick took
    # this path, so a dropped bridge froze occupancy for the rest of the session.
    first = DiesOnCommand(spi_in=b"\x01")
    second = DiesOnCommand(spi_in=b"\x01")
    device = _reopenable(monkeypatch, first, second)
    specs = [
        ChipConfig("sw", "hc595", bus="spi", spi_select="cs0"),
        ChipConfig("in", "hc165", bus="spi", spi_select="cs1"),
    ]
    input_iface, _track, _switch = build_real_interfaces(
        device, specs, {}, {"BLK_A": ("in", 0)}, {"SW_A": ("sw", 0, 1, None)}
    )
    assert input_iface.read_block_currents() == {"BLK_A": True}

    first.dead = True                       # the bridge falls off the bus
    assert input_iface.read_block_currents() == {"BLK_A": True}

    # The read came back on the new handle, and the '595 chain was driven low on
    # the way through: a bridge that browned out took its chips down with it, and
    # a '595 powering back up holds undefined bits — on a coil, a burning motor.
    assert ("spi_write", b"\x00") in second.calls
    assert device.link_status()["reconnects"] == 1
