"""C6 — Event log: operational-kind vocabulary + disk persistence + read-back.

design_new.md "Event log": the event log is the `NotificationCenter` stream kept
on disk and surfaced for review. It is not a second sink — every event still
flows through `NotificationCenter`, which assigns order (`notification_id`) and
schema; the log is that one stream persisted (append-only) and read back in id
order. The read-only viewer and replay layers consume the persisted records (the
frontend panels are out of this doc's scope).
"""

import pytest

from notification_center import (
    Kind,
    Notification,
    NotificationCenter,
    make_notification_emitter,
    path_ref,
    route_run_ref,
    train_ref,
)


def test_notification_roundtrip():
    n = Notification(
        notification_id=7, event_time=12.5, source="PathDispatcher",
        operation="release_path", kind=Kind.INFO, message="path P1 completed",
        entities=[train_ref("T1"), path_ref("P1")], payload={"x": 1},
    )
    assert Notification.from_dict(n.to_dict()) == n


def test_event_kind_vocabulary():
    # design_new.md "Event log" operational kinds.
    # `kind` is the level, and the only field runtime code may branch on.
    # What happened is `source` + `operation` (design_new.md "Event log").
    assert Kind.DEBUG == "debug"
    assert Kind.INFO == "info"
    assert Kind.WARNING == "warning"
    assert Kind.ERROR == "error"


def test_disk_persistence_and_readback(tmp_path):
    path = str(tmp_path / "events.jsonl")
    nc = NotificationCenter()
    nc.enable_event_log(path)
    nc.emit(
        source="PathDispatcher", operation="release_path", kind=Kind.INFO,
        message="path P1 completed", event_time=1.0,
        entities=[train_ref("T1"), path_ref("P1")],
        payload={"k": "v"},
    )
    nc.emit(
        source="BlockManager", operation="occupy_blocks", kind=Kind.INFO,
        message="T1 entered B1", event_time=2.0, entities=[train_ref("T1")],
    )
    events = NotificationCenter.read_event_log(path)
    assert [e.notification_id for e in events] == [1, 2]  # id order
    assert events[0].operation == "release_path"
    assert events[0].payload == {"k": "v"}
    assert events[1].operation == "occupy_blocks"


def test_in_memory_unaffected_when_no_disk_log(tmp_path):
    nc = NotificationCenter()
    nc.emit(source="X", operation="op", kind=Kind.INFO, message="m", event_time=0.0)
    # in-memory archive still works; nothing written to disk
    assert len(nc.peek_recent(10)) == 1
    assert NotificationCenter.read_event_log(str(tmp_path / "missing.jsonl")) == []


def test_read_missing_log_is_empty():
    assert NotificationCenter.read_event_log("/nonexistent/path/events.jsonl") == []


def _block_emitter(nc):
    """The narrow notification callback a runtime BlockManager is handed,
    bound to this test's NotificationCenter (design_new.md "BlockManager")."""
    return make_notification_emitter(nc, "BlockManager")


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
    manager = BlockManager(blocks, notification_emitter=_block_emitter(nc))

    result = manager.occupy_blocks(["B1"], "T1")
    assert bool(result)
    entered = [n for n in nc.peek_recent(20) if n.operation == "occupy_blocks"]
    assert entered, "occupy_blocks must emit a block-entered event"
    assert entered[-1].payload.get("block_id") == "B1"
    # `entities` is the only entity routing key.
    assert entered[-1].has_entity("train", "T1")
    assert entered[-1].has_entity("block", "B1")

    ok = manager.on_block_tail_vacated("B1", "T1", "E")
    assert ok is True
    vacated = [n for n in nc.peek_recent(20) if n.operation == "on_block_tail_vacated"]
    assert vacated, "on_block_tail_vacated must emit a block-vacated event"
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
    manager = BlockManager(blocks, notification_emitter=_block_emitter(nc))

    manager.occupy_blocks(["B1"], "T1")
    manager.occupy_blocks(["B1"], "T1")  # idempotent re-occupy

    entered = [n for n in nc.peek_recent(20) if n.operation == "occupy_blocks"]
    assert len(entered) == 1


# --- Time sampling: the stream is wall-clock ---------------------------------
# design_new.md "Time sampling": every site that records a notification
# `event_time` samples wall-clock `time.time()` at the moment of the write.
# Callers do not thread a timestamp across writers, and no site stamps with the
# playback-scaled `simulation_time` (which restarts at 0.0 each run and would
# not order against its wall-clock siblings).
def test_emit_samples_wall_clock_when_no_event_time_is_passed():
    import time

    nc = NotificationCenter()
    before = time.time()
    n = nc.emit(source="X", operation="op", kind=Kind.INFO, message="m")
    after = time.time()
    assert before <= n.event_time <= after


