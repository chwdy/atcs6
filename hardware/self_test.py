"""Bus scan + interactive port-test for the Hardware Setup screen.

`run_bus_scan` opens a transient device and reports every I2C address that
answers right now. That scan is the whole of the automatic proof: an I2C chip is
present when its address is on the bus, and a shift register (SPI / GPIO) cannot
be detected at all — it has no acknowledgement and no ID register, so a '595 or
'165 reads exactly the same unplugged as wired. Those are proven by hand
instead: `run_port_test` pulses one output line or reads one input line so the
user can watch a wire respond.

Every entry point accepts an already-open `device` — the fake in tests, and the
running session's controller when there is one — or falls back to opening a real
`Ch347Device` of its own. The caller decides which: the routes hand over the live
session's device (under `simulation_lock`) rather than letting a second open be
refused, since the controller is a single USB endpoint and the tools are most
useful on exactly the rig a session is holding. A passed-in device is used and
left open; only a device opened here is closed here.
"""

import time
from typing import Any, Callable, Dict, List, Optional

from block_power import MAX_TRACK_VOLTAGE

from hardware.catalog import (
    CHIP_CATALOG,
    MAX_PORT_TEST_PULSE_S,
    PORT_CURRENT_SENSE,
    PORT_TEST_PULSE_S,
)
from hardware.chips.hc595 import sleep_holding
from hardware.interfaces import (
    DriveZone,
    build_chip,
    classify_drive_zone,
    deciding_source,
)

# How long the track direction test holds each leg of its sequence. Long enough
# that a loco visibly moves one way, stops, and moves back the other way.
TRACK_TEST_HOLD_S = 1.0


def _open_real_device(cfg: Any) -> Any:
    from hardware.ch347_device import open_ch347_device

    return open_ch347_device(getattr(cfg.controller, "device_port", None))


def _zero_every_output(chip: Any, chip_id: str) -> List[str]:
    """Write `0` to every output channel of a chip a test drove — the safe-state
    sequence a Setup tool completes itself when it ends (design_new.md "Power",
    Hardware setup, Closing a Setup tool restores the safe state). Every channel,
    not only the ones the test touched. Returns one message when the write
    failed, empty when it landed; a chip with no outputs has nothing to zero.
    """
    if not hasattr(chip, "all_off"):
        return []
    try:
        chip.all_off()
    except Exception as exc:  # noqa: BLE001 - reported as a hardware I/O failure
        return [f"{chip_id}: {exc}"]
    return []


def _after_zeroing(result: Dict[str, Any], zero_failures: List[str]) -> Dict[str, Any]:
    """Fold a failed closing zeroing into a test's result.

    A failed zeroing write is a hardware I/O failure (design_new.md "Power",
    Hardware I/O failure is fatal to the physical session), so the result says
    the test failed and carries `zero_failures` for the route to report through
    `report_close_failures(...)`. The route removes that key before answering.
    """
    if not zero_failures:
        return result
    message = "could not zero every output after the test: " + "; ".join(zero_failures)
    if not result.get("ok") and result.get("error"):
        message = f"{result['error']}; {message}"
    return {"ok": False, "error": message, "zero_failures": list(zero_failures)}


