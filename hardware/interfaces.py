"""Real hardware drivers: map a logical id to a chip + port.

These satisfy the abstract contracts — `TrackOutput` (block track voltage),
`SwitchOutput` (switch throw), and `OccupancyInput` (block current) — so the
runtime keeps addressing hardware by `block_id` / `switch_id` and never sees a
chip or port. The binding map (logical id -> (chip_id, port)) is passed in from
persisted config; this is the only place that consumes it.

The drivers are board-agnostic. They hold chip objects and call chip methods,
and never touch the transport, so what talks to the chips (`Ch347Device` today)
is not their concern. Swapping the controller board replaces the device and
leaves everything here unchanged.
"""

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from block_occupancy import OccupancyInput
from block_power import MAX_TRACK_VOLTAGE, TrackOutput
from switch_power import SwitchOutput

from hardware.catalog import (
    BUS_GPIO,
    CHIP_CATALOG,
    PORT_CURRENT_SENSE,
    ROLE_OCCUPANCY,
    ROLE_SWITCH,
    ROLE_TRACK,
    SPI_SELECT_LINES,
    SWITCH_THROW_PULSE_S,
    latch_lines_for,
)
from hardware.chips.hc165 import HC165Chain
from hardware.chips.hc595 import HC595Chain
from hardware.chips.ina219 import INA219
from hardware.chips.ina3221 import INA3221
from hardware.chips.pca9685 import PCA9685
from hardware.watchdog import OutputWatchdog

# A binding map: logical id -> (chip_id, port).
BindingMap = Dict[str, Tuple[str, int]]
# A track binding map: block_id -> (chip_id, forward_channel, reverse_channel) —
# the two PWM channels driving that block's two rails.
TrackBindingMap = Dict[str, Tuple[str, int, int]]
# A switch binding map: switch_id -> (chip_id, port_main, port_diverge, port_standby).
# `port_standby` is None when the motor driver needs no standby/enable bit.
SwitchBindingMap = Dict[str, Tuple[str, int, int, Optional[int]]]
# A current-sense binding map: block_id -> (chip_id, port, baseline_a, threshold_a).
# Thresholds arrive already resolved to amps by the config layer.
CurrentBindingMap = Dict[str, Tuple[str, int, float, float]]

# Direct-call fallback deadband; the config layer passes its own resolved value.
_IDLE_DEADBAND_V = 0.25


def _resolved_bus(spec: Any, ck: Any) -> str:
    """The bus a chip spec is wired on, defaulting to the chip's first allowed
    bus when the spec did not record one (e.g. a directly-constructed test spec)."""
    return getattr(spec, "bus", None) or ck.buses[0]


def _resolved_latch(spec: Any, ck: Any) -> Optional[str]:
    """The latch line for a shift-register spec, falling back to the first line
    its bus offers."""
    bus = _resolved_bus(spec, ck)
    lines = latch_lines_for(bus)
    return spec.spi_select or (lines[0] if lines else None)


def gpio_transfer_cs(chip_specs: List[Any]) -> str:
    """The chip-select pad the SPI engine may assert while a GPIO-latched chip
    is clocked.

    A GPIO-latched shift register still moves its data over the SPI peripheral,
    which `ch347api` refuses to drive with no CS enabled — and the CH347 asserts
    then releases that CS around the transfer. Borrowing a pad a '595 latches on
    would clock stray bytes into it on the release edge (it could throw a
    switch), so those pads are excluded. A '165's load pad is safe to borrow: the
    transfer only freezes and re-loads its parallel inputs.
    """
    latch_pads = {
        spec.spi_select for spec in chip_specs
        if spec.kind == "hc595" and spec.spi_select in SPI_SELECT_LINES
    }
    for pad in SPI_SELECT_LINES:
        if pad not in latch_pads:
            return pad
    raise RuntimeError(
        "a GPIO-latched chip needs a chip-select pad that no '595 latches on for "
        f"its SPI data transfer; both {SPI_SELECT_LINES} are '595 latches"
    )


