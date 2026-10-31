"""
Path Dispatcher Module (v1)

The PathDispatcher is the dispatcher-owned authority registry. It owns:
- create_path / extend_path / cancel_path lifecycle for v1 Path entities
- on_controller_progress consumption of TrainController's Observe progress reports
- try_complete_active_path for the canonical completion handshake
- pathfinding helpers (find_paths, get_reachable_endpoints) used by routes/UI

In v1 the canonical Path schema lives in path.py and uses:
  requested_destination_block_id, requested_destination_direction,
  reservation_end_block_id, resource_sequence (List[ResourceSequenceEntry]),
  current_resource_index, last_status_updated_time, status.

There is no v0 start_block_id / end_block_id / block_sequence /
current_block_index / priority / dispatcher_id / switch_positions /
flank_protection_switches / overlap_blocks / approach_locked_blocks /
blocks_cleared. All call sites here use the v1 fields.

PathSegment (geometry-only candidate from BFS) and the ValidationResult /
ValidationError schemas are imported from runtime_payloads.py.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import logging
import time

logger = logging.getLogger(__name__)
from typing import Dict, List, Optional, Set, Tuple, Any, TYPE_CHECKING

from path import (
    Path,
    PathStatus,
    PathTileEntry,
    ResourceSequenceEntry,
)
from enums import BlockState, direction_from_delta, OPPOSITE_DIRECTIONS
from track import OPPOSITE_EDGE
from runtime_payloads import CancellationResult, ControllerProgressReport, ErrorRecord, PathAcquisitionFailureKind, PathAcquisitionResult, PathCompletionResult, PathSegment, ResolvedRouteTarget, ResourceEvent, RouteExecutorForPathDispatcher, ValidationError, ValidationErrorType, ValidationResult
from block_manager import ReservedBlockTraversal
from switch_manager import SwitchClaimKey, SwitchLockRequest
from railway_pathfinder import RailwayPathfinder
from path_utils import GraphWalkCandidate, GraphWalkStep

if TYPE_CHECKING:
    from network import RailwayNetwork
    from block import Block


# The exact ordered candidate-frame key the tick-start meet calculation pins a
# train to: one `(resource_type, resource_id, entry_edge, exit_edge)` tuple per
# pathfinder step, in traversal order. `required_candidate_resource_keys` on
# `create_path(...)` / `extend_path(...)` is a list of these.
CandidateResourceKey = Tuple[str, str, str, str]


@dataclass(frozen=True)
class WaitingConflict:
    """Why a blocked waiter cannot grow its authority next, and who is in the way.

    Produced by `PathDispatcher.get_waiting_conflict(...)`. `waiting_block_id` is
    the block the waiter holds at (the last block its authority reaches before
    the obstruction). `failed_resource_type` / `failed_resource_id` name the
    first forward resource its next acquisition step could not take — a block it
    would enter, or a switch it would cross. `holding_train_ids` are the trains
    live state says are in the way of that resource: the physical occupants if
    any, otherwise the reservation / lock / protection owners. Duplicates are
    removed; a same-train holder (the waiter blocking itself) is kept.
    """

    waiting_block_id: str
    failed_resource_type: str  # "block" | "switch"
    failed_resource_id: str
    holding_train_ids: Tuple[str, ...] = ()


class PathDispatcher:
    """
    Manages path creation, validation, lifecycle, and progress consumption.

    Public v1 API:
        create_path(train_id, requested_destination_block_id,
                    requested_destination_direction, reservation_cap_block_id)
        extend_path(path_id, requested_destination_block_id,
                    requested_destination_direction, reservation_cap_block_id)
        cancel_path(path_id, initiator, reason)
        on_controller_progress(report)
        try_complete_active_path(train_id)
        get_active_path(train_id)
        active_path_id_by_train(train_id)
        get_active_paths()
        get_path(path_id)
        find_paths(start_block_id, end_block_id, train_id)
        get_reachable_endpoints(start_block_id, include_stations)
        get_reachable_endpoints_for_extension(path_id, include_stations)
        validate_path(path_segment, path_id, train_id)
    """

    def __init__(self, network: "RailwayNetwork"):
        self.network = network
        self.paths: Dict[str, Path] = {}
        self.path_sequence_counter: int = 0
        # Injected named dependencies (design_new.md, "Preemption" and
        # "Dispatcher/route-executor inputs"): PathDispatcher
        # holds no general SimulationController reference. SimulationController
        # wires these once via wire_dependencies(...); they stay None for isolated
        # dispatcher unit tests, where every use below is None-guarded and no-ops.
        # `_route_executor` provides the cancellation/completion callbacks and the
        # preemption / reversal-trap resolvers; `_controllers` is the occupant-
        # controller lookup registry; `_notification_center` is the event-log sink.
        self._route_executor: Optional["RouteExecutorForPathDispatcher"] = None
        self._notification_center: Optional[Any] = None
        self._controllers: Optional[Dict[str, Any]] = None
        # Canonical pathfinding home shared with RouteExecutor (the shared
        # pathfinding home in design_new.md "Path And PathDispatcher"). The
        # same instance services dispatcher
        # candidate enumeration and any RouteExecutor platform-end
        # selection that needs the shared comparison rules.
        self._pathfinder = RailwayPathfinder(network)

    @property
    def active_paths(self) -> Dict[str, Path]:
        """Legacy alias for the underlying path registry.

        Per design_new.md "Retained path history", completed/cancelled paths are retained
        as historical records in the same dict (skipped by the canonical
        `active_path_id_by_train(...)` projection because their status is
        no longer ACTIVE). Callers that want only ACTIVE-status rows
        should use `get_active_paths()` instead of this raw view.
        """
        return self.paths

    @property
    def paths_by_id(self) -> Dict[str, Path]:
        """Canonical alias from design_new.md "Retained path history". Same dict as
        `self.paths`; includes retained terminal rows."""
        return self.paths

    # ========================================================================
    # Helpers
    # ========================================================================

    def _now(self) -> float:
        return time.time()

    def _generate_path_id(
        self, train_id: str, dest_block_id: str
    ) -> str:
        """Generate a human-readable path ID."""
        self.path_sequence_counter += 1
        train_part = (train_id or "ANON").upper().replace(" ", "_")[:16]
        dest_part = self._get_block_display_name(dest_block_id)
        return f"PATH_{train_part}_TO_{dest_part}_{self.path_sequence_counter:03d}"

    def _get_block_display_name(self, block_id: str) -> str:
        block = self.network.blocks.get(block_id)
        if block and block.name:
            name = block.name.upper().replace(" ", "_")[:20]
            return name
        return (block_id or "?").upper().replace(" ", "_")[:20]

    def _get_train(self, train_id: str):
        if train_id is None:
            return None
        return self.network.trains.get(train_id)

    def wire_dependencies(
        self,
        *,
        route_executor: Optional[RouteExecutorForPathDispatcher] = None,
        notification_center: Optional[Any] = None,
        controllers: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Inject the named dependencies PathDispatcher needs (design_new.md,
        "Preemption" and "Dispatcher/route-executor inputs"): the route executor
        (the cancellation/completion callbacks and the
        preemption / reversal-trap resolvers), the occupant-controller lookup
        registry, and the event-log NotificationCenter. Called once by
        SimulationController at construction; PathDispatcher holds no general
        SimulationController reference of its own."""
        self._route_executor = route_executor
        self._notification_center = notification_center
        self._controllers = controllers

    def _get_route_executor(self) -> Optional[RouteExecutorForPathDispatcher]:
        return self._route_executor

    def _meet_way_holder(self, resource_type: str, resource_id: str) -> Optional[str]:
        """The train the current meet assignment keeps this resource for, or
        None (design_new.md "Resource acquisition", A meet keeps a waiting
        train's way). Read through the injected route resolver, the same one
        that answers priority."""
        rx = self._get_route_executor()
        getter = getattr(rx, "meet_way_holder", None) if rx is not None else None
        if not callable(getter):
            return None
        return getter(resource_type, resource_id)

    def _get_controller(self, train_id: str):
        # Look up (never create) an occupant/covered controller from the injected
        # registry; controller existence is a RouteExecutor precondition.
        controllers = self._controllers
        if isinstance(controllers, dict):
            return controllers.get(train_id)
        return None

    def _train_covered_blocks(self, train_id: str) -> Set[str]:
        """The set of blocks the train currently occupies — its covered span.

        Enforces the covered-span invariant (design_new.md "`create_path()`
        flow", item 9): a stored authority must reach at least one
        block outside this set. Returns an empty set when the span is
        unavailable, which disables the exclusion rather than blocking
        acquisition.
        """
        ctrl = self._get_controller(train_id)
        if ctrl is None:
            return set()
        span = ctrl.get_current_span()
        if span is None:
            return set()
        return set(getattr(span, "occupied_blocks", None) or [])

    def _block_available_for_train(self, block_id: str, train_id: str) -> bool:
        """True when `block_id` could be acquired by `train_id` right now — FREE,
        or already RESERVED / OCCUPIED by this same train (re-entry).

        Read-only mirror of `BlockManager.is_block_available` over
        `self.network.blocks` (validate_path reads block state the same way). Used
        to tell a transient braking cap that will grow past a block from a cap the
        train will park at because the block beyond is blocked.
        """
        block = self.network.blocks.get(block_id)
        if block is None or getattr(block, "disabled", False):
            return False
        if block.state == BlockState.FREE:
            return True
        return bool(
            train_id
            and (
                getattr(block, "occupied_by", None) == train_id
                or getattr(block, "reserved_for_train", None) == train_id
            )
        )

    def _head_tile_for_train(self, train):
        """Train head tile via its controller's span, or None
        (design_new.md: head/tail live on the controller, not on Train)."""
        if train is None:
            return None
        ctrl = self._get_controller(getattr(train, "train_id", None))
        return ctrl._get_head_tile() if ctrl is not None else None

    def _get_current_block_id_for_train(self, train) -> Optional[str]:
        """Resolve the train's current block (head tile).

        A head standing on a switch tile belongs to no block, so the tile alone
        cannot answer. That is an ordinary place for a train to be stopped — a
        switch carries no detection and a consist can straddle one — and the
        train is still plainly on the block its body occupies behind that switch,
        which is the block planning must start from. Falling back to the
        head-side span block is the same "inward occupied block" rule
        `_resolve_create_start_block` already applies one step later.

        Refusing here instead would reject the train before any of the planning
        that knows how to handle a head on a switch has run, and report it as a
        train whose position cannot be resolved rather than as what it is. When
        authority genuinely cannot be built from that position, the best-effort
        path still says so, through `BLOCKED_BEFORE_TARGET` and a `reason`
        naming the head-on-switch case.
        """
        head_tile = self._head_tile_for_train(train)
        if not head_tile:
            return None
        track = self.network.get_track(*head_tile)
        if track is None:
            return None
        return track.block_id or self._head_side_span_block(train)

    def _head_side_span_block(self, train) -> Optional[str]:
        """The block at the head end of the train's span, or None.

        `occupied_blocks` runs `end_a` to `end_b`, so the head-side entry is the
        first or last depending on which end is the head (design_new.md
        "Reservation-side head derivation").
        """
        ctrl = self._get_controller(getattr(train, "train_id", None))
        if ctrl is None:
            return None
        span = ctrl.get_current_span()
        occupied = list(getattr(span, "occupied_blocks", None) or []) if span else []
        if not occupied:
            return None
        head_is_a = True
        label = getattr(ctrl, "_head_end_label", None)
        if callable(label):
            head_is_a = label() == "end_a"
        return occupied[0] if head_is_a else occupied[-1]

    def _resolve_create_start_block(
        self,
        train,
        train_id: str,
        target_block_id: str,
        head_block: Optional[str],
        manual_override: bool,
    ) -> Optional[str]:
        """Resolve the start-side block for `create_path` (design_new.md
        create_path flow items 4 and 13).

        The train compares both physical span ends against the resolved target
        and starts from the end whose best successful graph walk wins the
        cross-query comparison (``GraphWalkCandidate.cross_query_key()``:
        estimated travel time first when the train's max speed is set, then
        ``tile_count``, then ``walk_length_cm``; ``end_a`` wins exact ties). Each
        end uses the inward occupied block, which already skips a switch the
        physical end may sit on. Falls back to ``head_block`` whenever a second
        end cannot improve on it (no controller, single-block span, target
        inside the span, or neither end yields a walk), so the covered and
        turnaround paths are left unchanged.
        """
        ctrl = self._get_controller(train_id)
        if ctrl is None:
            return head_block

        span = ctrl.get_current_span()
        occupied = list(getattr(span, "occupied_blocks", None) or []) if span else []
        if len(occupied) < 2:
            return head_block

        # End_a-side and end_b-side blocks, skipping any end already at the
        # target (a covered/turnaround case handled outside this helper).
        seen: Set[str] = set()
        end_blocks: List[str] = []
        for b in (occupied[0], occupied[-1]):
            if b and b != target_block_id and b not in seen:
                seen.add(b)
                end_blocks.append(b)

        best_block: Optional[str] = None
        best_key = None
        for end_block in end_blocks:
            candidates = self._find_walk_candidates(
                end_block, target_block_id, train_id, manual_override=manual_override
            )
            if not candidates:
                continue
            # Cross-query comparison of the two ends' separate best candidates:
            # the first three keys only (estimated_travel_seconds, tile_count,
            # walk_length_cm). candidate_rank is excluded because it carries no
            # meaning across two queries. The end_a end is compared first and
            # only replaced on a strictly smaller key, so an exact tie keeps
            # end_a per the canonical tie-break.
            key = candidates[0].cross_query_key()
            if best_key is None or key < best_key:
                best_key = key
                best_block = end_block
        return best_block or head_block

    def _train_head_is_on_switch(self, train) -> bool:
        """Whether the train's head tile is a switch resource."""
        head_tile = self._head_tile_for_train(train)
        if not head_tile:
            return False
        track = self.network.get_track(*head_tile)
        if track is None:
            return False
        return bool(track.is_switch)

    def _segment_continues_forward(
        self, path: Path, segment: PathSegment
    ) -> bool:
        """Whether ``segment`` continues forward from ``path``'s authority end.

        ``segment`` is a candidate produced by ``find_paths`` whose first tile
        sits on the path's current authority-end block. The segment continues
        forward when its first hop exits that block at the geometrically
        valid exit edge — i.e. the edge the train would naturally take from
        its current entry, which for non-switch tracks is the unique non-
        entry edge defined by the track bits. Any other first-hop direction
        would force a 180-degree reverse through the authority-end block.
        """
        if not segment.tiles or len(segment.tiles) < 2:
            # Can't tell from geometry alone; treat as forward and let
            # downstream validation/acquisition reject if needed.
            return True
        path_tiles = path.get_path_tiles()
        if not path_tiles:
            return True
        terminal = path_tiles[-1]
        first = segment.tiles[0]
        nxt = segment.tiles[1]
        if (first[0], first[1]) != (terminal.x, terminal.y):
            # Candidate starts from a different tile on the authority-end
            # block than the existing prefix's terminal. find_paths is free
            # to start from any tile on the start block, so this means the
            # candidate would have to traverse the start block in a way
            # the existing prefix did not — that is a reversal of the
            # existing travel frame.
            return False
        seg_exit = direction_from_delta(nxt[0] - first[0], nxt[1] - first[1])
        if seg_exit is None:
            return True

        # Derive the terminal tile's true geometric exit edge from track
        # geometry (the unique non-entry edge for non-switch tracks). Note:
        # `PathTileEntry.exit_edge` cannot be used here directly — for
        # zigzag (NW/SE/NE/SW) terminal tiles its terminal-fallback value
        # is OPPOSITE-of-entry, which represents the train's velocity
        # vector rather than the tile's outgoing edge. Those two are
        # perpendicular on diagonals, so consulting `exit_edge` rejects
        # legitimate forward extensions on diagonal terminal tiles.
        terminal_entry = terminal.entry_edge or None
        if not terminal_entry:
            return True
        terminal_track = self.network.get_track(terminal.x, terminal.y)
        if terminal_track is None:
            return True
        valid_exits = terminal_track.get_exits(terminal_entry, use_switch_state=True)
        if not valid_exits:
            return True
        return seg_exit in valid_exits

    def _segment_would_revisit_resources(
        self,
        segment: PathSegment,
        existing_blocks: Set[str],
        existing_switches: Set[str],
    ) -> bool:
        """True iff the ordered tile walk over ``segment`` would land on a
        block or switch already in ``existing_blocks`` / ``existing_switches``,
        or on a block / switch already visited earlier in the same walk.

        Per design_new.md "Path extension": a path's
        ``resource_sequence`` must not contain the same block or switch
        twice; ``extend_path`` must reject any extension that would cause a
        resource to appear twice in the combined path, and ``create_path``
        must reject candidates with internal repeats.

        The first encountered block run in ``segment`` is treated as the
        seam (its block_id is recorded in ``seen_blocks`` and skipped for
        the overlap-against-``existing_blocks`` check; this matches the
        translator's ``skip_first_block=True`` behaviour for ``extend_path``
        and is harmless for ``create_path`` because ``existing_blocks`` is
        empty there). All later block runs and every switch tile are
        checked against both sets.

        ``find_ordered_walk`` dedupes the candidate's ``blocks`` list but
        leaves ``path_tiles`` free to traverse a block twice via non-
        contiguous runs (its ``visited`` set is keyed on
        ``(x, y, exit_edge, frozenset(blocks))`` so re-entering with a
        grown blocks set is treated as a fresh state). The translator at
        ``_segment_to_resource_sequence`` walks tiles and emits one
        resource entry per contiguous same-block run, which is what
        produces the duplicate ``resource_sequence`` entries seen in the
        bug. Walking ``segment.tiles`` here catches that case.
        """
        seen_blocks: Set[str] = set()
        seen_switches: Set[str] = set()
        prev_block_id: Optional[str] = None
        is_first_block_run = True
        for tile in segment.tiles:
            track = self.network.get_track(*tile)
            if track is None:
                continue
            if getattr(track, "is_switch", False):
                switch_entity = self._resolve_switch_at_position(tile)
                switch_id = (
                    getattr(switch_entity, "switch_id", None)
                    or getattr(track, "switch_id", None)
                )
                if not switch_id:
                    continue
                if switch_id in existing_switches or switch_id in seen_switches:
                    return True
                seen_switches.add(switch_id)
                # Switch interrupts any in-progress block run.
                prev_block_id = None
                continue
            block_id = getattr(track, "block_id", None)
            if not block_id:
                continue
            if block_id == prev_block_id:
                continue  # still inside the same contiguous tile run
            prev_block_id = block_id
            if is_first_block_run:
                is_first_block_run = False
                seen_blocks.add(block_id)
                continue
            if block_id in existing_blocks or block_id in seen_blocks:
                return True
            seen_blocks.add(block_id)
        return False

    # ========================================================================
    # Endpoint Discovery
    # ========================================================================

    def get_reachable_endpoints(
        self, start_block_id: str, include_stations: bool = True
    ) -> List[str]:
        """Topological reachable-endpoints from ``start_block_id``.

        Thin wrapper over the canonical
        ``RailwayPathfinder.get_reachable_endpoints`` — the dispatcher
        keeps this entry point for existing callers; the traversal
        itself lives in the pathfinder home (design_new.md
        "Operational pathfinding home").
        """
        return self._pathfinder.get_reachable_endpoints(
            start_block_id, include_stations
        )

    def get_reachable_endpoints_for_train(
        self, train_id: str, include_stations: bool = True
    ) -> List[str]:
        """Span-aware reachable endpoints for a placed train.

        Resolves the train's current span and its two end-outward edges via
        its ``TrainController`` and delegates to
        ``RailwayPathfinder.get_reachable_endpoints_for_span``. This is what
        the Quick Route tool calls: the result is exactly the set of blocks
        (and stations) the train can reach driving forward out of its head or
        in reverse out of its tail, never routing back through its own body or
        through a switch its body is sitting on. Returns an empty list when
        the train has no controller or no live span.
        """
        ctrl = self._get_controller(train_id)
        if ctrl is None:
            return []
        span = ctrl.get_current_span()
        if span is None:
            return []
        seeds = ctrl.get_span_end_exits()
        if not seeds:
            return []
        return self._pathfinder.get_reachable_endpoints_for_span(
            head_seeds=seeds,
            occupied_tiles=list(getattr(span, "occupied_tiles", None) or []),
            occupied_blocks=list(getattr(span, "occupied_blocks", None) or []),
            include_stations=include_stations,
        )

    def get_reachable_endpoints_for_extension(
        self, path_id: str, include_stations: bool = True
    ) -> Tuple[List[str], Optional[str]]:
        """All blocks reachable in the FORWARD direction from path's end."""
        path = self.paths.get(path_id)
        if not path:
            return ([], f"Path {path_id} not found")
        if not path.can_be_extended():
            return ([], f"Path cannot be extended (status: {path.status.value})")
        path_tiles = path.get_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return ([], "Path has insufficient tiles to determine direction")

        last_tile = path_tiles[-1]
        entry_edge = path.get_last_tile_entry_edge()
        if not entry_edge:
            return ([], "Cannot determine path exit direction")

        last_track = self.network.get_track(last_tile.x, last_tile.y)
        if not last_track:
            return ([], f"No track at path end position ({last_tile.x},{last_tile.y})")

        valid_exits = last_track.get_exits(entry_edge, use_switch_state=False)
        if not valid_exits:
            return ([], "No valid exits from path end tile")

        # Blocks the path still holds cannot be extension destinations; ones it
        # has already released can (see `_blocks_held_by`), so a loop's first
        # block stays offerable as a destination once the train has passed it.
        existing_blocks = self._blocks_held_by(path)
        reachable_blocks: Set[str] = set()
        visited: Set[Tuple[int, int, str]] = set()

        for exit_edge in valid_exits:
            queue = deque([(last_tile.x, last_tile.y, exit_edge)])
            while queue:
                curr_x, curr_y, curr_exit = queue.popleft()
                state = (curr_x, curr_y, curr_exit)
                if state in visited:
                    continue
                visited.add(state)
                if len(visited) > 1000:
                    break
                next_track = self.network.get_neighbor(
                    curr_x, curr_y, curr_exit
                )
                if not next_track:
                    continue
                next_x, next_y = next_track.x, next_track.y
                next_block_id = next_track.block_id
                if next_block_id and next_block_id not in existing_blocks:
                    reachable_blocks.add(next_block_id)
                next_entry_edge = OPPOSITE_EDGE.get(curr_exit)
                if not next_entry_edge or next_entry_edge not in next_track.edges:
                    continue
                all_exits = next_track.get_exits(
                    next_entry_edge, use_switch_state=False
                )
                for next_exit in all_exits:
                    queue.append((next_x, next_y, next_exit))

        result = list(reachable_blocks)
        if include_stations:
            for station_id, station in self.network.stations.items():
                for pos in station.positions:
                    track = self.network.get_track(*pos)
                    if track and track.block_id in reachable_blocks:
                        if station_id not in result:
                            result.append(station_id)
                        break
        return (result, None)

    # ========================================================================
    # Pathfinding
    # ========================================================================

    def find_paths(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
        manual_override: bool = False,
    ) -> List[PathSegment]:
        """Return the geometry candidates between two blocks.

        Thin wrapper over the shared `RailwayPathfinder.find_ordered_walk`.
        Each surviving candidate's geometry payload is unwrapped into a
        `PathSegment` for the existing dispatcher callers.

        `manual_override` (default False) is propagated from the calling
        `create_path(...)` / `extend_path(...)` per the `disabled` and `manual`
        availability flags canonical home in design_new.md (under TrackResource).
        """
        return [
            c.walk
            for c in self._find_walk_candidates(
                start_block_id, end_block_id, train_id,
                direction_constraint=direction_constraint,
                manual_override=manual_override,
            )
            if isinstance(c.walk, PathSegment)
        ]

    def forward_continuing_arrival_direction(
        self,
        path_id: str,
        target_block_id: str,
        manual_override: bool = False,
    ) -> Optional[str]:
        """The block-relative arrival direction at `target_block_id` for the
        walk that continues forward from this path's authority end, or `None`
        when it cannot be decided.

        A train extending its live authority is moving and cannot reverse
        through its authority-end block, so when a waypoint is reachable in both
        block frames at most one of them continues the train forward.
        `RouteExecutor` uses this to resolve a waypoint authored
        `task_direction == "either"` for an extend: resolving from the train's
        current block by shortest walk (see
        `RouteTargetResolver.resolve_task_direction`) can pick the frame whose
        walk would reverse at the authority end, which candidate search then
        rejects. Returns the unambiguous forward-continuing frame, or `None`
        when both frames continue forward (either is fine — keep the caller's
        resolution) or neither does (the extend fails legitimately downstream).
        """
        path = self.paths.get(path_id)
        if path is None:
            return None
        authority_end = path.get_reservation_end_block_id()
        if not authority_end:
            return None
        forward_ok = False
        reverse_ok = False
        for direction in ("forward", "reverse"):
            candidates = self.find_walk_candidates(
                authority_end,
                target_block_id,
                path.train_id,
                direction_constraint=direction,
                manual_override=manual_override,
            )
            if any(
                self._segment_continues_forward(path, c.walk) for c in candidates
            ):
                if direction == "forward":
                    forward_ok = True
                else:
                    reverse_ok = True
        if forward_ok and not reverse_ok:
            return "forward"
        if reverse_ok and not forward_ok:
            return "reverse"
        return None

    def find_walk_candidates(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
        manual_override: bool = False,
    ) -> List[GraphWalkCandidate]:
        """Return the ranked walk candidates between two blocks.

        The candidate-returning counterpart to `find_paths(...)`, which unwraps
        each candidate to its `.walk` PathSegment. A caller that must compare
        candidates across two separate queries — e.g. `RouteExecutor`
        platform-end selection — uses this so the comparison keys the candidate
        carries (including `estimated_travel_seconds`, populated when the train
        has a max speed) survive to the comparison. When `train_id` names a
        train, its max-speed / curve-radius / slope / length capabilities are
        resolved and passed to the query exactly as in `find_paths(...)`.
        """
        return self._find_walk_candidates(
            start_block_id, end_block_id, train_id,
            direction_constraint=direction_constraint,
            manual_override=manual_override,
        )

    def _find_walk_candidates(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
        manual_override: bool = False,
    ) -> List[GraphWalkCandidate]:
        """Return the ranked walk candidates between two blocks.

        The candidate carries both the flat `walk` (a `PathSegment`, used by the
        structural-validation/acquisition helpers) and the per-step `ordered_walk`
        (used to build `resource_sequence` and block/switch traversals without
        re-deriving from flat tiles). `create_path` / `extend_path` consume the
        whole candidate; `find_paths` is the thin `.walk`-only adapter for the
        UI/route callers that need only geometry.
        """
        # Resolve the train's curve-radius capability (so the pathfinder can
        # reject curves it cannot take) and its max speed (so candidates rank by
        # travel time). None (no train context) disables both, leaving the
        # distance-only ordering for geometry-only callers.
        train_min_radius_cm: Optional[float] = None
        train_max_speed_kmh: Optional[float] = None
        train_max_slope_percent: Optional[float] = None
        if train_id is not None:
            train = self.network.trains.get(train_id)
            if train is not None:
                train_min_radius_cm = train.min_radius_cm
                train_max_speed_kmh = train.max_speed
                train_max_slope_percent = train.max_slope
        request = self._pathfinder.block_walk_query(
            start_block_id,
            end_block_id,
            direction_constraint=direction_constraint,
            manual_override=manual_override,
            train_min_radius_cm=train_min_radius_cm,
            train_max_speed_kmh=train_max_speed_kmh,
            train_max_slope_percent=train_max_slope_percent,
        )
        if request is None:
            return []
        return self._pathfinder.find_ordered_walk(request)

    # ========================================================================
    # Validation
    # ========================================================================

    def validate_path(
        self,
        path: PathSegment,
        path_id: str,
        train_id: Optional[str] = None,
        manual_override: bool = False,
    ) -> ValidationResult:
        """Validate a PathSegment for resource availability.

        `manual_override` (default False) is the operator-override flag per the
        `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). When False, blocks/switches with
        `manual == True` produce ``BLOCK_MANUAL`` / ``SWITCH_MANUAL`` structural
        validation errors. When True, the manual filter is bypassed (disabled
        resources still produce ``BLOCK_DISABLED`` errors).
        """
        errors: List[ValidationError] = []
        for tile in path.tiles:
            track = self.network.get_track(*tile)
            if not track:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.UNBLOCKED_TILE,
                        tile=tile,
                        message=f"No track at position {tile}",
                    )
                )
                continue
            if track.is_switch:
                continue
            if not track.block_id:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.UNBLOCKED_TILE,
                        tile=tile,
                        message=f"Tile {tile} is not in any block",
                    )
                )

        for block_id in path.blocks:
            block = self.network.blocks.get(block_id)
            if not block:
                continue
            if block.disabled:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.BLOCK_DISABLED,
                        block_id=block_id,
                        message=f"Block {block_id} is disabled",
                    )
                )
                continue
            if getattr(block, "manual", False) and not manual_override:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.BLOCK_MANUAL,
                        block_id=block_id,
                        message=(
                            f"Block {block_id} is set to manual "
                            f"(manual_override required)"
                        ),
                    )
                )
                continue
            if not block.is_available_for_path(path_id, train_id):
                if block.state == BlockState.OCCUPIED:
                    errors.append(
                        ValidationError(
                            type=ValidationErrorType.BLOCK_OCCUPIED,
                            block_id=block_id,
                            occupied_by=block.occupied_by,
                            message=f"Block {block_id} is occupied",
                        )
                    )
                elif block.state == BlockState.RESERVED:
                    errors.append(
                        ValidationError(
                            type=ValidationErrorType.BLOCK_RESERVED,
                            block_id=block_id,
                            locked_by_path=block.reserved_by_path,
                            message=f"Block {block_id} is reserved",
                        )
                    )
            kept_for = self._meet_way_holder("block", block_id)
            if kept_for is not None and kept_for != train_id:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.MEET_WAY_KEPT,
                        block_id=block_id,
                        kept_for_train=kept_for,
                        message=(
                            f"Block {block_id} is on the way kept for {kept_for} "
                            "in a meet"
                        ),
                    )
                )

        for pos, required_state in path.required_switches.items():
            track = self.network.get_track(*pos)
            if not track:
                continue
            # Resolve the underlying Switch entity for availability checks.
            switch_entity = self._resolve_switch_at_position(pos)
            # Disabled switches are structural failures, not occupancy-class.
            # Per design_new.md "`disabled` and `manual` availability flags" canonical home, a
            # disabled switch behaves like missing track; per
            # ValidationErrorType.is_occupancy_class, SWITCH_DISABLED is not
            # occupancy-class, so incremental acquisition will not retry around it and a
            # RouteExecutor sees `no_viable_path` instead of a retry storm.
            if switch_entity is not None and getattr(switch_entity, "disabled", False):
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.SWITCH_DISABLED,
                        position=pos,
                        message=f"Switch at {pos} is disabled",
                    )
                )
                continue
            if switch_entity is not None and getattr(switch_entity, "manual", False) and not manual_override:
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.SWITCH_MANUAL,
                        position=pos,
                        message=(
                            f"Switch at {pos} is set to manual "
                            f"(manual_override required)"
                        ),
                    )
                )
                continue
            if switch_entity is not None and not switch_entity.can_lock(
                required_state, manual_override=manual_override
            ):
                # Representative lock holder for the diagnostic field; the
                # canonical lock store is `Switch.locks` (the Switch lock
                # ownership rule in design_new.md "Switch Entity").
                holder_path = next(iter(switch_entity.locks), (None,))[0]
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.SWITCH_LOCKED,
                        position=pos,
                        locked_by_path=holder_path,
                        message=f"Switch at {pos} locked",
                    )
                )
            if switch_entity is not None:
                kept_for = self._meet_way_holder("switch", switch_entity.switch_id)
                if kept_for is not None and kept_for != train_id:
                    errors.append(
                        ValidationError(
                            type=ValidationErrorType.MEET_WAY_KEPT,
                            position=pos,
                            kept_for_train=kept_for,
                            message=(
                                f"Switch at {pos} is on the way kept for "
                                f"{kept_for} in a meet"
                            ),
                        )
                    )

        return ValidationResult(valid=len(errors) == 0, errors=errors, path=path)

    # ========================================================================
    # v1 Public API: create_path / extend_path / cancel_path
    # ========================================================================

    def _reject(
        self,
        failure_kind: PathAcquisitionFailureKind,
        reason: str,
        requested_destination_direction: str,
        requested_destination_block_id: Optional[str] = None,
        path_id: Optional[str] = None,
        reservation_end_block_id: Optional[str] = None,
    ) -> PathAcquisitionResult:
        """Build a `success=False` PathAcquisitionResult for create_path /
        extend_path validation guards.

        Per the **`create_path()` / `extend_path()` return contract**: on
        failure only `failure_kind` and `reason` carry information. `path_id`,
        `resource_sequence`, `reservation_end_block_id`, `appended_resource_count`,
        `reached_target`, and both
        target fields are `None`, so a caller can never mistake a rejection's
        echoed request for authority the dispatcher actually stored.

        The context each guard passes is folded into `reason` instead, so no
        diagnostic is lost. `reason` is human-readable only — callers branch on
        `success`, `appended_resource_count == 0` versus `> 0`, `reached_target`,
        and `failure_kind`, never on this text.
        """
        context = []
        if path_id:
            context.append(f"path_id={path_id}")
        if requested_destination_block_id:
            context.append(f"target={requested_destination_block_id}")
        if requested_destination_direction:
            context.append(f"direction={requested_destination_direction}")
        if reservation_end_block_id:
            context.append(f"authority_end={reservation_end_block_id}")
        detail = f"{reason} ({', '.join(context)})" if context else reason

        return PathAcquisitionResult(
            success=False,
            failure_kind=failure_kind,
            reason=detail,
            resource_sequence=None,
        )

    def create_path(
        self,
        requested_destination_block_id: str = "",
        train_id: str = "",
        requested_destination_direction: str = "forward",
        manual_override: bool = False,
        zero_reservation_end_standoff: bool = False,
        reservation_cap_block_id: Optional[str] = None,
        required_candidate_resource_keys: Optional[List[CandidateResourceKey]] = None,
    ) -> PathAcquisitionResult:
        """v1 create_path: build authority for a train to a destination block.

        Argument order is dest-first per design_new.md "Canonical write
        interfaces"; every caller passes these by keyword. There is one
        acquisition behavior and no acquisition-mode parameter (design_new.md
        "One acquisition behavior"): it stores the furthest forward acquirable
        block-terminated prefix toward the target, bounded by
        `reservation_cap_block_id`.

        Args:
            requested_destination_block_id: Caller's target block.
            train_id: Train requesting authority. Required in v1.
            requested_destination_direction: "forward" or "reverse".
            manual_override: Operator-override flag per the `disabled` and
                `manual` availability flags canonical home in design_new.md
                (under TrackResource). Default False (every automatic caller,
                including `RouteExecutor`); True only for operator-initiated
                API/UI/admin calls that explicitly chose to use manual
                resources. Disabled resources are always excluded regardless.
            reservation_cap_block_id: Meet-coordination acquisition cap per the
                `Reservation-cap rule` (design_new.md, "Reservation-cap rule"). Default None (no
                cap). When set short of the requested target, authority is
                acquired toward the requested target but never stored past the
                cap block, so `reservation_end_block_id` lands on the cap and the
                result carries `reached_target = False` (never `True`) — the
                train holds at the cap without completing. The requested target
                is unchanged.
            required_candidate_resource_keys: Meet candidate-pinning per the
                `Path API migration` and `Meet coordination` (design_new.md).
                Default None. When set, it is the exact ordered
                `(resource_type, resource_id, entry_edge, exit_edge)` key list of
                the candidate the tick-start meet calculation selected; dispatcher
                filters the direction-compatible candidates to that one route
                before applying acquisition rules, never falling through to
                another candidate. A newly-invalid pin returns the ordinary
                no-viable/blocked result for this tick.

        Returns:
            PathAcquisitionResult.
        """
        # Validate direction.
        if requested_destination_direction not in ("forward", "reverse"):
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Invalid direction: {requested_destination_direction!r}",
            )

        if not train_id:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason="train_id required in v1",
            )

        train = self._get_train(train_id)
        if train is None:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Train {train_id} not found",
            )

        if getattr(train, "is_unidentified", False):
            # `PathDispatcher` never creates a path for an unidentified train: it has no
            # speed profile, no route and no operator controls, so there is nothing
            # to grant authority to (design_new.md "Unidentified trains", Never driven).
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Train {train_id} is an unidentified train; it is never given a path",
            )

        if not requested_destination_block_id:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_direction=requested_destination_direction,
                reason="requested_destination_block_id required",
            )
        if requested_destination_block_id not in self.network.blocks:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=f"Unknown block {requested_destination_block_id}",
            )

        # One ACTIVE path per train. Per design_new.md, this rejection must
        # fire as the first step (before path_id allocation, switch
        # acquisition, block reservation, or any controller / switch state
        # mutation) and surface the canonical TRAIN_ALREADY_HAS_ACTIVE_PATH
        # failure kind.
        existing = self.get_active_path(train_id)
        if existing is not None:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.TRAIN_ALREADY_HAS_ACTIVE_PATH,
                path_id=existing.path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=f"Train {train_id} already has active path {existing.path_id}",
            )

        # Resolve the head-end block (the fallback start-side anchor, and the
        # same-block rejection key below).
        head_block = self._get_current_block_id_for_train(train)
        if not head_block:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=f"Cannot resolve current block for train {train_id}",
            )

        # A same-HEAD-block target is a covered stop, finished by Plan's
        # covered-task index advance — never by dispatcher authority. Per the
        # create_path flow (design_new.md item 8) and invariant 9 (dispatcher
        # "never synthesizes an in-place frame from the train's own covered
        # tiles"), reject a destination already covered by the train's HEAD block
        # with `invalid_request`. A non-head spanned block (the tail) is a
        # legitimate turnaround target (e.g. a station turnaround route whose
        # next stop is the block the train's tail still occupies) and still falls
        # through to the normal two-end candidate search below — so the
        # rejection is head-keyed, NOT full-span.
        if head_block == requested_destination_block_id:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason="destination block already contains the train head",
            )

        # Resolve the start-side block for candidate search (design_new.md
        # create_path flow item 4). The train starts from whichever physical span
        # end has the shorter successful graph walk to the target, so a reverse /
        # turnaround move plans out of the tail end instead of failing to walk
        # from the head.
        start_block_id_resolved = self._resolve_create_start_block(
            train,
            train_id,
            requested_destination_block_id,
            head_block,
            manual_override,
        )

        # Find geometry candidates (shortest first). The pathfinder enforces
        # `Block.allowed_travel_directions` over the whole walk and rejects any
        # candidate whose terminal traversal does not satisfy the caller's
        # `requested_destination_direction` (design_new.md "`create_path()`
        # flow" step 2/6). Arrival direction is thereby guaranteed at
        # acquisition and never re-checked at completion. A terminal block whose
        # geometry cannot be classified (for example a single-tile tunnel
        # terminal) yields `terminal_block_direction is None`, which the filter
        # treats as allowed, so the constraint never spuriously rejects an
        # undecidable terminal.
        candidates = self._find_walk_candidates(
            start_block_id_resolved,
            requested_destination_block_id,
            train_id,
            manual_override=manual_override,
            direction_constraint=requested_destination_direction,
        )
        if not candidates:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason="No path exists between start and destination",
            )

        # Reject candidates with internal repeats — a single path's
        # resource_sequence must not contain the same block or switch
        # twice. Per design_new.md "Path extension" the same
        # rule applies to create_path() and extend_path(). At create_path
        # time there is no existing path to overlap against; we only
        # catch tile-walks whose post-seam path re-enters an earlier block
        # within the same candidate (find_ordered_walk dedupes the blocks
        # list but allows revisits in path_tiles when the visited-key's
        # `frozenset(blocks)` differs).
        empty_set: Set[str] = set()
        non_revisit_candidates = [
            cand for cand in candidates
            if not self._segment_would_revisit_resources(
                cand.walk, empty_set, empty_set
            )
        ]
        if not non_revisit_candidates:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=(
                    "all forward candidates contain repeated resources "
                    "(internal loop)"
                ),
            )
        candidates = non_revisit_candidates

        # Meet candidate-pinning (design_new.md, "Path API migration"): when the tick-start
        # meet calculation pinned this train to one route, keep only the
        # direction-compatible candidate matching that exact ordered
        # resource/frame key before choosing. A newly-invalid pin (the pinned
        # route is no longer among the candidates) leaves nothing to acquire, so
        # this call fails closed for the tick — it never falls through to a
        # candidate meet coordination did not evaluate.
        if required_candidate_resource_keys is not None:
            candidates = [
                cand for cand in candidates
                if self._candidate_matches_pinned_keys(
                    cand.ordered_walk, required_candidate_resource_keys
                )
            ]
            if not candidates:
                return self._reject(
                    failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    reason=(
                        "pinned meet candidate is no longer viable "
                        "(no matching direction-compatible walk this tick)"
                    ),
                )

        # create_path step 2 (design_new.md "Path And PathDispatcher") — dispatcher
        # selects EXACTLY ONE candidate
        # (the shortest walk per the GraphWalkCandidate comparison) and applies
        # the single acquisition behavior to that one candidate. It must NOT fall
        # through to a second, longer candidate when the chosen one cannot be
        # acquired or truncated; choosing a different route to the same resolved
        # target is forbidden here (RouteExecutor owns trying a different resolved
        # target later). Committing to the single best candidate is what makes a
        # train wait at a contested switch instead of taking a map-long detour
        # around it.
        candidates = candidates[:1]

        path_id = self._generate_path_id(train_id, requested_destination_block_id)
        now = self._now()

        # Acquire the furthest storable prefix of the single chosen candidate.
        last_failed_resource_id: Optional[str] = None
        last_reason: str = ""
        had_structural_failure = False
        covered_blocks_create = self._train_covered_blocks(train_id)
        for cand in candidates:
            # `segment` is the flat walk used by the structural/acquisition
            # helpers; `cand.ordered_walk` is the per-step geometry threaded
            # into resource_sequence / traversal building.
            segment = cand.walk
            ordered_walk = cand.ordered_walk

            # Meet-coordination acquisition cap (design_new.md, "Reservation-cap rule"): never store
            # authority past the cap block. Raw-slice the candidate walk at the
            # cap, then let the ordinary validate/acquire path below handle any
            # within-cap conflict (best-effort truncation pulls it back further)
            # and report `reached_target = False` via the cap-aware success return.
            # Holding-point chunking bounds the same acquisition at the first
            # holding point ahead; the nearer of the two wins.
            cap_status, capped_segment, capped_walk = self._truncate_segment_to_cap(
                segment, ordered_walk,
                self._effective_reservation_cap(
                    segment, reservation_cap_block_id, requested_destination_block_id
                ),
                requested_destination_block_id,
            )
            if cap_status == "hold":
                # The cap is the train's current block: no forward authority can
                # be created past it. Skip the candidate so the train holds.
                last_reason = "held at meet ceiling (acquisition cap)"
                continue
            if cap_status == "capped":
                # A cap short of the requested target stores the capped prefix and
                # reports reached_target = False (design_new.md "Acquisition-cap
                # rule"): there is one acquisition behavior, so the train holds at
                # the cap rather than failing to acquire.
                segment = capped_segment
                ordered_walk = capped_walk
                # Item-11 truncation-terminal rules on a cap terminal the train
                # will PARK at (design_new.md item 11). A braking cap that lands on
                # a junction-fouling or reversal-trap block is fine while the train
                # rolls through it, but not if the train must stop there because
                # the block beyond is blocked. When the next block cannot be
                # acquired, pull the terminal back to the furthest legal one.
                next_after_cap = (
                    cand.walk.blocks[len(segment.blocks)]
                    if len(cand.walk.blocks) > len(segment.blocks)
                    else None
                )
                if next_after_cap is not None and not self._block_available_for_train(
                    next_after_cap, train_id
                ):
                    legal_len = self._furthest_legal_cap_terminal_len(
                        segment, train_id, next_block_after_cap=next_after_cap
                    )
                    if legal_len <= 0:
                        last_reason = (
                            "no legal terminal at or behind the acquisition cap"
                        )
                        continue
                    if legal_len < len(segment.blocks):
                        segment, cutoff_tiles = self._make_prefix_segment(
                            segment, legal_len
                        )
                        ordered_walk = self._slice_ordered_walk(
                            ordered_walk, cutoff_tiles
                        )

            # Covered-span invariant for a reverse move (design_new.md
            # "`create_path()` flow", item 9): a stored reverse
            # authority must reach at least one block the train does not occupy.
            # When a reverse candidate is entirely within the covered span — the
            # degenerate self-reversal onto a tail block the train already sits on,
            # with no track beyond it — skip it so the request is a total
            # acquisition failure (item 11) rather than a one-tick body-internal
            # path whose reframed head lands off its own authority. A forward move
            # is not excluded here: its head stays covered by the stored path, so
            # an overshot covered-stop authority within the span is legitimate.
            if (
                requested_destination_direction == "reverse"
                and covered_blocks_create
                and not (set(segment.blocks) - covered_blocks_create)
            ):
                last_reason = "reverse candidate stays within the train's covered span"
                continue

            validation = self.validate_path(
                segment, path_id, train_id, manual_override=manual_override
            )
            # Preemption (design_new.md "Preemption"): when the only blockers are
            # reserved-but-unlocked blocks held by strictly lower-priority runs,
            # take them (truncating those holders) and re-validate; otherwise this
            # returns the same failed validation and the ordinary truncate path
            # below stores the shorter acquirable prefix.
            validation = self._apply_preemption(
                validation, segment, path_id, train_id, manual_override
            )
            if not validation.valid:
                structural = [
                    e for e in validation.errors
                    if not e.type.is_occupancy_class()
                ]
                occupancy = [
                    e for e in validation.errors
                    if e.type.is_occupancy_class()
                ]

                # Track first conflicting block for diagnostic, regardless
                # of category — the failure tail uses it for messaging.
                for err in validation.errors:
                    if err.block_id and not last_failed_resource_id:
                        last_failed_resource_id = err.block_id
                        last_reason = err.message

                if structural:
                    # Structural failures (disabled block, missing track)
                    # always hard-reject per design_new.md, "Viable-but-unavailable rule".
                    had_structural_failure = True
                    continue

                # Occupancy-only conflict: truncate to the prefix before the
                # first failing block (design_new.md "One acquisition behavior").
                hint = self._compute_truncate_hint(segment, occupancy)
                truncated = self._best_effort_truncate(
                    segment, path_id, train_id,
                    requested_destination_direction,
                    start_at_block_count=hint,
                    manual_override=manual_override,
                    ordered_walk=ordered_walk,
                )
                if truncated is None:
                    continue
                path = self._build_path_from_segment(
                    path_id=path_id,
                    train_id=train_id,
                    segment=truncated,
                    now=now,
                    zero_reservation_end_standoff=zero_reservation_end_standoff,
                    ordered_walk=self._slice_ordered_walk(
                        ordered_walk, truncated.tiles
                    ),
                )
                self.paths[path_id] = path
                return PathAcquisitionResult(
                    success=True,
                    # A create builds a brand-new record, so every entry in it
                    # is one this call added.
                    appended_resource_count=len(path.resource_sequence),
                    reached_target=path.reaches_target(
                        requested_destination_block_id
                    ),
                    path_id=path_id,
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    reservation_end_block_id=path.get_reservation_end_block_id(),
                    resource_sequence=list(path.resource_sequence),
                    reason="best_effort partial acquisition",
                )

            # Acquire resources.
            (
                acquire_ok,
                fail_reason,
                fail_resource,
                inserted_switch_claim_keys,
            ) = self._acquire_segment_resources(
                segment, path_id, train_id, requested_destination_direction,
                manual_override=manual_override,
                ordered_walk=ordered_walk,
            )
            if not acquire_ok:
                last_failed_resource_id = fail_resource
                last_reason = fail_reason
                # Roll back only what this probe newly acquired (empty on a
                # failed reserve — the coordinator already released its journal).
                self._rollback_segment(
                    segment, path_id, inserted_switch_claim_keys
                )
                # Acquire-time conflict: try a truncated prefix of this segment
                # (design_new.md "One acquisition behavior").
                truncated = self._best_effort_truncate(
                    segment, path_id, train_id, requested_destination_direction,
                    manual_override=manual_override,
                    ordered_walk=ordered_walk,
                )
                if truncated is None:
                    continue
                # Build path from truncated subsequence.
                path = self._build_path_from_segment(
                    path_id=path_id,
                    train_id=train_id,
                    segment=truncated,
                    now=now,
                    zero_reservation_end_standoff=zero_reservation_end_standoff,
                    ordered_walk=self._slice_ordered_walk(
                        ordered_walk, truncated.tiles
                    ),
                )
                self.paths[path_id] = path
                return PathAcquisitionResult(
                    success=True,
                    # A create builds a brand-new record, so every entry in it
                    # is one this call added.
                    appended_resource_count=len(path.resource_sequence),
                    reached_target=path.reaches_target(
                        requested_destination_block_id
                    ),
                    path_id=path_id,
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    reservation_end_block_id=path.get_reservation_end_block_id(),
                    resource_sequence=list(path.resource_sequence),
                    reason="best_effort partial acquisition",
                )

            # Full acquisition success.
            path = self._build_path_from_segment(
                path_id=path_id,
                train_id=train_id,
                segment=segment,
                now=now,
                zero_reservation_end_standoff=zero_reservation_end_standoff,
                ordered_walk=ordered_walk,
            )
            self.paths[path_id] = path
            # When a meet acquisition cap left the stored authority short of the
            # requested target, report `reached_target = False` so the path
            # never satisfies try_complete_active_path — the train holds at the
            # cap (design_new.md "Reservation-cap rule", Capped-short behavior).
            created_reached = path.reaches_target(requested_destination_block_id)
            created_reason = (
                "created"
                if created_reached
                else "created (capped short of requested target)"
            )
            return PathAcquisitionResult(
                success=True,
                appended_resource_count=len(path.resource_sequence),
                reached_target=created_reached,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id(),
                resource_sequence=list(path.resource_sequence),
                reason=created_reason,
            )

        # All candidates failed. When the train head currently sits on a switch
        # resource we cannot store a block-terminated holding authority, so
        # surface the head-on-switch case explicitly per design_new.md, "Resource acquisition".
        # This only fires when the loop exhausted because of occupancy conflicts —
        # a structural failure (e.g. disabled downstream block) is reported
        # through the BLOCKED_BEFORE_TARGET tail instead.
        if (
            not had_structural_failure
            and self._train_head_is_on_switch(train)
        ):
            head_switch_reason = "cannot create holding authority while head is on a switch"
            if last_failed_resource_id:
                head_switch_reason += f" (blocked at {last_failed_resource_id})"
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=head_switch_reason,
            )
        blocked_reason = last_reason or "All candidate paths blocked"
        if last_failed_resource_id and last_failed_resource_id not in blocked_reason:
            blocked_reason = f"{blocked_reason} (blocked at {last_failed_resource_id})"
        return self._reject(
            failure_kind=PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET,
            requested_destination_block_id=requested_destination_block_id,
            requested_destination_direction=requested_destination_direction,
            reason=blocked_reason,
        )

    def extend_path(
        self,
        path_id: str = "",
        requested_destination_block_id: str = "",
        requested_destination_direction: str = "forward",
        manual_override: bool = False,
        reservation_cap_block_id: Optional[str] = None,
        required_candidate_resource_keys: Optional[List[CandidateResourceKey]] = None,
    ) -> PathAcquisitionResult:
        """v1 extend_path: append more authority to an existing ACTIVE path.

        `manual_override` (default False) is the operator-override flag per the
        `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). Default for every automatic
        caller; True only for operator-initiated API/UI/admin calls that
        explicitly chose to use manual resources.

        `reservation_cap_block_id` (default None) is the Meet-coordination
        acquisition cap per the `Reservation-cap rule` (design_new.md, "Reservation-cap rule").
        When set short of the requested target, the append is truncated so
        authority never extends past the cap block; when the next forward
        resource lies beyond the cap, the extend appends nothing (a
        valid prefix held back by the cap) and the train holds there. The
        requested target is unchanged.

        `required_candidate_resource_keys` (default None) applies the same exact
        meet candidate-pinning `create_path(...)` does (design_new.md, "Path API migration"): the
        appended suffix must be the forward tail of the pinned candidate's
        ordered `(resource_type, resource_id, entry_edge, exit_edge)` key list.
        When the named candidate is no longer viable the extend returns the
        ordinary blocked/no-growth result without falling through to another
        candidate, leaving the existing path unchanged.
        """
        if requested_destination_direction not in ("forward", "reverse"):
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                path_id=path_id or None,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Invalid direction: {requested_destination_direction!r}",
            )

        path = self.paths.get(path_id)
        if path is None or path.status != PathStatus.ACTIVE:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.MISSING_OR_INACTIVE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Path {path_id} missing or not ACTIVE",
            )

        if not requested_destination_block_id:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                path_id=path_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id() or None,
                reason="requested_destination_block_id required",
            )

        # The live authority already reaches this target block, so there is
        # nothing to append. Per design_new.md "create_path() / extend_path()
        # return contract" this is `reached_requested_target`, not a failure:
        # Plan re-requests the current target on every eligible tick while
        # the train runs out its authority, and each of those calls is a
        # successful no-op.
        #
        # The test is on the block alone, not on block + direction. A task
        # authored `task_direction = "either"` is re-resolved from the train's
        # live position every tick, and the resolved direction legitimately
        # flips once the head enters the destination block — the walk that
        # reaches a block you are already standing in comes out the other way
        # round. Keying on direction would read that flip as a new request that
        # extend cannot serve. The direction that matters for arrival is the
        # one on the terminal frame this path already stored, and
        # `try_complete_active_path(...)` checks that independently before it
        # commits completion.
        if path.reaches_target(requested_destination_block_id):
            # Nothing was appended, so the terminal frame is unchanged. The
            # returned pair echoes what Plan passed (design_new.md "Path
            # extension"): the caller records it as the match key
            # for stop completion, and try_complete_active_path(...) checks the
            # actual terminal frame independently before it commits completion.
            return PathAcquisitionResult(
                success=True,
                appended_resource_count=0,
                reached_target=True,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id(),
                resource_sequence=list(path.resource_sequence),
                reason=None,
            )

        # A block this path still holds cannot be authorized a second time —
        # one path, one concurrent claim per block (see `_blocks_held_by`).
        # This is the repeated-resource rule, so it classifies as
        # `no_viable_path` per design_new.md "PathAcquisitionFailureKind".
        if requested_destination_block_id in self._blocks_held_by(path):
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id() or None,
                reason=(
                    f"{requested_destination_block_id} is still held by this path"
                ),
            )

        train_id = path.train_id
        train = self._get_train(train_id) if train_id else None
        if train is None:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id() or None,
                reason="path has no train",
            )

        # Find extension geometry from current authority end (shortest first).
        # As in create_path, the pathfinder enforces
        # `Block.allowed_travel_directions` over the whole walk and rejects any
        # candidate whose terminal traversal does not satisfy the caller's
        # `requested_destination_direction`; an undecidable terminal
        # (`terminal_block_direction is None`) is treated as allowed.
        start_block_id = path.get_reservation_end_block_id()
        candidates = self._find_walk_candidates(
            start_block_id,
            requested_destination_block_id,
            train_id,
            manual_override=manual_override,
            direction_constraint=requested_destination_direction,
        )
        if not candidates:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id() or None,
                reason=f"No forward path from {start_block_id} to {requested_destination_block_id}",
            )

        # Reject candidates that would reverse 180 degrees through the
        # current authority-end block. Per design_new.md "extend_path()
        # contract", the new suffix must continue forward from the existing
        # prefix's travel frame; if every candidate would have to exit the
        # authority-end block via an edge other than the prefix's terminal
        # exit_edge, no forward suffix reaches the target. Extension is
        # forward-only, so that is `no_viable_path` and the `reason` names the
        # reversal (design_new.md "`create_path()` / `extend_path()` return
        # contract", the `no_viable_path` entry).
        forward_candidates = [
            cand
            for cand in candidates
            if self._segment_continues_forward(path, cand.walk)
        ]
        if not forward_candidates:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id() or None,
                reason="extend would reverse at authority end",
            )
        candidates = forward_candidates

        # Reject candidates whose tile walk would make this path hold one block
        # or switch twice at the same time, per the repeated-resource rule in
        # design_new.md "Path invariants". Two separate checks live inside
        # `_segment_would_revisit_resources`: the candidate must not repeat a
        # resource within its own walk, and it must not land on a resource the
        # path is still holding. Without them, find_ordered_walk can return a
        # candidate whose post-seam tile path re-enters a block the path
        # already holds (via the south tunnel, or any other connection that
        # loops the network), incremental acquisition truncates that loop into a closed
        # prefix, and extend_path appends one full loop every Plan tick.
        #
        # Both blocks and switches are tested against live holdings
        # (`_blocks_held_by` / `_switches_held_by`), not the traversal history,
        # per design_new.md "Path invariants" (What counts as held is the live
        # claim, not the traversal record). Head entry consumes this path's main
        # switch lock and switch tail vacate clears its train-owned protection,
        # so a switch leaves the held set once the train has passed and vacated
        # it — which is what lets a `CIRCULAR` suffix re-cross a switch to close
        # its lap. A switch still carrying a live claim from this path stays in
        # the set and correctly blocks a double claim.
        existing_path_blocks = self._blocks_held_by(path)
        existing_path_switches = self._switches_held_by(path)
        non_revisit_candidates = [
            cand for cand in candidates
            if not self._segment_would_revisit_resources(
                cand.walk, existing_path_blocks, existing_path_switches
            )
        ]
        if not non_revisit_candidates:
            return self._reject(
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id() or None,
                reason=(
                    "all forward candidates would revisit a resource already "
                    "in the path"
                ),
            )
        candidates = non_revisit_candidates

        # Meet candidate-pinning (design_new.md, "Path API migration"): keep only the forward
        # suffix that continues the pinned candidate's route. When the pin no
        # longer matches any forward continuation, do NOT fall through to another
        # candidate and do NOT tear the path down — return the ordinary no-growth
        # retry result (success, appended 0, reached_target False) and leave the
        # existing live path unchanged, so the next tick can recompute the meet
        # assignment. A stale pin thus fails closed for one tick only.
        if required_candidate_resource_keys is not None:
            candidates = [
                cand for cand in candidates
                if self._candidate_matches_pinned_keys(
                    cand.ordered_walk, required_candidate_resource_keys
                )
            ]
            if not candidates:
                return PathAcquisitionResult(
                    success=True,
                    appended_resource_count=0,
                    reached_target=False,
                    path_id=path_id,
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    reservation_end_block_id=path.get_reservation_end_block_id(),
                    resource_sequence=list(path.resource_sequence),
                    reason=(
                        "pinned meet candidate is no longer viable; holding the "
                        "existing authority for the next tick's meet assignment"
                    ),
                )

        # create_path step 2 / extend_path single-candidate rule (design_new.md
        # "Path And PathDispatcher") — like create_path, extend commits to
        # EXACTLY ONE candidate (the shortest forward-continuing extension) and
        # never falls through to a longer alternative route to the same target.
        # When that one extension is blocked the tail appends nothing
        # so RouteExecutor keeps retrying the same path_id (train waits) rather
        # than the dispatcher detouring it.
        candidates = candidates[:1]

        last_reason = ""
        last_resource = None
        had_occupancy_blocker = False
        for cand in candidates:
            # `segment` is the flat walk used by the structural/acquisition
            # helpers; `cand.ordered_walk` is the per-step geometry threaded
            # into resource_sequence / traversal building (Step 3+).
            segment = cand.walk
            ordered_walk = cand.ordered_walk

            # Meet-coordination acquisition cap (design_new.md, "Reservation-cap rule"): never append
            # authority past the cap block. The forward suffix's first block is
            # the current authority end; a cap there (or off this suffix) means
            # the train already holds at the cap -> no growth. Holding-point
            # chunking bounds the same append at the next holding point past the
            # current authority end; the nearer of the two wins.
            cap_status, capped_segment, capped_walk = self._truncate_segment_to_cap(
                segment, ordered_walk,
                self._effective_reservation_cap(
                    segment, reservation_cap_block_id, requested_destination_block_id
                ),
                requested_destination_block_id,
            )
            if cap_status == "hold":
                had_occupancy_blocker = True
                last_reason = "held at meet ceiling (acquisition cap)"
                continue
            if cap_status == "capped":
                segment = capped_segment
                ordered_walk = capped_walk
                # Item-11 truncation-terminal rules on a cap terminal the train
                # will PARK at (design_new.md item 11): a braking cap must not
                # append authority ending on a junction-fouling or reversal-trap
                # block when the block beyond is blocked (the train can't grow past
                # it). When the next block cannot be acquired, pull the terminal
                # back to the furthest legal one; when that leaves only the current
                # authority end, the extend appends nothing and the train holds.
                next_after_cap = (
                    cand.walk.blocks[len(segment.blocks)]
                    if len(cand.walk.blocks) > len(segment.blocks)
                    else None
                )
                if next_after_cap is not None and not self._block_available_for_train(
                    next_after_cap, train_id
                ):
                    legal_len = self._furthest_legal_cap_terminal_len(
                        segment, train_id, next_block_after_cap=next_after_cap
                    )
                    if legal_len <= 1:
                        had_occupancy_blocker = True
                        last_reason = (
                            "no legal terminal past the authority end at the cap"
                        )
                        continue
                    if legal_len < len(segment.blocks):
                        segment, cutoff_tiles = self._make_prefix_segment(
                            segment, legal_len
                        )
                        ordered_walk = self._slice_ordered_walk(
                            ordered_walk, cutoff_tiles
                        )

            # Validate (pretend the extension uses the same path_id).
            validation = self.validate_path(
                segment, path_id, train_id, manual_override=manual_override
            )
            # Preemption (design_new.md "Preemption"): a strictly higher-priority
            # extend may take reserved-but-unlocked blocks from a lower-priority
            # holder here — the primary preemption site, a running train growing
            # toward contested track. No eligible victim → validation unchanged.
            validation = self._apply_preemption(
                validation, segment, path_id, train_id, manual_override
            )
            applied_segment: Optional[PathSegment] = None
            if not validation.valid:
                structural = [
                    e for e in validation.errors
                    if not e.type.is_occupancy_class()
                ]
                occupancy = [
                    e for e in validation.errors
                    if e.type.is_occupancy_class()
                ]
                for err in validation.errors:
                    if err.block_id and last_resource is None:
                        last_resource = err.block_id
                        last_reason = err.message

                if structural:
                    continue
                # Pure occupancy failure: this is the "valid prefix,
                # temporarily blocked" condition described by
                # design_new.md, "Viable-but-unavailable rule". Track it so the tail can return
                # `appended_resource_count = 0` if no candidate grows the path.
                had_occupancy_blocker = True

                # Occupancy-only conflict: truncate to the longest acquirable
                # prefix of the extension segment (design_new.md "One acquisition
                # behavior").
                hint = self._compute_truncate_hint(segment, occupancy)
                truncated = self._best_effort_truncate(
                    segment, path_id, train_id,
                    requested_destination_direction,
                    start_at_block_count=hint,
                    manual_override=manual_override,
                    ordered_walk=ordered_walk,
                )
                if truncated is None:
                    continue
                applied_segment = truncated
            else:
                (
                    ok,
                    fail_reason,
                    fail_resource,
                    inserted_switch_claim_keys,
                ) = self._acquire_segment_resources(
                    segment, path_id, train_id, requested_destination_direction,
                    manual_override=manual_override,
                    ordered_walk=ordered_walk,
                )
                if not ok:
                    last_resource = fail_resource
                    last_reason = fail_reason
                    self._rollback_segment(
                        segment, path_id, inserted_switch_claim_keys
                    )
                    # Acquire-time failure on a validation-passing segment
                    # is an occupancy-class blocker (a race between
                    # validation and acquire); track it so the tail can
                    # append nothing per the viable-but-unavailable rule.
                    had_occupancy_blocker = True
                    # Forward manual_override per the operator-initiated
                    # `manual_override` rule in design_new.md (under the
                    # `disabled` and `manual` availability flags canonical
                    # home); the sibling _best_effort_truncate sites in this file
                    # already do.
                    truncated = self._best_effort_truncate(
                        segment, path_id, train_id,
                        requested_destination_direction,
                        manual_override=manual_override,
                        ordered_walk=ordered_walk,
                    )
                    if truncated is None:
                        continue
                    applied_segment = truncated
                else:
                    applied_segment = segment

            # Append to existing path.resource_sequence (skipping the first
            # entry which is already present as authority_end).
            base_index = len(path.resource_sequence)
            new_entries = self._segment_to_resource_sequence(
                applied_segment, base_index, skip_first_block=True,
                ordered_walk=self._slice_ordered_walk(
                    ordered_walk, applied_segment.tiles
                ),
            )
            path.resource_sequence.extend(new_entries)
            # path_tiles is a derived `@property` over resource_sequence; no
            # explicit rebuild is needed (and no stored copy exists to write).
            # reservation_end_block_id is derived from resource_sequence[-1];
            # the resource_sequence.extend(...) above advanced it. The requested
            # target is route-owned and not stored on Path.
            # No status transition here: the path is already ACTIVE (guaranteed
            # by the MISSING_OR_INACTIVE_PATH guard at the top of extend_path).
            # Per design_new.md, `last_status_updated_time` advances only on a
            # real `status` transition, so a successful extend must not re-stamp
            # it.

            # Per design_new.md "create_path() / extend_path() return
            # contract": the authority grew, and `reached_target` says whether
            # it now reaches the requested target or is still short of it.
            grown_reached = path.reaches_target(requested_destination_block_id)
            grown_reason = (
                "extended to requested target"
                if grown_reached
                else "extended (truncated short of requested target)"
            )

            return PathAcquisitionResult(
                success=True,
                appended_resource_count=len(new_entries),
                reached_target=grown_reached,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id(),
                resource_sequence=list(path.resource_sequence),
                reason=grown_reason,
            )

        ext_reason = last_reason or "All extension paths blocked"
        if last_resource and last_resource not in ext_reason:
            ext_reason = f"{ext_reason} (blocked at {last_resource})"

        # Per design_new.md, "Viable-but-unavailable rule": when incremental acquisition extend cannot
        # append any new resource but the existing path is still a legal
        # forward prefix to the requested target (`find_paths` returned
        # forward candidates above) and the only obstruction was an
        # occupancy/reservation/lock conflict, return
        # `success=True` with `appended_resource_count = 0` and
        # `reached_target = False`. The requested target is route-owned and not
        # stored on Path; the `resource_sequence` is left untouched. Route
        # executors use this outcome to keep retrying the same path_id rather
        # than cancelling.
        if had_occupancy_blocker:
            # Take the no-growth hold when the only obstruction to extending is
            # an occupancy/reservation/lock conflict: an extend that cannot grow
            # returns appended_resource_count = 0 / reached_target = False, NOT
            # blocked_before_target (which only create_path returns)
            # (design_new.md, "Path API migration").
            # No status transition here: the path is already ACTIVE and stays
            # ACTIVE on a no-growth hold, so `last_status_updated_time` must
            # not be re-stamped (design_new.md: status transitions only).
            # The "waiting on" block is not stored — the UI-payload assembly
            # composes `waiting_for_block_id` live from the route-supplied
            # `get_waiting_for_block_id(...)` call (the `waiting_for_block_id`
            # live-recompute rule in design_new.md "Data Flow To UI").
            return PathAcquisitionResult(
                success=True,
                appended_resource_count=0,
                reached_target=False,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reservation_end_block_id=path.get_reservation_end_block_id(),
                resource_sequence=list(path.resource_sequence),
                reason=ext_reason,
            )

        # Extend failed outright but the existing authority is untouched and
        # still ACTIVE. The blocking resource for the "Show stuck" overlay is
        # not stored — the UI-payload assembly composes `waiting_for_block_id`
        # live from the route-supplied `get_waiting_for_block_id(...)` call (the
        # `waiting_for_block_id` live-recompute rule in design_new.md "Data Flow
        # To UI").
        # An extend that could not grow for a non-occupancy (structural) reason
        # is no_viable_path, never blocked_before_target — that failure kind is
        # create_path-only (design_new.md, "Path API migration").
        return self._reject(
            failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
            path_id=path_id,
            requested_destination_block_id=requested_destination_block_id,
            requested_destination_direction=requested_destination_direction,
            reservation_end_block_id=path.get_reservation_end_block_id() or None,
            reason=ext_reason,
        )

    def cancel_path(
        self,
        path_id: str,
        error: Optional["ErrorRecord"] = None,
        kind: str = "info",
        deviation: bool = False,
        latch: Optional[str] = None,
    ) -> CancellationResult:
        """v1 cancel_path: release reservations, mark path CANCELLED.

        Per design_new.md "Path API migration":
          * `error` is the diagnostic `ErrorRecord` that flows straight into the
            `cancel_path` event; its `origin` names what ended the path. It is
            required for every cancellation. No planning or routing logic
            branches on anything inside it.
          * `kind` is the event level the latch writer declared, carried through
            unchanged. This emit *is* the `path_cancel_request` consumption
            event (design_new.md "Latch consumption events"), so it reports the
            latched payload's own level rather than a level derived here.
          * `deviation` is the structured classification boolean forwarded to
            the route callback.
          * `latch` names the latch this cancel consumes, carried into the
            event's `payload.latch`: the Action explicit-cancel step passes
            `"path_cancel_request"`, and a direct cancel (environment
            activation, the layout emergency cut-out) leaves it `None`.
          * A `path_id` absent from `paths_by_id` is a caller-contract
            violation and raises (design_new.md "Path API migration"), like the
            error-pairing violation above.
          * An idempotent no-op (already ``COMPLETED`` / ``CANCELLED``) returns
            ``committed = False`` with ``new_status`` set to that terminal
            status and the canonical ``reason`` string (``"already completed"``
            / ``"already cancelled"``); ``new_status`` is always terminal on
            return.
          * On success the result carries ``committed = True`` and
            ``new_status = PathStatus.CANCELLED``.
        """
        if error is None:
            raise ValueError("every cancellation must carry an ErrorRecord")
        path = self.paths.get(path_id)
        if path is None:
            # A `path_id` not present in `paths_by_id` is a caller-contract
            # violation, not an outcome (design_new.md "Path API migration"): `cancel_path`
            # carries no `failure_kind` to report one, so it raises rather than
            # returning a result. Callers resolve the id through
            # `active_path_id_by_train(...)` first, so this never fires in v1.
            raise ValueError(
                f"cancel_path called with a path_id not present in "
                f"paths_by_id: {path_id!r}"
            )
        if path.status == PathStatus.COMPLETED:
            # Idempotent no-op: the path already released its future authority
            # through normal completion on an earlier tick. `new_status` is
            # always terminal on return (design_new.md "Path API migration").
            return CancellationResult(
                committed=False,
                path_id=None,
                new_status=PathStatus.COMPLETED,
                reason="already completed",
            )
        if path.status == PathStatus.CANCELLED:
            return CancellationResult(
                committed=False,
                path_id=None,
                new_status=PathStatus.CANCELLED,
                reason="already cancelled",
            )

        now = self._now()

        # Release block reservations + switch locks via the ResourceCoordinator.
        # Only the path's still-held future reservations are released; blocks
        # already consumed at head-entry are not part of the future set (see
        # `_blocks_still_reserved_by`). Switch locks are released for the whole
        # path inside `release_path_resources`, independent of this list.
        all_blocks = self._blocks_still_reserved_by(path.get_all_blocks(), path_id)
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is not None:
            coordinator.release_path_resources(
                path_id=path_id,
                block_ids=all_blocks,
                completed=False,
            )

        # Mark path CANCELLED.
        path._set_status(PathStatus.CANCELLED, now)

        # Notify route executor (bookkeeping only). Per design_new.md "route
        # path-event callback schema" the canonical
        # signature is `handle_path_cancellation(train_id, path_id, error)`. The
        # deviation classification is not passed to the callback — it rides the
        # `cancel_path` event emitted below (and `error.origin`), so the route
        # side records nothing of it here.
        train_id = path.train_id
        if train_id:
            route_executor = self._get_route_executor()
            if route_executor is not None:
                route_executor.handle_path_cancellation(
                    train_id,
                    path_id,
                    error=error,
                )

        # Emit the canonical `cancel_path` notification. Per design_new.md
        # "Latch consumption events" this event *is* the
        # `path_cancel_request` consumption event, so it carries the latched
        # payload's own `kind` (set by whichever writer latched it) rather than
        # a level derived here, and passes `latch_event=True` so the
        # unchanged-repeat suppression never drops it.
        nc = self._notification_center
        if nc is not None:
            try:
                from notification_center import train_ref as _train_ref, path_ref as _path_ref

                nc.emit(
                    source="PathDispatcher",
                    operation="cancel_path",
                    kind=kind,
                    message=f"path {path_id} cancelled for {train_id}",
                    entities=[_train_ref(train_id), _path_ref(path_id)],
                    error=error,
                    event_time=now,
                    payload={
                        "latch": latch,
                        "origin": error.origin,
                        "deviation": deviation,
                    },
                    latch_event=True,
                )
            except Exception:
                logger.warning(
                    "NotificationCenter.emit() failed for cancel_path "
                    "(path=%s); cancellation itself committed",
                    path_id,
                    exc_info=True,
                )

        # Per design_new.md "Retained path history": cancelled or completed paths remain
        # in `paths_by_id[path_id]` for history. They are no longer active
        # authority because `Path.status` is no longer ACTIVE, so
        # `active_path_id_by_train(...)` skips them and the controller's
        # `received_path_plan` property returns None on its next read.

        return CancellationResult(
            committed=True,
            path_id=path_id,
            new_status=PathStatus.CANCELLED,
            # `reason` is optional plain-text diagnostic context on the result;
            # the writer's own text now lives on the `ErrorRecord` that reaches
            # the event, so surface that rather than a second copy.
            reason=error.text if error is not None else None,
        )

    def _can_preempt(
        self, requesting_train_id: str, holder_train_id: Optional[str]
    ) -> bool:
        """True when `requesting_train_id`'s run is STRICTLY higher priority than
        the holder's — the priority half of the Preemption precondition
        (design_new.md "Preemption").

        Equal priority never preempts (first-come-first-served), so the
        comparison can never flip back and forth between two runs. Priority is
        read through the injected route resolver, the same dependency the
        reversal-trap exclusion uses; the dispatcher never reaches
        `RouteExecutor` through `SimulationController`.
        """
        if not holder_train_id or holder_train_id == requesting_train_id:
            return False
        rx = self._get_route_executor()
        if rx is None or not hasattr(rx, "effective_priority_for"):
            return False
        return rx.effective_priority_for(requesting_train_id) > rx.effective_priority_for(
            holder_train_id
        )

    def _apply_preemption(
        self, validation, segment, path_id: str, train_id: str, manual_override: bool
    ):
        """Preemption (design_new.md "Preemption"): when a candidate segment is
        blocked only by reserved-but-not-locked resources held by strictly
        lower-priority runs, take them and re-validate.

        A higher-priority run may take a reserved resource from a lower-priority
        one only when it is beyond the holder's braking-distance lock — for a
        block, `Block.locked_by_path is None`; for a switch, its
        `resource_sequence` index is beyond the holder's lock end and the holder
        neither occupies nor protects it. Committed track (a locked block, a
        switch at/before the lock end or already entered, or any occupied
        resource) is never taken. When EVERY occupancy conflict on the segment
        is such a preemptible resource, this truncates each holder's reservation
        before it through `truncate_path_before_resource(...)` (which releases
        the holder's tail and its `locked_by_path` flags, leaves it ACTIVE
        braking to its new nearer end, and never touches its `control_mode`),
        then re-validates so the caller's ordinary acquire path stores the
        preempting reservation.

        Any non-preemptible conflict — an occupied block, a locked block, a
        committed switch, or a holder of equal-or-higher priority — means no
        preemption: the segment's validation is returned unchanged and the
        caller falls back to truncating its own segment short. Preemption is
        atomic with acquisition: because every blocker is removed, the
        re-validated segment acquires cleanly on the caller's existing path.
        """
        if validation.valid:
            return validation
        occupancy = [e for e in validation.errors if e.type.is_occupancy_class()]
        structural = [e for e in validation.errors if not e.type.is_occupancy_class()]
        if structural or not occupancy:
            return validation
        victims = []  # (holder_path_id, resource_sequence_index)
        for err in occupancy:
            if err.type == ValidationErrorType.BLOCK_RESERVED and err.block_id:
                block = self.network.blocks.get(err.block_id)
                if block is None:
                    return validation
                holder_path = block.reserved_by_path
                if not holder_path or holder_path == path_id:
                    return validation
                if block.locked_by_path is not None:
                    # Committed to stopping within this block — never preemptible.
                    return validation
                if not self._can_preempt(train_id, block.reserved_for_train):
                    return validation
                holder = self.paths.get(holder_path)
                idx = (
                    self._forward_resource_index(holder, "block", err.block_id)
                    if holder is not None
                    else None
                )
                if idx is None:
                    return validation
                victims.append((holder_path, idx))
            elif err.type == ValidationErrorType.SWITCH_LOCKED and err.position is not None:
                switch = self._resolve_switch_at_position(err.position)
                holder_path = err.locked_by_path
                if switch is None or not holder_path or holder_path == path_id:
                    return validation
                holder = self.paths.get(holder_path)
                if holder is None:
                    return validation
                idx = self._forward_resource_index(
                    holder, "switch", switch.switch_id
                )
                # Beyond the holder's lock end, and not already entered/protected.
                if idx is None or idx <= self._lock_end_index(holder):
                    return validation
                if self._switch_committed_by_train(switch, holder.train_id):
                    return validation
                if not self._can_preempt(train_id, holder.train_id):
                    return validation
                victims.append((holder_path, idx))
            else:
                # An occupied block or any other occupancy conflict is committed
                # track — not preemptible.
                return validation
        for holder_path, idx in victims:
            self.truncate_path_before_resource(
                holder_path,
                idx,
                f"preempted by higher-priority train {train_id}",
            )
        return self.validate_path(
            segment, path_id, train_id, manual_override=manual_override
        )

    def _forward_resource_index(self, path, resource_type: str, resource_id: str):
        """First `resource_sequence` index strictly ahead of the head
        (`current_resource_index`) matching `(resource_type, resource_id)`, or
        `None`. Picks the forward (reserved) occurrence for a lapping path that
        holds one id at more than one index."""
        for i in range(path.current_resource_index + 1, len(path.resource_sequence)):
            entry = path.resource_sequence[i]
            if entry.resource_type == resource_type and entry.resource_id == resource_id:
                return i
        return None

    def _lock_end_index(self, path) -> int:
        """`resource_sequence` index of `path`'s lock end block, or the head
        index (`current_resource_index`) when the path locks nothing — the
        Preemption floor for a switch (everything strictly beyond is
        preemptible)."""
        lock_end_id = path.get_lock_end_block_id()
        if lock_end_id:
            for i in range(path.current_resource_index, len(path.resource_sequence)):
                entry = path.resource_sequence[i]
                if entry.resource_type == "block" and entry.resource_id == lock_end_id:
                    return i
        return path.current_resource_index

    def _switch_committed_by_train(self, switch, train_id: Optional[str]) -> bool:
        """True when `train_id` has physically committed to `switch` — it
        occupies it (`Switch.physical_occupants`) or protects it
        (`Switch.train_protection_claims`). A committed switch is never
        preempted (design_new.md "Preemption")."""
        if not train_id:
            return False
        occupants = getattr(switch, "physical_occupants", {}) or {}
        if train_id in occupants.values():
            return True
        claims = getattr(switch, "train_protection_claims", []) or []
        return any(claim and claim[0] == train_id for claim in claims)

    def truncate_path_before_resource(
        self,
        path_id: str,
        resource_sequence_index: int,
        reason: str,
    ) -> str:
        """Pull a live path's reservation back to end before the contested
        resource at `resource_sequence_index`, and return the shortened
        reservation's new end block id.

        Per design_new.md "Reservation truncation": a reserved-but-unlocked
        resource a path holds — a block a foreign train physically took (an
        Observe caller), or a block or switch a higher-priority run preempts —
        may be lost before this train reaches it, and reservation that runs
        through it is not reservation. The index carries the contested resource
        (block or switch) and disambiguates a lapping path that holds one
        resource id at more than one index. This cuts `resource_sequence` back
        to the furthest legal terminal that is both at or behind the last block
        before that index and at or beyond this path's lock end
        (`Path.lock_end_block_id`) — the cut never enters the locked span —
        releases the dropped entries' block reservations (and their
        `locked_by_path`) and switch locks, and leaves the path ACTIVE.

        It always succeeds: when no preferred legal terminal remains in that
        range, the cut falls back to the lock end itself (the last locked block,
        which the braking-distance lock guarantees the train can stop within),
        so the path always survives with a valid shorter remainder and no caller
        branches on a failure result. A path must end on a block (see "Path
        invariants"), so any switches between the terminal and the contested
        resource go with the dropped tail. Returns the new reservation end block
        id; an index out of range or at/behind the head (a resource the train
        already covers, nothing forward to cut) is a no-op that returns the
        current reservation end unchanged.
        """
        path = self.paths.get(path_id)
        if path is None or path.status != PathStatus.ACTIVE:
            return ""

        cut_index = resource_sequence_index
        # Out of range, or at/behind the head (a resource the train already
        # covers, nothing forward to cut): no-op, reservation unchanged.
        if (
            cut_index is None
            or cut_index < 0
            or cut_index >= len(path.resource_sequence)
            or cut_index <= path.current_resource_index
        ):
            return path.get_reservation_end_block_id()
        contested = path.resource_sequence[cut_index]

        # Lock-end floor: the cut never enters the locked span, so the terminal
        # search bottoms out at this path's lock end — the head's own block when
        # nothing ahead of it is locked.
        lock_floor_index = self._lock_floor_index(path)

        # Truncation-terminal rules (design_new.md "truncation-terminal rules"):
        # the cut lands at the furthest legal terminal at or behind the last
        # block before the contested resource and at or beyond the lock-end
        # floor. The reversal-trap check's "next block" is the first block at or
        # after the cut (the contested block itself, or the first block past a
        # contested switch).
        next_block_after_start = next(
            (
                e.resource_id
                for e in path.resource_sequence[cut_index:]
                if e.resource_type == "block"
            ),
            None,
        )
        keep_through = self._furthest_legal_terminal_index(
            path,
            cut_index - 1,
            next_block_after_start=next_block_after_start,
            floor_index=lock_floor_index,
        )
        if keep_through is None:
            # No preferred legal terminal in range — fall back to the lock end
            # (design_new.md "Reservation truncation", Where the cut lands).
            keep_through = lock_floor_index
        if keep_through is None:
            # Degenerate: no forward block at all to keep. Leave the reservation
            # unchanged rather than emptying it.
            return path.get_reservation_end_block_id()

        dropped = path.resource_sequence[keep_through + 1 :]
        dropped_blocks = self._blocks_still_reserved_by(
            [e.resource_id for e in dropped if e.resource_type == "block"], path_id
        )
        dropped_switches = [
            e.resource_id for e in dropped if e.resource_type == "switch"
        ]
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is not None:
            # Deliberate authority shortening: the dropped route points have
            # left the authority, so their whole switch families are released.
            coordinator.release_reservation_tail(
                dropped_blocks, dropped_switches, path_id
            )
        path.resource_sequence = path.resource_sequence[: keep_through + 1]

        nc = self._notification_center
        if nc is not None:
            from notification_center import (
                Kind as _Kind,
                train_ref as _train_ref,
                path_ref as _path_ref,
            )
            from runtime_payloads import ErrorRecord as _ErrorRecord

            nc.emit(
                source="PathDispatcher",
                operation="truncate_path_before_resource",
                kind=_Kind.WARNING,
                message=(
                    f"path {path_id} truncated to end at "
                    f"{path.get_reservation_end_block_id()}: {reason}"
                ),
                entities=[_train_ref(path.train_id), _path_ref(path_id)],
                error=_ErrorRecord(reason, "reservation_truncated"),
                event_time=self._now(),
                payload={
                    "cut_before_resource_type": contested.resource_type,
                    "cut_before_resource_id": contested.resource_id,
                    "reservation_end_block_id": path.get_reservation_end_block_id(),
                    "released_block_ids": list(dropped_blocks),
                    "released_switch_ids": list(dropped_switches),
                },
            )
        return path.get_reservation_end_block_id()

    def _lock_floor_index(self, path) -> Optional[int]:
        """The `resource_sequence` index the cut floor bottoms out at for a
        Reservation truncation of `path` — the path's lock end.

        The truncation cut never enters the locked span, so the furthest a cut
        may pull back is this path's lock end (`Path.lock_end_block_id`, the last
        locked block, which the braking-distance lock guarantees the train can
        stop within). When nothing ahead of the head is locked, the lock end is
        the head's own block, so the cut may pull the reservation back to the
        block the train stands in. When the head's block entry sits behind
        `current_resource_index` (the head index anchors on a switch), the floor
        is the first block ahead of the head. Returns `None` only when no such
        block exists.
        """
        lock_end_id = path.get_lock_end_block_id()
        if lock_end_id:
            for i in range(path.current_resource_index, len(path.resource_sequence)):
                entry = path.resource_sequence[i]
                if entry.resource_type == "block" and entry.resource_id == lock_end_id:
                    return i
        for i in range(path.current_resource_index + 1, len(path.resource_sequence)):
            if path.resource_sequence[i].resource_type == "block":
                return i
        return None

    def _furthest_legal_terminal_index(
        self,
        path,
        start_index: int,
        next_block_after_start: Optional[str],
        floor_index: Optional[int] = None,
    ) -> Optional[int]:
        """The furthest-forward `resource_sequence` block index at or behind
        `start_index` that is a legal authority terminal (design_new.md item-11
        truncation-terminal rules).

        Walks block candidates furthest-forward first and prefers the
        furthest-forward *holding point* that is a legal terminal over any longer
        non-holding-point terminal. The reversal-trap exclusion and the
        never-terminate-on-a-waypoint/run-target rule stay absolute; a holding
        point waives only the junction-fouling exclusion. When no holding point
        qualifies, keeps the longest terminal that also clears the two-way-switch
        foul. `next_block_after_start` is the first block that would be dropped
        just after `start_index` (the reversal-trap check's "next block").

        Candidate terminals are strictly ahead of the train's head resource
        (`current_resource_index`). `floor_index`, when given (Reservation
        truncation's lock-end floor), stops the scan there inclusive so the cut
        never pulls back past the lock; otherwise the scan stops one short of the
        head. `None` means no legal terminal remains in the scanned range.
        """
        route_executor = self._get_route_executor()
        excluded_terminal_targets = (
            route_executor.waypoint_and_run_target_blocks(path.train_id)
            if route_executor is not None
            and hasattr(route_executor, "waypoint_and_run_target_blocks")
            else set()
        )
        # The scan is inclusive of `floor_index` (the lock end is itself a legal
        # terminal), else exclusive of the head.
        scan_stop = (
            floor_index - 1
            if floor_index is not None
            else path.current_resource_index
        )
        keep_through: Optional[int] = None
        fallback_through: Optional[int] = None
        next_block_after_terminal: Optional[str] = next_block_after_start
        for index in range(start_index, scan_stop, -1):
            entry = path.resource_sequence[index]
            if entry.resource_type != "block":
                continue
            terminal_block_id = entry.resource_id
            trapped = next_block_after_terminal is not None and (
                self._terminal_traps_reversing_occupant(
                    next_block_id=next_block_after_terminal,
                    terminal_block_id=terminal_block_id,
                    train_id=path.train_id,
                )
            )
            next_block_after_terminal = terminal_block_id
            if trapped or terminal_block_id in excluded_terminal_targets:
                continue
            if self._block_is_holding_point(terminal_block_id):
                keep_through = index
                break
            if fallback_through is None and not self._resource_seq_terminal_fouls_switch(
                path, index, terminal_block_id
            ):
                fallback_through = index
        return keep_through if keep_through is not None else fallback_through

    # ========================================================================
    # Resource acquisition helpers (v1)
    # ========================================================================

    def _acquire_segment_resources(
        self,
        segment: PathSegment,
        path_id: str,
        train_id: str,
        direction: str,
        manual_override: bool = False,
        ordered_walk: Optional[List[GraphWalkStep]] = None,
    ) -> Tuple[bool, str, Optional[str], List[SwitchClaimKey]]:
        """Reserve all blocks + lock all switches in a PathSegment via the
        ResourceCoordinator facade.

        `manual_override` (default False) is propagated from the calling
        `create_path(...)` / `extend_path(...)` per the `disabled` and `manual`
        availability flags canonical home in design_new.md (under TrackResource).
        `ordered_walk` (sliced to ``segment.tiles`` by the caller) supplies the
        per-block traversal direction without re-deriving it from flat tiles.

        Returns (success, failure_reason, failed_resource_id,
        inserted_switch_claim_keys). The claim keys are the exact switch holds
        this call newly acquired; a caller that later rolls the segment back
        hands them to `_rollback_segment(...)` so only the probe's own new holds
        are released. On failure the list is empty — `reserve_segment(...)` has
        already released its own journal — so a failed-probe rollback releases
        nothing and cannot over-release a lock the path still holds.
        """
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is None:
            return (False, "no_resource_coordinator", None, [])

        # Compute traversal directions per block from the ordered walk (or, as
        # a fallback, from path tiles).
        block_traversals = self._compute_block_traversals(segment, ordered_walk)

        # Skip the first block in the segment if it's the train's current
        # block (which the train already physically occupies). The reservation
        # API rejects occupied blocks.
        future_blocks = [
            bid for bid in segment.blocks if not self._train_already_in(train_id, bid)
        ]
        traversal_map = {
            bid: block_traversals[bid]
            for bid in future_blocks
            if bid in block_traversals
        }

        switch_requests: List[SwitchLockRequest] = []
        # Build a map: switch tile xy -> its index in segment.tiles, so we
        # can derive the route (entry_edge, exit_edge) by looking at the
        # neighbouring tiles in path order.
        tile_index_by_xy: Dict[Tuple[int, int], int] = {
            tile: i for i, tile in enumerate(segment.tiles)
        }
        for pos, locked_state in segment.required_switches.items():
            switch_entity = self._resolve_switch_at_position(pos)
            if switch_entity is None:
                continue
            route = self._derive_switch_route(segment, pos, tile_index_by_xy)
            switch_requests.append(
                SwitchLockRequest(
                    switch_id=switch_entity.switch_id,
                    locked_state=int(locked_state),
                    source_switch_id=None,
                    route=route,
                )
            )

        result = coordinator.reserve_segment(
            future_block_ids=future_blocks,
            block_traversals=traversal_map,
            switch_requests=switch_requests,
            path_id=path_id,
            train_id=train_id,
            manual_override=manual_override,
        )
        if not result.success:
            return (
                False,
                result.reason or "reserve_failed",
                result.failed_resource_id,
                [],
            )

        return (True, "", None, list(result.inserted_switch_claim_keys))

    def _derive_switch_route(
        self,
        segment: PathSegment,
        switch_pos: Tuple[int, int],
        tile_index_by_xy: Dict[Tuple[int, int], int],
    ) -> Optional[frozenset]:
        """Return the unordered route key the path takes through the switch
        tile at ``switch_pos``, as ``frozenset({entry_edge, exit_edge})``.

        Derived from the neighbouring tiles in ``segment.tiles``:
          - entry_edge = the switch tile's edge facing the previous tile
          - exit_edge  = the switch tile's edge facing the next tile

        Returns None when the switch tile has no neighbour on one side
        within the segment (path terminates at the switch, which is
        already forbidden by the block-terminated-authority rule but
        defended for callers that pass an irregular segment).
        """
        idx = tile_index_by_xy.get(switch_pos)
        if idx is None:
            return None
        x, y = switch_pos
        # Entry edge: face of the switch tile toward the previous tile.
        entry_edge: Optional[str] = None
        if idx > 0:
            px, py = segment.tiles[idx - 1]
            # direction_from_delta(dx, dy) returns the cardinal heading
            # from prev → curr; the switch tile's entry edge is the
            # opposite of that heading.
            heading_in = direction_from_delta(x - px, y - py)
            if heading_in:
                entry_edge = OPPOSITE_EDGE.get(heading_in)
        # Exit edge: face of the switch tile toward the next tile.
        exit_edge: Optional[str] = None
        if idx + 1 < len(segment.tiles):
            nx, ny = segment.tiles[idx + 1]
            heading_out = direction_from_delta(nx - x, ny - y)
            if heading_out:
                exit_edge = heading_out
        if entry_edge is None or exit_edge is None:
            return None
        return frozenset({entry_edge, exit_edge})

    def _train_already_in(self, train_id: str, block_id: str) -> bool:
        # The train's currently-occupied blocks come from the controller-owned
        # span (design_new.md: physical occupancy lives on
        # `TrainController.physics_sync.current_span.occupied_blocks`).
        ctrl = self._get_controller(train_id)
        if ctrl is None:
            return False
        span = ctrl.get_current_span()
        if span is None:
            return False
        return block_id in (span.occupied_blocks or [])

    def _blocks_held_by(self, path: "Path") -> Set[str]:
        """The blocks this path holds *right now*.

        `Path.resource_sequence` is a history: it keeps every block the path
        has ever authorized, including ones the train has already rolled
        through and let go of. A block leaves the path's live holdings in two
        steps — head entry consumes the future reservation (`reserved_by_path`
        cleared) and tail vacate clears `occupied_by`
        (`BlockManager.on_block_tail_vacated()`), after which the block goes
        CAUTION and then FREE.

        The repeated-resource rule exists to stop one path holding one block
        twice at the same time, so it is tested against this live set rather
        than against the history. On a circular route that is what lets a lap
        close: by the time the route walk wraps back to the first block, the
        train has long since released it, so authorizing it again is an
        ordinary acquisition rather than a double claim.
        """
        train_id = path.train_id
        held: Set[str] = set()
        for block_id in path.get_all_blocks():
            block = self.network.blocks.get(block_id)
            if block is None:
                continue
            if getattr(block, "reserved_by_path", None) == path.path_id:
                held.add(block_id)
            elif train_id and getattr(block, "occupied_by", None) == train_id:
                held.add(block_id)
        return held

    def _switches_held_by(self, path: "Path") -> Set[str]:
        """The switches this path holds *right now*, by live claim.

        Per design_new.md "Path invariants" (What counts as held is the live
        claim, not the traversal record): a switch is held while a live claim on
        it names this path — a `Switch.locks` entry keyed on this `path_id`, or a
        `Switch.physical_occupants` (`route_owners`) / `train_protection_claims`
        entry owned by this path's train. Head entry consumes this path's main
        switch lock and converts flank locks to train-owned protection; switch
        tail vacate removes both, after which the switch carries no claim from
        this path and a lap may re-acquire it. Computing the repeated-resource
        set from `resource_sequence` history instead would wrongly reject a
        `CIRCULAR` suffix that re-crosses a switch the train has already
        vacated.
        """
        path_id = path.path_id
        train_id = path.train_id
        held: Set[str] = set()
        switches = getattr(self.network, "switches", None)
        if not isinstance(switches, dict):
            return held
        for switch_id, switch in switches.items():
            locks = getattr(switch, "locks", None) or {}
            if any(key[0] == path_id for key in locks):
                held.add(switch_id)
                continue
            if train_id:
                owners = getattr(switch, "route_owners", None) or {}
                if train_id in owners.values():
                    held.add(switch_id)
                    continue
                protection = getattr(switch, "train_protection_claims", None) or []
                if any(claim[0] == train_id for claim in protection):
                    held.add(switch_id)
        return held

    def _blocks_still_reserved_by(
        self, block_ids: List[str], path_id: str
    ) -> List[str]:
        """Subset of `block_ids` whose future reservation `path_id` still holds.

        Per the `release_path_block_reservations(...)` contract in design_new.md
        "BlockManager", that method is meant
        to release a path's *not-yet-entered* reservation set, and it emits the
        "not reserved for <path_id>" diagnostic for any block whose
        `reserved_by_path` no longer matches. A block leaves the path's future
        set in two normal ways: a transactionally-failed segment acquire never
        took ownership of it (`reserve_blocks_for_path` commits all-or-nothing;
        `reserve_segment` rolls back its own switch locks on block failure), and
        head-entry consumes the reservation as the train physically enters
        (`reserved_by_path` cleared). Handing those blocks to the release call
        anyway just fires the diagnostic — every Plan tick for an incremental acquisition
        path that keeps retrying `extend_path(...)`, and once per block at
        completion/cancellation. Filtering here keeps the diagnostic meaningful
        for genuine cross-path mismatches while removing the routine churn.
        """
        return [
            bid
            for bid in block_ids
            if getattr(self.network.blocks.get(bid), "reserved_by_path", None)
            == path_id
        ]

    def _rollback_segment(
        self,
        segment: PathSegment,
        path_id: str,
        inserted_switch_claim_keys: List[SwitchClaimKey],
    ) -> None:
        """Roll back a failed (or rejected) acquisition probe via the
        ResourceCoordinator facade, releasing only what the probe newly placed.

        `inserted_switch_claim_keys` are the exact switch holds this probe's
        `_acquire_segment_resources(...)` reported acquiring; they go to
        `ResourceCoordinator.rollback_probe(...)`, which releases only those
        keys. A switch the probe merely re-touched over crossover topology — one
        the path legitimately still holds — is never in the list, so it is left
        intact. This is why the rollback uses the probe's own journal rather
        than a whole-family `release_segment_locks(...)`: whole-family release
        would drop a re-touched lock the path still holds. On a failed acquire
        the list is empty (the coordinator already released its own journal), so
        the switch side is a clean no-op.

        Only blocks this path still owns are handed to the coordinator (see
        `_blocks_still_reserved_by`). Resources already committed to this path's
        live `resource_sequence` are excluded: a blocked incremental acquisition
        `extend_path(...)` re-walks the seam block — the current authority-end,
        already reserved and already in `resource_sequence` — inside its
        extension segment; without this guard the rollback would release that
        seam's reservation while it stays in the authority, leaving a
        not-yet-entered authority block unreserved. The completion/cancellation
        release rules in design_new.md "Path And PathDispatcher" require a
        blocked extend to keep the live path unchanged, and the dispatcher owns
        the reservation for every not-yet-entered authority resource. For
        `create_path(...)` the path record is not yet in `paths_by_id` when its
        acquire-failure rollback runs, so the exclusion set is empty.
        """
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is None:
            return
        committed_resource_ids: Set[str] = set()
        live_path = self.paths_by_id.get(path_id)
        if live_path is not None:
            committed_resource_ids = {
                entry.resource_id for entry in live_path.resource_sequence
            }
        block_ids = [
            bid
            for bid in self._blocks_still_reserved_by(segment.blocks, path_id)
            if bid not in committed_resource_ids
        ]
        coordinator.rollback_probe(
            block_ids=block_ids,
            inserted_switch_claim_keys=inserted_switch_claim_keys,
            path_id=path_id,
        )

    def _compute_truncate_hint(
        self,
        segment: PathSegment,
        errors: List[ValidationError],
    ) -> Optional[int]:
        """Return the prefix length at which incremental acquisition should start the
        backward walk, per design_new.md, "Resource acquisition".

        The index of the first failing occupancy-class block in
        `segment.blocks` is the count of blocks that are still acquirable
        (validation would have flagged any earlier conflict in the same
        pass).

        For occupancy-class errors that carry no `block_id` (e.g.
        `SWITCH_LOCKED` records the failure as a tile position only), find
        the most recent block in `segment.blocks` whose ordered tiles
        appear before the failing switch position in `segment.tiles` and
        use that block's index. This avoids the previous O(N) backward
        walk against a still-locked switch on every incremental acquisition dispatch
        tick.
        """
        candidate_indices: List[int] = []
        for err in errors:
            if not err.type.is_occupancy_class():
                continue
            if err.block_id and err.block_id in segment.blocks:
                candidate_indices.append(segment.blocks.index(err.block_id))
                continue
            if err.position is not None:
                idx = self._position_to_block_index(segment, err.position)
                if idx is not None:
                    candidate_indices.append(idx)
        if not candidate_indices:
            return None
        return min(candidate_indices)

    def _position_to_block_index(
        self,
        segment: PathSegment,
        position: Tuple[int, int],
    ) -> Optional[int]:
        """Return the index in `segment.blocks` of the last block whose
        ordered tile appears strictly before `position` in `segment.tiles`.

        Used by `_compute_truncate_hint` when an occupancy-class error
        carries only a tile position (e.g. `SWITCH_LOCKED`) so the
        incremental acquisition truncate can land on the block before the contested
        switch without walking the full prefix backwards.

        Returns None when the position is not in `segment.tiles` or when
        no earlier block is found (e.g. the failing switch is at the head
        of the segment).
        """
        try:
            switch_tile_idx = segment.tiles.index(position)
        except ValueError:
            return None
        # Walk back through tiles; for each, locate the owning block via
        # the network's track. The most recent block in `segment.blocks`
        # before the switch tile is the truncation hint (block-index = +1
        # of the count we want to acquire — the first one to drop is at
        # that index, so the prefix length is the index itself).
        for tile_idx in range(switch_tile_idx - 1, -1, -1):
            x, y = segment.tiles[tile_idx]
            track = self.network.get_track(x, y)
            if track is None:
                continue
            block_id = getattr(track, "block_id", None)
            if block_id and block_id in segment.blocks:
                # `block_id` is acquirable; the first block to drop is the
                # one AFTER this one in `segment.blocks`. _best_effort_truncate
                # interprets the hint as `start_at_block_count = N` meaning
                # "try the prefix of length N first," so return that count.
                return segment.blocks.index(block_id) + 1
        return None

    def _terminal_traps_reversing_occupant(
        self,
        next_block_id: str,
        terminal_block_id: str,
        train_id: str,
    ) -> bool:
        """True if ending best-effort authority at `terminal_block_id` would
        trap the train occupying `next_block_id`.

        The trap exists only when `next_block_id` is occupied by a *stopped*
        train (one with no live authority of its own — a moving occupant
        vacates forward under its own path) whose next route move leaves
        `next_block_id` through `terminal_block_id`. In that case the follower
        would hold the terminal block to go in while the occupant needs that
        same block to come out, and neither could move. This is the canonical
        reversal-trap exclusion home cross-referenced from design_new.md
        best-effort `create_path()` (item 11).

        Only the occupant's *immediate* exit block is tested, so an occupant
        that leaves `next_block_id` forward (an ordinary platform pass-through)
        never restricts the follower. The occupant holds no live reservation,
        so both physical ends of its span take part in predicting the walk its
        own next `create_path(...)` will choose (see
        `_predict_reservationless_walk`); the immediate exit block is the first
        block outside its span on that walk.
        """
        block = self.network.blocks.get(next_block_id)
        if block is None:
            return False
        occ = block.occupied_by
        if not occ or occ == train_id:
            return False
        # A moving occupant already holds authority and will clear forward.
        if self.get_active_path(occ) is not None:
            return False
        route_executor = self._get_route_executor()
        if route_executor is None:
            return False
        occ_target = route_executor.find_next_move_target(occ)
        if occ_target is None:
            # The occupant has no scheduled next move (no RUNNING run, a
            # route-less parked train, or a ONE_OFF run with no move task left)
            # to reverse through the terminal — a plain parked obstruction the
            # follower waits behind, not a reversal trap.
            return False
        if occ_target == "unresolved":
            # A running occupant whose next move task exists but will not resolve
            # this tick is NOT exempt: a resolution failure does not prove it
            # leaves forward, so bar the terminal conservatively (design_new.md
            # "find_next_move_target" canonical definition, `"unresolved"` is not
            # exempt). This is the same not-exempt outcome as the no-walk case
            # below.
            return True
        occ_span_blocks = self._span_blocks_for(occ) or [next_block_id]
        route_id = route_executor.assigned_route_id_for(occ)
        route = route_executor.get_route(route_id) if route_id else None
        walk = self._predict_reservationless_walk(
            occ,
            occ_span_blocks,
            occ_target,
            manual_override=bool(getattr(route, "manual_override", False)),
        )
        if walk is None:
            # A next-move target was supplied but no candidate walk resolves it.
            # Per design_new.md best-effort `create_path()` item 11 (the
            # reversal-trap exclusion), an occupant whose exit cannot be
            # resolved is treated as unresolved and is NOT exempt: we cannot
            # prove it leaves forward, so the terminal is barred conservatively.
            return True
        in_span = set(occ_span_blocks)
        exit_block = next((b for b in walk.walk.blocks if b not in in_span), None)
        return exit_block == terminal_block_id

    def _span_blocks_for(self, train_id: str) -> List[str]:
        """The train's span blocks in `end_a` -> `end_b` order, or empty."""
        ctrl = self._get_controller(train_id)
        span = ctrl.get_current_span() if ctrl is not None else None
        return list(getattr(span, "occupied_blocks", None) or []) if span else []

    def _predict_reservationless_walk(
        self,
        train_id: str,
        span_blocks: List[str],
        target: "ResolvedRouteTarget",
        manual_override: bool = False,
    ) -> Optional[GraphWalkCandidate]:
        """The walk a train with no live reservation would take to `target`
        (design_new.md "`create_path()` flow", the reversal-trap exclusion and
        Which train ends participate).

        Both physical ends take part: one graph query from each end's inward
        occupied block (the first and last of `span_blocks`, which already skip
        a switch the end may sit on), with the target's direction filter and
        the train's feasibility filters. The two ends' best candidates are
        compared with the cross-query key (`estimated_travel_seconds`,
        `tile_count`, `walk_length_cm`); `end_a` is compared first and replaced
        only on a strictly smaller key, so an exact tie goes to `end_a`.
        Returns None when neither end yields a walk.
        """
        end_blocks: List[str] = []
        for block_id in (span_blocks[0], span_blocks[-1]):
            if block_id and block_id not in end_blocks:
                end_blocks.append(block_id)
        best: Optional[GraphWalkCandidate] = None
        best_key = None
        for end_block in end_blocks:
            candidates = self._find_walk_candidates(
                end_block,
                target.requested_destination_block_id,
                train_id,
                direction_constraint=target.requested_destination_direction,
                manual_override=manual_override,
            )
            if not candidates:
                continue
            key = candidates[0].cross_query_key()
            if best_key is None or key < best_key:
                best_key = key
                best = candidates[0]
        return best

    def _terminal_fouls_two_way_switch_block(
        self, segment: "PathSegment", terminal_block_id: str
    ) -> bool:
        """True if ending truncated best-effort authority at
        `terminal_block_id` would leave the train stopped on a TWO-WAY block
        that the path enters directly off a switch — fouling the junction.

        A block is "one-way" when its `allowed_travel_directions` is restricted
        to a single direction; such a block can never host an opposing
        traversal, so stopping right after the switch is safe and returns
        False. A two-way block entered straight off a switch returns True, so
        `_best_effort_truncate` holds the authority back to a shorter, switch-
        clear prefix. This is the occupancy-independent companion to
        `_terminal_traps_reversing_occupant`.
        """
        block = self.network.blocks.get(terminal_block_id)
        if block is None:
            return False
        # A block no opposing move can reach is safe to stop just inside it,
        # whether it is one-way or in CAUTION behind a leader (design_new.md
        # item 11; canonical home Block.excludes_opposing_traversal). Hold the
        # authority back only for a genuine two-way block entered off a switch.
        if block.excludes_opposing_traversal():
            return False
        # Locate the terminal block's first non-switch tile in path order.
        first_idx: Optional[int] = None
        for idx, tile in enumerate(segment.tiles):
            track = self.network.get_track(*tile)
            if track is None or track.is_switch:
                continue
            if track.block_id == terminal_block_id:
                first_idx = idx
                break
        if first_idx is None:
            return False
        # Foul only if the immediately preceding tile in the path is a switch.
        k = first_idx - 1
        while k >= 0:
            prev = self.network.get_track(*segment.tiles[k])
            if prev is None:
                k -= 1
                continue
            return bool(prev.is_switch)
        return False

    def _block_is_holding_point(self, block_id: str) -> bool:
        """True when ``block_id`` is an operator-marked holding point.

        Reads the static `Block.is_holding_point` config flag (design_new.md
        "Block" field schema). Used only by the best-effort
        truncation holding-point preference (item 11).
        """
        blocks = getattr(self.network, "blocks", None)
        if not blocks:
            return False
        block = blocks.get(block_id)
        return bool(getattr(block, "is_holding_point", False))

    def _resource_seq_terminal_fouls_switch(
        self, path: "Path", terminal_seq_index: int, terminal_block_id: str
    ) -> bool:
        """Two-way-switch foul (design_new.md item 11) evaluated over a live
        path's ``resource_sequence`` rather than a ``PathSegment``.

        A block no opposing move can reach is safe to stop just inside, whether
        it is one-way or in CAUTION behind a leader
        (``Block.excludes_opposing_traversal``). Otherwise the terminal fouls the
        junction when the path enters it directly off a switch — i.e. the
        resource entry immediately before it in the sequence is a switch. This is
        the ``resource_sequence`` companion to
        ``_terminal_fouls_two_way_switch_block``.
        """
        block = self.network.blocks.get(terminal_block_id)
        if block is None:
            return False
        if block.excludes_opposing_traversal():
            return False
        prev_index = terminal_seq_index - 1
        return (
            prev_index >= 0
            and path.resource_sequence[prev_index].resource_type == "switch"
        )

    def _effective_reservation_cap(
        self,
        segment: PathSegment,
        reservation_cap_block_id: Optional[str],
        requested_destination_block_id: str,
    ) -> Optional[str]:
        """The block an acquisition along `segment` may not reserve past: the
        nearer of the meet ceiling and the first holding point past the walk's
        start block (design_new.md "Holding-point chunking").

        The dispatcher treats a holding point as an implicit waypoint terminal,
        so each create or extend reserves at most up to the next holding point,
        even when the track past it is clear. The walk's start block is the
        train's leading block for a create and the current authority end for an
        extend, so it is never its own chunk terminal. A holding point at or
        past the requested target adds no bound. Returns `None` when neither
        bound applies.
        """
        meet_index: Optional[int] = None
        if reservation_cap_block_id and reservation_cap_block_id in segment.blocks:
            meet_index = segment.blocks.index(reservation_cap_block_id)
        for index in range(1, len(segment.blocks)):
            block_id = segment.blocks[index]
            if block_id == requested_destination_block_id:
                break
            if meet_index is not None and index >= meet_index:
                break
            # A lapping walk may pass its own start block again; the start is
            # never its own chunk terminal.
            if block_id != segment.blocks[0] and self._block_is_holding_point(
                block_id
            ):
                return block_id
        return reservation_cap_block_id

    def _truncate_segment_to_cap(
        self,
        segment: PathSegment,
        ordered_walk: Optional[List[GraphWalkStep]],
        reservation_cap_block_id: Optional[str],
        requested_destination_block_id: str,
    ) -> Tuple[str, Optional[PathSegment], Optional[List[GraphWalkStep]]]:
        """Apply the Meet-coordination acquisition cap to a candidate walk.

        Per design_new.md "Reservation-cap rule": when a cap short
        of the requested target lies on this walk, authority must not be stored
        past the cap block. Returns one of:

        - ``("none", segment, ordered_walk)`` — no cap applies (cap unset, cap
          equals the requested target, or the cap is not on this walk); acquire
          toward the requested target normally.
        - ``("hold", None, None)`` — the cap is the walk's start block, so no
          resource may be acquired/appended past it. The caller holds without
          growth (``create_path`` skips the candidate; ``extend_path`` returns
          ``appended_resource_count = 0``).
        - ``("capped", truncated_segment, sliced_ordered_walk)`` — acquire only
          up to the cap block. The truncated segment is fed through the ordinary
          validate/acquire path, so any conflict within the cap truncates it
          further and ``reservation_end_block_id`` lands on the cap (or earlier).

        The cap never changes ``requested_destination_block_id``; the success
        path reports ``reached_target = False`` because the authority is short of
        the requested target.
        """
        if (
            not reservation_cap_block_id
            or reservation_cap_block_id == requested_destination_block_id
        ):
            return ("none", segment, ordered_walk)
        try:
            cap_count = segment.blocks.index(reservation_cap_block_id) + 1
        except ValueError:
            return ("none", segment, ordered_walk)
        if cap_count <= 1:
            return ("hold", None, None)
        if cap_count >= len(segment.blocks):
            # The cap is the walk terminal (the requested target is reached at or
            # before it) — no truncation needed.
            return ("none", segment, ordered_walk)
        prefix_blocks = segment.blocks[:cap_count]
        cutoff_tiles = self._compute_prefix_cutoff_tiles(segment, prefix_blocks)
        cutoff_switches: Dict[Tuple[int, int], int] = {
            pos: st
            for pos, st in segment.required_switches.items()
            if pos in cutoff_tiles
        }
        truncated = PathSegment(
            tiles=cutoff_tiles,
            distance_cm=0.0,  # not recomputed
            required_switches=cutoff_switches,
            blocks=prefix_blocks,
        )
        return (
            "capped",
            truncated,
            self._slice_ordered_walk(ordered_walk, cutoff_tiles),
        )

    def _furthest_legal_cap_terminal_len(
        self,
        segment: PathSegment,
        train_id: str,
        next_block_after_cap: Optional[str] = None,
    ) -> int:
        """The furthest-forward prefix length of a cap-shortened `segment` whose
        terminal is a legal authority terminal under the item-11 truncation-terminal
        rules (design_new.md "`create_path()` flow", item 11): the furthest holding
        point wins; otherwise the longest terminal that clears the reversal-trap
        and junction-fouling exclusions. Returns `0` when no legal terminal exists.

        Pure — it applies the same exclusions as `_best_effort_truncate` without
        acquiring; the caller slices the segment to the returned length. `segment`
        is already sliced to the cap block, so the scan starts at the cap terminal;
        `next_block_after_cap` is the first block the cap dropped, used only for the
        reversal-trap check at the cap terminal itself. Callers invoke this only
        when the train would park at the cap (the block beyond it is not
        acquirable); a cap the train will grow past keeps its terminal untouched.
        """
        route_executor = self._get_route_executor()
        excluded_terminal_targets = (
            route_executor.waypoint_and_run_target_blocks(train_id)
            if route_executor is not None
            and hasattr(route_executor, "waypoint_and_run_target_blocks")
            else set()
        )
        fallback_block_count = 0
        for prefix_block_count in range(len(segment.blocks), 1, -1):
            terminal_block_id = segment.blocks[prefix_block_count - 1]
            if prefix_block_count < len(segment.blocks):
                next_block_id: Optional[str] = segment.blocks[prefix_block_count]
            else:
                next_block_id = next_block_after_cap
            if next_block_id is not None and self._terminal_traps_reversing_occupant(
                next_block_id=next_block_id,
                terminal_block_id=terminal_block_id,
                train_id=train_id,
            ):
                continue
            if terminal_block_id in excluded_terminal_targets:
                continue
            # A holding point overrides the junction-fouling back-off (item 11,
            # Holding-point preference); the furthest-forward one wins.
            if self._block_is_holding_point(terminal_block_id):
                return prefix_block_count
            if self._terminal_fouls_two_way_switch_block(segment, terminal_block_id):
                continue
            if fallback_block_count == 0:
                fallback_block_count = prefix_block_count
        return fallback_block_count

    def _best_effort_truncate(
        self,
        segment: PathSegment,
        path_id: str,
        train_id: str,
        direction: str,
        start_at_block_count: Optional[int] = None,
        manual_override: bool = False,
        ordered_walk: Optional[List[GraphWalkStep]] = None,
    ) -> Optional[PathSegment]:
        """Try to acquire a strictly shorter prefix of `segment`.

        When `start_at_block_count` is provided, start the backward walk at
        that prefix length (capped to `len(segment.blocks) - 1`). Callers
        compute it from the first occupancy-class validation error's index
        per design_new.md, "Resource acquisition" ("use the first failing block/switch as
        the truncation point"). Default `None` preserves the full backward
        walk for callers that have no hint.
        """
        max_prefix = len(segment.blocks) - 1
        if start_at_block_count is None:
            start = max_prefix
        else:
            start = max(2, min(start_at_block_count, max_prefix))
        # Per the route-managed point-to-point planning rule in design_new.md
        # "Path And PathDispatcher": resolve this run's waypoint and run
        # target blocks so the loop never stores a live authority terminating
        # on either kind of target.
        route_executor = self._get_route_executor()
        excluded_terminal_targets = (
            route_executor.waypoint_and_run_target_blocks(train_id)
            if route_executor is not None
            and hasattr(route_executor, "waypoint_and_run_target_blocks")
            else set()
        )
        # Covered-span invariant (design_new.md "`create_path()` flow", item 9): every stored live authority must cross at least one
        # resource boundary beyond the train's covered span — its blocks must
        # include at least one the train does not currently occupy. A prefix
        # whose blocks are all within the covered span (the degenerate reversal
        # onto a tail block the train already sits on) is not a storable
        # authority; excluding it here makes such a request a total acquisition
        # failure (item 11) instead of a one-tick body-internal path. An empty
        # cover (span unavailable) excludes nothing.
        covered_blocks = self._train_covered_blocks(train_id)
        # Terminal-preference (design_new.md "`create_path()` flow", item 11,
        # Holding-point preference): scan the acquirable prefix furthest-forward
        # first. The furthest-forward *holding point* that is a legal terminal
        # wins over any longer non-holding-point prefix, because a holding point
        # is the operator's explicit safe-wait place and overrides the
        # junction-fouling back-off. The reversal-trap exclusion and the
        # never-terminate-on-a-waypoint/run-target rule stay absolute for every
        # terminal, holding points included. When no acquirable holding point
        # qualifies, fall back to the longest acquirable prefix that also clears
        # the two-way-switch foul.
        #
        # Acquisition is monotonic — a prefix of an acquirable walk is itself
        # acquirable — so a probe that acquires the longest non-holding-point
        # fallback guarantees any shorter holding point acquires too; the
        # fallback is remembered by length and re-acquired only if no holding
        # point is found.
        fallback_block_count: Optional[int] = None
        for prefix_block_count in range(start, 1, -1):
            terminal_block_id = segment.blocks[prefix_block_count - 1]
            # Covered-span exclusion for a reverse move (design_new.md "`create_path()` flow", item 9): skip any reverse prefix whose blocks are all within the
            # train's covered span, so a stored reverse authority always reaches
            # at least one block the train does not occupy. This stays absolute for
            # every terminal, holding points included. A forward prefix is not
            # excluded (its head stays covered — an overshot covered stop).
            if (
                direction == "reverse"
                and covered_blocks
                and not (set(segment.blocks[:prefix_block_count]) - covered_blocks)
            ):
                continue
            # When `start` was clamped up to the 2-block floor on a short
            # segment, `prefix_block_count` can equal the block count; nothing
            # is dropped ahead, so the reversal-trap / junction-fouling
            # exclusions (which look at the first dropped block) do not apply.
            drops_a_block = prefix_block_count < len(segment.blocks)
            if drops_a_block:
                next_block_id = segment.blocks[prefix_block_count]
                if self._terminal_traps_reversing_occupant(
                    next_block_id=next_block_id,
                    terminal_block_id=terminal_block_id,
                    train_id=train_id,
                ):
                    continue
            if terminal_block_id in excluded_terminal_targets:
                continue
            is_holding_point = self._block_is_holding_point(terminal_block_id)
            if (
                drops_a_block
                and not is_holding_point
                and self._terminal_fouls_two_way_switch_block(
                    segment, terminal_block_id
                )
            ):
                continue
            truncated, cutoff_tiles = self._make_prefix_segment(
                segment, prefix_block_count
            )
            ok, _reason, _, inserted_switch_claim_keys = (
                self._acquire_segment_resources(
                    truncated, path_id, train_id, direction,
                    manual_override=manual_override,
                    ordered_walk=self._slice_ordered_walk(
                        ordered_walk, cutoff_tiles
                    ),
                )
            )
            if not ok:
                self._rollback_segment(
                    truncated, path_id, inserted_switch_claim_keys
                )
                continue
            if is_holding_point:
                # Furthest-forward acquirable holding point — the preferred
                # terminal; store it.
                return truncated
            # Longest acquirable non-holding-point terminal: remember it and
            # keep scanning shorter for a holding point, which by monotonicity
            # will also acquire if one exists.
            self._rollback_segment(truncated, path_id, inserted_switch_claim_keys)
            if fallback_block_count is None:
                fallback_block_count = prefix_block_count

        if fallback_block_count is not None:
            truncated, cutoff_tiles = self._make_prefix_segment(
                segment, fallback_block_count
            )
            ok, _reason, _, inserted_switch_claim_keys = (
                self._acquire_segment_resources(
                    truncated, path_id, train_id, direction,
                    manual_override=manual_override,
                    ordered_walk=self._slice_ordered_walk(
                        ordered_walk, cutoff_tiles
                    ),
                )
            )
            if ok:
                return truncated
            self._rollback_segment(truncated, path_id, inserted_switch_claim_keys)
        return None

    def _make_prefix_segment(
        self, segment: PathSegment, prefix_block_count: int
    ) -> Tuple[PathSegment, List[Tuple[int, int]]]:
        """Build the block-terminated prefix `PathSegment` of `segment` keeping
        its first `prefix_block_count` blocks, and return it with its cutoff
        tile list (for slicing the ordered walk)."""
        prefix_blocks = segment.blocks[:prefix_block_count]
        cutoff_tiles = self._compute_prefix_cutoff_tiles(segment, prefix_blocks)
        cutoff_switches: Dict[Tuple[int, int], int] = {
            pos: st
            for pos, st in segment.required_switches.items()
            if pos in cutoff_tiles
        }
        return (
            PathSegment(
                tiles=cutoff_tiles,
                distance_cm=0.0,  # not recomputed
                required_switches=cutoff_switches,
                blocks=prefix_blocks,
            ),
            cutoff_tiles,
        )

    def _compute_prefix_cutoff_tiles(
        self, segment: PathSegment, prefix_blocks: List[str]
    ) -> List[Tuple[int, int]]:
        """Tiles of `segment` that fall inside `prefix_blocks`, in path order.

        A block tile is kept while its `block_id` is in
        `prefix_blocks`. A switch tile is kept only when the next non-switch
        tile in path order belongs to a kept block — i.e. the switch sits
        inside the prefix, not at the boundary to a dropped block. Per
        design_new.md, "Resource acquisition" the stored live authority is always
        block-terminated, and per "Resource acquisition" truncation must land "before that
        failed switch", so a boundary switch must not appear in the truncated
        segment.
        """
        cutoff_tiles: List[Tuple[int, int]] = []
        n_tiles = len(segment.tiles)
        i = 0
        while i < n_tiles:
            tile = segment.tiles[i]
            track = self.network.get_track(*tile)
            if track is None:
                i += 1
                continue
            if track.is_switch:
                # Look ahead to the next non-switch tile; include this
                # switch only if that tile belongs to a kept block.
                next_in_prefix = False
                j = i + 1
                while j < n_tiles:
                    next_track = self.network.get_track(*segment.tiles[j])
                    if next_track is None:
                        j += 1
                        continue
                    if next_track.is_switch:
                        j += 1
                        continue
                    next_in_prefix = (
                        next_track.block_id in prefix_blocks
                    )
                    break
                if next_in_prefix:
                    cutoff_tiles.append(tile)
                    i += 1
                    continue
                # Boundary switch to a dropped block: truncate here.
                break
            if track.block_id in prefix_blocks:
                cutoff_tiles.append(tile)
                i += 1
            else:
                # Stop at first tile of an excluded block.
                break
        return cutoff_tiles

    def _compute_block_traversals(
        self,
        segment: PathSegment,
        ordered_walk: Optional[List[GraphWalkStep]] = None,
    ) -> Dict[str, ReservedBlockTraversal]:
        """Compute (exit_edge, block_direction) for each block in segment.

        Per design_new.md every block in the path's authority — including the
        destination block — needs a `ReservedBlockTraversal` entry; the block
        manager's `reserve_blocks_for_path(...)` rejects with
        `missing_direction` otherwise.

        When ``ordered_walk`` is provided, both values come from the precomputed
        per-step geometry: `exit_edge` is the block's first step exit edge and
        `block_direction` is the canonical `Block.ordered_tiles` direction
        (`_build_ordered_walk` / `_terminal_direction` convention). Otherwise
        they are re-derived from tile deltas (the fallback; for tiles 0..N-2 the
        exit_edge is the direction-of-travel toward the next tile, and for the
        final tile from the previous step).
        """
        if ordered_walk:
            return self._ordered_walk_block_traversals(ordered_walk)
        result: Dict[str, ReservedBlockTraversal] = {}
        if not segment.tiles or len(segment.tiles) < 2:
            return result
        n = len(segment.tiles)
        for i in range(n):
            curr = segment.tiles[i]
            if i < n - 1:
                nxt = segment.tiles[i + 1]
                dx = nxt[0] - curr[0]
                dy = nxt[1] - curr[1]
            else:
                # Final tile: continue the direction-of-travel from prev → curr.
                prev = segment.tiles[i - 1]
                dx = curr[0] - prev[0]
                dy = curr[1] - prev[1]
            track = self.network.get_track(*curr)
            if track is None or not track.block_id:
                continue
            block_id = track.block_id
            if block_id in result:
                continue
            exit_edge = direction_from_delta(dx, dy)
            if exit_edge is None:
                continue
            # block_direction ("forward"/"reverse") from Block.ordered_tiles:
            # multi-tile by which endpoint the run enters (enter at index-0 tile
            # => forward), single-tile by the entry edge vs the forward-entry
            # anchor (design_new.md "Block model" — a single tile can be
            # traversed in either direction). Matches the ordered-walk path's
            # RailwayPathfinder._block_run_direction convention.
            block = self.network.blocks.get(block_id)
            block_direction = "forward"
            ordered = (block.ordered_tiles or []) if block is not None else []
            if ordered:
                first = (ordered[0].x, ordered[0].y)
                last = (ordered[-1].x, ordered[-1].y)
                if first == last:
                    resolved = block.block_direction_for_compass_exit(exit_edge)
                    if resolved is not None:
                        block_direction = resolved
                elif curr == last:
                    block_direction = "reverse"
            result[block_id] = ReservedBlockTraversal(
                exit_edge=exit_edge,
                block_direction=block_direction,
            )
        return result

    def _ordered_walk_block_traversals(
        self, ordered_walk: List[GraphWalkStep]
    ) -> Dict[str, ReservedBlockTraversal]:
        """Per-block `ReservedBlockTraversal` read from the ordered walk.

        For each block's first run: `exit_edge` is the run's first step exit
        edge (the travel direction entering the block, matching the legacy
        first-tile derivation) and `block_direction` is the canonical
        `Block.ordered_tiles` direction carried on the step (defaulting to
        ``forward`` when geometry cannot decide, as the legacy path did).
        """
        result: Dict[str, ReservedBlockTraversal] = {}
        i = 0
        n = len(ordered_walk)
        while i < n:
            step = ordered_walk[i]
            if step.resource_type != "block" or not step.block_id:
                i += 1
                continue
            block_id = step.block_id
            run_first = step
            j = i
            while (
                j < n
                and ordered_walk[j].resource_type == "block"
                and ordered_walk[j].block_id == block_id
            ):
                j += 1
            i = j
            if block_id in result:
                continue
            result[block_id] = ReservedBlockTraversal(
                exit_edge=run_first.exit_edge,
                block_direction=run_first.block_direction or "forward",
            )
        return result

    def _resolve_switch_at_position(self, pos: Tuple[int, int]):
        """Resolve a Switch entity by its tile position."""
        track = self.network.get_track(*pos)
        if track is None:
            return None
        switch_id = getattr(track, "switch_id", None)
        if switch_id and hasattr(self.network, "switches"):
            switches = self.network.switches
            if isinstance(switches, dict):
                ent = switches.get(switch_id)
                if ent is not None:
                    return ent
        # Fallback: find by (x, y).
        if hasattr(self.network, "switches") and isinstance(
            self.network.switches, dict
        ):
            for s in self.network.switches.values():
                if (s.x, s.y) == pos:
                    return s
        return None

    def _segment_to_resource_sequence(
        self,
        segment: PathSegment,
        base_index: int,
        skip_first_block: bool = False,
        ordered_walk: Optional[List[GraphWalkStep]] = None,
    ) -> List[ResourceSequenceEntry]:
        """Convert a PathSegment into a ResourceSequenceEntry list.

        When ``ordered_walk`` (the chosen candidate's per-step geometry) is
        provided, the per-tile entry/exit edges and the block/switch grouping
        are read from the precomputed steps -- tunnel-aware and built once at
        cache time -- instead of being re-derived from tile deltas. The flat
        tile-walk below is kept as the fallback for segments that have no
        candidate geometry (e.g. legacy callers).
        """
        if ordered_walk:
            return self._ordered_walk_to_resource_sequence(
                ordered_walk, base_index, skip_first_block
            )
        entries: List[ResourceSequenceEntry] = []
        # Walk segment.tiles, grouping by resource (block or switch).
        if not segment.tiles:
            return entries

        seq_index = base_index
        i = 0
        seen_first_block = not skip_first_block
        while i < len(segment.tiles):
            tile = segment.tiles[i]
            track = self.network.get_track(*tile)
            if track is None:
                i += 1
                continue
            if track.is_switch:
                # Single tile resource.
                entry_edge = self._tile_entry_edge(segment.tiles, i)
                exit_edge = self._tile_exit_edge(segment.tiles, i)
                switch_entity = self._resolve_switch_at_position(tile)
                resource_id = (
                    getattr(switch_entity, "switch_id", None)
                    or getattr(track, "switch_id", None)
                    or f"{tile[0]},{tile[1]}"
                )
                tiles_for_entry = [
                    PathTileEntry(
                        x=tile[0],
                        y=tile[1],
                        entry_edge=entry_edge or "",
                        exit_edge=exit_edge or "",
                        resource_sequence_index=seq_index,
                    )
                ]
                entries.append(
                    ResourceSequenceEntry(
                        sequence_index=seq_index,
                        resource_type="switch",
                        resource_id=resource_id,
                        path_tiles=tiles_for_entry,
                        entry_edge=entry_edge or "",
                        exit_edge=exit_edge or "",
                    )
                )
                seq_index += 1
                i += 1
                continue

            # Block tiles: collect contiguous run with same block id.
            block_id = track.block_id
            if not block_id:
                i += 1
                continue
            if not seen_first_block:
                # Skip the first block tile run (already in caller's path).
                while i < len(segment.tiles):
                    tile2 = segment.tiles[i]
                    track2 = self.network.get_track(*tile2)
                    if (
                        track2 is None
                        or track2.is_switch
                        or track2.block_id != block_id
                    ):
                        break
                    i += 1
                seen_first_block = True
                continue

            block_tiles: List[PathTileEntry] = []
            entry_edge_first: Optional[str] = None
            exit_edge_last: Optional[str] = None
            while i < len(segment.tiles):
                tile2 = segment.tiles[i]
                track2 = self.network.get_track(*tile2)
                if track2 is None or track2.is_switch:
                    break
                if track2.block_id != block_id:
                    break
                ee = self._tile_entry_edge(segment.tiles, i)
                xe = self._tile_exit_edge(segment.tiles, i)
                if entry_edge_first is None:
                    entry_edge_first = ee
                exit_edge_last = xe
                block_tiles.append(
                    PathTileEntry(
                        x=tile2[0],
                        y=tile2[1],
                        entry_edge=ee or "",
                        exit_edge=xe or "",
                        resource_sequence_index=seq_index,
                    )
                )
                i += 1
            if block_tiles:
                entries.append(
                    ResourceSequenceEntry(
                        sequence_index=seq_index,
                        resource_type="block",
                        resource_id=block_id,
                        path_tiles=block_tiles,
                        entry_edge=entry_edge_first or "",
                        exit_edge=exit_edge_last or "",
                    )
                )
                seq_index += 1
        return entries

    def _ordered_walk_to_resource_sequence(
        self,
        ordered_walk: List[GraphWalkStep],
        base_index: int,
        skip_first_block: bool,
    ) -> List[ResourceSequenceEntry]:
        """Build resource_sequence entries directly from the per-step
        ``ordered_walk``.

        Switch steps are singletons; consecutive same-block steps form one block
        entry. Mirrors the flat-tile builder's grouping, switch-id resolution,
        and ``skip_first_block`` semantics (skip the first BLOCK run, which is
        already in the caller's path), sourcing all edges from the steps.
        """
        entries: List[ResourceSequenceEntry] = []
        seq_index = base_index
        i = 0
        n = len(ordered_walk)
        seen_first_block = not skip_first_block
        while i < n:
            step = ordered_walk[i]
            if step.resource_type == "switch":
                resource_id = self._switch_resource_id(step.tile)
                tile_entry = PathTileEntry(
                    x=step.tile[0],
                    y=step.tile[1],
                    entry_edge=step.entry_edge or "",
                    exit_edge=step.exit_edge or "",
                    resource_sequence_index=seq_index,
                )
                entries.append(
                    ResourceSequenceEntry(
                        sequence_index=seq_index,
                        resource_type="switch",
                        resource_id=resource_id,
                        path_tiles=[tile_entry],
                        entry_edge=step.entry_edge or "",
                        exit_edge=step.exit_edge or "",
                    )
                )
                seq_index += 1
                i += 1
                continue

            block_id = step.block_id
            if not block_id:
                i += 1
                continue
            # Collect the contiguous run of steps in this block.
            j = i
            while (
                j < n
                and ordered_walk[j].resource_type == "block"
                and ordered_walk[j].block_id == block_id
            ):
                j += 1
            run = ordered_walk[i:j]
            i = j
            if not seen_first_block:
                # Skip the first block run (already present as authority_end).
                seen_first_block = True
                continue
            path_tiles = [
                PathTileEntry(
                    x=s.tile[0],
                    y=s.tile[1],
                    entry_edge=s.entry_edge or "",
                    exit_edge=s.exit_edge or "",
                    resource_sequence_index=seq_index,
                )
                for s in run
            ]
            entries.append(
                ResourceSequenceEntry(
                    sequence_index=seq_index,
                    resource_type="block",
                    resource_id=block_id,
                    path_tiles=path_tiles,
                    entry_edge=run[0].entry_edge or "",
                    exit_edge=run[-1].exit_edge or "",
                )
            )
            seq_index += 1
        return entries

    def _switch_resource_id(self, tile: Tuple[int, int]) -> str:
        """Resolve the switch resource_id at ``tile``, matching the flat-tile
        builder's resolution (switch entity id, then track switch_id, then the
        ``x,y`` fallback)."""
        switch_entity = self._resolve_switch_at_position(tile)
        track = self.network.get_track(*tile)
        return (
            getattr(switch_entity, "switch_id", None)
            or getattr(track, "switch_id", None)
            or f"{tile[0]},{tile[1]}"
        )

    def _slice_ordered_walk(
        self,
        ordered_walk: Optional[List[GraphWalkStep]],
        tiles: List[Tuple[int, int]],
    ) -> Optional[List[GraphWalkStep]]:
        """Restrict ``ordered_walk`` to the steps whose tile is in ``tiles``,
        preserving order.

        For a full segment this returns the whole list; for an incremental acquisition
        truncated segment (whose tiles are a contiguous prefix of the walk) it
        returns the matching prefix. Returns None when there is no candidate
        geometry, so callers fall back to the flat-tile builder.
        """
        if not ordered_walk:
            return None
        tileset = set(tiles)
        return [s for s in ordered_walk if s.tile in tileset]

    def _tile_entry_edge(
        self, tiles: List[Tuple[int, int]], i: int
    ) -> Optional[str]:
        if i == 0:
            return None
        prev = tiles[i - 1]
        curr = tiles[i]
        dx = curr[0] - prev[0]
        dy = curr[1] - prev[1]
        d = direction_from_delta(dx, dy)
        if d is None:
            return None
        return OPPOSITE_DIRECTIONS.get(d)

    def _tile_exit_edge(
        self, tiles: List[Tuple[int, int]], i: int
    ) -> Optional[str]:
        if i + 1 < len(tiles):
            curr = tiles[i]
            nxt = tiles[i + 1]
            dx = nxt[0] - curr[0]
            dy = nxt[1] - curr[1]
            return direction_from_delta(dx, dy)
        # Terminal tile fallback: the path ends here, so there's no next
        # tile to derive the exit direction from. The forward traversal
        # direction is still well-defined — it's the opposite of how the
        # train entered this tile. `PathDispatcher.try_complete_active_path(...)`
        # consults this exit edge to verify the terminal authority direction
        # matches the requested terminal direction the run recorded (the run's
        # `resolved_target`); leaving it empty would make completion impossible.
        if i > 0:
            entry = self._tile_entry_edge(tiles, i)
            if entry is not None:
                return OPPOSITE_DIRECTIONS.get(entry)
        return None

    def _build_path_from_segment(
        self,
        path_id: str,
        train_id: str,
        segment: PathSegment,
        now: float,
        ordered_walk: Optional[List[GraphWalkStep]] = None,
        zero_reservation_end_standoff: bool = False,
    ) -> Path:
        """Construct a v1 Path entity from a successfully acquired PathSegment.

        ``ordered_walk`` (sliced to ``segment.tiles`` by the caller) carries the
        per-step geometry used to build ``resource_sequence`` without re-deriving
        edges from flat tiles. The requested target the segment was acquired
        toward is route-owned and not stored on Path.
        """
        sequence = self._segment_to_resource_sequence(
            segment, base_index=0, skip_first_block=False, ordered_walk=ordered_walk
        )
        # `path_tiles` is a derived `@property` over `resource_sequence`; do
        # not pass it as a constructor field. reservation_end_block_id is also
        # derived from resource_sequence[-1].
        path = Path(
            path_id=path_id,
            status=PathStatus.ACTIVE,
            train_id=train_id,
            resource_sequence=sequence,
            current_resource_index=0,
            last_status_updated_time=now,
            zero_reservation_end_standoff=zero_reservation_end_standoff,
            # Wire the network so Path.get_lock_end_block_id() can read
            # per-block Block.locked_by_path (design_new.md "Path field schema").
            network=self.network,
        )
        return path

    # ========================================================================
    # Observe progress consumer
    # ========================================================================

    def on_controller_progress(self, report: ControllerProgressReport) -> None:
        """Observe progress consumer: commit occupancy (block entry / tail-vacate),
        advance the path head index, and record each mapped head-entry on the
        controller's `physics_sync.entered_blocks_this_tick` for Plan settlement.
        Finishes no route task itself (design_new.md "Observe finishes no route
        task"); the head-advance callback and stop-arrival run in Plan settlement."""
        if report is None:
            return
        train_id = report.train_id
        if not train_id:
            return

        active_path = self.get_active_path(train_id)

        # design_new.md fix #3: stop processing entered_resources on first fatal conflict.
        fatal_in_this_report = False
        for event in list(report.entered_resources or []):
            clean = self._record_clean_physical_entry(event, train_id)
            if not clean:
                fatal_in_this_report = True
                break

        if report.deviation:
            if active_path is not None:
                # design_new.md step `389a`: movement-derived
                # deviation LATCHES the cancel instead of committing it inline.
                # Writing the controller's `set_path_cancel_request` leaves the
                # live path ACTIVE with its not-yet-entered reservations and
                # switch locks, so a still-rolling deviated train keeps consuming
                # its own locks cleanly at each entry instead of releasing future
                # resources ahead of itself. The canonical lookahead reports
                # `atc_limit = 0` with `atc_limit_reason_code = "deviation"`, so
                # ATC brakes the train and the overspeed branch escalates to EB;
                # the gated Action explicit-cancel step (`7b40`) commits the
                # latch through `cancel_path(...)` once `is_stopped()` is true,
                # carrying the report's own `ErrorRecord` forward.
                controller = self._get_controller(train_id)
                if controller is not None:
                    # A recoverable problem the runtime acts on, so `"warning"`
                    # per design_new.md "Error events". The writer's own
                    # `ErrorRecord` (origin `"out_of_sequence"`) rides the
                    # payload into the Action commit.
                    controller.route_hooks.phase_latches.set_path_cancel_request(
                        report.deviation_error
                        or ErrorRecord("head left resource_sequence", "out_of_sequence"),
                        "warning",
                        deviation=True,
                    )
            # design_new.md fix #2: do NOT return here — fall through so that
            # tail_vacated_resources for already-occupied resources still fire.

        # design_new.md step `640a`: apply ordinary active-path head progress and
        # run the route head-advance callback for entered blocks, but ONLY for a
        # report that carries a movement-derived head mapping
        # (`current_head_sequence_index is not None`) — including ticks where a
        # cancel latch is pending. A detection-only report (Observe step `3e9b`,
        # `current_head_sequence_index = None`) and a `deviation == true` report
        # (head unmappable, also `None`) are the occupancy commit alone and
        # suppress the callback, so a stopped pass or an OFF / hand-pushed
        # detected entry cannot consume a waypoint task (design_new.md step
        # 640a). A fatal conflict (fix #3) also skips this. Within a mapped
        # report, a per-entry `sequence_index is None` advances no path index.
        if (
            not fatal_in_this_report
            and report.current_head_sequence_index is not None
            and active_path is not None
        ):
            for event in list(report.entered_resources or []):
                if event.sequence_index is not None:
                    if event.sequence_index > active_path.current_resource_index:
                        active_path.current_resource_index = event.sequence_index
                # Observe applies the dispatcher-side head-index advance above
                # but finishes no route task: it records the head-entry for this
                # train's Plan settlement, which runs the route head-advance
                # callback (design_new.md step `640a`; "Observe finishes no route
                # task"). The route-side consumption is deferred to
                # `RouteExecutor.tick_train_plan(...)` settlement.
                self._record_head_entry_for_settlement(
                    train_id=train_id,
                    path_id=active_path.path_id,
                    event=event,
                )

        # design_new.md fix #3: skip tail-vacates on fatal conflict.
        if not fatal_in_this_report:
            for event in list(report.tail_vacated_resources or []):
                self._release_tail_vacated_resource(event, train_id)

    def _record_clean_physical_entry(
        self, event: ResourceEvent, train_id: str
    ) -> bool:
        """Route a clean-physical-entry event to ResourceCoordinator.

        Returns True on clean entry, False on fatal conflict.

        The coordinator dispatches to BlockManager.occupy_block for blocks and
        to SwitchManager.on_physical_entry for switches; the latter owns the
        flank-lock conversion and fatal-conflict reporting that previously
        lived inline here.

        Per design_new.md fix #3: callers must break on False and skip
        subsequent callback/tail-vacate processing for the same report.
        """
        if event is None or not event.resource_id:
            return True  # no-op is not fatal
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is None:
            return True  # no coordinator = no conflict detection
        active_path = self.get_active_path(train_id)
        path_id = active_path.path_id if active_path is not None else None
        # For switch events the controller side filled entry_edge +
        # switch_exit_edge on the ResourceEvent; build the unordered route
        # key (frozenset of two edges) for route-keyed occupancy.
        route = None
        if (
            event.resource_type == "switch"
            and event.entry_edge
            and event.switch_exit_edge
        ):
            route = frozenset({event.entry_edge, event.switch_exit_edge})
        return coordinator.on_physical_entry(
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            train_id=train_id,
            path_id=path_id,
            route=route,
        )

    def _record_head_entry_for_settlement(
        self, train_id: str, path_id: str, event: ResourceEvent
    ) -> None:
        """Record one Observe head-entry for this train's Plan settlement.

        Populate the block-relative traversal_direction the head-advance
        callback matches a waypoint against (the head-advance waypoint-match
        rule — block AND direction — in design_new.md
        "PathDispatcher.on_controller_progress"). For a block resource it is
        computed by the canonical block-direction helper from the entered block's
        `resource_sequence` entry exit_edge against `Block.ordered_tiles`; for
        switches it stays None. The controller's ResourceEvent.exit_edge is None
        for entered blocks, so the direction is derived from the dispatcher-owned
        path geometry, not the event.

        Observe does not run the route callback (design_new.md "Observe finishes
        no route task"): the `(path_id, event)` pair is appended to the
        controller's `physics_sync.entered_blocks_this_tick`, and Plan settlement
        replays `RouteExecutor.handle_train_advanced(...)` over it in order.
        """
        if event.resource_type == "block" and event.traversal_direction is None:
            event.traversal_direction = self._block_traversal_direction(
                path_id, event
            )
        controller = self._get_controller(train_id)
        if controller is None:
            return
        controller.physics_sync.entered_blocks_this_tick.append((path_id, event))

    def _block_traversal_direction(
        self, path_id: str, event: ResourceEvent
    ) -> Optional[str]:
        """Block-relative "forward"/"reverse" for an entered block event.

        Resolves the block's `resource_sequence` entry on the active path
        (by `sequence_index` when present, else by `resource_id`), then maps
        that entry's compass `exit_edge` to a block-relative direction via the
        canonical `Block.block_direction_for_compass_exit(...)`. Returns None
        when the entry, block, or geometry cannot be resolved.
        """
        path = self.paths.get(path_id)
        if path is None:
            return None
        entry = None
        if event.sequence_index is not None:
            for e in path.resource_sequence:
                if e.sequence_index == event.sequence_index:
                    entry = e
                    break
        if entry is None:
            for e in path.resource_sequence:
                if e.resource_type == "block" and e.resource_id == event.resource_id:
                    entry = e
                    break
        if entry is None or not entry.exit_edge:
            return None
        blocks = getattr(self.network, "blocks", None)
        block = blocks.get(event.resource_id) if blocks else None
        if block is None:
            return None
        helper = getattr(block, "block_direction_for_compass_exit", None)
        if not callable(helper):
            return None
        return helper(entry.exit_edge)

    def _release_tail_vacated_resource(
        self, event: ResourceEvent, train_id: str
    ) -> None:
        """Route a tail-vacate event to ResourceCoordinator.

        For switch events, builds the route key the train used (from the
        vacating tile's entry_edge / switch_exit_edge) so the right
        ``route_owners`` entry gets cleared. Block events forward
        ``direction`` (the vacated block's forward exit edge) as before.
        """
        if event is None or not event.resource_id:
            return
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is None:
            return
        route = None
        if (
            event.resource_type == "switch"
            and event.entry_edge
            and event.switch_exit_edge
        ):
            route = frozenset({event.entry_edge, event.switch_exit_edge})
        coordinator.release_tail_vacated(
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            train_id=train_id,
            direction=event.exit_edge,
            route=route,
        )

    # ========================================================================
    # Completion handshake
    # ========================================================================

    def try_complete_active_path(self, train_id: str) -> PathCompletionResult:
        # design_new.md "Time sampling": the writer samples wall-clock time
        # itself. One local sample covers this writer's own two writes — the
        # `Path.last_status_updated_time` status write and its `path_completed`
        # notification. It is not threaded on to `RouteExecutor`, which is a
        # separate writer and samples `finished_at` fresh.
        event_time = time.time()
        path = self.get_active_path(train_id)
        if path is None:
            return PathCompletionResult(
                success=False,
                path_id=None,
                new_status=None,
                reason="no active path",
            )
        path_id = path.path_id

        controller = self._get_controller(train_id)
        if controller is None:
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="controller not available",
            )

        stop_check = getattr(controller, "is_stopped_at_reservation_end", None)
        if not callable(stop_check) or not stop_check():
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="controller not stopped at authority end",
            )

        if not path.get_reservation_end_block_id():
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="path missing authority end",
            )
        # design_new.md "Stop-arrival check" (step `8df5`), conditions 1 and 2:
        # resolve the served row through the route-owned completion projection
        # and accept an arrival ONLY when the next physical task is a `stop`
        # whose stored resolved-target block is the live authority's terminal
        # block. A non-stop next task (`waypoint` / `run` / `dwell`) or a `None`
        # projection makes the arrival predicate false, so this call never
        # completes — nor releases — a path whose next task is not a stop. That
        # gate is what lets a terminal `waypoint`'s spent authority be released
        # by the route's revoke-cancel flow (design_new.md step `592a`) instead
        # of by an accidental completion here. `try_complete_active_path` reads
        # only `task_kind` and `stored_resolved_target` from the projection and
        # never indexes `RouteRun.task_runs` or `Route.tasks`
        # (design_new.md "CompletionTaskProjection schema").
        route_executor = self._get_route_executor()
        projection = (
            route_executor.find_next_completion_task(train_id)
            if route_executor is not None
            else None
        )
        if projection is None or projection.task_kind != "stop":
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="next completion task is not a stop",
            )
        stored_target = projection.stored_resolved_target
        if (
            stored_target is None
            or path.get_reservation_end_block_id()
            != stored_target.requested_destination_block_id
        ):
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="authority end has not reached the served stop's target",
            )

        # Arrival direction is NOT re-checked here. Per design_new.md "Path
        # field schema" (the Stop-arrival check bullet): "Arrival direction is
        # guaranteed at acquisition instead of re-checked later" — candidate
        # search already rejected any candidate whose terminal traversal did
        # not satisfy the caller's `requested_destination_direction`, so a
        # stored authority that reaches a target reaches it in a legal frame by
        # construction. Direction is not among the Stop-arrival conditions.

        # Release reservations + switch locks via the ResourceCoordinator
        # (completed=True). Only the path's still-held future reservations are
        # released; blocks already consumed at head-entry are not part of the
        # future set (see `_blocks_still_reserved_by`). The completed=True
        # caution force-clear only applies to still-owned blocks, so this
        # filtering does not drop any caution clearing.
        all_blocks = self._blocks_still_reserved_by(path.get_all_blocks(), path_id)
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is not None:
            coordinator.release_path_resources(
                path_id=path_id,
                block_ids=all_blocks,
                completed=True,
            )

        # Mark COMPLETED.
        path._set_status(PathStatus.COMPLETED, event_time)

        # Event-log operational event (design_new.md "Event log"): path
        # completed. Emitted through NotificationCenter — the one stream the
        # event log persists — at the point completion is committed.
        _nc = self._notification_center
        if _nc is not None:
            from notification_center import Kind, path_ref, train_ref

            _nc.emit(
                source="PathDispatcher",
                operation="try_complete_active_path",
                kind=Kind.INFO,
                message=f"path {path_id} completed for {train_id}",
                entities=[train_ref(train_id), path_ref(path_id)],
                event_time=event_time,
                payload={
                    "reservation_end_block_id": path.get_reservation_end_block_id(),
                },
            )

        # Notify controller.
        on_complete = getattr(controller, "on_path_complete", None)
        if callable(on_complete):
            on_complete()

        # Deliver completion to the injected route executor, which records the
        # served stop through RouteExecutor.advance_walk_position(...) and emits the
        # canonical `path_completion` notification itself (the terminal-teardown
        # flow, step `09b0` in design_new.md "Core Runtime Loop"). The dispatcher
        # holds the route executor as an injected named dependency, not through a
        # SimulationController reference (design_new.md, "Dispatcher/route-executor inputs").
        #
        # Per design_new.md step `bc55`: the callback "records the stop as
        # finished through `RouteExecutor.advance_walk_position(walk_position)`" for
        # the walk-position `find_next_completion_task(...)` already resolved
        # above. It is handed that `walk_position`, not the authority-end block
        # or the requested direction — the served row is already identified and
        # its arrival frame was fixed at acquisition.
        served_walk_position = projection.walk_position
        route_executor = self._get_route_executor()
        if route_executor is not None:
            route_executor.handle_path_completion(
                train_id,
                path_id,
                served_walk_position,
            )

        # Per design_new.md "Retained path history": completed paths remain in
        # `paths_by_id[path_id]` for history. `Path.status == COMPLETED`
        # is what makes `active_path_id_by_train(...)` skip them.

        return PathCompletionResult(
            success=True,
            path_id=path_id,
            new_status=PathStatus.COMPLETED,
            reason=None,
        )

    # ========================================================================
    # Query API
    # ========================================================================

    def get_path(self, path_id: str) -> Optional[Path]:
        return self.paths.get(path_id)

    def get_active_paths(self) -> List[Path]:
        """Return all currently ACTIVE path records.

        Per design_new.md "Retained path history" cancelled/completed rows remain in
        `paths_by_id` as historical records; they are filtered out here so
        callers that ask for "active paths" never see terminal rows.
        """
        return [p for p in self.paths.values() if p.status == PathStatus.ACTIVE]

    def get_active_path(self, train_id: str) -> Optional[Path]:
        if train_id is None:
            return None
        for path in self.paths.values():
            if path.train_id == train_id and path.status == PathStatus.ACTIVE:
                return path
        return None

    def active_path_id_by_train(self, train_id: str) -> Optional[str]:
        path = self.get_active_path(train_id)
        return path.path_id if path else None

    def most_recently_terminated_path(self, train_id: str) -> Optional[Path]:
        """The train's most recently terminated (non-`ACTIVE`) `Path` record.

        Cancelled and completed rows stay in `paths_by_id` as history; this
        returns the one with the latest status change (`last_status_updated_time`),
        or `None` when the train has no terminated path. The read model uses it
        to derive `RouteRun.display_state == "CANCELLED"` (design_new.md
        "Route-run state", `display_state`).
        """
        if train_id is None:
            return None
        terminals = [
            p
            for p in self.paths.values()
            if p.train_id == train_id and p.status != PathStatus.ACTIVE
        ]
        if not terminals:
            return None
        return max(terminals, key=lambda p: p.last_status_updated_time)

    def next_call_walk_candidates(
        self,
        train_id: str,
        requested_destination_block_id: str,
        requested_destination_direction: Optional[str] = None,
        manual_override: bool = False,
    ) -> List[GraphWalkCandidate]:
        """The walks this train's next acquisition call enumerates, best first,
        before any meet key filter (design_new.md "Meet coordination", Conflict
        detection).

        With a live path that is `extend_path(...)`'s list: walks from the
        reservation-end block that continue forward and revisit nothing the
        path still holds. Without one it is `create_path(...)`'s list: walks
        from the start-side span end, with no internal repeat. Meet
        coordination builds its routes and `candidate_resource_keys` from this
        list, so the keys it chooses are ones the next call can match. Returns
        an empty list wherever that call would fail before choosing a walk.
        Read-only.
        """
        if not train_id or not requested_destination_block_id:
            return []
        if requested_destination_block_id not in self.network.blocks:
            return []
        path = self.get_active_path(train_id)
        if path is not None:
            held_blocks = self._blocks_held_by(path)
            if requested_destination_block_id in held_blocks:
                return []
            candidates = self._find_walk_candidates(
                path.get_reservation_end_block_id(),
                requested_destination_block_id,
                train_id,
                manual_override=manual_override,
                direction_constraint=requested_destination_direction,
            )
            held_switches = self._switches_held_by(path)
            return [
                cand
                for cand in candidates
                if self._segment_continues_forward(path, cand.walk)
                and not self._segment_would_revisit_resources(
                    cand.walk, held_blocks, held_switches
                )
            ]
        train = self._get_train(train_id)
        if train is None:
            return []
        head_block = self._get_current_block_id_for_train(train)
        if not head_block or head_block == requested_destination_block_id:
            return []
        start_block_id = self._resolve_create_start_block(
            train,
            train_id,
            requested_destination_block_id,
            head_block,
            manual_override,
        )
        candidates = self._find_walk_candidates(
            start_block_id,
            requested_destination_block_id,
            train_id,
            manual_override=manual_override,
            direction_constraint=requested_destination_direction,
        )
        empty: Set[str] = set()
        return [
            cand
            for cand in candidates
            if not self._segment_would_revisit_resources(cand.walk, empty, empty)
        ]

    def get_waiting_conflict(
        self,
        train_id: str,
        stored_resolved_target: Optional[ResolvedRouteTarget],
        manual_override: bool = False,
        reservation_cap_block_id: Optional[str] = None,
        required_candidate_resource_keys: Optional[List[CandidateResourceKey]] = None,
    ) -> Optional[WaitingConflict]:
        """Canonical forward-resource acquisition read for a blocked waiter
        (design_new.md "Contention snapshot").

        Reruns the exact pathfinder / eligibility walk `extend_path(...)` would
        run from the active path's current authority end toward
        `stored_resolved_target`, finds the first forward resource that fails
        acquisition because of a live occupancy / reservation / lock conflict,
        and reports it together with the trains in the way. `manual_override`,
        `reservation_cap_block_id`, and `required_candidate_resource_keys` are the
        same override and tick-start meet constraints Plan would pass to
        `extend_path(...)`.

        Read-only: candidate enumeration and `validate_path` touch no
        path/block/switch state, so it is safe on every projection read
        (best-effort retries change the blocker tick to tick, which is why it is
        recomputed rather than retained). Returns None when the train has no
        active path, no armed target, the authority already reaches the target,
        no viable or pinned suffix exists, the suffix is currently acquirable
        end to end, or the failure has no live train owner.
        """
        if stored_resolved_target is None or not train_id:
            return None
        path = self.get_active_path(train_id)
        if path is None:
            return None
        target = stored_resolved_target.requested_destination_block_id
        authority_end = path.get_reservation_end_block_id()
        # Reached the requested target (or no target recorded): not waiting.
        # Whether a live authority reaches a target is `Path.reaches_target(...)`,
        # never `authority_end == target` (design_new.md, "Path field schema"): a live
        # authority can legitimately run past a waypoint target, and an
        # authority-end equality test would miss that reached case.
        if not target or path.reaches_target(target):
            return None

        # Same pathfinder call extend_path makes from the current authority end.
        candidates = self._find_walk_candidates(
            authority_end, target, train_id, manual_override=manual_override,
        )
        forward_candidates = [
            cand
            for cand in candidates
            if self._segment_continues_forward(path, cand.walk)
        ]
        # A pinned meet candidate narrows the list to exactly that route before
        # anything else; when it is no longer present the train is not waiting on
        # a live conflict this read can name (design_new.md "Meet coordination").
        if required_candidate_resource_keys is not None:
            forward_candidates = [
                cand
                for cand in forward_candidates
                if self._candidate_matches_pinned_keys(
                    cand.ordered_walk, required_candidate_resource_keys
                )
            ]
        if not forward_candidates:
            # No viable suffix toward the target (or the pinned candidate is gone).
            return None

        segment = forward_candidates[0].walk
        ordered_walk = forward_candidates[0].ordered_walk
        # Honour a meet acquisition cap the same way create/extend do: never look
        # past the cap block. When the cap sits at or before the current
        # authority end, the train holds at the cap rather than waiting on a live
        # conflict, so there is no conflict to report.
        if reservation_cap_block_id is not None:
            cap_status, capped_segment, _capped_walk = self._truncate_segment_to_cap(
                segment, ordered_walk, reservation_cap_block_id, target,
            )
            if cap_status == "hold":
                return None
            if cap_status == "capped":
                segment = capped_segment

        # Mirror extend_path's first-blocker derivation read-only: validate the
        # best forward candidate against this path_id and read off the first
        # occupancy-class (occupied / reserved / switch-locked) conflict.
        validation = self.validate_path(
            segment, path.path_id, train_id, manual_override=manual_override,
        )
        if validation.valid:
            # Suffix is currently acquirable end-to-end — nothing blocking it.
            return None
        return self._first_conflict_from_validation(segment, validation)

    def get_waiting_for_block_id(
        self,
        train_id: str,
        stored_resolved_target: Optional[ResolvedRouteTarget] = None,
        manual_override: bool = False,
        reservation_cap_block_id: Optional[str] = None,
        required_candidate_resource_keys: Optional[List[CandidateResourceKey]] = None,
    ) -> Optional[str]:
        """Read-model projection of `waiting_block_id` from `get_waiting_conflict`.

        Forwards the complete request and returns only the block the waiter is
        stuck at (design_new.md "Contention snapshot", "Data Flow To UI"). Does
        not rerun the walk itself — it is the one-field view of the full
        `WaitingConflict` for the "show stuck" overlay.
        """
        conflict = self.get_waiting_conflict(
            train_id,
            stored_resolved_target,
            manual_override,
            reservation_cap_block_id,
            required_candidate_resource_keys,
        )
        return conflict.waiting_block_id if conflict is not None else None

    def _first_conflict_from_validation(
        self, segment: PathSegment, validation: ValidationResult
    ) -> Optional[WaitingConflict]:
        """Build the `WaitingConflict` for the first occupancy-class error in a
        validated suffix, or None when none has a live train owner."""
        for err in validation.errors:
            if not err.type.is_occupancy_class():
                continue
            if err.block_id:
                # A block conflict: the occupied / reserved block ahead is both
                # the failed resource and the block the waiter is stuck behind.
                # A free block on a kept way is held by the train the way is
                # kept for (design_new.md "Contention snapshot").
                if err.type == ValidationErrorType.MEET_WAY_KEPT:
                    holders = (err.kept_for_train,) if err.kept_for_train else ()
                else:
                    holders = self._block_conflict_holders(err)
                if not holders:
                    continue
                return WaitingConflict(
                    waiting_block_id=err.block_id,
                    failed_resource_type="block",
                    failed_resource_id=err.block_id,
                    holding_train_ids=holders,
                )
            # A SWITCH_LOCKED / flank conflict records the failing switch as a
            # tile position, not a block_id. The waiter is stuck at the last
            # acquirable block before that locked switch (the same
            # switch-position -> block resolution `_compute_truncate_hint` uses),
            # while the failed resource is the switch itself.
            if err.position is not None:
                held_block = None
                prefix_len = self._position_to_block_index(segment, err.position)
                if prefix_len and 0 < prefix_len <= len(segment.blocks):
                    held_block = segment.blocks[prefix_len - 1]
                if held_block is None:
                    continue
                switch_id, holders = self._switch_conflict_holders(err)
                if err.type == ValidationErrorType.MEET_WAY_KEPT:
                    holders = (err.kept_for_train,) if err.kept_for_train else ()
                if not holders:
                    continue
                return WaitingConflict(
                    waiting_block_id=held_block,
                    failed_resource_type="switch",
                    failed_resource_id=switch_id,
                    holding_train_ids=holders,
                )
        return None

    def _block_conflict_holders(self, err: ValidationError) -> Tuple[str, ...]:
        """Trains in the way of a blocked block: the physical occupant if any,
        otherwise the reservation owner (`reserved_for_train`, or failing that
        the reserving path's train). A same-train holder is kept."""
        block = self.network.blocks.get(err.block_id)
        occupant = err.occupied_by or (
            getattr(block, "occupied_by", None) if block is not None else None
        )
        if occupant:
            return (occupant,)
        reserver = getattr(block, "reserved_for_train", None) if block is not None else None
        if reserver:
            return (reserver,)
        # A block reserved by a path but not carrying a `reserved_for_train`
        # owner still has a holder: the reserving path's train. A normal
        # `reserve_for_path` sets both together, so resolve the path owner
        # directly here to keep a reserved-by-path-only state surfacing as
        # `waiting_for_block_id` / a contention edge (the old read reported the
        # block unconditionally for any occupancy-class error).
        reserving_path = (
            getattr(block, "reserved_by_path", None) if block is not None else None
        )
        if reserving_path:
            path = self.paths_by_id.get(reserving_path)
            holder = path.train_id if path is not None else None
            if holder:
                return (holder,)
        return ()

    def _switch_conflict_holders(
        self, err: ValidationError
    ) -> Tuple[str, Tuple[str, ...]]:
        """Resolve `(switch_id, holder_train_ids)` for a switch-lock / flank
        conflict at `err.position`: physical occupants first, otherwise every
        lock and train-protection owner on that switch. Duplicates removed,
        deterministic order (physical group then owner group, each sorted)."""
        switch_id = ""
        track = self.network.get_track(*err.position) if err.position else None
        if track is not None:
            switch_id = getattr(track, "switch_id", None) or ""
        switch = None
        if switch_id:
            switch = getattr(self.network, "switches", {}).get(switch_id)
        if switch is None:
            # Fall back to the path-owner the validation error named.
            owner = self._train_for_path(err.locked_by_path)
            return switch_id, ((owner,) if owner else ())

        physical = sorted({t for t in switch.route_owners.values() if t})
        if physical:
            return switch_id, tuple(physical)
        owners = {t for (t, _state) in switch.locks.values() if t}
        owners |= {
            t for (t, _src, _state) in switch.train_protection_claims if t
        }
        return switch_id, tuple(sorted(owners))

    def _train_for_path(self, path_id: Optional[str]) -> Optional[str]:
        """The train that owns `path_id`, or None."""
        if not path_id:
            return None
        path = self.paths.get(path_id)
        return path.train_id if path is not None else None

    @staticmethod
    def _ordered_walk_keys(ordered_walk) -> List[CandidateResourceKey]:
        """The `(resource_type, resource_id, entry_edge, exit_edge)` key of every
        pathfinder step, in traversal order."""
        keys: List[CandidateResourceKey] = []
        for step in ordered_walk or []:
            keys.append((
                getattr(step, "resource_type", "") or "",
                getattr(step, "resource_id", "") or "",
                getattr(step, "entry_edge", "") or "",
                getattr(step, "exit_edge", "") or "",
            ))
        return keys

    @classmethod
    def _candidate_matches_pinned_keys(
        cls, ordered_walk, required_keys: Optional[List[CandidateResourceKey]]
    ) -> bool:
        """True when this acquisition's candidate is exactly the walk the
        caller pinned (design_new.md "Path And PathDispatcher", `create_path()`
        flow step 4).

        `required_keys` start where this call starts — the start-side span end
        for `create_path(...)`, the reservation end for `extend_path(...)` —
        because meet coordination and the required walk build them from the
        same enumeration (`next_call_walk_candidates`). So the candidate must
        have the same steps, in the same order, from the first to the last. The
        first step matches on `(type, id, exit_edge)` only: its `entry_edge` is
        the walk's back-edge fallback and can differ from the same tile's entry
        mid-route (a curved reservation-end tile). Every later step matches on
        the full `(type, id, entry_edge, exit_edge)` frame, which pins the
        traversal direction.
        """
        required = [tuple(k) for k in (required_keys or [])]
        cand = cls._ordered_walk_keys(ordered_walk)
        if not required or len(cand) != len(required):
            return False
        first_c, first_r = cand[0], required[0]
        if (first_c[0], first_c[1], first_c[3]) != (first_r[0], first_r[1], first_r[3]):
            return False
        return cand[1:] == required[1:]

    def get_active_path_progress(self, train_id: str) -> Optional[Dict[str, Any]]:
        """Read-only projection of dispatcher state for a single train.

        Used by the API/UI layer per design_new.md "Data Flow To UI". Returns
        the five dispatcher-owned path fields (``path_id``, ``status``,
        ``current_resource_index``, ``reservation_end_block_id``, and
        ``resource_sequence``); it does not read train/controller mirror fields.
        ``None`` when no active path exists for the train.

        ``waiting_for_block_id`` is NOT part of this projection: it needs the
        complete route-owned request (the run's ``resolved_target`` plus its
        manual override), so the UI-payload assembly composes it there from the
        route-supplied
        ``get_waiting_for_block_id(...)`` call (design_new.md "Data Flow To UI",
        "Contention snapshot").
        """
        path = self.get_active_path(train_id)
        if path is None:
            return None
        return {
            "path_id": path.path_id,
            "status": path.status.value,
            "current_resource_index": path.current_resource_index,
            "reservation_end_block_id": path.get_reservation_end_block_id(),
            "resource_sequence": [
                {
                    "sequence_index": entry.sequence_index,
                    "resource_type": entry.resource_type,
                    "resource_id": entry.resource_id,
                    "entry_edge": entry.entry_edge,
                    "exit_edge": entry.exit_edge,
                }
                for entry in path.resource_sequence
            ],
        }


