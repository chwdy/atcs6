"""Speed-profile service: the library of models and sets, and hand edits.

design_new.md "SpeedProfile": a record is the fixed Default profile `0`, a set
under a `LocomotiveModel`, or one train's instance profile (Models, sets and
instance profiles). The HTTP layer in ``routes/speed_profiles.py`` parses JSON;
this layer owns the registry writes and their refusals.

Like ``layout_service``, every operation takes ``state.simulation_lock`` so a
profile write never races the simulation tick.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app_state import AppState
from services.exceptions import ConflictError, NotFoundError, ValidationError
from speed_profile import (
    DEFAULT_PROFILE_ID,
    IDENTITY_FIELDS,
    LocomotiveModel,
    SpeedProfile,
    SpeedProfileRegistry,
    as_profile_id,
    curve_problems,
)

_VOLTAGE_EPSILON = 1e-9


def profile_problems(profile: SpeedProfile) -> List[str]:
    """What breaks in one profile (design_new.md "SpeedProfile", Editing a
    profile by hand): the curve's three conditions, **A lighting voltage stays
    below the starting voltage**, **A starting voltage stays below
    `max_voltage`**, and **An ATO minimum voltage stays on the measured part of
    the curve**. Plain reasons, each naming the profile."""
    pid = profile.profile_id
    problems: List[str] = []
    for reason in curve_problems(profile.voltage_speed_curve, profile.max_voltage):
        problems.append(f"profile {pid}: {reason}")
    starting = float(profile.starting_voltage or 0.0)
    if starting < 0.0:
        problems.append(f"profile {pid}: the starting voltage must not be below 0")
    if starting >= float(profile.max_voltage) - _VOLTAGE_EPSILON:
        problems.append(
            f"profile {pid}: starting voltage {starting:g} V is not below "
            f"max_voltage {float(profile.max_voltage):g} V"
        )
    lighting = profile.lighting_voltage
    if lighting is not None and lighting >= starting:
        problems.append(
            f"profile {pid}: lighting voltage {lighting:g} V is not below the "
            f"starting voltage {starting:g} V"
        )
    ato_min = profile.ato_min_voltage
    if ato_min is not None:
        low = profile.first_point_voltage
        high = float(profile.max_voltage)
        if ato_min < low - _VOLTAGE_EPSILON or ato_min > high + _VOLTAGE_EPSILON:
            problems.append(
                f"profile {pid}: ATO minimum voltage {ato_min:g} V must lie from "
                f"the curve's first point above the starting voltage ({low:g} V) "
                f"up to max_voltage ({high:g} V)"
            )
    return problems


def settle_profile_writes(
    registry: SpeedProfileRegistry,
    replacements: Dict[int, SpeedProfile],
    *,
    keep_ato_min: Iterable[int] = (),
) -> Tuple[Dict[int, SpeedProfile], List[str]]:
    """The records a write actually stores, and what it would break.

    Each record whose `starting_voltage` or `voltage_speed_curve` differs from
    the stored one loses its `ato_min_voltage` in the same write (design_new.md
    "SpeedProfile", A changed starting voltage or curve clears the ATO minimum
    voltage) -- except a record named in `keep_ato_min`, whose own request sets
    a new one. Each record is then checked by `profile_problems`. Every record
    is complete and stands alone, so a write reaches only the records it names.
    The caller stores nothing when the reasons are not empty. The hand edit,
    the speed calibration commit, the trial saves and the ATO minimum test
    commit all write through here.
    """
    keep = {as_profile_id(k) for k in keep_ato_min}
    final: Dict[int, SpeedProfile] = {}
    problems: List[str] = []
    for pid, record in replacements.items():
        stored = registry.get(pid)
        if stored is not None and record.ato_min_voltage is not None and pid not in keep:
            changed = stored.starting_voltage != record.starting_voltage or list(
                stored.voltage_speed_curve
            ) != list(record.voltage_speed_curve)
            if changed:
                record = replace(record, ato_min_voltage=None)
        final[pid] = record
        problems.extend(profile_problems(record))
    return final, problems


# ---------------------------------------------------------------- reads ----


def list_profiles(state: AppState) -> List[SpeedProfile]:
    """Every registered profile: the Default profile, every set, and every
    train's instance profile."""
    with state.simulation_lock:
        return state.speed_profile_registry.profiles()


