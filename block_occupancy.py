"""
Block occupancy detection (hardware current sensing).

design_new.md "Block occupancy detection": each electrically-isolated
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
entered/vacated observations are committed in Observe through the existing
PathDispatcher.on_controller_progress flow (the controller re-sources its block
events from the detector instead of from the precise tile diff). Switch
occupancy is NOT detected here — switches keep commanded Switch.position
authority and their precise-traversal entry/exit.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

from constants import (
    OCCUPANCY_OFF_THRESHOLD,
    OCCUPANCY_ON_THRESHOLD,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train_controller import TrainController


class AttributionKind(Enum):
    """What a newly OCCUPIED or newly CLEAR block resolved to (design_new.md
    "Block occupancy detection", The attribution result)."""

    HELD = "held"
    ENTRY = "entry"
    OVERRUN_ENTRY = "overrun_entry"
    UNIDENTIFIED = "unidentified"
    CONFLICT = "conflict"
    VACATE = "vacate"
    HELD_CLEAR = "held_clear"
    UNOWNED_CLEAR = "unowned_clear"


# The kinds that add the block to a train's block list. The block-list commit
# and reservation truncation read exactly these.
JOINS_BLOCK_LIST = frozenset(
    {AttributionKind.ENTRY, AttributionKind.OVERRUN_ENTRY, AttributionKind.UNIDENTIFIED}
)


@dataclass(frozen=True)
class AttributionResult:
    """One newly OCCUPIED or newly CLEAR block and what it resolved to
    (design_new.md "Block occupancy detection", The attribution result).

    `train_id` is the matched train for `held`, `entry` and `overrun_entry`, and
    the holder for `vacate` and `held_clear`. For `unidentified` it is the
    unidentified train the block joined or was minted as, and it stays `None`
    until `UnidentifiedTrainTracker` names that train, because creating a train
    is not attribution's job. For `conflict` it is `None`, and the tied trains
    are in `candidates`. For `unowned_clear` it is `None`.
    """

    kind: AttributionKind
    block_id: str
    train_id: Optional[str] = None
    candidates: Tuple[str, ...] = ()


def blocks_joined_by_train(
    results: Iterable[AttributionResult],
) -> Dict[str, Set[str]]:
    """train_id -> the blocks these results add to that train's block list."""
    joined: Dict[str, Set[str]] = {}
    for result in results:
        if result.kind in JOINS_BLOCK_LIST and result.train_id is not None:
            joined.setdefault(result.train_id, set()).add(result.block_id)
    return joined


