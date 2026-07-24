"""Bus scan + interactive port-test for the Hardware Setup screen.

`run_bus_scan` opens a transient device and reports every I2C address that
answers right now. That scan is the whole of the automatic proof: an I2C chip is
present when its address is on the bus, and a shift register (SPI / GPIO) cannot
be detected at all — it has no acknowledgement and no ID register, so a '595 or
'165 reads exactly the same unplugged as wired. Those are proven by hand
instead: `run_port_test` pulses one output line or reads one input line so the
user can watch a wire respond.

Every entry point accepts an already-open `device` (the fake, in tests) or falls
back to opening a real `Ch347Device`. If a real session already holds the CH347,
opening fails and the result reports the device as unreachable ("busy") rather
than raising — commissioning is a sim-mode activity.
"""

import time
from typing import Any, Callable, Dict, Optional

from block_power import MAX_TRACK_VOLTAGE

from hardware.catalog import (
    CHIP_CATALOG,
    PORT_CURRENT_SENSE,
    SWITCH_THROW_PULSE_S,
)
from hardware.interfaces import build_chip

# How long the track direction test holds each leg of its sequence. Long enough
# that a loco visibly moves one way, stops, and moves back the other way.
TRACK_TEST_HOLD_S = 1.0


def _open_real_device(cfg: Any) -> Any:
    from hardware.ch347_device import open_ch347_device

    return open_ch347_device(getattr(cfg.controller, "device_port", None))


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
) -> Dict[str, Any]:
    """Pulse one output line ('595 bit / PCA9685 channel) or read one input line
    ('165 bit / current channel) so the user can confirm the wire physically
    responds. `port` is the raw line — a channel on a PWM chip, a bit on a shift
    register — so the port picker can drive any single channel (its double-click
    pulse). The whole-block track direction sweep is `run_track_test`."""
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
            _pulse(spec.kind, chip, port)
            return {"ok": True}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if close_after:
            try:
                device.close()
            except Exception:
                pass


def _pulse(kind: str, chip: Any, line: int, hold_s: float = 0.4) -> None:
    """Briefly drive one output line, then return it to the off state — the port
    picker's double-click "does this wire respond" pulse.

    `line` is the raw channel/bit: a single PWM channel on a PCA9685 (0..15, not
    a pair), a single output bit on a '595. A shift-out bit gets the much shorter
    switch-coil pulse instead of `hold_s`, because it may be wired to a solenoid
    point motor, which burns out if its coil is held energized."""
    if kind == "hc595":
        chip.set_output(line, True)
        time.sleep(SWITCH_THROW_PULSE_S)
        chip.set_output(line, False)
    elif kind == "pca9685":
        chip.configure()
        chip.set_duty_cycle(line, 0.5)       # this one channel at half power
        time.sleep(hold_s)
        chip.set_duty_cycle(line, 0.0)
    else:
        raise ValueError(f"chip kind {kind!r} has no pulse-output test")


def _drive_sequence(
    chip: Any,
    forward: int,
    reverse: int,
    volts: float = MAX_TRACK_VOLTAGE,
    hold_s: float = TRACK_TEST_HOLD_S,
) -> None:
    """Drive a block's two rails through both polarities so the user can watch a
    loco move: full voltage one way, both rails off, full voltage the other way.

    `forward` and `reverse` are the block's two bound PWM channels (its H-bridge
    inputs). A polarity is one channel at duty with the other at zero; only one is
    ever driven at a time, since both high would shoot through the bridge. The
    track is returned to zero in a `finally` so an exception mid-sequence (a
    dropped USB bridge, an interrupted request) still leaves the rails dead.
    """
    chip.configure()
    duty = min(abs(volts) / MAX_TRACK_VOLTAGE, 1.0) if MAX_TRACK_VOLTAGE else 0.0
    try:
        chip.set_duty_cycle(reverse, 0.0)      # forward rail hot, reverse at ground
        chip.set_duty_cycle(forward, duty)
        time.sleep(hold_s)
        chip.set_duty_cycle(forward, 0.0)      # both rails off
        chip.set_duty_cycle(reverse, 0.0)
        time.sleep(hold_s)
        chip.set_duty_cycle(reverse, duty)     # reverse rail hot, forward at ground
        time.sleep(hold_s)
    finally:
        chip.set_duty_cycle(forward, 0.0)
        chip.set_duty_cycle(reverse, 0.0)


def run_track_test(
    cfg: Any,
    block_id: str,
    device: Optional[Any] = None,
    open_device: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Sweep a block's track through both polarities (the block editor's track
    Test button). Reads the block's `block_track` binding, then drives its two
    bound channels forward / off / reverse so a loco on the block runs, stops,
    and reverses. Leaves the track de-energized."""
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

    try:
        chip = build_chip(device, spec)
        _drive_sequence(chip, binding.forward, binding.reverse)
        return {"ok": True, "volts": MAX_TRACK_VOLTAGE, "hold_s": TRACK_TEST_HOLD_S}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
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
    block is being driven (see `OccupancyDriver`): the digital
    detector ('165 bit) while the block is idle, the current-sense channel
    (INA3221 / INA219) while it is powered. This reads both regardless, reads the
    drive state back off the PWM chip's own registers, and applies that same rule
    — so the user sees each detector's raw state side by side and the one answer
    the runtime would take from them.

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
        deadband = _idle_deadband_v()
        track = _read_track_state(device, cfg, block_id)
        digital = _read_digital_detector(device, cfg, block_id)
        current = _read_current_detector(device, cfg, block_id)

        driven = abs(track["volts"]) > deadband if track["bound"] else False
        # The same precedence the runtime interface uses.
        if driven and current["bound"]:
            source = "current"
        elif digital["bound"]:
            source = "digital"
        elif current["bound"]:
            source = "current"
        else:
            source = "none"

        decided = {"digital": digital, "current": current}.get(source)
        occupied = bool(decided and decided.get("occupied"))
        return {
            "ok": True,
            "block_id": block_id,
            "track": track,
            "driven": driven,
            "deadband_v": deadband,
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


def _idle_deadband_v() -> float:
    """The drive-state deadband the runtime uses (imported here rather than at
    module scope so config keeps depending on hardware, not the reverse)."""
    from hardware_config import DEFAULT_IDLE_DEADBAND_V

    return DEFAULT_IDLE_DEADBAND_V


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
    """The block's '165 detector bit. A set bit means current present."""
    binding = cfg.block_occupancy.get(block_id)
    if binding is None:
        return {"bound": False}
    spec = _spec_for(cfg, binding.chip_id)
    result: Dict[str, Any] = {
        "bound": True, "chip_id": binding.chip_id, "port": binding.port,
    }
    if spec is None:
        result["error"] = f"unknown chip {binding.chip_id!r}"
        return result
    try:
        chip = build_chip(device, spec)
        value = bool(chip.read_bit(binding.port))
        result["value"] = value
        result["occupied"] = value
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _read_current_detector(device: Any, cfg: Any, block_id: str) -> Dict[str, Any]:
    """The block's current-sense channel, with the calibration it is judged
    against: occupied when `abs(current - baseline)` reaches the threshold."""
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
