"""Hardware configuration — the chip inventory, port bindings, and the device a
real session drives (design_new.md "Power -> Hardware configuration").

This store answers three questions for the next operation session:

- **What is wired?** A `controller` (the transport bridge) plus a list of `chips`
  (each a PWM driver, shift-out, or shift-in), every chip carrying the parameters
  the user set: an I2C address, or an SPI select line + cascade length.
- **What connects to what?** The `bindings` — three maps from a logical id to a
  chip port: `block_track` (block_id -> track port), `block_occupancy` (block_id
  -> input port), `switch_output` (switch_id -> throw port). This is the logical
  id -> physical channel map; the concrete driver in `hardware/` is the only
  consumer of it (design_new.md "Power", Hardware interface boundary).
- **Is it live?** `enabled` gates real mode. A session runs real when `enabled`
  is set and at least one chip is configured; each interface (track / switch /
  occupancy) resolves real only when a chip of its role exists, else simulated.

Like the previous store it differs from `runtime_config.py`: hardware
configuration is read only when a session is built (`bootstrap.py`), so a change
reaches the next session, not the running one, and it persists to an app-managed
JSON file (`CONFIG_PATH`) written only through the `PUT /api/hardware/config`
API so the selection survives the restart that applies it.
"""

import atexit
from dataclasses import dataclass, field, fields
import json
from typing import Any, Dict, List, Optional, Tuple

from enums import OperationMode
from hardware.catalog import (
    CHIP_CATALOG,
    CONTROLLER_CATALOG,
    ROLE_OCCUPANCY,
    ROLE_SWITCH,
    ROLE_TRACK,
    SPI_SELECT_LINES,
    ports_for_chip,
)

# On-disk store — an app-managed sibling to the layout save files, written only
# through the API. Tests point this at a temp path.
CONFIG_PATH = "hardware_config.json"

# I2C 7-bit address range for a peripheral (0x00-0x02 and 0x78-0x7F reserved).
_I2C_MIN, _I2C_MAX = 0x03, 0x77


class HardwareConfigError(ValueError):
    """A rejected hardware-configuration update.

    Raised for an unknown controller/chip kind, a missing or out-of-range chip
    parameter, or a binding that references an unknown chip / port / role. The
    route layer turns this into an HTTP 400 and writes nothing.
    """


class HardwareBackendUnavailable(RuntimeError):
    """Real mode was selected but its driver could not be brought up.

    Raised when the `hardware` driver package / `ch347api` is missing, the device
    cannot be opened (absent or busy), or a configured chip fails to initialize.
    `bootstrap.py` catches it and falls back to sim mode so the server still boots.
    """


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class ControllerConfig:
    """The transport bridge a real session talks through."""

    kind: str = "ch347"
    device_port: Optional[str] = None   # ch347 opens by VID/PID when None


@dataclass
class ChipConfig:
    """One configured chip. `role` and `bus` are derived from `kind` via the
    catalog, never stored, so they cannot drift."""

    chip_id: str
    kind: str
    i2c_address: Optional[int] = None   # pca9685 (user-set jumper address)
    chain_length: int = 1               # hc595/hc165 cascade count
    spi_select: Optional[str] = None    # hc595/hc165 latch/load line, by pad
    label: str = ""

    @property
    def role(self) -> Optional[str]:
        ck = CHIP_CATALOG.get(self.kind)
        return ck.role if ck else None

    @property
    def bus(self) -> Optional[str]:
        ck = CHIP_CATALOG.get(self.kind)
        return ck.bus if ck else None


@dataclass
class PortBinding:
    """A logical id's physical channel: which chip, which port on it."""

    chip_id: str
    port: int


@dataclass
class HardwareConfig:
    """The whole selection. Defaults are the all-virtual sim-mode configuration."""

    enabled: bool = False
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    chips: List[ChipConfig] = field(default_factory=list)
    block_track: Dict[str, PortBinding] = field(default_factory=dict)
    block_occupancy: Dict[str, PortBinding] = field(default_factory=dict)
    switch_output: Dict[str, PortBinding] = field(default_factory=dict)


_config = HardwareConfig()


def get_hardware_config() -> HardwareConfig:
    """Return the live hardware-configuration singleton."""
    return _config


def is_real(cfg: Optional[HardwareConfig] = None) -> bool:
    """True when the session should wire real hardware: enabled with >=1 chip."""
    cfg = cfg if cfg is not None else _config
    return bool(cfg.enabled and cfg.chips)


def derive_operation_mode(cfg: Optional[HardwareConfig] = None) -> OperationMode:
    """Map the configuration to its `OperationMode` (design_new.md "Run modes")."""
    return OperationMode.REAL if is_real(cfg) else OperationMode.SIM


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _binding_dict(m: Dict[str, PortBinding]) -> Dict[str, Dict[str, Any]]:
    return {lid: {"chip_id": b.chip_id, "port": b.port} for lid, b in m.items()}


