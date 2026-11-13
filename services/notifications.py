"""Shared `command_result` notification emit for service-layer commands.

design_v2.md "Runtime notifications", Command execution results: every
entity-flag PATCH endpoint surfaces its outcome as one
canonical `command_result` event on `NotificationCenter` with the closed-core
payload `{command_id, <entity>_id, success, reason}`; the command name rides the
event's `operation` field, not the payload. This is the single emit helper those
handlers route through so HTTP and non-HTTP callers alike observe the same event
stream.
"""

import uuid
from typing import Optional

from app_state import AppState


def emit_command_result(
    state: AppState,
    *,
    source: str,
    entity_id_key: str,
    entity_id: str,
    command_name: str,
    success: bool,
    reason: str = "",
    command_id: Optional[str] = None,
) -> str:
    """Emit one `command_result` event; never raises.

    Returns the (possibly generated) `command_id` so a caller can correlate.
    Notification failure must not affect the caller, so every step is guarded —
    a missing `NotificationCenter` (pre-init / editor-only window) is a no-op.
    """
    resolved_command_id = command_id or str(uuid.uuid4())
    try:
        from notification_center import EntityRef, Kind

        sim_controller = getattr(state, "sim_controller", None)
        notification_center = getattr(sim_controller, "notification_center", None)
        if notification_center is None:
            return resolved_command_id
        # `entity_id_key` is "<entity>_id"; the entity type is that prefix.
        entity_type = entity_id_key[:-3] if entity_id_key.endswith("_id") else ""
        notification_center.emit(
            source=source,
            operation=command_name,
            kind=Kind.INFO if success else Kind.WARNING,
            message=reason or (
                f"{command_name} accepted" if success
                else f"{command_name} rejected"
            ),
            entities=(
                [EntityRef(entity_type, entity_id)] if entity_type else []
            ),
            payload={
                "command_id": resolved_command_id,
                entity_id_key: entity_id,
                "success": success,
                "reason": reason or "",
            },
        )
    except Exception:
        pass  # notification failure must not affect the command result
    return resolved_command_id
