"""Hardware Setup — chip inventory, bindings, calibration, and safety wiring.

This store answers three questions for the next operation session:

- **What is wired?** A `controller` (the transport bridge) plus a list of `chips`
  (each a PWM driver, shift-out, or shift-in), every chip carrying the parameters
  the user set: an I2C address, or an SPI select line + cascade length.
- **What connects to what?** The `bindings` — maps from a logical id to chip
  ports: `block_track` (block_id -> the two PWM channels driving its rails,
  `forward` + `reverse`), `switch_output` (switch_id -> the two coil ports plus
  an optional standby port), and two occupancy sources for
  the hybrid detector — `block_occupancy` (block_id -> '165 digital-detector port) and
  `block_occupancy_current` (block_id -> INA3221 current channel, with its
  per-channel baseline/threshold). This is the logical id -> physical channel map; the
  concrete driver in `hardware/` is the only consumer of it (design_new.md
  "Power", Hardware interface boundary).
- **What it does not decide:** the selected operating environment. That lives in
  ``operation_environment.py`` and is persisted independently.

Like the previous store it differs from `runtime_config.py`: hardware
setup is read only when a session is built (`bootstrap.py`), so a change
reaches the next session, not the running one, and it persists to an app-managed
JSON file (`CONFIG_PATH`) written only through the `PUT /api/hardware/config`
API so the selection survives the restart that applies it.
"""

from dataclasses import dataclass, field, fields
import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from enums import OperationMode
import operation_environment
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
    lines_for_chip,
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
DEFAULT_CURRENT_THRESHOLD_MA = 3.0
DEFAULT_CURRENT_BASELINE_MA = 0.0
DEFAULT_IDLE_DEADBAND_V = 0.25
# The drive level at or above which the current channel decides occupancy on a
# single read. Between this and the idle deadband sits the lighting zone, where
# the current channel still decides — the '165 watches a rail the drive is
# pulsing — but a stopped-but-lit train may draw too little for it to read, so
# no single read may CLEAR a block there (see
# `hardware.interfaces.OccupancyDriver`). Tunable because where a locomotive
# starts drawing a readable current depends on the locomotive.
#
# It must stay ABOVE the profile's `lighting_voltage` (default 1.5 V): a
# stopped-but-lit train is by definition not being driven, and putting its
# standby level at or above this bound would hand its block to the current
# channel alone — the one source a lit, motionless locomotive is least likely
# to register on.
DEFAULT_CURRENT_SENSE_MIN_DRIVE_V = 4.0


class HardwareConfigError(ValueError):
    """A rejected hardware-configuration update.

    Raised for an unknown controller/chip kind, a missing or out-of-range chip
    parameter, or a binding that references an unknown chip / port / role. The
    route layer turns this into an HTTP 400 and writes nothing.
    """


class HardwareBackendUnavailable(RuntimeError):
    """A hardware-backed environment could not bring up its device.

    Raised when the `hardware` driver package / `ch347api` is missing, the device
    cannot be opened (absent or busy), or a configured chip fails to initialize.
    Activation handling decides how that failure is surfaced to the operator.
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
    # Detector polarity for a shift-in chip: the comparator feeding it (an LM339
    # with no inverter stage) pulls its output LOW when a train draws current, so
    # a raw bit reads 0 occupied / 1 clear. The real input interface flips it
    # below `read_block_currents()`, so nothing above the interface knows a
    # polarity exists.
    active_low: bool = False

    @property
    def role(self) -> Optional[str]:
        ck = CHIP_CATALOG.get(self.kind)
        return ck.role if ck else None


@dataclass
class PortBinding:
    """A logical id's physical channel: which chip, which port on it.

    Detector polarity is not part of a binding: a chain is wired one way
    throughout, so `ChipConfig.active_low` states it once for the whole chip."""

    chip_id: str
    port: int


@dataclass
class TrackBinding:
    """A block's track wiring: the two PWM channels driving its two rails.

    A block's track is an H-bridge whose two inputs are driven by two PCA9685
    PWM channels — `forward` drives the block's nominal-positive direction,
    `reverse` the other. They are chosen independently (not a fixed adjacent
    pair), so the two rails may sit on any two of the chip's 16 channels. Voltage
    magnitude sets duty on whichever channel matches the sign; the other rests at
    zero, so the two inputs are never driven together.

    `reversed` records that the block's two rails are wired the opposite way
    round from the nominal-positive sense its tile order implies. It is wiring
    config, so it lives with the wiring and auto-persists with every other
    binding edit. The concrete driver swaps the two channels as it resolves the
    binding when it is set; the `forward`/`reverse` channels themselves are
    never rebound, because one of those physical rails may carry the block's
    idle detection feed (design_new.md "Power", Rail polarity lives on the rail
    binding)."""

    chip_id: str
    forward: int
    reverse: int
    reversed: bool = False

    @property
    def channels(self) -> List[int]:
        return [self.forward, self.reverse]


@dataclass
class SwitchBinding:
    """A switch's throw wiring: one output bit per coil, on one chip.

    A solenoid point motor has two coils — energize one to throw toward MAIN,
    the other toward DIVERGE — so a switch takes two bits, not a level on one.
    `port_standby` is the optional bit wired to a motor driver's standby/enable
    pin (a TB6612 needs it high before either coil does anything); leave it None
    when the driver is always enabled in hardware. Unlike the coil bits it is not
    exclusive to this switch — one TB6612 puts both of its motor channels behind
    a single STBY pin, so both switches on it name the same standby bit.
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
    """The rig description only; operating-environment selection lives elsewhere."""
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    chips: List[ChipConfig] = field(default_factory=list)
    block_track: Dict[str, TrackBinding] = field(default_factory=dict)
    # Two occupancy sources per the hybrid detector: `block_occupancy` is the
    # digital detector ('165, read when a block is idle) and
    # `block_occupancy_current` is the INA3221 channel (read when the block is
    # powered). A block may appear in either or both.
    block_occupancy: Dict[str, PortBinding] = field(default_factory=dict)
    block_occupancy_current: Dict[str, CurrentBinding] = field(default_factory=dict)
    switch_output: Dict[str, SwitchBinding] = field(default_factory=dict)
    # switch_id -> block_id whose feed powers that switch's rails. The one
    # binding whose value is another logical id rather than a chip port: a
    # switch has no track channel of its own, so its rails are wired to the
    # block at its main entrance and carry that block's `track voltage`
    # (design_new.md "Power", Switch power-controller role).
    # `SwitchPowerController` is its only reader.
    switch_track_source: Dict[str, str] = field(default_factory=dict)
    # Optional per-PCA output-enable line: key = track (pca9685) chip_id, value =
    # Lower edge of the hybrid occupancy detector's drive zones: at or below this
    # magnitude a block is idle and its '165 detector answers. Tunable because
    # how small a commanded voltage still counts as parked is a property of the
    # layout's drivers and detectors, not a constant. `None` means the module
    # default.
    idle_deadband_v: float = DEFAULT_IDLE_DEADBAND_V
    # Upper edge of the same scale: at or above this magnitude the current
    # channel decides on one read. Between the two is the lighting zone, where
    # it still decides but no single read may CLEAR a block (see
    # `hardware.interfaces.OccupancyDriver`).
    current_sense_min_drive_v: float = DEFAULT_CURRENT_SENSE_MIN_DRIVE_V


