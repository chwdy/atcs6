"""
Block occupancy detection: the read side of the hardware boundary for block
occupancy, and the debounce that turns a noisy per-block reading into the set
of blocks that read occupied (design_v2.md "Occupancy detection").

  - OccupancyInput: the per-block read (raw booleans, no train identity).
  - InactiveOccupancyInput: what the runtime holds while no environment is
    active. It reads every block empty and answers for no block.
  - BlockOccupancyDetector: per-block debounce with on/off thresholds. Its
    `confirmed_occupied` set is what "reads occupied" means everywhere else.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set, Tuple, TYPE_CHECKING

from constants import (
    OCCUPANCY_OFF_THRESHOLD,
    OCCUPANCY_ON_THRESHOLD,
)

if TYPE_CHECKING:
    from network import RailwayNetwork


class OccupancyInput(ABC):
    """Read side for hardware sensors that no software output drives
    (design_v2.md "Hardware interface", Input and sensing).

    It reads block current only. A concrete implementation owns the map from a
    logical block_id to its physical detector channel and is the only place that
    knows the wiring; nothing above this interface names a pin or bus.
    """

    @abstractmethod
    def read_block_currents(self) -> Dict[str, bool]:
        """Return the raw per-block current map: block_id -> current detected.

        Raw booleans only, with no train identity — a real detector knows only
        on/off per electrically-isolated block. Putting a train's label on a
        block is a separate step (design_v2.md "Trains").
        """
        raise NotImplementedError

    def detected_block_ids(self) -> Set[str]:
        """The blocks this input can answer for (design_v2.md "Hardware
        interface", Input and sensing).

        A real driver returns the union of its occupancy binding maps. The
        default is empty, which is the honest answer for an input that senses
        nothing physical: a block outside this set never has a physical reading
        behind it.
        """
        return set()


class InactiveOccupancyInput(OccupancyInput):
    """The input bound while no operating environment is active. It reads
    every block empty and answers for no block, so nothing ever reads occupied
    through it; `SimulationController.occupancy_is_real` is False while it is
    bound, and the runtime makes no pass."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._network = network

    def read_block_currents(self) -> Dict[str, bool]:
        return {block_id: False for block_id in self._network.blocks}


@dataclass
class BlockOccupancyDetector:
    """Per-block debounce.

    Each pass, `step()` reads the raw current map, advances per-block on/off
    counters, and returns the whole diff of the confirmed-occupied set
    (newly_entered, newly_vacated). A block is confirmed occupied after
    `on_threshold` consecutive current reads and confirmed empty after
    `off_threshold` no-current reads (design_v2.md "Occupancy detection",
    Debounce with on/off thresholds; A lone current read pauses the clear
    count).
    """

    network: "RailwayNetwork"
    input_interface: OccupancyInput
    on_threshold: int = OCCUPANCY_ON_THRESHOLD
    off_threshold: int = OCCUPANCY_OFF_THRESHOLD
    notify: Optional[Callable[[str, str], None]] = None
    _on_counts: Dict[str, int] = field(default_factory=dict)
    _off_counts: Dict[str, int] = field(default_factory=dict)
    _confirmed: Set[str] = field(default_factory=set)
    # Last raw read + a monotonically increasing read counter, recorded every
    # `step()` for the /api/hardware/occupancy/raw debug endpoint and the
    # detector reading log. Read-back only: no runtime decision reads these.
    _last_raw: Dict[str, bool] = field(default_factory=dict)
    _read_count: int = 0

    def step(self) -> Tuple[Set[str], Set[str]]:
        """Read the raw currents, advance debounce, and return
        (newly_entered, newly_vacated) confirmed-block sets for this tick."""
        raw = self.input_interface.read_block_currents()
        self._last_raw = dict(raw)
        self._read_count += 1
        new_confirmed: Set[str] = set()
        for block_id in self.network.blocks:
            present = bool(raw.get(block_id, False))
            if present:
                self._on_counts[block_id] = self._on_counts.get(block_id, 0) + 1
                # A confirmed block's clear count restarts only on the evidence
                # an entry needs; a lone current read pauses it (design_v2.md
                # "Occupancy detection", A lone current read pauses the clear
                # count).
                if (
                    block_id not in self._confirmed
                    or self._on_counts[block_id] >= self.on_threshold
                ):
                    self._off_counts[block_id] = 0
            else:
                self._off_counts[block_id] = self._off_counts.get(block_id, 0) + 1
                self._on_counts[block_id] = 0

            if block_id in self._confirmed:
                # Stay confirmed until off_threshold consecutive clears.
                if self._off_counts[block_id] < self.off_threshold:
                    new_confirmed.add(block_id)
            elif self._on_counts[block_id] >= self.on_threshold:
                new_confirmed.add(block_id)

        entered = new_confirmed - self._confirmed
        vacated = self._confirmed - new_confirmed
        self._confirmed = new_confirmed
        return entered, vacated

    @property
    def last_raw(self) -> Dict[str, bool]:
        """The map the last `step()` read from `read_block_currents()`, before
        debounce. The detector reading log records it once per pass."""
        return self._last_raw

    def debug_snapshot(self) -> Dict[str, object]:
        """What the last `step()` saw, per block: the raw pre-debounce read, the
        confirmed verdict, and the live debounce counters. `read_count` lets a
        poller detect whether a new read happened between two samples. Purely a
        diagnostic read-back for the raw-occupancy debug endpoint."""
        blocks: Dict[str, Dict[str, object]] = {}
        for block_id in set(self._last_raw) | self._confirmed:
            blocks[block_id] = {
                "raw": bool(self._last_raw.get(block_id, False)),
                "confirmed": block_id in self._confirmed,
                "on_count": self._on_counts.get(block_id, 0),
                "off_count": self._off_counts.get(block_id, 0),
            }
        return {
            "read_count": self._read_count,
            "on_threshold": self.on_threshold,
            "off_threshold": self.off_threshold,
            "blocks": blocks,
        }

    @property
    def confirmed_occupied(self) -> Set[str]:
        """The current confirmed-occupied block set (the occupancy authority)."""
        return set(self._confirmed)

    def detected_block_ids(self) -> Set[str]:
        """The blocks the wired input can answer for, passed straight through.

        `TrainPhysics` reads this to decide which blocks may hold a calculated
        span back: a block with no physical reading behind it is never capped
        against, because nothing would ever release such a cap.
        """
        return self.input_interface.detected_block_ids()
