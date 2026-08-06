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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from block_occupancy import OccupancyInput
from block_power import MAX_TRACK_VOLTAGE, TrackOutput
from constants import LIGHTING_CLEAR_HOLD_READS
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
from hardware.safety import OutputSafety
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
# Direct-call fallback for the drive level at or above which the current-sense
# channel is trusted to decide occupancy; below it a drive is too weak to put a
# readable load on the sensor (see `OccupancyDriver`). It sits above the default
# `SpeedProfile.lighting_voltage`, because a lit-but-stationary train must land
# in the lighting zone rather than be judged as driven.
_CURRENT_SENSE_MIN_DRIVE_V = 4.0


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
    """

    def __init__(
        self,
        chips_by_id: Dict[str, PCA9685],
        bindings: "TrackBindingMap",
        watchdog: Optional[OutputWatchdog] = None,
        polarity_of: Optional[Callable[[str], bool]] = None,
        safety: Optional[OutputSafety] = None,
    ) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._voltages: Dict[str, float] = {}
        self._watchdog = watchdog
        self._polarity_of = polarity_of
        self._safety = safety

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
        # two H-bridge inputs are never driven together.
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
        return self._voltages.get(block_id, 0.0)

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
        safety: Optional[OutputSafety] = None,
    ) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._last: Dict[str, int] = {}
        self._watchdog = watchdog
        self._pulse_s = pulse_s
        self._safety = safety

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

    `voltage_of` reports the last commanded block voltage (from the real track
    interface). Its magnitude puts the block in one of three zones, because the
    two detectors are trustworthy in different ones:

      - **idle** (|V| <= `idle_deadband_v`): nothing is driving the rails, so
        there is no current to sense and the '165 detector decides.
      - **driven** (|V| >= `current_sense_min_drive_v`): the rail voltage swamps
        the '165's comparator, but a motor under real drive draws a current the
        sense channel reads cleanly, so the current channel decides.
      - **lighting** (between the two): a stopped-but-lit locomotive. **No
        single read may clear a block here.** The drive is strong enough to
        disturb the '165 (a low-duty PWM makes its bit flicker with sampling
        phase) yet far too weak to put a readable load on the current channel,
        which just reads ~0 mA. Either source may CONFIRM on one read, but a
        clear has to be earned: the block is let go only after BOTH sources
        have read clear for `LIGHTING_CLEAR_HOLD_READS` consecutive reads, and
        until then the last reported value holds.

    That release rule is not merely a debounce, and neither was the indefinite
    hold it replaced. Occupancy and drive are a closed loop — a block that reads
    clear is released, which cuts its voltage, which changes which detector
    answers — so a false clear in the lighting zone used to send the block
    around a confirm/vacate limit cycle at a few hertz, with the track voltage
    pulsing in step. The sustained-agreement window breaks that loop at its one
    open link: a detector flickering under PWM asserts occupied somewhere inside
    the window and resets the count, so it delays the release rather than racing
    it, and the cycle cannot re-form.

    Holding forever broke the loop too, but it made the hold a latch: a block
    both sources called clear stayed OCCUPIED for as long as software kept
    driving it, and software kept driving it *because* it was occupied. Nothing
    physical could ever release it. Requiring sustained agreement keeps the
    stability and gives the latch an exit.

    The trade is deliberate and fail-safe in the direction that matters: a
    single blind read never clears a block, and a locomotive that keeps its
    detector alive holds its block indefinitely. The exposure it accepts is a
    locomotive that goes invisible to BOTH sources for the whole window — one
    drawing under `threshold_ma` whose '165 also reads clear. Raising the
    profile's `lighting_voltage` is what keeps a real lit loco above that floor;
    the window only decides how long a genuinely silent block waits.

    A block may have both sources, only one, or neither:

      - driven + has current    -> current channel
      - lighting                -> either source confirms; clears only on
                                   sustained agreement from both
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
        voltage_of: Optional[Callable[[str], float]] = None,
        idle_deadband_v: float = _IDLE_DEADBAND_V,
        digital_active_low: Optional[Dict[str, bool]] = None,
        current_sense_min_drive_v: float = _CURRENT_SENSE_MIN_DRIVE_V,
        lighting_clear_hold_reads: int = LIGHTING_CLEAR_HOLD_READS,
    ) -> None:
        self._digital_chips = digital_chips
        self._digital = digital_bindings
        self._current_chips = current_chips or {}
        self._current = current_bindings or {}
        self._voltage_of = voltage_of
        self._idle_deadband_v = idle_deadband_v
        self._digital_active_low = digital_active_low or {}
        self._current_sense_min_drive_v = current_sense_min_drive_v
        self._lighting_clear_hold_reads = lighting_clear_hold_reads
        # What this driver last reported per block. Read only in the lighting
        # zone, where it is what stops a blind read from clearing a block.
        self._last_reported: Dict[str, bool] = {}
        # Consecutive lighting-zone reads on which BOTH sources called a block
        # clear. Reset by any occupied read and by leaving the zone, so it only
        # ever counts unbroken agreement; at the threshold the block is released
        # and the count starts over.
        self._lighting_clear_counts: Dict[str, int] = {}
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
            if zone != "lighting":
                # The window counts unbroken agreement inside the zone only. A
                # block that left it was answered by a source trusted to clear
                # on its own, so nothing carries over when it comes back.
                self._lighting_clear_counts[block_id] = 0
            entry: Dict[str, Any] = {
                "zone": zone,
                "driven": zone == "driven",
                "commanded_volts": (
                    round(self._voltage_of(block_id), 2)
                    if self._voltage_of is not None
                    else None
                ),
            }
            if zone == "lighting":
                # No single read can clear a block here — see the class
                # docstring. Both sources are read (either may CONFIRM), and the
                # last reported value holds through a both-clear read until they
                # have agreed for `_lighting_clear_hold_reads` reads running.
                entry["source"] = "held"
                digital = (
                    self._read_digital(block_id, digital_cache, entry)
                    if block_id in self._digital
                    else False
                )
                current = (
                    self._read_current(block_id, entry)
                    if block_id in self._current
                    else False
                )
                held = self._last_reported.get(block_id, False)
                if digital or current:
                    self._lighting_clear_counts[block_id] = 0
                    entry["clear_count"] = 0
                    entry["held_from_previous"] = False
                    result[block_id] = True
                else:
                    count = self._lighting_clear_counts.get(block_id, 0) + 1
                    released = count >= self._lighting_clear_hold_reads
                    # Start the window over on release, so a block that
                    # re-confirms and goes quiet again waits the full window
                    # rather than dropping out on its next silent read.
                    self._lighting_clear_counts[block_id] = 0 if released else count
                    entry["clear_count"] = 0 if released else count
                    entry["clear_hold_reads"] = self._lighting_clear_hold_reads
                    entry["held_from_previous"] = held and not released
                    result[block_id] = held and not released
            elif zone == "driven" and block_id in self._current:
                entry["source"] = "current"
                result[block_id] = self._read_current(block_id, entry)
            elif block_id in self._digital:
                entry["source"] = "digital"
                result[block_id] = self._read_digital(block_id, digital_cache, entry)
            elif block_id in self._current:
                entry["source"] = "current"
                result[block_id] = self._read_current(block_id, entry)
            else:
                entry["source"] = "none"
                result[block_id] = False
            detail[block_id] = entry
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

        The lighting zone has no single-read verdict to give (a clear there is
        earned over a window this call does not touch), so `overall` reports what
        either source can confirm on one read, and `held_zone` marks it — the same
        contract `self_test.run_occupancy_test` reports for one block.
        """
        digital_cache: Dict[str, bytes] = {}
        out: Dict[str, Dict[str, Any]] = {}
        for block_id in set(self._digital) | set(self._current):
            entry: Dict[str, Any] = {
                "zone": self._drive_zone(block_id),
                "commanded_volts": (
                    round(self._voltage_of(block_id), 2)
                    if self._voltage_of is not None
                    else None
                ),
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
            entry["driven"] = entry["zone"] == "driven"
            if entry["zone"] == "lighting":
                entry["source"] = "held"
                entry["held_zone"] = True
                entry["overall"] = bool(digital or current)
            elif entry["zone"] == "driven" and current is not None:
                entry["source"] = "current"
                entry["overall"] = bool(current)
            elif digital is not None:
                entry["source"] = "digital"
                entry["overall"] = bool(digital)
            elif current is not None:
                entry["source"] = "current"
                entry["overall"] = bool(current)
            else:
                entry["source"] = "none"
                entry["overall"] = False
            out[block_id] = entry
        return out

    def _drive_zone(self, block_id: str) -> str:
        """Which detector regime this block's COMMANDED voltage puts it in:
        "idle", "lighting", or "driven" (see the class docstring).

        Commanded, not measured: the question is how hard software is driving
        the rails, which is what decides whether a detector can be believed.
        """
        if self._voltage_of is None:
            return "idle"
        volts = abs(self._voltage_of(block_id))
        if volts <= self._idle_deadband_v:
            return "idle"
        if volts >= self._current_sense_min_drive_v:
            return "driven"
        return "lighting"

    def _is_driven(self, block_id: str) -> bool:
        return self._drive_zone(block_id) == "driven"

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
            if detail is not None:
                detail["current_ma"] = None
            return False
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
        def _drop_to_safe_state() -> None:
            # A stalled control loop is the case this exists for, so say so:
            # the rig went dead under a train and the operator is owed the
            # reason. The chips are safed through the shared object; each
            # interface then clears its own observable command state.
            logging.getLogger(__name__).error(
                "output watchdog: no output write landed for %.1fs — every "
                "track voltage and point motor driven to its safe state",
                watchdog_timeout_s,
            )
            safety.enter_safe_state()
            for target in watchdog_targets:
                target.enter_safe_state()

        watchdog = OutputWatchdog(_drop_to_safe_state, timeout_s=watchdog_timeout_s)

    block_out = (
        TrackDriver(
            track_chips,
            block_track,
            watchdog=watchdog,
            polarity_of=track_polarity_of,
            safety=safety,
        )
        if track_chips
        else None
    )
    switch_out = (
        SwitchDriver(switch_chips, switch_output, watchdog=watchdog, safety=safety)
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
        next tick.

        Raising out of here is deliberate. `Ch347Device._recover()` treats a
        hook that raises as a failed reconnect: it closes the handle and waits
        out the cooldown, which is the honest outcome when the outputs could
        not be brought back up safely.
        """
        safety.enter_safe_state()
        for chip in track_chips.values():
            chip.configure()
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
            voltage_of=(block_out.get_actual_voltage if block_out is not None else None),
            idle_deadband_v=idle_deadband_v,
            current_sense_min_drive_v=current_sense_min_drive_v,
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
