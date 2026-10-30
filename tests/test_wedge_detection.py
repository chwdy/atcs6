"""Wedge detection — the per-tick deadlock report (design_new.md "Wedge
detection").

A wedge is a deadlock: a set of trains that are each stopped with nowhere to go,
every member waiting for a resource another member holds, so none can move.
`RouteExecutor` builds `wedge_report` between the contention snapshot and the
meet assignment, finds the mutual-blocking cycles in the snapshot's wait-for
edges, and emits one keyed operator event per wedge (resolved when it clears).

These cover the testable units: the cycle finder (pure), the membership
predicate (stopped with nowhere to go), the emit/suppress/resolve lifecycle, and
the tick-order integration through `build_tick_coordination`.
"""

from types import SimpleNamespace

from enums import AtcLimitReasonCode
from network import RailwayNetwork
from notification_center import NotificationCenter, block_ref, train_ref
from path_dispatcher import WaitingConflict
from route_executor import (
    ContentionEdge,
    ContentionSnapshot,
    RouteExecutor,
    Wedge,
    WedgeReport,
)


def _executor():
    return RouteExecutor(RailwayNetwork())


def _edge(waiting_block, waiter, holder, priority=5):
    # `priority` is accepted for call-site compatibility but no longer stored on
    # the edge: the snapshot carries edges only, and wedge/meet detection read
    # priority fresh from the route registry.
    return ContentionEdge(waiting_block, waiter, holder)


# --- cycle finding (pure) --------------------------------------------------


def test_two_train_mutual_block_is_a_wedge():
    # A waits on B (on block bAB); B waits on A (on block bBA). Both are stopped
    # with nowhere to go, so the ring is a wedge naming both trains and both
    # contested blocks.
    edges = [_edge("bAB", "A", "B"), _edge("bBA", "B", "A")]
    wedges = RouteExecutor._find_wedges(edges, {"A", "B"})
    assert wedges == [Wedge(train_ids=("A", "B"), block_ids=("bAB", "bBA"))]


def test_one_train_self_wedge():
    # A train whose own body holds the resource its authority needs next: the
    # wait-for edge points back at itself. One-train wedge (design_new.md).
    wedges = RouteExecutor._find_wedges([_edge("bX", "A", "A")], {"A"})
    assert wedges == [Wedge(train_ids=("A",), block_ids=("bX",))]


def test_simple_queue_is_not_a_wedge():
    # A waits on B, B waits on C, C waits on no one: a queue, not a cycle. No
    # member is mutually blocked, so nothing is reported as a wedge. This is the
    # gate's "non-wedged contention is NOT reported as a wedge" case.
    edges = [_edge("b1", "A", "B"), _edge("b2", "B", "C")]
    assert RouteExecutor._find_wedges(edges, {"A", "B", "C"}) == []


def test_three_train_ring_is_one_wedge():
    edges = [_edge("b1", "A", "B"), _edge("b2", "B", "C"), _edge("b3", "C", "A")]
    assert RouteExecutor._find_wedges(edges, {"A", "B", "C"}) == [
        Wedge(train_ids=("A", "B", "C"), block_ids=("b1", "b2", "b3"))
    ]


def test_rolling_holder_breaks_the_wedge():
    # The same mutual-block edges, but B is still rolling (not a participant),
    # so the resource may clear before A reaches it: no wedge.
    edges = [_edge("bAB", "A", "B"), _edge("bBA", "B", "A")]
    assert RouteExecutor._find_wedges(edges, {"A"}) == []


def test_overlapping_cycles_report_as_one_wedge():
    # Two 2-cycles that share train A (A<->B and A<->C) are one mutual-blocking
    # set, said once, not two overlapping reports (design_new.md "Why this
    # shape").
    edges = [
        _edge("b1", "A", "B"),
        _edge("b2", "B", "A"),
        _edge("b3", "A", "C"),
        _edge("b4", "C", "A"),
    ]
    wedges = RouteExecutor._find_wedges(edges, {"A", "B", "C"})
    assert wedges == [
        Wedge(train_ids=("A", "B", "C"), block_ids=("b1", "b2", "b3", "b4"))
    ]