def _chip_persist(c: ChipConfig) -> Dict[str, Any]:
    return {
        "chip_id": c.chip_id,
        "kind": c.kind,
        "i2c_address": c.i2c_address,
        "chain_length": c.chain_length,
        "spi_select": c.spi_select,
        "label": c.label,
    }


def _persist_dict(cfg: HardwareConfig) -> Dict[str, Any]:
    """Minimal on-disk form — only user-set fields (no derived data)."""
    return {
        "enabled": cfg.enabled,
        "controller": {"kind": cfg.controller.kind, "device_port": cfg.controller.device_port},
        "chips": [_chip_persist(c) for c in cfg.chips],
        "block_track": _binding_dict(cfg.block_track),
        "block_occupancy": _binding_dict(cfg.block_occupancy),
        "switch_output": _binding_dict(cfg.switch_output),
    }


def as_dict() -> Dict[str, Any]:
    """API payload for `GET /api/hardware/config` — the persisted form enriched
    with each chip's derived role / bus / port_count for the frontend."""
    payload = _persist_dict(_config)
    for chip_payload, chip in zip(payload["chips"], _config.chips):
        chip_payload["role"] = chip.role
        chip_payload["bus"] = chip.bus
        chip_payload["port_count"] = (
            ports_for_chip(chip.kind, chip.chain_length) if chip.kind in CHIP_CATALOG else 0
        )
    return payload


# ---------------------------------------------------------------------------
# Parsing / coercion
# ---------------------------------------------------------------------------
def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> Optional[int]:
    """Accept an int, a decimal string, or a hex string ('0x40')."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise HardwareConfigError(f"expected an integer, got {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError as exc:
        raise HardwareConfigError(f"expected an integer, got {value!r}") from exc


def _parse_controller(data: Any) -> ControllerConfig:
    data = data if isinstance(data, dict) else {}
    return ControllerConfig(
        kind=str(data.get("kind", "ch347")).strip() or "ch347",
        device_port=_opt_str(data.get("device_port")),
    )


def _parse_chip(data: Any) -> ChipConfig:
    data = data if isinstance(data, dict) else {}
    return ChipConfig(
        chip_id=str(data.get("chip_id", "")).strip(),
        kind=str(data.get("kind", "")).strip(),
        i2c_address=_opt_int(data.get("i2c_address")),
        chain_length=int(data.get("chain_length", 1) or 1),
        spi_select=_opt_str(data.get("spi_select")),
        label=str(data.get("label", "") or ""),
    )


def _parse_bindings(data: Any) -> Dict[str, PortBinding]:
    data = data if isinstance(data, dict) else {}
    out: Dict[str, PortBinding] = {}
    for logical_id, raw in data.items():
        raw = raw if isinstance(raw, dict) else {}
        chip_id = str(raw.get("chip_id", "")).strip()
        try:
            port = int(raw.get("port"))
        except (TypeError, ValueError) as exc:
            raise HardwareConfigError(
                f"binding {logical_id!r} needs an integer port"
            ) from exc
        out[str(logical_id)] = PortBinding(chip_id=chip_id, port=port)
    return out


def _merged_from_updates(updates: Dict[str, Any]) -> HardwareConfig:
    """Build the candidate config: each top-level key present in `updates`
    replaces that section; absent keys keep the current singleton's value."""
    merged = HardwareConfig(
        enabled=_config.enabled,
        controller=ControllerConfig(**vars(_config.controller)),
        chips=list(_config.chips),
        block_track=dict(_config.block_track),
        block_occupancy=dict(_config.block_occupancy),
        switch_output=dict(_config.switch_output),
    )
    if "enabled" in updates:
        merged.enabled = bool(updates["enabled"])
    if "controller" in updates:
        merged.controller = _parse_controller(updates["controller"])
    if "chips" in updates:
        merged.chips = [_parse_chip(c) for c in (updates["chips"] or [])]
    if "block_track" in updates:
        merged.block_track = _parse_bindings(updates["block_track"])
    if "block_occupancy" in updates:
        merged.block_occupancy = _parse_bindings(updates["block_occupancy"])
    if "switch_output" in updates:
        merged.switch_output = _parse_bindings(updates["switch_output"])
    return merged


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate(cfg: HardwareConfig) -> None:
    """Raise `HardwareConfigError` if `cfg` is not a valid v1 configuration."""
    if cfg.controller.kind not in CONTROLLER_CATALOG:
        raise HardwareConfigError(
            f"controller kind must be one of {list(CONTROLLER_CATALOG)}, "
            f"got {cfg.controller.kind!r}"
        )

    seen_ids: set = set()
    for c in cfg.chips:
        if not c.chip_id:
            raise HardwareConfigError("every chip needs a chip_id")
        if c.chip_id in seen_ids:
            raise HardwareConfigError(f"duplicate chip_id {c.chip_id!r}")
        seen_ids.add(c.chip_id)

        ck = CHIP_CATALOG.get(c.kind)
        if ck is None:
            raise HardwareConfigError(
                f"chip {c.chip_id!r}: kind must be one of {list(CHIP_CATALOG)}, "
                f"got {c.kind!r}"
            )
        if ck.needs_i2c_address:
            if c.i2c_address is None:
                raise HardwareConfigError(f"chip {c.chip_id!r}: {c.kind} needs an i2c_address")
            if not (_I2C_MIN <= c.i2c_address <= _I2C_MAX):
                raise HardwareConfigError(
                    f"chip {c.chip_id!r}: i2c_address {hex(c.i2c_address)} out of "
                    f"range {hex(_I2C_MIN)}..{hex(_I2C_MAX)}"
                )
        if ck.needs_spi_select:
            if c.spi_select is None:
                raise HardwareConfigError(f"chip {c.chip_id!r}: {c.kind} needs a spi_select line")
            if c.spi_select not in SPI_SELECT_LINES:
                raise HardwareConfigError(
                    f"chip {c.chip_id!r}: spi_select must be one of {SPI_SELECT_LINES}, "
                    f"got {c.spi_select!r}"
                )
        if c.chain_length < 1:
            raise HardwareConfigError(f"chip {c.chip_id!r}: chain_length must be >= 1")
        if not ck.cascadable and c.chain_length != 1:
            raise HardwareConfigError(
                f"chip {c.chip_id!r}: {c.kind} is not cascadable (chain_length must be 1)"
            )

    chips_by_id = {c.chip_id: c for c in cfg.chips}
    _validate_bindings(cfg.block_track, ROLE_TRACK, chips_by_id, "block_track")
    _validate_bindings(cfg.switch_output, ROLE_SWITCH, chips_by_id, "switch_output")
    _validate_bindings(cfg.block_occupancy, ROLE_OCCUPANCY, chips_by_id, "block_occupancy")


