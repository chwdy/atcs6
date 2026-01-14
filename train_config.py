"""
Train System Configuration Constants

Definition:
This module contains all configuration constants for the train control system.
These values can be adjusted to tune the system behavior.

Model Scale:
The MODEL_SCALE setting determines the ratio between prototype (real-world) and model.
- 1:160 (N scale): MODEL_SCALE = 160
- 1:87 (HO scale): MODEL_SCALE = 87
- 1:1 (full scale simulation): MODEL_SCALE = 1

When scale is applied:
- Speeds are divided by scale (60 km/h prototype = 0.375 km/h model at N scale)
- This affects how fast trains appear to move on the layout
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


# Common scale presets
SCALE_PRESETS = {
    "1:1": 1.0,
    "1:22.5": 22.5,  # G scale
    "1:43.5": 43.5,  # O scale
    "1:64": 64.0,  # S scale
    "1:87": 87.0,  # HO scale
    "1:120": 120.0,  # TT scale
    "1:148": 148.0,  # N scale (UK)
    "1:160": 160.0,  # N scale
    "1:220": 220.0,  # Z scale
}

# =============================================================================
# Track Layout Configuration
# =============================================================================
# NOTE: DEFAULT_TILE_LENGTH_CM was removed - blocks, stations, and switches must have
# explicit length_cm configuration. No default fallback values are provided.
# This ensures configuration errors are caught early via warnings.

# =============================================================================
# NOTE: DEFAULT_TILE_LENGTH_CM was removed - blocks, stations, and switches must have
# explicit length_cm configuration. No default fallback values are provided.
# This ensures configuration errors are caught early via warnings.

# =============================================================================
# Speed Limits
# =============================================================================
DEFAULT_MAX_SPEED_KMH = 100.0
YELLOW_SIGNAL_SPEED_KMH = 40.0  # Speed limit when approaching yellow signal
STOPPED_SPEED_THRESHOLD_KMH = 0.5  # Below this = considered stopped

# =============================================================================
# ATC Configuration
# =============================================================================
ATC_SAFETY_MARGIN_CM = 25.0  # Buffer distance before obstruction (general)
ATC_PLATFORM_SAFETY_MARGIN_CM = 5.0  # Buffer for platform/destination stops
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3  # How many blocks ahead to check signals
ATC_MIN_CLEARANCE_CM = 10.0  # Minimum clearance to allow any movement
ATC_NO_BLOCK_FALLBACK_CM = 100000.0  # 1km - clearance when no block is configured

# =============================================================================
# Voltage Configuration
# =============================================================================
MAX_TRACK_VOLTAGE = 12.0
MIN_TRACK_VOLTAGE = 0.0
DEFAULT_MIN_START_VOLTAGE = 2.0  # Voltage to overcome static friction
DEFAULT_LIGHTING_VOLTAGE = 2.5  # Voltage for lights without movement

# =============================================================================
# Timing Configuration
# =============================================================================
DEFAULT_DWELL_TIME_SEC = 30.0  # Default station stop duration

# =============================================================================
# Physics Defaults
# =============================================================================
DEFAULT_ACCELERATION_RATE = 20.0  # km/h per second (0-60 km/h in 3 seconds)
DEFAULT_DECELERATION_RATE = 30.0  # km/h per second (60-0 km/h in 2 seconds)
DEFAULT_EMERGENCY_BRAKE_RATE = 50.0  # km/h per second (60-0 km/h in 1.2 seconds)

# =============================================================================
# Train Defaults
# =============================================================================
DEFAULT_MIN_RADIUS_CM = 30.0
DEFAULT_TRAIN_LENGTH_CM = 50.0  # Default train length if not specified

# =============================================================================
# Default Speed Profile
# =============================================================================
DEFAULT_SPEED_PROFILE_ID = "default"
DEFAULT_SPEED_PROFILE_NAME = "Default Profile"
DEFAULT_SPEED_VOLTAGE_CURVE = [
    (0.0, 0.0),
    (20.0, 4.0),
    (40.0, 6.0),
    (60.0, 8.0),
    (80.0, 10.0),
    (100.0, 11.0),
    (120.0, 12.0),
]

# =============================================================================
# Unit Conversions
# =============================================================================


def kmh_to_cm_per_sec(kmh: float, apply_scale: bool = True) -> float:
    """Convert km/h to cm/s, optionally applying model scale.

    Args:
        kmh: Speed in km/h (prototype scale)
        apply_scale: If True, divide by model scale for scaled speed
    """
    base_cm_s = kmh * 100000 / 3600
    if apply_scale:
        return base_cm_s / _MODEL_SCALE
    return base_cm_s


def cm_per_sec_to_kmh(cm_s: float, apply_scale: bool = True) -> float:
    """Convert cm/s to km/h, optionally applying model scale.

    Args:
        cm_s: Speed in cm/s (at model scale if apply_scale was used)
        apply_scale: If True, multiply by model scale to get prototype speed
    """
    base_kmh = cm_s * 3600 / 100000
    if apply_scale:
        return base_kmh * _MODEL_SCALE
    return base_kmh


def get_speed_conversion_factor() -> float:
    """Get the km/h to cm/s conversion factor including scale."""
    return 100000 / 3600 / _MODEL_SCALE
