"""The locomotive file: where speed profiles and the train roster are kept.

design_new.md "SpeedProfile", Where profiles and the roster are kept. Every
`SpeedProfile` and every `RosterEntry` lives in one app-managed JSON file,
`locomotives.json`, not in a layout save file, so a measured curve survives a
restart, loading another layout, and clearing the network.

The file is off until `enable(path)` names it. Only the real server does that
(through `bootstrap_runtime(locomotive_file_path=...)`), so unit tests never
read or write the developer's file.

- `load_into(network, notification_center)` runs once, at startup, after the
  scene and its trains exist. A file record replaces a scene record with the
  same id.
- `save_if_changed(network, notification_center)` runs after every request that
  can change state, and rewrites the file only when the profiles or the roster
  differ from what it last wrote.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from notification_center import Kind
from runtime_payloads import ErrorRecord

LOCOMOTIVE_FILE_PATH = "locomotives.json"
UNREADABLE_SUFFIX = ".unreadable"
FILE_VERSION = 1

_SOURCE = "LocomotiveFile"

# The file this process reads and writes, or None while the file is off.
_path: Optional[str] = None
# What the last successful write (or the startup read) put in the file, so an
# unchanged request writes nothing.
_last_written: Optional[Dict[str, Any]] = None


def enable(path: str = LOCOMOTIVE_FILE_PATH) -> None:
    """Turn the file on for this process, naming where it lives."""
    global _path, _last_written
    _path = str(path)
    _last_written = None


def disable() -> None:
    """Turn the file off (tests use this to restore the default)."""
    global _path, _last_written
    _path = None
    _last_written = None


def enabled_path() -> Optional[str]:
    return _path


def contents(network) -> Dict[str, Any]:
    """What the file holds for `network`: every saved profile and roster entry.

    The seeded pair an unidentified train carries is left out
    (design_new.md "Unidentified trains", Runtime only), through
    `RailwayNetwork.saved_speed_profiles()`.
    """
    return {
        "version": FILE_VERSION,
        "speed_profiles": network.saved_speed_profiles(),
        "train_roster": network.train_roster.to_dict(),
    }


def _emit_error(notification_center, operation: str, message: str, detail: str) -> None:
    print(f"[LocomotiveFile] {message}: {detail}")
    if notification_center is None:
        return
    notification_center.emit(
        source=_SOURCE,
        operation=operation,
        kind=Kind.ERROR,
        message=message,
        entities=[],
        error=ErrorRecord(detail, "locomotive_file"),
    )


def load_into(network, notification_center=None) -> bool:
    """Add every profile and roster entry in the file to `network`.

    Merges in place, so every holder of `network.speed_profiles` (the
    simulation controller keeps a reference) sees the loaded records. Returns
    True when the file was read. A missing file reads as empty. An unreadable
    file is renamed to `<name>.unreadable` so the next write cannot overwrite
    the operator's data, and one error event says so.
    """
    global _last_written
    if _path is None:
        return False
    path = Path(_path)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("the file does not hold a JSON object")
        from speed_profile import SpeedProfile
        from train_roster import RosterEntry

        profiles = [
            SpeedProfile.from_dict(p)
            for pid, p in (data.get("speed_profiles") or {}).items()
            if pid != "default"
        ]
        entries = [RosterEntry.from_dict(e) for e in data.get("train_roster") or []]
    except Exception as exc:  # any parse failure: keep the data, start fresh
        moved = path.with_name(path.name + UNREADABLE_SUFFIX)
        try:
            os.replace(path, moved)
            where = f"renamed to {moved.name}"
        except OSError as move_exc:
            where = f"could not be renamed ({move_exc})"
        _emit_error(
            notification_center,
            "load_into",
            f"locomotive file {path.name} could not be read and was {where}; "
            "starting from the scene's profiles and roster",
            str(exc),
        )
        return False
    for profile in profiles:
        network.speed_profiles.register(profile)
    for entry in entries:
        network.train_roster.add_entry(entry)
    _last_written = contents(network)
    return True


def save_if_changed(network, notification_center=None) -> bool:
    """Rewrite the file when the profiles or roster differ from the last write.

    Returns True when it wrote. The write goes to a temporary file first and is
    then renamed over the old one, so a crash mid-write never leaves half a file.
    A failed write emits one error event and leaves `_last_written` unchanged,
    so the next request tries again.
    """
    global _last_written
    if _path is None:
        return False
    current = contents(network)
    if current == _last_written:
        return False
    path = Path(_path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(current, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except OSError as exc:
        _emit_error(
            notification_center,
            "save_if_changed",
            f"locomotive file {path.name} could not be written; "
            "the next change tries again",
            str(exc),
        )
        return False
    _last_written = current
    return True
