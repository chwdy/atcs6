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
  above `0` it is a straight line in speed from `0 km/h` to the top speed
  (design_new.md "SpeedProfile", Power percent is a share of the speed range).

Three voltages are stored beside the curve, each set for one locomotive
(design_new.md "SpeedProfile"): `lighting_voltage`, the voltage a standing
train is held at so its lamps light; `starting_voltage`, the lowest voltage at
which the locomotive moves at all, which the curve is read from (The curve is
read from the starting voltage up); and `ato_min_voltage`, the lowest voltage
ATO drives a moving train at (ATO minimum voltage and ATO minimum speed).

Records are organised as models, sets and instance profiles (design_new.md
"SpeedProfile", Models, sets and instance profiles): the fixed Default profile
`0`; sets, each belonging to one `LocomotiveModel`; and one instance profile
per train, a copy of the profile the train was given. Every profile and model
id is a number from a counter that never gives a number out twice
(Profile and model ids are numbers).
"""

import copy
from dataclasses import dataclass, field, fields
from typing import Dict, Iterable, List, Tuple, Any, Optional

from constants import (
    DEFAULT_SEEDED_FIRST_POINT_VOLTAGE_V,
    DEFAULT_SEEDED_MAX_VOLTAGE_V,
    DEFAULT_SEEDED_STARTING_VOLTAGE_V,
    DEFAULT_SEEDED_TOP_SPEED_KMH,
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
    `DEFAULT_SEEDED_FIRST_POINT_VOLTAGE_V` (above the seeded starting voltage)
    and one at `max_voltage`."""
    top = float(max_speed_kmh)
    top_v = float(max_voltage)
    start_v = min(float(DEFAULT_SEEDED_FIRST_POINT_VOLTAGE_V), top_v)
    start_speed = top * start_v / top_v if top_v > 0 else 0.0
    return [(start_v, start_speed), (top_v, top)]


def curve_problems(
    curve: Optional[List[Tuple[float, float]]], max_voltage: float
) -> List[str]:
    """Every one of the three conditions on a curve's points that `curve` breaks
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
        problems.append("the first point's voltage and speed must be above 0")
    if abs(voltages[-1] - float(max_voltage)) > 1e-6:
        problems.append(
            f"the last point must be at max_voltage ({float(max_voltage):g} V)"
        )
    if any(b < a for a, b in zip(speeds, speeds[1:])):
        problems.append("speed must not fall as voltage rises")
    if speeds[-1] <= first_s:
        problems.append("the top speed must be above the first point's speed")
    return problems


# The fixed Default profile's number (design_new.md "SpeedProfile", Models,
# sets and instance profiles).
DEFAULT_PROFILE_ID = 0

# The fields that say which record a profile is and where it came from. A copy,
# a promote and a hand edit never write them as values (design_new.md
# "SpeedProfile", Copying a profile; Editing a profile by hand).
IDENTITY_FIELDS = ("profile_id", "model_id", "set_name", "parent_profile_id")


def _top_speed_or_default(max_speed_kmh: Any) -> float:
    """A usable top speed: `max_speed_kmh`, or the Default top speed when it is
    missing, not a number, or not above 0."""
    try:
        top = float(max_speed_kmh)
    except (TypeError, ValueError):
        return float(DEFAULT_SEEDED_TOP_SPEED_KMH)
    return top if top > 0.0 else float(DEFAULT_SEEDED_TOP_SPEED_KMH)


def as_profile_id(value: Any) -> Optional[int]:
    """`value` as a profile or model number, or None when it is not one. A
    profile id is never a string (design_new.md "SpeedProfile", Profile and
    model ids are numbers); a JSON body or a URL may still carry its digits."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


@dataclass
class LocomotiveModel:
    """A locomotive model as written on the box (design_new.md "SpeedProfile",
    `LocomotiveModel`). It groups the sets measured on locomotives of that
    model; every set names it by `model_id`."""

    model_id: int
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return {"model_id": self.model_id, "name": self.name}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocomotiveModel":
        return cls(model_id=int(data["model_id"]), name=str(data.get("name", "")))


