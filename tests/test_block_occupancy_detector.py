"""Unit tests for block_occupancy.BlockOccupancyDetector.

Covers the debounce state machine (on/off thresholds, flicker tolerance,
multi-block-per-tick diffing) and model-based attribution (reserved_for_train,
span-owner fallback, unknown-occupant attribution, occupied_by for vacates).
These exercise the detector in isolation with scripted current frames — no
simulation loop.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from block_occupancy import BlockOccupancyDetector, InputInterface
from constants import UNKNOWN_TRAIN_ID


class ScriptedInput(InputInterface):
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
class FakeNetwork:
    blocks: Dict[str, FakeBlock] = field(default_factory=dict)


@dataclass
class FakeSpan:
    occupied_blocks: List[str] = field(default_factory=list)


@dataclass
class FakePhysicsSync:
    current_span: Optional[FakeSpan] = None


@dataclass
class FakeController:
    physics_sync: FakePhysicsSync = field(default_factory=FakePhysicsSync)


def make_detector(block_ids, frames, on=1, off=3, controllers=None):
    network = FakeNetwork(blocks={bid: FakeBlock() for bid in block_ids})
    detector = BlockOccupancyDetector(
        network=network,
        input_interface=ScriptedInput(frames),
        controllers=controllers or {},
        on_threshold=on,
        off_threshold=off,
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
    net, det = make_detector(["A"], [{"A": True}], on=1)
    net.blocks["A"].reserved_for_train = "T1"
    entered, _ = det.step()
    entered_by_train, vacated_by_train = det.attribute(entered, set())
    assert entered_by_train == {"T1": {"A"}}
    assert vacated_by_train == {}


def test_attribute_entered_falls_back_to_span_owner():
    controllers = {
        "T2": FakeController(
            physics_sync=FakePhysicsSync(current_span=FakeSpan(occupied_blocks=["A"]))
        )
    }
    net, det = make_detector(["A"], [{"A": True}], on=1, controllers=controllers)
    # reserved_for_train stays None -> must fall back to the covering train.
    entered, _ = det.step()
    entered_by_train, _ = det.attribute(entered, set())
    assert entered_by_train == {"T2": {"A"}}


def test_attribute_unresolved_entry_becomes_unknown_occupant():
    net, det = make_detector(["A"], [{"A": True}], on=1)
    entered, _ = det.step()
    assert entered == {"A"}
    # No reserving train and no covering span -> unmodeled (unknown) occupant
    # (design_new.md "Attribution"): committed to the reserved sentinel, not
    # routed to any train, and kept confirmed so it blocks availability there.
    entered_by_train, _ = det.attribute(entered, set())
    assert entered_by_train == {}
    assert net.blocks["A"].occupied_by == UNKNOWN_TRAIN_ID
    assert det.confirmed_occupied == {"A"}


def test_attribute_vacated_uses_occupied_by():
    net, det = make_detector(["A"], [{"A": True}, {"A": False}], on=1, off=1)
    det.step()  # confirm
    net.blocks["A"].occupied_by = "T1"
    _, vacated = det.step()  # vacate (off=1)
    assert vacated == {"A"}
    entered_by_train, vacated_by_train = det.attribute(set(), vacated)
    assert vacated_by_train == {"T1": {"A"}}
    assert entered_by_train == {}


def test_attribute_vacated_already_clear_is_benign():
    net, det = make_detector(["A"], [{"A": True}, {"A": False}], on=1, off=1)
    det.step()
    _, vacated = det.step()
    # occupied_by stays None (already cleared elsewhere) -> nothing attributed.
    _, vacated_by_train = det.attribute(set(), vacated)
    assert vacated_by_train == {}