def run_bus_scan(
    cfg: Any,
    device: Optional[Any] = None,
    open_device: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Open the controller and report every I2C address answering right now.

    This is the Hardware Setup screen's Detect / Rescan. It asks the bus what is
    out there rather than asking each configured chip whether it is there, so the
    answer includes addresses no chip is configured for yet — a chip strapped to
    an address the user did not expect shows up as an unclaimed address instead
    of as silence.

    Addresses come back as lowercase hex strings (`"0x40"`), the same form the
    address picker uses, so the caller compares them without re-formatting.
    """
    controller_kind = getattr(cfg.controller, "kind", "ch347")
    close_after = False
    if device is None:
        opener = open_device or (lambda: _open_real_device(cfg))
        try:
            device = opener()
        except Exception as exc:  # missing or busy device
            return {
                "ok": False,
                "error": str(exc),
                "controller": {"kind": controller_kind, "reachable": False, "i2c_scan": []},
            }
        close_after = True

    try:
        try:
            scan = device.i2c_scan()
        except Exception:
            scan = []
        return {
            "ok": True,
            "controller": {
                "kind": controller_kind,
                "reachable": True,
                "i2c_scan": [f"0x{addr:02x}" for addr in scan],
            },
        }
    finally:
        if close_after:
            try:
                device.close()
            except Exception:
                pass


def run_port_test(
    cfg: Any,
    chip_id: str,
    port: int,
    action: str,
    device: Optional[Any] = None,
    open_device: Optional[Callable[[], Any]] = None,
    pulse_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Pulse one output line ('595 bit / PCA9685 channel) or read one input line
    ('165 bit / current channel) so the user can confirm the wire physically
    responds. `port` is the raw line — a channel on a PWM chip, a bit on a shift
    register — so the port picker can drive any single channel (its double-click
    pulse). The whole-block track direction sweep is `run_track_test`.

    `pulse_s` overrides how long the line is held (default `PORT_TEST_PULSE_S`),
    for a user measuring with something slower than an LED. The result echoes the
    width actually used, so the UI can say what it just did."""
    spec = next((c for c in cfg.chips if c.chip_id == chip_id), None)
    if spec is None:
        return {"ok": False, "error": f"unknown chip {chip_id!r}"}

    close_after = False
    if device is None:
        opener = open_device or (lambda: _open_real_device(cfg))
        try:
            device = opener()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        close_after = True

    try:
        chip = build_chip(device, spec)
        if action == "read_input":
            # A current-sense channel (INA3221 / INA219) reports live current in
            # mA for calibration; a digital detector ('165) reports its bit.
            if CHIP_CATALOG[spec.kind].port_kind == PORT_CURRENT_SENSE:
                chip.configure()
                channel = port + 1               # port 0 -> sensor channel 1
                return {
                    "ok": True,
                    "current_ma": chip.read_current(channel) * 1000.0,
                    "bus_v": chip.read_bus_voltage(channel),
                }
            return {"ok": True, "value": bool(chip.read_bit(port))}
        if action == "pulse_output":
            hold_s = clamp_pulse_s(pulse_s)
            try:
                _pulse(spec.kind, chip, port, hold_s=hold_s)
                result: Dict[str, Any] = {"ok": True, "pulse_s": hold_s}
            except Exception as exc:  # noqa: BLE001 - the chip is still zeroed
                result = {"ok": False, "error": str(exc)}
            # Ending the test zeroes every output of the chip it drove, not just
            # the pulsed line (Closing a Setup tool restores the safe state).
            return _after_zeroing(result, _zero_every_output(chip, chip_id))
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if close_after:
            try:
                device.close()
            except Exception:
                pass


def _pulse(kind: str, chip: Any, line: int, hold_s: float = PORT_TEST_PULSE_S) -> None:
    """Drive one output line for `hold_s`, then return it to the off state — the
    port picker's double-click "does this wire respond" pulse.

    `line` is the raw channel/bit: a single PWM channel on a PCA9685 (0..15, not
    a pair), a single output bit on a '595. Both kinds get the same width, long
    enough to read on a meter (`PORT_TEST_PULSE_S`) rather than the switch-coil
    throw width, which is unmeasurable by design — see there for why holding a
    coil-bound bit this long is a bench-only activity.

    A '595 hold longer than the chain's guard ceiling sleeps through
    `sleep_holding`, which re-latches inside the window so the guard does not
    release the line partway through the measurement.

    De-energizing runs in a `finally`: `run_port_test` reports a raised exception
    as a failed test, so without it a bridge that dropped mid-pulse would answer
    "port test failed" while leaving the coil energized."""
    if kind == "hc595":
        try:
            chip.set_output(line, True)
            sleep_holding(chip, hold_s)
        finally:
            chip.set_output(line, False)
    elif kind == "pca9685":
        chip.configure()
        try:
            chip.set_duty_cycle(line, 0.5)   # this one channel at half power
            time.sleep(hold_s)
        finally:
            chip.set_duty_cycle(line, 0.0)
    else:
        raise ValueError(f"chip kind {kind!r} has no pulse-output test")


def clamp_pulse_s(value: Any, default: float = PORT_TEST_PULSE_S) -> float:
    """Coerce a requested pulse width to a usable one.

    The width crosses the REST boundary, so it is clamped rather than trusted:
    anything unparseable falls back to `default`, and an over-long request is cut
    to `MAX_PORT_TEST_PULSE_S`. The floor is 0 — a caller may legitimately ask
    for the shortest possible blip."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds:                       # NaN
        return default
    return max(0.0, min(seconds, MAX_PORT_TEST_PULSE_S))


def _drive_sequence(
    chip: Any,
    forward: int,
    reverse: int,
    volts: float = MAX_TRACK_VOLTAGE,
    hold_s: float = TRACK_TEST_HOLD_S,
    on_voltage: Optional[Callable[[float], None]] = None,
) -> None:
    """Drive a block's two rails through both polarities so the user can watch a
    loco move: full voltage one way, both rails off, full voltage the other way.

    `forward` and `reverse` are the block's two bound PWM channels (its H-bridge
    inputs). A polarity is one channel at duty with the other at zero; only one is
    ever driven at a time, since both high would shoot through the bridge. The
    track is returned to zero in a `finally` so an exception mid-sequence (a
    dropped USB bridge, an interrupted request) still leaves the rails dead.

    `on_voltage` is called with the voltage each leg puts on the rails, signed in
    terms of the two channels passed in (`forward` positive), as the leg starts —
    so a caller can mirror the sweep somewhere the operator watches it while it
    runs. The `finally`'s zero is reported too, so a sweep that dies partway
    leaves the mirror dead alongside the rails rather than latched at the last
    leg it managed to drive.
    """
    chip.configure()
    duty = min(abs(volts) / MAX_TRACK_VOLTAGE, 1.0) if MAX_TRACK_VOLTAGE else 0.0

    def _report(driven: float) -> None:
        if on_voltage is not None:
            on_voltage(driven)

    try:
        chip.set_duty_cycle(reverse, 0.0)      # forward rail hot, reverse at ground
        chip.set_duty_cycle(forward, duty)
        _report(volts)
        time.sleep(hold_s)
        chip.set_duty_cycle(forward, 0.0)      # both rails off
        chip.set_duty_cycle(reverse, 0.0)
        _report(0.0)
        time.sleep(hold_s)
        chip.set_duty_cycle(reverse, duty)     # reverse rail hot, forward at ground
        _report(-volts)
        time.sleep(hold_s)
    finally:
        chip.set_duty_cycle(forward, 0.0)
        chip.set_duty_cycle(reverse, 0.0)
        _report(0.0)


def run_track_test(
    cfg: Any,
    block_id: str,
    device: Optional[Any] = None,
    open_device: Optional[Callable[[], Any]] = None,
    on_voltage: Optional[Callable[[float], None]] = None,
    polarity_reversed: bool = False,
) -> Dict[str, Any]:
    """Sweep a block's track through both polarities (the block editor's track
    Test button). Reads the block's `block_track` binding, then drives its two
    bound channels forward / off / reverse so a loco on the block runs, stops,
    and reverses. Leaves every channel of the block's PWM chip at 0; a failed
    zeroing comes back as `zero_failures` for the caller to report as a hardware
    I/O failure.

    `on_voltage` reports each leg as it starts, as the *nominal* signed voltage —
    the same value the runtime would command through `TrackOutput.set_voltage`
    for those rails — so a caller can mirror the sweep onto the model and the
    operator sees the block energize while it happens.

    `polarity_reversed` is the block's rail-binding `TrackBinding.reversed` flag,
    and the sweep resolves it exactly as `TrackDriver.set_voltage` does: the two
    channels trade places, so the first leg drives the channel the binding calls
    `reverse`. The sweep sits directly under the block panel's Track direction
    control, and it is the only way to check that control: an operator watches a
    loco run the wrong way, flips the block to Reversed, and sweeps again to see
    it run the right way. A sweep that drove the binding raw would run backwards
    both times and never confirm the correction — discovery still works because a
    fresh binding is Normal. Resolving the flag here also keeps the mirrored sign
    honest for free: the first leg is nominally positive on every block, because
    it drives whichever rail a nominal positive command drives.
    """
    binding = cfg.block_track.get(block_id)
    if binding is None:
        return {"ok": False, "error": f"block {block_id!r} has no track binding"}
    spec = _spec_for(cfg, binding.chip_id)
    if spec is None:
        return {"ok": False, "error": f"unknown chip {binding.chip_id!r}"}

    close_after = False
    if device is None:
        opener = open_device or (lambda: _open_real_device(cfg))
        try:
            device = opener()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        close_after = True

    # Resolve the binding the way the driver does, so the sweep and the runtime
    # agree about which physical rail "forward" names on this block.
    forward, reverse = binding.forward, binding.reverse
    if polarity_reversed:
        forward, reverse = reverse, forward

    try:
        chip = build_chip(device, spec)
    except Exception as exc:
        if close_after:
            try:
                device.close()
            except Exception:
                pass
        return {"ok": False, "error": str(exc)}

    try:
        try:
            _drive_sequence(chip, forward, reverse, on_voltage=on_voltage)
            result: Dict[str, Any] = {
                "ok": True, "volts": MAX_TRACK_VOLTAGE, "hold_s": TRACK_TEST_HOLD_S
            }
        except Exception as exc:  # noqa: BLE001 - the chip is still zeroed
            result = {"ok": False, "error": str(exc)}
        # Ending the sweep zeroes every channel of the chip it drove, not just
        # the block's two (Closing a Setup tool restores the safe state).
        return _after_zeroing(result, _zero_every_output(chip, binding.chip_id))
    finally:
        if close_after:
            try:
                device.close()
            except Exception:
                pass


def run_occupancy_test(
    cfg: Any,
    block_id: str,
    device: Optional[Any] = None,
    open_device: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Read both of a block's occupancy detectors at once and report which one
    decides, plus the verdict it gives.

    A block's occupancy comes from one of two sources depending on whether the
    block is being driven (see `OccupancyDriver`): the digital detector ('165
    bit) while the block is idle, the current-sense channel (INA3221 / INA219)
    once anything is driving the rails — lighting voltage included, because a
    driven rail is exactly what the '165 cannot be read through. This reads both
    regardless, reads the drive state back off the PWM chip's own registers, and
    applies that same rule — so the user sees each detector's raw state side by
    side and the one answer the runtime would take from them.

    Nothing is driven: the block is left in whatever state it was already in.
    """
    close_after = False
    if device is None:
        opener = open_device or (lambda: _open_real_device(cfg))
        try:
            device = opener()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        close_after = True

    try:
        deadband = _idle_deadband_v(cfg)
        min_drive = _current_sense_min_drive_v(cfg)
        track = _read_track_state(device, cfg, block_id)

        # The same zone rule and detector precedence the runtime uses — one
        # definition of each, in `hardware.interfaces` — applied to the voltage
        # read back off the PWM chip, since a test builds no track driver. The
        # lighting zone decides on the current channel as the driven zone does,
        # against the channel's lighting threshold rather than its driven one,
        # and a clear there takes a sustained run of reads.
        zone = classify_drive_zone(
            track["volts"] if track["bound"] else 0.0, deadband, min_drive
        )
        driven = zone is DriveZone.DRIVEN
        digital = _read_digital_detector(device, cfg, block_id)
        current = _read_current_detector(device, cfg, block_id, driven=driven)
        held_zone = zone is DriveZone.LIGHTING
        source = deciding_source(zone, digital["bound"], current["bound"])

        decided = {"digital": digital, "current": current}.get(source)
        occupied = bool(decided and decided.get("occupied"))
        return {
            "ok": True,
            "block_id": block_id,
            "track": track,
            "driven": driven,
            "zone": zone.value,
            # A test is a one-shot read with no previous value to hold and no
            # window to count, so in the lighting zone it reports what the
            # deciding source sees right now. `held_zone` tells the UI that a
            # clear here would not have vacated the block on its own.
            "held_zone": held_zone,
            "deadband_v": deadband,
            "min_drive_v": min_drive,
            "digital": digital,
            "current": current,
            "source": source,
            "occupied": occupied,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if close_after:
            try:
                device.close()
            except Exception:
                pass


def _idle_deadband_v(cfg: Any) -> float:
    """The drive-state deadband the runtime uses, taken from the configuration
    the session was built with — both zone thresholds are per-configuration
    tunables, and `build_real_interfaces` hands these same two values to
    `TrackDriver`, which owns the zone. Reading the module default instead would make the bench
    test disagree with the running session on any block whose operator tuned
    them, which is the one case the settings exist for.

    The default is the fallback for a config that does not carry the field at
    all (a directly-constructed test stub). Imported here rather than at module
    scope so config keeps depending on hardware, not the reverse."""
    from hardware_config import DEFAULT_IDLE_DEADBAND_V

    value = getattr(cfg, "idle_deadband_v", None)
    return DEFAULT_IDLE_DEADBAND_V if value is None else float(value)


def _current_sense_min_drive_v(cfg: Any) -> float:
    """The drive floor above which the current channel decides, resolved the
    same way and for the same reason as `_idle_deadband_v`."""
    from hardware_config import DEFAULT_CURRENT_SENSE_MIN_DRIVE_V

    value = getattr(cfg, "current_sense_min_drive_v", None)
    return DEFAULT_CURRENT_SENSE_MIN_DRIVE_V if value is None else float(value)


def _spec_for(cfg: Any, chip_id: str) -> Optional[Any]:
    return next((c for c in cfg.chips if c.chip_id == chip_id), None)


def _read_track_state(device: Any, cfg: Any, block_id: str) -> Dict[str, Any]:
    """The voltage the block's track is currently outputting, read back from the
    two bound PWM channels. Signed: positive when the forward channel is driven."""
    binding = cfg.block_track.get(block_id)
    if binding is None:
        return {"bound": False, "volts": 0.0}
    spec = _spec_for(cfg, binding.chip_id)
    result: Dict[str, Any] = {
        "bound": True, "chip_id": binding.chip_id,
        "forward": binding.forward, "reverse": binding.reverse, "volts": 0.0,
    }
    if spec is None:
        result["error"] = f"unknown chip {binding.chip_id!r}"
        return result
    try:
        chip = build_chip(device, spec)
        forward = chip.read_duty_cycle(binding.forward)
        reverse = chip.read_duty_cycle(binding.reverse)
        result["volts"] = (forward - reverse) * MAX_TRACK_VOLTAGE
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _read_digital_detector(device: Any, cfg: Any, block_id: str) -> Dict[str, Any]:
    """The block's '165 detector bit, resolved the way the runtime resolves it.

    The raw bit does not mean "occupied" on its own — the chip's `active_low`
    decides that (`resolve_detector_bit`). This reports both: `value` is what the
    pin reads, `occupied` what the runtime would conclude. Reading only the pin
    would make this test disagree with the simulation on exactly the wiring the
    polarity setting exists for."""
    from hardware.interfaces import resolve_detector_bit

    binding = cfg.block_occupancy.get(block_id)
    if binding is None:
        return {"bound": False}
    spec = _spec_for(cfg, binding.chip_id)
    active_low = bool(getattr(spec, "active_low", False)) if spec is not None else False
    result: Dict[str, Any] = {
        "bound": True, "chip_id": binding.chip_id, "port": binding.port,
        "active_low": active_low,
    }
    if spec is None:
        result["error"] = f"unknown chip {binding.chip_id!r}"
        return result
    try:
        chip = build_chip(device, spec)
        value = bool(chip.read_bit(binding.port))
        result["value"] = value
        result["occupied"] = resolve_detector_bit(value, active_low=active_low)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _read_current_detector(
    device: Any, cfg: Any, block_id: str, *, driven: bool = False
) -> Dict[str, Any]:
    """The block's current-sense channel, with the calibration it is judged
    against: occupied when `abs(current - baseline)` reaches the threshold for
    the block's drive zone — `driven_threshold_ma` (or `threshold_ma` when it is
    unset) while driven, `threshold_ma` otherwise."""
    from hardware_config import DEFAULT_CURRENT_BASELINE_MA, DEFAULT_CURRENT_THRESHOLD_MA

    binding = cfg.block_occupancy_current.get(block_id)
    if binding is None:
        return {"bound": False}
    baseline = (
        binding.baseline_ma if binding.baseline_ma is not None else DEFAULT_CURRENT_BASELINE_MA
    )
    threshold = (
        binding.threshold_ma if binding.threshold_ma is not None else DEFAULT_CURRENT_THRESHOLD_MA
    )
    if driven and binding.driven_threshold_ma is not None:
        threshold = binding.driven_threshold_ma
    spec = _spec_for(cfg, binding.chip_id)
    result: Dict[str, Any] = {
        "bound": True, "chip_id": binding.chip_id, "port": binding.port,
        "baseline_ma": baseline, "threshold_ma": threshold,
    }
    if spec is None:
        result["error"] = f"unknown chip {binding.chip_id!r}"
        return result
    try:
        chip = build_chip(device, spec)
        chip.configure()
        channel = binding.port + 1             # port 0 -> sensor channel 1
        current_ma = chip.read_current(channel) * 1000.0
        result["current_ma"] = current_ma
        result["bus_v"] = chip.read_bus_voltage(channel)
        result["occupied"] = abs(current_ma - baseline) >= threshold
    except Exception as exc:
        result["error"] = str(exc)
    return result
