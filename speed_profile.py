"""
Speed Profile Management

Definition:
This module defines the SpeedProfile class which maps a train's target speed (km/h)
to the voltage that should be output to the blocks it occupies. Each train model
has unique motor characteristics represented by a speed-voltage curve.

The module also provides:
- Interpolation methods for speed ↔ voltage conversion
- Acceleration/deceleration calculations for speed ramping
- Default profile for trains without custom calibration
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Any, Optional
import math

from model_scale import get_speed_conversion_factor


@dataclass
class SpeedProfile:
    """
    Maps target speed to voltage output for DC analog control.

    Each train has unique motor characteristics - the speed-voltage curve
    captures this relationship through calibration points that are interpolated.

    Attributes:
        profile_id: Unique identifier for this profile
        name: Display name (e.g., "HO Generic", "N Scale Slow")
        speed_voltage_curve: List of (speed_kmh, voltage) calibration points
        min_start_voltage: Minimum voltage to overcome static friction
        lighting_voltage: Voltage for lights without movement (IDLE/STANDBY states)
        acceleration_rate: Speed increase rate (km/h per second)
        deceleration_rate: Service brake rate (km/h per second)
        emergency_brake_rate: Emergency brake rate (km/h per second)
        friction_rate: Coasting deceleration rate (km/h per second) when handle is N
    """

    profile_id: str
    name: str
    speed_voltage_curve: List[Tuple[float, float]] = field(default_factory=list)

    min_start_voltage: float = 2.0  # Voltage to overcome static friction
    lighting_voltage: float = 2.5  # Voltage for lights without movement
    acceleration_rate: float = 10.0  # km/h per second
    deceleration_rate: float = 15.0  # km/h per second (service brake)
    emergency_brake_rate: float = 30.0  # km/h per second
    friction_rate: float = 0.5  # km/h per second (coasting deceleration)
    operational_decel_ratio: float = (
        0.5  # Use 50% of max brake for ATC operational braking
    )

    def __post_init__(self):
        """Ensure speed_voltage_curve is sorted by speed."""
        if self.speed_voltage_curve:
            self.speed_voltage_curve = sorted(
                self.speed_voltage_curve, key=lambda x: x[0]
            )

    def speed_to_voltage(self, speed_kmh: float) -> float:
        """
        Convert target speed to required voltage (interpolated).

        Uses linear interpolation between calibration points.
        Returns 0V for 0 speed, and clamps to curve bounds.

        Args:
            speed_kmh: Target speed in km/h

        Returns:
            Voltage (0-12V typically) required for that speed
        """
        if not self.speed_voltage_curve:
            # No curve defined - linear approximation (12V = 120 km/h)
            return min(12.0, max(0.0, speed_kmh / 10.0))

        if speed_kmh <= 0:
            return 0.0

        # Find bracketing points for interpolation
        curve = self.speed_voltage_curve

        # Below minimum point
        if speed_kmh <= curve[0][0]:
            if curve[0][0] == 0:
                return curve[0][1]
            # Extrapolate from first two points
            if len(curve) > 1:
                return self._interpolate(0, 0, curve[0][0], curve[0][1], speed_kmh)
            return curve[0][1]

        # Above maximum point
        if speed_kmh >= curve[-1][0]:
            return curve[-1][1]

        # Find bracketing points
        for i in range(len(curve) - 1):
            s1, v1 = curve[i]
            s2, v2 = curve[i + 1]
            if s1 <= speed_kmh <= s2:
                return self._interpolate(s1, v1, s2, v2, speed_kmh)

        return curve[-1][1]

    def voltage_to_speed(self, voltage: float) -> float:
        """
        Convert voltage to expected speed (interpolated).

        Inverse of speed_to_voltage - useful for feedback/display.

        Args:
            voltage: Input voltage (0-12V typically)

        Returns:
            Expected speed in km/h
        """
        if not self.speed_voltage_curve:
            # No curve defined - linear approximation
            return max(0.0, voltage * 10.0)

        if voltage <= 0:
            return 0.0

        # Build inverse curve (voltage → speed)
        inverse_curve = [(v, s) for s, v in self.speed_voltage_curve]
        inverse_curve.sort(key=lambda x: x[0])

        # Below minimum voltage
        if voltage <= inverse_curve[0][0]:
            if inverse_curve[0][0] == 0:
                return inverse_curve[0][1]
            return 0.0

        # Above maximum voltage
        if voltage >= inverse_curve[-1][0]:
            return inverse_curve[-1][1]

        # Find bracketing points
        for i in range(len(inverse_curve) - 1):
            v1, s1 = inverse_curve[i]
            v2, s2 = inverse_curve[i + 1]
            if v1 <= voltage <= v2:
                return self._interpolate(v1, s1, v2, s2, voltage)

        return inverse_curve[-1][1]

    def calculate_new_speed(
        self,
        current_speed: float,
        target_speed: float,
        delta_time: float,
        is_emergency: bool = False,
    ) -> float:
        """
        Calculate speed after delta_time given acceleration/deceleration.

        Applies appropriate rate based on whether accelerating or braking,
        and whether emergency braking is active.

        Args:
            current_speed: Current speed in km/h
            target_speed: Target speed in km/h
            delta_time: Time step in seconds
            is_emergency: Whether to use emergency brake rate

        Returns:
            New speed in km/h (clamped to non-negative)
        """
        if abs(current_speed - target_speed) < 0.01:
            return target_speed

        if current_speed < target_speed:
            # Accelerating
            rate = self.acceleration_rate
            new_speed = current_speed + (rate * delta_time)
            return min(new_speed, target_speed)
        else:
            # Decelerating
            rate = self.emergency_brake_rate if is_emergency else self.deceleration_rate
            new_speed = current_speed - (rate * delta_time)
            return max(new_speed, target_speed, 0.0)

    def get_braking_distance_cm(
        self,
        current_speed: float,
        target_speed: float = 0.0,
        is_emergency: bool = False,
    ) -> float:
        """
        Calculate braking distance in centimeters.

        Uses physics: d = (v_initial² - v_final²) / (2 * a)
        where a is deceleration rate.

        Args:
            current_speed: Current speed in km/h
            target_speed: Target speed in km/h (default 0 = stop)
            is_emergency: Whether to use emergency brake rate

        Returns:
            Braking distance in cm
        """
        if current_speed <= target_speed:
            return 0.0

        # Get deceleration rate in km/h per second
        rate = self.emergency_brake_rate if is_emergency else self.deceleration_rate
        if rate <= 0:
            return float("inf")

        km_h_to_cm_s = get_speed_conversion_factor()

        v_initial_cm_s = current_speed * km_h_to_cm_s
        v_final_cm_s = target_speed * km_h_to_cm_s

        # Deceleration in cm/s²: km/h/s → cm/s² = multiply by km_h_to_cm_s
        decel_cm_s2 = rate * km_h_to_cm_s

        # d = (v_i² - v_f²) / (2 * a)
        distance_cm = (v_initial_cm_s**2 - v_final_cm_s**2) / (2 * decel_cm_s2)

        return max(0.0, distance_cm)

    def get_time_to_speed(
        self, current_speed: float, target_speed: float, is_emergency: bool = False
    ) -> float:
        """
        Calculate time in seconds to reach target speed.

        Args:
            current_speed: Current speed in km/h
            target_speed: Target speed in km/h
            is_emergency: Whether to use emergency brake rate (for decel)

        Returns:
            Time in seconds
        """
        speed_diff = abs(target_speed - current_speed)
        if speed_diff < 0.01:
            return 0.0

        if target_speed > current_speed:
            rate = self.acceleration_rate
        else:
            rate = self.emergency_brake_rate if is_emergency else self.deceleration_rate

        if rate <= 0:
            return float("inf")

        return speed_diff / rate

    def _interpolate(
        self, x1: float, y1: float, x2: float, y2: float, x: float
    ) -> float:
        """Linear interpolation between two points."""
        if x2 == x1:
            return y1
        t = (x - x1) / (x2 - x1)
        return y1 + t * (y2 - y1)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "speed_voltage_curve": [[s, v] for s, v in self.speed_voltage_curve],
            "min_start_voltage": self.min_start_voltage,
            "lighting_voltage": self.lighting_voltage,
            "acceleration_rate": self.acceleration_rate,
            "deceleration_rate": self.deceleration_rate,
            "emergency_brake_rate": self.emergency_brake_rate,
            "friction_rate": self.friction_rate,
            "operational_decel_ratio": self.operational_decel_ratio,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpeedProfile":
        """Reconstruct from dictionary after deserialization."""
        curve = data.get("speed_voltage_curve", [])
        # Convert from list of lists to list of tuples
        curve_tuples = [(float(s), float(v)) for s, v in curve]

        return cls(
            profile_id=data.get("profile_id", ""),
            name=data.get("name", ""),
            speed_voltage_curve=curve_tuples,
            min_start_voltage=float(data.get("min_start_voltage", 2.0)),
            lighting_voltage=float(data.get("lighting_voltage", 2.5)),
            acceleration_rate=float(data.get("acceleration_rate", 10.0)),
            deceleration_rate=float(data.get("deceleration_rate", 15.0)),
            emergency_brake_rate=float(data.get("emergency_brake_rate", 30.0)),
            friction_rate=float(data.get("friction_rate", 0.5)),
            operational_decel_ratio=float(data.get("operational_decel_ratio", 0.5)),
        )


DEFAULT_SPEED_PROFILE = SpeedProfile(
    profile_id="default",
    name="Default HO Scale",
    speed_voltage_curve=[
        (0.0, 0.0),
        (10.0, 3.0),
        (30.0, 5.0),
        (60.0, 8.0),
        (100.0, 11.0),
        (120.0, 12.0),
    ],
    min_start_voltage=2.0,
    lighting_voltage=2.5,
    acceleration_rate=10.0,
    deceleration_rate=15.0,
    emergency_brake_rate=30.0,
)


class SpeedProfileRegistry:
    def __init__(self):
        self._profiles: Dict[str, SpeedProfile] = {}
        self._profiles["default"] = DEFAULT_SPEED_PROFILE

    def register(self, profile: SpeedProfile) -> None:
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> Optional[SpeedProfile]:
        return self._profiles.get(profile_id)

    def get_default(self) -> SpeedProfile:
        return DEFAULT_SPEED_PROFILE

    def list_profiles(self) -> List[str]:
        return list(self._profiles.keys())

    def remove(self, profile_id: str) -> bool:
        if profile_id == "default":
            return False
        if profile_id in self._profiles:
            del self._profiles[profile_id]
            return True
        return False
