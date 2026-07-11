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
    n = nc.emit(source="X", kind="diagnostic", severity=Severity.INFO)
    after = time.time()
    assert before <= n.event_time <= after


def test_emit_still_accepts_one_local_sample_shared_with_an_entity_write():
    # The one case design_new.md allows: a single writer samples once locally
    # and uses that value for both its notification and its entity write.
    nc = NotificationCenter()
    n = nc.emit(source="X", kind="diagnostic", severity=Severity.INFO, event_time=1234.5)
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


# --- NotificationCenter is the only runtime sink -----------------------------
# design_new.md "Out of Scope": "Logging beyond NotificationCenter | None |
# NotificationCenter is the only runtime sink | Component-local logging is
# forbidden by design." Routine traces must not exist in domain code. The
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
    first.emit(source="A", kind="diagnostic", severity=Severity.INFO)
    first.emit(source="A", kind="diagnostic", severity=Severity.INFO)

    # A restart builds a brand-new NotificationCenter against the same file.
    second = NotificationCenter()
    second.enable_event_log(path)
    second.emit(source="B", kind="diagnostic", severity=Severity.INFO)

    ids = [e.notification_id for e in NotificationCenter.read_event_log(path)]
    assert ids == [1, 2, 3], ids
    assert len(set(ids)) == len(ids), "duplicate notification_id on disk"


def test_enable_event_log_on_a_fresh_path_numbers_from_one(tmp_path):
    nc = NotificationCenter()
    nc.enable_event_log(str(tmp_path / "fresh.jsonl"))
    n = nc.emit(source="A", kind="diagnostic", severity=Severity.INFO)
    assert n.notification_id == 1


def test_corrupt_event_log_line_does_not_block_boot(tmp_path):
    path = tmp_path / "corrupt.jsonl"
    path.write_text('{"notification_id": 5}\nnot json at all\n{"no_id": true}\n')
    nc = NotificationCenter()
    nc.enable_event_log(str(path))
    assert nc.emit(source="A", kind="diagnostic", severity=Severity.INFO).notification_id == 6
