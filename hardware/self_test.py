"""Self-test + interactive port-test for the Hardware Setup screen.

`run_self_test` opens a transient device, scans the I2C bus, and probes each
configured chip; every reachable chip contributes its ports to the bindable
pool. `run_port_test` pulses one output port or reads one input port so the user
can confirm an individual wire before binding it.

Both accept an already-open `device` (the fake, in tests) or fall back to
opening a real `Ch347Device`. If a real session already holds the CH347, opening
fails and the result reports the device as unreachable ("busy") rather than
raising — self-test is a commissioning-time (sim-mode) activity.
"""

import time
from typing import Any, Callable, Dict, Optional

from hardware.catalog import CHIP_CATALOG, ports_for_chip
from hardware.interfaces import build_chip


def _open_real_device(cfg: Any) -> Any:
    from hardware.ch347_device import open_ch347_device

    return open_ch347_device(getattr(cfg.controller, "device_port", None))


def run_self_test(
    cfg: Any,
    device: Optional[Any] = None,
    open_device: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Probe the controller + every configured chip. Returns a JSON-able report."""
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
                "controller": {"kind": controller_kind, "reachable": False},
                "chips": [],
            }
        close_after = True

    try:
        try:
            scan = device.i2c_scan()
        except Exception:
            scan = []
        controller = {
            "kind": controller_kind,
            "reachable": True,
            "i2c_scan": [f"0x{addr:02x}" for addr in scan],
        }
        chips = [_probe_chip(device, spec) for spec in cfg.chips]
        return {"ok": True, "controller": controller, "chips": chips}
    finally:
        if close_after:
            try:
                device.close()
            except Exception:
                pass


def _probe_chip(device: Any, spec: Any) -> Dict[str, Any]:
    ck = CHIP_CATALOG[spec.kind]
    chip = build_chip(device, spec)
    reachable = bool(chip.probe())
    count = ports_for_chip(spec.kind, getattr(spec, "chain_length", 1))
    return {
        "chip_id": spec.chip_id,
        "kind": spec.kind,
        "role": ck.role,
        "bus": ck.bus,
        "label": getattr(spec, "label", "") or ck.label,
        "reachable": reachable,
        "port_count": count,
        # A reachable chip's ports are all available; write-only chips ('595)
        # cannot verify individual ports without an interactive pulse.
        "ports": [{"port": p, "available": reachable} for p in range(count)],
    }


def run_port_test(
    cfg: Any,
    chip_id: str,
    port: int,
    action: str,
    device: Optional[Any] = None,
    open_device: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Pulse one output port ('595 bit / PCA9685 pair) or read one input port
    ('165 bit) so the user can confirm the wire physically responds."""
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


def _pulse(kind: str, chip: Any, port: int, hold_s: float = 0.4) -> None:
    """Briefly drive an output port, then return it to the off state."""
    if kind == "hc595":
        chip.set_output(port, True)
        time.sleep(hold_s)
        chip.set_output(port, False)
    elif kind == "pca9685":
        chip.configure()
        chip.set_duty_cycle(2 * port, 0.5)   # forward input at half power
        time.sleep(hold_s)
        chip.set_duty_cycle(2 * port, 0.0)
    else:
        raise ValueError(f"chip kind {kind!r} has no pulse-output test")