# A deadband at or above this would swallow ordinary crawl voltages and leave
# every block reading idle, so the config layer refuses it.
MAX_IDLE_DEADBAND_V = 5.0
# Ceiling for the current-sense drive floor: a floor above the maximum track
# voltage would put every block in the lighting zone, where nothing ever clears.
MAX_TRACK_VOLTAGE = 12.0

_config = HardwareConfig()

# The Virtual Hardware session's FakeCh347Device. None in Simulation and Real
# Hardware. The `/api/debug/hw` endpoints poke this device's
# input state on the live session.
_session_device: Optional[Any] = None

# Whichever device the live session opened — virtual or physical; None in Simulation.
# The commissioning tools borrow this rather than opening a second handle: the
# controller is a single USB HID endpoint, so a transient open while a session
# holds it fails, and it used to fail with "is it plugged in?" on a bridge that
# was plugged in and working. A borrowed device is never closed by the borrower.
_live_device: Optional[Any] = None

# The setup the running session was BUILT from, in persisted form, or None when
# no hardware-backed session is up. Compared against the saved setup to answer
# `setup_awaits_activation()` — see there for what the comparison covers.
_activated_setup: Optional[Dict[str, Any]] = None


def get_hardware_config() -> HardwareConfig:
    """Return the live hardware-configuration singleton."""
    return _config


def get_session_device() -> Optional[Any]:
    """The live Virtual Hardware device; None otherwise.

    Deliberately narrower than `get_live_device`: the `/api/debug/hw` endpoints
    gate on this because they inject fake input state, which only a fake can
    accept.
    """
    return _session_device


def get_live_device() -> Optional[Any]:
    """The device the running session holds — virtual or physical — or None.

    For a caller that only wants to talk to the controller and does not care
    which kind it is: the commissioning tests borrow it instead of opening a
    second handle the driver would refuse. Read-only or briefly-driving callers
    must hold `simulation_lock` while they use it, so a bench test cannot
    interleave with a tick on the same bus.
    """
    return _live_device


def setup_awaits_activation() -> bool:
    """Whether the saved setup holds changes the running session is not using.

    A hardware-backed session is built from the setup as it stood when the
    environment was activated: the chips, their transport, and the maps the
    drivers are constructed and checked against. Editing any of that afterwards
    saves and persists, but the running session keeps what it was built with —
    so the operator is looking at a screen that no longer describes the rig that
    is running, with nothing on it saying so. This is that missing signal
    (design_new.md "Hardware setup", the `GET /api/operation/environment` row).

    The comparison covers only what activation captures. The values a driver
    reads per write — a switch's coil ports, a block's rail-polarity flag, a
    switch's rail feed — are already live, so counting them here would report a
    pending change for an edit that has taken effect, which is worse than
    silence. See `A binding is read per write` in design_new.md for that split.

    False whenever no hardware-backed session is up: with nothing running,
    nothing is out of date.
    """
    if _activated_setup is None:
        return False
    return _activation_captured(_persist_dict(_config)) != _activated_setup


def _activation_captured(persisted: Dict[str, Any]) -> Dict[str, Any]:
    """`persisted` reduced to the parts activation captures — the parts a later
    edit cannot reach without rebuilding the session. Drops the per-write reads
    named in `setup_awaits_activation`."""
    captured = dict(persisted)
    captured.pop("switch_output", None)
    captured.pop("switch_track_source", None)
    captured["block_track"] = {
        block_id: {k: v for k, v in binding.items() if k != "reversed"}
        for block_id, binding in persisted.get("block_track", {}).items()
    }
    return captured


def note_activated_setup(cfg: Optional[HardwareConfig]) -> None:
    """Record the setup a session was just built from, or clear it with None."""
    global _activated_setup
    _activated_setup = None if cfg is None else _activation_captured(_persist_dict(cfg))