def test_switch_lock_holders_use_holder_identity():
    # A single waiter blocked by two switch-lock holders yields two edges; when
    # those holders each block the waiter back, the whole set is one wedge.
    # Holder identity works for lock/protection conflicts, not just block
    # occupancy (design_new.md "Wedge detection").
    edges = [
        _edge("bW", "W", "HA"),
        _edge("bW", "W", "HB"),
        _edge("bA", "HA", "W"),
        _edge("bB", "HB", "W"),
    ]
    wedges = RouteExecutor._find_wedges(edges, {"W", "HA", "HB"})
    assert wedges == [
        Wedge(train_ids=("HA", "HB", "W"), block_ids=("bA", "bB", "bW"))
    ]


# --- membership predicate (fake controllers) -------------------------------


def _fake_controller(stopped=True, has_plan=True, reason="reservation_stop", curves=True):
    payload = {
        "lookahead": {
            "speed_curves": ([{"atc_limit_reason_code": reason}] if curves else [])
        }
    }
    return SimpleNamespace(
        path_projection=SimpleNamespace(
            received_path_plan=(object() if has_plan else None)
        ),
        is_stopped=lambda: stopped,
        get_lookahead=lambda: payload,
    )


def _executor_with_controllers(controllers):
    ex = _executor()
    ex.wire_dependencies(controllers=controllers)
    return ex


def test_participant_is_stopped_at_authority_boundary():
    ex = _executor_with_controllers({"T": _fake_controller()})
    assert ex._is_wedge_participant("T") is True


def test_rolling_train_is_not_a_participant():
    ex = _executor_with_controllers({"T": _fake_controller(stopped=False)})
    assert ex._is_wedge_participant("T") is False


def test_train_without_authority_is_not_a_participant():
    ex = _executor_with_controllers({"T": _fake_controller(has_plan=False)})
    assert ex._is_wedge_participant("T") is False


def test_train_with_room_ahead_is_not_a_participant():
    # Dominant restriction is a posted speed limit ahead, not the zero-clearance
    # reservation-stop hold — the train still has clearance to advance.
    ex = _executor_with_controllers({"T": _fake_controller(reason="speed_limit")})
    assert ex._is_wedge_participant("T") is False


def test_missing_controller_is_not_a_participant():
    ex = _executor_with_controllers({})
    assert ex._is_wedge_participant("ghost") is False


# --- emit / suppress / resolve lifecycle (real NotificationCenter) ---------


def _executor_with_center():
    ex = _executor()
    nc = NotificationCenter()
    ex.wire_dependencies(notification_center=nc, controllers={})
    return ex, nc


def _mutual_snapshot():
    return ContentionSnapshot(
        edges=[_edge("bAB", "A", "B"), _edge("bBA", "B", "A")]
    )


def test_wedge_emits_one_keyed_warning_naming_trains_and_blocks():
    ex, nc = _executor_with_center()
    ex._is_wedge_participant = lambda tid: tid in {"A", "B"}
    ex.contention_snapshot = _mutual_snapshot()

    report = ex._build_wedge_report(["A", "B"])

    assert report.wedges == [Wedge(("A", "B"), ("bAB", "bBA"))]
    assert len(nc._archive) == 1
    event = nc._archive[0]
    assert event.operation == "build_wedge_report"
    assert event.kind == "warning"
    assert event.error is not None and event.error.origin == "wedge"
    # Names the trains in the cycle and the blocks the cycle runs through.
    assert event.entities == [
        train_ref("A"),
        train_ref("B"),
        block_ref("bAB"),
        block_ref("bBA"),
    ]
    assert event.payload == {
        "train_ids": ["A", "B"],
        "block_ids": ["bAB", "bBA"],
    }
    # A UI "why is this train stopped?" read finds the live wedge.
    live = nc.latest_error_for("train", "A", origin="wedge")
    assert live is not None and live.notification_id == event.notification_id


def test_persistent_wedge_is_not_re_emitted():
    ex, nc = _executor_with_center()
    ex._is_wedge_participant = lambda tid: tid in {"A", "B"}
    ex.contention_snapshot = _mutual_snapshot()

    ex._build_wedge_report(["A", "B"])
    ex._build_wedge_report(["A", "B"])  # same wedge, same payload

    assert len(nc._archive) == 1  # suppressed as an unchanged repeat


