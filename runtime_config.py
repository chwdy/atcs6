"""Runtime-tunable operational configuration.

Single source of truth for the operational tunables a user can adjust live from
the **Advanced** settings panel, instead of them being hidden module constants.
Seeded from the documented defaults; mutated through the settings API
(`routes/settings.py`).

How it takes effect without code churn: the consuming modules already read these
values as *module-level globals* (e.g. `STOPPED_SPEED_THRESHOLD_KMH`), and a
module-global name lookup is dynamic — it re-reads the module's attribute on
every access. So `apply_updates` writes the new value back onto every module
that holds that name (the `_TARGETS` map below, derived from the importers), and
existing consumer code picks it up on its next read. No per-read rewiring.

Structural constants that must not drift at runtime (run-number arithmetic,
priority bounds used in validation) are intentionally NOT exposed here.
"""

from dataclasses import asdict, dataclass, fields
import importlib
import math
from typing import Any, Dict, List, Tuple

import constants as C


@dataclass
class RuntimeConfig:
    # --- ATC overspeed escalation thresholds (were inline literals) ---
    overspeed_eb_threshold_kmh: float = 10.0
    overspeed_warn_threshold_kmh: float = 5.0
    # --- ATC / ATO control (audit Tier 3) ---
    atc_signal_lookahead_blocks: int = 3
    ato_speed_margin_kmh: float = 3.0
    ato_speed_tolerance_max_kmh: float = 2.0
    ato_speed_tolerance_min_kmh: float = 0.05
    ato_speed_tolerance_ratio: float = 0.1
    lookahead_headroom: float = 0.5
    min_lookahead_cm: float = 5.0
    lookahead_sample_count: int = 4
    brake_deadband_kmh: float = 0.3
    # --- Safety margins / geometry globals (audit Tier 4) ---
    stopped_speed_threshold_kmh: float = C.STOPPED_SPEED_THRESHOLD_KMH
    track_end_safety_margin_cm: float = C.TRACK_END_SAFETY_MARGIN_CM
    platform_safety_margin_cm: float = C.PLATFORM_SAFETY_MARGIN_CM
    caution_speed_limit_kmh: float = C.CAUTION_SPEED_LIMIT_KMH
    # --- Sim timing (audit Tier 5) ---
    idle_hold_release_ticks: int = C.IDLE_HOLD_RELEASE_TICKS
    idle_hold_cooldown_ticks: int = C.IDLE_HOLD_COOLDOWN_TICKS
    # --- Route planning ---
    waypoint_lookahead_blocks: int = C.WAYPOINT_LOOKAHEAD_BLOCKS
    # --- Route timing ---
    delay_class_on_time_tolerance_seconds: float = (
        C.DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS
    )
    # --- Event stream ---
    notification_retention_rows: int = C.NOTIFICATION_RETENTION_ROWS


# field name -> the module-global sites that hold that value. Reassigned on
# update so live consumers (which read the module global per access) see it.
# Derived from the importer grep; `constants` is always included where it owns
# the canonical definition.
_TARGETS: Dict[str, List[Tuple[str, str]]] = {
    "overspeed_eb_threshold_kmh": [("train_controller", "OVERSPEED_EB_THRESHOLD_KMH")],
    "overspeed_warn_threshold_kmh": [("train_controller", "OVERSPEED_WARN_THRESHOLD_KMH")],
    "atc_signal_lookahead_blocks": [
        ("train_controller", "ATC_SIGNAL_LOOKAHEAD_BLOCKS"),
        ("train_controller.atc", "ATC_SIGNAL_LOOKAHEAD_BLOCKS"),
    ],
    "ato_speed_margin_kmh": [("train_controller", "ATO_SPEED_MARGIN_KMH")],
    "ato_speed_tolerance_max_kmh": [("train_controller.ato", "ATO_SPEED_TOLERANCE_MAX")],
    "ato_speed_tolerance_min_kmh": [("train_controller.ato", "ATO_SPEED_TOLERANCE_MIN")],
    "ato_speed_tolerance_ratio": [("train_controller.ato", "ATO_SPEED_TOLERANCE_RATIO")],
    "lookahead_headroom": [("train_controller.ato", "LOOKAHEAD_HEADROOM")],
    "min_lookahead_cm": [("train_controller.ato", "MIN_LOOKAHEAD_CM")],
    "lookahead_sample_count": [("train_controller.ato", "LOOKAHEAD_SAMPLE_COUNT")],
    "brake_deadband_kmh": [("train_controller.ato", "BRAKE_DEADBAND_KMH")],
    "stopped_speed_threshold_kmh": [
        ("constants", "STOPPED_SPEED_THRESHOLD_KMH"),
        ("train_controller", "STOPPED_SPEED_THRESHOLD_KMH"),
        ("train_controller.atc", "STOPPED_SPEED_THRESHOLD_KMH"),
        ("train_controller.ato", "STOPPED_SPEED_THRESHOLD_KMH"),
        ("train_physics", "STOPPED_SPEED_THRESHOLD_KMH"),
    ],
    "track_end_safety_margin_cm": [
        ("constants", "TRACK_END_SAFETY_MARGIN_CM"),
        ("train_controller", "TRACK_END_SAFETY_MARGIN_CM"),
        ("train_controller.atc", "TRACK_END_SAFETY_MARGIN_CM"),
    ],
    "platform_safety_margin_cm": [
        ("constants", "PLATFORM_SAFETY_MARGIN_CM"),
        ("train_controller", "PLATFORM_SAFETY_MARGIN_CM"),
        ("train_controller.atc", "PLATFORM_SAFETY_MARGIN_CM"),
    ],
    "caution_speed_limit_kmh": [
        ("constants", "CAUTION_SPEED_LIMIT_KMH"),
        ("train_controller", "CAUTION_SPEED_LIMIT_KMH"),
        ("train_controller.atc", "CAUTION_SPEED_LIMIT_KMH"),
    ],
    "idle_hold_release_ticks": [
        ("constants", "IDLE_HOLD_RELEASE_TICKS"),
        ("route_executor", "IDLE_HOLD_RELEASE_TICKS"),
    ],
    "idle_hold_cooldown_ticks": [
        ("constants", "IDLE_HOLD_COOLDOWN_TICKS"),
        ("route_executor", "IDLE_HOLD_COOLDOWN_TICKS"),
    ],
    # `route_executor` imports `WAYPOINT_LOOKAHEAD_BLOCKS` as its own module
    # global and reads it in the step-`66aa` waypoint-lookahead arming
    # (`_apply_waypoint_lookahead`), so a live PUT (design_new.md **Tunable
    # fields**, `waypoint_lookahead_blocks`) must rebind that mirror too or the
    # reader keeps the stale import binding.
    "waypoint_lookahead_blocks": [
        ("constants", "WAYPOINT_LOOKAHEAD_BLOCKS"),
        ("route_executor", "WAYPOINT_LOOKAHEAD_BLOCKS"),
    ],
    "delay_class_on_time_tolerance_seconds": [
        ("constants", "DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS"),
        ("route_executor", "DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS"),
    ],
    "notification_retention_rows": [
        ("constants", "NOTIFICATION_RETENTION_ROWS"),
        ("notification_center", "NOTIFICATION_RETENTION_ROWS"),
    ],
}


