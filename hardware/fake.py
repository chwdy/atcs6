"""In-memory CH347 stand-in for tests and hardware-free UI dev.

Implements the same duck-typed device surface as `Ch347Device`
(`i2c_write` / `i2c_read` / `i2c_scan` / `latch_out` / `load_in`) over plain
dicts, plus test helpers to declare which I2C addresses are present and to set
'165 input bits. The chip classes, interface adapters, and self-test run against
this unchanged, so the whole real-mode stack is exercised without USB hardware.
"""

from typing import Callable, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Operator-facing virtual-hardware fault taxonomy
# (design_new.md "Fault injection belongs to Virtual Hardware" and
# "Hardware REST payload schemas").
#
# These are the injectable fault KINDS an operator names through
# `PUT /api/virtual-hardware/fault`. They are a behavioral layer ABOVE this
# fake device's own chip-transport fault vocabulary
# (`FakeCh347Device.ALLOWED_FAULTS` — the i2c/shift/stuck names that device- and
# driver-level tests inject directly). Injecting one operator kind reproduces it
# on the transport, so the same hardware-failure lifecycle Real Hardware uses is
# proven without physical equipment, while the low-level transport names stay
# available for the driver tests that need them.
# ---------------------------------------------------------------------------

# The 6 canonical VirtualHardwareFaultKind values (design_new.md 3222 / 3353).
VIRTUAL_HARDWARE_FAULT_KINDS = frozenset({
    "transaction_refusal",
    "timeout",
    "device_disconnect",
    "invalid_reading",
    "input_hold_or_flicker",
    "output_disagrees_with_command",
})

# The 5 VirtualHardwareFaultRequest.target_kind values (design_new.md 3353).
VIRTUAL_HARDWARE_FAULT_TARGETS = frozenset({
    "controller",
    "chip",
    "port",
    "transaction",
    "readback",
})

# Which fault kinds each target_kind may carry. An incompatible pair is rejected
# rather than translated to another name (design_new.md 3353). Derived from the
# canonical kinds' own parentheticals: invalid_reading is a readback fault,
# input_hold_or_flicker an OccupancyInput port fault, output_disagrees_with_
# command a TrackOutput readback/port fault, transaction_refusal / timeout are
# transaction faults, and device_disconnect takes down a chip or the controller.
FAULT_TARGET_COMPATIBILITY: Dict[str, frozenset] = {
    "controller": frozenset({"device_disconnect"}),
    "chip": frozenset({"device_disconnect"}),
    "transaction": frozenset({"transaction_refusal", "timeout"}),
    "port": frozenset({"input_hold_or_flicker", "output_disagrees_with_command"}),
    "readback": frozenset({"invalid_reading", "output_disagrees_with_command"}),
}

# How each operator fault kind reproduces on this fake's chip transport. The
# device raises / drops on these transport names, driving the same OSError /
# ConnectionError the real occupancy and output pipelines already handle. The
# two read-value faults (invalid_reading, input_hold_or_flicker) are modelled by
# failing the read path, the observable outcome the failure lifecycle catches.
_FAULT_TO_TRANSPORT_FAULTS: Dict[str, frozenset] = {
    "device_disconnect": frozenset({"disconnect"}),
    "transaction_refusal": frozenset({
        "i2c_write_error",
        "i2c_read_error",
        "shift_write_error",
        "shift_read_error",
    }),
    "timeout": frozenset({
        "i2c_write_error",
        "i2c_read_error",
        "shift_write_error",
        "shift_read_error",
    }),
    "invalid_reading": frozenset({"i2c_read_error", "shift_read_error"}),
    "input_hold_or_flicker": frozenset({"shift_read_error"}),
    "output_disagrees_with_command": frozenset({
        "stuck_i2c_output",
        "stuck_shift_output",
    }),
}


