"""
Speed Profile Management

Definition:
This module defines the SpeedProfile class: one locomotive's measured response
to track voltage, stored as `voltage_speed_curve` — `(track_voltage, speed_kmh)`
points in order of rising voltage (design_new.md "SpeedProfile").

Three values are joined by two mappings (design_new.md "SpeedProfile",
Two mappings join the three values):
- `track_voltage <-> speed_kmh` — measured, read through `voltage_speed_curve`;
- `power_percent <-> speed_kmh` — defined, never stored: `0%` is stopped and
  above `0` it is a straight line in speed from the starting speed to the top
  speed (design_new.md "SpeedProfile", Power percent is a share of the speed
  range).

The curve's first point is the starting voltage and starting speed — the
lowest voltage at which the locomotive moves steadily, and its speed there
(design_new.md "SpeedProfile", Starting voltage and starting speed).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Any, Optional

from constants import (
    DEFAULT_SEEDED_MAX_VOLTAGE_V,
    DEFAULT_SEEDED_STARTING_VOLTAGE_V,
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


def seeded_voltage_speed_curve(
    max_speed_kmh: float, max_voltage: float = DEFAULT_SEEDED_MAX_VOLTAGE_V
) -> List[Tuple[float, float]]:
    """The seeded default `voltage_speed_curve` (design_new.md "SpeedProfile",
    `voltage_speed_curve`): two points on the straight line from `0 V` ->
    `0 km/h` to `max_voltage` -> `max_speed_kmh`, one at
    `DEFAULT_SEEDED_STARTING_VOLTAGE_V` and one at `max_voltage`."""
    top = float(max_speed_kmh)
    top_v = float(max_voltage)
    start_v = min(float(DEFAULT_SEEDED_STARTING_VOLTAGE_V), top_v)
    start_speed = top * start_v / top_v if top_v > 0 else 0.0
    return [(start_v, start_speed), (top_v, top)]


def curve_problems(
    curve: Optional[List[Tuple[float, float]]], max_voltage: float
) -> List[str]:
    """Every one of the four conditions on a curve's points that `curve` breaks
    (design_new.md "SpeedProfile", `voltage_speed_curve`), as plain reasons; an
    empty list when the curve is valid. The commit and the profile save both
    refuse a curve this returns a reason for.
    """
    if not curve or len(curve) < 2:
        return ["the curve needs at least two points"]
    problems: List[str] = []
    points = [(float(v), float(s)) for v, s in curve]
    voltages = [v for v, _s in points]
    speeds = [s for _v, s in points]
    if any(b <= a for a, b in zip(voltages, voltages[1:])):
        problems.append("the points must be in order of rising voltage")
    first_v, first_s = points[0]
    if first_v <= 0.0 or first_s <= 0.0:
        problems.append(
            "the first point (the starting voltage and speed) must be above 0"
        )
    if abs(voltages[-1] - float(max_voltage)) > 1e-6:
        problems.append(
            f"the last point must be at max_voltage ({float(max_voltage):g} V)"
        )
    if any(b < a for a, b in zip(speeds, speeds[1:])):
        problems.append("speed must not fall as voltage rises")
    if speeds[-1] <= first_s:
        problems.append("the top speed must be above the starting speed")
    return problems


@dataclass
class SpeedProfile:
    """
    One locomotive's (or one model's) measured response to track voltage.

    Attributes:
        profile_id: Unique identifier for this profile
        name: Display name
        voltage_speed_curve: `(track_voltage, speed_kmh)` points, voltage
            ascending. The first point is the starting voltage and starting
            speed; the last is at `max_voltage` and carries the top speed.
        lighting_voltage: The voltage a powered train that nothing drives
            stands at so its lamps light, or None for a locomotive whose lamps
            light only once it moves (it then stands at `0` V).
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
    # fields (`lighting_voltage`, `voltage_speed_curve`, `calibrated_at`) and
    # reads every other field from the model profile it derives from;
    # `SpeedProfileRegistry.resolve` is the one accessor that merges the two.
    derived_from: Optional[str] = None
    # The locomotive model this profile describes, written the same way
    # `RosterEntry.model` writes it (design_new.md "SpeedProfile"). Carried by
    # model profiles only. Blank on a model profile seeded at train creation.
    model: str = ""
    # Wall-clock ISO-8601 stamp of the last `voltage_speed_curve` write by a
    # speed-calibration commit; None until a commit first writes it. A hand edit
    # does not write it, so it dates the last measurement, not the last change.
    # On an instance it is resolved from whichever record supplies the curve
    # (see `_overlay_instance_on_model`).
    calibrated_at: Optional[str] = None
    # One of the three instance-eligible fields. None means "unset": a MODEL
    # profile fills the seeded default in __post_init__; an INSTANCE profile
    # keeps None and `SpeedProfileRegistry.resolve` reads the model's curve.
    voltage_speed_curve: Optional[List[Tuple[float, float]]] = None
    # One of the three instance-eligible fields. None on a model profile means
    # the locomotive has no lighting voltage (it stands at 0 V); None on an
    # instance profile means "read the model's value".
    lighting_voltage: Optional[float] = None
    acceleration_rate: float = 10.0  # km/h per second
    deceleration_rate: float = 15.0  # km/h per second (service brake)
    emergency_brake_rate: float = 30.0  # km/h per second
    friction_rate: float = 0.5  # km/h per second (coasting deceleration)
    operational_decel_ratio: float = 0.8  # Use 80% of max brake for smoother ATO curve
    # Air resistance simulation (quadratic drag model)
    air_resistance_enabled: bool = True  # Master switch
    drag_coefficient: float = 0.001  # Quadratic drag coefficient
    reference_speed_kmh: float = 100.0  # Speed at which drag = drag_coefficient km/h/s
    # Top of the usable track_voltage range; the curve's last point sits here
    # (design_new.md "SpeedProfile", `max_voltage` field).
    max_voltage: float = DEFAULT_SEEDED_MAX_VOLTAGE_V
    # Per-train notch mapping. Power notches scale `acceleration_rate`; brake
    # notches scale the operational decel rate (`deceleration_rate ×
    # operational_decel_ratio`).
    power_multipliers: Dict[HandlePosition, float] = field(
        default_factory=lambda: dict(POWER_MULTIPLIERS)
    )
    brake_multipliers: Dict[HandlePosition, float] = field(
        default_factory=lambda: dict(BRAKE_MULTIPLIERS)
    )

    def __post_init__(self) -> None:
        """Fill a model profile's unset curve and sort the curve by voltage.

        A MODEL profile (`derived_from is None`) is a complete record: an unset
        `voltage_speed_curve` gets the seeded default for a `100 km/h` top
        speed. An INSTANCE profile keeps its unset fields None ("read from the
        model").
        """
        if self.derived_from is None and self.voltage_speed_curve is None:
            self.voltage_speed_curve = seeded_voltage_speed_curve(
                100.0, self.max_voltage
            )
        if self.voltage_speed_curve is not None:
            self.voltage_speed_curve = sorted(
                ((float(v), float(sp)) for v, sp in self.voltage_speed_curve),
                key=lambda x: x[0],
            )

    # -- the curve's landmarks ---------------------------------------------

    def _curve(self) -> List[Tuple[float, float]]:
        return list(self.voltage_speed_curve or [])

    @property
    def starting_voltage(self) -> float:
        """The lowest voltage at which the locomotive moves steadily: the
        curve's first point (design_new.md "SpeedProfile", Starting voltage and
        starting speed)."""
        curve = self._curve()
        return float(curve[0][0]) if curve else 0.0

    @property
    def starting_speed_kmh(self) -> float:
        """The locomotive's speed at its starting voltage: the curve's first
        point."""
        curve = self._curve()
        return float(curve[0][1]) if curve else 0.0

    @property
    def top_speed_kmh(self) -> float:
        """The speed of the curve's last point, at `max_voltage`."""
        curve = self._curve()
        return float(curve[-1][1]) if curve else 0.0

    def voltage_when_not_driven(self) -> float:
        """What a powered train that nothing drives outputs: `lighting_voltage`,
        or `0` V when it is unset (design_new.md "TrainPhysics", A powered train
        that nothing drives outputs `lighting_voltage`)."""
        return float(self.lighting_voltage) if self.lighting_voltage is not None else 0.0

    # -- voltage <-> speed (measured) ---------------------------------------

    def voltage_to_speed(self, voltage: float) -> float:
        """Read the curve forward (design_new.md "SpeedProfile", Voltage to
        speed reads the curve forward): a voltage below the starting voltage is
        `0 km/h`; one at or above it interpolates between the points around it;
        one above the last point reads the top speed."""
        curve = self._curve()
        if not curve:
            return 0.0
        v = float(voltage)
        if v < curve[0][0]:
            return 0.0
        if v >= curve[-1][0]:
            return float(curve[-1][1])
        for (v1, s1), (v2, s2) in zip(curve, curve[1:]):
            if v1 <= v <= v2:
                return self._interpolate(v1, s1, v2, s2, v)
        return float(curve[-1][1])

    def speed_to_voltage(self, speed_kmh: float) -> float:
        """Read the curve backward (design_new.md "SpeedProfile", Speed to
        voltage reads the curve backward).

        A speed of `0` is the voltage a train that nothing drives outputs. A
        speed from the starting speed to the top speed interpolates between the
        points around it, taking the lowest voltage of a flat stretch. A speed
        above the top speed is `max_voltage`. A moving train never runs below
        its starting speed (design_new.md "TrainPhysics"), so a speed between
        `0` and the starting speed reads as the starting voltage.
        """
        speed = float(speed_kmh)
        if speed <= 0.0:
            return self.voltage_when_not_driven()
        curve = self._curve()
        if not curve:
            return 0.0
        if speed <= curve[0][1]:
            return float(curve[0][0])
        if speed > curve[-1][1]:
            return float(self.max_voltage)
        for (v1, s1), (v2, s2) in zip(curve, curve[1:]):
            if s1 <= speed <= s2:
                if s2 == s1:
                    return float(v1)
                return self._interpolate(s1, v1, s2, v2, speed)
        return float(curve[-1][0])

    # -- power percent <-> speed (defined) -----------------------------------

    def power_percent_to_speed(self, percent: float) -> float:
        """`0%` is stopped; any percent above `0` is a straight line in speed
        from the starting speed to the top speed (design_new.md "SpeedProfile",
        Power percent is a share of the speed range)."""
        p = float(percent)
        if p <= 0.0:
            return 0.0
        p = min(p, 100.0)
        start = self.starting_speed_kmh
        return start + (self.top_speed_kmh - start) * p / 100.0

    def speed_to_power_percent(self, speed_kmh: float) -> float:
        """The same line read backward: `0 km/h` is `0%`, and a speed from the
        starting speed to the top speed is its share of that range."""
        speed = float(speed_kmh)
        if speed <= 0.0:
            return 0.0
        start = self.starting_speed_kmh
        span = self.top_speed_kmh - start
        if span <= 0.0:
            return 100.0
        return max(0.0, min(100.0, 100.0 * (speed - start) / span))

    def power_percent_to_voltage(self, percent: float) -> float:
        """A percent to a voltage, through speed (design_new.md "SpeedProfile",
        Two mappings join the three values)."""
        return self.speed_to_voltage(self.power_percent_to_speed(percent))

    @staticmethod
    def _interpolate(x1: float, y1: float, x2: float, y2: float, x: float) -> float:
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
            "voltage_speed_curve": (
                None
                if self.voltage_speed_curve is None
                else [[v, sp] for v, sp in self.voltage_speed_curve]
            ),
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
        raw_curve = data.get("voltage_speed_curve")
        curve = (
            None
            if raw_curve is None
            else [(float(v), float(sp)) for v, sp in raw_curve]
        )
        lighting_raw = data.get("lighting_voltage")
        lighting_voltage = None if lighting_raw is None else float(lighting_raw)

        return cls(
            profile_id=data.get("profile_id", ""),
            name=data.get("name", ""),
            derived_from=data.get("derived_from") or None,
            model=str(data.get("model", "") or ""),
            calibrated_at=data.get("calibrated_at") or None,
            voltage_speed_curve=curve,
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
    acceleration_rate=10.0,
    deceleration_rate=15.0,
    emergency_brake_rate=30.0,
)


