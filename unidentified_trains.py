"""Ghost trains — the runtime's model of current it detects on track that no
train the app controls is standing on.

design_new.md "Power", Ghost trains: a locomotive placed on the rails by hand, a
car left behind, a train from before the app started. The app cannot drive it —
driving needs a cab, a speed profile and a route, and a ghost was never set up
with any of them — so all the runtime does is keep track of it, which is what
keeps every other train clear of it.

A ghost is an ordinary `Train` with `is_ghost = True` and its own
`TrainController`, so its block entries and vacates flow through the same
Step-0 -> Phase-2 callback -> Phase-3 coordinator/manager chain every other train
uses. Nothing here writes `Block.occupied_by`; the two exceptions are the
identity reconciliation below (a ghost-to-ghost handoff after a merge or split,
which is not a detector commit) and none other.

`GhostTrainTracker` owns:

- **Creation and identity** — a fresh `train_id` per connected group of
  ghost-held blocks, absorbed forward as the group grows.
- **The gap** — switch tiles carry no detection, so a ghost crossing one holds no
  blocks for a moment; it is held with an empty span for `GHOST_GAP_HOLD_S` and
  keeps its id if it reappears through undetected track.
- **Span** — exactly the blocks it holds, claiming their full extent.
- **Retirement** — a ghost still holding nothing when its hold window ends.
- **Adoption** — forgetting a ghost the operator has set up as a real train
  (`SimulationController.adopt_ghost_train(...)` owns that conversion).

A ghost is not driveable *as a ghost*, but it is not a dead end either: the
operator can tell the app what it is looking at, and from then on it is an
ordinary train. That is adoption, and it is the only way `is_ghost` ever goes
back to `False`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from constants import GHOST_GAP_HOLD_S
from network import _OPPOSITE_EDGE
from train import Train
from train_span import SpanEndLocator, TrainSpan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from block import Block
    from network import RailwayNetwork
    from simulation import SimulationController


_EDGES: Tuple[str, ...] = ("N", "E", "S", "W")


class GhostTrainTracker:
    """The ghost-train side of the Step-0 detector preamble."""

    def __init__(
        self,
        network: "RailwayNetwork",
        simulation_controller: "SimulationController",
    ) -> None:
        self._network = network
        self._sim = simulation_controller

        # Monotonic id source. A retired ghost's `train_id` is never reused
        # (design_new.md "Creation and identity"), so this only ever counts up.
        self._next_seq = 1

        # ghost_id -> when the runtime created it. Used to pick which id survives
        # when two ghost groups grow together (the older one keeps its id).
        self._created_at: Dict[str, float] = {}
        # (ghost_id, block_id) -> when this ghost first held that block. Used to
        # pick which part of a split keeps the older id (the part holding the
        # block the ghost held longest).
        self._block_held_since: Dict[Tuple[str, str], float] = {}
        # ghost_id -> when its span went empty, for ghosts currently in the gap
        # hold. Absent means the ghost is holding blocks.
        self._gap_hold_since: Dict[str, float] = {}
        # ghost_id -> the blocks it held at the moment its span went empty, so a
        # reappearance through undetected track can be matched back to it.
        self._blocks_before_gap: Dict[str, Set[str]] = {}
        # ghost_id -> the blocks it held at the last reconcile that found it
        # holding anything, which is where `_blocks_before_gap` comes from.
        self._last_held_blocks: Dict[str, Set[str]] = {}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def is_ghost(self, train_id: Optional[str]) -> bool:
        """True when `train_id` names a ghost train currently in the network."""
        if train_id is None:
            return False
        train = self._network.trains.get(train_id)
        return bool(train is not None and getattr(train, "is_ghost", False))

    @property
    def ghost_ids(self) -> List[str]:
        return sorted(
            train_id
            for train_id, train in self._network.trains.items()
            if getattr(train, "is_ghost", False)
        )

    # ------------------------------------------------------------------
    # Step 0: attribution of confirmed entries that match no existing train
    # ------------------------------------------------------------------

    def attribute_unmatched_entries(
        self, block_ids: Set[str], now: float
    ) -> Dict[str, Set[str]]:
        """Attribute confirmed entries that match no existing train to ghosts.

        Returns `ghost_id -> entered block ids`, ready for the caller to merge
        into the ordinary attribution map: from here on the ghost is an ordinary
        train to every other rule.

        Each block goes, in order, to:

        1. the ghost already holding a block reachable from it through
           undetected track, including one this same read just handed a
           neighbouring block — the group grew forward;
        2. the gap-held ghost whose last held blocks are reachable from it the
           same way — the same ghost continuing its run across a switch tile
           (most recently held wins when more than one could account for it);
        3. a newly created ghost.
        """
        out: Dict[str, Set[str]] = {}
        assigned_here: Dict[str, str] = {}
        for block_id in sorted(block_ids):
            ghost_id = self._existing_ghost_for_entry(block_id, assigned_here)
            if ghost_id is None:
                ghost_id = self._create_ghost(now).train_id
            self._gap_hold_since.pop(ghost_id, None)
            self._blocks_before_gap.pop(ghost_id, None)
            self._block_held_since.setdefault((ghost_id, block_id), now)
            assigned_here[block_id] = ghost_id
            out.setdefault(ghost_id, set()).add(block_id)
        return out

    def _existing_ghost_for_entry(
        self, block_id: str, assigned_here: Dict[str, str]
    ) -> Optional[str]:
        neighbours = self.reachable_blocks(block_id)

        for neighbour_id in sorted(neighbours):
            # A neighbour this same read already handed to a ghost counts: two
            # blocks confirmed on one tick are one ghost, not two.
            if neighbour_id in assigned_here:
                return assigned_here[neighbour_id]
            block = self._network.blocks.get(neighbour_id)
            owner = getattr(block, "occupied_by", None) if block else None
            if self.is_ghost(owner):
                return owner

        candidates = [
            (held_at, ghost_id)
            for ghost_id, held_at in self._gap_hold_since.items()
            if neighbours & set(self._blocks_before_gap.get(ghost_id, ()))
        ]
        if candidates:
            # "If more than one held ghost could account for the entry, the one
            # whose hold began most recently takes it."
            return max(candidates)[1]
        return None

    def note_vacate_directions(
        self, ghost_id: str, entered: Set[str], vacated: Set[str]
    ) -> None:
        """Record the caution direction for each block this ghost is vacating.

        A ghost has no calculated tail exit; its direction is simply the block it
        moved into (design_new.md "Direction"). When it vacates a block while
        still holding a neighbour, that neighbour's edge is the vacated block's
        `exit_edge`, so the block it left behind takes normal CAUTION. The edge
        goes into `physics_sync.pending_block_exit_edges`, the one carrier for a
        per-block tail-vacate direction, and the controller's Phase-2 callback
        consumes it on this same tick — for a ghost it never buffers across ticks.
        """
        if not vacated:
            return
        controller = self._sim.controllers.get(ghost_id)
        if controller is None:
            return
        still_held = (self._held_blocks(ghost_id) | set(entered)) - set(vacated)
        if not still_held:
            # Last block clearing: `direction` stays None, the block goes
            # straight to FREE, and the ghost enters the gap hold.
            return
        for block_id in sorted(vacated):
            for edge in _EDGES:
                next_block = self._next_block(block_id, edge)
                if next_block is not None and next_block in still_held:
                    controller.physics_sync.pending_block_exit_edges[block_id] = edge
                    break

    def write_detected_span(
        self, ghost_id: str, entered: Set[str], vacated: Set[str]
    ) -> bool:
        """Set a ghost's span from this tick's detector slice, during Step 0.

        Called by `TrainPhysics.reconcile_to_detected_occupancy(...)`, which stays
        the Step-0 span writer for every train (design_new.md ":2629"): detection
        is a ghost's only span source, and it arrives through that ordinary call.
        The blocks are the ones it holds now plus this tick's confirmed entries
        minus its confirmed vacates — the commits themselves land in Phase 3.

        Returns True when the span changed.
        """
        held = (self._held_blocks(ghost_id) | set(entered)) - set(vacated)
        return self._write_span(ghost_id, held)

    # ------------------------------------------------------------------
    # End of tick: identity reconciliation, span refresh, hold, retirement
    # ------------------------------------------------------------------

    def reconcile(self, now: float) -> None:
        """Bring ghost identity, spans, and holds back in line with the blocks
        the detector-confirmed commits actually left in place this tick.

        Runs after every train's Phase 3, so `Block.occupied_by` is committed.
        Identity follows connectivity: one ghost per connected group of
        ghost-held blocks. A group holding two ids (two ghosts grew together)
        keeps the older id; a ghost whose blocks split into two groups keeps its
        id on the part holding the block it has held longest, and the other part
        becomes a new ghost.
        """
        groups = self._connected_ghost_groups()

        claimed: Set[str] = set()
        absorbed: Set[str] = set()
        for group in groups:
            owners = {
                self._network.blocks[b].occupied_by
                for b in group
                if b in self._network.blocks
            }
            owners = {o for o in owners if o is not None}
            survivor = self._survivor_for_group(group, owners, claimed, now)
            claimed.add(survivor)
            absorbed |= owners - {survivor}
            for block_id in sorted(group):
                self._hand_block_to(block_id, survivor, now)

        # An absorbed ghost did not roll into an undetected gap — another ghost
        # took over every block it held — so it is retired at once rather than
        # waiting out a hold window it is not in.
        for ghost_id in sorted(absorbed):
            if self.is_ghost(ghost_id) and not self._held_blocks(ghost_id):
                self._retire(ghost_id)

        for ghost_id in self.ghost_ids:
            held = self._held_blocks(ghost_id)
            if held:
                self._gap_hold_since.pop(ghost_id, None)
                self._blocks_before_gap.pop(ghost_id, None)
                self._last_held_blocks[ghost_id] = set(held)
                for key in [
                    k
                    for k in self._block_held_since
                    if k[0] == ghost_id and k[1] not in held
                ]:
                    self._block_held_since.pop(key, None)
                self._write_span(ghost_id, held)
                continue

            # Holding nothing: the gap hold (design_new.md "The gap").
            self._write_span(ghost_id, set())
            started = self._gap_hold_since.get(ghost_id)
            if started is None:
                self._gap_hold_since[ghost_id] = now
                self._blocks_before_gap[ghost_id] = set(
                    self._last_held_blocks.get(ghost_id, set())
                )
                continue
            if now - started >= GHOST_GAP_HOLD_S:
                self._retire(ghost_id)

    def _survivor_for_group(
        self,
        group: Set[str],
        owners: Set[str],
        claimed: Set[str],
        now: float,
    ) -> str:
        """The ghost id this connected group keeps."""
        available = sorted(
            (self._created_at.get(o, now), o)
            for o in owners
            if self.is_ghost(o) and o not in claimed
        )
        if not available:
            # Every id in this group already survived another group: this group
            # is the far side of a split, so it becomes a new ghost.
            return self._create_ghost(now).train_id
        if len(available) == 1:
            return available[0][1]

        # Two ghost groups grew together, or a ghost split and this side holds
        # the block it has held longest. Both rules pick the same winner: the
        # oldest id that holds the longest-held block in this group.
        longest_held = min(
            (
                (self._block_held_since.get((owner, block_id), now), owner)
                for block_id in group
                for owner in owners
                if self._network.blocks.get(block_id) is not None
                and self._network.blocks[block_id].occupied_by == owner
            ),
            default=None,
        )
        if longest_held is not None and longest_held[1] in {o for _, o in available}:
            return longest_held[1]
        return available[0][1]

    def _hand_block_to(self, block_id: str, ghost_id: str, now: float) -> None:
        """Move one block's ghost ownership to `ghost_id`.

        Only ever runs for a ghost-to-ghost handoff after a merge or a split, and
        goes through the canonical `BlockManager` writers so the block never sits
        owned by two trains and the voltage drain still sees the release.
        """
        block = self._network.blocks.get(block_id)
        if block is None or block.occupied_by == ghost_id:
            return
        previous = block.occupied_by
        manager = self._sim.block_manager
        if manager is None or not self.is_ghost(previous):
            return
        manager.on_block_tail_vacated(block_id, previous, None)
        manager.occupy_blocks([block_id], ghost_id)
        self._block_held_since.pop((previous, block_id), None)
        self._block_held_since.setdefault((ghost_id, block_id), now)

    # ------------------------------------------------------------------
    # Creation / retirement
    # ------------------------------------------------------------------

    def _fresh_id(self) -> str:
        while True:
            train_id = f"GHOST_{self._next_seq}"
            self._next_seq += 1
            if train_id not in self._network.trains:
                return train_id

    def _create_ghost(self, now: float) -> Train:
        """Build the ghost `Train`, its controller, and its physics instance.

        Blank train configuration with the default `SpeedProfile`, which a ghost
        never uses because it is never powered (design_new.md "SpeedProfile").
        """
        train_id = self._fresh_id()
        train = Train(train_id=train_id, is_ghost=True)

        profiles = getattr(self._network, "speed_profiles", None)
        if profiles is not None:
            from speed_profile import make_default_speed_profile

            profile = make_default_speed_profile(train_id, train.max_speed)
            profiles.register(profile)
            train.speed_profile_id = profile.profile_id

        self._network.add_train(train)
        self._sim.get_or_create_controller(train_id)
        self._sim.get_or_create_physics(train_id)
        self._created_at[train_id] = now

        self._emit(
            "ghost_train_detected",
            f"current detected on track no controllable train covers; "
            f"tracking it as ghost train {train_id}",
            train_id,
        )
        return train

    def forget(self, ghost_id: str) -> None:
        """Drop every scrap of tracker bookkeeping for `ghost_id`.

        Used when a ghost stops being a ghost — the operator adopted it as a
        real train (design_new.md "Ghost trains", Adoption). The `Train`, its
        controller, and the blocks it holds all survive; only the tracker's own
        identity/hold state goes, so the adopted train is never absorbed into a
        neighbouring ghost group, never gap-held, and never retired. Its
        `train_id` still counts as used, because `_fresh_id` skips ids the
        network already holds.
        """
        self._gap_hold_since.pop(ghost_id, None)
        self._blocks_before_gap.pop(ghost_id, None)
        self._last_held_blocks.pop(ghost_id, None)
        self._created_at.pop(ghost_id, None)
        for key in [k for k in self._block_held_since if k[0] == ghost_id]:
            self._block_held_since.pop(key, None)

    def _retire(self, ghost_id: str) -> None:
        """Retire a ghost that held no blocks for the whole hold window."""
        self.forget(ghost_id)

        train = self._network.trains.get(ghost_id)
        profile_id = getattr(train, "speed_profile_id", None) if train else None
        self._network.remove_train(ghost_id)
        self._sim.controllers.pop(ghost_id, None)
        self._sim.physics_instances.pop(ghost_id, None)
        profiles = getattr(self._network, "speed_profiles", None)
        if profiles is not None and profile_id:
            profiles.remove(profile_id)

        self._emit(
            "ghost_train_retired",
            f"ghost train {ghost_id} drew no current for "
            f"{GHOST_GAP_HOLD_S}s and is retired",
            ghost_id,
        )

    # ------------------------------------------------------------------
    # Span
    # ------------------------------------------------------------------

    def _write_span(self, ghost_id: str, held: Set[str]) -> bool:
        """Set the ghost's span to exactly the blocks it holds.

        Its length, and where inside those blocks it actually sits, are unknown,
        so the end locators sit at the outer boundaries of its end blocks and
        `occupied_tiles` lists every tile of every block it holds — the most
        track the detector allows, which is the safe reading for a train nobody
        is driving (design_new.md "Span"). Returns True when the span changed.
        """
        controller = self._sim.controllers.get(ghost_id)
        if controller is None:
            return False
        previous = controller.physics_sync.current_span
        previous_blocks = list(getattr(previous, "occupied_blocks", []) or [])
        if not held:
            controller.physics_sync.current_span = None
            return previous is not None

        ordered_blocks = self._chain_order(held)
        entries: List[Tuple[int, int, Optional[str]]] = []
        for block_id in ordered_blocks:
            entries.extend(self._block_tile_entries(block_id))
        if not entries:
            controller.physics_sync.current_span = None
            return previous is not None

        first, last = entries[0], entries[-1]
        last_length = self._network._get_tile_length_cm(int(last[0]), int(last[1]))
        controller.physics_sync.current_span = TrainSpan(
            # end_a sits at the entry edge of the first tile and end_b at the far
            # end of the last one: the outer boundaries of the end blocks. Each
            # block contributes its own canonical tile order, since a ghost's
            # real orientation is unknown.
            end_a=SpanEndLocator(
                tile=(int(first[0]), int(first[1])),
                distance_cm=0.0,
                entry_edge=self._entry_edge_for(first),
            ),
            end_b=SpanEndLocator(
                tile=(int(last[0]), int(last[1])),
                distance_cm=float(last_length),
                entry_edge=self._entry_edge_for(last),
            ),
            occupied_tiles=[(int(e[0]), int(e[1])) for e in entries],
            occupied_blocks=list(ordered_blocks),
        )
        return list(ordered_blocks) != previous_blocks

    def _block_tile_entries(
        self, block_id: str
    ) -> List[Tuple[int, int, Optional[str]]]:
        """One block's tiles as `(x, y, entry_edge)`, in its canonical order.

        A block's own `ordered_tiles` carries the per-tile forward-entry edge;
        when a block was built without that geometry, the network's tile-ownership
        index still knows which tiles it owns, and the edge falls back to one the
        tile actually has (see `_entry_edge_for`).
        """
        block = self._network.blocks.get(block_id)
        entries = list(block.ordered_tiles) if block is not None else []
        if entries:
            return [(int(e[0]), int(e[1]), e[2]) for e in entries]
        return [
            (int(x), int(y), None)
            for (x, y) in self._network.get_block_positions(block_id)
        ]

    def _entry_edge_for(self, entry: Tuple[int, int, Optional[str]]) -> str:
        """The entry edge to stamp on a ghost span end.

        A ghost's real orientation is unknown, so when the tile carries no
        recorded forward-entry edge, any edge the tile actually has is an honest
        answer; picking the first in compass order keeps it deterministic.
        """
        x, y, edge = entry
        if edge:
            return str(edge)
        track = self._network.get_track(int(x), int(y))
        edges = sorted(getattr(track, "edges", ()) or ())
        return edges[0] if edges else "N"

    def _chain_order(self, held: Set[str]) -> List[str]:
        """`held` ordered end to end along connectivity when it forms a chain."""
        adjacency = {
            block_id: sorted(self.reachable_blocks(block_id) & held)
            for block_id in sorted(held)
        }
        ends = [b for b in sorted(held) if len(adjacency[b]) <= 1]
        start = ends[0] if ends else sorted(held)[0]

        ordered: List[str] = []
        seen: Set[str] = set()
        current: Optional[str] = start
        while current is not None:
            ordered.append(current)
            seen.add(current)
            nxt = None
            for candidate in adjacency[current]:
                if candidate not in seen:
                    nxt = candidate
                    break
            current = nxt
        # Anything the walk could not reach (a branched or looped group) still
        # belongs to the span; append it in a deterministic order.
        ordered.extend(b for b in sorted(held) if b not in seen)
        return ordered

    # ------------------------------------------------------------------
    # Topology helpers
    # ------------------------------------------------------------------

    def _next_block(self, block_id: str, edge: str) -> Optional[str]:
        """The first block reached leaving `block_id` through `edge`, if any.

        Used for the vacate direction: the edge whose walk lands on a block the
        ghost still holds is the vacated block's `exit_edge`.
        """
        for tile in self._network.get_block_tracks(block_id):
            if edge not in (tile.edges or ()):
                continue
            found = self._walk_out(tile, edge, block_id)
            if found:
                return sorted(found)[0]
        return None

    def reachable_blocks(self, block_id: str) -> Set[str]:
        """Every block reachable from `block_id` through undetected track.

        Public because attribution needs the same neighbour test before it
        ever reaches a ghost: a confirmed block next to one an ordinary train
        already holds belongs to that train (see `BlockOccupancyDetector`,
        and design_new.md "Power", Attribution).

        Undetected track is everything between two blocks: bare switch tiles
        (crossed on their live `Switch.position`) and any track tile no block
        owns. The walk starts from the network's tile-ownership index rather than
        `Block.ordered_tiles`, because a block's tile membership can live only in
        that index (an editor-built block serializes no ordered tiles of its own).
        """
        found: Set[str] = set()
        for tile in self._network.get_block_tracks(block_id):
            for edge in sorted(tile.edges or ()):
                found |= self._walk_out(tile, edge, block_id)
        return found - {block_id}

    def _walk_out(self, tile, edge: str, origin_block_id: str) -> Set[str]:
        """Blocks reached by stepping out of `tile` through `edge`.

        Follows live connectivity across undetected track; a switch tile is
        crossed only through the exits its current position actually offers, so
        a ghost is never credited with reaching a block the points do not lead to.
        """
        reached: Set[str] = set()
        visited: Set[Tuple[int, int, str]] = set()
        frontier = [(tile, edge)]
        while frontier:
            current, exit_edge = frontier.pop()
            key = (current.x, current.y, exit_edge)
            if key in visited:
                continue
            visited.add(key)

            neighbour = self._network.get_neighbor(current.x, current.y, exit_edge)
            if neighbour is None:
                continue
            entry_edge = _OPPOSITE_EDGE.get(exit_edge)
            if entry_edge is None or entry_edge not in (neighbour.edges or ()):
                continue  # the two tiles do not actually share a rail

            block_id = getattr(neighbour, "block_id", None)
            if block_id and block_id != origin_block_id:
                reached.add(block_id)
                continue
            if block_id == origin_block_id:
                continue  # still inside the block we started from
            for onward in neighbour.get_exits(entry_edge, use_switch_state=True) or ():
                frontier.append((neighbour, onward))
        return reached

    def _held_blocks(self, ghost_id: str) -> Set[str]:
        return {
            block_id
            for block_id, block in self._network.blocks.items()
            if getattr(block, "occupied_by", None) == ghost_id
        }

    def _connected_ghost_groups(self) -> List[Set[str]]:
        """Connected groups of blocks currently held by ghosts."""
        ghost_held = {
            block_id
            for block_id, block in self._network.blocks.items()
            if self.is_ghost(getattr(block, "occupied_by", None))
        }
        groups: List[Set[str]] = []
        unvisited = set(ghost_held)
        while unvisited:
            seed = sorted(unvisited)[0]
            group = {seed}
            frontier = [seed]
            unvisited.discard(seed)
            while frontier:
                current = frontier.pop()
                for neighbour in sorted(self.reachable_blocks(current) & ghost_held):
                    if neighbour in unvisited:
                        unvisited.discard(neighbour)
                        group.add(neighbour)
                        frontier.append(neighbour)
            groups.append(group)
        return groups

    # ------------------------------------------------------------------

    def _emit(self, kind: str, message: str, train_id: str) -> None:
        center = getattr(self._sim, "notification_center", None)
        if center is None:
            return
        from notification_center import Severity

        try:
            center.emit(
                source="GhostTrainTracker",
                kind=kind,
                severity=Severity.WARNING,
                event_time=time.time(),
                train_id=train_id,
                payload={"message": message},
            )
        except Exception:
            pass
