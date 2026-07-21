"""Hardware configuration — the chip inventory, port bindings, and the device a
real session drives (design_new.md "Power -> Hardware configuration").

This store answers three questions for the next operation session:

- **What is wired?** A `controller` (the transport bridge) plus a list of `chips`
  (each a PWM driver, shift-out, or shift-in), every chip carrying the parameters
  the user set: an I2C address, or an SPI select line + cascade length.
- **What connects to what?** The `bindings` — maps from a logical id to a chip
  port: `block_track` (block_id -> track port), `switch_output` (switch_id -> the
  two coil ports plus an optional standby port), and two occupancy sources for
  the hybrid detector — `block_occupancy` (block_id -> '165 digital-detector port) and
  `block_occupancy_current` (block_id -> INA3221 current channel, with its
  per-channel baseline/threshold). `output_enable` optionally names the '595 bit
  wired to each PCA's /OE. This is the logical id -> physical channel map; the
  concrete driver in `hardware/` is the only consumer of it (design_new.md
  "Power", Hardware interface boundary).
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
    BUS_I2C,
    CHIP_CATALOG,
    CONTROLLER_CATALOG,
    PORT_CURRENT_SENSE,
    PORT_DIGITAL_IN,
    ROLE_OCCUPANCY,
    ROLE_SWITCH,
    ROLE_TRACK,
    latch_lines_for,
    ports_for_chip,
)

# On-disk store — an app-managed sibling to the layout save files, written only
# through the API. Tests point this at a temp path.
CONFIG_PATH = "hardware_config.json"

# I2C 7-bit address range for a peripheral (0x00-0x02 and 0x78-0x7F reserved).
_I2C_MIN, _I2C_MAX = 0x03, 0x77

# Hybrid current-sense occupancy defaults (design_new.md Power, Block occupancy
# detection). A block powered above the deadband is read by its INA3221 current
# channel; abs(current - baseline) at/above the threshold marks it occupied. A
# per-channel `CurrentBinding` may override the threshold/baseline; None on a
# field means "use the default below".
DEFAULT_CURRENT_THRESHOLD_MA = 15.0
DEFAULT_CURRENT_BASELINE_MA = 0.0
DEFAULT_IDLE_DEADBAND_V = 0.25


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
    """One configured chip. `bus` is stored (a shift register can be wired on SPI
    or GPIO), defaulting to the kind's first allowed bus; `role` is derived from
    `kind` via the catalog, never stored, so it cannot drift."""

    chip_id: str
    kind: str
    bus: Optional[str] = None            # i2c/spi/gpio; None -> kind's first allowed bus
    i2c_address: Optional[int] = None   # i2c bus (user-set jumper address)
    chain_length: int = 1               # hc595/hc165 cascade count
    spi_select: Optional[str] = None    # spi/gpio latch/load line, by pad
    label: str = ""

    @property
    def role(self) -> Optional[str]:
        ck = CHIP_CATALOG.get(self.kind)
        return ck.role if ck else None


@dataclass
class PortBinding:
    """A logical id's physical channel: which chip, which port on it."""

    chip_id: str
    port: int


@dataclass
class SwitchBinding:
    """A switch's throw wiring: one output bit per coil, on one chip.

    A solenoid point motor has two coils — energize one to throw toward MAIN,
    the other toward DIVERGE — so a switch takes two bits, not a level on one.
    `port_standby` is the optional bit wired to a motor driver's standby/enable
    pin (a TB6612 needs it high before either coil does anything); leave it None
    when the driver is always enabled in hardware.
    """

    chip_id: str
    port_main: int
    port_diverge: int
    port_standby: Optional[int] = None

    @property
    def ports(self) -> List[int]:
        """Every port this switch occupies, standby included."""
        used = [self.port_main, self.port_diverge]
        if self.port_standby is not None:
            used.append(self.port_standby)
        return used


@dataclass
class CurrentBinding:
    """A block's INA3221 current-sense channel plus its per-channel calibration.

    `baseline_ma` / `threshold_ma` None means "use the module default" — the
    interface resolves them so a user only sets what differs from the norm."""

    chip_id: str
    port: int
    baseline_ma: Optional[float] = None
    threshold_ma: Optional[float] = None


