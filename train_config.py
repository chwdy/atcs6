"""
Train System Configuration Constants

Definition:
This module contains all configuration constants for the train control system.
These values can be adjusted to tune the system behavior.
"""

# =============================================================================
# Track Layout Configuration
# =============================================================================
DEFAULT_TILE_LENGTH_CM = 1000.0  # 10m per tile (scale: 1:87 HO = 115mm real = ~10m)

# =============================================================================
# Speed Limits
# =============================================================================
DEFAULT_MAX_SPEED_KMH = 100.0
YELLOW_SIGNAL_SPEED_KMH = 40.0  # Speed limit when approaching yellow signal
STOPPED_SPEED_THRESHOLD_KMH = 0.5  # Below this = considered stopped

# =============================================================================
# ATC Configuration
# =============================================================================
ATC_SAFETY_MARGIN_CM = 50.0  # Buffer distance before obstruction
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
DEFAULT_ACCELERATION_RATE = 10.0  # km/h per second
DEFAULT_DECELERATION_RATE = 15.0  # km/h per second (service brake)
DEFAULT_EMERGENCY_BRAKE_RATE = 30.0  # km/h per second

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


def kmh_to_cm_per_sec(kmh: float) -> float:
    """Convert km/h to cm/s."""
    return kmh * 100000 / 3600  # ≈ kmh * 27.78


def cm_per_sec_to_kmh(cm_s: float) -> float:
    """Convert cm/s to km/h."""
    return cm_s * 3600 / 100000
