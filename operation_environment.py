"""Persisted selection of the next operating environment.

This is deliberately separate from ``hardware_config``: choosing Virtual
Hardware or Real Hardware does not edit the rig's chips or bindings, and
loading a layout's Hardware Setup never changes this selection
(design_v2.md "Operating environment and activation").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from enums import OperationMode


ENVIRONMENT_PATH = "operation_environment.json"
# Virtual Hardware never opens the physical controller, so it is the
# selection a fresh checkout starts with.
DEFAULT_ENVIRONMENT = OperationMode.VIRTUAL_HARDWARE
_selected = DEFAULT_ENVIRONMENT


def get_selected() -> OperationMode:
    return _selected


def as_dict() -> dict[str, str]:
    return {"selected": _selected.value}


def parse(value: Any) -> OperationMode:
    if isinstance(value, OperationMode):
        return value
    text = str(value or "").strip().lower()
    try:
        return OperationMode(text)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in OperationMode)
        raise ValueError(f"environment must be one of {allowed}, got {value!r}") from exc


def select(value: Any, *, persist: bool = True) -> OperationMode:
    global _selected
    _selected = parse(value)
    if persist:
        save_to_disk()
    return _selected


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
    return select(DEFAULT_ENVIRONMENT, persist=False)
