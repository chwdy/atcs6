"""C2 — Contention snapshot + meet coordination logic.

design_new.md "Contention snapshot" + "Meet coordination". These cover the
testable logic units: the highest-priority-waiter snapshot, the
contention-driven release predicate, block-level contested-zone detection, the
furthest-holding-point ceiling, and the lower-priority tie-break. The full
single-track-meet integration runs through the live tick (`build_tick_coordination`).
"""

from meet_coordination import MeetCeiling, MeetCoordinator
from network import RailwayNetwork
from route_executor import RouteExecutor


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


def test_contention_snapshot_records_highest_priority_waiter():
    ex = _executor()
    waiting = {"T1": "B_X", "T2": "B_X", "T3": None}
    pris = {"T1": 5, "T2": 8, "T3": 5}
    ex._waiting_for_block_id = lambda tid: waiting.get(tid)
    ex._effective_priority_for = lambda tid: pris.get(tid, 5)
    snap = ex._build_contention_snapshot(["T1", "T2", "T3"])
    assert snap == {"B_X": ("T2", 8)}


def test_contention_release_due_for_higher_priority_waiter():
    ex = _executor()
    ex.contention_snapshot = {"B_HELD": ("T_HI", 8)}
    ex._effective_priority_for = lambda tid: {"T_LO": 5, "T_HI": 8}.get(tid, 5)
    ex._held_future_reservations = lambda tid: {"B_HELD"} if tid == "T_LO" else set()
    assert ex._contention_release_due("T_LO") is True


def test_contention_release_not_due_for_equal_priority():
    ex = _executor()
    ex.contention_snapshot = {"B_HELD": ("T_EQ", 5)}
    ex._effective_priority_for = lambda tid: 5
    ex._held_future_reservations = lambda tid: {"B_HELD"}
    assert ex._contention_release_due("T_LO") is False


def test_contention_release_not_due_when_block_not_held():
    ex = _executor()
    ex.contention_snapshot = {"B_OTHER": ("T_HI", 8)}
    ex._effective_priority_for = lambda tid: {"T_LO": 5, "T_HI": 8}.get(tid, 5)
    ex._held_future_reservations = lambda tid: {"B_HELD"}  # not B_OTHER
    assert ex._contention_release_due("T_LO") is False
