"""Persisted selection of the next operating environment.

This is deliberately separate from ``hardware_config``: choosing Simulation,
Virtual Hardware, or Real Hardware does not edit the rig's chips or bindings,
and loading a layout's Hardware Setup never changes this selection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from enums import OperationMode


ENVIRONMENT_PATH = "operation_environment.json"
_selected = OperationMode.SIMULATION


def get_selected() -> OperationMode:
    return _selected


def as_dict() -> dict[str, str]:
    return {"selected": _selected.value}


def parse(value: Any) -> OperationMode:
    if isinstance(value, OperationMode):
        return value
    text = str(value or "").strip().lower()
    legacy = {
        "sim": OperationMode.SIMULATION,
        "simulation": OperationMode.SIMULATION,
        "develop": OperationMode.VIRTUAL_HARDWARE,
        "developer": OperationMode.VIRTUAL_HARDWARE,
        "virtual": OperationMode.VIRTUAL_HARDWARE,
        "virtual_hardware": OperationMode.VIRTUAL_HARDWARE,
        "real": OperationMode.REAL_HARDWARE,
        "real_hardware": OperationMode.REAL_HARDWARE,
    }
    try:
        return legacy[text]
    except KeyError as exc:
        allowed = ", ".join(mode.value for mode in OperationMode)
        raise ValueError(f"environment must be one of {allowed}, got {value!r}") from exc


def select(value: Any, *, persist: bool = True) -> OperationMode:
    global _selected
    _selected = parse(value)
    if persist:
        save_to_disk()
    return _selected


def select_from_legacy_flags(
    *, enabled: Any, develop: Any, persist: bool = True
) -> OperationMode:
    """Temporary API migration adapter for the old two-toggle frontend."""
    target = (
        OperationMode.SIMULATION
        if not bool(enabled)
        else OperationMode.VIRTUAL_HARDWARE
        if bool(develop)
        else OperationMode.REAL_HARDWARE
    )
    return select(target, persist=persist)


def save_to_disk() -> None:
    path = Path(ENVIRONMENT_PATH)
    path.write_text(json.dumps(as_dict(), indent=2, sort_keys=True) + "\n")


def load_from_disk() -> OperationMode:
    path = Path(ENVIRONMENT_PATH)
    if not path.exists():
        return _selected
    try:
        data = json.loads(path.read_text())
        return select(data.get("selected"), persist=False)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _selected


def reset_to_defaults() -> OperationMode:
    return select(OperationMode.SIMULATION, persist=False)
