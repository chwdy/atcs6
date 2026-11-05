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

import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from block_occupancy import OccupancyInput
from block_power import MAX_TRACK_VOLTAGE, TrackOutput
from constants import LIGHTING_CLEAR_HOLD_READS, OCCUPANCY_ON_THRESHOLD
from switch_power import SwitchOutput

from hardware.catalog import (
    BUS_GPIO,
    CHIP_CATALOG,
    MAX_OUTPUT_HIGH_S,
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
from hardware.safety import OutputSafety, SafeStateError
from hardware.watchdog import OutputWatchdog, WatchdogTripRecord

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
# Direct-call fallback for the drive level at or above which the current-sense
# channel is trusted to decide occupancy; below it a drive is too weak to put a
# readable load on the sensor (see `OccupancyDriver`). It sits above any set
# `SpeedProfile.lighting_voltage`, because a train standing at its lighting
# voltage must land in the lighting zone rather than be judged as driven.
_CURRENT_SENSE_MIN_DRIVE_V = 4.0


class DriveZone(Enum):
    """How hard a block's rails are being driven, which decides which of the
    block's two detectors can be believed (design_new.md "Hardware setup",
    Occupancy source follows drive state).

    `TrackDriver` owns this. It classifies each block's last commanded voltage
    into a zone and reports it through `drive_zone(block_id)`; `OccupancyDriver`
    asks for the zone rather than comparing a voltage against the levels itself,
    so what a commanded voltage means for the rails and what it means for
    detection are decided in one place.

      - IDLE (|V| <= `idle_deadband_v`): nothing drives the rails. `TrackDriver`
        holds both H-bridge inputs low, which a DRV8871 answers by letting both
        outputs float, so the rails are undriven and the '165 detector decides.
      - LIGHTING (between the two levels): a locomotive standing at its
        `lighting_voltage`, or one driven slowly. The current channel decides,
        and a clear is held for a run of reads.
      - DRIVEN (|V| >= `current_sense_min_drive_v`): the current channel decides.
    """

    IDLE = "idle"
    LIGHTING = "lighting"
    DRIVEN = "driven"


def classify_drive_zone(
    volts: float, idle_deadband_v: float, current_sense_min_drive_v: float
) -> DriveZone:
    """The zone a commanded voltage puts a block in. The sign is direction and has
    no bearing on it, so only the magnitude is compared.

    The one definition of the rule: `TrackDriver` applies it to the voltage it
    last commanded, and the block occupancy test applies it to the voltage it
    reads back off the PWM chip, so the bench and the runtime cannot disagree
    about a block."""
    magnitude = abs(volts)
    if magnitude <= idle_deadband_v:
        return DriveZone.IDLE
    if magnitude >= current_sense_min_drive_v:
        return DriveZone.DRIVEN
    return DriveZone.LIGHTING


def deciding_source(zone: DriveZone, has_digital: bool, has_current: bool) -> str:
    """Which detector answers for a block in `zone`: "current", "digital", or
    "none" when the block has neither binding.

    Whenever the rails are driven at all (lighting or driven) the current channel
    decides if the block has one, because the '165 reads the drive rather than the
    train there. Otherwise the '165 decides, and a block with only a current
    channel falls back to it (it reads close to zero with no drive; best effort).
    The one definition of the precedence, shared by the runtime read, the
    both-sources diagnostic read, and the block occupancy test."""
    if zone is not DriveZone.IDLE and has_current:
        return "current"
    if has_digital:
        return "digital"
    if has_current:
        return "current"
    return "none"


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


def _auto_release_reporter(chip_id: str) -> Callable[[int], None]:
    """Report a '595 hold-guard release. Reaching the guard means some path
    raised an output bit and never lowered it — on a coil bit that is seconds
    away from a burnt point motor — so it logs at error level even though the
    guard itself already made the board safe."""

    def report(bits: int) -> None:
        logging.getLogger(__name__).error(
            "hc595 %s: hold guard forced outputs 0x%02x low after %.1fs still "
            "high — some caller raised a bit and never dropped it",
            chip_id, bits, MAX_OUTPUT_HIGH_S,
        )

    return report


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
        if spec.kind == "hc595":
            return HC595Chain(
                device,
                select=_resolved_latch(spec, ck),
                chain_length=spec.chain_length,
                on_auto_release=_auto_release_reporter(spec.chip_id),
            )
        return HC165Chain(
            device, select=_resolved_latch(spec, ck), chain_length=spec.chain_length
        )
    if spec.kind in ("ina3221", "ina219"):
        address = spec.i2c_address if spec.i2c_address is not None else ck.default_i2c_address
        cls = INA3221 if spec.kind == "ina3221" else INA219
        return cls(device, address=address)
    raise ValueError(f"unknown chip kind {spec.kind!r}")


class TrackDriver(TrackOutput):
    """Block track voltage over two PCA9685 PWM channels per block — the two
    inputs of that block's H-bridge, chosen independently (not a fixed pair).

    `polarity_of` reports whether a block's rails are wired the opposite way
    round from the nominal-positive sense its geometry implies (the rail
    binding's `reversed` flag); when it answers True the two channels trade
    places, so a positive commanded voltage drives what the binding calls
    `reverse`. It is a callable rather than a snapshot so an operator flipping a
    block in the editor takes effect on the next write, without rebuilding the
    session — the binding names two physical rails and stays put, which matters
    when the two are not interchangeable (one carries the idle detection rail).

    It also owns each block's drive zone (see `DriveZone`). It is the one object
    that knows what it last did to a block's rails, so it is the one that says
    which zone the block is in; `OccupancyDriver` asks it through `drive_zone`.
    The two zone levels are therefore handed to this driver, not to the detector.
    """

    def __init__(
        self,
        chips_by_id: Dict[str, PCA9685],
        bindings: "TrackBindingMap",
        watchdog: Optional[OutputWatchdog] = None,
        polarity_of: Optional[Callable[[str], bool]] = None,
        safety: Optional[OutputSafety] = None,
        readback_of: Optional[Callable[[str], Optional[float]]] = None,
        idle_deadband_v: float = _IDLE_DEADBAND_V,
        current_sense_min_drive_v: float = _CURRENT_SENSE_MIN_DRIVE_V,
    ) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._voltages: Dict[str, float] = {}
        self._watchdog = watchdog
        self._polarity_of = polarity_of
        self._safety = safety
        self._idle_deadband_v = idle_deadband_v
        self._current_sense_min_drive_v = current_sense_min_drive_v
        # Reads the block's bound current-sense channel bus voltage (the actual
        # rail voltage), or None where the block has no such facility; raising on
        # a failed transaction. `None` when the rig has no current-sense chips at
        # all, so the readback simply reports no measurement.
        self._readback_of = readback_of

    def set_voltage(self, block_id: str, voltage: float) -> bool:
        binding = self._bindings.get(block_id)
        if binding is None:
            return False
        chip_id, forward, reverse = binding
        if self._polarity_of is not None and self._polarity_of(block_id):
            forward, reverse = reverse, forward
        chip = self._chips.get(chip_id)
        if chip is None:
            return False
        duty = min(abs(voltage) / MAX_TRACK_VOLTAGE, 1.0) if MAX_TRACK_VOLTAGE else 0.0
        # Drive the channel matching the sign; the other rests at zero, so the
        # two H-bridge inputs are never driven together. At zero both inputs rest
        # low, so the bridge drives neither rail — that is how the idle zone is
        # realized, and it is what lets the '165 read the block (see `DriveZone`).
        if voltage >= 0:
            chip.set_duty_cycle(reverse, 0.0)
            chip.set_duty_cycle(forward, duty)
        else:
            chip.set_duty_cycle(forward, 0.0)
            chip.set_duty_cycle(reverse, duty)
        self._voltages[block_id] = voltage
        # A landed write is proof the control loop is alive.
        if self._watchdog is not None:
            self._watchdog.pet()
        return True

    def get_actual_voltage(self, block_id: str) -> float:
        """The last voltage commanded for this block (its own output, read
        back). `drive_zone` classifies this value, so it must be the app's
        commanded value, not the measured rail voltage — that is
        `get_readback_voltage(...)`."""
        return self._voltages.get(block_id, 0.0)

    def drive_zone(self, block_id: str) -> DriveZone:
        """The zone this driver has put the block's rails in, from the voltage it
        last commanded there. A block never written, and every block after a safe
        state or a reconnect re-initialization, reads `DriveZone.IDLE`, because
        its rails are then undriven."""
        return classify_drive_zone(
            self._voltages.get(block_id, 0.0),
            self._idle_deadband_v,
            self._current_sense_min_drive_v,
        )

    def forget_commanded_voltages(self) -> None:
        """Record that every channel was driven to zero underneath this driver.

        Called by the safe state and by the reconnect re-initialization, both of
        which zero the PWM chips directly. Without it this driver would keep
        reporting the old voltage and the old drive zone for rails that are now
        undriven, and `OccupancyDriver` would read a dead block through the
        current channel instead of the '165."""
        for block_id in self._voltages:
            self._voltages[block_id] = 0.0

    def get_readback_voltage(self, block_id: str) -> Optional[float]:
        """Read the block's bound current-sense channel bus voltage — the actual
        rail voltage — never the commanded value (design_new.md "Power", the
        block-voltage read surface). Returns None where the block has no such
        facility; a failed readback transaction raises so the diagnostic
        consumer reports it."""
        if self._readback_of is None:
            return None
        return self._readback_of(block_id)

    def get_all_voltages(self) -> Dict[str, float]:
        return dict(self._voltages)

    def enter_safe_state(self) -> None:
        # Disarm first: this call IS the safe state, so the watchdog must not
        # also fire against a runtime that has deliberately stopped driving.
        if self._watchdog is not None:
            self._watchdog.disarm()
        if self._safety is not None:
            self._safety.enter_safe_state()
        else:
            for chip in self._chips.values():
                chip.all_off()
        self.forget_commanded_voltages()

    def zero_every_channel(self) -> List[str]:
        """Write `0` to every channel of every chip this driver drives, bound to
        a block or not, without `OutputSafety` (design_new.md "Power", Hardware
        setup, Closing a Setup tool restores the safe state). A Setup tool's
        close uses it: the runtime's power controllers do not own the outputs
        while a tool holds them.

        Every chip is attempted even when one fails; returns one message per
        chip that failed, empty when all landed.
        """
        errors: List[str] = []
        for chip_id, chip in self._chips.items():
            try:
                chip.all_off()
            except Exception as exc:  # noqa: BLE001 - zero the remaining chips
                errors.append(f"{chip_id}: {exc}")
        self.forget_commanded_voltages()
        return errors


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

    Which bits those are is read per throw through `binding_of`, not captured
    when the session was built (design_new.md "Hardware setup", A binding is
    read per write): re-picking a coil port aims the next throw at the new bit.
    The chips are the session's; the port numbers are the setup's.
    """

    def __init__(
        self,
        chips_by_id: Dict[str, HC595Chain],
        bindings: SwitchBindingMap,
        watchdog: Optional[OutputWatchdog] = None,
        pulse_s: float = SWITCH_THROW_PULSE_S,
        safety: Optional[OutputSafety] = None,
        binding_of: Optional[
            Callable[[str], Optional[Tuple[str, int, int, Optional[int]]]]
        ] = None,
    ) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._binding_of = binding_of
        self._last: Dict[str, int] = {}
        self._watchdog = watchdog
        self._pulse_s = pulse_s
        self._safety = safety

    def _binding_for(
        self, switch_id: str
    ) -> Optional[Tuple[str, int, int, Optional[int]]]:
        """This switch's `(chip_id, port_main, port_diverge, port_standby)`.

        `binding_of` reads the live setup — the coil-side counterpart of the
        track driver's `polarity_of` — so an operator re-picking a coil port
        sees the next throw land on the new bit. It answers for every switch,
        including one whose binding has been cleared: a switch the setup no
        longer wires must stop driving, not fall back to the ports this session
        happened to start with. The construction-time map answers only when no
        resolver was passed, which is how a test builds a driver from a literal.
        """
        if self._binding_of is not None:
            return self._binding_of(switch_id)
        return self._bindings.get(switch_id)

    def set_throw(self, switch_id: str, target_state: int) -> bool:
        binding = self._binding_for(switch_id)
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
        if self._safety is not None:
            self._safety.enter_safe_state()
        else:
            for chip in self._chips.values():
                chip.all_off()
        self._last.clear()


def resolve_detector_bit(bit: bool, *, active_low: bool) -> bool:
    """A raw '165 bit as the meaning the rest of the system uses: True is
    "current detected".

    Polarity is a property of the chain: the chip's `active_low` says which way
    its comparators drive the line, and every block on that chain reads the same
    way. The single place this is decided, so the runtime read and the bench test
    cannot answer a block differently."""
    return bit != active_low


class OccupancyDriver(OccupancyInput):
    """Hybrid block occupancy: a digital detector ('165) plus a current-sense
    channel (INA3221 or INA219), selected per block by how hard the block is
    being driven.

    `track` is the real `TrackDriver`, and this driver asks it which drive zone
    each block is in (`TrackDriver.drive_zone`) rather than comparing a voltage
    against the zone levels itself — the track driver is what put the rails in
    that state, so it is the one that says so. The two detectors are trustworthy
    in different zones:

      - **idle** (|V| <= `idle_deadband_v`): nothing is driving the rails, so
        there is no current to sense and the '165 detector decides.
      - **driven** (|V| >= `current_sense_min_drive_v`): the rail voltage swamps
        the '165's comparator, but a motor under real drive draws a current the
        sense channel reads cleanly, so the current channel decides.
      - **lighting** (between the two): a locomotive standing at its
        `lighting_voltage`, or one driven slowly. The current channel decides, exactly as in the driven zone, but **no single
        read may clear a block here**: a clear is reported only after the
        current channel has read clear for `LIGHTING_CLEAR_HOLD_READS`
        consecutive reads, and until then the last reported value holds.

    The '165 is not read at all in the lighting zone when the block has a
    current channel, because the rail its comparator watches is the rail being
    pulsed. `TrackDriver` drives lighting voltage as duty = |V| / 12 V, so a
    1.5 V standby is 12.5% duty of full 12 V at 1 kHz, and the '165 samples that
    at an unrelated phase. Measured on the rig, an EMPTY block commanded to
    1.5 V had its '165 assert "occupied" on 21% of reads (11 of 53) — the duty
    cycle, read back as a train. Its current channel over the same 53 reads
    averaged 4.3 mA against a 10 mA trip, while a block with a locomotive
    actually parked on it averaged 34.5 mA (22.8 - 52.0). The current channel
    separates occupied from empty at lighting voltage by roughly eight to one;
    the '165 does not separate them at all. So it gets no vote here — neither to
    confirm nor to veto a release.

    Letting it vote is what made this a latch. `OCCUPANCY_ON_THRESHOLD` is 1, so
    the first flicker confirmed the block; being confirmed kept it energized,
    which kept it in this zone; and every later flicker reset the release
    window. At a 21% assert rate a run of 30 clean reads has a ~0.1% chance of
    ever completing (the longest observed was 12), so an empty block a train was
    merely approaching stayed OCCUPIED for as long as the train stayed powered.
    Occupancy and drive are a closed loop — a block that reads clear is
    released, which cuts its voltage, which changes which detector answers — and
    a source that reads the drive itself closes that loop instead of breaking
    it.

    The window survives because the current channel can still go quiet on a real
    locomotive: one drawing under `threshold_ma` at standby reads the same as an
    empty block. Holding the last value for a run of reads keeps the release
    slow and fail-safe, and keeps a marginal loco from cycling the block at a
    few hertz. Raising the profile's `lighting_voltage` (below its starting
    voltage), or lowering `threshold_ma`, is what keeps a real lit loco above
    the floor; the window
    only decides how long a genuinely silent block waits.

    The window holds only an ESTABLISHED presence: one the deciding source has
    read on `confirm_reads` consecutive reads (`OCCUPANCY_ON_THRESHOLD`, the same
    run `BlockOccupancyDetector` needs to confirm a block) since this driver last
    reported the block clear. The run counts reads in any zone, so a train that
    was confirmed while driven or idle is held when it stops in this zone. A
    lone current-present read is reported as read, and the clear read after it
    is reported clear. Holding it instead would hand the detector the second
    consecutive "present" it asks for, so one noisy sample would confirm an
    empty block. Measured on the rig: a locomotive starting on the next block
    put a single 12.8 mA reading on an empty block's channel (10 mA trip, the
    reads either side under 7.2 mA), and the hold turned it into a confirmed
    entry.

    A block may have both sources, only one, or neither (`deciding_source` is the
    one definition of this precedence):

      - driven + has current    -> current channel
      - lighting + has current  -> current channel; an established presence
                                   clears only after sustained agreement from
                                   it ('165 not read)
      - lighting, digital only  -> '165 bit, under that same clear window
      - otherwise + has digital -> '165 bit
      - idle + only current     -> current channel (reads ~0 with no drive; best effort)
      - no binding              -> not detected (False)

    Detector polarity is resolved here too. A comparator with no inverter stage
    (an LM339 driving the '165 directly) pulls its output LOW when a train draws
    current, so its raw bit reads 0 occupied / 1 clear. `digital_active_low`
    names the chips wired that way and this class flips their bits, so the
    boolean leaving the interface always means "current detected" regardless of
    how the detector is wired. Polarity is set per chip, because a detector chain
    is wired one way throughout.

    This all stays below `read_block_currents()`, so the detector above the
    interface still sees only a per-block boolean — it never learns which source
    answered, what zone it was in, or what polarity the wiring uses.
    """

    def __init__(
        self,
        digital_chips: Dict[str, HC165Chain],
        digital_bindings: BindingMap,
        current_chips: Optional[Dict[str, Any]] = None,
        current_bindings: Optional[CurrentBindingMap] = None,
        track: Optional[TrackDriver] = None,
        digital_active_low: Optional[Dict[str, bool]] = None,
        lighting_clear_hold_reads: int = LIGHTING_CLEAR_HOLD_READS,
        confirm_reads: int = OCCUPANCY_ON_THRESHOLD,
    ) -> None:
        self._digital_chips = digital_chips
        self._digital = digital_bindings
        self._current_chips = current_chips or {}
        self._current = current_bindings or {}
        # The track driver whose drive zone decides which detector answers.
        # None when the rig has no track chips: nothing can drive the rails, so
        # every block is idle.
        self._track = track
        self._digital_active_low = digital_active_low or {}
        self._lighting_clear_hold_reads = lighting_clear_hold_reads
        # What this driver last reported per block. Read only in the lighting
        # zone, where it is what stops a blind read from clearing a block.
        self._last_reported: Dict[str, bool] = {}
        # Consecutive lighting-zone reads on which the zone's deciding source
        # called a block clear. Reset by any occupied read and by leaving the
        # zone, so it only ever counts an unbroken run; at the threshold the
        # block is released and the count starts over.
        self._lighting_clear_counts: Dict[str, int] = {}
        # Consecutive reads, in any zone, on which a block's deciding source read
        # current. Reset by any clear read.
        self._confirm_reads = confirm_reads
        self._present_runs: Dict[str, int] = {}
        # Blocks whose reported presence is established: a run of
        # `_confirm_reads` present reads since the block was last reported
        # clear. Only these are held through the lighting zone's clear reads.
        self._established: Set[str] = set()
        # Per-block detail of the last read_block_currents() call — which source
        # answered, what it measured, and the commanded drive state at the time.
        # Diagnostic read-back for the raw-occupancy debug endpoint only.
        self._last_read_detail: Dict[str, Dict[str, Any]] = {}
        # One threshold to judge every current channel against, in place of the
        # per-block configured ones. Only the occupancy bench sets this — see
        # `set_current_threshold_override`.
        self._current_threshold_override_a: Optional[float] = None

    def detected_block_ids(self) -> Set[str]:
        """Every block wired to a detector — the union of the two occupancy
        binding maps, which is exactly the key set `read_block_currents()`
        answers for."""
        return set(self._digital) | set(self._current)

    def read_block_currents(self) -> Dict[str, bool]:
        digital_cache: Dict[str, bytes] = {}
        result: Dict[str, bool] = {}
        detail: Dict[str, Dict[str, Any]] = {}
        for block_id in set(self._digital) | set(self._current):
            zone = self._drive_zone(block_id)
            if zone is not DriveZone.LIGHTING:
                # The window counts unbroken agreement inside the zone only. A
                # block that left it was answered by a source trusted to clear
                # on its own, so nothing carries over when it comes back.
                self._lighting_clear_counts[block_id] = 0
            source = deciding_source(
                zone, block_id in self._digital, block_id in self._current
            )
            entry: Dict[str, Any] = {
                "zone": zone.value,
                "driven": zone is DriveZone.DRIVEN,
                "commanded_volts": self._commanded_volts(block_id),
                "source": source,
            }
            present = self._read_source(source, block_id, digital_cache, entry)
            detail[block_id] = entry
            if present:
                runs = self._present_runs.get(block_id, 0) + 1
                self._present_runs[block_id] = runs
                if runs >= self._confirm_reads:
                    self._established.add(block_id)
            else:
                self._present_runs[block_id] = 0
            if zone is not DriveZone.LIGHTING:
                result[block_id] = present
                if not present:
                    self._established.discard(block_id)
                continue
            # The lighting zone. The current channel decides and the '165 is not
            # read at all, because the rail its comparator watches is the rail
            # being pulsed and its bit reports the duty cycle rather than the
            # train (see the class docstring); a digital-only block has nothing
            # else to ask, so it falls back to the '165. No single read can
            # CLEAR an established presence in either case: the last reported
            # value holds through a clear read until the deciding source has
            # read clear for `_lighting_clear_hold_reads` running. A presence
            # that is not established is not held, so a lone noisy read never
            # becomes the run the detector confirms on.
            entry["held_zone"] = True
            held = (
                self._last_reported.get(block_id, False)
                and block_id in self._established
            )
            if present:
                self._lighting_clear_counts[block_id] = 0
                entry["clear_count"] = 0
                entry["held_from_previous"] = False
                result[block_id] = True
            else:
                if held:
                    count = self._lighting_clear_counts.get(block_id, 0) + 1
                    released = count >= self._lighting_clear_hold_reads
                else:
                    count, released = 0, False
                # Start the window over on release, so a block that re-confirms
                # and goes quiet again waits the full window rather than
                # dropping out on its next silent read.
                self._lighting_clear_counts[block_id] = 0 if released else count
                entry["clear_count"] = 0 if released else count
                entry["clear_hold_reads"] = self._lighting_clear_hold_reads
                entry["held_from_previous"] = held and not released
                result[block_id] = held and not released
                if not result[block_id]:
                    self._established.discard(block_id)
        self._last_read_detail = detail
        self._last_reported = dict(result)
        return result

    def last_read_detail(self) -> Dict[str, Dict[str, Any]]:
        """Per-block detail of the last read: source ("current"/"digital"), the
        measured mA or raw bit, and the commanded drive state. Diagnostic only."""
        return self._last_read_detail

    def read_all_sources(self) -> Dict[str, Dict[str, Any]]:
        """Read BOTH detectors for every bound block and report each separately,
        alongside the verdict the zone rule takes from them.

        `read_block_currents` reads only the source its zone trusts — that is the
        whole point of the hybrid, and it is what the runtime wants. It is the
        wrong shape for diagnosing the hybrid itself: at 40% drive the zone is
        `driven`, so the '165 bit is never sampled, and "the current channel says
        clear" cannot be told apart from "the current channel is miswired" without
        the other detector's opinion beside it.

        So this reads both unconditionally and reports `digital` / `current` /
        `overall` per block. It is a diagnostic path: nothing in the runtime calls
        it, no debounce counter moves, `_last_reported` and the lighting-zone
        clear window are left exactly as `read_block_currents` left them. A block
        with no binding on one side reports `None` there rather than False —
        "unwired" and "reads clear" are different answers.

        The lighting zone has the same deciding source as the driven zone — the
        current channel — so `overall` reports that channel's single read. What
        it cannot report is the clear window: a clear there is earned over a run
        of reads this call does not touch, so `held_zone` marks the verdict as
        one the runtime would not have acted on by itself — the same contract
        `self_test.run_occupancy_test` reports for one block.
        """
        digital_cache: Dict[str, bytes] = {}
        out: Dict[str, Dict[str, Any]] = {}
        for block_id in set(self._digital) | set(self._current):
            zone = self._drive_zone(block_id)
            entry: Dict[str, Any] = {
                "zone": zone.value,
                "commanded_volts": self._commanded_volts(block_id),
            }
            digital = (
                self._read_digital(block_id, digital_cache, entry)
                if block_id in self._digital
                else None
            )
            current = (
                self._read_current(block_id, entry)
                if block_id in self._current
                else None
            )
            entry["digital"] = digital
            entry["current"] = current
            entry["driven"] = zone is DriveZone.DRIVEN
            if zone is DriveZone.LIGHTING:
                entry["held_zone"] = True
            source = deciding_source(zone, digital is not None, current is not None)
            entry["source"] = source
            entry["overall"] = bool({"digital": digital, "current": current}.get(source))
            out[block_id] = entry
        return out

    def _drive_zone(self, block_id: str) -> DriveZone:
        """The zone the track driver reports for this block. Never re-derived
        here from a voltage: the track driver put the rails in that state, and
        it is the one place the zone levels are applied (see `DriveZone`)."""
        if self._track is None:
            return DriveZone.IDLE
        return self._track.drive_zone(block_id)

    def _commanded_volts(self, block_id: str) -> Optional[float]:
        """The track driver's last commanded voltage for this block, rounded, for
        the diagnostic detail only — the decision is `_drive_zone`. None when the
        rig has no track driver."""
        if self._track is None:
            return None
        return round(self._track.get_actual_voltage(block_id), 2)

    def _read_source(
        self,
        source: str,
        block_id: str,
        cache: Dict[str, bytes],
        detail: Dict[str, Any],
    ) -> bool:
        """Read the one detector `deciding_source` chose for this block."""
        if source == "current":
            return self._read_current(block_id, detail)
        if source == "digital":
            return self._read_digital(block_id, cache, detail)
        return False

    def _read_digital(
        self,
        block_id: str,
        cache: Dict[str, bytes],
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
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
        if detail is not None:
            detail["raw_bit"] = bit
        # Convert the wiring's polarity to the interface's meaning: True is
        # always "current detected", whichever way the detector drives the line.
        return resolve_detector_bit(
            bit, active_low=self._digital_active_low.get(chip_id, False)
        )

    def set_current_threshold_override(self, threshold_ma: Optional[float]) -> None:
        """Judge every current channel against `threshold_ma` instead of its own
        configured threshold; `None` restores the per-block values.

        Diagnostic seam for the occupancy bench, which exists so an operator can
        sweep one threshold across the whole layout and watch where each block
        crosses over — the calibration `hardware_config` then wants per channel.
        Nothing in the runtime sets this, and it defaults to unset.

        Deliberately an override rather than an edit of `_current`: the bindings
        keep their configured baselines and thresholds, so clearing the override
        restores them exactly, and a bench session cannot leave a driver quietly
        judging against a number nobody configured.
        """
        self._current_threshold_override_a = (
            None if threshold_ma is None else max(0.0, float(threshold_ma)) / 1000.0
        )

    def _read_current(
        self, block_id: str, detail: Optional[Dict[str, Any]] = None
    ) -> bool:
        chip_id, port, baseline_a, configured_a = self._current[block_id]
        threshold_a = (
            configured_a
            if self._current_threshold_override_a is None
            else self._current_threshold_override_a
        )
        chip = self._current_chips.get(chip_id)
        if chip is None:
            return False
        try:
            current = chip.read_current(port + 1)   # port 0 -> sensor channel 1
        except Exception:
            # A failed current-sense transaction is a hardware I/O failure, not a
            # clear block: "Missing never counts as clear" (design_new.md
            # "Hardware setup", Both occupancy sources are mandatory). Re-raise so
            # the detector-read seam escalates it to enter_hardware_failure rather
            # than silently reporting the block empty.
            if detail is not None:
                detail["current_ma"] = None
            raise
        if detail is not None:
            detail["current_ma"] = round(current * 1000.0, 3)
            # The EFFECTIVE threshold — the number the verdict beside it was
            # actually judged against. Reporting the configured one under an
            # override would make the reading and the verdict look inconsistent.
            detail["threshold_ma"] = round(threshold_a * 1000.0, 3)
            detail["configured_threshold_ma"] = round(configured_a * 1000.0, 3)
            detail["baseline_ma"] = round(baseline_a * 1000.0, 3)
        return abs(current - baseline_a) >= threshold_a


def _assert_switch_ports_low(
    switch_chips: Dict[str, HC595Chain],
    switch_output: SwitchBindingMap,
) -> None:
    """Raise unless every coil and standby bit named by a switch binding is low.

    Drops the chains it can before raising, so the caller's failure path starts
    from a de-energized board rather than the state it just rejected."""
    hot: List[str] = []
    for switch_id, (chip_id, port_main, port_diverge, port_standby) in switch_output.items():
        chip = switch_chips.get(chip_id)
        if chip is None:
            continue
        for label, port in (
            ("main", port_main),
            ("diverge", port_diverge),
            ("standby", port_standby),
        ):
            if port is not None and chip.is_high(port):
                hot.append(f"{switch_id}.{label} ({chip_id} port {port})")
    if not hot:
        return
    for chip in switch_chips.values():
        try:
            chip.all_off()
        except Exception:  # noqa: BLE001 - the raise below is the report
            pass
    raise RuntimeError(
        "refusing to start: switch coil outputs are energized after init — "
        + ", ".join(sorted(hot))
    )


def build_real_interfaces(
    device: Any,
    chip_specs: List[Any],
    block_track: TrackBindingMap,
    block_occupancy: BindingMap,
    switch_output: SwitchBindingMap,
    block_occupancy_current: Optional[CurrentBindingMap] = None,
    idle_deadband_v: float = _IDLE_DEADBAND_V,
    track_polarity_of: Optional[Callable[[str], bool]] = None,
    switch_binding_of: Optional[
        Callable[[str], Optional[Tuple[str, int, int, Optional[int]]]]
    ] = None,
    watchdog_timeout_s: Optional[float] = None,
    current_sense_min_drive_v: float = _CURRENT_SENSE_MIN_DRIVE_V,
    lighting_clear_hold_reads: int = LIGHTING_CLEAR_HOLD_READS,
) -> Tuple[Optional[OccupancyInput], Optional[TrackOutput], Optional[SwitchOutput]]:
    """Instantiate + initialize chips grouped by role and wire the three
    interfaces. A role with no chip returns `None` (its simulated default is
    used). Performs live init (PWM reset/freq, shift-register clear, INA3221
    channel enable) so hardware starts in a known state. Occupancy is the hybrid
    of the '165 digital detector and the INA3221 current channels; the returned
    input interface selects between them per block by drive state.

    `track_polarity_of` reports a block's rail-binding `reversed` flag to the
    track driver, which swaps that block's two rails when resolving its binding.
    `switch_binding_of` reports a switch's coil ports to the switch driver the
    same way. Both are read per write rather than captured here, so an operator
    correcting the wiring does not have to rebuild the session to see it; the
    maps passed positionally are what the chips are built and checked against.

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

    # Clear every shift-out chain before anything else on the bus. A '595 powers
    # up with undefined register contents, so until this lands a coil bit may be
    # driving a point motor; the I2C PWM setup below is comparatively slow, and
    # none of it is worth doing ahead of de-energizing a solenoid. Nothing here
    # raises a bit, so the chain is left all-low for the bindings check further
    # down. `build_chip` is cheap and side-effect-free, so building the switch
    # chips in their own pass costs only the second loop.
    for spec in chip_specs:
        if CHIP_CATALOG[spec.kind].role == ROLE_SWITCH:
            chip = build_chip(device, spec)
            chip.set_high_outputs(())      # one latch: every bit low
            switch_chips[spec.chip_id] = chip

    for spec in chip_specs:
        ck = CHIP_CATALOG[spec.kind]
        if ck.role == ROLE_SWITCH:
            continue                       # already built and cleared above
        chip = build_chip(device, spec)
        if ck.role == ROLE_TRACK:
            chip.configure()
            track_chips[spec.chip_id] = chip
        elif ck.role == ROLE_OCCUPANCY:
            if ck.port_kind == PORT_CURRENT_SENSE:
                chip.configure()               # reset + enable all three channels
                current_chips[spec.chip_id] = chip
            else:
                digital_chips[spec.chip_id] = chip

    safety = OutputSafety(track_chips, switch_chips)

    # One watchdog covers both output interfaces: loss of control must drop the
    # track AND the point motors. The targets list is filled just below, before
    # anything can pet the watchdog into an armed state.
    watchdog_targets: List[Any] = []
    watchdog: Optional[OutputWatchdog] = None
    if watchdog_timeout_s is not None and (track_chips or switch_chips):
        trip_record = WatchdogTripRecord()

        def _drop_to_safe_state() -> None:
            # A stalled control loop is the case this exists for, so say so:
            # the rig went dead under a train and the operator is owed the
            # reason. The chips are safed through the shared object; each
            # interface then clears its own observable command state. The
            # daemon may not touch the model, so it only records the transport
            # result in its thread-safe trip record; the runtime thread
            # reconciles the recorded voltages and escalates a failed drop to
            # hardware failure when it consumes that record on its next tick
            # (design_new.md "Power", Loss-of-control watchdog).
            logging.getLogger(__name__).error(
                "output watchdog: no output write landed for %.1fs — every "
                "track voltage and point motor driven to its safe state",
                watchdog_timeout_s,
            )
            failed = False
            try:
                safety.enter_safe_state()
            except SafeStateError:
                failed = True
            except Exception:  # noqa: BLE001 - any transport failure is a failed drop
                failed = True
            for target in watchdog_targets:
                try:
                    target.enter_safe_state()
                except Exception:  # noqa: BLE001
                    failed = True
            trip_record.record(failed)

        watchdog = OutputWatchdog(
            _drop_to_safe_state, timeout_s=watchdog_timeout_s
        )
        watchdog.trip_record = trip_record

    def _block_bus_voltage_readback(block_id: str) -> Optional[float]:
        """Real / Virtual Hardware block-voltage readback: the bus voltage on the
        block's bound current-sense channel — the actual rail voltage
        (design_new.md "Power", the block-voltage read surface). Returns None
        where the block has no current-sense facility; a failed transaction
        raises so the diagnostic consumer reports it as None plus a warning."""
        binding = (block_occupancy_current or {}).get(block_id)
        if binding is None:
            return None
        chip_id, port = binding[0], binding[1]
        chip = current_chips.get(chip_id)
        if chip is None:
            return None
        return float(chip.read_bus_voltage(port + 1))

    block_out = (
        TrackDriver(
            track_chips,
            block_track,
            watchdog=watchdog,
            polarity_of=track_polarity_of,
            safety=safety,
            readback_of=_block_bus_voltage_readback,
            idle_deadband_v=idle_deadband_v,
            current_sense_min_drive_v=current_sense_min_drive_v,
        )
        if track_chips
        else None
    )
    switch_out = (
        SwitchDriver(
            switch_chips,
            switch_output,
            watchdog=watchdog,
            safety=safety,
            binding_of=switch_binding_of,
        )
        if switch_chips
        else None
    )
    watchdog_targets.extend(
        iface for iface in (block_out, switch_out) if iface is not None
    )

    # Last word on the outputs before the session goes live: no bit a coil sits
    # on may be high. The clear above already guarantees it — this re-checks it
    # against the bindings so that a write added between the two cannot quietly
    # energize a point motor for the whole session. Failing the build is right:
    # an unusable session beats a burning solenoid nobody is watching.
    _assert_switch_ports_low(switch_chips, switch_output)

    def _resync_hardware() -> None:
        """Redo the live init above against the chips already built.

        A reopened USB bridge is a blank bridge, and a bridge that dropped
        because its supply sagged probably took the chips down with it: a '595
        that lost power comes back with undefined register contents, which on a
        coil bit is a point motor burning with nobody watching. So this repeats
        the build's own order — chains low first, then the slow I2C setup, then
        the bindings check.

        Track voltage is deliberately not restored: `configure()` leaves every
        PWM channel at zero, and the control loop re-commands voltage on its
        next tick. The track driver is told its channels were zeroed, so until
        that tick it reports every block idle — the state the rails are really
        in — and the detector reads each block through its '165.

        Raising out of here is deliberate. `Ch347Device._recover()` treats a
        hook that raises as a failed reconnect: it closes the handle and waits
        out the cooldown, which is the honest outcome when the outputs could
        not be brought back up safely.
        """
        safety.enter_safe_state()
        for chip in track_chips.values():
            chip.configure()
        if block_out is not None:
            block_out.forget_commanded_voltages()
        for chip in current_chips.values():
            chip.configure()
        _assert_switch_ports_low(switch_chips, switch_output)

    register_hook = getattr(device, "add_reconnect_hook", None)
    if register_hook is not None:      # the fake device has no link to lose
        register_hook(_resync_hardware)

    input_iface: Optional[OccupancyInput] = None
    if digital_chips or current_chips:
        input_iface = OccupancyDriver(
            digital_chips,
            block_occupancy,
            current_chips=current_chips,
            current_bindings=block_occupancy_current or {},
            # The track driver owns the drive zone; the detector asks it.
            track=block_out,
            lighting_clear_hold_reads=lighting_clear_hold_reads,
            # Which shift-in chains are wired active-low, so the interface can
            # normalize their bits (design_new.md Power, Block occupancy detection).
            digital_active_low={
                spec.chip_id: bool(getattr(spec, "active_low", False))
                for spec in chip_specs
                if spec.chip_id in digital_chips
            },
        )
    return input_iface, block_out, switch_out
