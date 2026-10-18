"""Layout persistence service.

Save / load / list / delete saved layouts. Wraps `PersistenceManager`
and keeps the shared "pause-while-saving" pattern in one place so both
the HTTP handlers and any non-HTTP caller observe it.

The save-step indication and recovery flag are returned as plain dicts;
HTTP handlers translate them into wire JSON.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from app_state import AppState
from services.exceptions import NotFoundError, ValidationError


def list_layouts(state: AppState) -> List[Dict[str, Any]]:
    """Return the persistence-managers raw layout descriptors."""
    return state.persistence_manager.list_layouts()


def save_layout(state: AppState, *, name: str, description: str = "") -> Dict[str, Any]:
    """Save the current network as a layout, together with the current hardware
    configuration (the binding maps are keyed by the layout's own ids).

    Returns:
        Dict with keys `path`, `steps_generated`, `steps_count`, and
        optionally `steps_error` -- the same shape `save_merged_layout`
        returns to its caller.

    Raises:
        ValidationError when the name is blank, or when the persistence
            manager raises ValueError.
        IOError when the persistence manager fails to write.
    """
    name = (name or "").strip()
    if not name:
        raise ValidationError("Layout name is required")

    was_running = state.simulation_running
    state.simulation_running = False

    try:
        with state.simulation_lock:
            try:
                result = state.persistence_manager.save_merged_layout(
                    name, state.network, description
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            state.last_autosave_time = time.time()
        return result
    finally:
        state.simulation_running = was_running


def load_layout(state: AppState, name: str) -> Dict[str, Any]:
    """Load a layout by name, replacing the current network state.

    Also adopts the layout's saved hardware configuration (see
    `PersistenceManager._restore_hardware`), which reaches only the next
    session build, not the session this call runs inside.

    Returns:
        The persistence-managers result dict (`success`, `method`,
        `hardware`, optionally `recovery`, `hardware_error`, `error`).

    Raises:
        NotFoundError when the persistence manager raises FileNotFoundError.
        ValidationError when the load fails for an input reason.
    """
    was_running = state.simulation_running
    state.simulation_running = False

    try:
        with state.simulation_lock:
            # The trains are about to be replaced, so every calibration run ends
            # and every held result is discarded first (design_new.md "What ends
            # a calibration run").
            state.sim_controller.speed_calibration.discard_all(
                "calibration ended: a layout was loaded"
            )
            try:
                result = state.persistence_manager.load_layout_with_fallback(
                    name, state.network
                )
            except FileNotFoundError as exc:
                raise NotFoundError(str(exc)) from exc
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            if not result.get("success"):
                raise ValidationError(
                    result.get("error", "Failed to load layout"),
                )

            state.sim_controller.network = state.network
            state.last_autosave_time = time.time()
        return result
    finally:
        state.simulation_running = was_running


def delete_layout(state: AppState, name: str) -> bool:
    """Delete a saved layout.

    Returns:
        True on success.

    Raises:
        NotFoundError when no layout exists with that name.
    """
    if not state.persistence_manager.delete_layout(name):
        raise NotFoundError(f"Layout {name!r} not found")
    return True