def _validate_bindings(
    m: Dict[str, PortBinding],
    role: str,
    chips_by_id: Dict[str, ChipConfig],
    name: str,
) -> None:
    used: set = set()
    for logical_id, b in m.items():
        chip = chips_by_id.get(b.chip_id)
        if chip is None:
            raise HardwareConfigError(
                f"{name}[{logical_id!r}] references unknown chip {b.chip_id!r}"
            )
        ck = CHIP_CATALOG[chip.kind]
        if ck.role != role:
            raise HardwareConfigError(
                f"{name}[{logical_id!r}] chip {b.chip_id!r} is {ck.role}, needs {role}"
            )
        count = ports_for_chip(chip.kind, chip.chain_length)
        if not (0 <= b.port < count):
            raise HardwareConfigError(
                f"{name}[{logical_id!r}] port {b.port} out of range 0..{count - 1} "
                f"on chip {b.chip_id!r}"
            )
        key = (b.chip_id, b.port)
        if key in used:
            raise HardwareConfigError(
                f"{name}: port {b.port} on chip {b.chip_id!r} is bound more than once"
            )
        used.add(key)


# ---------------------------------------------------------------------------
# Commit / persistence
# ---------------------------------------------------------------------------
def _apply(cfg: HardwareConfig) -> HardwareConfig:
    """Commit a validated config onto the singleton in place (preserves identity)."""
    for f in fields(HardwareConfig):
        setattr(_config, f.name, getattr(cfg, f.name))
    return _config


def apply_updates(updates: Dict[str, Any], *, persist: bool = True) -> HardwareConfig:
    """Apply a partial section->value dict, validate the whole, and (by default)
    write it to disk. On any violation nothing is committed and
    `HardwareConfigError` is raised, so a rejected request leaves the singleton
    and the file untouched."""
    merged = _merged_from_updates(updates or {})
    _validate(merged)   # raises before any commit
    _apply(merged)
    if persist:
        save_to_disk()
    return _config


BINDING_MAPS = ("block_track", "block_occupancy", "switch_output")


def _copy_config() -> HardwareConfig:
    """A shallow working copy of the singleton (chips + binding objects shared,
    the containers fresh) for validate-before-commit edits."""
    return HardwareConfig(
        enabled=_config.enabled,
        controller=ControllerConfig(**vars(_config.controller)),
        chips=list(_config.chips),
        block_track=dict(_config.block_track),
        block_occupancy=dict(_config.block_occupancy),
        switch_output=dict(_config.switch_output),
    )


