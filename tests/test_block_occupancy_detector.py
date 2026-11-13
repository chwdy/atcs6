"""Unit tests for block_occupancy.BlockOccupancyDetector: the debounce
(design_v2.md "Occupancy detection", Debounce with on/off thresholds; A lone
current read pauses the clear count; Several changes in one step).

These exercise the detector in isolation with scripted current frames.
"""

from dataclasses import dataclass, field
from typing import Dict, List

from block_occupancy import BlockOccupancyDetector, OccupancyInput


@dataclass
class FakeNetwork:
    blocks: Dict[str, object] = field(default_factory=dict)


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


def make_detector(block_ids, frames, on=1, off=3):
    """A detector over `block_ids` reading `frames` in turn."""
    network = FakeNetwork(blocks={bid: object() for bid in block_ids})
    detector = BlockOccupancyDetector(
        network=network,
        input_interface=ScriptedInput(frames),
        on_threshold=on,
        off_threshold=off,
    )
    return network, detector


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


def test_a_lone_current_read_pauses_the_clear_count():
    # design_new.md "Block occupancy detection", A lone current read pauses the
    # clear count: one stray sample on an empty powered block neither restarts
    # the count nor adds to it.
    _, det = make_detector(
        ["A"],
        [{"A": True}, {"A": True}, {"A": False}, {"A": True}, {"A": False}, {"A": False}],
        on=2,
        off=3,
    )
    det.step()
    assert det.step()[0] == {"A"}  # confirmed on the second read
    assert det.step()[1] == set()  # clear count 1
    assert det.step()[1] == set()  # lone current read: count stays 1
    assert det.step()[1] == set()  # clear count 2
    entered, vacated = det.step()  # clear count 3
    assert vacated == {"A"}


def test_current_reads_in_a_row_restart_the_clear_count():
    # OCCUPANCY_ON_THRESHOLD consecutive current reads — the evidence that
    # confirms an entry — restart the count.
    frames = [{"A": True}, {"A": True}, {"A": False}, {"A": False},
              {"A": True}, {"A": True}, {"A": False}, {"A": False}, {"A": False}]
    _, det = make_detector(["A"], frames, on=2, off=3)
    vacated_at = None
    for read in range(1, len(frames) + 1):
        if det.step()[1] == {"A"}:
            vacated_at = read
            break
    assert vacated_at == 9  # three clears after the restart at read 6


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