def build_chip(device: Any, spec: Any) -> Any:
    """Instantiate the chip driver for a chip spec (duck-typed: chip_id, kind,
    bus, i2c_address, chain_length, spi_select)."""
    ck = CHIP_CATALOG[spec.kind]
    if spec.kind == "pca9685":
        address = spec.i2c_address if spec.i2c_address is not None else ck.default_i2c_address
        return PCA9685(device, address=address)
    if spec.kind in ("hc595", "hc165"):
        # The latch line is a chip-select pad (spi bus) or a general-purpose pin
        # (gpio bus); the device branches on the pad name. Data clocks over the
        # SPI peripheral either way.
        cls = HC595Chain if spec.kind == "hc595" else HC165Chain
        return cls(device, select=_resolved_latch(spec, ck), chain_length=spec.chain_length)
    if spec.kind in ("ina3221", "ina219"):
        address = spec.i2c_address if spec.i2c_address is not None else ck.default_i2c_address
        cls = INA3221 if spec.kind == "ina3221" else INA219
        return cls(device, address=address)
    raise ValueError(f"unknown chip kind {spec.kind!r}")


class TrackDriver(TrackOutput):
    """Block track voltage over two PCA9685 PWM channels per block — the two
    inputs of that block's H-bridge, chosen independently (not a fixed pair)."""

    def __init__(
        self,
        chips_by_id: Dict[str, PCA9685],
        bindings: "TrackBindingMap",
        watchdog: Optional[OutputWatchdog] = None,
    ) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._voltages: Dict[str, float] = {}
        self._watchdog = watchdog

    def set_voltage(self, block_id: str, voltage: float) -> bool:
        binding = self._bindings.get(block_id)
        if binding is None:
            return False
        chip_id, forward, reverse = binding
        chip = self._chips.get(chip_id)
        if chip is None:
            return False
        duty = min(abs(voltage) / MAX_TRACK_VOLTAGE, 1.0) if MAX_TRACK_VOLTAGE else 0.0
        # Drive the channel matching the sign; the other rests at zero, so the
        # two H-bridge inputs are never driven together.
        if voltage >= 0:
            chip.set_duty_cycle(forward, duty)
            chip.set_duty_cycle(reverse, 0.0)
        else:
            chip.set_duty_cycle(forward, 0.0)
            chip.set_duty_cycle(reverse, duty)
        self._voltages[block_id] = voltage
        # A landed write is proof the control loop is alive.
        if self._watchdog is not None:
            self._watchdog.pet()
        return True

    def get_actual_voltage(self, block_id: str) -> float:
        return self._voltages.get(block_id, 0.0)

    def get_all_voltages(self) -> Dict[str, float]:
        return dict(self._voltages)

    def enter_safe_state(self) -> None:
        # Disarm first: this call IS the safe state, so the watchdog must not
        # also fire against a runtime that has deliberately stopped driving.
        if self._watchdog is not None:
            self._watchdog.disarm()
        for chip in self._chips.values():
            chip.all_off()
        for block_id in self._voltages:
            self._voltages[block_id] = 0.0


class SwitchDriver(SwitchOutput):
    """Switch throw over 74HC595 output bits — two coil bits per switch, pulsed.

    A solenoid point motor is a momentary device: one coil throws toward MAIN,
    the other toward DIVERGE, and either burns out if left energized. So a throw
    drives its coil for `SWITCH_THROW_PULSE_S` and then de-energizes; both coils
    rest low between throws, and the alignment is held by the points themselves,
    not by a held output. When the binding names a standby bit (a motor driver
    such as the TB6612 ignores its inputs until that pin is high), it is raised
    for the pulse and dropped with it, so the driver idles disabled.

    The write order is the sequence proven on the bench not to burn a motor:

      1. standby high — enable the bridge
      2. target coil high
      3. **other coil low** — the two coil bits are the bridge's two inputs, and
         leaving the previous throw's input high would drive both at once. This
         is asserted every throw rather than assumed, so a throw is correct from
         any prior bit pattern, including the one a chain powers up in.
      4. wait the pulse width
      5. **standby low first** — one write puts the outputs in high impedance.
         Dropping a coil input first would leave the bridge enabled with both
         inputs low, which is its brake mode: a short across the coil.
      6. coil low — the bridge is already off, so this only settles the resting
         pattern for the next throw.

    Steps 5-6 run in a `finally`, so a failure part-way through still leaves no
    coil driven. A driver with no standby bit de-energizes at step 6 instead,
    which is why that write is unconditional.
    """

    def __init__(
        self,
        chips_by_id: Dict[str, HC595Chain],
        bindings: SwitchBindingMap,
        watchdog: Optional[OutputWatchdog] = None,
        pulse_s: float = SWITCH_THROW_PULSE_S,
    ) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._last: Dict[str, int] = {}
        self._watchdog = watchdog
        self._pulse_s = pulse_s

    def set_throw(self, switch_id: str, target_state: int) -> bool:
        binding = self._bindings.get(switch_id)
        if binding is None:
            return False
        chip_id, port_main, port_diverge, port_standby = binding
        chip = self._chips.get(chip_id)
        if chip is None:
            return False
        state = 0 if target_state == 0 else 1
        coil = port_main if state == 0 else port_diverge
        other_coil = port_diverge if state == 0 else port_main
        try:
            if port_standby is not None:
                chip.set_output(port_standby, True)
            chip.set_output(coil, True)
            chip.set_output(other_coil, False)
            time.sleep(self._pulse_s)
        finally:
            if port_standby is not None:
                chip.set_output(port_standby, False)
            chip.set_output(coil, False)
        self._last[switch_id] = state
        if self._watchdog is not None:
            self._watchdog.pet()
        return True

    def get_last_throw(self, switch_id: str) -> Optional[int]:
        return self._last.get(switch_id)

    def enter_safe_state(self) -> None:
        if self._watchdog is not None:
            self._watchdog.disarm()
        for chip in self._chips.values():
            chip.all_off()
        self._last.clear()


