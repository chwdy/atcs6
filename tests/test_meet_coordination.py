"""C2 — Contention snapshot + meet coordination logic.

design_new.md "Contention snapshot" + "Meet coordination". These cover the
testable logic units: the highest-priority-waiter snapshot, the
contention-driven release predicate, block-level contested-zone detection, the
furthest-holding-point ceiling, and the lower-priority tie-break. The full
single-track-meet integration runs through the live tick (`build_tick_coordination`).
"""

from meet_coordination import MeetCeiling, MeetCoordinator
from network import RailwayNetwork
from path_dispatcher import WaitingConflict
from route_executor import (
    ContentionEdge,
    ContentionSnapshot,
    RouteExecutor,
)


# --- contested-zone detection (pure) --------------------------------------


def test_contested_zone_opposing_direction():
    a = [("B1", "forward"), ("B2", "forward"), ("B3", "forward")]
    b = [("B5", "reverse"), ("B2", "reverse"), ("B0", "reverse")]
    zone, near = MeetCoordinator._contested_zone(a, b)
    assert zone == {"B2"}
    assert near == 1  # B2 is index 1 on a's route


def test_no_contested_zone_same_direction():
    a = [("B1", "forward"), ("B2", "forward")]
    b = [("B2", "forward"), ("B3", "forward")]  # shared B2, same direction
    zone, near = MeetCoordinator._contested_zone(a, b)
    assert zone == set()
    assert near is None


def test_no_contested_zone_disjoint():
    a = [("B1", "forward"), ("B2", "forward")]
    b = [("B7", "reverse"), ("B8", "reverse")]
    zone, near = MeetCoordinator._contested_zone(a, b)
    assert zone == set()
    assert near is None


# --- holding-point ceiling + priority (stubbed executor) ------------------


class _StubBlock:
    def __init__(self, is_holding_point=False):
        self.is_holding_point = is_holding_point


class _StubNet:
    def __init__(self, blocks):
        self.blocks = blocks
        self.trains = {}


class _StubExec:
    def __init__(self, blocks=None, priorities=None):
        self.network = _StubNet(blocks or {})
        self._priorities = priorities or {}
        self.contention_snapshot = {}

    def _effective_priority_for(self, train_id):
        return self._priorities.get(train_id, 5)


def test_holding_point_ceiling_picks_furthest_before_zone():
    blocks = {
        "B0": _StubBlock(True),
        "B1": _StubBlock(False),
        "B2": _StubBlock(True),
        "B3": _StubBlock(False),
    }
    mc = MeetCoordinator(_StubExec(blocks))
    route = [("B0", "forward"), ("B1", "forward"), ("B2", "forward"), ("B3", "forward")]
    # contested zone near index 3 -> furthest holding point before it is B2
    assert mc._holding_point_ceiling(route, 3) == MeetCeiling("B2", "forward")


def test_holding_point_ceiling_none_when_no_holding_point():
    blocks = {"B0": _StubBlock(False), "B1": _StubBlock(False)}
    mc = MeetCoordinator(_StubExec(blocks))
    route = [("B0", "forward"), ("B1", "forward")]
    assert mc._holding_point_ceiling(route, 2) is None


def test_lower_priority_by_effective_priority():
    mc = MeetCoordinator(_StubExec(priorities={"T_HI": 7, "T_LO": 5}))
    assert mc._lower_priority("T_HI", "T_LO", {"T_HI": 0, "T_LO": 1}) == "T_LO"


def test_lower_priority_tie_breaks_to_later_position():
    mc = MeetCoordinator(_StubExec(priorities={"A": 5, "B": 5}))
    # equal priority -> later in the runnable order is the lower one
    assert mc._lower_priority("A", "B", {"A": 0, "B": 1}) == "B"


# --- contention snapshot + release (real executor, stubbed inputs) --------


def _executor():
    return RouteExecutor(RailwayNetwork())


