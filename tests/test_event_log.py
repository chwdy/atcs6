"""C6 — Event log: operational-kind vocabulary + disk persistence + read-back.

design_new.md "Event log": the event log is the `NotificationCenter` stream kept
on disk and surfaced for review. It is not a second sink — every event still
flows through `NotificationCenter`, which assigns order (`notification_id`) and
schema; the log is that one stream persisted (append-only) and read back in id
order. The read-only viewer and replay layers consume the persisted records (the
frontend panels are out of this doc's scope).
"""

from notification_center import EventKind, Notification, NotificationCenter, Severity


def test_notification_roundtrip():
    n = Notification(
        notification_id=7, event_time=12.5, source="PathDispatcher",
        kind=EventKind.PATH_COMPLETED, severity=Severity.INFO,
        train_id="T1", path_id="P1", payload={"x": 1},
    )
    assert Notification.from_dict(n.to_dict()) == n


def test_event_kind_vocabulary():
    # design_new.md "Event log" operational kinds.
    assert EventKind.TRAIN_PLACED == "train_placed"
    assert EventKind.BLOCK_ENTERED == "block_entered"
    assert EventKind.BLOCK_VACATED == "block_vacated"
    assert EventKind.SWITCH_THROWN == "switch_thrown"
    assert EventKind.PATH_COMPLETED == "path_completed"
    assert EventKind.EB_ACTIVATED == "eb_activated"


def test_disk_persistence_and_readback(tmp_path):
    path = str(tmp_path / "events.jsonl")
    nc = NotificationCenter()
    nc.enable_event_log(path)
    nc.emit(
        source="PathDispatcher", kind=EventKind.PATH_COMPLETED,
        severity=Severity.INFO, event_time=1.0, train_id="T1", path_id="P1",
        payload={"k": "v"},
    )
    nc.emit(
        source="BlockManager", kind=EventKind.BLOCK_ENTERED,
        severity=Severity.INFO, event_time=2.0, train_id="T1",
    )
    events = NotificationCenter.read_event_log(path)
    assert [e.notification_id for e in events] == [1, 2]  # id order
    assert events[0].kind == EventKind.PATH_COMPLETED
    assert events[0].payload == {"k": "v"}
    assert events[1].kind == EventKind.BLOCK_ENTERED


def test_in_memory_unaffected_when_no_disk_log(tmp_path):
    nc = NotificationCenter()
    nc.emit(source="X", kind="diagnostic", severity=Severity.INFO, event_time=0.0)
    # in-memory archive still works; nothing written to disk
    assert len(nc.peek_recent(10)) == 1
    assert NotificationCenter.read_event_log(str(tmp_path / "missing.jsonl")) == []


def test_read_missing_log_is_empty():
    assert NotificationCenter.read_event_log("/nonexistent/path/events.jsonl") == []


def test_bootstrap_persists_operational_events(tmp_path):
    """bootstrap_runtime(event_log_path=...) wires NotificationCenter persistence
    at boot, and demo placement emits TRAIN_PLACED operational events to the
    on-disk log (design_new.md "Event log": persistence + operational kinds).
    Also pins the canonical `train_placed` kind (not legacy `train_added`)."""
    import bootstrap

    path = str(tmp_path / "event_log.jsonl")
    _network, _pd, sim, _pm = bootstrap.bootstrap_runtime(
        demo=True, setup_train=True, event_log_path=path,
    )
    # Persistence was wired at boot (opt-in via event_log_path).
    assert sim.notification_center._log_path == path
    # Demo train placement emitted TRAIN_PLACED events, persisted to disk.
    events = NotificationCenter.read_event_log(path)
    placed = [e for e in events if e.kind == EventKind.TRAIN_PLACED]
    assert placed, "expected TRAIN_PLACED events from demo train placement"
    assert all(e.kind != "train_added" for e in events), (
        "train placement must use the canonical 'train_placed' kind"
    )


class _SimWithNotifications:
    """Minimal back-reference exposing only `notification_center`, which is
    all `BlockManager._emit_event` resolves through."""

    def __init__(self, nc):
        self.notification_center = nc


def test_block_manager_emits_block_entered_and_vacated():
    """design_new.md "Event log": entering and vacating a block are
    operational events. The *live* BlockManager — not just a hand-built
    Notification — must emit BLOCK_ENTERED on a real occupation transition
    and BLOCK_VACATED on a clean tail-vacate, with the exit edge on the
    vacate payload."""
    from block import Block
    from block_manager import BlockManager

    nc = NotificationCenter()
    blocks = {"B1": Block("B1", ordered_tiles=[(0, 0, "W")])}
    manager = BlockManager(blocks, simulation_controller=_SimWithNotifications(nc))

    result = manager.occupy_blocks(["B1"], "T1")
    assert bool(result)
    entered = [n for n in nc.peek_recent(20) if n.kind == EventKind.BLOCK_ENTERED]
    assert entered, "occupy_blocks must emit BLOCK_ENTERED"
    assert entered[-1].payload.get("block_id") == "B1"
    assert entered[-1].train_id == "T1"

    ok = manager.on_block_tail_vacated("B1", "T1", "E")
    assert ok is True
    vacated = [n for n in nc.peek_recent(20) if n.kind == EventKind.BLOCK_VACATED]
    assert vacated, "on_block_tail_vacated must emit BLOCK_VACATED"
    assert vacated[-1].payload.get("block_id") == "B1"
    assert vacated[-1].payload.get("exit_edge") == "E"


def test_block_reoccupy_does_not_re_emit_block_entered():
    """An idempotent re-occupy of a block the train already holds must NOT
    emit a second BLOCK_ENTERED — the event fires only on a real transition
    into OCCUPIED."""
    from block import Block
    from block_manager import BlockManager

    nc = NotificationCenter()
    blocks = {"B1": Block("B1", ordered_tiles=[(0, 0, "W")])}
    manager = BlockManager(blocks, simulation_controller=_SimWithNotifications(nc))

    manager.occupy_blocks(["B1"], "T1")
    manager.occupy_blocks(["B1"], "T1")  # idempotent re-occupy

    entered = [n for n in nc.peek_recent(20) if n.kind == EventKind.BLOCK_ENTERED]
    assert len(entered) == 1