class OccupancyDriver(OccupancyInput):
    """Hybrid block occupancy: a digital detector ('165) plus a current-sense
    channel (INA3221 or INA219), selected per block by whether the block is being
    driven.

    While a block is powered its rail voltage is a poor occupancy signal, so the
    current channel decides (abs current above the block's threshold). When the
    block is idle the '165 detector decides. `voltage_of` reports the last
    commanded block voltage (from the real track interface); a block over the
    deadband is "driven". A block may have both sources, only one, or neither:

      - driven + has current   -> current channel
      - otherwise + has digital -> '165 bit
      - idle + only current    -> current channel (reads ~0 with no drive; best effort)
      - no binding             -> not detected (False)

    Detector polarity is resolved here too. A comparator with no inverter stage
    (an LM339 driving the '165 directly) pulls its output LOW when a train draws
    current, so its raw bit reads 0 occupied / 1 clear. `digital_active_low`
    names the chips wired that way and this class flips their bits, so the
    boolean leaving the interface always means "current detected" regardless of
    how the detector is wired.

    This all stays below `read_block_currents()`, so the detector above the
    interface still sees only a per-block boolean — it never learns which source
    answered, or what polarity the wiring uses.
    """

    def __init__(
        self,
        digital_chips: Dict[str, HC165Chain],
        digital_bindings: BindingMap,
        current_chips: Optional[Dict[str, Any]] = None,
        current_bindings: Optional[CurrentBindingMap] = None,
        voltage_of: Optional[Callable[[str], float]] = None,
        idle_deadband_v: float = _IDLE_DEADBAND_V,
        digital_active_low: Optional[Dict[str, bool]] = None,
    ) -> None:
        self._digital_chips = digital_chips
        self._digital = digital_bindings
        self._current_chips = current_chips or {}
        self._current = current_bindings or {}
        self._voltage_of = voltage_of
        self._idle_deadband_v = idle_deadband_v
        self._digital_active_low = digital_active_low or {}

    def read_block_currents(self) -> Dict[str, bool]:
        digital_cache: Dict[str, bytes] = {}
        result: Dict[str, bool] = {}
        for block_id in set(self._digital) | set(self._current):
            driven = self._is_driven(block_id)
            if driven and block_id in self._current:
                result[block_id] = self._read_current(block_id)
            elif block_id in self._digital:
                result[block_id] = self._read_digital(block_id, digital_cache)
            elif block_id in self._current:
                result[block_id] = self._read_current(block_id)
            else:
                result[block_id] = False
        return result

    def _is_driven(self, block_id: str) -> bool:
        if self._voltage_of is None:
            return False
        return abs(self._voltage_of(block_id)) > self._idle_deadband_v

    def _read_digital(self, block_id: str, cache: Dict[str, bytes]) -> bool:
        chip_id, port = self._digital[block_id]
        chip = self._digital_chips.get(chip_id)
        if chip is None:
            return False
        if chip_id not in cache:               # read each '165 chain once per call
            cache[chip_id] = chip.read_all()
        data = cache[chip_id]
        byte_idx, bit_idx = divmod(port, 8)
        if byte_idx >= len(data):
            return False
        bit = bool(data[byte_idx] & (1 << bit_idx))
        # Convert the wiring's polarity to the interface's meaning: True is
        # always "current detected", whichever way the detector drives the line.
        return not bit if self._digital_active_low.get(chip_id, False) else bit

    def _read_current(self, block_id: str) -> bool:
        chip_id, port, baseline_a, threshold_a = self._current[block_id]
        chip = self._current_chips.get(chip_id)
        if chip is None:
            return False
        try:
            current = chip.read_current(port + 1)   # port 0 -> sensor channel 1
        except Exception:
            return False
        return abs(current - baseline_a) >= threshold_a


