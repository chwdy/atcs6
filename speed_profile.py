"""
Speed Profile Management

Definition:
This module defines the SpeedProfile class which maps a train's target speed (km/h)
to the voltage that should be output to the blocks it occupies. Each train model
has unique motor characteristics represented by a speed-voltage curve.

The module also provides:
- Interpolation methods for speed → voltage conversion
- Default profile for trains without custom calibration
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Any, Optional


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
        power_speed_curve: List of (power_percent, speed_kmh) calibration points
            for manual `power_percent` mode. Default is linear 0-100% -> 0-100 km/h.
        startup_voltage: Minimum voltage to overcome static friction
        lighting_voltage: Voltage for lights without movement (READY state)
        acceleration_rate: Speed increase rate (km/h per second)
        deceleration_rate: Service brake rate (km/h per second)
        emergency_brake_rate: Emergency brake rate (km/h per second)
        friction_rate: Coasting deceleration rate (km/h per second) when handle is N
    """

    profile_id: str
    name: str
    speed_voltage_curve: List[Tuple[float, float]] = field(default_factory=list)
    power_speed_curve: List[Tuple[float, float]] = field(
        default_factory=lambda: [(0.0, 0.0), (100.0, 100.0)]
    )

    startup_voltage: float = 2.0  # Voltage to overcome static friction
    lighting_voltage: float = 2.5  # Voltage for lights without movement
    acceleration_rate: float = 10.0  # km/h per second
    deceleration_rate: float = 15.0  # km/h per second (service brake)
    emergency_brake_rate: float = 30.0  # km/h per second
    friction_rate: float = 0.5  # km/h per second (coasting deceleration)
    operational_decel_ratio: float = 0.6  # Use 60% of max brake for smoother ATO curve
    # Air resistance simulation (quadratic drag model)
    # Applied to all running trains for realistic simulation
    # Set air_resistance_enabled=False or drag_coefficient=0.0 to disable (for real train operation)
    air_resistance_enabled: bool = True  # Master switch
    drag_coefficient: float = 0.001  # Quadratic drag coefficient
    reference_speed_kmh: float = 100.0  # Speed at which drag = drag_coefficient km/h/s

    def __post_init__(self) -> None:
        """Ensure both calibration curves are sorted by their independent axis."""
        if self.speed_voltage_curve:
            self.speed_voltage_curve = sorted(
                self.speed_voltage_curve, key=lambda x: x[0]
            )
        if self.power_speed_curve:
            self.power_speed_curve = sorted(
                self.power_speed_curve, key=lambda x: x[0]
            )

    def disable_air_resistance(self) -> None:
        """Disable air resistance for real train operation.

        Call this method on a train's speed profile when connecting to a real train
        to disable simulation-only air resistance.

        Example:
            profile = network.speed_profile_registry.get("my_profile")
            profile.disable_air_resistance()
        """
        self.air_resistance_enabled = False
        self.drag_coefficient = 0.0

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

    def power_percent_to_speed(self, percent: float) -> float:
        """Convert manual power percentage (0-100) to target speed (km/h).

        Used by manual `power_percent` mode. Linear interpolation between
        calibration points; clamps at curve bounds.
        """
        if not self.power_speed_curve:
            return 0.0

        if percent <= 0:
            return 0.0

        curve = self.power_speed_curve

        if percent <= curve[0][0]:
            return curve[0][1]

        if percent >= curve[-1][0]:
            return curve[-1][1]

        for i in range(len(curve) - 1):
            p1, s1 = curve[i]
            p2, s2 = curve[i + 1]
            if p1 <= percent <= p2:
                return self._interpolate(p1, s1, p2, s2, percent)

        return curve[-1][1]

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
            "power_speed_curve": [[p, s] for p, s in self.power_speed_curve],
            "startup_voltage": self.startup_voltage,
            "lighting_voltage": self.lighting_voltage,
            "acceleration_rate": self.acceleration_rate,
            "deceleration_rate": self.deceleration_rate,
            "emergency_brake_rate": self.emergency_brake_rate,
            "friction_rate": self.friction_rate,
            "operational_decel_ratio": self.operational_decel_ratio,
            "air_resistance_enabled": self.air_resistance_enabled,
            "drag_coefficient": self.drag_coefficient,
            "reference_speed_kmh": self.reference_speed_kmh,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpeedProfile":
        """Reconstruct from dictionary after deserialization."""
        curve = data.get("speed_voltage_curve", [])
        # Convert from list of lists to list of tuples
        curve_tuples = [(float(s), float(v)) for s, v in curve]

        power_curve = data.get("power_speed_curve")
        if power_curve is None:
            power_tuples = [(0.0, 0.0), (100.0, 100.0)]
        else:
            power_tuples = [(float(p), float(s)) for p, s in power_curve]

        return cls(
            profile_id=data.get("profile_id", ""),
            name=data.get("name", ""),
            speed_voltage_curve=curve_tuples,
            power_speed_curve=power_tuples,
            startup_voltage=float(data.get("startup_voltage", 2.0)),
            lighting_voltage=float(data.get("lighting_voltage", 2.5)),
            acceleration_rate=float(data.get("acceleration_rate", 10.0)),
            deceleration_rate=float(data.get("deceleration_rate", 15.0)),
            emergency_brake_rate=float(data.get("emergency_brake_rate", 30.0)),
            friction_rate=float(data.get("friction_rate", 0.5)),
            operational_decel_ratio=float(data.get("operational_decel_ratio", 0.6)),
            air_resistance_enabled=bool(data.get("air_resistance_enabled", True)),
            drag_coefficient=float(data.get("drag_coefficient", 0.001)),
            reference_speed_kmh=float(data.get("reference_speed_kmh", 100.0)),
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
    startup_voltage=2.0,
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
