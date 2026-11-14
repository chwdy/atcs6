"""The session's one device handle and the record of who owns the outputs.

design_v2.md "Hardware interface", Who may write the outputs: at any moment at
most one part of v2 owns the outputs (every output port of every chip), and only
the owner's writes reach a chip. The owner is the runtime, one Setup tool, or
nobody. Every write to an output goes through the session's one device handle
and names its writer, and the handle refuses a write from anyone but the owner
before it reaches a chip. One check in one place covers every caller.

How a write names its writer: nothing calls the device itself. A caller asks
the handle for a `DeviceWriter` under its own writer name (`handle.writer(...)`)
and builds its chip drivers on that object, which has the same methods as the
device (`i2c_write`, `i2c_read`, `i2c_scan`, `latch_out`, `load_in`). Every
write a chip driver makes through it therefore carries that writer's name.

Which device calls count as an output write. The doc defines the outputs as
"every output port of every chip in the Hardware Setup". At the device level:

- every shift-out transaction (`latch_out`), because only a '595 chain
  clocks out bits, and each one drives every bit of its chain;
- every I2C register write of length two or more to an output chip (a PCA9685
  PWM channel write, and its MODE1 / PRESCALE setup writes). `build_chip` builds
  each chip whose catalog role is track output or switch output on the
  writer's output side (`DeviceWriter.output_side()`), and only writes made
  through that side are checked;
- a one-byte I2C write only moves a chip's register pointer before a read
  (the PCA9685 duty read-back, the INA reads), so it is a read, not an output
  write.

Not output writes, so never checked: every read (`i2c_read`, `i2c_scan`,
`load_in`), and the register writes of an input chip (an INA3221 / INA219
configuration write), because those chips have no output port.
(design_v2.md "Hardware interface", Reads have no owner.)

The safe state is accepted from anyone (design_v2.md "Hardware interface", The
safe state is accepted from anyone): `OutputSafety.enter_safe_state()` runs its
writes inside `safe_state_writes()`, which lets them through whoever owns the
outputs, on whatever thread calls it (the Real Hardware watchdog calls it from
its own thread).
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

_LOG = logging.getLogger(__name__)

# The writer names: the runtime and the Setup tools that take the outputs
# (design_v2.md "Hardware interface", Two kinds of Setup tool take the
# outputs).
RUNTIME = "runtime"
COMMISSIONING_SESSION = "commissioning_session"
OCCUPANCY_BENCH = "occupancy_bench"
PORT_TEST_PULSE = "port_test_pulse"
TRACK_DIRECTION_SWEEP = "track_direction_sweep"

# Tools that only read. They get a `DeviceWriter` like everyone else, so a
# write one of them made by mistake would be refused like any other.
SELF_TEST = "self_test"
PORT_TEST_READ = "port_test_read"
BLOCK_OCCUPANCY_TEST = "block_occupancy_test"

# The plain names the page and the event log show for each writer.
WRITER_LABELS: Dict[str, str] = {
    RUNTIME: "the runtime",
    COMMISSIONING_SESSION: "the commissioning session",
    OCCUPANCY_BENCH: "the occupancy bench",
    PORT_TEST_PULSE: "a port-test output pulse",
    TRACK_DIRECTION_SWEEP: "the track direction sweep",
    SELF_TEST: "self-test",
    PORT_TEST_READ: "a port-test input read",
    BLOCK_OCCUPANCY_TEST: "the block occupancy test",
}


def writer_label(writer: Optional[str]) -> str:
    if writer is None:
        return "nobody"
    return WRITER_LABELS.get(writer, writer)


class OutputsOwned(RuntimeError):
    """A tool asked for the outputs while another owner holds them
    (design_v2.md "Hardware interface", A tool that would take owned outputs is
    refused). The route answers it with `409` and the reason `outputs_owned`."""

    reason = "outputs_owned"

    def __init__(self, owner: str, requester: str) -> None:
        self.owner = owner
        self.requester = requester
        super().__init__(
            f"{writer_label(owner)} is using the outputs; close it first"
        )


class OutputNotOwned(RuntimeError):
    """The device handle refused a write from a writer that does not own the
    outputs. The write reached no chip. It is not a hardware I/O failure
    (design_v2.md "Hardware interface", A refused write changes nothing), so
    every caller that turns a device error into a hardware failure lets this
    one through untouched."""

    def __init__(self, writer: str, output: str, owner: Optional[str]) -> None:
        self.writer = writer
        self.output = output
        self.owner = owner
        super().__init__(
            f"{writer_label(writer)} does not own the outputs "
            f"(owner: {writer_label(owner)}); the write to {output} was refused"
        )


class SessionDeviceHandle:
    """The session's one device handle: the device, the environment it was
    opened for, and the owner of the outputs.

    The owner changes only through `take` and `give_back`, which the code
    calls at the doc's six change points (design_v2.md "Hardware interface",
    When the owner changes).
    """

    def __init__(
        self,
        device: Any,
        environment: Any,
        *,
        on_refused: Optional[Callable[[str, str, Optional[str]], None]] = None,
    ) -> None:
        self.device = device
        self.environment = environment
        # Called once per refused write with (writer, output, owner), to emit
        # the `output_not_owned` warning. Logged when nothing is wired.
        self.on_refused = on_refused
        self._owner: Optional[str] = None
        self._owner_lock = threading.Lock()
        self._safe_state = threading.local()
        self._closed = False
        # The latest reconnect hook each writer registered. A writer that
        # rebuilds its chips (a reopened bench, a new activation) replaces its
        # old hook rather than adding a second one.
        self._reconnect_hooks: Dict[str, Callable[[], None]] = {}
        register = getattr(device, "add_reconnect_hook", None)
        if callable(register):
            register(self._run_reconnect_hooks)

    # -- the owner record ------------------------------------------------
    @property
    def owner(self) -> Optional[str]:
        return self._owner

    def take(self, writer: str) -> None:
        """Make `writer` the owner. Raises `OutputsOwned` while another owner
        holds the outputs; taking them again as the same owner changes
        nothing."""
        with self._owner_lock:
            if self._owner is not None and self._owner != writer:
                raise OutputsOwned(self._owner, writer)
            self._owner = writer

    def give_back(self, writer: str) -> None:
        """Give the outputs back to nobody, when `writer` holds them."""
        with self._owner_lock:
            if self._owner == writer:
                self._owner = None

    def refusal_for(self, writer: str) -> Optional[OutputsOwned]:
        """The refusal `take(writer)` would raise, without taking anything."""
        owner = self._owner
        if owner is not None and owner != writer:
            return OutputsOwned(owner, writer)
        return None

    # -- writes ----------------------------------------------------------
    def writer(self, name: str) -> "DeviceWriter":
        """The device as `name` sees it: every write made through it names
        `name` as its writer."""
        return DeviceWriter(self, name)

    @contextmanager
    def safe_state_writes(self) -> Iterator[None]:
        """Let every write made on this thread through, whoever owns the
        outputs, for the length of the block. Only the safe-state sequence
        uses it (design_v2.md "Hardware interface", The safe state is accepted
        from anyone)."""
        depth = getattr(self._safe_state, "depth", 0)
        self._safe_state.depth = depth + 1
        try:
            yield
        finally:
            self._safe_state.depth = depth

    def check_write(self, writer: str, output: str) -> None:
        """Refuse an output write from anyone but the owner, before it reaches
        a chip: emit one `output_not_owned` warning and raise
        `OutputNotOwned`."""
        if getattr(self._safe_state, "depth", 0) > 0:
            return
        owner = self._owner
        if owner == writer:
            return
        report = self.on_refused
        if report is not None:
            try:
                report(writer, output, owner)
            except Exception:  # noqa: BLE001 - the refusal must still happen
                _LOG.exception("could not report a refused output write")
        else:
            _LOG.warning(
                "output write refused: %s does not own the outputs (owner: %s); "
                "output %s", writer_label(writer), writer_label(owner), output,
            )
        raise OutputNotOwned(writer, output, owner)

    # -- the device's own state ------------------------------------------
    def is_usable(self) -> bool:
        """Whether the device is open and its controller is not lost
        (design_v2.md "Setup tools", A device that does not open)."""
        if self._closed:
            return False
        device = self.device
        connected = getattr(device, "connected", None)
        if callable(connected):
            return bool(connected())
        link = getattr(device, "link_status", None)
        if callable(link):
            try:
                return bool(link().get("open"))
            except Exception:  # noqa: BLE001 - a status that cannot answer is lost
                return False
        return True

    def reopen(self) -> bool:
        """Try to open the same device again after its controller was lost.
        True when it is usable afterwards. Every request from a tool that
        needs the device calls it first (design_v2.md "Setup tools", A device
        that does not open); it does nothing while the device is usable."""
        if self._closed:
            return False
        if self.is_usable():
            return True
        try:
            try:
                self.device.close()
            except Exception:  # noqa: BLE001 - a dead handle may not close
                pass
            self.device.open()
            # A reopened bridge is a blank bridge, so the chips get the same
            # resync a reconnect gives them.
            self._run_reconnect_hooks()
        except Exception as exc:  # noqa: BLE001 - reported as no_device
            _LOG.warning("could not reopen the device: %s", exc)
            return False
        return self.is_usable()

    def close(self) -> None:
        """Close the device. Nobody owns the outputs afterwards, and every
        `DeviceWriter` on it reaches a closed device."""
        self._closed = True
        with self._owner_lock:
            self._owner = None
        try:
            self.device.close()
        except Exception:  # noqa: BLE001 - letting go is the point
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    # -- reconnect -------------------------------------------------------
    def set_reconnect_hook(self, writer: str, hook: Callable[[], None]) -> None:
        self._reconnect_hooks[writer] = hook

    def _run_reconnect_hooks(self) -> None:
        """Run every writer's latest reconnect hook. Each hook drives its own
        chips to the safe state (accepted from anyone) and redoes its chip
        setup only while its writer owns the outputs (see
        `build_real_interfaces`). A hook that raises fails the reconnect, as
        `Ch347Device._recover` expects."""
        for hook in list(self._reconnect_hooks.values()):
            hook()


class DeviceWriter:
    """The device as one writer sees it.

    It has the device's own methods, so chip drivers are built on it unchanged.
    Reads pass straight through. A write made through the output side
    (`output_side()`) is checked against the owner first; see the module
    docstring for which writes those are.
    """

    def __init__(self, handle: SessionDeviceHandle, writer: str, output: bool = False) -> None:
        object.__setattr__(self, "_handle", handle)
        object.__setattr__(self, "_writer", writer)
        object.__setattr__(self, "_output", output)

    @property
    def handle(self) -> SessionDeviceHandle:
        return self._handle

    @property
    def writer_name(self) -> str:
        return self._writer

    def output_side(self) -> "DeviceWriter":
        """The same writer, for an output chip: its register writes are output
        writes and are checked."""
        return DeviceWriter(self._handle, self._writer, output=True)

    def owns_outputs(self) -> bool:
        return self._handle.owner == self._writer

    def safe_state_writes(self):
        return self._handle.safe_state_writes()

    # -- the device surface ----------------------------------------------
    def i2c_write(self, addr: int, data: bytes) -> bool:
        data = bytes(data)
        if self._output and len(data) >= 2:
            self._handle.check_write(
                self._writer, f"I2C chip 0x{int(addr):02x} register 0x{data[0]:02x}"
            )
        return self._handle.device.i2c_write(addr, data)

    def latch_out(self, data: bytes, select: str) -> None:
        self._handle.check_write(self._writer, f"shift-out chain on {select}")
        self._handle.device.latch_out(data, select)

    def i2c_read(self, addr: int, length: int):
        return self._handle.device.i2c_read(addr, length)

    def i2c_scan(self) -> List[int]:
        return self._handle.device.i2c_scan()

    def load_in(self, count: int, select: str) -> bytes:
        return self._handle.device.load_in(count, select)

    def add_reconnect_hook(self, hook: Callable[[], None]) -> None:
        self._handle.set_reconnect_hook(self._writer, hook)

    def open(self) -> None:
        """A writer never opens the device: the session's handle is opened
        and closed only by `hardware_config`."""

    def close(self) -> None:
        """A writer never closes the device (see `open`)."""

    def __getattr__(self, name: str) -> Any:
        # Everything else (`link_status`, `gpio_transfer_cs`, a fake device's
        # helpers) is the device's own.
        return getattr(self._handle.device, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # `build_real_interfaces` sets `gpio_transfer_cs` on the device.
        setattr(self._handle.device, name, value)