def set_binding(
    map_name: str,
    logical_id: str,
    chip_id: Optional[str] = None,
    port: Optional[int] = None,
    *,
    persist: bool = True,
) -> HardwareConfig:
    """Set or clear a single port binding (used by the per-entity block/switch
    editors). `chip_id` None/empty clears the binding. The whole configuration is
    re-validated, so an out-of-range / role-mismatched / already-used port is
    rejected and nothing is committed."""
    if map_name not in BINDING_MAPS:
        raise HardwareConfigError(f"binding map must be one of {list(BINDING_MAPS)}, got {map_name!r}")
    candidate = _copy_config()
    target = getattr(candidate, map_name)
    if chip_id in (None, ""):
        target.pop(logical_id, None)
    else:
        if port is None:
            raise HardwareConfigError("port is required when chip_id is set")
        target[logical_id] = PortBinding(chip_id=str(chip_id), port=int(port))
    _validate(candidate)
    _apply(candidate)
    if persist:
        save_to_disk()
    return _config


def config_from_payload(payload: Dict[str, Any]) -> HardwareConfig:
    """Build and validate a standalone `HardwareConfig` from a full payload
    (no merge with the singleton). Used by the self-test / port-test endpoints
    to exercise the config a user is editing before it is saved."""
    payload = payload if isinstance(payload, dict) else {}
    cfg = HardwareConfig(
        enabled=bool(payload.get("enabled", False)),
        controller=_parse_controller(payload.get("controller", {})),
        chips=[_parse_chip(c) for c in (payload.get("chips") or [])],
        block_track=_parse_bindings(payload.get("block_track", {})),
        block_occupancy=_parse_bindings(payload.get("block_occupancy", {})),
        switch_output=_parse_bindings(payload.get("switch_output", {})),
    )
    _validate(cfg)
    return cfg


def save_to_disk(path: Optional[str] = None) -> None:
    """Write the current configuration to the app-managed JSON file."""
    target = path if path is not None else CONFIG_PATH
    with open(target, "w") as fh:
        json.dump(_persist_dict(_config), fh, indent=2)


def load_from_disk(path: Optional[str] = None) -> HardwareConfig:
    """Load the configuration from disk into the singleton, if a valid file
    exists (called once at session build). Fail-safe: a missing, unreadable,
    malformed, or invalid file leaves the singleton at its all-virtual defaults,
    so a corrupt file never boots the runtime into a broken real mode."""
    target = path if path is not None else CONFIG_PATH
    try:
        with open(target) as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _config
    if not isinstance(data, dict):
        return _config
    try:
        candidate = _merged_from_updates(data)
        _validate(candidate)
    except HardwareConfigError:
        return _config
    return _apply(candidate)


def reset_to_defaults() -> HardwareConfig:
    """Restore the all-virtual defaults on the singleton (used by tests). Does
    not touch the on-disk file."""
    global _config
    _config = HardwareConfig()
    return _config


# ---------------------------------------------------------------------------
# Session interface construction
# ---------------------------------------------------------------------------
def _binding_tuples(m: Dict[str, PortBinding]) -> Dict[str, Tuple[str, int]]:
    return {lid: (b.chip_id, b.port) for lid, b in m.items()}


def build_session_interfaces(
    cfg: Optional[HardwareConfig] = None,
) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Resolve the configuration to the session's interface overrides:
    `(input_interface, block_output_interface, switch_output_interface)`.

    All-`None` in sim mode (the `Simulated*` defaults are constructed by
    `SimulationController`). In real mode, opens the device and wires the real
    interfaces for every role that has a chip; a role without a chip stays
    `None` (simulated). Any failure to bring up the driver, open the device, or
    initialize a chip raises `HardwareBackendUnavailable`, which `bootstrap.py`
    turns into a sim-mode fallback.
    """
    cfg = cfg if cfg is not None else _config
    if not is_real(cfg):
        return None, None, None

    try:
        from hardware.ch347_device import open_ch347_device
        from hardware.interfaces import build_real_interfaces
    except ImportError as exc:
        raise HardwareBackendUnavailable(
            "real mode needs the hardware driver package (ch347api), which is "
            "not available in this build"
        ) from exc

    try:
        device = open_ch347_device(cfg.controller.device_port)
    except Exception as exc:
        raise HardwareBackendUnavailable(
            f"could not open {cfg.controller.kind} device: {exc}"
        ) from exc

    try:
        interfaces = build_real_interfaces(
            device,
            cfg.chips,
            _binding_tuples(cfg.block_track),
            _binding_tuples(cfg.block_occupancy),
            _binding_tuples(cfg.switch_output),
        )
    except Exception as exc:
        try:
            device.close()
        except Exception:
            pass
        raise HardwareBackendUnavailable(
            f"failed to initialize hardware chips: {exc}"
        ) from exc

    # The device holds a USB handle for the life of the session; release it at
    # process exit, after the safe-state zeroing that other atexit hooks issue
    # (they are registered later, so they run first under atexit's LIFO order).
    atexit.register(device.close)
    return interfaces