def test_emit_still_accepts_one_local_sample_shared_with_an_entity_write():
    # The one case design_new.md allows: a single writer samples once locally
    # and uses that value for both its notification and its entity write.
    nc = NotificationCenter()
    n = nc.emit(source="X", operation="op", kind=Kind.INFO, message="m", event_time=1234.5)
    assert n.event_time == 1234.5


def test_no_source_file_stamps_event_time_with_the_simulation_clock():
    """Static guard: `simulation_time` must never reach an `event_time`.

    The sim clock starts at 0.0 and scales with playback; wall clock is ~1.7e9.
    Mixing them makes the persisted log non-orderable across event kinds. A
    dynamic test cannot reach every emit site, so this scans the source instead.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    # Matches `event_time=self.simulation_time` and
    # `event_time=getattr(sc, "simulation_time", ...)` alike.
    offender = re.compile(r"event_time\s*=[^\n]*simulation_time")
    hits = []
    for path in root.rglob("*.py"):
        parts = path.parts
        if ".claude" in parts or "tests" in parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if offender.search(line):
                hits.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not hits, "event_time stamped with the simulation clock:\n" + "\n".join(hits)


def test_detector_diagnostic_is_stamped_wall_clock():
    """The detector notify path is one of the sites that used to stamp sim time."""
    import time

    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from simulation import SimulationController

    network = RailwayNetwork()
    sim = SimulationController(network, PathDispatcher(network))
    sim.update(0.1)
    assert sim.simulation_time > 0.0  # the sim clock is small and nonzero

    sim.block_occupancy_detector.notify("block_attribution_unknown", "probe")
    emitted = sim.notification_center.peek_recent(10)
    assert emitted, "detector notify should have emitted a diagnostic"
    assert emitted[-1].event_time > time.time() - 60.0


# --- NotificationCenter is the only runtime event sink -----------------------
# design_new.md "Out of Scope", Logging beyond NotificationCenter:
# NotificationCenter is the only runtime event sink, and component-local logging
# is forbidden by design (the detector reading log records detector readings,
# not events). Routine traces must not exist in domain code. The
# surviving `logger.warning/error/exception` calls are each the last-resort
# fallback for a NotificationCenter emit that itself raised — without them an
# emit failure would be silent — so they are allowed and asserted separately.
_DOMAIN_MODULES = [
    "simulation.py",
    "route_executor.py",
    "path_dispatcher.py",
    "train_physics.py",
    "block_manager.py",
    "switch_manager.py",
    "switch.py",
    "block.py",
    "meet_coordination.py",
    "train_controller/__init__.py",
    "train_controller/commands.py",
    "train_controller/eb.py",
    "train_controller/ato.py",
    "train_controller/atc.py",
]


def _domain_sources():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in _DOMAIN_MODULES:
        path = root / rel
        if path.exists():
            yield rel, path.read_text()


def test_domain_code_emits_no_routine_logger_traces():
    import re

    routine = re.compile(r"\blogger\.(debug|info)\s*\(")
    hits = [
        f"{rel}:{i}: {line.strip()}"
        for rel, src in _domain_sources()
        for i, line in enumerate(src.splitlines(), 1)
        if routine.search(line)
    ]
    assert not hits, (
        "domain code must not log routine traces to a second sink; route them "
        "through NotificationCenter as kind='diagnostic':\n" + "\n".join(hits)
    )


def test_domain_code_does_not_print_to_stdout():
    import re

    printing = re.compile(r"(?<![\w.])print\s*\(")
    hits = [
        f"{rel}:{i}: {line.strip()}"
        for rel, src in _domain_sources()
        for i, line in enumerate(src.splitlines(), 1)
        if printing.search(line)
    ]
    assert not hits, (
        "domain code must not print; emit a NotificationCenter diagnostic "
        "instead:\n" + "\n".join(hits)
    )


def test_notification_ids_stay_monotonic_across_sessions(tmp_path):
    """design_new.md "Event log": the increasing `notification_id` is the order
    the log reads back in. The log is append-only across process restarts, so a
    fresh session must not restart numbering at 1 and duplicate ids on disk."""
    path = str(tmp_path / "event_log.jsonl")

    first = NotificationCenter()
    first.enable_event_log(path)
    first.emit(source="A", operation="op", kind=Kind.INFO, message="m")
    first.emit(source="A", operation="op", kind=Kind.INFO, message="m")

    # A restart builds a brand-new NotificationCenter against the same file.
    second = NotificationCenter()
    second.enable_event_log(path)
    second.emit(source="B", operation="op", kind=Kind.INFO, message="m")

    ids = [e.notification_id for e in NotificationCenter.read_event_log(path)]
    assert ids == [1, 2, 3], ids
    assert len(set(ids)) == len(ids), "duplicate notification_id on disk"


def test_enable_event_log_on_a_fresh_path_numbers_from_one(tmp_path):
    nc = NotificationCenter()
    nc.enable_event_log(str(tmp_path / "fresh.jsonl"))
    n = nc.emit(source="A", operation="op", kind=Kind.INFO, message="m")
    assert n.notification_id == 1


def test_corrupt_event_log_line_does_not_block_boot(tmp_path):
    path = tmp_path / "corrupt.jsonl"
    path.write_text('{"notification_id": 5}\nnot json at all\n{"no_id": true}\n')
    nc = NotificationCenter()
    nc.enable_event_log(str(path))
    assert nc.emit(source="A", operation="op", kind=Kind.INFO, message="m").notification_id == 6


# ---------------------------------------------------------------------------
# Error events: suppression + the derived "current reason" read
# ---------------------------------------------------------------------------


def test_an_unchanged_repeat_error_is_suppressed():
    """Emit on change, not per tick (design_new.md "Error events").

    Plan retries a blocked task every eligible tick, so the same entity
    blocked for the same reason under the same `condition_key` must not fill
    the stream: the second call is dropped while the key stays active.
    """
    from runtime_payloads import ErrorRecord

    nc = NotificationCenter()
    args = dict(
        source="RouteExecutor", operation="tick_train_plan", kind=Kind.WARNING,
        message="blocked", entities=[train_ref("T1")],
        condition_key="route_run:R1:planning_failed",
    )

    first = nc.emit(error=ErrorRecord("no path", "planning_failed"), **args)
    repeat = nc.emit(error=ErrorRecord("no path", "planning_failed"), **args)

    assert first is not None
    assert repeat is None, "an unchanged repeat must be dropped"
    assert len(nc.peek_recent(50)) == 1


def test_a_changed_reason_is_a_new_event():
    from runtime_payloads import ErrorRecord

    nc = NotificationCenter()
    # One live condition (one `condition_key`); a changed fingerprint under it
    # is appended and replaces the active value (design_new.md "Error events").
    args = dict(
        source="RouteExecutor", operation="tick_train_plan", kind=Kind.WARNING,
        message="blocked", entities=[train_ref("T1")],
        condition_key="route_run:R1:planning_failed",
    )

    nc.emit(error=ErrorRecord("no path", "planning_failed"), **args)
    # A different origin is a different reason...
    assert nc.emit(error=ErrorRecord("no path", "wedge"), **args) is not None
    # ...and so is a changed payload, such as a meet ceiling moving.
    assert nc.emit(
        error=ErrorRecord("no path", "wedge"), payload={"ceiling": "B2"}, **args
    ) is not None


def test_ordinary_traffic_is_never_suppressed():
    """Only diagnostic-carrying events are "the same reason" repeats; dropping
    ordinary occurrences would lose real events."""
    nc = NotificationCenter()
    for _ in range(3):
        nc.emit(
            source="BlockManager", operation="occupy_blocks", kind=Kind.INFO,
            message="T1 entered B1", entities=[train_ref("T1")],
        )
    assert len(nc.peek_recent(50)) == 3


def test_latest_error_for_answers_the_current_reason():
    from runtime_payloads import ErrorRecord

    nc = NotificationCenter()
    nc.emit(
        source="RouteExecutor", operation="tick_train_plan", kind=Kind.WARNING,
        message="blocked", entities=[train_ref("T1")],
        error=ErrorRecord("no path", "planning_failed"),
        condition_key="route_run:R1:planning_failed",
    )
    # Ordinary info traffic must not become the answer.
    nc.emit(
        source="BlockManager", operation="occupy_blocks", kind=Kind.INFO,
        message="T1 entered B1", entities=[train_ref("T1")],
    )

    found = nc.latest_error_for("train", "T1")
    assert found is not None
    assert found.error.origin == "planning_failed"

    # Narrowing by operation and origin.
    assert nc.latest_error_for("train", "T1", operation="tick_train_plan") is not None
    assert nc.latest_error_for("train", "T1", origin="wedge") is None
    # A different entity has no reason of its own.
    assert nc.latest_error_for("train", "T2") is None


def test_latest_error_for_ignores_an_error_with_no_condition_key():
    """`latest_error_for` combines the retained event row with the active-
    condition map, so an error emitted with no `condition_key` is never a
    current reason — it lives on only as history (design_new.md "Error
    events", Reading the current reason)."""
    from runtime_payloads import ErrorRecord

    nc = NotificationCenter()
    # No condition_key: always appended, but never active, so never current.
    appended = nc.emit(
        source="RouteExecutor", operation="handle_path_cancellation",
        kind=Kind.WARNING, message="cancelled", entities=[train_ref("T1")],
        error=ErrorRecord("operator cancelled", "operator_cancelled"),
    )
    assert appended is not None
    assert nc.latest_error_for("train", "T1") is None


def test_a_task_event_is_found_by_both_run_and_task_filters():
    """An event about a task names both refs, so a run-level filter and a
    task-level filter both find it while its condition is active."""
    from notification_center import route_run_ref, walk_position_ref
    from runtime_payloads import ErrorRecord

    nc = NotificationCenter()
    nc.emit(
        source="RouteExecutor", operation="tick_train_plan", kind=Kind.WARNING,
        message="task blocked",
        entities=[route_run_ref("R1"), walk_position_ref("R1", 2)],
        error=ErrorRecord("target missing", "task_skipped"),
        condition_key="route_run:R1:task_skipped",
    )

    assert nc.latest_error_for("route_run", "R1") is not None
    assert nc.latest_error_for("walk_position", "R1#2") is not None
    assert nc.latest_error_for("walk_position", "R1#3") is None


# ---------------------------------------------------------------------------
# condition_key lifecycle (design_new.md "Error events", Suppression mechanism
# + Resolution and recurrence)
# ---------------------------------------------------------------------------


def _blocked_emit(nc, key, origin="planning_failed", payload=None):
    from runtime_payloads import ErrorRecord

    return nc.emit(
        source="RouteExecutor", operation="tick_train_plan", kind=Kind.WARNING,
        message="blocked", entities=[route_run_ref("R1")],
        error=ErrorRecord("no path", origin), payload=payload,
        condition_key=key,
    )


def test_same_condition_key_while_active_gives_one_live_event():
    """(a) Emitting the same condition on every eligible tick, under the same
    still-active `condition_key`, appends exactly one event."""
    nc = NotificationCenter()
    key = "route_run:R1:planning_failed"
    results = [_blocked_emit(nc, key) for _ in range(5)]

    assert results[0] is not None
    assert all(r is None for r in results[1:]), "later repeats are dropped"
    assert len(nc.peek_recent(50)) == 1


def test_resolve_condition_then_re_emit_is_a_fresh_event():
    """(b) After the owner resolves the key, the otherwise-identical condition
    emits again — a fresh event with a new notification_id."""
    nc = NotificationCenter()
    key = "route_run:R1:planning_failed"

    first = _blocked_emit(nc, key)
    assert _blocked_emit(nc, key) is None  # suppressed while active

    nc.resolve_condition(key)

    second = _blocked_emit(nc, key)
    assert second is not None
    assert second.notification_id != first.notification_id
    assert len(nc.peek_recent(50)) == 2


def test_latest_error_for_tracks_active_and_clears_on_resolve():
    """(c) `latest_error_for` returns the active-keyed error, and `None` once
    its owner resolves the condition. The historical event is untouched."""
    nc = NotificationCenter()
    key = "route_run:R1:planning_failed"

    _blocked_emit(nc, key)
    found = nc.latest_error_for("route_run", "R1")
    assert found is not None and found.error.origin == "planning_failed"

    nc.resolve_condition(key)
    assert nc.latest_error_for("route_run", "R1") is None
    # Resolution does not delete or rewrite the historical event.
    assert len(nc.peek_recent(50)) == 1
    # Idempotent: resolving an inactive key is a no-op.
    nc.resolve_condition(key)
    assert nc.latest_error_for("route_run", "R1") is None


def test_latch_event_with_error_is_never_suppressed():
    """(d) A latch write/consume passes `latch_event=True`, so even carrying an
    `ErrorRecord` under a condition key it is never dropped — the intent and
    the commit must both show (design_new.md "Error events", Latch events are
    exempt)."""
    from runtime_payloads import ErrorRecord

    nc = NotificationCenter()
    args = dict(
        source="TrainController", operation="set_path_cancel_request",
        kind=Kind.WARNING, message="EB cancel", entities=[train_ref("T1")],
        error=ErrorRecord("operator EB", "eb"), condition_key="train:T1:eb",
        latch_event=True,
    )
    a = nc.emit(**args)
    b = nc.emit(**args)
    assert a is not None and b is not None
    assert len(nc.peek_recent(50)) == 2
    # A latch event never registers an active condition, so it is not a
    # current reason for latest_error_for.
    assert nc.latest_error_for("train", "T1") is None


def test_changed_fingerprint_replaces_the_current_reason():
    """A changed payload under the same key is appended and becomes the new
    current reason (design_new.md "Error events": a meet ceiling moving to a
    different block is a new event)."""
    nc = NotificationCenter()
    key = "route_run:R1:planning_failed"

    _blocked_emit(nc, key, payload={"ceiling": "B2"})
    assert nc.latest_error_for("route_run", "R1").payload == {"ceiling": "B2"}

    moved = _blocked_emit(nc, key, payload={"ceiling": "B5"})
    assert moved is not None
    assert nc.latest_error_for("route_run", "R1").payload == {"ceiling": "B5"}
    assert len(nc.peek_recent(50)) == 2