@dataclass
class HardwareConfig:
    """The whole selection. Defaults are the all-virtual sim-mode configuration."""

    enabled: bool = False
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    chips: List[ChipConfig] = field(default_factory=list)
    block_track: Dict[str, PortBinding] = field(default_factory=dict)
    # Two occupancy sources per the hybrid detector: `block_occupancy` is the
    # digital detector ('165, read when a block is idle) and
    # `block_occupancy_current` is the INA3221 channel (read when the block is
    # powered). A block may appear in either or both.
    block_occupancy: Dict[str, PortBinding] = field(default_factory=dict)
    block_occupancy_current: Dict[str, CurrentBinding] = field(default_factory=dict)
    switch_output: Dict[str, SwitchBinding] = field(default_factory=dict)
    # Optional per-PCA output-enable line: key = track (pca9685) chip_id, value =
    # the '595 bit wired to that PCA's /OE. Driven to enable once at session
    # build and held; there is no per-tick coast window. Empty when /OE is tied
    # enabled in hardware.
    output_enable: Dict[str, PortBinding] = field(default_factory=dict)


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


def _switch_binding_dict(m: Dict[str, SwitchBinding]) -> Dict[str, Dict[str, Any]]:
    return {
        lid: {
            "chip_id": b.chip_id,
            "port_main": b.port_main,
            "port_diverge": b.port_diverge,
            "port_standby": b.port_standby,
        }
        for lid, b in m.items()
    }


def _current_binding_dict(m: Dict[str, CurrentBinding]) -> Dict[str, Dict[str, Any]]:
    return {
        lid: {
            "chip_id": b.chip_id,
            "port": b.port,
            "baseline_ma": b.baseline_ma,
            "threshold_ma": b.threshold_ma,
        }
        for lid, b in m.items()
    }


def _chip_persist(c: ChipConfig) -> Dict[str, Any]:
    return {
        "chip_id": c.chip_id,
        "kind": c.kind,
        "bus": c.bus,
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
        "block_occupancy_current": _current_binding_dict(cfg.block_occupancy_current),
        "switch_output": _switch_binding_dict(cfg.switch_output),
        "output_enable": _binding_dict(cfg.output_enable),
    }


def as_dict() -> Dict[str, Any]:
    """API payload for `GET /api/hardware/config` — the persisted form (which
    already carries each chip's stored `bus`) enriched with each chip's derived
    role / direction / port_count / port_kind for the frontend."""
    payload = _persist_dict(_config)
    for chip_payload, chip in zip(payload["chips"], _config.chips):
        ck = CHIP_CATALOG.get(chip.kind)
        chip_payload["role"] = chip.role
        chip_payload["direction"] = ck.direction if ck else None
        chip_payload["port_count"] = ports_for_chip(chip.kind, chip.chain_length) if ck else 0
        chip_payload["port_kind"] = ck.port_kind if ck else None
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
        bus=_opt_str(data.get("bus")),
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


def _req_port(raw: Dict[str, Any], key: str, logical_id: str, what: str) -> int:
    """Read a required integer port field off a binding payload."""
    try:
        return int(raw.get(key))
    except (TypeError, ValueError) as exc:
        raise HardwareConfigError(
            f"{what} {logical_id!r} needs an integer {key}"
        ) from exc


def _parse_switch_bindings(data: Any) -> Dict[str, SwitchBinding]:
    data = data if isinstance(data, dict) else {}
    out: Dict[str, SwitchBinding] = {}
    for logical_id, raw in data.items():
        raw = raw if isinstance(raw, dict) else {}
        lid = str(logical_id)
        out[lid] = SwitchBinding(
            chip_id=str(raw.get("chip_id", "")).strip(),
            port_main=_req_port(raw, "port_main", lid, "switch binding"),
            port_diverge=_req_port(raw, "port_diverge", lid, "switch binding"),
            port_standby=_opt_int(raw.get("port_standby")),
        )
    return out


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HardwareConfigError(f"expected a number, got {value!r}") from exc