@dataclass
class SpeedProfile:
    """
    A locomotive's measured response to track voltage, plus its motion rates.

    A record is the Default profile (`profile_id == 0`), a set (`model_id`
    set), or a train's instance profile (design_new.md "SpeedProfile", Models,
    sets and instance profiles). Every record is complete: no field is read
    from another record.

    Attributes:
        profile_id: The profile's number.
        model_id: On a set, the `LocomotiveModel` it belongs to.
        set_name: On a set, its name within its model.
        parent_profile_id: On an instance profile, the profile it was last
            copied from (a set or the Default profile), or None.
        voltage_speed_curve: `(track_voltage, speed_kmh)` points, voltage
            ascending. The last is at `max_voltage` and carries the top speed.
            It is read from `starting_voltage` up.
        lighting_voltage: The voltage a powered train that nothing drives
            stands at so its lamps light, or None for a locomotive whose lamps
            light only once it moves (it then stands at `0` V).
        starting_voltage: The lowest voltage at which the locomotive moves at
            all; below `max_voltage`.
        ato_min_voltage: The ATO minimum voltage the operator set, or None
            (ATO then runs from the curve's first point above the starting
            voltage).
    """

    profile_id: int = DEFAULT_PROFILE_ID
    model_id: Optional[int] = None
    set_name: str = ""
    parent_profile_id: Optional[int] = None
    # Wall-clock ISO-8601 stamp of the last `voltage_speed_curve` write by a
    # speed-calibration commit; None until a commit first writes it. A hand edit
    # does not write it, so it dates the last measurement, not the last change.
    calibrated_at: Optional[str] = None
    # None at construction means "the Default curve" (filled in __post_init__).
    voltage_speed_curve: Optional[List[Tuple[float, float]]] = None
    lighting_voltage: Optional[float] = None
    # None at construction means "the Default starting voltage".
    starting_voltage: Optional[float] = None
    ato_min_voltage: Optional[float] = None
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
        """Fill an unset curve and starting voltage with the Default values,
        and sort the curve by voltage, so every record is complete."""
        if self.voltage_speed_curve is None:
            self.voltage_speed_curve = seeded_voltage_speed_curve(
                DEFAULT_SEEDED_TOP_SPEED_KMH, self.max_voltage
            )
        if self.starting_voltage is None:
            self.starting_voltage = float(DEFAULT_SEEDED_STARTING_VOLTAGE_V)
        self.voltage_speed_curve = sorted(
            ((float(v), float(sp)) for v, sp in self.voltage_speed_curve),
            key=lambda x: x[0],
        )

    # -- which record this is ------------------------------------------------

    @property
    def is_default(self) -> bool:
        return self.profile_id == DEFAULT_PROFILE_ID

    @property
    def is_set(self) -> bool:
        return self.model_id is not None

    @property
    def is_instance(self) -> bool:
        return not self.is_default and not self.is_set

    @classmethod
    def value_fields(cls) -> Tuple[str, ...]:
        """Every field a copy or a promote writes: all but the identity fields."""
        return tuple(f.name for f in fields(cls) if f.name not in IDENTITY_FIELDS)

    def copy_values_from(self, source: "SpeedProfile") -> None:
        """Write every value field of `source` onto this record (design_new.md
        "SpeedProfile", Copying a profile). Identity fields are left alone."""
        for name in self.value_fields():
            setattr(self, name, copy.deepcopy(getattr(source, name)))

    # -- the curve's landmarks ---------------------------------------------

    def _curve(self) -> List[Tuple[float, float]]:
        return list(self.voltage_speed_curve or [])

    def _starting(self) -> float:
        return float(self.starting_voltage) if self.starting_voltage is not None else 0.0

    def readable_curve(self) -> List[Tuple[float, float]]:
        """The points both readings use (design_new.md "SpeedProfile", The curve
        is read from the starting voltage up): `(starting_voltage, 0 km/h)`,
        then every stored point above the starting voltage. A stored point at
        or below the starting voltage is not read."""
        start = self._starting()
        return [(start, 0.0)] + [
            (float(v), float(s)) for v, s in self._curve() if float(v) > start
        ]

    @property
    def first_point_voltage(self) -> float:
        """The voltage of the curve's first point above the starting voltage —
        the lowest measured voltage, and where the measured part of the curve
        begins. `max_voltage` when no point lies above it."""
        readable = self.readable_curve()
        return float(readable[1][0]) if len(readable) > 1 else float(self.max_voltage)

    @property
    def top_speed_kmh(self) -> float:
        """The speed of the curve's last point, at `max_voltage`."""
        curve = self._curve()
        return float(curve[-1][1]) if curve else 0.0

    @property
    def ato_minimum_voltage(self) -> float:
        """The lowest voltage ATO drives a moving train at (design_new.md
        "SpeedProfile", ATO minimum voltage and ATO minimum speed): the
        resolved `ato_min_voltage` when set, otherwise the curve's first point
        above the starting voltage."""
        if self.ato_min_voltage is not None:
            return float(self.ato_min_voltage)
        return self.first_point_voltage

    @property
    def ato_minimum_speed_kmh(self) -> float:
        """The speed the curve gives at the ATO minimum voltage."""
        return self.voltage_to_speed(self.ato_minimum_voltage)

    def voltage_when_not_driven(self) -> float:
        """What a powered train that nothing drives outputs: `lighting_voltage`,
        or `0` V when it is unset (design_new.md "TrainPhysics", A powered train
        that nothing drives outputs `lighting_voltage`)."""
        return float(self.lighting_voltage) if self.lighting_voltage is not None else 0.0

    # -- voltage <-> speed (measured) ---------------------------------------

    def voltage_to_speed(self, voltage: float) -> float:
        """Read the curve forward (design_new.md "SpeedProfile", Voltage to
        speed reads the curve forward): a voltage at or below the starting
        voltage is `0 km/h`; one above it interpolates between the points
        around it, on the straight line from `(starting_voltage, 0)` to the
        first stored point below that point; one at or above the last point
        reads the top speed."""
        curve = self.readable_curve()
        v = float(voltage)
        if len(curve) < 2 or v <= curve[0][0]:
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
        speed above `0` up to the top speed interpolates between the points
        around it — so it is always above the starting voltage — taking the
        lowest voltage of a flat stretch. A speed above the top speed is
        `max_voltage`.
        """
        speed = float(speed_kmh)
        if speed <= 0.0:
            return self.voltage_when_not_driven()
        curve = self.readable_curve()
        if len(curve) < 2:
            return float(self.max_voltage)
        if speed > curve[-1][1]:
            return float(self.max_voltage)
        for (v1, s1), (v2, s2) in zip(curve, curve[1:]):
            if s1 < speed <= s2 or (s1 == speed and s2 == speed):
                if s2 == s1:
                    return float(v1)
                return self._interpolate(s1, v1, s2, v2, speed)
        return float(curve[-1][0])

    # -- power percent <-> speed (defined) -----------------------------------

    def power_percent_to_speed(self, percent: float) -> float:
        """`0%` is stopped; any percent above `0` is a straight line in speed
        from `0 km/h` to the top speed (design_new.md "SpeedProfile", Power
        percent is a share of the speed range)."""
        p = float(percent)
        if p <= 0.0:
            return 0.0
        p = min(p, 100.0)
        return self.top_speed_kmh * p / 100.0

    def speed_to_power_percent(self, speed_kmh: float) -> float:
        """The same line read backward: `0 km/h` is `0%`, and a speed up to the
        top speed is its share of the top speed."""
        speed = float(speed_kmh)
        if speed <= 0.0:
            return 0.0
        top = self.top_speed_kmh
        if top <= 0.0:
            return 100.0
        return max(0.0, min(100.0, 100.0 * speed / top))

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
            "model_id": self.model_id,
            "set_name": self.set_name,
            "parent_profile_id": self.parent_profile_id,
            "calibrated_at": self.calibrated_at,
            "voltage_speed_curve": [[v, sp] for v, sp in self.voltage_speed_curve],
            "lighting_voltage": self.lighting_voltage,
            "starting_voltage": self.starting_voltage,
            "ato_min_voltage": self.ato_min_voltage,
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
        starting_raw = data.get("starting_voltage")
        starting_voltage = None if starting_raw is None else float(starting_raw)
        ato_min_raw = data.get("ato_min_voltage")
        ato_min_voltage = None if ato_min_raw is None else float(ato_min_raw)
        profile_id = as_profile_id(data.get("profile_id"))

        return cls(
            profile_id=DEFAULT_PROFILE_ID if profile_id is None else profile_id,
            model_id=as_profile_id(data.get("model_id")),
            set_name=str(data.get("set_name", "") or ""),
            parent_profile_id=as_profile_id(data.get("parent_profile_id")),
            calibrated_at=data.get("calibrated_at") or None,
            voltage_speed_curve=curve,
            lighting_voltage=lighting_voltage,
            starting_voltage=starting_voltage,
            ato_min_voltage=ato_min_voltage,
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


def make_default_profile() -> SpeedProfile:
    """The fixed Default profile `0` (design_new.md "SpeedProfile", The
    Default profile is fixed)."""
    return SpeedProfile(profile_id=DEFAULT_PROFILE_ID)


# The Default profile as a constant, for readers that need a complete profile
# before any registry exists. It is never written.
DEFAULT_SPEED_PROFILE = make_default_profile()


def copied_profile(
    source: SpeedProfile, *, profile_id: int, max_speed_kmh: Any = None
) -> SpeedProfile:
    """A new instance-profile record holding a copy of `source` (design_new.md
    "SpeedProfile", Copying a profile).

    Every value field comes from `source`; `parent_profile_id` names
    `source.profile_id`. A copy of the Default profile draws its curve to
    `max_speed_kmh` instead of the Default top speed.
    """
    record = SpeedProfile(profile_id=int(profile_id))
    record.copy_values_from(source)
    record.parent_profile_id = source.profile_id
    if source.is_default:
        record.voltage_speed_curve = seeded_voltage_speed_curve(
            _top_speed_or_default(max_speed_kmh), record.max_voltage
        )
    return record


def fallback_profile_for(train) -> SpeedProfile:
    """A complete profile for a train whose instance profile cannot be found.

    Every train owns an instance profile (design_new.md "SpeedProfile", Every
    train owns exactly one instance profile), so this only guards a reader that
    runs before the instance is registered: the Default profile copied for the
    train's `max_speed`. It is never registered.
    """
    return copied_profile(
        DEFAULT_SPEED_PROFILE,
        profile_id=DEFAULT_PROFILE_ID,
        max_speed_kmh=getattr(train, "max_speed", None),
    )


def resolve_memoized_default_profile(cache_holder, train) -> SpeedProfile:
    """`fallback_profile_for(train)`, memoized on `cache_holder` and rebuilt
    only when the train's id or `max_speed` changes, so a per-tick read does not
    allocate a profile every tick."""
    key = (train.train_id, train.max_speed)
    cache = getattr(cache_holder, "_default_profile_cache", None)
    if cache is None or cache[0] != key:
        cache = (key, fallback_profile_for(train))
        cache_holder._default_profile_cache = cache
    return cache[1]


class SpeedProfileRegistry:
    """Every `SpeedProfile` and `LocomotiveModel`, keyed by number.

    design_new.md "SpeedProfile": the registry always holds the Default
    profile `0`, gives each new profile and model the next number from its own
    counter, and never gives a number out twice (Profile and model ids are
    numbers). The library -- models, sets and both counters -- is what the
    locomotive file stores; instance profiles live with their trains (Where
    models, sets and instance profiles are kept).
    """

    def __init__(self):
        self._profiles: Dict[int, SpeedProfile] = {
            DEFAULT_PROFILE_ID: make_default_profile()
        }
        self._models: Dict[int, LocomotiveModel] = {}
        self.next_profile_id: int = 1
        self.next_model_id: int = 1

    # -- numbers ------------------------------------------------------------

    def allocate_profile_id(self) -> int:
        pid = self.next_profile_id
        self.next_profile_id += 1
        return pid

    def allocate_model_id(self) -> int:
        mid = self.next_model_id
        self.next_model_id += 1
        return mid

    # -- profiles -----------------------------------------------------------

    def register(self, profile: SpeedProfile) -> None:
        """Store `profile` under its own number. The counter moves past it, so
        the number is never given out again."""
        pid = int(profile.profile_id)
        if pid == DEFAULT_PROFILE_ID:
            return
        self._profiles[pid] = profile
        if pid >= self.next_profile_id:
            self.next_profile_id = pid + 1

    def get(self, profile_id: Any) -> Optional[SpeedProfile]:
        pid = as_profile_id(profile_id)
        return None if pid is None else self._profiles.get(pid)

    def list_profiles(self) -> List[int]:
        return list(self._profiles.keys())

    def profiles(self) -> List[SpeedProfile]:
        return list(self._profiles.values())

    def remove(self, profile_id: Any) -> bool:
        pid = as_profile_id(profile_id)
        if pid is None or pid == DEFAULT_PROFILE_ID:
            return False
        return self._profiles.pop(pid, None) is not None

    def create_instance(
        self,
        *,
        source_profile_id: Any = None,
        source_profile: Optional[SpeedProfile] = None,
        max_speed_kmh: Any = None,
    ) -> SpeedProfile:
        """Register a new instance profile copied from a set, the Default
        profile, or a scene's own record (design_new.md "SpeedProfile", Giving a
        train a profile; Where models, sets and instance profiles are kept, A
        scene adds no models or sets).

        `source_profile` is the scene's record: the copy takes its values with
        `parent_profile_id` unset, and the record itself is not registered.
        Otherwise `source_profile_id` names the Default profile or a set
        (None means the Default profile).
        """
        if source_profile is not None:
            record = SpeedProfile(profile_id=self.allocate_profile_id())
            record.copy_values_from(source_profile)
            record.parent_profile_id = None
        else:
            source = self.copy_source(source_profile_id)
            record = copied_profile(
                source,
                profile_id=self.allocate_profile_id(),
                max_speed_kmh=max_speed_kmh,
            )
        self.register(record)
        return record

    def copy_source(self, profile_id: Any) -> SpeedProfile:
        """The Default profile or set `profile_id` names (None: the Default
        profile). Raises ValueError for an instance profile or no profile."""
        if profile_id is None:
            return self._profiles[DEFAULT_PROFILE_ID]
        source = self.get(profile_id)
        if source is None:
            raise ValueError(f"No speed profile {profile_id!r}")
        if source.is_instance:
            raise ValueError(
                f"Speed profile {profile_id!r} is a train's instance profile; a "
                "train is given the Default profile or a set"
            )
        return source

    def copy_into_instance(
        self, instance_id: Any, source_profile_id: Any, max_speed_kmh: Any = None
    ) -> SpeedProfile:
        """Copy the Default profile or a set into an existing instance profile,
        keeping its number (design_new.md "SpeedProfile", Copying a profile)."""
        instance = self.get(instance_id)
        if instance is None or not instance.is_instance:
            raise ValueError(f"No instance profile {instance_id!r}")
        source = self.copy_source(source_profile_id)
        fresh = copied_profile(
            source, profile_id=instance.profile_id, max_speed_kmh=max_speed_kmh
        )
        self._profiles[instance.profile_id] = fresh
        return fresh

    def remove_instances(self, keep: Iterable[Any] = ()) -> List[int]:
        """Remove every instance profile except those in `keep`; return the
        numbers removed. Used when the trains that own them go."""
        kept = {as_profile_id(k) for k in keep}
        doomed = [
            pid for pid, p in self._profiles.items() if p.is_instance and pid not in kept
        ]
        for pid in doomed:
            del self._profiles[pid]
        return doomed

    # -- models and sets ----------------------------------------------------

    def models(self) -> List[LocomotiveModel]:
        return list(self._models.values())

    def get_model(self, model_id: Any) -> Optional[LocomotiveModel]:
        mid = as_profile_id(model_id)
        return None if mid is None else self._models.get(mid)

    def add_model(self, name: str) -> LocomotiveModel:
        name = (name or "").strip()
        if not name:
            raise ValueError("A model's name must not be blank")
        model = LocomotiveModel(model_id=self.allocate_model_id(), name=name)
        self._models[model.model_id] = model
        return model

    def register_model(self, model: LocomotiveModel) -> None:
        self._models[int(model.model_id)] = model
        if model.model_id >= self.next_model_id:
            self.next_model_id = model.model_id + 1

    def sets(self, model_id: Any = None) -> List[SpeedProfile]:
        mid = as_profile_id(model_id)
        return [
            p
            for p in self._profiles.values()
            if p.is_set and (model_id is None or p.model_id == mid)
        ]

    def create_set(
        self, model_id: int, set_name: str, source: SpeedProfile
    ) -> SpeedProfile:
        """Register a new set under `model_id` holding a copy of `source`'s
        values (design_new.md "SpeedProfile", Promoting an instance profile)."""
        if self.get_model(model_id) is None:
            raise ValueError(f"No model {model_id!r}")
        record = SpeedProfile(profile_id=self.allocate_profile_id())
        record.copy_values_from(source)
        record.model_id = int(model_id)
        record.set_name = (set_name or "").strip()
        self.register(record)
        return record

    def remove_model(self, model_id: Any) -> List[int]:
        """Remove a model and every set naming it; return the set numbers
        removed (design_new.md "SpeedProfile", Deleting a set or a model)."""
        mid = as_profile_id(model_id)
        if mid is None or mid not in self._models:
            return []
        removed = [p.profile_id for p in self.sets(mid)]
        for pid in removed:
            del self._profiles[pid]
        del self._models[mid]
        return removed

    def parent_label(self, profile: SpeedProfile) -> Dict[str, Any]:
        """Where an instance profile came from, by name (design_new.md
        "SpeedProfile", A train shows where its profile came from)."""
        parent_id = profile.parent_profile_id
        if parent_id is None:
            return {"kind": "none"}
        if parent_id == DEFAULT_PROFILE_ID:
            return {"kind": "default"}
        parent = self._profiles.get(parent_id)
        if parent is None or not parent.is_set:
            return {"kind": "deleted"}
        model = self._models.get(parent.model_id)
        return {
            "kind": "set",
            "model_name": model.name if model else "",
            "set_name": parent.set_name,
        }

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Every profile, keyed by its number as a string (the live snapshot's
        `speed_profiles`)."""
        return {str(pid): p.to_dict() for pid, p in self._profiles.items()}

    def models_to_list(self) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self._models.values()]

    def library_dict(self) -> Dict[str, Any]:
        """The locomotive file's contents: every model, every set, and both
        counters (design_new.md "SpeedProfile", Where models, sets and instance
        profiles are kept)."""
        return {
            "models": self.models_to_list(),
            "sets": [p.to_dict() for p in self.sets()],
            "next_profile_id": self.next_profile_id,
            "next_model_id": self.next_model_id,
        }

    def load_library(self, data: Dict[str, Any]) -> None:
        """Add the locomotive file's models and sets and take its counters."""
        for model_data in data.get("models") or []:
            self.register_model(LocomotiveModel.from_dict(model_data))
        for set_data in data.get("sets") or []:
            record = SpeedProfile.from_dict(set_data)
            if record.is_set and record.profile_id != DEFAULT_PROFILE_ID:
                self.register(record)
        self.next_profile_id = max(
            self.next_profile_id, int(data.get("next_profile_id") or 1)
        )
        self.next_model_id = max(
            self.next_model_id, int(data.get("next_model_id") or 1)
        )
