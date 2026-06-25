"""Switch lifecycle service.

CRUD over ``Switch`` records plus the two operations that must coordinate
more than one collaborator:

  * Toggling the ``disabled`` availability flag runs the admin-reject
    preconditions (occupancy / future locks / train-protection claims)
    and, when the value actually changes, notifies ``BlockManager`` that
    connectivity changed -- per design_new.md "Availability flag REST API
    surface".
  * Changing a switch's commanded position routes through
    ``SwitchManager`` (so the BlockManager notification stays paired with
    the position write) and emits a ``command_result`` event on
    ``NotificationCenter`` -- so HTTP and non-HTTP callers alike observe
    the outcome on the event stream rather than a synchronous return.

The HTTP layer in ``routes/switches.py`` parses JSON and maps the typed
exceptions raised here to status codes. Unlike ``speed_profile_service``
this layer takes no ``simulation_lock`` -- the switch routes never did.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app_state import AppState
from services.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from services.validation import coerce_nonneg
from switch import Switch


def list_switches(state: AppState) -> List[Switch]:
    """Return every registered switch."""
    return list(state.network.switches.values())


def get_switch(state: AppState, switch_id: str) -> Optional[Switch]:
    """Return the switch, or None when not found."""
    return state.network.get_switch(switch_id)


def get_switch_at(state: AppState, x: int, y: int) -> Optional[Switch]:
    """Return the switch registered at a tile, or None."""
    return state.network.get_switch_at(x, y)


def create_switch_at_track(
    state: AppState, x: int, y: int, *, data: Optional[Dict[str, Any]] = None
) -> Switch:
    """Create the switch config for a switch-track tile (idempotent).

    When a switch already exists at the tile it is returned unchanged --
    the caller decides whether that is a 200 (already present) or a 201
    (freshly created) by checking ``get_switch_at`` beforehand.

    Raises:
        NotFoundError when there is no track at the tile.
        ValidationError when the track exists but is not a switch.
    """
    track = state.network.get_track(x, y)
    if not track:
        raise NotFoundError("No track at position")
    if not track.is_switch:
        raise ValidationError("Track is not a switch")

    existing = state.network.get_switch_at(x, y)
    if existing is not None:
        return existing

    data = dict(data or {})
    # Default to 0.0 cm for both branches when the caller omits geometry.
    switch = Switch(
        switch_id=f"SW_{x}_{y}",
        x=x,
        y=y,
        main_length_cm=data.get("main_length_cm", 0.0),
        diverge_length_cm=data.get("diverge_length_cm", 0.0),
        main_radius_cm=data.get("main_radius_cm"),
        diverge_radius_cm=data.get("diverge_radius_cm"),
        name=data.get("name", ""),
        switch_type=data.get("switch_type", "standard"),
        notes=data.get("notes", ""),
    )
    state.network.add_switch(switch)
    return switch


def update_switch(
    state: AppState, switch_id: str, *, data: Dict[str, Any]
) -> Switch:
    """Apply an update to a switch's config and/or availability flags.

    ``disabled`` and ``manual`` are handled here; any remaining fields are
    forwarded to ``RailwayNetwork.update_switch``. Enabling ``disabled``
    runs the admin-reject preconditions and, when the flag's value really
    changes, fires ``BlockManager.on_switch_position_changed``.

    Raises:
        NotFoundError when no switch has that id.
        ConflictError when ``disabled`` is being set True while the switch
            is occupied, holds future locks, or holds train-protection
            claims.
    """
    switch = state.network.get_switch(switch_id)
    if switch is None:
        raise NotFoundError("Switch not found")

    # Pull the availability flags out before forwarding the rest; we own
    # the admin-reject + BlockManager notify for those two here.
    data = dict(data or {})
    disabled_change = data.pop("disabled", None)
    manual_change = data.pop("manual", None)

    if disabled_change is not None:
        new_disabled = bool(disabled_change)
        if new_disabled and not switch.disabled:
            if switch.occupied_by is not None:
                raise ConflictError(f"Switch is occupied by {switch.occupied_by}")
            if switch.locks:
                raise ConflictError("Switch has live future locks")
            if switch.train_protection_claims:
                raise ConflictError("Switch has live train-protection claims")
        disabled_actually_changed = new_disabled != switch.disabled
        switch.disabled = new_disabled
        # Connectivity changed -- notify BlockManager, but only on a real
        # value change (the UI's availability segment always sends both
        # disabled and manual together).
        if disabled_actually_changed:
            block_manager = getattr(state.network, "block_manager", None)
            if block_manager is not None:
                try:
                    block_manager.on_switch_position_changed(switch_id)
                except Exception:
                    pass
            # design_new.md line 708: the disabled toggle is a topology-
            # availability change, so neighbouring switches' stored
            # flank_protection can go stale against the opened/closed circuit.
            # Recompute network-wide (the same recompute the add/remove/update-
            # switch topology paths run; cheap and only fires on this edit).
            recompute = getattr(
                state.network, "recompute_all_flank_protection", None
            )
            if callable(recompute):
                try:
                    recompute()
                except Exception:
                    pass

    if manual_change is not None:
        switch.manual = bool(manual_change)

    # Geometry config validation (the UI enters whole mm and sends cm). The
    # two path lengths are required non-negative numbers; the two radii accept
    # None = "no curve (straight)". All non-None values must be numbers >= 0.
    for length_field in ("main_length_cm", "diverge_length_cm"):
        if length_field in data:
            if data[length_field] is None:
                raise ValidationError(f"{length_field} is required and cannot be empty")
            data[length_field] = coerce_nonneg(data[length_field], length_field)
    for radius_field in ("main_radius_cm", "diverge_radius_cm"):
        if radius_field in data and data[radius_field] is not None:
            data[radius_field] = coerce_nonneg(data[radius_field], radius_field)

    # Forward any remaining config fields to the network's update helper.
    if data:
        switch = state.network.update_switch(switch_id, **data) or switch

    return switch


def delete_switch(state: AppState, switch_id: str) -> bool:
    """Remove a switch config. Returns True on success.

    Raises:
        NotFoundError when no switch has that id.
    """
    if not state.network.remove_switch(switch_id):
        raise NotFoundError("Switch not found")
    return True


def request_position_change(
    state: AppState,
    switch_id: str,
    *,
    new_state: int,
    command_id: Optional[str] = None,
    manual_override: bool = False,
) -> str:
    """Command a switch to MAIN (0) or DIVERGE (1).

    Routes through ``SwitchManager`` so the BlockManager notification is
    paired with the position write, then emits a ``command_result`` event
    on ``NotificationCenter``. The command outcome surfaces on that event
    stream; this function returns only the (possibly generated) command id
    the caller can correlate against.

    Raises:
        ValidationError when ``new_state`` is not 0 or 1.
        NotFoundError when no switch has that id.
        ServiceUnavailableError when the SwitchManager is not wired yet.
    """
    if new_state not in (0, 1):
        raise ValidationError(
            "Invalid state value. Must be 0 (MAIN) or 1 (DIVERGE)"
        )

    switch = state.network.switches.get(switch_id)
    if not switch:
        raise NotFoundError(f"Switch {switch_id} not found")

    switch_manager = state.network.switch_manager
    if switch_manager is None:
        raise ServiceUnavailableError(
            "SwitchManager not available — runtime not initialised"
        )

    resolved_command_id = command_id or str(uuid.uuid4())

    success, message = switch_manager.request_position_change(
        switch_id, new_state, manual_override=bool(manual_override)
    )

    _emit_command_result(state, switch_id, resolved_command_id, success, message)

    return resolved_command_id


def _emit_command_result(
    state: AppState,
    switch_id: str,
    command_id: str,
    success: bool,
    message: str,
) -> None:
    """Emit a Switch ``command_result`` event; never raises.

    Delegates to the shared `command_result` emit helper (the single canonical
    emit path, design_new.md "Runtime notifications") with
    `command_name = "change_state"`.
    """
    from services.notifications import emit_command_result

    emit_command_result(
        state,
        source="Switch",
        entity_id_key="switch_id",
        entity_id=switch_id,
        command_name="change_state",
        success=success,
        reason=message,
        command_id=command_id,
    )
