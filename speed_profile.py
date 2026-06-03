"""
Speed Profile Management

Definition:
This module defines the SpeedProfile class which maps a train's target speed (km/h)
to the track voltage output for the blocks it occupies, through a two-stage
calibration: `track_voltage <-> power_percent` (voltage_power_curve) and
`power_percent <-> speed_kmh` (power_speed_curve). Each train model has unique
motor characteristics represented by these calibration curves.

The module also provides:
- Interpolation methods for the two-stage speed <-> voltage conversion
- Default profile for trains without custom calibration
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Any, Optional


@dataclass
class SpeedProfile:
    """
    Maps target speed to voltage output for DC analog control.

    Each train has unique motor characteristics - the two-stage calibration
    (voltage_power_curve and power_speed_curve) captures this relationship
    through calibration points that are interpolated.

    Attributes:
        profile_id: Unique identifier for this profile
        name: Display name (e.g., "HO Generic", "N Scale Slow")
        power_speed_curve: List of (power_percent, speed_kmh) calibration points
            for the second stage (`power_percent <-> speed_kmh`), also used by
            manual `power_percent` mode. Default is linear 0-100% -> 0-100 km/h.
        voltage_power_curve: List of (voltage, power_percent) calibration points
            for the first stage (`track_voltage <-> power_percent`). Voltage
            ascending, power_percent ascending. Default is linear across the
            usable range: lighting_voltage (2 V) -> 0%, max_voltage (12 V) -> 100%.
        lighting_voltage: Voltage at which the train is lit but still holds
            (the stopped-but-lit states use it) and the 0%-power point of
            voltage_power_curve. At exactly this voltage power is 0 and the
            train does not move; just above it the train begins to move.
        acceleration_rate: Speed increase rate (km/h per second)
        deceleration_rate: Service brake rate (km/h per second)
        emergency_brake_rate: Emergency brake rate (km/h per second)
        friction_rate: Coasting deceleration rate (km/h per second) when handle is N
    """

    profile_id: str
    name: str
    power_speed_curve: List[Tuple[float, float]] = field(
        default_factory=lambda: [(0.0, 0.0), (100.0, 100.0)]
    )
    voltage_power_curve: List[Tuple[float, float]] = field(
        default_factory=lambda: [(2.0, 0.0), (12.0, 100.0)]
    )

    lighting_voltage: float = 2.0  # Lit-but-stationary voltage; also the 0%-power anchor of voltage_power_curve
    acceleration_rate: float = 10.0  # km/h per second
    deceleration_rate: float = 15.0  # km/h per second (service brake)
    emergency_brake_rate: float = 30.0  # km/h per second
    friction_rate: float = 0.5  # km/h per second (coasting deceleration)
    operational_decel_ratio: float = 0.8  # Use 80% of max brake for smoother ATO curve
    # Air resistance simulation (quadratic drag model)
    # Applied to all running trains for realistic simulation
    # Set air_resistance_enabled=False or drag_coefficient=0.0 to disable (for real train operation)
    air_resistance_enabled: bool = True  # Master switch
    drag_coefficient: float = 0.001  # Quadratic drag coefficient
    reference_speed_kmh: float = 100.0  # Speed at which drag = drag_coefficient km/h/s
    # Top of the usable track_voltage range for the first mapping stage (design line 2028)
    max_voltage: float = 12.0

    def __post_init__(self) -> None:
        """Ensure all calibration curves are sorted by their independent axis."""
        if self.power_speed_curve:
            self.power_speed_curve = sorted(
                self.power_speed_curve, key=lambda x: x[0]
            )
        if self.voltage_power_curve:
            self.voltage_power_curve = sorted(
                self.voltage_power_curve, key=lambda x: x[0]
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

    def voltage_to_power(self, voltage: float) -> float:
        """Convert track voltage to power percent using voltage_power_curve (stage 1).

        Linear interpolation through the curve; clamped at endpoints.

        Args:
            voltage: Track voltage (V).

        Returns:
            Power percent (0–100).
        """
        curve = self.voltage_power_curve
        if not curve:
            return 0.0
        if voltage <= curve[0][0]:
            return curve[0][1]
        if voltage >= curve[-1][0]:
            return curve[-1][1]
        for i in range(len(curve) - 1):
            v1, p1 = curve[i]
            v2, p2 = curve[i + 1]
            if v1 <= voltage <= v2:
                return self._interpolate(v1, p1, v2, p2, voltage)
        return curve[-1][1]

    def power_to_voltage(self, power_percent: float) -> float:
        """Convert power percent to track voltage using voltage_power_curve (stage 1).

        Linear interpolation through the curve (inverted axis); clamped at endpoints.

        Args:
            power_percent: Power percent (0–100).

        Returns:
            Track voltage (V).
        """
        curve = self.voltage_power_curve
        if not curve:
            return 0.0
        if power_percent <= curve[0][1]:
            return curve[0][0]
        if power_percent >= curve[-1][1]:
            return curve[-1][0]
        for i in range(len(curve) - 1):
            v1, p1 = curve[i]
            v2, p2 = curve[i + 1]
            if p1 <= power_percent <= p2:
                return self._interpolate(p1, v1, p2, v2, power_percent)
        return curve[-1][0]

    def speed_to_voltage_two_stage(self, speed_kmh: float) -> float:
        """Convert target speed to voltage via the two-stage pipeline.

        Stage 2: speed_kmh -> power_percent (power_speed_curve inverted).
        Stage 1: power_percent -> voltage (voltage_power_curve inverted).

        A commanded speed of 0 (or below) means the train is not being driven
        forward, so the output is 0 V (the `lighting_voltage` floor applies
        only to the stopped-but-lit states, not to this driven path).

        Args:
            speed_kmh: Target speed in km/h.

        Returns:
            Track voltage (V).
        """
        if speed_kmh <= 0:
            return 0.0
        # Invert power_speed_curve: speed -> power_percent
        power_percent = self._speed_to_power(speed_kmh)
        return self.power_to_voltage(power_percent)

    def voltage_to_speed_two_stage(self, voltage: float) -> float:
        """Convert track voltage to speed via the two-stage pipeline.

        Stage 1: voltage -> power_percent (voltage_power_curve).
        Stage 2: power_percent -> speed_kmh (power_speed_curve).

        Args:
            voltage: Track voltage (V).

        Returns:
            Speed in km/h.
        """
        power_percent = self.voltage_to_power(voltage)
        return self.power_percent_to_speed(power_percent)

    def _speed_to_power(self, speed_kmh: float) -> float:
        """Invert power_speed_curve to get power_percent from speed_kmh.

        Linear interpolation; clamped at endpoints.
        """
        curve = self.power_speed_curve
        if not curve:
            return 0.0
        if speed_kmh <= 0:
            return 0.0
        if speed_kmh <= curve[0][1]:
            return curve[0][0]
        if speed_kmh >= curve[-1][1]:
            return curve[-1][0]
        for i in range(len(curve) - 1):
            p1, s1 = curve[i]
            p2, s2 = curve[i + 1]
            if s1 <= speed_kmh <= s2:
                return self._interpolate(s1, p1, s2, p2, speed_kmh)
        return curve[-1][0]

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
            "power_speed_curve": [[p, s] for p, s in self.power_speed_curve],
            "voltage_power_curve": [[v, p] for v, p in self.voltage_power_curve],
            "lighting_voltage": self.lighting_voltage,
            "max_voltage": self.max_voltage,
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
        power_curve = data.get("power_speed_curve")
        if power_curve is None:
            power_tuples = [(0.0, 0.0), (100.0, 100.0)]
        else:
            power_tuples = [(float(p), float(s)) for p, s in power_curve]

        voltage_power_raw = data.get("voltage_power_curve")
        if voltage_power_raw is None:
            voltage_power_tuples: List[Tuple[float, float]] = [
                (2.0, 0.0), (12.0, 100.0)
            ]
        else:
            voltage_power_tuples = [(float(v), float(p)) for v, p in voltage_power_raw]

        return cls(
            profile_id=data.get("profile_id", ""),
            name=data.get("name", ""),
            power_speed_curve=power_tuples,
            voltage_power_curve=voltage_power_tuples,
            lighting_voltage=float(data.get("lighting_voltage", 2.0)),
            acceleration_rate=float(data.get("acceleration_rate", 10.0)),
            deceleration_rate=float(data.get("deceleration_rate", 15.0)),
            emergency_brake_rate=float(data.get("emergency_brake_rate", 30.0)),
            friction_rate=float(data.get("friction_rate", 0.5)),
            operational_decel_ratio=float(data.get("operational_decel_ratio", 0.8)),
            air_resistance_enabled=bool(data.get("air_resistance_enabled", True)),
            drag_coefficient=float(data.get("drag_coefficient", 0.001)),
            reference_speed_kmh=float(data.get("reference_speed_kmh", 100.0)),
            max_voltage=float(data.get("max_voltage", 12.0)),
        )


DEFAULT_SPEED_PROFILE = SpeedProfile(
    profile_id="default",
    name="Default HO Scale",
    lighting_voltage=2.0,
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
