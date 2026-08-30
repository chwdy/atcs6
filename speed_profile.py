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

from constants import (
    DEFAULT_SEEDED_LIGHTING_VOLTAGE_V,
    DEFAULT_SEEDED_MAX_VOLTAGE_V,
)
from enums import HandlePosition, POWER_MULTIPLIERS, BRAKE_MULTIPLIERS


def _multipliers_from_raw(
    raw: Any, default: Dict[HandlePosition, float]
) -> Dict[HandlePosition, float]:
    """Rebuild a {HandlePosition: multiplier} table from serialized {str: float}.

    Falls back to ``default`` when the data is absent or carries no recognized
    handle keys. Unknown handle strings are skipped rather than raising.
    """
    if not isinstance(raw, dict):
        return dict(default)
    out: Dict[HandlePosition, float] = {}
    for key, value in raw.items():
        try:
            out[HandlePosition(key)] = float(value)
        except (ValueError, TypeError):
            continue
    return out if out else dict(default)


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
            usable range: lighting_voltage (1.5 V) -> 0%, max_voltage (12 V) -> 100%.
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
    # The model profile this record overlays. None on a model profile (a full
    # record describing a locomotive model); set to a model profile's id on an
    # instance profile (one physical locomotive). It is the field that says
    # which of the two kinds a record is (design_new.md "SpeedProfile", Model
    # profiles and instance profiles). An instance profile carries only its own
    # measured fields (`lighting_voltage`, `power_speed_curve`, `calibrated_at`)
    # and reads every other field from the model profile it derives from;
    # `SpeedProfileRegistry.resolve` is the one accessor that merges the two.
    derived_from: Optional[str] = None
    # The locomotive model this profile describes, written the same way
    # `RosterEntry.model` writes it (design_new.md "SpeedProfile"). It is what
    # makes a measured curve reusable: `train_roster.match_profile_for_model`
    # links a new roster entry to the model profile already measured for its
    # model, so a second locomotive of that model starts from a real curve
    # instead of the seeded linear default. Carried by model profiles only; an
    # instance leaves it unset and resolves it from its model. Blank on a model
    # profile seeded at train creation.
    model: str = ""
    # Wall-clock ISO-8601 stamp of the last `power_speed_curve` write by a
    # speed-calibration commit; None while the curve is still the seeded linear
    # default. It is the one field that says whether a profile is measured or a
    # placeholder, and it breaks ties when two model profiles name one model. It
    # is one of the three fields an instance profile may carry; None means
    # "unset -> read from the model" on an instance.
    calibrated_at: Optional[str] = None
    # One of the three instance-eligible fields. None means "unset" here: on a
    # MODEL profile __post_init__ fills the v1 linear default, so a model profile
    # always ends up with a real curve (behaviour unchanged); on an INSTANCE
    # profile None stays None and `SpeedProfileRegistry.resolve` reads the
    # model's curve instead (design_new.md "SpeedProfile", Model profiles and
    # instance profiles).
    power_speed_curve: Optional[List[Tuple[float, float]]] = None
    # Carried by model profiles only. On a model profile an empty value is
    # synthesized in __post_init__ as a linear ramp anchored to this profile's
    # own `lighting_voltage` (0%) and `max_voltage` (100%); with the default
    # lighting_voltage=1.5 / max_voltage=12.0 this is [(1.5, 0.0), (12.0, 100.0)].
    # An instance profile stores none of its own: __post_init__ forces it empty,
    # and the instance's resolved curve is the model's curve re-anchored at the
    # instance's resolved `lighting_voltage` (design_new.md "SpeedProfile", Model
    # profiles and instance profiles).
    voltage_power_curve: List[Tuple[float, float]] = field(default_factory=list)

    # Lit-but-stationary voltage; also the 0%-power anchor of
    # voltage_power_curve. Set high enough that a lit, motionless locomotive
    # draws a current its block's sense channel can actually read: that channel
    # is the only source that decides a lit block, and below the readable floor
    # it reports ~0 mA and the block rests on its clear window until it times
    # out (see `hardware.interfaces.OccupancyDriver`, lighting zone). It must
    # stay below `DEFAULT_CURRENT_SENSE_MIN_DRIVE_V`, or a standby block reads
    # as driven. One of the three instance-eligible fields: None means "unset"
    # (a MODEL profile fills the 1.5 V default in __post_init__; an INSTANCE
    # profile leaves it None and resolves it from the model).
    lighting_voltage: Optional[float] = None
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
    # Top of the usable track_voltage range for the first mapping stage
    # (design_new.md "SpeedProfile", `max_voltage` field)
    max_voltage: float = DEFAULT_SEEDED_MAX_VOLTAGE_V
    # Per-train notch mapping. Power notches scale `acceleration_rate`; brake
    # notches scale the operational decel rate (`deceleration_rate ×
    # operational_decel_ratio`). Different train models have different notch
    # response, so these live on the profile. Default to the standard table in
    # `enums`; the consumers (TrainPhysics, ATOComputer) read these, not the
    # module-level tables.
    power_multipliers: Dict[HandlePosition, float] = field(
        default_factory=lambda: dict(POWER_MULTIPLIERS)
    )
    brake_multipliers: Dict[HandlePosition, float] = field(
        default_factory=lambda: dict(BRAKE_MULTIPLIERS)
    )

    def __post_init__(self) -> None:
        """Fill model-profile defaults and sort the curves.

        A MODEL profile (`derived_from is None`) is a complete record: an unset
        `lighting_voltage` or `power_speed_curve` is filled with the v1 default,
        and an empty `voltage_power_curve` is synthesized as the linear ramp from
        `lighting_voltage` (0%) to `max_voltage` (100%), so a model profile
        behaves exactly as before.

        An INSTANCE profile (`derived_from` set) carries only its own measured
        fields; an unset one stays None ("read from the model" — design_new.md
        "SpeedProfile", Model profiles and instance profiles). An instance stores
        no `voltage_power_curve` of its own, so any supplied one is dropped and
        the resolved curve is rebuilt by `SpeedProfileRegistry.resolve`.
        """
        if self.derived_from is None:
            if self.lighting_voltage is None:
                self.lighting_voltage = DEFAULT_SEEDED_LIGHTING_VOLTAGE_V
            if self.power_speed_curve is None:
                self.power_speed_curve = [(0.0, 0.0), (100.0, 100.0)]
            if not self.voltage_power_curve:
                self.voltage_power_curve = [
                    (self.lighting_voltage, 0.0),
                    (self.max_voltage, 100.0),
                ]
            self.voltage_power_curve = sorted(
                self.voltage_power_curve, key=lambda x: x[0]
            )
        else:
            # Instance profile: no stored voltage_power_curve, unset measured
            # fields stay None.
            self.voltage_power_curve = []
        if self.power_speed_curve is not None:
            self.power_speed_curve = sorted(
                self.power_speed_curve, key=lambda x: x[0]
            )

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
            "derived_from": self.derived_from,
            "model": self.model,
            "calibrated_at": self.calibrated_at,
            "power_speed_curve": (
                None
                if self.power_speed_curve is None
                else [[p, s] for p, s in self.power_speed_curve]
            ),
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
            "power_multipliers": {
                h.value: m for h, m in self.power_multipliers.items()
            },
            "brake_multipliers": {
                h.value: m for h, m in self.brake_multipliers.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpeedProfile":
        """Reconstruct from dictionary after deserialization."""
        # None (or an absent key) is preserved as None here: __post_init__ fills
        # the v1 default for a MODEL profile and leaves it None ("read from the
        # model") for an INSTANCE profile.
        power_curve = data.get("power_speed_curve")
        if power_curve is None:
            power_tuples: Optional[List[Tuple[float, float]]] = None
        else:
            power_tuples = [(float(p), float(s)) for p, s in power_curve]

        voltage_power_raw = data.get("voltage_power_curve")
        if not voltage_power_raw:
            # Leave empty so __post_init__ synthesizes the default ramp for a
            # model profile (or keeps it empty for an instance profile).
            voltage_power_tuples: List[Tuple[float, float]] = []
        else:
            voltage_power_tuples = [(float(v), float(p)) for v, p in voltage_power_raw]

        lighting_raw = data.get("lighting_voltage")
        lighting_voltage = None if lighting_raw is None else float(lighting_raw)

        return cls(
            profile_id=data.get("profile_id", ""),
            name=data.get("name", ""),
            derived_from=data.get("derived_from") or None,
            model=str(data.get("model", "") or ""),
            calibrated_at=data.get("calibrated_at") or None,
            power_speed_curve=power_tuples,
            voltage_power_curve=voltage_power_tuples,
            lighting_voltage=lighting_voltage,
            acceleration_rate=float(data.get("acceleration_rate", 10.0)),
            deceleration_rate=float(data.get("deceleration_rate", 15.0)),
            emergency_brake_rate=float(data.get("emergency_brake_rate", 30.0)),
            friction_rate=float(data.get("friction_rate", 0.5)),
            operational_decel_ratio=float(data.get("operational_decel_ratio", 0.8)),
            air_resistance_enabled=bool(data.get("air_resistance_enabled", True)),
            drag_coefficient=float(data.get("drag_coefficient", 0.001)),
            reference_speed_kmh=float(data.get("reference_speed_kmh", 100.0)),
            max_voltage=float(data.get("max_voltage", DEFAULT_SEEDED_MAX_VOLTAGE_V)),
            power_multipliers=_multipliers_from_raw(
                data.get("power_multipliers"), POWER_MULTIPLIERS
            ),
            brake_multipliers=_multipliers_from_raw(
                data.get("brake_multipliers"), BRAKE_MULTIPLIERS
            ),
        )


DEFAULT_SPEED_PROFILE = SpeedProfile(
    profile_id="default",
    name="Default HO Scale",
    lighting_voltage=DEFAULT_SEEDED_LIGHTING_VOLTAGE_V,
    acceleration_rate=10.0,
    deceleration_rate=15.0,
    emergency_brake_rate=30.0,
)


def make_default_speed_profile(train_id: str, max_speed_kmh: float) -> SpeedProfile:
    """Build a train's default `SpeedProfile` (design_new.md "SpeedProfile").

    The `power_speed_curve` is linear from 0% -> 0 km/h to 100% -> the owning
    train's configured `max_speed`, so manual `power_percent` mode is anchored
    to THIS train's top speed instead of the module-global `DEFAULT_SPEED_PROFILE`
    whose curve maps 100% -> 100 km/h regardless of the train. This is the single
    home for that default: `SimulationController.add_train` registers one per
    train, and the `_get_speed_profile` resolvers fall back to it for trains
    created by paths that do not seed a profile (layout load, step replay,
    editor add-without-place).
    """
    # `Train.from_dict` returns None for an explicit `"max_speed": null` (the
    # 100.0 default only applies when the key is absent), and `float(None)`
    # raises. Treat a missing / non-numeric max_speed as the module-global top
    # speed so the curve stays well-formed instead of crashing.
    try:
        top_speed = float(max_speed_kmh)
    except (TypeError, ValueError):
        top_speed = 100.0
    return SpeedProfile(
        profile_id=f"default_{train_id}",
        name=f"Default ({train_id})",
        power_speed_curve=[(0.0, 0.0), (100.0, top_speed)],
    )


def reanchor_voltage_power_curve(
    curve: List[Tuple[float, float]], anchor: float, max_voltage: float
) -> List[Tuple[float, float]]:
    """A `voltage_power_curve` with its 0%-power point moved to `anchor`.

    Drops every point at or below the new anchor (each names a voltage that does
    not move this locomotive) and the old 0%-power anchor, then prepends
    `(anchor, 0%)` (design_new.md "SpeedProfile", Model profiles and instance
    profiles). This builds an instance profile's resolved curve from its model
    profile's stored curve, and is a no-op when `anchor` already equals the
    curve's bottom voltage.
    """
    kept = [
        (float(v), float(p))
        for v, p in (curve or [])
        if float(v) > anchor and float(p) > 0.0
    ]
    if not kept:
        kept = [(float(max_voltage), 100.0)]
    return [(float(anchor), 0.0)] + sorted(kept)


def make_instance_profile(
    model_profile_id: str, *, profile_id: str, name: str = ""
) -> SpeedProfile:
    """An empty instance profile deriving from `model_profile_id`.

    It carries none of its three measured fields yet (design_new.md
    "SpeedProfile", Model profiles and instance profiles): every field resolves
    from the model profile until **Speed calibration** or **Lighting-voltage
    calibration** writes a measured value onto this instance.
    """
    return SpeedProfile(
        profile_id=profile_id, name=name, derived_from=model_profile_id
    )


def seed_default_profile_pair(registry, train_id: str, max_speed_kmh) -> str:
    """Register a default model profile plus an empty instance under it.

    design_new.md "SpeedProfile": when placement seeds no profile,
    `add_train(...)` creates a model profile whose `power_speed_curve` 100% point
    is anchored to the train's `max_speed`, plus an empty instance deriving from
    it, and the train names the instance. Registers both and returns the
    instance's `profile_id`, so calibrating one locomotive never touches another
    that shares its model curve.
    """
    model = make_default_speed_profile(train_id, max_speed_kmh)
    instance = make_instance_profile(
        model.profile_id,
        profile_id=f"{model.profile_id}__instance",
        name=f"{model.name} instance",
    )
    registry.register(model)
    registry.register(instance)
    return instance.profile_id


def _overlay_instance_on_model(
    model: SpeedProfile, instance: SpeedProfile
) -> SpeedProfile:
    """The complete profile an instance resolves to over its model profile.

    design_new.md "SpeedProfile", Model profiles and instance profiles: the
    instance's measured fields where it carries them, the model's values
    everywhere else, and a `voltage_power_curve` rebuilt from the model's curve
    re-anchored at the resolved `lighting_voltage`. The result carries the
    instance's `profile_id` (so a train that names the instance still matches its
    resolved profile) and reads as a flattened model profile.
    """
    lighting = (
        instance.lighting_voltage
        if instance.lighting_voltage is not None
        else model.lighting_voltage
    )
    power_speed = (
        instance.power_speed_curve
        if instance.power_speed_curve is not None
        else model.power_speed_curve
    )
    calibrated_at = (
        instance.calibrated_at
        if instance.calibrated_at is not None
        else model.calibrated_at
    )
    voltage_power = reanchor_voltage_power_curve(
        model.voltage_power_curve, float(lighting), float(model.max_voltage)
    )
    return SpeedProfile(
        profile_id=instance.profile_id,
        name=instance.name or model.name,
        derived_from=None,
        model=model.model,
        calibrated_at=calibrated_at,
        power_speed_curve=list(power_speed) if power_speed is not None else None,
        voltage_power_curve=list(voltage_power),
        lighting_voltage=float(lighting),
        acceleration_rate=model.acceleration_rate,
        deceleration_rate=model.deceleration_rate,
        emergency_brake_rate=model.emergency_brake_rate,
        friction_rate=model.friction_rate,
        operational_decel_ratio=model.operational_decel_ratio,
        air_resistance_enabled=model.air_resistance_enabled,
        drag_coefficient=model.drag_coefficient,
        reference_speed_kmh=model.reference_speed_kmh,
        max_voltage=model.max_voltage,
        power_multipliers=dict(model.power_multipliers),
        brake_multipliers=dict(model.brake_multipliers),
    )


def _instance_without_model(instance: SpeedProfile) -> SpeedProfile:
    """A usable profile for an instance whose model profile is missing.

    design_new.md keeps every instance deriving from a live model, so this only
    guards a corrupt load (a dangling `derived_from`). It fills the instance's
    unset fields with the model-profile defaults so a consumer still gets a
    complete, drivable record instead of a partial one.
    """
    return SpeedProfile(
        profile_id=instance.profile_id,
        name=instance.name,
        derived_from=None,
        model=instance.model,
        calibrated_at=instance.calibrated_at,
        power_speed_curve=instance.power_speed_curve,
        lighting_voltage=instance.lighting_voltage,
    )


def resolve_memoized_default_profile(cache_holder, train) -> SpeedProfile:
    """Per-train default `SpeedProfile`, memoized on `cache_holder`.

    `TrainController` and `TrainPhysics` both fall back here when a train has no
    assigned / resolvable profile (layout load, step replay, editor
    add-without-place). The result is cached on the holder under
    `_default_profile_cache` and rebuilt only when the train's id or `max_speed`
    changes, so the per-tick read does not re-allocate and re-sort a
    `SpeedProfile` every tick. This is the single home for that fallback, so the
    controller and physics read paths cannot drift.
    """
    key = (train.train_id, train.max_speed)
    cache = getattr(cache_holder, "_default_profile_cache", None)
    if cache is None or cache[0] != key:
        cache = (key, make_default_speed_profile(train.train_id, train.max_speed))
        cache_holder._default_profile_cache = cache
    return cache[1]


class SpeedProfileRegistry:
    def __init__(self):
        self._profiles: Dict[str, SpeedProfile] = {}
        self._profiles["default"] = DEFAULT_SPEED_PROFILE

    def register(self, profile: SpeedProfile) -> None:
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> Optional[SpeedProfile]:
        return self._profiles.get(profile_id)

    def resolve(self, profile_id: str) -> Optional[SpeedProfile]:
        """Return a fully-filled `SpeedProfile` for `profile_id`.

        For a model profile (`derived_from is None`) this is the profile itself,
        unchanged. For an instance profile it is the overlay: the instance's
        measured fields where the instance carries them, the model profile's
        values everywhere else, and a `voltage_power_curve` rebuilt from the
        model's curve re-anchored at the resolved `lighting_voltage`.

        design_new.md "SpeedProfile", Model profiles and instance profiles: this
        is the one accessor every consumer that drives a train reads through, so
        a partial instance record is never consumed directly. `get(...)` still
        returns the raw stored record, which the calibration and editor write
        paths need.
        """
        profile = self._profiles.get(profile_id)
        if profile is None:
            return None
        if profile.derived_from is None:
            return profile
        model = self._profiles.get(profile.derived_from)
        if model is None:
            return _instance_without_model(profile)
        return _overlay_instance_on_model(model, profile)

    def list_profiles(self) -> List[str]:
        return list(self._profiles.keys())

    def remove(self, profile_id: str) -> bool:
        if profile_id == "default":
            return False
        if profile_id in self._profiles:
            del self._profiles[profile_id]
            return True
        return False

    def remove_pair(self, profile_id: str) -> bool:
        """Remove `profile_id` and, when it is an instance, its now-orphaned model.

        The model profile an instance derives from is removed only when no other
        registered profile still derives from it, so a model shared by several
        instances stays in place. Returns True when the named profile was
        removed. Used when a train is retired (adoption replaces an unidentified
        train's seeded pair — design_new.md "Train Entity").
        """
        profile = self._profiles.get(profile_id)
        if profile is None:
            return False
        removed = self.remove(profile_id)
        model_id = getattr(profile, "derived_from", None)
        if model_id and not any(
            getattr(p, "derived_from", None) == model_id
            for p in self._profiles.values()
        ):
            self.remove(model_id)
        return removed