def test_contention_snapshot_emits_one_edge_per_waiter_holder():
    ex = _executor()
    # T1 and T2 are both blocked on B_X (held by H); T3 is not blocked. The
    # snapshot emits one edge per (waiter, holder) in runnable order. The
    # snapshot carries edges only; priority is read fresh from the registry by
    # wedge/meet detection, not stored on the edge.
    conflicts = {
        "T1": WaitingConflict("B_X", "block", "B_X", ("H",)),
        "T2": WaitingConflict("B_X", "block", "B_X", ("H",)),
        "T3": None,
    }
    ex._waiting_conflict_for = lambda tid: conflicts.get(tid)
    snap = ex._build_contention_snapshot(["T1", "T2", "T3"])
    assert snap.edges == [
        ContentionEdge("B_X", "T1", "H"),
        ContentionEdge("B_X", "T2", "H"),
    ]


def test_contention_snapshot_one_edge_per_holder():
    ex = _executor()
    # A single waiter blocked by two holders (e.g. two lock owners on a switch)
    # yields one edge per holder.
    ex._waiting_conflict_for = lambda tid: (
        WaitingConflict("B_HELD", "switch", "SW1", ("HA", "HB"))
        if tid == "T1"
        else None
    )
    snap = ex._build_contention_snapshot(["T1"])
    assert snap.edges == [
        ContentionEdge("B_HELD", "T1", "HA"),
        ContentionEdge("B_HELD", "T1", "HB"),
    ]



# --- build() attaches the pinned candidate keys to the ceiling ------------


class _FakeCand:
    def __init__(self, ordered_walk):
        self.ordered_walk = ordered_walk


class _FakeStep:
    def __init__(self, resource_type, resource_id, block_direction, entry, exit_):
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.block_id = resource_id if resource_type == "block" else None
        self.block_direction = block_direction
        self.entry_edge = entry
        self.exit_edge = exit_


class _MeetBuildStub:
    """Minimal executor surface for driving `MeetCoordinator.build(...)`."""

    def __init__(self, snapshot, priorities):
        self.network = _StubNet(
            {
                "HP": _StubBlock(True),   # holding point before the zone
                "MID": _StubBlock(False),  # the contested single-track block
                "X": _StubBlock(False),
            }
        )
        self.contention_snapshot = snapshot
        self._priorities = priorities
        self.notification_center = None

    def _effective_priority_for(self, tid):
        return self._priorities.get(tid, 5)


def test_build_pins_held_train_to_its_selected_candidate_keys():
    """A single-track meet: LO (lower priority) is held at HP before the shared
    MID block, and its MeetCeiling carries the ordered resource keys of the very
    candidate the ceiling was computed on — so Plan can pin the acquisition."""
    steps_lo = [
        _FakeStep("block", "HP", "forward", "W", "E"),
        _FakeStep("block", "MID", "forward", "W", "E"),
    ]
    steps_hi = [
        _FakeStep("block", "X", "reverse", "E", "W"),
        _FakeStep("block", "MID", "reverse", "E", "W"),
    ]
    snapshot = ContentionSnapshot(
        edges=[ContentionEdge("MID", "HI", "LO")]
    )
    stub = _MeetBuildStub(snapshot, {"LO": 5, "HI": 8})
    mc = MeetCoordinator(stub)
    raw = {"LO": [_FakeCand(steps_lo)], "HI": [_FakeCand(steps_hi)]}
    mc._raw_candidates = lambda tid, *, max_candidates: raw.get(tid, [])

    assignment = mc.build(["HI", "LO"])

    assert set(assignment) == {"LO"}  # only the held (lower-priority) train
    ceiling = assignment["LO"]
    assert ceiling.block_id == "HP"
    assert ceiling.direction == "forward"
    # The pin is the held train's selected-candidate ordered key list.
    assert ceiling.candidate_resource_keys == (
        ("block", "HP", "W", "E"),
        ("block", "MID", "W", "E"),
    )
