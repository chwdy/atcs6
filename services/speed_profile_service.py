"""Speed-profile lifecycle service.

CRUD over the ``SpeedProfile`` records held by the shared
``SpeedProfileRegistry``. The HTTP layer in ``routes/speed_profiles.py``
parses JSON; this layer owns the registry mutations plus the
duplicate / default / in-use guards.

Like ``layout_service``, every operation takes ``state.simulation_lock``
so a profile swap never races the simulation tick. Non-HTTP callers (CLI
tools, journey tests) get the same lock discipline for free.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app_state import AppState
from services.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationError,
)
from speed_profile import SpeedProfile, reanchor_voltage_power_curve

# The one profile id that may never be deleted -- every train falls back to
# it, so removing it would orphan the fleet.
DEFAULT_PROFILE_ID = "default"

# The three fields an instance profile may carry (design_new.md "SpeedProfile",
# Model profiles and instance profiles).
INSTANCE_FIELDS = ("lighting_voltage", "power_speed_curve", "calibrated_at")

# Fields a hand edit never changes (design_new.md "SpeedProfile", Editing a
# profile by hand): the stored value always wins over the request body.
_NOT_HAND_EDITED = ("derived_from", "lighting_voltage", "calibrated_at")

# What a hand edit may change on an instance profile besides its name; every
# other field belongs to the model profile it derives from.
_INSTANCE_HAND_EDITED = ("name", "power_speed_curve")


def list_profiles(state: AppState) -> List[SpeedProfile]:
    """Return every registered profile (registry order)."""
    with state.simulation_lock:
        return [
            profile
            for profile in (
                state.speed_profile_registry.get(pid)
                for pid in state.speed_profile_registry.list_profiles()
            )
            if profile is not None
        ]


def get_profile(state: AppState, profile_id: str) -> Optional[SpeedProfile]:
    """Return the profile, or None when not found."""
    with state.simulation_lock:
        return state.speed_profile_registry.get(profile_id)


def resolved_view(state: AppState, profile_id: str) -> Optional[Dict[str, Any]]:
    """The profile as the Speed Profile panel shows it, or None when absent.

    Every value is the resolved one, read through
    ``SpeedProfileRegistry.resolve(...)``, so an instance profile shows what its
    locomotive runs on. The stored record's own ``profile_id``, ``name`` and
    ``derived_from`` stay, and ``own_fields`` lists the instance fields the
    record carries itself (empty on a model profile, which carries all of
    them) -- design_new.md "SpeedProfile", Editing a profile by hand.
    """
    with state.simulation_lock:
        registry = state.speed_profile_registry
        stored = registry.get(profile_id)
        if stored is None:
            return None
        view = registry.resolve(profile_id).to_dict()
        view["profile_id"] = stored.profile_id
        view["name"] = stored.name
        view["derived_from"] = stored.derived_from
        view["own_fields"] = (
            []
            if stored.derived_from is None
            else [f for f in INSTANCE_FIELDS if getattr(stored, f) is not None]
        )
        return view


def require_profile(state: AppState, profile_id: str) -> SpeedProfile:
    """Return the profile or raise NotFoundError."""
    profile = get_profile(state, profile_id)
    if profile is None:
        raise NotFoundError(f"Speed profile '{profile_id}' not found")
    return profile


def create_profile(state: AppState, *, data: Dict[str, Any]) -> SpeedProfile:
    """Create and register a new profile from a request-shaped dict.

    Args:
        data: Profile fields. ``profile_id`` and ``name`` are required;
            the rest are read by ``SpeedProfile.from_dict``.

    Raises:
        ValidationError when profile_id/name is missing, or the data does
            not parse into a SpeedProfile.
        ConflictError when a profile with that id already exists.
    """
    data = dict(data or {})
    profile_id = data.get("profile_id")
    name = data.get("name")

    if not profile_id:
        raise ValidationError("profile_id is required")
    if not name:
        raise ValidationError("name is required")

    with state.simulation_lock:
        if state.speed_profile_registry.get(profile_id):
            raise ConflictError(
                f"Speed profile with profile_id '{profile_id}' already exists"
            )
        try:
            profile = SpeedProfile.from_dict(data)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"Invalid profile data: {exc}") from exc
        state.speed_profile_registry.register(profile)

    return profile


def update_profile(
    state: AppState, profile_id: str, *, data: Dict[str, Any]
) -> SpeedProfile:
    """Apply a hand edit to an existing profile.

    design_new.md "SpeedProfile", Editing a profile by hand:

    * Every field the body leaves out keeps its stored value, so the panel
      sends only the fields the operator changed, and an instance profile's
      unset fields stay unset and go on reading from its model profile.
    * ``lighting_voltage``, ``calibrated_at`` and ``derived_from`` are never
      edited by hand; the stored values win over the body. A model profile's
      ``voltage_power_curve`` stays anchored at its stored lighting voltage.
    * On an instance profile only ``name`` and ``power_speed_curve`` may be
      written; a body that changes any other field is refused, because that
      field belongs to the model profile.

    The URL id wins: a ``profile_id`` in the body must match it or be omitted.

    Raises:
        ValidationError when the body's profile_id disagrees with the URL
            id, the body changes a model-profile field on an instance profile,
            a curve has fewer than two points, a curve's speed falls as power
            rises, or the data does not parse.
        NotFoundError when no profile has that id.
    """
    data = dict(data or {})
    if "profile_id" in data and data["profile_id"] != profile_id:
        raise ValidationError(
            "profile_id in body must match URL parameter or be omitted"
        )
    data.pop("profile_id", None)
    for key in _NOT_HAND_EDITED:
        data.pop(key, None)
    curve = data.get("power_speed_curve")
    if curve is not None and len(curve) < 2:
        raise ValidationError("power_speed_curve needs at least two points")

    with state.simulation_lock:
        existing = state.speed_profile_registry.get(profile_id)
        if not existing:
            raise NotFoundError(f"Speed profile '{profile_id}' not found")

        if existing.derived_from is not None:
            model_fields = sorted(k for k in data if k not in _INSTANCE_HAND_EDITED)
            if model_fields:
                raise ValidationError(
                    f"{', '.join(model_fields)} belong to model profile "
                    f"'{existing.derived_from}'; edit that profile instead"
                )

        merged = existing.to_dict()
        merged.update(data)
        if existing.derived_from is None and data.get("voltage_power_curve"):
            merged["voltage_power_curve"] = reanchor_voltage_power_curve(
                [(float(v), float(p)) for v, p in data["voltage_power_curve"]],
                float(existing.lighting_voltage),
                float(merged.get("max_voltage", existing.max_voltage)),
            )

        try:
            updated = SpeedProfile.from_dict(merged)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"Invalid profile data: {exc}") from exc
        if curve is not None:
            # design_new.md "SpeedProfile", A hand-edited curve must stay
            # invertible: physics reads the curve in both directions, so speed
            # must not fall as power rises. `from_dict` has sorted it by power.
            points = updated.power_speed_curve or []
            for (low_power, low_speed), (high_power, high_speed) in zip(
                points, points[1:]
            ):
                if high_speed < low_speed:
                    raise ValidationError(
                        "power_speed_curve speed must not fall as power rises: "
                        f"{high_power:g}% is {high_speed:g} km/h, below "
                        f"{low_speed:g} km/h at {low_power:g}%"
                    )
        state.speed_profile_registry.register(updated)

    return updated


def delete_profile(state: AppState, profile_id: str) -> bool:
    """Delete a speed profile. Returns True on success.

    Raises:
        NotFoundError when no profile has that id.
        ConflictError when the profile is the default, or is assigned to
            one or more trains.
    """
    with state.simulation_lock:
        profile = state.speed_profile_registry.get(profile_id)
        if not profile:
            raise NotFoundError(f"Speed profile '{profile_id}' not found")

        if profile_id == DEFAULT_PROFILE_ID:
            raise ConflictError("Cannot delete default speed profile")

        trains_using = [
            train.train_id
            for train in state.network.trains.values()
            if getattr(train, "speed_profile_id", None) == profile_id
        ]
        if trains_using:
            raise ConflictError(
                "Cannot delete speed profile in use by trains: "
                + ", ".join(trains_using)
            )

        # A model profile may not be deleted while any registered instance
        # profile still derives from it (design_new.md "SpeedProfile", Model
        # profiles and instance profiles): those instances resolve their
        # measured fields through the model, so removing it would strand them.
        registry = state.speed_profile_registry
        derived_instances = [
            pid
            for pid in registry.list_profiles()
            if getattr(registry.get(pid), "derived_from", None) == profile_id
        ]
        if derived_instances:
            raise ConflictError(
                "Cannot delete model speed profile still derived from by "
                "instances: " + ", ".join(sorted(derived_instances))
            )

        if not state.speed_profile_registry.remove(profile_id):
            raise ServiceError(f"Failed to delete speed profile '{profile_id}'")

    return True