def get_profile(state: AppState, profile_id: Any) -> Optional[SpeedProfile]:
    """Return the profile, or None when not found."""
    with state.simulation_lock:
        return state.speed_profile_registry.get(profile_id)


def require_profile(state: AppState, profile_id: Any) -> SpeedProfile:
    """Return the profile or raise NotFoundError."""
    profile = get_profile(state, profile_id)
    if profile is None:
        raise NotFoundError(f"Speed profile {profile_id!r} not found")
    return profile


def profile_view(state: AppState, profile_id: Any) -> Optional[Dict[str, Any]]:
    """The profile as the Speed Profile panel shows it, or None when absent.

    The stored record, plus which kind it is, and for an instance profile where
    it came from by name (design_new.md "SpeedProfile", A train shows where its
    profile came from), and for a set its model's name.
    """
    with state.simulation_lock:
        registry = state.speed_profile_registry
        profile = registry.get(profile_id)
        if profile is None:
            return None
        view = profile.to_dict()
        view["kind"] = (
            "default" if profile.is_default else "set" if profile.is_set else "instance"
        )
        if profile.is_instance:
            view["parent"] = registry.parent_label(profile)
        if profile.is_set:
            model = registry.get_model(profile.model_id)
            view["model_name"] = model.name if model else ""
        return view


# ----------------------------------------------------------- hand edits ----


def update_profile(
    state: AppState, profile_id: Any, *, data: Dict[str, Any]
) -> SpeedProfile:
    """Apply a hand edit to a set or to a train's instance profile.

    design_new.md "SpeedProfile", Editing a profile by hand:

    * The Default profile is not edited (The Default profile is fixed).
    * Every field the body leaves out keeps its stored value.
    * ``calibrated_at`` is never edited by hand; the stored value wins.
    * ``profile_id``, ``model_id`` and ``parent_profile_id`` are never edited
      by hand; a body that changes one is refused.
    * ``set_name`` changes only on a set, and must not be blank there; a body
      that sets it on an instance profile is refused.
    * The record must still pass ``profile_problems``, and an edit that changes
      the starting voltage or the curve clears the ATO minimum voltage unless
      the same body sets one (``settle_profile_writes``).

    Raises:
        ValidationError when the body breaks one of those rules, a voltage is
            below 0, the edit breaks the profile, or the data does not parse.
        ConflictError when the profile is the Default profile.
        NotFoundError when no profile has that id.
    """
    data = dict(data or {})
    data.pop("calibrated_at", None)
    curve = data.get("voltage_speed_curve")
    if curve is not None and len(curve) < 2:
        raise ValidationError("voltage_speed_curve needs at least two points")
    lighting = data.get("lighting_voltage")
    if lighting is not None and float(lighting) < 0.0:
        raise ValidationError("lighting_voltage must not be below 0")
    if data.get("starting_voltage") is not None and float(data["starting_voltage"]) < 0.0:
        raise ValidationError("starting_voltage must not be below 0")

    with state.simulation_lock:
        registry = state.speed_profile_registry
        existing = registry.get(profile_id)
        if existing is None:
            raise NotFoundError(f"Speed profile {profile_id!r} not found")
        if existing.is_default:
            raise ConflictError("The Default profile cannot be changed")
        _refuse_edit_while_calibration_holds(state, existing.profile_id)
        for key in ("profile_id", "model_id", "parent_profile_id"):
            if key in data and as_profile_id(data[key]) != getattr(existing, key):
                raise ValidationError(f"{key} is not edited by hand")
            data.pop(key, None)
        if "set_name" in data:
            if not existing.is_set:
                raise ValidationError("set_name belongs to a set")
            data["set_name"] = str(data["set_name"] or "").strip()
            if not data["set_name"]:
                raise ValidationError("A set's name must not be blank")

        merged = existing.to_dict()
        merged.update(data)
        try:
            updated = SpeedProfile.from_dict(merged)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"Invalid profile data: {exc}") from exc
        keep = (existing.profile_id,) if "ato_min_voltage" in data else ()
        final, problems = settle_profile_writes(
            registry, {existing.profile_id: updated}, keep_ato_min=keep
        )
        if problems:
            raise ValidationError("; ".join(problems))
        for record in final.values():
            registry.register(record)
        return final[existing.profile_id]


