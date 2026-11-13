"""The shared command-result emit helper
(`services.notifications.emit_command_result`).

design_v2.md "Runtime notifications", Command execution results: a command's
result is one event whose payload is `{command_id, success, reason}` plus the
id field of the one entity it targets, on acceptance and on rejection alike;
the command name rides the event's `operation` field, not the payload.
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
    # The command name rides `operation`, not the payload (closed core only).
    assert events[0].operation == "update_flags"
    assert events[0].payload == {
        "command_id": cid,
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