def make_default_speed_profile(train_id: str, max_speed_kmh: float) -> SpeedProfile:
    """Build a train's default `SpeedProfile` (design_new.md "SpeedProfile").

    Its `voltage_speed_curve` is the seeded straight line reaching the owning
    train's configured `max_speed` at `max_voltage`. This is the single home for
    that default: `SimulationController.add_train` registers one per train, and
    the `_get_speed_profile` resolvers fall back to it for trains created by
    paths that do not seed a profile (layout load, step replay, editor
    add-without-place).
    """
    # `Train.from_dict` returns None for an explicit `"max_speed": null`, and
    # `float(None)` raises; a missing / non-numeric max_speed reads as 100 km/h.
    try:
        top_speed = float(max_speed_kmh)
    except (TypeError, ValueError):
        top_speed = 100.0
    if top_speed <= 0.0:
        top_speed = 100.0
    return SpeedProfile(
        profile_id=f"default_{train_id}",
        name=f"Default ({train_id})",
        voltage_speed_curve=seeded_voltage_speed_curve(top_speed),
    )


def make_instance_profile(
    model_profile_id: str, *, profile_id: str, name: str = ""
) -> SpeedProfile:
    """An empty instance profile deriving from `model_profile_id`.

    It carries none of its three fields yet (design_new.md "SpeedProfile",
    Model profiles and instance profiles): every field resolves from the model
    profile until **Speed calibration** or a hand edit writes one onto this
    instance.
    """
    return SpeedProfile(
        profile_id=profile_id, name=name, derived_from=model_profile_id
    )