def build_real_interfaces(
    device: Any,
    chip_specs: List[Any],
    block_track: TrackBindingMap,
    block_occupancy: BindingMap,
    switch_output: SwitchBindingMap,
    block_occupancy_current: Optional[CurrentBindingMap] = None,
    output_enable: Optional[BindingMap] = None,
    idle_deadband_v: float = _IDLE_DEADBAND_V,
    watchdog_timeout_s: Optional[float] = None,
) -> Tuple[Optional[OccupancyInput], Optional[TrackOutput], Optional[SwitchOutput]]:
    """Instantiate + initialize chips grouped by role and wire the three
    interfaces. A role with no chip returns `None` (its simulated default is
    used). Performs live init (PWM reset/freq, shift-register clear, INA3221
    channel enable) so hardware starts in a known state. Occupancy is the hybrid
    of the '165 digital detector and the INA3221 current channels; the returned
    input interface selects between them per block by drive state.

    `watchdog_timeout_s` arms the loss-of-control watchdog on both output
    interfaces (design_new.md "Hardware interface boundary"); `None` leaves it
    unarmed, which is what a test harness driving a fake device wants."""
    track_chips: Dict[str, PCA9685] = {}
    switch_chips: Dict[str, HC595Chain] = {}
    digital_chips: Dict[str, HC165Chain] = {}
    current_chips: Dict[str, Any] = {}

    # A GPIO-latched chip borrows a chip-select pad for its SPI transfer. Choose a
    # safe one before any chip is built, because the first `all_off()` latches.
    if any(getattr(spec, "bus", None) == BUS_GPIO for spec in chip_specs):
        device.gpio_transfer_cs = gpio_transfer_cs(chip_specs)

    for spec in chip_specs:
        ck = CHIP_CATALOG[spec.kind]
        chip = build_chip(device, spec)
        if ck.role == ROLE_TRACK:
            chip.configure()
            track_chips[spec.chip_id] = chip
        elif ck.role == ROLE_SWITCH:
            chip.all_off()
            switch_chips[spec.chip_id] = chip
        elif ck.role == ROLE_OCCUPANCY:
            if ck.port_kind == PORT_CURRENT_SENSE:
                chip.configure()               # reset + enable all three channels
                current_chips[spec.chip_id] = chip
            else:
                digital_chips[spec.chip_id] = chip

    # One watchdog covers both output interfaces: loss of control must drop the
    # track AND the point motors. The targets list is filled just below, before
    # anything can pet the watchdog into an armed state.
    watchdog_targets: List[Any] = []
    watchdog: Optional[OutputWatchdog] = None
    if watchdog_timeout_s is not None and (track_chips or switch_chips):
        def _drop_to_safe_state() -> None:
            for target in watchdog_targets:
                target.enter_safe_state()

        watchdog = OutputWatchdog(_drop_to_safe_state, timeout_s=watchdog_timeout_s)

    block_out = (
        TrackDriver(track_chips, block_track, watchdog=watchdog)
        if track_chips
        else None
    )
    switch_out = (
        SwitchDriver(switch_chips, switch_output, watchdog=watchdog)
        if switch_chips
        else None
    )
    watchdog_targets.extend(
        iface for iface in (block_out, switch_out) if iface is not None
    )

    # Enable-and-hold each PCA's /OE (active-low): drive its '595 bit low once.
    # There is no per-tick coast window, so nothing drives it high afterwards.
    for _track_chip_id, (drive_chip_id, port) in (output_enable or {}).items():
        drive = switch_chips.get(drive_chip_id)
        if drive is not None:
            drive.set_output(port, False)

    input_iface: Optional[OccupancyInput] = None
    if digital_chips or current_chips:
        input_iface = OccupancyDriver(
            digital_chips,
            block_occupancy,
            current_chips=current_chips,
            current_bindings=block_occupancy_current or {},
            voltage_of=(block_out.get_actual_voltage if block_out is not None else None),
            idle_deadband_v=idle_deadband_v,
            # Which shift-in chains are wired active-low, so the interface can
            # normalize their bits (design_new.md Power, Block occupancy detection).
            digital_active_low={
                spec.chip_id: bool(getattr(spec, "active_low", False))
                for spec in chip_specs
                if spec.chip_id in digital_chips
            },
        )
    return input_iface, block_out, switch_out