def blocks_vacated_by_train(
    results: Iterable[AttributionResult],
) -> Dict[str, Set[str]]:
    """train_id -> the blocks these results take out of that train's block
    list (the `vacate` results)."""
    vacated: Dict[str, Set[str]] = {}
    for result in results:
        if result.kind is AttributionKind.VACATE and result.train_id is not None:
            vacated.setdefault(result.train_id, set()).add(result.block_id)
    return vacated


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

    `attribute()` matches each confirmed transition to a train from the live
    authority model, so `read_block_currents` stays a pure per-block read with
    no train identity baked in. It owns the whole match order for an entered
    block and yields one `AttributionResult` per block; a vacated block goes to
    its current holder (`Block.occupied_by`).
    """

    network: "RailwayNetwork"
    input_interface: OccupancyInput
    controllers: Dict[str, "TrainController"] = field(default_factory=dict)
    on_threshold: int = OCCUPANCY_ON_THRESHOLD
    off_threshold: int = OCCUPANCY_OFF_THRESHOLD
    notify: Optional[Callable[[str, str], None]] = None
    # Blocks reachable from a given block through undetected track only, the
    # walk `UnidentifiedTrainTracker.reachable_blocks` owns. `attribute()` and
    # `touching_trains_for_block()` both need it to decide whether a train could
    # physically be standing on a block, so `SimulationController` wires it at
    # construction. Without it no entry is ever credited to a train it is not
    # standing next to, which is the fail-safe reading.
    reachable_blocks: Optional[Callable[[str], Set[str]]] = None
    # Whether a train is directional: it holds a live reservation
    # (`received_path_plan` is set) and is not stopped. A directional train is
    # touched on its head side only; any other train on either side.
    # `SimulationController` wires it. Unwired, every train counts as
    # directional, which is the fail-safe reading.
    directional: Optional[Callable[[str], bool]] = None
    # The block on a train's head side given the blocks it gained on this read
    # (`TrainPhysics.head_side_block`). Adjacency says two things are next to
    # each other and nothing about which way either is going, so a directional
    # train is credited only the block in front of it. `SimulationController`
    # wires it. Unwired, a directional train touches nothing.
    head_side: Optional[Callable[[str, Set[str]], Optional[str]]] = None
    # A ghosted train's retained span blocks, which stand in for its empty block
    # list (design_new.md "Block occupancy detection", A ghosted train's
    # retained span stands in for its block list); empty for any other train.
    # `SimulationController` wires it.
    retained_span: Optional[Callable[[str], Set[str]]] = None
    # A train's block list — the blocks `Block.occupied_by` names it on,
    # ordered rear to front. A newly clear block is vacated only when it is the
    # rearmost block of its holder's list, so the clear results need the order.
    # `SimulationController` wires it to `TrainPhysics.block_list_rear_to_front`.
    # An empty list (unwired, or a train with no span) vacates every clear.
    block_list: Optional[Callable[[str], List[str]]] = None
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
                # an entry needs; a lone current read pauses it (design_new.md
                # "Block occupancy detection", A lone current read pauses the
                # clear count).
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
        self,
        entered: Set[str],
        vacated: Set[str],
        reoffered: Iterable[str] = (),
        held_clears: Iterable[str] = (),
    ) -> List[AttributionResult]:
        """Attribute confirmed transitions to trains.

        Returns one `AttributionResult` per entered, vacated or re-offered
        block, sorted by block id (design_new.md "Block occupancy detection",
        The attribution result); every consumer reads its `kind`.

        An entered block resolves by the first of these that applies
        (design_new.md "Block occupancy detection", Match order for a newly
        OCCUPIED block):

        1. `held` — `Block.occupied_by` already names a train. Nothing changes.
           That is the read on which current returns under a block held through
           a detection fault; every other occupied block is seeded as already
           confirmed, so it never reaches here as an entry.
        2. `entry` — the reserving train (`Block.reserved_for_train`) touches
           the block (`_match_entry`).
        3. The one train that touches the block (`touching_trains_for_block`),
           credited as `entry` or `overrun_entry` (`_credited`).
        4. `conflict` — more than one train touches it. It goes to no train.
        5. `unidentified` — no train touches it. `train_id` stays `None`.

        A block touches a train when it is next to a block in that train's
        block list and, for a directional train, is the block on its head side
        (`_touches`). Nothing here reads a calculated position, except the
        frozen span of a ghosted train (`retained_span`).

        `reoffered` holds blocks that stay confirmed occupied while no train is
        credited with them — a standing `conflict` block (design_new.md "Block
        occupancy detection", An overrun entry or a conflict raises the emergency
        cut-out). Each is matched again on every read exactly as a fresh entry
        is. It is not a fresh entry, so when it ends up `unidentified` it raises
        no unidentified-train warning: the conflict was reported when it first
        arose.

        A vacated block, and each block in `held_clears` (a clear held on the
        last read that the detector still reads clear and the same train still
        holds — design_new.md "Block occupancy detection", A held clear is
        re-offered until it is an end block), resolves against its holder
        (`Block.occupied_by`) by `_clear_results`: `vacate`, `held_clear`, or
        `unowned_clear` when no train holds it.

        Touching is what keeps a reserved-ahead corridor from swallowing a
        foreign occupant. When current appears on a reserved block with a clear
        detected block still between it and the train, claiming it would mean
        the app believes its own train skipped the block in between — track no
        detector ever confirmed. Such current is a different object and belongs
        to an unidentified train (`reachable_blocks` stops at the first block,
        so a block with a clear block between it and every train touches
        nobody). Adjacency alone says nothing about which way a train is going,
        so a directional train touches only the block on its head side: current
        appearing behind a train driving away from it is not that train.

        Matching runs to a fixed point rather than in one pass, because a train
        fast enough to cross several blocks between two reads has them all
        confirmed in the same diff. The second block is adjacent to the first
        rather than to anything the train held when the tick began, so each
        block this batch resolves joins the evidence the next one is tested
        against.

        A confirmed entry that matches no existing train belongs to an
        **unidentified train** (design_new.md "Attribution"), but creating one is not this pure
        attribution step's job: it writes no block state and registers no train.
        The global detector read hands the `unidentified` blocks to
        `UnidentifiedTrainTracker`, which resolves each one to an unidentified
        train `train_id` — new or continuing — and fills it into the result.
        From that point the unidentified train is an ordinary train, so every
        slice commits through its own Observe commit and nothing here needs a
        side channel into `Block.occupied_by`.
        """
        reoffered = set(reoffered)
        results: Dict[str, AttributionResult] = {}
        claimed: Dict[str, Set[str]] = {}
        pending: Set[str] = set()
        for block_id in set(entered) | reoffered:
            holder = getattr(self.network.blocks.get(block_id), "occupied_by", None)
            if holder is not None:
                results[block_id] = AttributionResult(
                    AttributionKind.HELD, block_id, holder
                )
            else:
                pending.add(block_id)

        resolved = True
        while pending and resolved:
            resolved = False
            for block_id in sorted(pending):
                train_id = self._match_entry(block_id, claimed)
                if train_id is None:
                    continue
                results[block_id] = self._credited(
                    block_id, train_id, claimed.get(train_id, set())
                )
                claimed.setdefault(train_id, set()).add(block_id)
                pending.discard(block_id)
                resolved = True

        self._match_touching(pending, claimed, results)

        for block_id in sorted(pending):
            results[block_id] = AttributionResult(AttributionKind.UNIDENTIFIED, block_id)
            if block_id in reoffered:
                continue
            # design_new.md "Attribution": a confirmed entry that matches no
            # existing train is an unidentified train. Unreachable under the
            # simulated detector, which builds current from live spans.
            self._emit(
                "block_attribution_unknown",
                f"confirmed entry on block {block_id} matches no existing "
                f"train; attributing to an unidentified train",
            )

        self._clear_results(set(vacated) | set(held_clears), results)

        return [results[block_id] for block_id in sorted(results)]

    def _clear_results(
        self, clears: Set[str], results: Dict[str, AttributionResult]
    ) -> None:
        """Resolve each newly clear block against its holder (design_new.md
        "Block occupancy detection", A newly CLEAR block).

        - `unowned_clear` — no train holds the block.
        - `vacate` — the block is the rearmost block of its holder's block list.
          Vacates run from the rear while the rearmost block reads clear, so a
          train that cleared several blocks between two reads vacates them all:
          each one taken out of the list leaves the next one rearmost. Every
          clear of an unidentified train is a `vacate`, because it integrates no
          motion to weigh the clear against (An unidentified train's clears
          commit at once). So is every clear of a train whose list has no order
          — no `block_list` wired, or no span to order it by.
        - `held_clear` — any other block the holder holds: a middle block, or
          the front block of a list that holds more than one block.
        """
        by_holder: Dict[str, Set[str]] = {}
        for block_id in clears:
            holder = getattr(self.network.blocks.get(block_id), "occupied_by", None)
            if holder is None:
                results[block_id] = AttributionResult(
                    AttributionKind.UNOWNED_CLEAR, block_id
                )
            else:
                by_holder.setdefault(holder, set()).add(block_id)

        for holder, blocks in sorted(by_holder.items()):
            order: List[str] = []
            if not self._is_unidentified(holder) and self.block_list is not None:
                order = list(self.block_list(holder))
            vacated: Set[str] = set()
            if not order:
                vacated = set(blocks)
            for block_id in order:
                if block_id not in blocks:
                    break
                vacated.add(block_id)
            for block_id in blocks:
                kind = (
                    AttributionKind.VACATE
                    if block_id in vacated
                    else AttributionKind.HELD_CLEAR
                )
                results[block_id] = AttributionResult(kind, block_id, holder)

    def _match_touching(
        self,
        pending: Set[str],
        claimed: Dict[str, Set[str]],
        results: Dict[str, AttributionResult],
    ) -> None:
        """Give each still-pending block to the one ordinary train that touches
        it, and record a `conflict` for every block more than one train touches
        (design_new.md "Block occupancy detection", Attribution). Removes every
        block it resolves from `pending`; a block no train touches stays there
        for the unidentified-train step.

        A locomotive standing across a block boundary draws current on both
        blocks. The far one matches no reservation and no span, so without this
        it would be read as a second object and handed an unidentified train —
        which then de-energizes it, because an unidentified train is never
        energized, stranding the very locomotive the app is driving. Only the
        first block across undetected track counts (`reachable_blocks`), so a
        block with empty blocks between it and every train is still an
        unidentified train.

        Matching runs in rounds, each round testing every pending block against
        the evidence the round started with, so the result does not depend on
        the order the blocks are visited in. Each block a train gains here is
        evidence for the next round, so a train that crossed several blocks
        between two reads is still one train. A conflict block is never
        evidence.
        """
        evidence = {train_id: set(blocks) for train_id, blocks in claimed.items()}
        while pending:
            touching = {
                block_id: self.touching_trains_for_block(block_id, evidence)
                for block_id in sorted(pending)
            }
            resolved = False
            for block_id, train_ids in touching.items():
                if len(train_ids) == 1:
                    train_id = train_ids[0]
                    results[block_id] = self._credited(
                        block_id, train_id, evidence.get(train_id, set())
                    )
                    evidence.setdefault(train_id, set()).add(block_id)
                    pending.discard(block_id)
                    resolved = True
                elif len(train_ids) > 1:
                    results[block_id] = AttributionResult(
                        AttributionKind.CONFLICT, block_id, candidates=tuple(train_ids)
                    )
                    pending.discard(block_id)
            if not resolved:
                break

    def _credited(
        self, block_id: str, train_id: str, gained: Set[str]
    ) -> AttributionResult:
        """The result for a block credited to `train_id` (design_new.md "Block
        occupancy detection", Match order for a newly OCCUPIED block, ranks 2
        and 3). `gained` is what this read has already credited the train.

        - `entry` when the block is reserved for that train (rank 2).
        - `overrun_entry` when the block is in front of a train that holds a
          path — it touches the train on its head side — because the train ran
          past its authority to get there.
        - `entry` otherwise: a train with no path has no authority to run
          past, and a block behind a train is its own body, such as a
          locomotive or lit car bridging the joint behind a stopped train.
        """
        block = self.network.blocks.get(block_id)
        reserved_for = getattr(block, "reserved_for_train", None) if block else None
        if reserved_for == train_id or not self._holds_path(train_id):
            return AttributionResult(AttributionKind.ENTRY, block_id, train_id)
        if (
            self.head_side is not None
            and self.head_side(train_id, set(gained)) != block_id
        ):
            return AttributionResult(AttributionKind.ENTRY, block_id, train_id)
        return AttributionResult(AttributionKind.OVERRUN_ENTRY, block_id, train_id)

    def _holds_path(self, train_id: str) -> bool:
        """Whether `train_id` holds a path (`received_path_plan` is set)."""
        controller = self.controllers.get(train_id)
        projection = getattr(controller, "path_projection", None)
        return getattr(projection, "received_path_plan", None) is not None

    def _match_entry(
        self, block_id: str, claimed: Dict[str, Set[str]]
    ) -> Optional[str]:
        """The reserving train when it touches `block_id`, or None — rank 2 of
        the match order (design_new.md "Block occupancy detection", Match order
        for a newly OCCUPIED block).

        `claimed` is what this batch has attributed so far, so a block resolved
        earlier in the same pass counts as evidence for the next one. A
        reservation is evidence among touching trains only: a train reserves
        the corridor in front of it before it gets there, so a reservation on
        its own says nothing about where the locomotive is.
        """
        block = self.network.blocks.get(block_id)
        reserved_for = getattr(block, "reserved_for_train", None) if block else None
        if reserved_for is None or self.reachable_blocks is None:
            return None
        if self._is_unidentified(reserved_for):
            return None
        neighbours = set(self.reachable_blocks(block_id))
        if self._touches(reserved_for, block_id, neighbours, claimed):
            return reserved_for
        return None

    def touching_trains_for_block(
        self, block_id: str, claimed: Optional[Dict[str, Set[str]]] = None
    ) -> List[str]:
        """Every ordinary train that touches `block_id`, sorted by train id
        (design_new.md "Block occupancy detection", Touching).

        `claimed` is this read's already-attributed entries; each joins its
        train's block list for the next pass, so a train crossing several
        blocks between two reads stays one train. A locomotive standing across
        a block boundary draws current on both blocks, and the far one matches
        no reservation; one touching train is what keeps that second reading
        one train rather than two objects. More than one touching train is a
        `conflict` (rank 5): the block goes to no train.
        """
        if self.reachable_blocks is None:
            return []
        claimed = claimed or {}
        neighbours = set(self.reachable_blocks(block_id))
        candidates = set(self.controllers)
        for block in self.network.blocks.values():
            owner = getattr(block, "occupied_by", None)
            if owner is not None:
                candidates.add(owner)
        return sorted(
            train_id
            for train_id in candidates
            if not self._is_unidentified(train_id)
            and self._touches(train_id, block_id, neighbours, claimed)
        )

    def _touches(
        self,
        train_id: str,
        block_id: str,
        neighbours: Set[str],
        claimed: Dict[str, Set[str]],
    ) -> bool:
        """Whether `block_id` touches `train_id`: it is next to a block in the
        train's block list — reachable through undetected track with no other
        block in between (`neighbours`) — and, for a directional train, it is
        the block on the train's head side.

        The block list is the blocks `Block.occupied_by` names the train on plus
        the blocks `claimed` credits it on this read. For a ghosted train it is
        its retained span, and a block of that span touches it too. An
        unidentified train touches nothing: it is never energized and
        integrates no motion, so it enters nothing under its own power.
        """
        if self._is_unidentified(train_id):
            return False
        gained = set(claimed.get(train_id, set()))
        listed = {
            bid
            for bid, block in self.network.blocks.items()
            if getattr(block, "occupied_by", None) == train_id
        } | gained
        retained = (
            set(self.retained_span(train_id)) if self.retained_span is not None else set()
        )
        if block_id in retained:
            return True
        if not ((listed | retained) & neighbours):
            return False
        directional = (
            self.directional(train_id) if self.directional is not None else True
        )
        if not directional:
            return True
        if self.head_side is None:
            return False
        return self.head_side(train_id, gained) == block_id

    def _is_unidentified(self, train_id: str) -> bool:
        train = self.network.trains.get(train_id)
        return bool(getattr(train, "is_unidentified", False))

    def _emit(self, kind: str, message: str) -> None:
        if self.notify is not None:
            self.notify(kind, message)
