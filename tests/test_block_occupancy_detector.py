"""Unit tests for block_occupancy.BlockOccupancyDetector.

Covers the debounce state machine (on/off thresholds, flicker tolerance,
multi-block-per-tick diffing) and model-based attribution (the
`AttributionResult` each entered block yields — held, reserved_for_train,
span-owner fallback, touching train, conflict, unidentified — and occupied_by
for vacates).
These exercise the detector in isolation with scripted current frames — no
simulation loop.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from block_occupancy import (
    AttributionKind,
    AttributionResult,
    BlockOccupancyDetector,
    OccupancyInput,
    blocks_joined_by_train,
    blocks_vacated_by_train,
)


class ScriptedInput(OccupancyInput):
    """Returns a pre-scripted sequence of raw per-block current maps, one per
    read_block_currents() call; repeats the final frame once exhausted."""

    def __init__(self, frames: List[Dict[str, bool]]):
        self._frames = frames
        self._i = 0

    def read_block_currents(self) -> Dict[str, bool]:
        frame = self._frames[min(self._i, len(self._frames) - 1)]
        self._i += 1
        return dict(frame)


@dataclass
class FakeBlock:
    reserved_for_train: Optional[str] = None
    occupied_by: Optional[str] = None


@dataclass
class FakeTrain:
    is_unidentified: bool = False


@dataclass
class FakeNetwork:
    blocks: Dict[str, FakeBlock] = field(default_factory=dict)
    trains: Dict[str, FakeTrain] = field(default_factory=dict)


@dataclass
class FakeSpan:
    occupied_blocks: List[str] = field(default_factory=list)


@dataclass
class FakePhysicsSync:
    current_span: Optional[FakeSpan] = None


@dataclass
class FakePathProjection:
    received_path_plan: Optional[object] = None


@dataclass
class FakeController:
    physics_sync: FakePhysicsSync = field(default_factory=FakePhysicsSync)
    path_projection: FakePathProjection = field(default_factory=FakePathProjection)


def make_detector(
    block_ids,
    frames,
    on=1,
    off=3,
    controllers=None,
    neighbours=None,
    can_enter=None,
):
    """A detector over `block_ids`.

    `neighbours` is the undetected-track adjacency the real
    `UnidentifiedTrainTracker.reachable_blocks` walk supplies: block_id -> the
    blocks reachable from it with nothing in between. Attribution needs it to
    decide whether a train could physically be standing on a block it reserved,
    so a test that exercises the reservation match must declare it.

    `can_enter` is the plausibility gate the real `TrainPhysics.
    could_be_entering` supplies: whether a train could be the current that just
    appeared on a block, which it answers from the train's direction of travel.
    These tests isolate adjacency and the reservation match, so the default
    allows every candidate; `test_block_occupancy_plausibility.py` covers the
    gate itself against real geometry.
    """
    network = FakeNetwork(blocks={bid: FakeBlock() for bid in block_ids})
    adjacency = {bid: set(vals) for bid, vals in (neighbours or {}).items()}
    detector = BlockOccupancyDetector(
        network=network,
        input_interface=ScriptedInput(frames),
        controllers=controllers or {},
        on_threshold=on,
        off_threshold=off,
        reachable_blocks=lambda block_id: adjacency.get(block_id, set()),
        can_enter=can_enter or (lambda train_id, block_id: True),
    )
    return network, detector


# --------------------------------------------------------------------------
# Debounce: ON threshold
# --------------------------------------------------------------------------

def test_on_threshold_one_confirms_immediately():
    _, det = make_detector(["A"], [{"A": True}], on=1)
    entered, vacated = det.step()
    assert entered == {"A"}
    assert vacated == set()
    assert det.confirmed_occupied == {"A"}


def test_on_threshold_two_needs_two_consecutive_reads():
    _, det = make_detector(["A"], [{"A": True}, {"A": True}], on=2)
    entered, _ = det.step()
    assert entered == set()  # on_count == 1 < 2
    assert det.confirmed_occupied == set()
    entered, _ = det.step()
    assert entered == {"A"}  # on_count == 2 >= 2


# --------------------------------------------------------------------------
# Debounce: OFF threshold and flicker tolerance
# --------------------------------------------------------------------------

def test_off_threshold_rides_through_single_dropout():
    # Confirm A, then a single no-current read (off=1 < 3) then current again:
    # A must stay confirmed the whole time (a flicker, not a real vacate).
    _, det = make_detector(
        ["A"], [{"A": True}, {"A": False}, {"A": True}], on=1, off=3
    )
    assert det.step()[0] == {"A"}
    entered, vacated = det.step()  # dropout
    assert vacated == set()
    assert det.confirmed_occupied == {"A"}
    entered, vacated = det.step()  # current restored
    assert vacated == set()
    assert det.confirmed_occupied == {"A"}


def test_off_threshold_vacates_after_n_consecutive_clears():
    _, det = make_detector(
        ["A"],
        [{"A": True}, {"A": False}, {"A": False}, {"A": False}],
        on=1,
        off=3,
    )
    assert det.step()[0] == {"A"}  # confirmed
    assert det.step()[1] == set()  # off_count 1 < 3
    assert det.step()[1] == set()  # off_count 2 < 3
    entered, vacated = det.step()  # off_count 3 >= 3
    assert vacated == {"A"}
    assert det.confirmed_occupied == set()


# --------------------------------------------------------------------------
# Multi-block per tick (fast train)
# --------------------------------------------------------------------------

def test_multiple_blocks_enter_in_one_tick():
    _, det = make_detector(
        ["A", "B", "C"], [{"A": True, "B": True, "C": True}], on=1
    )
    entered, vacated = det.step()
    assert entered == {"A", "B", "C"}
    assert vacated == set()


def test_multiple_blocks_vacate_in_one_tick():
    # off=1 so a cleared block vacates the same tick — assert the diff reports
    # both cleared blocks in a single step, not one at a time.
    _, det = make_detector(
        ["A", "B", "C"],
        [{"A": True, "B": True, "C": True}, {"A": False, "B": False, "C": True}],
        on=1,
        off=1,
    )
    assert det.step()[0] == {"A", "B", "C"}
    entered, vacated = det.step()
    assert vacated == {"A", "B"}
    assert det.confirmed_occupied == {"C"}


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------

def test_attribute_entered_uses_reserved_for_train():
    """The train reserved A and is standing on B right next to it — this is the
    locomotive arriving on the block it reserved."""
    net, det = make_detector(
        ["A", "B"], [{"A": True, "B": True}], on=1, neighbours={"A": {"B"}}
    )
    net.blocks["A"].reserved_for_train = "T1"
    net.blocks["B"].occupied_by = "T1"
    det.seed_occupied({"B"})  # the block the train is already standing on
    entered, _ = det.step()
    assert entered == {"A"}
    results = det.attribute(entered, set())
    assert results == [AttributionResult(AttributionKind.ENTRY, "A", "T1")]


def test_reserved_block_the_train_cannot_reach_is_not_that_train():
    """The reported bug: a foreign occupant on a reserved block far ahead.

    T1 stands on C and has reserved A and B. Current appears on A with B still
    reading clear, so the only way A could be T1 is if T1 skipped B — track no
    detector confirmed. The reservation does not claim it; it is `unidentified`,
    and Observe mints an unidentified train for it.
    """
    net, det = make_detector(
        ["A", "B", "C"],
        [{"A": True, "B": False, "C": True}],
        on=1,
        neighbours={"A": {"B"}, "B": {"A", "C"}, "C": {"B"}},
    )
    net.blocks["A"].reserved_for_train = "T1"
    net.blocks["B"].reserved_for_train = "T1"
    net.blocks["C"].occupied_by = "T1"
    det.seed_occupied({"C"})
    entered, _ = det.step()
    assert entered == {"A"}

    results = det.attribute(entered, set())

    assert results == [AttributionResult(AttributionKind.UNIDENTIFIED, "A")]


def test_a_train_crossing_several_blocks_in_one_read_stays_one_train():
    """Both blocks confirm in the same diff. B is next to the train's own block,
    and A is next to B — which the batch has just resolved — so the chain holds
    and neither becomes an unidentified train."""
    net, det = make_detector(
        ["A", "B", "C"],
        [{"A": True, "B": True, "C": True}],
        on=1,
        neighbours={"A": {"B"}, "B": {"A", "C"}, "C": {"B"}},
    )
    net.blocks["A"].reserved_for_train = "T1"
    net.blocks["B"].reserved_for_train = "T1"
    net.blocks["C"].occupied_by = "T1"
    det.seed_occupied({"C"})
    entered, _ = det.step()
    assert entered == {"A", "B"}

    results = det.attribute(entered, set())

    assert results == [
        AttributionResult(AttributionKind.ENTRY, "A", "T1"),
        AttributionResult(AttributionKind.ENTRY, "B", "T1"),
    ]


def test_attribute_entered_falls_back_to_span_owner():
    controllers = {
        "T2": FakeController(
            physics_sync=FakePhysicsSync(current_span=FakeSpan(occupied_blocks=["A"]))
        )
    }
    net, det = make_detector(["A"], [{"A": True}], on=1, controllers=controllers)
    # reserved_for_train stays None -> must fall back to the covering train.
    # T2 holds no path, so the block is an `entry`, not an overrun.
    entered, _ = det.step()
    results = det.attribute(entered, set())
    assert results == [AttributionResult(AttributionKind.ENTRY, "A", "T2")]


def test_attribute_unresolved_entry_is_reported_as_unmatched():
    net, det = make_detector(["A"], [{"A": True}], on=1)
    entered, _ = det.step()
    assert entered == {"A"}
    # No reserving train and no covering span -> the entry belongs to an unidentified train
    # train (design_new.md "Attribution"). `attribute()` is a pure attribution
    # step: it reports the block as `unidentified`, names no train, writes no
    # block state and registers no train. Observe hands it to
    # UnidentifiedTrainTracker.
    results = det.attribute(entered, set())
    assert results == [AttributionResult(AttributionKind.UNIDENTIFIED, "A")]
    assert net.blocks["A"].occupied_by is None
    assert det.confirmed_occupied == {"A"}


def test_attribute_vacated_uses_occupied_by():
    net, det = make_detector(["A"], [{"A": True}, {"A": False}], on=1, off=1)
    det.step()  # confirm
    net.blocks["A"].occupied_by = "T1"
    _, vacated = det.step()  # vacate (off=1)
    assert vacated == {"A"}
    results = det.attribute(set(), vacated)
    # A one-block list: its only block is the rearmost, so the clear vacates.
    assert results == [AttributionResult(AttributionKind.VACATE, "A", "T1")]
    assert blocks_vacated_by_train(results) == {"T1": {"A"}}


def test_attribute_vacated_already_clear_is_benign():
    net, det = make_detector(["A"], [{"A": True}, {"A": False}], on=1, off=1)
    det.step()
    _, vacated = det.step()
    # occupied_by stays None (already cleared elsewhere): no train holds the
    # block, so nothing changes and the result says so.
    results = det.attribute(set(), vacated)
    assert results == [AttributionResult(AttributionKind.UNOWNED_CLEAR, "A")]
    assert blocks_vacated_by_train(results) == {}


# --------------------------------------------------------------------------
# The attribution result record (design_new.md "Block occupancy detection",
# The attribution result)
# --------------------------------------------------------------------------

def _train_holding(block_id, net, with_path):
    """A controller for a train standing on `block_id`, with or without a path."""
    net.blocks[block_id].occupied_by = "T1"
    controller = FakeController(
        physics_sync=FakePhysicsSync(current_span=FakeSpan(occupied_blocks=[block_id])),
        path_projection=FakePathProjection(
            received_path_plan=object() if with_path else None
        ),
    )
    return {"T1": controller}


def test_a_block_the_train_already_holds_is_held():
    """Rank 1: current returning under a block already in a train's block list
    resolves to that train and nothing changes."""
    net, det = make_detector(["A"], [{"A": True}], on=1)
    net.blocks["A"].occupied_by = "T1"
    results = det.attribute({"A"}, set())
    assert results == [AttributionResult(AttributionKind.HELD, "A", "T1")]
    assert blocks_joined_by_train(results) == {}


def test_an_unreserved_block_next_to_a_train_with_a_path_is_an_overrun_entry():
    net, det = make_detector(
        ["A", "B"],
        [{"A": True, "B": True}],
        on=1,
        neighbours={"A": {"B"}, "B": {"A"}},
    )
    det.controllers.update(_train_holding("B", net, with_path=True))
    det.seed_occupied({"B"})
    entered, _ = det.step()
    results = det.attribute(entered, set())
    assert results == [AttributionResult(AttributionKind.OVERRUN_ENTRY, "A", "T1")]
    assert blocks_joined_by_train(results) == {"T1": {"A"}}


def test_an_unreserved_block_next_to_a_train_with_no_path_is_an_entry():
    """A train with no path has no authority to run past, so the block it
    touches is an `entry` and raises no cut-out."""
    net, det = make_detector(
        ["A", "B"],
        [{"A": True, "B": True}],
        on=1,
        neighbours={"A": {"B"}, "B": {"A"}},
    )
    det.controllers.update(_train_holding("B", net, with_path=False))
    det.seed_occupied({"B"})
    entered, _ = det.step()
    results = det.attribute(entered, set())
    assert results == [AttributionResult(AttributionKind.ENTRY, "A", "T1")]


def test_a_block_two_trains_touch_is_a_conflict_naming_both():
    net, det = make_detector(
        ["A", "B", "C"],
        [{"A": True, "B": True, "C": True}],
        on=1,
        neighbours={"A": {"B", "C"}, "B": {"A"}, "C": {"A"}},
    )
    net.blocks["B"].occupied_by = "T1"
    net.blocks["C"].occupied_by = "T2"
    det.controllers.update({"T1": FakeController(), "T2": FakeController()})
    det.seed_occupied({"B", "C"})
    entered, _ = det.step()
    results = det.attribute(entered, set())
    assert results == [
        AttributionResult(AttributionKind.CONFLICT, "A", candidates=("T1", "T2"))
    ]
    assert blocks_joined_by_train(results) == {}


def test_the_unidentified_notice_fires_only_for_an_unidentified_block():
    """A block the touching step gives to a train is not reported as going to
    an unidentified train; a block no train touches is."""
    notices = []
    net, det = make_detector(
        ["A", "B", "Z"],
        [{"A": True, "B": True, "Z": True}],
        on=1,
        neighbours={"A": {"B"}, "B": {"A"}},
    )
    det.notify = lambda operation, message: notices.append(message)
    det.controllers.update(_train_holding("B", net, with_path=False))
    det.seed_occupied({"B"})
    entered, _ = det.step()
    results = det.attribute(entered, set())
    assert [(r.kind, r.block_id) for r in results] == [
        (AttributionKind.ENTRY, "A"),
        (AttributionKind.UNIDENTIFIED, "Z"),
    ]
    assert len(notices) == 1
    assert "block Z" in notices[0]