def close_live_device() -> None:
    """Release the current hardware-backed session device after outputs are safe."""
    global _session_device, _live_device, _activated_setup
    device = _live_device
    _session_device = None
    _live_device = None
    # The session those interfaces belonged to is over, so there is no longer a
    # captured setup for an edit to be pending against.
    _activated_setup = None
    if device is not None:
        try:
            device.close()
        except Exception:
            # Letting go is the point, and the globals above are already clear:
            # a handle whose bridge has been unplugged may fail to close, and
            # that must not become the exception the caller sees instead of the
            # one that actually matters (`CommissionSession._close_locked`
            # swallows its close for the same reason).
            pass


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _binding_dict(m: Dict[str, PortBinding]) -> Dict[str, Dict[str, Any]]:
    return {lid: {"chip_id": b.chip_id, "port": b.port} for lid, b in m.items()}


def _track_binding_dict(m: Dict[str, TrackBinding]) -> Dict[str, Dict[str, Any]]:
    return {
        lid: {
            "chip_id": b.chip_id,
            "forward": b.forward,
            "reverse": b.reverse,
            "reversed": b.reversed,
        }
        for lid, b in m.items()
    }


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
        "active_low": c.active_low,
    }


def _persist_dict(cfg: HardwareConfig) -> Dict[str, Any]:
    """Minimal on-disk form — only user-set fields (no derived data)."""
    return {
        "controller": {"kind": cfg.controller.kind, "device_port": cfg.controller.device_port},
        "chips": [_chip_persist(c) for c in cfg.chips],
        "block_track": _track_binding_dict(cfg.block_track),
        "block_occupancy": _binding_dict(cfg.block_occupancy),
        "block_occupancy_current": _current_binding_dict(cfg.block_occupancy_current),
        "switch_output": _switch_binding_dict(cfg.switch_output),
        "switch_track_source": dict(cfg.switch_track_source),
        "idle_deadband_v": cfg.idle_deadband_v,
        "current_sense_min_drive_v": cfg.current_sense_min_drive_v,
    }


def as_dict() -> Dict[str, Any]:
    """API payload for `GET /api/hardware/config` — the persisted form (which
    already carries each chip's stored `bus`) enriched with each chip's derived
    role / direction / port_count / port_kind for the frontend."""
    payload = _persist_dict(_config)
    # Temporary response projection for the existing setup screen. These are
    # not Hardware Setup fields and are never persisted in this store.
    selected = operation_environment.get_selected()
    payload["enabled"] = selected is not OperationMode.SIMULATION
    payload["develop"] = selected is OperationMode.VIRTUAL_HARDWARE
    for chip_payload, chip in zip(payload["chips"], _config.chips):
        ck = CHIP_CATALOG.get(chip.kind)
        chip_payload["role"] = chip.role
        chip_payload["direction"] = ck.direction if ck else None
        chip_payload["port_count"] = ports_for_chip(chip.kind, chip.chain_length) if ck else 0
        # The picker addresses raw lines: same as ports for every chip except a
        # bipolar-pair PWM, whose port is a channel pair (see lines_for_chip).
        chip_payload["line_count"] = lines_for_chip(chip.kind, chip.chain_length) if ck else 0
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
        active_low=bool(data.get("active_low", False)),
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


def _parse_track_bindings(data: Any) -> Dict[str, TrackBinding]:
    data = data if isinstance(data, dict) else {}
    out: Dict[str, TrackBinding] = {}
    for logical_id, raw in data.items():
        raw = raw if isinstance(raw, dict) else {}
        lid = str(logical_id)
        out[lid] = TrackBinding(
            chip_id=str(raw.get("chip_id", "")).strip(),
            forward=_req_port(raw, "forward", lid, "track binding"),
            reverse=_req_port(raw, "reverse", lid, "track binding"),
            reversed=bool(raw.get("reversed", False)),
        )
    return out


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


def _parse_switch_track_source(data: Any) -> Dict[str, str]:
    """switch_id -> block_id. Unlike every other binding map the value is a
    logical id, not a chip port, so there is nothing here to range-check; the
    block's existence is a layout question and is checked by
    `activation_report`."""
    data = data if isinstance(data, dict) else {}
    out: Dict[str, str] = {}
    for switch_id, raw in data.items():
        block_id = str(raw).strip()
        if not block_id:
            raise HardwareConfigError(
                f"switch_track_source[{str(switch_id)!r}] needs a block id"
            )
        out[str(switch_id)] = block_id
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
        controller=ControllerConfig(**vars(_config.controller)),
        chips=list(_config.chips),
        block_track=dict(_config.block_track),
        block_occupancy=dict(_config.block_occupancy),
        block_occupancy_current=dict(_config.block_occupancy_current),
        switch_output=dict(_config.switch_output),
        switch_track_source=dict(_config.switch_track_source),
        idle_deadband_v=_config.idle_deadband_v,
        current_sense_min_drive_v=_config.current_sense_min_drive_v,
    )
    if "idle_deadband_v" in updates:
        merged.idle_deadband_v = _parse_deadband(updates["idle_deadband_v"])
    if "current_sense_min_drive_v" in updates:
        merged.current_sense_min_drive_v = _parse_min_drive(
            updates["current_sense_min_drive_v"]
        )
    if "controller" in updates:
        merged.controller = _parse_controller(updates["controller"])
    if "chips" in updates:
        merged.chips = [_parse_chip(c) for c in (updates["chips"] or [])]
    if "block_track" in updates:
        merged.block_track = _parse_track_bindings(updates["block_track"])
    if "block_occupancy" in updates:
        merged.block_occupancy = _parse_bindings(updates["block_occupancy"])
    if "block_occupancy_current" in updates:
        merged.block_occupancy_current = _parse_current_bindings(updates["block_occupancy_current"])
    if "switch_output" in updates:
        merged.switch_output = _parse_switch_bindings(updates["switch_output"])
    if "switch_track_source" in updates:
        merged.switch_track_source = _parse_switch_track_source(
            updates["switch_track_source"]
        )
    return merged


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate(cfg: HardwareConfig) -> None:
    """Raise `HardwareConfigError` if `cfg` is not a valid v1 configuration."""
    # A negative deadband is meaningless (the test is on magnitude) and one at or
    # above the ceiling would mark every block idle, silently retiring the
    # current-sense source.
    if not (0.0 <= cfg.idle_deadband_v < MAX_IDLE_DEADBAND_V):
        raise HardwareConfigError(
            f"idle_deadband_v must be at least 0 and below {MAX_IDLE_DEADBAND_V} V, "
            f"got {cfg.idle_deadband_v}"
        )

    # The two edges bound the lighting zone between them, so the floor must sit
    # above the deadband. Setting it AT the deadband is allowed and means "no
    # lighting zone" — the old two-way idle/driven split.
    if not (cfg.idle_deadband_v <= cfg.current_sense_min_drive_v <= MAX_TRACK_VOLTAGE):
        raise HardwareConfigError(
            f"current_sense_min_drive_v must be between idle_deadband_v "
            f"({cfg.idle_deadband_v} V) and {MAX_TRACK_VOLTAGE} V, "
            f"got {cfg.current_sense_min_drive_v}"
        )

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
    _validate_track_bindings(cfg.block_track, chips_by_id)
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


