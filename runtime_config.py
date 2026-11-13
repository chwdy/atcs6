"""Runtime-tunable operational configuration.

Single source of truth for the values an operator can change from a live
session (design_v2.md "Runtime settings").
Seeded from the documented defaults; mutated through the settings API
(`routes/settings.py`).

How it takes effect without code churn: the consuming modules already read these
values as *module-level globals* (e.g. `STOPPED_SPEED_THRESHOLD_KMH`), and a
module-global name lookup is dynamic — it re-reads the module's attribute on
every access. So `apply_updates` writes the new value back onto every module
that holds that name (the `_TARGETS` map below), and existing consumer code
picks it up on its next read.
"""

from dataclasses import asdict, dataclass, fields
import importlib
import math
from typing import Any, Dict, List, Tuple

import constants as C


@dataclass
class RuntimeConfig:
    """Every tunable field (design_v2.md "Runtime settings", Tunable fields)."""

    # --- Trains (the runtime) ---
    hold_time_s: float = 5.0
    # --- Event stream ---
    notification_retention_rows: int = C.NOTIFICATION_RETENTION_ROWS


# field name -> the module-global sites that hold that value. Reassigned on
# update so live consumers (which read the module global per access) see it.
_TARGETS: Dict[str, List[Tuple[str, str]]] = {
    "hold_time_s": [("train_runtime", "HOLD_TIME_S")],
    "notification_retention_rows": [
        ("constants", "NOTIFICATION_RETENTION_ROWS"),
        ("notification_center", "NOTIFICATION_RETENTION_ROWS"),
    ],
}


# --- Validation (design_v2.md "Runtime settings", Validation) ---------------
# After type coercion, every numeric value must be finite and satisfy its field
# rule; a value that fails is ignored exactly like a coercion failure, while
# other valid fields in the same request still apply.

# Single-field lower bounds. `>= b` (inclusive) and `> b` (exclusive).
_MIN_INCLUSIVE: Dict[str, float] = {
    "notification_retention_rows": 1,
}
_MIN_EXCLUSIVE: Dict[str, float] = {
    "hold_time_s": 0.0,
}


def _passes_single_field_bound(field_name: str, value: Any) -> bool:
    """Whether `value` satisfies `field_name`'s single-field bound (or has
    none). Finiteness is checked separately by the caller."""
    if field_name in _MIN_INCLUSIVE:
        return value >= _MIN_INCLUSIVE[field_name]
    if field_name in _MIN_EXCLUSIVE:
        return value > _MIN_EXCLUSIVE[field_name]
    return True


_config = RuntimeConfig()


def get_runtime_config() -> RuntimeConfig:
    """Return the live, mutable runtime configuration singleton."""
    return _config


def as_dict() -> Dict[str, Any]:
    """Serialize the current config for the settings API."""
    return asdict(_config)


def _propagate(field_name: str, value: Any) -> None:
    """Write `value` onto every module global that holds this tunable, so
    live consumers reading the module global pick it up on next access."""
    for module_path, attr in _TARGETS.get(field_name, []):
        try:
            module = importlib.import_module(module_path)
        except Exception:
            continue
        if hasattr(module, attr):
            setattr(module, attr, value)


def apply_updates(updates: Dict[str, Any]) -> RuntimeConfig:
    """Apply a partial dict of field→value updates, coercing to each field's
    type, validating per design_v2.md "Runtime settings" (Validation), and
    propagating surviving writes to consumer module globals.

    Unknown keys and `null` values are ignored. After coercion each value must
    be finite and satisfy its field rule; a failing value is dropped exactly
    like a coercion failure while other fields in the same request still apply.
    """
    valid = {f.name for f in fields(RuntimeConfig)}
    candidates: Dict[str, Any] = {}
    for key, raw in (updates or {}).items():
        if key not in valid or raw is None:
            continue
        coerce = type(getattr(_config, key))
        try:
            value = coerce(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value):
            continue
        if not _passes_single_field_bound(key, value):
            continue
        candidates[key] = value
    for key, value in candidates.items():
        setattr(_config, key, value)
        _propagate(key, value)
    return _config


def reset_to_defaults() -> RuntimeConfig:
    """Restore all fields to seed defaults and re-propagate (used by tests)."""
    global _config
    _config = RuntimeConfig()
    for f in fields(RuntimeConfig):
        _propagate(f.name, getattr(_config, f.name))
    return _config