def validate_virtual_fault_request(
    action: Optional[str],
    target_kind: Optional[str],
    fault: Optional[str],
) -> Optional[str]:
    """Return an error string if a VirtualHardwareFaultRequest is malformed,
    else None (design_new.md "Hardware REST payload schemas").

    `fault` is required for `inject` and must be absent for `clear`; an
    incompatible (target_kind, fault) pair is rejected, not translated.
    """
    if action not in ("inject", "clear"):
        return f"action must be 'inject' or 'clear', got {action!r}"
    if target_kind not in VIRTUAL_HARDWARE_FAULT_TARGETS:
        return f"unknown target_kind {target_kind!r}"
    if action == "clear":
        # A per-record clear may carry the record's `fault` kind so only that
        # fault is removed; a broad clear omits it. Validate a present fault is a
        # known kind (target compatibility is not required to REMOVE a record).
        if fault is not None and fault not in VIRTUAL_HARDWARE_FAULT_KINDS:
            return f"unknown fault {fault!r}"
        return None
    if fault is None:
        return "fault is required for an inject request"
    if fault not in VIRTUAL_HARDWARE_FAULT_KINDS:
        return f"unknown fault {fault!r}"
    if fault not in FAULT_TARGET_COMPATIBILITY.get(target_kind, frozenset()):
        return (
            f"fault {fault!r} is not compatible with target_kind {target_kind!r}"
        )
    return None


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
        self.faults: set[str] = set()
        # Operator-facing injected faults (VirtualHardwareFaultRequest records),
        # distinct from the low-level transport `faults` above. Each entry is
        # {target_kind, target_id, operation, fault}.
        self.virtual_faults: List[Dict[str, Optional[str]]] = []
        self._on_reconnect: List[Callable[[], None]] = []

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._raise_if_fault("open_error")
        self.opened = True
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def add_reconnect_hook(self, hook: Callable[[], None]) -> None:
        """Register a post-reconnect resync, same as `Ch347Device`.

        This fake never drops a link, so nothing here ever calls the hook on its
        own. It is part of the device surface anyway because `build_real_
        interfaces` registers against it, and what that hook does to the outputs
        is worth testing without a USB bridge — `simulate_reconnect()` is how a
        test fires it.
        """
        self._on_reconnect.append(hook)

    def simulate_reconnect(self) -> None:
        """Run the registered resync hooks, as `Ch347Device._recover()` does
        once it has a fresh handle. Test helper; no link state changes."""
        for hook in list(self._on_reconnect):
            hook()

    # -- I2C -------------------------------------------------------------
    def i2c_write(self, addr: int, data: bytes) -> bool:
        self._raise_if_closed("i2c_write")
        self._raise_if_fault("i2c_write_error")
        if "stuck_i2c_output" in self._active_transport_faults():
            return True
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
        self._raise_if_closed("i2c_read")
        self._raise_if_fault("i2c_read_error")
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
        self._raise_if_closed("i2c_scan")
        self._raise_if_fault("i2c_read_error")
        return sorted(self.present)

    # -- SPI shift registers --------------------------------------------
    def latch_out(self, data: bytes, select: str) -> None:
        self._raise_if_closed("latch_out")
        self._raise_if_fault("shift_write_error")
        if "stuck_shift_output" in self._active_transport_faults():
            return
        self.latched[select] = bytes(data)

    def load_in(self, count: int, select: str) -> bytes:
        self._raise_if_closed("load_in")
        self._raise_if_fault("shift_read_error")
        raw = bytes(self.inputs.get(select, bytearray(count)))
        return (raw + bytes(count))[:count]

    # -- deterministic fault injection ---------------------------------
    # Low-level CHIP-TRANSPORT fault vocabulary. These are an implementation
    # layer below the operator-facing VIRTUAL_HARDWARE_FAULT_KINDS (module top):
    # device- and driver-level tests inject these names directly via
    # `set_fault(...)`, and each operator fault reproduces on one or more of them.
    ALLOWED_FAULTS = frozenset({
        "disconnect",
        "open_error",
        "i2c_write_error",
        "i2c_read_error",
        "shift_write_error",
        "shift_read_error",
        "stuck_i2c_output",
        "stuck_shift_output",
    })

    def set_fault(self, kind: str, enabled: bool = True) -> None:
        kind = str(kind)
        if kind not in self.ALLOWED_FAULTS:
            raise ValueError(f"unknown virtual-hardware fault {kind!r}")
        if enabled:
            self.faults.add(kind)
        else:
            self.faults.discard(kind)

    def clear_faults(self) -> None:
        self.faults.clear()

    def fault_status(self) -> Dict[str, bool]:
        return {kind: kind in self.faults for kind in sorted(self.ALLOWED_FAULTS)}

    def _raise_if_closed(self, what: str) -> None:
        """Refuse a bus operation after `close()`, with `Ch347Device`'s message.

        A device that kept answering after close would hide, in Virtual
        Hardware, a write sent through a handle Real Hardware has already let
        go of -- which is how a shutdown once reported the outputs safed in
        Virtual Hardware and failed to safe them on the rig.
        """
        if self.closed:
            raise RuntimeError(f"{what}: the CH347 is not open")

    def _raise_if_fault(self, operation_fault: str) -> None:
        active = self._active_transport_faults()
        if "disconnect" in active:
            raise ConnectionError("virtual CH347 disconnected")
        if operation_fault in active:
            raise OSError(f"injected virtual-hardware fault: {operation_fault}")

    # -- operator-facing virtual-hardware fault injection ---------------
    # `PUT /api/virtual-hardware/fault` drives these (design_new.md
    # "Hardware REST payload schemas"). They name one of the 6
    # VIRTUAL_HARDWARE_FAULT_KINDS against a target and reproduce on the
    # transport faults above.
    def inject_virtual_fault(
        self,
        target_kind: str,
        target_id: Optional[str],
        operation: Optional[str],
        fault: str,
    ) -> None:
        err = validate_virtual_fault_request("inject", target_kind, fault)
        if err is not None:
            raise ValueError(err)
        record: Dict[str, Optional[str]] = {
            "target_kind": target_kind,
            "target_id": target_id,
            "operation": operation,
            "fault": fault,
        }
        if record not in self.virtual_faults:
            self.virtual_faults.append(record)

    def clear_virtual_fault(
        self,
        target_kind: str,
        target_id: Optional[str],
        operation: Optional[str] = None,
        fault: Optional[str] = None,
    ) -> None:
        err = validate_virtual_fault_request("clear", target_kind, fault)
        if err is not None:
            raise ValueError(err)
        self.virtual_faults = [
            r
            for r in self.virtual_faults
            if not (
                r["target_kind"] == target_kind
                and r["target_id"] == target_id
                and (operation is None or r["operation"] == operation)
                and (fault is None or r["fault"] == fault)
            )
        ]

    def clear_all_virtual_faults(self) -> None:
        self.virtual_faults = []

    def virtual_fault_status(self) -> List[Dict[str, Optional[str]]]:
        return [dict(r) for r in self.virtual_faults]

    @staticmethod
    def _transport_for_record(record: Dict[str, Optional[str]]) -> set:
        """The transport faults one injected operator fault implies. For a
        transaction fault the request's `operation` narrows it to just the
        read or the write side (design_new.md types `operation` on the
        request); an absent operation means the whole transaction."""
        fault = record["fault"]
        transport = set(_FAULT_TO_TRANSPORT_FAULTS.get(fault or "", frozenset()))
        if fault in ("transaction_refusal", "timeout"):
            operation = record.get("operation")
            if operation == "write":
                return {t for t in transport if "write" in t}
            if operation == "read":
                return {t for t in transport if "read" in t}
        return transport

    def _operator_transport_faults(self) -> set:
        """The transport faults the currently-injected operator faults imply."""
        active: set = set()
        for r in self.virtual_faults:
            active |= self._transport_for_record(r)
        return active

    def _active_transport_faults(self) -> set:
        """Transport faults in force now: directly-set ones plus those implied
        by injected operator faults."""
        return self.faults | self._operator_transport_faults()

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