def _validate_track_bindings(
    m: Dict[str, TrackBinding],
    chips_by_id: Dict[str, ChipConfig],
) -> None:
    """Each block names one track-role chip and two distinct PWM channels (its
    two rails). Driving both channels of one block at once would short the
    H-bridge, so forward and reverse must differ; and a channel used by one block
    cannot drive another, so channels are unique across the whole map."""
    used: set = set()
    for logical_id, b in m.items():
        chip = chips_by_id.get(b.chip_id)
        if chip is None:
            raise HardwareConfigError(
                f"block_track[{logical_id!r}] references unknown chip {b.chip_id!r}"
            )
        ck = CHIP_CATALOG[chip.kind]
        if ck.role != ROLE_TRACK:
            raise HardwareConfigError(
                f"block_track[{logical_id!r}] chip {b.chip_id!r} is {ck.role}, needs {ROLE_TRACK}"
            )
        count = lines_for_chip(chip.kind, chip.chain_length)
        seen_here: set = set()
        for label, channel in (("forward", b.forward), ("reverse", b.reverse)):
            if not (0 <= channel < count):
                raise HardwareConfigError(
                    f"block_track[{logical_id!r}] {label} channel {channel} out of range "
                    f"0..{count - 1} on chip {b.chip_id!r}"
                )
            if channel in seen_here:
                raise HardwareConfigError(
                    f"block_track[{logical_id!r}] uses channel {channel} for both "
                    f"forward and reverse"
                )
            seen_here.add(channel)
            key = (b.chip_id, channel)
            if key in used:
                raise HardwareConfigError(
                    f"block_track: channel {channel} on chip {b.chip_id!r} is bound more than once"
                )
            used.add(key)


def _validate_switch_bindings(
    m: Dict[str, SwitchBinding],
    chips_by_id: Dict[str, ChipConfig],
) -> None:
    """Each switch names one switch-role chip and two distinct coil bits on it,
    plus an optional standby bit.

    Coil bits are exclusive: two switches sharing one would throw both machines
    from one command, and two coils of one switch sharing a bit would drive both
    directions at once — so a repeat is rejected within an entry and across
    entries alike.

    A standby bit is *not* exclusive. One TB6612 carries two motor channels
    behind a single STBY pin, so the two switches wired to it name the same
    standby bit. What is still refused is a standby bit that is some switch's
    coil: the driver raises standby for the whole pulse, which would energize
    that coil for as long as the enable is up."""
    coil_ports: Dict[Tuple[str, int], str] = {}
    standby_ports: Dict[Tuple[str, int], str] = {}
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
            if label == "standby":
                standby_ports.setdefault(key, logical_id)
                continue
            if key in coil_ports:
                raise HardwareConfigError(
                    f"switch_output: port {port} on chip {b.chip_id!r} is bound more than once"
                )
            coil_ports[key] = logical_id

    # A standby bit may be shared between switches but never with a coil. This
    # runs after the loop because either one can be declared first.
    for (chip_id, port), logical_id in standby_ports.items():
        owner = coil_ports.get((chip_id, port))
        if owner is not None:
            raise HardwareConfigError(
                f"switch_output[{logical_id!r}] standby port {port} on chip "
                f"{chip_id!r} is a coil bit of switch {owner!r}; standby is held "
                f"high for the whole pulse"
            )


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
    # switch_track_source names no chip at all — it bonds a switch to a block —
    # so removing a chip cannot dangle it. Its entries are pruned against the
    # layout by `activation_report`, not against the chip list.


