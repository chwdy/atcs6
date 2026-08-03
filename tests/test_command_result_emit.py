"""A3 — entity-flag PATCH endpoints emit a canonical command_result.

design_new.md line 658: all four entity-flag PATCH endpoints (block / switch /
platform / station) emit one `command_result` notification with payload
`{command_id, command_name, <entity>_id, success, reason}`, on success and on
the 409 admin-reject alike. This exercises the shared emit helper they route
through (`services.notifications.emit_command_result`).
"""

from notification_center import NotificationCenter
from services.notifications import emit_command_result


class _SimCtrl:
    def __init__(self) -> None:
        self.notification_center = NotificationCenter()
        self.simulation_time = 0.0


class _State:
    def __init__(self) -> None:
        self.sim_controller = _SimCtrl()


def _command_results(state):
    return [
        n
        for n in state.sim_controller.notification_center.peek_recent(20)
        if n.payload.get("command_id")
    ]


def test_emit_success_payload_shape():
    state = _State()
    cid = emit_command_result(
        state, source="Block", entity_id_key="block_id", entity_id="BLK_1",
        command_name="update_flags", success=True,
    )
    events = _command_results(state)
    assert len(events) == 1
    assert events[0].source == "Block"
    assert events[0].payload == {
        "command_id": cid,
        "command_name": "update_flags",
        "block_id": "BLK_1",
        "success": True,
        "reason": "",
    }


def test_emit_failure_carries_reason():
    """The 409 admin-reject path emits success=False with the rejection reason."""
    state = _State()
    cid = emit_command_result(
        state, source="Switch", entity_id_key="switch_id", entity_id="SW_1",
        command_name="update_flags", success=False, reason="Switch is occupied",
        command_id="CMD-9",
    )
    assert cid == "CMD-9"
    events = _command_results(state)
    assert len(events) == 1
    assert events[0].payload["switch_id"] == "SW_1"
    assert events[0].payload["success"] is False
    assert events[0].payload["reason"] == "Switch is occupied"
    assert events[0].payload["command_id"] == "CMD-9"


def test_emit_noop_without_notification_center():
    """A missing NotificationCenter (editor-only / pre-init) is a silent no-op."""

    class _NoCtrlState:
        sim_controller = None

    cid = emit_command_result(
        _NoCtrlState(), source="Platform", entity_id_key="platform_id",
        entity_id="PLT_1", command_name="update_flags", success=True,
    )
    assert isinstance(cid, str) and cid  # still returns a usable id