def test_cleared_wedge_is_resolved():
    ex, nc = _executor_with_center()
    ex._is_wedge_participant = lambda tid: tid in {"A", "B"}
    ex.contention_snapshot = _mutual_snapshot()
    ex._build_wedge_report(["A", "B"])

    # Next tick the contention is gone: the wedge clears.
    ex.contention_snapshot = ContentionSnapshot()
    report = ex._build_wedge_report(["A", "B"])

    assert report.wedges == []
    assert ex.wedge_condition_keys == set()
    assert nc.latest_error_for("train", "A", origin="wedge") is None
    assert len(nc._archive) == 1  # the resolve does not append a new event


def test_recurring_wedge_emits_again_after_resolution():
    ex, nc = _executor_with_center()
    ex._is_wedge_participant = lambda tid: tid in {"A", "B"}
    ex.contention_snapshot = _mutual_snapshot()
    ex._build_wedge_report(["A", "B"])  # appears
    ex.contention_snapshot = ContentionSnapshot()
    ex._build_wedge_report(["A", "B"])  # clears + resolves
    ex.contention_snapshot = _mutual_snapshot()
    ex._build_wedge_report(["A", "B"])  # recurs

    assert len(nc._archive) == 2
    assert nc.latest_error_for("train", "A", origin="wedge") is not None


def test_changed_contested_blocks_is_a_new_event():
    # Same trains, different contested blocks -> changed payload -> new event
    # replacing the active one (design_new.md "Emit on change, not per tick").
    ex, nc = _executor_with_center()
    ex._is_wedge_participant = lambda tid: tid in {"A", "B"}
    ex.contention_snapshot = _mutual_snapshot()
    ex._build_wedge_report(["A", "B"])
    ex.contention_snapshot = ContentionSnapshot(
        edges=[_edge("bAB2", "A", "B"), _edge("bBA2", "B", "A")]
    )
    ex._build_wedge_report(["A", "B"])

    assert len(nc._archive) == 2
    live = nc.latest_error_for("train", "A", origin="wedge")
    assert live.payload["block_ids"] == ["bAB2", "bBA2"]


# --- tick-order integration through build_tick_coordination ----------------


def test_build_tick_coordination_reports_a_wedge():
    ex, nc = _executor_with_center()
    ex._is_wedge_participant = lambda tid: tid in {"A", "B"}
    # A and B mutually block on their shared single track; the contention
    # snapshot is built from the injected conflicts, then the wedge report.
    conflicts = {
        "A": WaitingConflict("bAB", "block", "bAB", ("B",)),
        "B": WaitingConflict("bBA", "block", "bBA", ("A",)),
    }
    ex._waiting_conflict_for = lambda tid: conflicts.get(tid)
    ex._effective_priority_for = lambda tid: 5
    # No route runs are registered, so the meet pass finds no candidate routes
    # and writes an empty assignment; only the wedge report is under test here.

    ex.build_tick_coordination(["A", "B"])

    assert ex.wedge_report.wedges == [Wedge(("A", "B"), ("bAB", "bBA"))]
    assert nc.latest_error_for("train", "A", origin="wedge") is not None


def test_build_tick_coordination_queue_reports_no_wedge():
    # A real queue: A waits on B, B waits on C, no cycle. The contention
    # snapshot records the wait-for edges, but the wedge report is empty and no
    # wedge event is emitted.
    ex, nc = _executor_with_center()
    ex._is_wedge_participant = lambda tid: tid in {"A", "B", "C"}
    conflicts = {
        "A": WaitingConflict("b1", "block", "b1", ("B",)),
        "B": WaitingConflict("b2", "block", "b2", ("C",)),
        "C": None,
    }
    ex._waiting_conflict_for = lambda tid: conflicts.get(tid)
    ex._effective_priority_for = lambda tid: 5

    ex.build_tick_coordination(["A", "B", "C"])

    assert ex.contention_snapshot.edges  # the queue IS recorded as contention
    assert ex.wedge_report.wedges == []  # ...but it is not a wedge
    assert nc.latest_error_for("train", "A", origin="wedge") is None
    assert not any(e.error and e.error.origin == "wedge" for e in nc._archive)