def apply_updates(updates: Dict[str, Any], *, persist: bool = True) -> HardwareConfig:
    """Apply a partial section->value dict, validate the whole, and (by default)
    write it to disk. On any violation nothing is committed and
    `HardwareConfigError` is raised, so a rejected request leaves the singleton
    and the file untouched."""
    updates = updates or {}
    merged = _merged_from_updates(updates)
    _prune_dangling_bindings(merged)   # a chip removed on the setup screen drops its bindings
    _validate(merged)   # raises before any commit
    _apply(merged)
    # Migration bridge for the old setup UI. The canonical API writes the
    # environment store directly; these flags are ignored by HardwareConfig.
    if "enabled" in updates or "develop" in updates:
        selected = operation_environment.get_selected()
        enabled = updates.get(
            "enabled", selected is not OperationMode.SIMULATION
        )
        develop = updates.get(
            "develop", selected is OperationMode.VIRTUAL_HARDWARE
        )
        if "develop" in updates and bool(develop) and "enabled" not in updates:
            enabled = True
        operation_environment.select_from_legacy_flags(
            enabled=enabled, develop=develop, persist=persist
        )
    if persist:
        save_to_disk()
    return _config


def coverage_report(
    cfg: HardwareConfig,
    block_ids: Iterable[str],
    switch_ids: Iterable[str],
) -> Dict[str, Any]:
    """Which entities in a layout are wired, and which bindings name entities
    the layout does not have.

    `_validate` answers a different question — whether each binding points at a
    real chip and an in-range port — and answers it about the bindings that
    exist. It has nothing to say about the ones that do not, so a configuration
    covering three of twelve blocks passes it cleanly and goes live. In real
    mode that is not a partial win: `OccupancyDriver.read_block_currents`
    returns only bound blocks, and the detector reads a block missing from that
    map as permanently clear — so an unbound block is one no train can ever be
    detected entering, and a train rolling into it stops committing its
    progress. An unbound switch is one no route can align. Neither announces
    itself; both look like the app being broken.

    So this reports the gaps by name, for the setup screen to show before the
    operator goes live. Purely descriptive: it decides nothing and blocks
    nothing, because a layout being wired up a block at a time is the normal way
    to arrive at a wired one.

    A block counts as detected only when BOTH occupancy sources are bound — the
    digital detector AND the current-sense channel. Both are mandatory for every
    block before Virtual or Real Hardware may activate (design_new.md "Power",
    Both occupancy sources are mandatory); a block bound in only one map is
    incomplete, never silently covered by the other.
    """
    blocks = sorted(set(block_ids))
    switches = sorted(set(switch_ids))
    detected = set(cfg.block_occupancy) & set(cfg.block_occupancy_current)

    def split(ids: List[str], bound: Any) -> Dict[str, List[str]]:
        return {
            "bound": [i for i in ids if i in bound],
            "unbound": [i for i in ids if i not in bound],
        }

    # A binding whose logical id is not in the layout: a block or switch that
    # was renamed or deleted after it was wired. Harmless to the runtime, which
    # simply never addresses it, but it is dead configuration and it makes the
    # counts above read as better coverage than there is.
    known_blocks, known_switches = set(blocks), set(switches)
    stale = {
        "block_occupancy": sorted(set(cfg.block_occupancy) - known_blocks),
        "block_occupancy_current": sorted(
            set(cfg.block_occupancy_current) - known_blocks
        ),
        "block_track": sorted(set(cfg.block_track) - known_blocks),
        "switch_output": sorted(set(cfg.switch_output) - known_switches),
    }

    return {
        "enabled": operation_environment.get_selected()
        is not OperationMode.SIMULATION,
        "blocks": {
            "total": len(blocks),
            "occupancy": split(blocks, detected),
            "track": split(blocks, set(cfg.block_track)),
        },
        "switches": {"total": len(switches), **split(switches, set(cfg.switch_output))},
        "stale": stale,
        "complete": not (
            [b for b in blocks if b not in detected]
            or [b for b in blocks if b not in cfg.block_track]
            or [s for s in switches if s not in cfg.switch_output]
            or any(stale.values())
        ),
    }