def _parse_current_bindings(data: Any) -> Dict[str, CurrentBinding]:
    data = data if isinstance(data, dict) else {}
    out: Dict[str, CurrentBinding] = {}
    for logical_id, raw in data.items():
        raw = raw if isinstance(raw, dict) else {}
        chip_id = str(raw.get("chip_id", "")).strip()
        try:
            port = int(raw.get("port"))
        except (TypeError, ValueError) as exc:
            raise HardwareConfigError(
                f"current binding {logical_id!r} needs an integer port"
            ) from exc
        out[str(logical_id)] = CurrentBinding(
            chip_id=chip_id,
            port=port,
            baseline_ma=_opt_float(raw.get("baseline_ma")),
            threshold_ma=_opt_float(raw.get("threshold_ma")),
        )
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
        block_occupancy_current=dict(_config.block_occupancy_current),
        switch_output=dict(_config.switch_output),
        output_enable=dict(_config.output_enable),
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
    if "block_occupancy_current" in updates:
        merged.block_occupancy_current = _parse_current_bindings(updates["block_occupancy_current"])
    if "switch_output" in updates:
        merged.switch_output = _parse_switch_bindings(updates["switch_output"])
    if "output_enable" in updates:
        merged.output_enable = _parse_bindings(updates["output_enable"])
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
        # Resolve the bus this chip is wired on (default = the kind's first
        # allowed bus) and check the kind actually supports it.
        if c.bus is None:
            c.bus = ck.buses[0]
        if c.bus not in ck.buses:
            raise HardwareConfigError(
                f"chip {c.chip_id!r}: {c.kind} cannot be on bus {c.bus!r}; "
                f"allowed: {list(ck.buses)}"
            )

        # The coordinate a chip needs depends on its bus: an I2C chip addresses,
        # an SPI/GPIO chip latches on a line.
        if c.bus == BUS_I2C:
            if c.i2c_address is None:
                raise HardwareConfigError(f"chip {c.chip_id!r}: an i2c chip needs an i2c_address")
            if not (_I2C_MIN <= c.i2c_address <= _I2C_MAX):
                raise HardwareConfigError(
                    f"chip {c.chip_id!r}: i2c_address {hex(c.i2c_address)} out of "
                    f"range {hex(_I2C_MIN)}..{hex(_I2C_MAX)}"
                )
        else:
            lines = latch_lines_for(c.bus)
            if c.spi_select is None:
                raise HardwareConfigError(f"chip {c.chip_id!r}: a {c.bus} chip needs a latch line")
            if c.spi_select not in lines:
                raise HardwareConfigError(
                    f"chip {c.chip_id!r}: latch line must be one of {lines}, "
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
    _validate_switch_bindings(cfg.switch_output, chips_by_id)
    # The two occupancy maps share the occupancy role but split by port kind: the
    # digital detector ('165) vs the current-sense channel (INA3221).
    _validate_bindings(
        cfg.block_occupancy, ROLE_OCCUPANCY, chips_by_id, "block_occupancy",
        port_kind=PORT_DIGITAL_IN,
    )
    _validate_bindings(
        cfg.block_occupancy_current, ROLE_OCCUPANCY, chips_by_id, "block_occupancy_current",
        port_kind=PORT_CURRENT_SENSE,
    )
    for logical_id, b in cfg.block_occupancy_current.items():
        if b.threshold_ma is not None and b.threshold_ma < 0:
            raise HardwareConfigError(
                f"block_occupancy_current[{logical_id!r}] threshold_ma must be >= 0"
            )
    _validate_output_enable(cfg.output_enable, chips_by_id)


def _validate_bindings(
    m: Dict[str, Any],
    role: str,
    chips_by_id: Dict[str, ChipConfig],
    name: str,
    port_kind: Optional[str] = None,
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
        if port_kind is not None and ck.port_kind != port_kind:
            raise HardwareConfigError(
                f"{name}[{logical_id!r}] chip {b.chip_id!r} is {ck.port_kind}, needs {port_kind}"
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


def _validate_switch_bindings(
    m: Dict[str, SwitchBinding],
    chips_by_id: Dict[str, ChipConfig],
) -> None:
    """Each switch names one switch-role chip and two distinct coil bits on it,
    plus an optional distinct standby bit. Two coils sharing a bit would drive
    both directions at once, so a repeat within an entry is rejected the same way
    a repeat across entries is."""
    used: set = set()
    for logical_id, b in m.items():
        chip = chips_by_id.get(b.chip_id)
        if chip is None:
            raise HardwareConfigError(
                f"switch_output[{logical_id!r}] references unknown chip {b.chip_id!r}"
            )
        ck = CHIP_CATALOG[chip.kind]
        if ck.role != ROLE_SWITCH:
            raise HardwareConfigError(
                f"switch_output[{logical_id!r}] chip {b.chip_id!r} is {ck.role}, "
                f"needs {ROLE_SWITCH}"
            )
        count = ports_for_chip(chip.kind, chip.chain_length)
        seen_here: set = set()
        for label, port in (
            ("main", b.port_main),
            ("diverge", b.port_diverge),
            ("standby", b.port_standby),
        ):
            if port is None:
                continue
            if not (0 <= port < count):
                raise HardwareConfigError(
                    f"switch_output[{logical_id!r}] {label} port {port} out of range "
                    f"0..{count - 1} on chip {b.chip_id!r}"
                )
            if port in seen_here:
                raise HardwareConfigError(
                    f"switch_output[{logical_id!r}] uses port {port} for more than "
                    f"one of main / diverge / standby"
                )
            seen_here.add(port)
            key = (b.chip_id, port)
            if key in used:
                raise HardwareConfigError(
                    f"switch_output: port {port} on chip {b.chip_id!r} is bound more than once"
                )
            used.add(key)


def _validate_output_enable(
    m: Dict[str, PortBinding],
    chips_by_id: Dict[str, ChipConfig],
) -> None:
    """Each /OE binding keys on a track (pca9685) chip_id and points at the '595
    output bit wired to that PCA's /OE pin."""
    used: set = set()
    for track_chip_id, b in m.items():
        target = chips_by_id.get(track_chip_id)
        if target is None:
            raise HardwareConfigError(
                f"output_enable key {track_chip_id!r} is not a configured chip"
            )
        if CHIP_CATALOG[target.kind].role != ROLE_TRACK:
            raise HardwareConfigError(
                f"output_enable key {track_chip_id!r} must be a track chip, "
                f"is {CHIP_CATALOG[target.kind].role}"
            )
        drive = chips_by_id.get(b.chip_id)
        if drive is None:
            raise HardwareConfigError(
                f"output_enable[{track_chip_id!r}] references unknown chip {b.chip_id!r}"
            )
        if CHIP_CATALOG[drive.kind].role != ROLE_SWITCH:
            raise HardwareConfigError(
                f"output_enable[{track_chip_id!r}] chip {b.chip_id!r} is "
                f"{CHIP_CATALOG[drive.kind].role}, needs an {ROLE_SWITCH} bit for /OE"
            )
        count = ports_for_chip(drive.kind, drive.chain_length)
        if not (0 <= b.port < count):
            raise HardwareConfigError(
                f"output_enable[{track_chip_id!r}] port {b.port} out of range "
                f"0..{count - 1} on chip {b.chip_id!r}"
            )
        key = (b.chip_id, b.port)
        if key in used:
            raise HardwareConfigError(
                f"output_enable: port {b.port} on chip {b.chip_id!r} is bound more than once"
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


def _prune_dangling_bindings(cfg: HardwareConfig) -> None:
    """Drop binding entries that reference a chip no longer configured. The
    Hardware Setup screen saves only the chip list — port bindings are edited in
    the per-entity block/switch panels — so removing a chip there must not leave
    a binding pointing at a chip that is gone. Such a binding is dropped, not a
    fatal validation error."""
    ids = {c.chip_id for c in cfg.chips}
    for map_name in ("block_track", "block_occupancy", "block_occupancy_current", "switch_output"):
        m = getattr(cfg, map_name)
        for lid in [k for k, b in m.items() if b.chip_id not in ids]:
            del m[lid]
    # output_enable is keyed by the track chip and points at the switch chip that
    # drives its /OE; drop the entry if either chip is gone.
    oe = cfg.output_enable
    for track_id in [k for k, b in oe.items() if k not in ids or b.chip_id not in ids]:
        del oe[track_id]


def apply_updates(updates: Dict[str, Any], *, persist: bool = True) -> HardwareConfig:
    """Apply a partial section->value dict, validate the whole, and (by default)
    write it to disk. On any violation nothing is committed and
    `HardwareConfigError` is raised, so a rejected request leaves the singleton
    and the file untouched."""
    merged = _merged_from_updates(updates or {})
    _prune_dangling_bindings(merged)   # a chip removed on the setup screen drops its bindings
    _validate(merged)   # raises before any commit
    _apply(merged)
    if persist:
        save_to_disk()
    return _config


# The maps a plain (chip_id, port) binding covers. `switch_output` and
# `block_occupancy_current` carry more than one field per entry, so each has its
# own setter below.
BINDING_MAPS = ("block_track", "block_occupancy")


def _copy_config() -> HardwareConfig:
    """A shallow working copy of the singleton (chips + binding objects shared,
    the containers fresh) for validate-before-commit edits."""
    return HardwareConfig(
        enabled=_config.enabled,
        controller=ControllerConfig(**vars(_config.controller)),
        chips=list(_config.chips),
        block_track=dict(_config.block_track),
        block_occupancy=dict(_config.block_occupancy),
        block_occupancy_current=dict(_config.block_occupancy_current),
        switch_output=dict(_config.switch_output),
        output_enable=dict(_config.output_enable),
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


def set_switch_binding(
    switch_id: str,
    chip_id: Optional[str] = None,
    port_main: Optional[int] = None,
    port_diverge: Optional[int] = None,
    *,
    port_standby: Any = None,
    persist: bool = True,
) -> HardwareConfig:
    """Set or clear one switch's throw wiring (the per-entity switch editor's
    Hardware section). `chip_id` None/empty clears it; otherwise both coil ports
    are required and `port_standby` None means the driver needs no standby bit.
    The whole configuration is re-validated, so an out-of-range, role-mismatched,
    repeated, or already-used port is rejected and nothing is committed."""
    candidate = _copy_config()
    target = candidate.switch_output
    if chip_id in (None, ""):
        target.pop(switch_id, None)
    else:
        if port_main is None or port_diverge is None:
            raise HardwareConfigError(
                "port_main and port_diverge are both required when chip_id is set"
            )
        target[switch_id] = SwitchBinding(
            chip_id=str(chip_id),
            port_main=int(port_main),
            port_diverge=int(port_diverge),
            port_standby=_opt_int(port_standby),
        )
    _validate(candidate)
    _apply(candidate)
    if persist:
        save_to_disk()
    return _config


def set_current_binding(
    block_id: str,
    chip_id: Optional[str] = None,
    port: Optional[int] = None,
    *,
    baseline_ma: Any = None,
    threshold_ma: Any = None,
    persist: bool = True,
) -> HardwareConfig:
    """Set or clear one block's INA3221 current binding (the per-entity block
    editor's current-sense row). `chip_id` None/empty clears it. `baseline_ma` /
    `threshold_ma` None keep the module defaults. The whole configuration is
    re-validated, so an out-of-range / wrong-port-kind / already-used channel is
    rejected and nothing is committed."""
    candidate = _copy_config()
    target = candidate.block_occupancy_current
    if chip_id in (None, ""):
        target.pop(block_id, None)
    else:
        if port is None:
            raise HardwareConfigError("port is required when chip_id is set")
        target[block_id] = CurrentBinding(
            chip_id=str(chip_id),
            port=int(port),
            baseline_ma=_opt_float(baseline_ma),
            threshold_ma=_opt_float(threshold_ma),
        )
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
        block_occupancy_current=_parse_current_bindings(payload.get("block_occupancy_current", {})),
        switch_output=_parse_switch_bindings(payload.get("switch_output", {})),
        output_enable=_parse_bindings(payload.get("output_enable", {})),
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


def _switch_binding_tuples(
    m: Dict[str, SwitchBinding],
) -> Dict[str, Tuple[str, int, int, Optional[int]]]:
    """Resolve each switch to (chip_id, port_main, port_diverge, port_standby)
    for the driver, which addresses coils by port number only."""
    return {
        lid: (b.chip_id, b.port_main, b.port_diverge, b.port_standby)
        for lid, b in m.items()
    }


def _current_binding_tuples(
    m: Dict[str, CurrentBinding],
) -> Dict[str, Tuple[str, int, float, float]]:
    """Resolve each current binding to (chip_id, port, baseline_a, threshold_a),
    filling per-channel None with the module defaults and converting mA -> A so
    the interface compares against raw INA3221 amps."""
    out: Dict[str, Tuple[str, int, float, float]] = {}
    for lid, b in m.items():
        baseline_ma = b.baseline_ma if b.baseline_ma is not None else DEFAULT_CURRENT_BASELINE_MA
        threshold_ma = b.threshold_ma if b.threshold_ma is not None else DEFAULT_CURRENT_THRESHOLD_MA
        out[lid] = (b.chip_id, b.port, baseline_ma / 1000.0, threshold_ma / 1000.0)
    return out


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
        from hardware.watchdog import DEFAULT_WATCHDOG_TIMEOUT_S
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
            _switch_binding_tuples(cfg.switch_output),
            block_occupancy_current=_current_binding_tuples(cfg.block_occupancy_current),
            output_enable=_binding_tuples(cfg.output_enable),
            idle_deadband_v=DEFAULT_IDLE_DEADBAND_V,
            # A real session drives real track; arm the loss-of-control watchdog
            # (design_new.md "Power -> Hardware interface boundary").
            watchdog_timeout_s=DEFAULT_WATCHDOG_TIMEOUT_S,
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
