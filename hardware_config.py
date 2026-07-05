"""Hardware configuration — which concrete implementation each interface wires.

design_new.md "Power -> Hardware configuration": this store picks the backend
for the output interfaces (block + switch power) and for the input interface
(occupancy) independently, plus the device a real backend runs against.

It differs from the ATC/ATO runtime tunables (`runtime_config.py`) in two ways,
both spelled out in the doc:

- **When it takes effect.** A runtime tunable is read live and applies on the
  consuming code's next read. Hardware configuration is read only when an
  operation session is built (`bootstrap.py`), so a change reaches only the
  next session, not the one already running.
- **Persistence.** Because that next session build happens only after a process
  restart, an in-memory value would be discarded before it could take effect.
  So hardware configuration persists to an app-managed JSON file
  (`CONFIG_PATH`), written only through the `PUT /api/hardware/config` API — the
  same way the app owns its layout save files, not a hand-authored config
  surface (design_new.md "Out of Scope", Hand-authored config files or env vars
  for settings).

The logical-id -> physical-channel map (which pin / expander port / I2C address
a `block_id` or `switch_id` uses) is NOT here: it lives inside the concrete
implementation, in code. This store only selects the implementation and the
device it talks to (design_new.md "Power", Hardware interface boundary).
"""

from dataclasses import asdict, dataclass, fields
import json
import os
from typing import Any, Dict, Optional, Tuple

from enums import OperationMode

# Allowed values per field (design_new.md "Power -> Hardware configuration").
OUTPUT_BACKENDS = ("virtual", "gpio")
OCCUPANCY_BACKENDS = ("virtual", "i2c")
DEVICE_TARGETS = ("ch347", "raspberry_pi")

# On-disk store. A sibling to the layout save files the app already persists —
# app-managed, written only through the API. Tests point this at a temp path.
CONFIG_PATH = "hardware_config.json"


class HardwareConfigError(ValueError):
    """A rejected hardware-configuration update.

    Raised for an out-of-range backend/device value, or for a real backend
    selected without the required `device_target`. The route layer turns this
    into an HTTP 400 and writes nothing.
    """


class HardwareBackendUnavailable(RuntimeError):
    """A real backend was selected but its driver is not available in this build.

    The concrete GPIO / I2C driver implementations are provided by the `hardware`
    package. Until that package is present, resolving a real backend raises this,
    and `bootstrap.py` falls back to sim mode so the server still boots.
    """


@dataclass
class HardwareConfig:
    """The selected backend for each interface, plus the device a real backend
    runs against. Defaults are the all-virtual sim-mode configuration."""

    output_backend: str = "virtual"       # OutputInterface + SwitchOutputInterface
    occupancy_backend: str = "virtual"    # InputInterface
    device_target: Optional[str] = None   # "ch347" | "raspberry_pi"; None when all-virtual
    device_port: Optional[str] = None     # ch347 serial/HID path; None for raspberry_pi


_config = HardwareConfig()


def get_hardware_config() -> HardwareConfig:
    """Return the live hardware-configuration singleton."""
    return _config


def as_dict() -> Dict[str, Any]:
    """Serialize the current configuration for the hardware-config API."""
    return asdict(_config)


def is_real(cfg: Optional[HardwareConfig] = None) -> bool:
    """True when either backend is not `virtual` (i.e. the session is real mode)."""
    cfg = cfg if cfg is not None else _config
    return cfg.output_backend != "virtual" or cfg.occupancy_backend != "virtual"


def derive_operation_mode(cfg: Optional[HardwareConfig] = None) -> OperationMode:
    """Map the configuration to its `OperationMode` (design_new.md "Run modes":
    sim when both backends are virtual, real when either is not)."""
    return OperationMode.REAL if is_real(cfg) else OperationMode.SIM


def _validate(cfg: HardwareConfig) -> None:
    """Raise `HardwareConfigError` if `cfg` is not a valid v1 configuration."""
    if cfg.output_backend not in OUTPUT_BACKENDS:
        raise HardwareConfigError(
            f"output_backend must be one of {list(OUTPUT_BACKENDS)}, "
            f"got {cfg.output_backend!r}"
        )
    if cfg.occupancy_backend not in OCCUPANCY_BACKENDS:
        raise HardwareConfigError(
            f"occupancy_backend must be one of {list(OCCUPANCY_BACKENDS)}, "
            f"got {cfg.occupancy_backend!r}"
        )
    if cfg.device_target is not None and cfg.device_target not in DEVICE_TARGETS:
        raise HardwareConfigError(
            f"device_target must be one of {list(DEVICE_TARGETS)} or null, "
            f"got {cfg.device_target!r}"
        )
    # A real backend needs a device to run against (design_new.md: device_target
    # "is required whenever either backend is not virtual, and ignored when both
    # are").
    if is_real(cfg) and cfg.device_target is None:
        raise HardwareConfigError(
            "device_target is required when output_backend or occupancy_backend "
            "is not 'virtual'"
        )


