"""Unit tests for block_occupancy.BlockOccupancyDetector.

Covers the debounce state machine (on/off thresholds, flicker tolerance,
multi-block-per-tick diffing) and model-based attribution (reserved_for_train,
span-owner fallback, unknown-occupant attribution, occupied_by for vacates).
These exercise the detector in isolation with scripted current frames — no
simulation loop.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from block_occupancy import BlockOccupancyDetector, OccupancyInput


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
class FakeController:
    physics_sync: FakePhysicsSync = field(default_factory=FakePhysicsSync)


def make_detector(
    block_ids, frames, on=1, off=3, controllers=None, neighbours=None
):
    """A detector over `block_ids`.

    `neighbours` is the undetected-track adjacency the real
    `UnidentifiedTrainTracker.reachable_blocks` walk supplies: block_id -> the
    blocks reachable from it with nothing in between. Attribution needs it to
    decide whether a train could physically be standing on a block it reserved,
    so a test that exercises the reservation match must declare it.
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
    entered_by_train, vacated_by_train, unmatched = det.attribute(entered, set())
    assert entered_by_train == {"T1": {"A"}}
    assert vacated_by_train == {}
    assert unmatched == set()


def test_reserved_block_the_train_cannot_reach_is_not_that_train():
    """The reported bug: a foreign occupant on a reserved block far ahead.

    T1 stands on C and has reserved A and B. Current appears on A with B still
    reading clear, so the only way A could be T1 is if T1 skipped B — track no
    detector confirmed. The reservation does not claim it; it is unmatched, and
    the Step-0 preamble mints an unidentified train for it.
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

    entered_by_train, _, unmatched = det.attribute(entered, set())

    assert unmatched == {"A"}
    assert entered_by_train == {}


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

    entered_by_train, _, unmatched = det.attribute(entered, set())

    assert unmatched == set()
    assert entered_by_train["T1"] == {"A", "B"}


def test_attribute_entered_falls_back_to_span_owner():
    controllers = {
        "T2": FakeController(
            physics_sync=FakePhysicsSync(current_span=FakeSpan(occupied_blocks=["A"]))
        )
    }
    net, det = make_detector(["A"], [{"A": True}], on=1, controllers=controllers)
    # reserved_for_train stays None -> must fall back to the covering train.
    entered, _ = det.step()
    entered_by_train, _, unmatched = det.attribute(entered, set())
    assert entered_by_train == {"T2": {"A"}}
    assert unmatched == set()


def test_attribute_unresolved_entry_is_reported_as_unmatched():
    net, det = make_detector(["A"], [{"A": True}], on=1)
    entered, _ = det.step()
    assert entered == {"A"}
    # No reserving train and no covering span -> the entry belongs to an unidentified train
    # train (design_new.md "Attribution"). `attribute()` is a pure attribution
    # step: it reports the block as unmatched and writes no block state and
    # registers no train. The Step-0 preamble hands it to UnidentifiedTrainTracker.
    entered_by_train, _, unmatched = det.attribute(entered, set())
    assert entered_by_train == {}
    assert unmatched == {"A"}
    assert net.blocks["A"].occupied_by is None
    assert det.confirmed_occupied == {"A"}


def test_attribute_vacated_uses_occupied_by():
    net, det = make_detector(["A"], [{"A": True}, {"A": False}], on=1, off=1)
    det.step()  # confirm
    net.blocks["A"].occupied_by = "T1"
    _, vacated = det.step()  # vacate (off=1)
    assert vacated == {"A"}
    entered_by_train, vacated_by_train, unmatched = det.attribute(set(), vacated)
    assert vacated_by_train == {"T1": {"A"}}
    assert entered_by_train == {}
    assert unmatched == set()


def test_attribute_vacated_already_clear_is_benign():
    net, det = make_detector(["A"], [{"A": True}, {"A": False}], on=1, off=1)
    det.step()
    _, vacated = det.step()
    # occupied_by stays None (already cleared elsewhere) -> nothing attributed.
    _, vacated_by_train, _unmatched = det.attribute(set(), vacated)
    assert vacated_by_train == {}