def _refuse_edit_while_calibration_holds(state: AppState, profile_id: int) -> None:
    """Refuse a hand edit of a train's instance profile while a calibration run
    is open for that train or its finished speed pass waits (design_new.md
    "SpeedProfile", A train's instance profile is not edited while calibration
    holds it): the edit would change the curve and voltages under the run.
    A set belongs to no train, so it is never refused here."""
    sim = getattr(state, "sim_controller", None)
    calibration = getattr(sim, "speed_calibration", None) if sim else None
    runs = getattr(calibration, "runs", {}) if calibration is not None else {}
    if not runs:
        return
    for train_id, train in getattr(state.network, "trains", {}).items():
        if getattr(train, "speed_profile_id", None) == profile_id and train_id in runs:
            raise ConflictError(
                f"Train {train_id!r} has a calibration run open or a finished "
                "pass waiting; commit or discard it first"
            )


# -------------------------------------------------------------- deletes ----


def delete_profile(state: AppState, profile_id: Any) -> List[int]:
    """Delete a set and return the numbers removed.

    design_new.md "SpeedProfile", Deleting a set or a model: the Default
    profile and an instance profile are refused -- an instance profile goes
    only with its train. No delete is refused because a train came from the
    set; every train runs on its own copy.

    Raises:
        NotFoundError when no profile has that id.
        ConflictError when the profile is the Default profile or an instance
            profile.
    """
    with state.simulation_lock:
        registry = state.speed_profile_registry
        profile = registry.get(profile_id)
        if profile is None:
            raise NotFoundError(f"Speed profile {profile_id!r} not found")
        if profile.is_default:
            raise ConflictError("The Default profile cannot be deleted")
        if profile.is_instance:
            raise ConflictError(
                "A train's instance profile goes only with its train; delete "
                "the train instead"
            )
        registry.remove(profile.profile_id)
        return [profile.profile_id]


# --------------------------------------------------------------- models ----


def list_models(state: AppState) -> List[LocomotiveModel]:
    with state.simulation_lock:
        return state.speed_profile_registry.models()


def rename_model(state: AppState, model_id: Any, *, name: Any) -> LocomotiveModel:
    """Rename a model (design_new.md "SpeedProfile", Renaming a model moves
    nothing). Raises ValidationError for a blank name, NotFoundError for an
    unknown model."""
    new_name = str(name or "").strip()
    if not new_name:
        raise ValidationError("A model's name must not be blank")
    with state.simulation_lock:
        model = state.speed_profile_registry.get_model(model_id)
        if model is None:
            raise NotFoundError(f"Model {model_id!r} not found")
        model.name = new_name
        return model


def delete_model(state: AppState, model_id: Any) -> List[int]:
    """Delete a model and every set naming it; return the set numbers removed
    (design_new.md "SpeedProfile", Deleting a set or a model)."""
    with state.simulation_lock:
        registry = state.speed_profile_registry
        if registry.get_model(model_id) is None:
            raise NotFoundError(f"Model {model_id!r} not found")
        return registry.remove_model(model_id)


__all__ = [
    "DEFAULT_PROFILE_ID",
    "IDENTITY_FIELDS",
    "delete_model",
    "delete_profile",
    "get_profile",
    "list_models",
    "list_profiles",
    "profile_problems",
    "profile_view",
    "rename_model",
    "require_profile",
    "settle_profile_writes",
    "update_profile",
]