def activation_report(
    cfg: HardwareConfig,
    environment: OperationMode,
    block_ids: Iterable[str],
    switch_ids: Iterable[str],
    lighting_voltages: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Return the fail-closed readiness verdict for one whole environment.

    `issues` are activation blockers; `warnings` are advisory and leave `ready`
    true. Only setup a rig cannot operate without blocks activation — an
    optional facility the rig simply does not have is reported, not enforced.
    """
    environment = operation_environment.parse(environment)
    if environment is OperationMode.SIMULATION:
        return {
            "ready": True,
            "environment": environment.value,
            "issues": [],
            "warnings": [],
        }

    blocks = sorted(set(block_ids))
    switches = sorted(set(switch_ids))
    issues: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def _entry(code: str, message: str, ids: Optional[List[str]]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"code": code, "message": message}
        if ids:
            entry["ids"] = ids
        return entry

    def issue(code: str, message: str, ids: Optional[List[str]] = None) -> None:
        issues.append(_entry(code, message, ids))

    def warn(code: str, message: str, ids: Optional[List[str]] = None) -> None:
        warnings.append(_entry(code, message, ids))

    if not cfg.chips:
        issue("no_chips", "Hardware Setup has no chips")
    missing_track = [block_id for block_id in blocks if block_id not in cfg.block_track]
    # Both occupancy sources are mandatory for every block (design_new.md
    # "Power", Both occupancy sources are mandatory): a block missing EITHER the
    # digital or the current-sense binding is not ready. A missing current
    # binding is an activation blocker, never a silent fall-back to the digital
    # detector.
    detected = set(cfg.block_occupancy) & set(cfg.block_occupancy_current)
    missing_occupancy = [block_id for block_id in blocks if block_id not in detected]
    missing_switch = [switch_id for switch_id in switches if switch_id not in cfg.switch_output]
    # Every switch must name the block feeding its rails, and that block must be
    # one the layout actually has (design_new.md "Power", Bindings).
    missing_feed = [
        switch_id for switch_id in switches if switch_id not in cfg.switch_track_source
    ]
    unknown_feed = sorted(
        switch_id
        for switch_id, block_id in cfg.switch_track_source.items()
        if switch_id in set(switches) and block_id not in set(blocks)
    )
    known_blocks, known_switches = set(blocks), set(switches)
    stale = sorted(
        (set(cfg.block_track) | set(cfg.block_occupancy) | set(cfg.block_occupancy_current))
        - known_blocks
    )
    stale_switches = sorted(
        (set(cfg.switch_output) | set(cfg.switch_track_source)) - known_switches
    )
    if missing_track:
        issue("missing_track_binding", "Blocks are missing track-output bindings", missing_track)
    if missing_occupancy:
        issue("missing_occupancy_binding", "Blocks are missing occupancy bindings", missing_occupancy)
    if missing_switch:
        issue("missing_switch_binding", "Switches are missing throw bindings", missing_switch)
    if missing_feed:
        issue(
            "missing_switch_track_source",
            "Switches are missing the block that feeds their rails",
            missing_feed,
        )
    if unknown_feed:
        issue(
            "unknown_switch_track_source",
            "Switches name a feed block not in the loaded layout",
            unknown_feed,
        )
    if stale:
        issue("stale_block_binding", "Bindings name blocks not in the loaded layout", stale)
    if stale_switches:
        issue("stale_switch_binding", "Bindings name switches not in the loaded layout", stale_switches)

    # Every placed train's resolved lighting_voltage must sit below the setup's
    # current_sense_min_drive_v, so a lit but motionless train lands in the
    # middle zone rather than reading as driven (design_new.md "Power",
    # current_sense_min_drive_v must sit above lighting_voltage). Checked here at
    # activation the same way a missing binding is, whatever set the value
    # (calibration, an imported roster entry, an adoption default, a Simulation
    # save that never evaluated this bound).
    over_floor = sorted(
        train_id
        for train_id, lighting_voltage in (lighting_voltages or {}).items()
        if lighting_voltage is not None
        and lighting_voltage >= cfg.current_sense_min_drive_v
    )
    if over_floor:
        issue(
            "lighting_voltage_over_current_floor",
            "Trains have a lighting_voltage at or above current_sense_min_drive_v "
            "(a lit stopped train would read as driven)",
            over_floor,
        )

    # A rig that loses its host mid-run keeps whatever the PCA9685 and '595
    # were last told to hold: safe state is a write, and a dead process
    # writes nothing. Reported once here so activation is an informed act.
    if environment is OperationMode.REAL_HARDWARE:
        warn(
            "outputs_latch_on_host_death",
            "Track output and point motors hold their last commanded value "
            "if this process or the host dies: safe state needs a working "
            "process to write it",
        )

    return {
        "ready": not issues,
        "environment": environment.value,
        "issues": issues,
        "warnings": warnings,
    }


# The maps a plain (chip_id, port) binding covers. `block_track`, `switch_output`,
# and `block_occupancy_current` carry more than one field per entry, so each has
# its own setter below.
BINDING_MAPS = ("block_occupancy",)


def _copy_config() -> HardwareConfig:
    """A shallow working copy of the singleton (chips + binding objects shared,
    the containers fresh) for validate-before-commit edits."""
    return HardwareConfig(
        controller=ControllerConfig(**vars(_config.controller)),
        chips=list(_config.chips),
        block_track=dict(_config.block_track),
        block_occupancy=dict(_config.block_occupancy),
        block_occupancy_current=dict(_config.block_occupancy_current),
        switch_output=dict(_config.switch_output),
        switch_track_source=dict(_config.switch_track_source),
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
    editors). `logical_id` is the block for `block_occupancy`. `chip_id`
    None/empty clears the binding. The whole configuration is re-validated, so an
    out-of-range / role-mismatched / already-used port is rejected and nothing is committed."""
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


def set_track_binding(
    block_id: str,
    chip_id: Optional[str] = None,
    forward: Optional[int] = None,
    reverse: Optional[int] = None,
    *,
    polarity_reversed: Any = None,
    persist: bool = True,
) -> HardwareConfig:
    """Set or clear one block's track wiring (the per-entity block editor's
    Hardware section). `chip_id` None/empty clears it; otherwise both the forward
    and reverse PWM channels are required. `polarity_reversed` sets the binding's
    `reversed` wiring flag; None keeps the existing value, so a channel edit does
    not silently reset a corrected block. The whole configuration is
    re-validated, so an out-of-range, role-mismatched, equal, or already-used
    channel is rejected and nothing is committed."""
    candidate = _copy_config()
    target = candidate.block_track
    if chip_id in (None, ""):
        target.pop(block_id, None)
    else:
        if forward is None or reverse is None:
            raise HardwareConfigError(
                "forward and reverse channels are both required when chip_id is set"
            )
        prior = target.get(block_id)
        target[block_id] = TrackBinding(
            chip_id=str(chip_id),
            forward=int(forward),
            reverse=int(reverse),
            reversed=(
                bool(polarity_reversed)
                if polarity_reversed is not None
                else bool(prior.reversed) if prior is not None else False
            ),
        )
    _validate(candidate)
    _apply(candidate)
    if persist:
        save_to_disk()
    return _config


def track_polarity_reversed(block_id: str) -> bool:
    """Whether `block_id`'s rails are wired reversed, per the live configuration.

    The track driver's `polarity_of` read (design_new.md "Power", Rail polarity
    lives on the rail binding). Reads the module-level `_config` on every call —
    not a snapshot — so an editor flip lands on the next track write without a
    session rebuild. A block with no rail binding has no rails to be reversed:
    False."""
    binding = _config.block_track.get(block_id)
    return bool(binding.reversed) if binding is not None else False


def switch_coil_ports(
    switch_id: str,
) -> Optional[Tuple[str, int, int, Optional[int]]]:
    """This switch's `(chip_id, port_main, port_diverge, port_standby)`, per the
    live configuration.

    The switch driver's `binding_of` read (design_new.md "Hardware setup", A
    binding is read per write), and the coil-side counterpart of
    `track_polarity_reversed` above. Reads the module-level `_config` on every
    call — not a snapshot — so re-picking a coil port aims the next throw at the
    new bit without a session rebuild, which is what makes the panel's port
    dropdowns mean something on a rig being wired up. A switch with no binding
    has no coil to drive: None."""
    binding = _config.switch_output.get(switch_id)
    if binding is None:
        return None
    return (
        binding.chip_id,
        binding.port_main,
        binding.port_diverge,
        binding.port_standby,
    )


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


def set_switch_track_source(
    switch_id: str,
    block_id: Optional[str] = None,
    *,
    persist: bool = True,
) -> HardwareConfig:
    """Set or clear the block whose feed powers one switch's rails (the
    per-entity switch editor's Hardware section). `block_id` None/empty clears
    it.

    Unlike every other binding this names a logical id rather than a chip port,
    so there is nothing to range-check here; whether the block exists in the
    loaded layout is a layout question and `activation_report` answers it."""
    candidate = _copy_config()
    if block_id in (None, ""):
        candidate.switch_track_source.pop(switch_id, None)
    else:
        candidate.switch_track_source[switch_id] = str(block_id).strip()
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


def _parse_deadband(raw: Any) -> float:
    """The idle deadband from either config path. `None` (or an absent key, via
    the caller) means the module default, so clearing the field on the setup
    screen restores it rather than dropping to zero."""
    if raw is None:
        return DEFAULT_IDLE_DEADBAND_V
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise HardwareConfigError(
            f"idle_deadband_v must be a number, got {raw!r}"
        ) from exc


def _parse_min_drive(raw: Any) -> float:
    """The current-sense drive floor from either config path. `None` means the
    module default, matching `_parse_deadband`."""
    if raw is None:
        return DEFAULT_CURRENT_SENSE_MIN_DRIVE_V
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise HardwareConfigError(
            f"current_sense_min_drive_v must be a number, got {raw!r}"
        ) from exc


def _config_from_dict(payload: Any) -> HardwareConfig:
    """Build a standalone `HardwareConfig` from a whole-configuration dict, with
    no merge against the singleton and no validation: an absent section takes its
    default rather than the singleton's current value."""
    payload = payload if isinstance(payload, dict) else {}
    return HardwareConfig(
        controller=_parse_controller(payload.get("controller", {})),
        chips=[_parse_chip(c) for c in (payload.get("chips") or [])],
        block_track=_parse_track_bindings(payload.get("block_track", {})),
        block_occupancy=_parse_bindings(payload.get("block_occupancy", {})),
        block_occupancy_current=_parse_current_bindings(payload.get("block_occupancy_current", {})),
        switch_output=_parse_switch_bindings(payload.get("switch_output", {})),
        switch_track_source=_parse_switch_track_source(
            payload.get("switch_track_source", {})
        ),
        idle_deadband_v=_parse_deadband(payload.get("idle_deadband_v")),
        current_sense_min_drive_v=_parse_min_drive(
            payload.get("current_sense_min_drive_v")
        ),
    )


def config_from_payload(payload: Dict[str, Any]) -> HardwareConfig:
    """Build and validate a standalone `HardwareConfig` from a full payload
    (no merge with the singleton). Used by the self-test / port-test endpoints
    to exercise the config a user is editing before it is saved."""
    cfg = _config_from_dict(payload)
    # Commissioning requests may still arrive from the old two-toggle UI.
    # Carry the requested environment only as transient request metadata; it is
    # not a Hardware Setup field and is never serialized.
    if "environment" in payload:
        requested = operation_environment.parse(payload.get("environment"))
    elif bool(payload.get("develop", False)):
        requested = OperationMode.VIRTUAL_HARDWARE
    else:
        requested = OperationMode.REAL_HARDWARE
    setattr(cfg, "_requested_environment", requested)
    _validate(cfg)
    return cfg


def persisted_dict() -> Dict[str, Any]:
    """The current configuration in its exact on-disk form — the same dict
    `save_to_disk` writes. This is what a layout save file carries in its
    `hardware` section, so a layout is portable with its wiring."""
    return _persist_dict(_config)


def apply_snapshot(data: Any, *, persist: bool = True) -> HardwareConfig:
    """Adopt a whole configuration at once (a layout file's `hardware` section)
    and, by default, write it to the app-managed config file so it survives the
    restart that applies it.

    Unlike `apply_updates`, this replaces the configuration rather than merging:
    a section absent from `data` resets to its default instead of keeping the
    running value, because a snapshot is a complete configuration, not a partial
    edit. Bindings naming a chip the snapshot does not carry are dropped the same
    way the setup screen drops them. On any violation nothing is committed and
    `HardwareConfigError` is raised."""
    candidate = _config_from_dict(data)
    _prune_dangling_bindings(candidate)
    _validate(candidate)   # raises before any commit
    _apply(candidate)
    if persist:
        save_to_disk()
    return _config


def save_to_disk(path: Optional[str] = None) -> None:
    """Write the current configuration to the app-managed JSON file."""
    target = path if path is not None else CONFIG_PATH
    with open(target, "w") as fh:
        json.dump(_persist_dict(_config), fh, indent=2)


def load_from_disk(path: Optional[str] = None) -> HardwareConfig:
    """Load the configuration from disk into the singleton, if a valid file
    exists (called once at session build). Fail-safe: a missing, unreadable,
    malformed, or invalid file leaves the setup singleton unchanged. It never
    changes the separately selected environment."""
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
    """Restore test defaults without touching either on-disk store.

    Production never couples these stores; this helper resets the separate
    environment singleton as well so an isolated test starts in Simulation.
    """
    global _config, _session_device, _live_device, _activated_setup
    _config = HardwareConfig()
    _session_device = None
    _live_device = None
    # The device handles above are what a session was; dropping them without
    # dropping what it captured would leave an edit pending against a session
    # that no longer exists.
    _activated_setup = None
    operation_environment.reset_to_defaults()
    return _config


# ---------------------------------------------------------------------------
# Session interface construction
# ---------------------------------------------------------------------------
def _binding_tuples(m: Dict[str, PortBinding]) -> Dict[str, Tuple[str, int]]:
    return {lid: (b.chip_id, b.port) for lid, b in m.items()}


def _track_binding_tuples(
    m: Dict[str, TrackBinding],
) -> Dict[str, Tuple[str, int, int]]:
    """Resolve each block's track to (chip_id, forward, reverse) for the driver,
    which addresses the two rails by PWM channel number."""
    return {lid: (b.chip_id, b.forward, b.reverse) for lid, b in m.items()}


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
    *,
    environment: Optional[OperationMode] = None,
    track_polarity_of: Optional[Callable[[str], bool]] = None,
) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Resolve the configuration to the session's interface overrides:
    `(input_interface, block_output_interface, switch_output_interface)`.

    All-`None` in Simulation (the happy-path model interfaces are constructed by
    `SimulationController`). Virtual Hardware opens the in-memory device and
    Real Hardware opens the physical controller. Completeness and the removal
    of per-role fallback are enforced by the activation phase.

    `track_polarity_of` answers a block's rail-binding `reversed` flag for the
    track driver — `track_polarity_reversed` above is the canonical lookup, and
    a caller passes it (rather than a snapshot) so an operator can flip a
    block's rails without rebuilding the session. `switch_coil_ports` answers
    the same way for the switch driver's coil ports; it is passed directly
    rather than injected, because it reads this module's own configuration and
    a caller has nothing to say about it.

    Records what it built from, so `setup_awaits_activation` can tell an edit
    the session is not using from one it is.
    """
    cfg = cfg if cfg is not None else _config
    environment = (
        operation_environment.get_selected()
        if environment is None
        else operation_environment.parse(environment)
    )
    if environment is OperationMode.SIMULATION:
        # Simulation builds no chips from the setup, so no part of it is captured
        # and no edit to it can be pending.
        note_activated_setup(None)
        return None, None, None

    global _session_device, _live_device
    if environment is OperationMode.VIRTUAL_HARDWARE:
        # Virtual Hardware: the same setup-aware stack on the in-memory device.
        # No ch347api needed, and the loss-of-control watchdog stays unarmed —
        # there is no physical track to protect, and a paused dev session must
        # not have its outputs zeroed under it.
        from hardware import virtual_rig
        from hardware.fake import FakeCh347Device
        from hardware.interfaces import build_real_interfaces

        device = FakeCh347Device(
            present_addresses={
                c.i2c_address for c in cfg.chips if c.i2c_address is not None
            }
        )
        device.open()
        try:
            interfaces = build_real_interfaces(
                device,
                cfg.chips,
                _track_binding_tuples(cfg.block_track),
                _binding_tuples(cfg.block_occupancy),
                _switch_binding_tuples(cfg.switch_output),
                block_occupancy_current=_current_binding_tuples(cfg.block_occupancy_current),
                idle_deadband_v=cfg.idle_deadband_v,
                current_sense_min_drive_v=cfg.current_sense_min_drive_v,
                track_polarity_of=track_polarity_of,
                switch_binding_of=switch_coil_ports,
                watchdog_timeout_s=None,
            )
            # A rig at rest is an empty layout with one train parked on it, not
            # the raw register file. See `seed_startup_presence` — on an
            # `active_low` detector chain an unwritten fake reads every block
            # occupied, which is the one state a real rig can never be in.
            virtual_rig.seed_startup_presence(cfg, device)
            virtual_rig.install_drive_swamp(cfg, device)
        except Exception as exc:
            raise HardwareBackendUnavailable(
                f"failed to initialize fake hardware chips: {exc}"
            ) from exc
        _session_device = device
        _live_device = device
        note_activated_setup(cfg)
        return interfaces

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
            _track_binding_tuples(cfg.block_track),
            _binding_tuples(cfg.block_occupancy),
            _switch_binding_tuples(cfg.switch_output),
            block_occupancy_current=_current_binding_tuples(cfg.block_occupancy_current),
            idle_deadband_v=cfg.idle_deadband_v,
            current_sense_min_drive_v=cfg.current_sense_min_drive_v,
            track_polarity_of=track_polarity_of,
            switch_binding_of=switch_coil_ports,
            # A real session drives real track; arm the loss-of-control watchdog
            # (design_new.md "Hardware interface boundary").
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

    # The device holds a USB handle for the life of the session. It is closed
    # only through `close_live_device`: by the next activation, by commissioning
    # or the occupancy bench taking the rig, or at process exit by shutdown.py
    # once the safe state has been written through it. No atexit hook is registered here -- atexit runs
    # the newest hook first, so a close registered by an activation after boot
    # would run before the shutdown safe state and leave every write refused.
    # Publishing it as the live device is what lets the commissioning tests run
    # against the rig instead of failing to open a second handle for it.
    _live_device = device
    note_activated_setup(cfg)
    return interfaces
