"""
Block occupancy detection (hardware current sensing).

design_new.md "Power -> Block occupancy detection": each electrically-isolated
block has a current detector. Current present means a train sits on the block;
an open circuit means it does not. This module is the read side of the hardware
boundary for block occupancy, plus the debounce + attribution layer that turns a
noisy per-block current stream into confirmed, train-attributed entered/vacated
block observations.

Layers (mirrors the TrackOutput pattern in block_power.py):
  - OccupancyInput: abstract per-block current read (raw booleans, no train id).
  - SimulatedOccupancyDriver: in-memory default; derives current from the model
    (a block reads current iff some train's live span covers it).
  - BlockOccupancyDetector: per-block debounce (on/off thresholds) producing the
    confirmed-occupied set each tick, plus model-based attribution of each
    confirmed transition to a train.

Authority: the confirmed set is what Block.occupied_by follows. The confirmed
entered/vacated observations are committed in Phase 3 through the existing
PathDispatcher.on_controller_progress flow (the controller re-sources its block
events from the detector instead of from the precise tile diff). Switch
occupancy is NOT detected here — switches keep commanded Switch.position
authority and their precise-traversal entry/exit.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Optional, Set, Tuple, TYPE_CHECKING

from constants import (
    OCCUPANCY_OFF_THRESHOLD,
    OCCUPANCY_ON_THRESHOLD,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train_controller import TrainController


class OccupancyInput(ABC):
    """Read side for hardware sensors that no software output drives
    (design_new.md Power, Hardware interface boundary).

    v1 reads block current only. A concrete implementation owns the map from a
    logical block_id to its physical detector channel and is the only place that
    knows the wiring; nothing above this interface names a pin or bus.
    """

    @abstractmethod
    def read_block_currents(self) -> Dict[str, bool]:
        """Return the raw per-block current map: block_id -> current detected.

        Raw booleans only, with no train identity — a real detector knows only
        on/off per electrically-isolated block. Attribution to a train is a
        separate model step (BlockOccupancyDetector.attribute).
        """
        raise NotImplementedError

    def detected_block_ids(self) -> Set[str]:
        """The blocks this input can answer for (design_new.md "Power",
        Hardware interface boundary).

        A real driver returns the union of its occupancy binding maps. The
        default is empty, which is the honest answer for an input that senses
        nothing physical: a block outside this set never has a physical reading
        behind it, so nothing may be held waiting on one (see
        `TrainPhysics.cap_distance_to_detected_occupancy`).
        """
        return set()


class SimulatedOccupancyDriver(OccupancyInput):
    """In-memory default used when no real hardware is connected.

    Derives the raw current map from the model: a block reads current iff some
    train's live span (`current_span.occupied_blocks`) currently covers it. This
    is "simulate the train entering a block by calculation". A fault-injecting
    subclass may override `read_block_currents` to assert or withhold current
    early/late to exercise the snap-to-seam and debounce paths in tests.
    """

    def __init__(
        self,
        network: "RailwayNetwork",
        controllers: Dict[str, "TrainController"],
    ) -> None:
        self._network = network
        self._controllers = controllers

    def detected_block_ids(self) -> Set[str]:
        """Empty, and it must stay empty.

        This driver builds its current map *from* the live spans, so a train the
        span cap held out of a block would leave that block reading clear
        forever and its own cap would never release. Reporting no detected
        blocks is what makes the cap inert here (design_new.md "Power", The
        calculated span never runs ahead of the detector).
        """
        return set()

    def read_block_currents(self) -> Dict[str, bool]:
        currents: Dict[str, bool] = {bid: False for bid in self._network.blocks}
        for controller in self._controllers.values():
            span = getattr(
                getattr(controller, "physics_sync", None), "current_span", None
            )
            if span is None:
                continue
            for block_id in span.occupied_blocks or []:
                if block_id in currents:
                    currents[block_id] = True
        return currents


@dataclass
class BlockOccupancyDetector:
    """Per-block debounce plus model-based attribution.

    Each tick, `step()` reads the raw current map, advances per-block on/off
    counters, and returns the FULL diff of the confirmed-occupied set
    (newly_entered, newly_vacated). Diffing the whole set means a fast train that
    crosses or clears several blocks in one tick is reported in a single pass. A
    block is confirmed occupied after `on_threshold` consecutive current reads
    and confirmed clear after `off_threshold` consecutive no-current reads. The
    bias is fail-safe: quick to occupy, slow to clear, so a momentary open
    circuit inside the train's span does not vacate the block.

    `attribute()` maps each confirmed transition to a train from the live
    authority model — an entered block to its reserving train
    (`Block.reserved_for_train`, the reserved-ahead corridor), a vacated block to
    its current holder (`Block.occupied_by`) — so `read_block_currents` stays a
    pure per-block read with no train identity baked in.
    """

    network: "RailwayNetwork"
    input_interface: OccupancyInput
    controllers: Dict[str, "TrainController"] = field(default_factory=dict)
    on_threshold: int = OCCUPANCY_ON_THRESHOLD
    off_threshold: int = OCCUPANCY_OFF_THRESHOLD
    notify: Optional[Callable[[str, str], None]] = None
    # Blocks reachable from a given block through undetected track only, the
    # walk `UnidentifiedTrainTracker.reachable_blocks` owns. `attribute()` and
    # `adjacent_train_for_block()` both need it to decide whether a train could
    # physically be standing on a block, so `SimulationController` wires it at
    # construction. Without it no entry is ever credited to a train it is not
    # standing next to, which is the fail-safe reading.
    reachable_blocks: Optional[Callable[[str], Set[str]]] = None
    # Whether a given train could be the current that just appeared on a given
    # block — it is moving, and the block is in front of it. Adjacency says two
    # things are next to each other and nothing about which way either is going,
    # so on its own it reads a hand-placed locomotive beside a stopped train as
    # that train. `SimulationController` wires this to
    # `TrainPhysics.could_be_entering`. Without it no entry is credited to an
    # adjacent train at all, which is the fail-safe reading.
    can_enter: Optional[Callable[[str, str], bool]] = None
    _on_counts: Dict[str, int] = field(default_factory=dict)
    _off_counts: Dict[str, int] = field(default_factory=dict)
    _confirmed: Set[str] = field(default_factory=set)
    # Last raw read + a monotonically increasing read counter, recorded every
    # `step()` for the /api/hardware/occupancy/raw debug endpoint. Diagnostic
    # read-back only: nothing in the runtime consumes these.
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

    def seed_occupied(self, block_ids: Iterable[str]) -> None:
        """Adopt externally-committed occupancy as already confirmed.

        `occupy_block` is also the writer for placement and restore
        (`place_train` occupies a train's starting blocks directly). Those
        blocks are occupied before the detector ever observed current on them,
        so without seeding the first read would emit a spurious entered event
        for a block the train was already standing on. Seeding makes the
        detector track only future changes; the block still vacates normally
        once its current drops for off_threshold ticks.
        """
        for block_id in block_ids:
            if block_id not in self._confirmed:
                self._confirmed.add(block_id)
                self._on_counts[block_id] = max(
                    self._on_counts.get(block_id, 0), self.on_threshold
                )
                self._off_counts[block_id] = 0

    def attribute(
        self, entered: Set[str], vacated: Set[str]
    ) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]], Set[str]]:
        """Attribute confirmed transitions to trains.

        Returns (entered_by_train, vacated_by_train, unmatched_entries): the two
        maps are train_id -> set of block_ids, and `unmatched_entries` holds the
        entered blocks that match no existing train.

        Entered blocks attribute to `Block.reserved_for_train` (the train that
        reserved the block it is entering) **only when that train is standing
        next to the block and could be entering it**, falling back to the train
        whose live span covers the block. Vacated blocks attribute to
        `Block.occupied_by`; a vacate whose block is already clear is a benign
        double-clear and is skipped.

        The adjacency condition on the reservation match is what keeps a
        reserved-ahead corridor from swallowing a foreign occupant. A train
        reserves the corridor in front of it before it gets there, so a
        reservation on its own says nothing about where the locomotive is. When
        current appears on a reserved block with a clear detected block still
        between it and the train, the reservation is the one thing that would
        claim it, and claiming it means the app believes its own train skipped
        the block in between — track no detector ever confirmed. Such current is
        a different object and belongs to an unidentified train, exactly as the
        span and reachability matches already read it (`reachable_blocks`
        stops at the first block, so a block with a clear block between it and
        every train matches nobody).

        Adjacency alone is still not enough, because it says two things are next
        to each other and nothing about which way either is going. A train
        standing at a signal and a locomotive somebody sets down in front of it
        are adjacent; so are a train driving east and current appearing on the
        block behind it. Neither is that train arriving. Every adjacency match
        therefore also asks `can_enter` whether the train is moving and driving
        toward the block (`_could_enter`).

        Matching runs to a fixed point rather than in one pass, because a train
        fast enough to cross several blocks between two reads has them all
        confirmed in the same diff. The second block is adjacent to the first
        rather than to anything the train held when the tick began, so each
        block this batch resolves joins the evidence the next one is tested
        against.

        A confirmed entry that matches no existing train belongs to an
        **unidentified train** (design_new.md "Attribution"), but creating one is not this pure
        attribution step's job: it writes no block state and registers no train.
        The Step-0 preamble hands `unmatched_entries` to `UnidentifiedTrainTracker`,
        which resolves each block to an unidentified train `train_id` — new or continuing — and
        the result merges into `entered_by_train`. From that point the unidentified train is an
        ordinary train, so every slice commits through its own Phase-3
        transaction and nothing here needs a side channel into
        `Block.occupied_by`.
        """
        entered_by_train: Dict[str, Set[str]] = {}
        pending: Set[str] = set(entered)
        resolved = True
        while pending and resolved:
            resolved = False
            for block_id in sorted(pending):
                train_id = self._match_entry(block_id, entered_by_train)
                if train_id is None:
                    continue
                entered_by_train.setdefault(train_id, set()).add(block_id)
                pending.discard(block_id)
                resolved = True

        unmatched_entries: Set[str] = pending
        for block_id in sorted(unmatched_entries):
            # design_new.md "Attribution": a confirmed entry that matches no
            # existing train is an unidentified train. Unreachable under the
            # simulated detector, which builds current from live spans.
            self._emit(
                "block_attribution_unknown",
                f"confirmed entry on block {block_id} matches no existing "
                f"train; attributing to an unidentified train",
            )

        vacated_by_train: Dict[str, Set[str]] = {}
        for block_id in vacated:
            block = self.network.blocks.get(block_id)
            train_id = getattr(block, "occupied_by", None) if block else None
            if train_id is None:
                continue
            vacated_by_train.setdefault(train_id, set()).add(block_id)

        return entered_by_train, vacated_by_train, unmatched_entries

    def _match_entry(
        self, block_id: str, claimed: Dict[str, Set[str]]
    ) -> Optional[str]:
        """The train a confirmed entry on `block_id` belongs to, or None.

        `claimed` is what this batch has attributed so far, so a block resolved
        earlier in the same pass counts as evidence for the next one.
        """
        block = self.network.blocks.get(block_id)
        reserved_for = getattr(block, "reserved_for_train", None) if block else None
        if reserved_for is not None and self._train_is_adjacent(
            reserved_for, block_id, claimed
        ):
            return reserved_for
        return self._span_owner(block_id)

    def adjacent_train_for_block(
        self, block_id: str, claimed: Optional[Dict[str, Set[str]]] = None
    ) -> Optional[str]:
        """The ordinary train standing next to `block_id`, or None.

        Next to means reachable through undetected track — bare switch tiles and
        any tile no block owns — with no other block in between, which is what
        `reachable_blocks` walks. A locomotive standing across a block boundary
        draws current on both blocks, and the far one matches no reservation and
        no span; this is what keeps that second reading one train rather than
        two objects.

        The calculated span is tried before `Block.occupied_by` because the span
        is where the app is driving the train and it leads confirmed occupancy on
        entry — the block a train has just been given may not have confirmed yet.
        `claimed` adds this tick's already-attributed entries to the same
        evidence, so a train crossing several blocks between two reads stays one
        train.
        """
        if self.reachable_blocks is None:
            return None
        for neighbour_id in sorted(self.reachable_blocks(block_id)):
            for train_id in sorted(self.controllers):
                if self._is_unidentified(train_id):
                    continue
                span = getattr(
                    getattr(self.controllers[train_id], "physics_sync", None),
                    "current_span",
                    None,
                )
                covered = set(span.occupied_blocks or []) if span is not None else set()
                if claimed:
                    covered |= claimed.get(train_id, set())
                if neighbour_id in covered and self._could_enter(train_id, block_id):
                    return train_id
            block = self.network.blocks.get(neighbour_id)
            owner = getattr(block, "occupied_by", None) if block else None
            if (
                owner is not None
                and not self._is_unidentified(owner)
                and self._could_enter(owner, block_id)
            ):
                return owner
        return None

    def _could_enter(self, train_id: str, block_id: str) -> bool:
        """Whether `train_id` could be the current that just appeared on
        `block_id` — moving, and driving toward it rather than away.

        Adjacency alone answers a different question: it says the two are next
        to each other, which is true of a locomotive somebody set down beside a
        train standing at a signal. This is the gate that keeps such current
        from being read as the train (design_new.md "Power", Attribution is
        gated on plausibility). With no gate wired nothing is plausible, so no
        entry is credited to an adjacent train.
        """
        if self.can_enter is None:
            return False
        return bool(self.can_enter(train_id, block_id))

    def _train_is_adjacent(
        self, train_id: str, block_id: str, claimed: Dict[str, Set[str]]
    ) -> bool:
        """Whether `train_id` in particular is standing next to `block_id` and
        could be entering it.

        `adjacent_train_for_block` answers with one train, picked by its own
        preference order; the reservation gate needs the answer for the
        reserving train specifically, because a block may sit next to a
        different train than the one that reserved it.

        A reservation is the weakest evidence of the three, so it carries the
        plausibility gate too: a train stopped at the end of its authority still
        holds the reservation on the block in front of it, and current appearing
        there while it stands still is not that train.
        """
        if self.reachable_blocks is None:
            return False
        controller = self.controllers.get(train_id)
        span = getattr(getattr(controller, "physics_sync", None), "current_span", None)
        standing_on = set(span.occupied_blocks or []) if span is not None else set()
        standing_on |= claimed.get(train_id, set())
        standing_on |= {
            bid
            for bid, block in self.network.blocks.items()
            if getattr(block, "occupied_by", None) == train_id
        }
        if not (standing_on & self.reachable_blocks(block_id)):
            return False
        return self._could_enter(train_id, block_id)

    def _is_unidentified(self, train_id: str) -> bool:
        train = self.network.trains.get(train_id)
        return bool(getattr(train, "is_unidentified", False))

    def _span_owner(self, block_id: str) -> Optional[str]:
        """The train whose live span currently covers `block_id`, if any."""
        for train_id, controller in self.controllers.items():
            span = getattr(
                getattr(controller, "physics_sync", None), "current_span", None
            )
            if span is None:
                continue
            if block_id in (span.occupied_blocks or []):
                return train_id
        return None

    def _emit(self, kind: str, message: str) -> None:
        if self.notify is not None:
            self.notify(kind, message)