def _coerce(cfg: HardwareConfig) -> HardwareConfig:
    """Normalize field types from a raw (JSON/body) source: backends to str,
    the two optional device fields to str-or-None. Empty strings on the optional
    fields read as None so a cleared form field means 'unset'."""
    def _opt(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    return HardwareConfig(
        output_backend=str(cfg.output_backend).strip(),
        occupancy_backend=str(cfg.occupancy_backend).strip(),
        device_target=_opt(cfg.device_target),
        device_port=_opt(cfg.device_port),
    )


def _apply(cfg: HardwareConfig) -> HardwareConfig:
    """Commit a validated config onto the singleton in place."""
    for f in fields(HardwareConfig):
        setattr(_config, f.name, getattr(cfg, f.name))
    return _config


def apply_updates(updates: Dict[str, Any], *, persist: bool = True) -> HardwareConfig:
    """Apply a partial field->value dict, validate the result, and (by default)
    write it to disk.

    Unknown keys are ignored. The merged configuration is validated as a whole;
    on any violation nothing is committed and `HardwareConfigError` is raised, so
    a rejected request leaves both the singleton and the file untouched.
    """
    valid = {f.name for f in fields(HardwareConfig)}
    merged = HardwareConfig(**asdict(_config))
    for key, raw in (updates or {}).items():
        if key in valid:
            setattr(merged, key, raw)
    merged = _coerce(merged)
    _validate(merged)  # raises before any commit
    _apply(merged)
    if persist:
        save_to_disk()
    return _config


def save_to_disk(path: Optional[str] = None) -> None:
    """Write the current configuration to the app-managed JSON file."""
    target = path if path is not None else CONFIG_PATH
    with open(target, "w") as fh:
        json.dump(asdict(_config), fh, indent=2)


def load_from_disk(path: Optional[str] = None) -> HardwareConfig:
    """Load the configuration from disk into the singleton, if a valid file
    exists. Called once at session build (`bootstrap.py`).

    Fail-safe: a missing, unreadable, malformed, or invalid file leaves the
    singleton at its all-virtual defaults, so a corrupt file never boots the
    runtime into a broken real mode.
    """
    target = path if path is not None else CONFIG_PATH
    try:
        with open(target) as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _config
    if not isinstance(data, dict):
        return _config
    valid = {f.name for f in fields(HardwareConfig)}
    candidate = HardwareConfig()
    for key, raw in data.items():
        if key in valid:
            setattr(candidate, key, raw)
    candidate = _coerce(candidate)
    try:
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


def build_session_interfaces(
    cfg: Optional[HardwareConfig] = None,
) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Resolve the configuration to the interface overrides a session is built
    with: `(input_interface, block_output_interface, switch_output_interface)`.

    A `None` in any slot means "use the simulated default" — `SimulationController`
    constructs the `Simulated*` interface for it. A real backend is constructed
    from the `hardware` driver package; when that package is not present in the
    build, `HardwareBackendUnavailable` is raised (see the class docstring), and
    `bootstrap.py` falls back to sim mode.
    """
    cfg = cfg if cfg is not None else _config
    input_interface: Optional[Any] = None
    block_output_interface: Optional[Any] = None
    switch_output_interface: Optional[Any] = None

    if cfg.occupancy_backend == "i2c":
        input_interface = _build_i2c_input(cfg)
    if cfg.output_backend == "gpio":
        block_output_interface, switch_output_interface = _build_gpio_outputs(cfg)

    return input_interface, block_output_interface, switch_output_interface


def _build_gpio_outputs(cfg: HardwareConfig) -> Tuple[Any, Any]:
    """Construct the real GPIO block + switch output interfaces for `device_target`."""
    try:
        from hardware.gpio_output import (
            GpioOutputInterface,
            GpioSwitchOutputInterface,
        )
    except ImportError as exc:
        raise HardwareBackendUnavailable(
            f"output_backend='gpio' on device {cfg.device_target!r} needs the "
            f"GPIO driver (hardware.gpio_output), which is not available in this "
            f"build"
        ) from exc
    return (
        GpioOutputInterface(cfg.device_target, cfg.device_port),
        GpioSwitchOutputInterface(cfg.device_target, cfg.device_port),
    )


def _build_i2c_input(cfg: HardwareConfig) -> Any:
    """Construct the real I2C block-current input interface for `device_target`."""
    try:
        from hardware.i2c_input import I2CInputInterface
    except ImportError as exc:
        raise HardwareBackendUnavailable(
            f"occupancy_backend='i2c' on device {cfg.device_target!r} needs the "
            f"I2C driver (hardware.i2c_input), which is not available in this "
            f"build"
        ) from exc
    return I2CInputInterface(cfg.device_target, cfg.device_port)
