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
from speed_profile import SpeedProfile

# The one profile id that may never be deleted -- every train falls back to
# it, so removing it would orphan the fleet.
DEFAULT_PROFILE_ID = "default"


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
    """Replace an existing profile's fields from a request-shaped dict.

    The URL id wins: a ``profile_id`` in the body must match it or be
    omitted, and ``name`` falls back to the existing value when absent.

    Raises:
        ValidationError when the body's profile_id disagrees with the URL
            id, or the data does not parse.
        NotFoundError when no profile has that id.
    """
    data = dict(data or {})
    if "profile_id" in data and data["profile_id"] != profile_id:
        raise ValidationError(
            "profile_id in body must match URL parameter or be omitted"
        )

    with state.simulation_lock:
        existing = state.speed_profile_registry.get(profile_id)
        if not existing:
            raise NotFoundError(f"Speed profile '{profile_id}' not found")

        data["profile_id"] = profile_id
        if "name" not in data:
            data["name"] = existing.name

        try:
            updated = SpeedProfile.from_dict(data)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"Invalid profile data: {exc}") from exc
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