def seed_default_profile_pair(registry, train_id: str, max_speed_kmh) -> str:
    """Register a default model profile plus an empty instance under it.

    design_new.md "SpeedProfile": when placement seeds no profile,
    `add_train(...)` creates a model profile whose `voltage_speed_curve` reaches
    the train's `max_speed` at `max_voltage`, plus an empty instance deriving from
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

    design_new.md "SpeedProfile", The curve and the lighting voltage resolve on
    their own: the instance's own fields where it carries them, the model's
    values everywhere else, with no curve point moved or dropped. The result
    carries the instance's `profile_id` (so a train that names the instance
    still matches its resolved profile) and reads as a flattened model profile.
    """
    lighting = (
        instance.lighting_voltage
        if instance.lighting_voltage is not None
        else model.lighting_voltage
    )
    curve = (
        instance.voltage_speed_curve
        if instance.voltage_speed_curve is not None
        else model.voltage_speed_curve
    )
    # `calibrated_at` follows the curve (design_new.md "SpeedProfile",
    # `calibrated_at` follows the curve).
    calibrated_at = (
        instance.calibrated_at
        if instance.voltage_speed_curve is not None
        else model.calibrated_at
    )
    return SpeedProfile(
        profile_id=instance.profile_id,
        name=instance.name or model.name,
        derived_from=None,
        model=model.model,
        calibrated_at=calibrated_at,
        voltage_speed_curve=list(curve) if curve is not None else None,
        lighting_voltage=lighting,
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
        voltage_speed_curve=instance.voltage_speed_curve,
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
        own fields where the instance carries them, and the model profile's
        values everywhere else.

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

    def to_dict(self) -> Dict[str, Any]:
        """Serialize every registered profile except the built-in ``default``.

        Persisted with the layout (design_new.md "SpeedProfile": a measured
        curve and lighting voltage stay with the locomotive across placements,
        through ordinary `SpeedProfile` persistence). ``default`` is a code
        constant that ``__init__`` always re-seeds, so it is not written — a
        stale persisted copy must never shadow a changed constant on reload.
        """
        return {
            profile_id: profile.to_dict()
            for profile_id, profile in self._profiles.items()
            if profile_id != "default"
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SpeedProfileRegistry":
        """Rebuild a registry from `to_dict(...)` output (or an absent/empty map
        in a legacy save, which yields the default-only registry)."""
        registry = cls()
        for profile_id, profile_data in (data or {}).items():
            if profile_id == "default":
                continue
            registry.register(SpeedProfile.from_dict(profile_data))
        return registry

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
