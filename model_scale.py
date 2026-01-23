"""
Model Scale Configuration

Definition:
This module provides model scale configuration and speed conversion utilities
used across train physics and control.
"""

# =============================================================================
# Model Scale Configuration
# =============================================================================
_MODEL_SCALE = 160.0  # Default: N scale (1:160)


def get_model_scale() -> float:
    """Get the current model scale ratio."""
    return _MODEL_SCALE


def set_model_scale(scale: float) -> None:
    """Set the model scale ratio (e.g., 160 for N scale, 87 for HO)."""
    global _MODEL_SCALE
    if scale <= 0:
        raise ValueError("Scale must be positive")
    _MODEL_SCALE = scale


SCALE_PRESETS = {
    "1:1": 1.0,
    "1:22.5": 22.5,
    "1:43.5": 43.5,
    "1:64": 64.0,
    "1:87": 87.0,
    "1:120": 120.0,
    "1:148": 148.0,
    "1:160": 160.0,
    "1:220": 220.0,
}


def kmh_to_cm_per_sec(kmh: float, apply_scale: bool = True) -> float:
    """Convert km/h to cm/s, optionally applying model scale."""
    base_cm_s = kmh * 100000 / 3600
    if apply_scale:
        return base_cm_s / _MODEL_SCALE
    return base_cm_s


def cm_per_sec_to_kmh(cm_s: float, apply_scale: bool = True) -> float:
    """Convert cm/s to km/h, optionally applying model scale."""
    base_kmh = cm_s * 3600 / 100000
    if apply_scale:
        return base_kmh * _MODEL_SCALE
    return base_kmh


def get_speed_conversion_factor() -> float:
    """Get the km/h to cm/s conversion factor including scale."""
    return 100000 / 3600 / _MODEL_SCALE