# --- Validation (design_new.md "Runtime settings", Validation) --------------
# After type coercion, every numeric value must be finite and satisfy these
# field rules; a value that fails is ignored exactly like a coercion failure,
# while unrelated valid fields in the same request still apply.

# Single-field lower bounds. `>= b` (inclusive) and `> b` (exclusive).
_MIN_INCLUSIVE: Dict[str, float] = {
    "overspeed_warn_threshold_kmh": 0.0,
    "atc_signal_lookahead_blocks": 1,
    "lookahead_sample_count": 1,
    "idle_hold_release_ticks": 1,
    "idle_hold_cooldown_ticks": 0,
    "waypoint_lookahead_blocks": 0,
    "notification_retention_rows": 1,
    "ato_speed_margin_kmh": 0.0,
    "lookahead_headroom": 0.0,
    "brake_deadband_kmh": 0.0,
    "track_end_safety_margin_cm": 0.0,
    "platform_safety_margin_cm": 0.0,
    "delay_class_on_time_tolerance_seconds": 0.0,
}
_MIN_EXCLUSIVE: Dict[str, float] = {
    "ato_speed_tolerance_min_kmh": 0.0,
    "ato_speed_tolerance_ratio": 0.0,
    "stopped_speed_threshold_kmh": 0.0,
    "min_lookahead_cm": 0.0,
    "caution_speed_limit_kmh": 0.0,
}
# Relational rules, each `(high_field, low_field, strict)` requiring
# `high {> | >=} low`, evaluated against the merged post-request pair. A request
# that would violate one drops BOTH its writes for that pair.
_RELATIONAL: List[Tuple[str, str, bool]] = [
    ("overspeed_eb_threshold_kmh", "overspeed_warn_threshold_kmh", True),
    ("ato_speed_tolerance_max_kmh", "ato_speed_tolerance_min_kmh", False),
]


def _passes_single_field_bound(field_name: str, value: Any) -> bool:
    """Whether `value` satisfies `field_name`'s single-field bound (or has
    none). Finiteness is checked separately by the caller."""
    if field_name in _MIN_INCLUSIVE:
        return value >= _MIN_INCLUSIVE[field_name]
    if field_name in _MIN_EXCLUSIVE:
        return value > _MIN_EXCLUSIVE[field_name]
    return True


def _drop_relational_violations(candidates: Dict[str, Any]) -> None:
    """Drop both writes of any relational pair that the merged post-request
    values would violate, in place. The merged value of a field absent from the
    request is its current live value."""
    for high, low, strict in _RELATIONAL:
        if high not in candidates and low not in candidates:
            continue
        high_val = candidates.get(high, getattr(_config, high))
        low_val = candidates.get(low, getattr(_config, low))
        ok = high_val > low_val if strict else high_val >= low_val
        if not ok:
            candidates.pop(high, None)
            candidates.pop(low, None)


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
    type, validating per design_new.md "Runtime settings" (Validation), and
    propagating surviving writes to consumer module globals.

    Unknown keys and `null` values are ignored. After coercion each value must
    be finite and satisfy its single-field bound; a failing value is dropped
    exactly like a coercion failure while other fields in the same request still
    apply. Relational rules (overspeed EB > warn; ATO tolerance max >= min) are
    evaluated against the merged post-request pair, and a request that would
    violate one drops both its writes for that pair.
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
    _drop_relational_violations(candidates)
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
