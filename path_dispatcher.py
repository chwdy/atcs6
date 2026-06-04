"""
Path Dispatcher Module (v1)

The PathDispatcher is the dispatcher-owned authority registry. It owns:
- create_path / extend_path / cancel_path lifecycle for v1 Path entities
- on_controller_progress consumption of TrainController's Phase-3 progress reports
- try_complete_active_path for the canonical completion handshake
- pathfinding helpers (find_paths, get_reachable_endpoints) used by routes/UI

In v1 the canonical Path schema lives in path.py and uses:
  requested_destination_block_id, requested_destination_direction,
  authority_end_block_id, resource_sequence (List[ResourceSequenceEntry]),
  current_resource_index, last_status_updated_time, status.

There is no v0 start_block_id / end_block_id / block_sequence /
current_block_index / priority / dispatcher_id / switch_positions /
flank_protection_switches / overlap_blocks / approach_locked_blocks /
blocks_cleared. All call sites here use the v1 fields.

PathSegment (geometry-only candidate from BFS) and the ValidationResult /
ValidationError schemas are imported from runtime_payloads.py.
"""

from collections import deque
from datetime import datetime
import logging
import time

logger = logging.getLogger(__name__)
import uuid
from typing import Dict, List, Optional, Set, Tuple, Any, TYPE_CHECKING

from path import (
    Path,
    PathStatus,
    PathTileEntry,
    ResourceSequenceEntry,
)
from enums import (
    BlockState,
    SwitchState,
    TrainState,
    direction_from_delta,
    OPPOSITE_DIRECTIONS,
)
from track import OPPOSITE_EDGE
from model_scale import kmh_to_cm_per_sec
from runtime_payloads import (
    AcquisitionMode,
    CancellationResult,
    Command,
    ControllerProgressReport,
    DEFAULT_PATH_PRIORITY,
    PathAcquisitionFailureKind,
    PathAcquisitionResult,
    PathAcquisitionResultKind,
    PathCompletionResult,
    PathSegment,
    PendingPathCancelRequest,
    ResourceEvent,
    RouteExecutorForPathDispatcher,
    ValidationError,
    ValidationErrorType,
    ValidationResult,
)
from block_manager import ReservedBlockTraversal
from switch_manager import SwitchLockRequest
from railway_pathfinder import RailwayPathfinder
from path_utils import GraphWalkCandidate, GraphWalkStep

if TYPE_CHECKING:
    from network import RailwayNetwork
    from block import Block
    from track import TrackSegment



class PathDispatcher:
    """
    Manages path creation, validation, lifecycle, and progress consumption.

    Public v1 API:
        create_path(train_id, requested_destination_block_id,
                    requested_destination_direction, acquisition_mode)
        extend_path(path_id, requested_destination_block_id,
                    requested_destination_direction, acquisition_mode)
        cancel_path(path_id, initiator, reason)
        on_controller_progress(report)
        try_complete_active_path(train_id, event_time)
        get_active_path(train_id)
        active_path_id_by_train(train_id)
        get_active_paths()
        get_path(path_id)
        find_paths(start_block_id, end_block_id, train_id)
        get_reachable_endpoints(start_block_id, include_stations)
        get_reachable_endpoints_for_extension(path_id, include_stations)
        validate_path(path_segment, path_id, train_id)
    """

    # Per design_new.md "create_path() flow" (line 884), AcquisitionMode is
    # documented as a `PathDispatcher`-owned enum. The implementation lives in
    # `runtime_payloads.py` to avoid circular imports; this class attribute
    # exposes the canonical `PathDispatcher.AcquisitionMode` name.
    AcquisitionMode = AcquisitionMode

    def __init__(self, network: "RailwayNetwork"):
        self.network = network
        self.paths: Dict[str, Path] = {}
        self.path_sequence_counter: int = 0
        # Back-reference to SimulationController, wired by the runtime when
        # available. Used to resolve TrainController instances and the
        # NotificationCenter for progress callbacks.
        self.simulation_controller: Optional[Any] = None
        # Canonical pathfinding home shared with RouteExecutor (per
        # design_new.md line 605). The same instance services dispatcher
        # candidate enumeration and any RouteExecutor platform-end
        # selection that needs the shared comparison rules.
        self._pathfinder = RailwayPathfinder(network)

    @property
    def active_paths(self) -> Dict[str, Path]:
        """Legacy alias for the underlying path registry.

        Per design_new.md ~line 80, completed/cancelled paths are retained
        as historical records in the same dict (skipped by the canonical
        `active_path_id_by_train(...)` projection because their status is
        no longer ACTIVE). Callers that want only ACTIVE-status rows
        should use `get_active_paths()` instead of this raw view.
        """
        return self.paths

    @property
    def paths_by_id(self) -> Dict[str, Path]:
        """Canonical alias from design_new.md ~line 78. Same dict as
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

    def _get_route_executor(self) -> Optional[RouteExecutorForPathDispatcher]:
        sc = self.simulation_controller
        if sc is None:
            return None
        return sc.route_executor

    def _get_controller(self, train_id: str):
        sc = self.simulation_controller
        if sc is None:
            return None
        getter = (
            getattr(sc, "get_or_create_controller", None)
            or getattr(sc, "get_controller", None)
            or getattr(sc, "_get_or_create_controller", None)
        )
        if callable(getter):
            try:
                return getter(train_id)
            except Exception:
                pass
        controllers = getattr(sc, "controllers", None)
        if isinstance(controllers, dict):
            return controllers.get(train_id)
        return None

    def _head_tile_for_train(self, train):
        """Train head tile via its controller's cab-derived span, or None
        (design_new.md: head/tail live on the controller, not on Train)."""
        if train is None:
            return None
        ctrl = self._get_controller(getattr(train, "train_id", None))
        return ctrl._get_head_tile() if ctrl is not None else None

    def _get_current_block_id_for_train(self, train) -> Optional[str]:
        """Resolve the train's current block (head tile)."""
        head_tile = self._head_tile_for_train(train)
        if not head_tile:
            return None
        track = self.network.get_track(*head_tile)
        if track is None:
            return None
        return track.block_section_id

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

        Per design_new.md "Path extension" (lines 1040-1041, 1083): a path's
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
            block_id = getattr(track, "block_section_id", None)
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
        "Pathfinding-home migration").
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

        existing_blocks = set(path.get_all_blocks())
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
                next_block_id = next_track.block_section_id
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
                    if track and track.block_section_id in reachable_blocks:
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
        return self._pathfinder.find_ordered_walk(
            start_block_id=start_block_id,
            end_block_id=end_block_id,
            direction_constraint=direction_constraint,
            manual_override=manual_override,
        )

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
            if not track.block_section_id:
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

        for pos, required_state in path.required_switches.items():
            track = self.network.get_track(*pos)
            if not track:
                continue
            # Resolve the underlying Switch entity for availability checks.
            switch_entity = self._resolve_switch_at_position(pos)
            # Disabled switches are structural failures, not occupancy-class.
            # Per design_new.md "Availability flags" canonical home, a
            # disabled switch behaves like missing track; per
            # ValidationErrorType.is_occupancy_class, SWITCH_DISABLED is not
            # occupancy-class, so BEST_EFFORT will not retry around it and a
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
                # canonical lock store is `Switch.locks` (design_new.md:651).
                holder_path = next(iter(switch_entity.locks), (None,))[0]
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.SWITCH_LOCKED,
                        position=pos,
                        locked_by_path=holder_path,
                        message=f"Switch at {pos} locked",
                    )
                )

        return ValidationResult(valid=len(errors) == 0, errors=errors, path=path)

    # ========================================================================
    # v1 Public API: create_path / extend_path / cancel_path
    # ========================================================================

    def create_path(
        self,
        train_id: str = "",
        requested_destination_block_id: str = "",
        requested_destination_direction: str = "forward",
        acquisition_mode: AcquisitionMode = AcquisitionMode.BEST_EFFORT,
        manual_override: bool = False,
    ) -> PathAcquisitionResult:
        """v1 create_path: build authority for a train to a destination block.

        Args:
            train_id: Train requesting authority. Required in v1.
            requested_destination_block_id: Caller's target block.
            requested_destination_direction: "forward" or "reverse".
            acquisition_mode: ALL_OR_NOTHING or BEST_EFFORT.
            manual_override: Operator-override flag per the `disabled` and
                `manual` availability flags canonical home in design_new.md
                (under TrackResource). Default False (every automatic caller,
                including `RouteExecutor`); True only for operator-initiated
                API/UI/admin calls that explicitly chose to use manual
                resources. Disabled resources are always excluded regardless.

        Returns:
            PathAcquisitionResult.
        """
        # Validate direction.
        if requested_destination_direction not in ("forward", "reverse"):
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Invalid direction: {requested_destination_direction!r}",
            )

        if not train_id:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason="train_id required in v1",
            )

        train = self._get_train(train_id)
        if train is None:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Train {train_id} not found",
            )

        if not requested_destination_block_id:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                requested_destination_direction=requested_destination_direction,
                reason="requested_destination_block_id required",
            )
        if requested_destination_block_id not in self.network.blocks:
            return PathAcquisitionResult(
                success=False,
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
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.TRAIN_ALREADY_HAS_ACTIVE_PATH,
                path_id=existing.path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=f"Train {train_id} already has active path {existing.path_id}",
            )

        # Resolve start block from train's current head tile.
        start_block_id_resolved = self._get_current_block_id_for_train(train)
        if not start_block_id_resolved:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=f"Cannot resolve current block for train {train_id}",
            )

        # Same-block repositioning (design_new.md:958-:971). When the requested
        # destination block is already inside the train's current occupied span,
        # the pathfinder cannot walk a block to itself, so dispatcher builds a
        # synthetic one-block local authority over the train's own block instead
        # of running the normal candidate search. The block is already occupied
        # by this train (the covered-span seed), so it is NOT newly reserved.
        # Restrict the synthetic path to the case where the train's HEAD is in
        # the destination block — a genuine reposition within the head's own
        # block. A destination that is a *non-head* spanned block (e.g. the tail)
        # is a reversal/turnaround, which must go through the normal cab-flip +
        # covered-task flow; building a degenerate synthetic path there would
        # hijack it. Per design_new.md:960 the synthetic path is an explicit
        # repositioning capability, "rather than ... the normal route-side
        # handling for an already covered stop waypoint."
        if self._get_current_block_id_for_train(train) == requested_destination_block_id:
            return self._create_same_block_path(
                train=train,
                train_id=train_id,
                block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                now=self._now(),
            )

        # Find geometry candidates (shortest first). Direction is enforced
        # downstream during acquisition; it is intentionally not passed as a
        # walk-search filter here because the terminal-direction check is not
        # tunnel-aware (a tunnel hop has no meaningful tile delta to classify).
        candidates = self._find_walk_candidates(
            start_block_id_resolved,
            requested_destination_block_id,
            train_id,
            manual_override=manual_override,
        )
        if not candidates:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason="No path exists between start and destination",
            )

        # Reject candidates with internal repeats — a single path's
        # resource_sequence must not contain the same block or switch
        # twice. Per design_new.md "Path extension" line 1083 the same
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
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=(
                    "all forward candidates contain repeated resources "
                    "(internal loop)"
                ),
            )
        candidates = non_revisit_candidates

        # design_new.md:931 / :940 — dispatcher selects EXACTLY ONE candidate
        # (the shortest walk per the GraphWalkCandidate comparison) and applies
        # acquisition_mode to that one candidate. It must NOT fall through to a
        # second, longer candidate when the chosen one cannot be acquired or
        # truncated; choosing a different route to the same resolved target is
        # forbidden here (RouteExecutor owns trying a different resolved target
        # later). Committing to the single best candidate is what makes a train
        # wait at a contested switch instead of taking a map-long detour around
        # it.
        candidates = candidates[:1]

        path_id = self._generate_path_id(train_id, requested_destination_block_id)
        now = self._now()

        # Apply acquisition_mode to the single chosen candidate.
        last_failed_resource_id: Optional[str] = None
        last_reason: str = ""
        had_structural_failure = False
        for cand in candidates:
            # `segment` is the flat walk used by the structural/acquisition
            # helpers; `cand.ordered_walk` is the per-step geometry threaded
            # into resource_sequence / traversal building.
            segment = cand.walk
            ordered_walk = cand.ordered_walk
            validation = self.validate_path(
                segment, path_id, train_id, manual_override=manual_override
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
                    # always hard-reject per design_new.md ":890".
                    had_structural_failure = True
                    continue

                if acquisition_mode == AcquisitionMode.ALL_OR_NOTHING:
                    continue

                # BEST_EFFORT + occupancy-only: truncate to the prefix
                # before the first failing block, per design_new.md ":938".
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
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    now=now,
                    ordered_walk=self._slice_ordered_walk(
                        ordered_walk, truncated.tiles
                    ),
                )
                self.paths[path_id] = path
                return PathAcquisitionResult(
                    success=True,
                    result_kind=PathAcquisitionResultKind.TRUNCATED_GREW,
                    path_id=path_id,
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    authority_end_block_id=path.get_authority_end_block_id(),
                    resource_sequence=list(path.resource_sequence),
                    reason="best_effort partial acquisition",
                )

            # Acquire resources.
            acquire_ok, fail_reason, fail_resource = self._acquire_segment_resources(
                segment, path_id, train_id, requested_destination_direction,
                manual_override=manual_override,
                ordered_walk=ordered_walk,
            )
            if not acquire_ok:
                last_failed_resource_id = fail_resource
                last_reason = fail_reason
                # Roll back any partial acquisition we did on this segment.
                self._rollback_segment(segment, path_id)
                if acquisition_mode == AcquisitionMode.ALL_OR_NOTHING:
                    continue
                # BEST_EFFORT: try to acquire a truncated prefix of this segment.
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
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    now=now,
                    ordered_walk=self._slice_ordered_walk(
                        ordered_walk, truncated.tiles
                    ),
                )
                self.paths[path_id] = path
                return PathAcquisitionResult(
                    success=True,
                    result_kind=PathAcquisitionResultKind.TRUNCATED_GREW,
                    path_id=path_id,
                    requested_destination_block_id=requested_destination_block_id,
                    requested_destination_direction=requested_destination_direction,
                    authority_end_block_id=path.get_authority_end_block_id(),
                    resource_sequence=list(path.resource_sequence),
                    reason="best_effort partial acquisition",
                )

            # Full acquisition success.
            path = self._build_path_from_segment(
                path_id=path_id,
                train_id=train_id,
                segment=segment,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                now=now,
                ordered_walk=ordered_walk,
            )
            self.paths[path_id] = path
            return PathAcquisitionResult(
                success=True,
                result_kind=PathAcquisitionResultKind.REACHED_REQUESTED_TARGET,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id(),
                resource_sequence=list(path.resource_sequence),
                reason="created",
            )

        # All candidates failed. In best_effort mode, when the train head
        # currently sits on a switch resource we cannot store a block-
        # terminated holding authority, so surface the head-on-switch case
        # explicitly per design_new.md ":894". This only fires when the
        # loop exhausted because of occupancy conflicts — a structural
        # failure (e.g. disabled downstream block) is reported through the
        # BLOCKED_BEFORE_TARGET tail instead.
        if (
            acquisition_mode == AcquisitionMode.BEST_EFFORT
            and not had_structural_failure
            and self._train_head_is_on_switch(train)
        ):
            head_switch_reason = "cannot create best-effort authority while head is on a switch"
            if last_failed_resource_id:
                head_switch_reason += f" (blocked at {last_failed_resource_id})"
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.HEAD_ON_SWITCH_NO_BLOCK_AUTHORITY,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                reason=head_switch_reason,
            )
        blocked_reason = last_reason or "All candidate paths blocked"
        if last_failed_resource_id and last_failed_resource_id not in blocked_reason:
            blocked_reason = f"{blocked_reason} (blocked at {last_failed_resource_id})"
        return PathAcquisitionResult(
            success=False,
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
        acquisition_mode: AcquisitionMode = AcquisitionMode.BEST_EFFORT,
        manual_override: bool = False,
    ) -> PathAcquisitionResult:
        """v1 extend_path: append more authority to an existing ACTIVE path.

        `manual_override` (default False) is the operator-override flag per the
        `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). Default for every automatic
        caller; True only for operator-initiated API/UI/admin calls that
        explicitly chose to use manual resources.
        """
        if requested_destination_direction not in ("forward", "reverse"):
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                path_id=path_id or None,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Invalid direction: {requested_destination_direction!r}",
            )

        path = self.paths.get(path_id)
        if path is None or path.status != PathStatus.ACTIVE:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.MISSING_OR_INACTIVE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id or None,
                requested_destination_direction=requested_destination_direction,
                reason=f"Path {path_id} missing or not ACTIVE",
            )

        if not requested_destination_block_id:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                path_id=path_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id() or None,
                reason="requested_destination_block_id required",
            )

        existing_blocks = path.get_all_blocks()
        if requested_destination_block_id in existing_blocks:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id() or None,
                reason=f"{requested_destination_block_id} is already in path",
            )

        train_id = path.train_id
        train = self._get_train(train_id) if train_id else None
        if train is None:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id() or None,
                reason="path has no train",
            )

        # Find extension geometry from current authority end (shortest first).
        # As in create_path, direction is enforced downstream during
        # acquisition, not as a walk-search filter (the terminal-direction
        # check is not tunnel-aware).
        start_block_id = path.get_authority_end_block_id()
        candidates = self._find_walk_candidates(
            start_block_id,
            requested_destination_block_id,
            train_id,
            manual_override=manual_override,
        )
        if not candidates:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id() or None,
                reason=f"No forward path from {start_block_id} to {requested_destination_block_id}",
            )

        # Reject candidates that would reverse 180 degrees through the
        # current authority-end block. Per design_new.md "extend_path()
        # contract", the new suffix must continue forward from the existing
        # prefix's travel frame; if every candidate would have to exit the
        # authority-end block via an edge other than the prefix's terminal
        # exit_edge, the request is an invalid reverse.
        forward_candidates = [
            cand
            for cand in candidates
            if self._segment_continues_forward(path, cand.walk)
        ]
        if not forward_candidates:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_REVERSE,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id() or None,
                reason="extend would reverse at authority end",
            )
        candidates = forward_candidates

        # Reject candidates whose tile walk would cause any block or switch
        # to appear twice in the combined (existing path + appended segment)
        # resource_sequence, per design_new.md "Path extension" lines
        # 1040-1041, 1083 ("repeated resources in a single path are invalid
        # and should be rejected during both create_path() and extend_path()").
        # Without this, find_ordered_walk can return a candidate whose
        # post-seam tile path re-enters the path's start block via the
        # south tunnel (or any other connection that loops the network),
        # BEST_EFFORT truncates that loop into a closed prefix, and
        # extend_path appends one full loop to the path every Phase-1 tick.
        existing_path_blocks = set(path.get_all_blocks())
        existing_path_switches = {
            e.resource_id for e in path.resource_sequence
            if e.resource_type == "switch"
        }
        non_revisit_candidates = [
            cand for cand in candidates
            if not self._segment_would_revisit_resources(
                cand.walk, existing_path_blocks, existing_path_switches
            )
        ]
        if not non_revisit_candidates:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id() or None,
                reason=(
                    "all forward candidates would revisit a resource already "
                    "in the path"
                ),
            )
        candidates = non_revisit_candidates

        # design_new.md:931 / :940 / :1045 — like create_path, extend commits to
        # EXACTLY ONE candidate (the shortest forward-continuing extension) and
        # never falls through to a longer alternative route to the same target.
        # When that one extension is blocked the tail returns BLOCKED_NO_GROWTH
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
            # Validate (pretend the extension uses the same path_id).
            validation = self.validate_path(
                segment, path_id, train_id, manual_override=manual_override
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
                # design_new.md ":994". Track it so the tail can return
                # `BLOCKED_NO_GROWTH` if no candidate grows the path.
                had_occupancy_blocker = True
                if acquisition_mode == AcquisitionMode.ALL_OR_NOTHING:
                    continue

                # BEST_EFFORT + occupancy-only: truncate to the longest
                # acquirable prefix of the extension segment, per
                # design_new.md ":938" / ":994".
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
                ok, fail_reason, fail_resource = self._acquire_segment_resources(
                    segment, path_id, train_id, requested_destination_direction,
                    manual_override=manual_override,
                    ordered_walk=ordered_walk,
                )
                if not ok:
                    last_resource = fail_resource
                    last_reason = fail_reason
                    self._rollback_segment(segment, path_id)
                    # Acquire-time failure on a validation-passing segment
                    # is an occupancy-class blocker (a race between
                    # validation and acquire); track it so the tail can
                    # return `BLOCKED_NO_GROWTH` per design_new.md ":994".
                    had_occupancy_blocker = True
                    if acquisition_mode == AcquisitionMode.ALL_OR_NOTHING:
                        continue
                    # Forward manual_override per the operator-initiated
                    # `manual_override` rule in design_new.md (under the
                    # `disabled` and `manual` availability flags canonical
                    # home); the three sibling _best_effort_truncate sites
                    # in this file already do.
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
            # authority_end_block_id is derived from resource_sequence[-1];
            # the resource_sequence.extend(...) above advanced it.
            path.requested_destination_block_id = requested_destination_block_id
            path.requested_destination_direction = requested_destination_direction
            # No status transition here: the path is already ACTIVE (guaranteed
            # by the MISSING_OR_INACTIVE_PATH guard at the top of extend_path).
            # Per design_new.md, `last_status_updated_time` advances only on a
            # real `status` transition, so a successful extend must not re-stamp
            # it.

            # Per design_new.md: when the live authority reaches the
            # requested target, return REACHED_REQUESTED_TARGET; otherwise
            # the live authority grew but is still short of the target, so
            # return TRUNCATED_GREW.
            if path.get_authority_end_block_id() == requested_destination_block_id:
                grown_kind = PathAcquisitionResultKind.REACHED_REQUESTED_TARGET
                grown_reason = "extended to requested target"
                # Reached the target: no longer waiting on anything.
                path.waiting_for_block_id = None
            else:
                grown_kind = PathAcquisitionResultKind.TRUNCATED_GREW
                grown_reason = "extended (truncated short of requested target)"
                # Grew but still short — `last_resource` is the block that
                # stopped further growth, i.e. what the train is waiting on.
                path.waiting_for_block_id = last_resource

            return PathAcquisitionResult(
                success=True,
                result_kind=grown_kind,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id(),
                resource_sequence=list(path.resource_sequence),
                reason=grown_reason,
            )

        ext_reason = last_reason or "All extension paths blocked"
        if last_resource and last_resource not in ext_reason:
            ext_reason = f"{ext_reason} (blocked at {last_resource})"

        # Per design_new.md ":994" / ":997": when BEST_EFFORT extend cannot
        # append any new resource but the existing path is still a legal
        # forward prefix to the requested target (`find_paths` returned
        # forward candidates above) and the only obstruction was an
        # occupancy/reservation/lock conflict, return
        # `success=True / result_kind=BLOCKED_NO_GROWTH`. The stored
        # requested target is updated to reflect this attempt; the
        # `resource_sequence` is left untouched. Route executors use this
        # outcome to keep retrying the same path_id rather than cancelling.
        if (
            acquisition_mode == AcquisitionMode.BEST_EFFORT
            and had_occupancy_blocker
        ):
            path.requested_destination_block_id = requested_destination_block_id
            path.requested_destination_direction = requested_destination_direction
            # No status transition here: the path is already ACTIVE and stays
            # ACTIVE on a BLOCKED_NO_GROWTH hold, so `last_status_updated_time`
            # must not be re-stamped (design_new.md: status transitions only).
            # Held short with no growth — `last_resource` is the occupied/
            # reserved block the train is waiting on.
            path.waiting_for_block_id = last_resource
            return PathAcquisitionResult(
                success=True,
                result_kind=PathAcquisitionResultKind.BLOCKED_NO_GROWTH,
                path_id=path_id,
                requested_destination_block_id=requested_destination_block_id,
                requested_destination_direction=requested_destination_direction,
                authority_end_block_id=path.get_authority_end_block_id(),
                resource_sequence=list(path.resource_sequence),
                reason=ext_reason,
            )

        # Extend failed outright but the existing authority is untouched and
        # still ACTIVE — surface the blocking resource for the "Show stuck"
        # overlay (None when the obstruction was not a specific resource).
        path.waiting_for_block_id = last_resource
        return PathAcquisitionResult(
            success=False,
            failure_kind=PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET,
            path_id=path_id,
            requested_destination_block_id=requested_destination_block_id,
            requested_destination_direction=requested_destination_direction,
            authority_end_block_id=path.get_authority_end_block_id() or None,
            reason=ext_reason,
        )

    @staticmethod
    def _severity_for_initiator(initiator: Optional[str]):
        """Map cancel initiator to notification severity.

        Per design_new.md: "eb" is ERROR (safety-critical); "deviation" is
        WARNING (off-path); all other initiators ("operator_cancelled",
        "power_off", "revoked", None) are INFO.
        """
        from notification_center import Severity as _Sev
        if initiator == "eb":
            return _Sev.ERROR
        if initiator == "deviation":
            return _Sev.WARNING
        return _Sev.INFO

    def cancel_path(
        self,
        path_id: str,
        initiator: Optional[str] = None,
        reason: Optional[str] = None,
        deviation_kind: Optional[str] = None,
    ) -> CancellationResult:
        """v1 cancel_path: release reservations, mark path CANCELLED.

        Per design_new.md "cancel_path() return contract" (lines 925-934):
          * `initiator` is a free-form diagnostic string carried on the
            ``path_cancellation`` notification (default ``None``); planning
            logic must not branch on it.
          * Failure reasons use the canonical strings ``"path not found"``,
            ``"already completed"``, ``"already cancelled"``.
          * On success the result carries ``new_status = PathStatus.CANCELLED``.
        """
        path = self.paths.get(path_id)
        if path is None:
            return CancellationResult(
                success=False,
                path_id=None,
                new_status=None,
                reason="path not found",
            )
        if path.status == PathStatus.COMPLETED:
            return CancellationResult(
                success=False,
                path_id=None,
                new_status=None,
                reason="already completed",
            )
        if path.status == PathStatus.CANCELLED:
            return CancellationResult(
                success=False,
                path_id=None,
                new_status=None,
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

        # Notify route executor (bookkeeping only). Per design_new.md line 1559
        # the diagnostic `initiator` does not cross the callback boundary — it is
        # carried only on the `path_cancellation` notification emitted below.
        train_id = path.train_id
        if train_id:
            route_executor = self._get_route_executor()
            if route_executor is not None:
                route_executor.handle_path_cancellation(train_id, path_id)

        # Emit the canonical `path_cancellation` notification.
        sc = self.simulation_controller
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is not None:
            try:
                # design_new.md fix #15: severity driven by initiator, not hardcoded WARNING.
                # design_new.md fix #16: forward deviation_kind through to notification payload.
                nc.emit(
                    source="PathDispatcher",
                    kind="path_cancellation",
                    severity=self._severity_for_initiator(initiator),
                    event_time=now,
                    train_id=train_id,
                    path_id=path_id,
                    payload={
                        "initiator": initiator,
                        "reason": reason or "",
                        "deviation_kind": deviation_kind,
                    },
                )
            except Exception:
                logger.warning(
                    "NotificationCenter.emit() failed for path_cancellation "
                    "(path=%s, initiator=%s); cancellation itself committed",
                    path_id,
                    initiator,
                    exc_info=True,
                )

        # Per design_new.md ~line 80: cancelled or completed paths remain
        # in `paths_by_id[path_id]` for history. They are no longer active
        # authority because `Path.status` is no longer ACTIVE, so
        # `active_path_id_by_train(...)` skips them and the controller's
        # `received_path_plan` property returns None on its next read.

        return CancellationResult(
            success=True,
            path_id=path_id,
            new_status=PathStatus.CANCELLED,
            reason=reason,
        )

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
    ) -> Tuple[bool, str, Optional[str]]:
        """Reserve all blocks + lock all switches in a PathSegment via the
        ResourceCoordinator facade.

        `manual_override` (default False) is propagated from the calling
        `create_path(...)` / `extend_path(...)` per the `disabled` and `manual`
        availability flags canonical home in design_new.md (under TrackResource).
        `ordered_walk` (sliced to ``segment.tiles`` by the caller) supplies the
        per-block traversal direction without re-deriving it from flat tiles.

        Returns (success, failure_reason, failed_resource_id).
        """
        coordinator = getattr(self.network, "resource_coordinator", None)
        if coordinator is None:
            return (False, "no_resource_coordinator", None)

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
            )

        return (True, "", None)

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

    def _blocks_still_reserved_by(
        self, block_ids: List[str], path_id: str
    ) -> List[str]:
        """Subset of `block_ids` whose future reservation `path_id` still holds.

        Per design_new.md:781, `release_path_block_reservations(...)` is meant
        to release a path's *not-yet-entered* reservation set, and it emits the
        "not reserved for <path_id>" diagnostic for any block whose
        `reserved_by_path` no longer matches. A block leaves the path's future
        set in two normal ways: a transactionally-failed segment acquire never
        took ownership of it (`reserve_blocks_for_path` commits all-or-nothing;
        `reserve_segment` rolls back its own switch locks on block failure), and
        head-entry consumes the reservation as the train physically enters
        (`reserved_by_path` cleared). Handing those blocks to the release call
        anyway just fires the diagnostic — every Phase-1 tick for a BEST_EFFORT
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

    def _rollback_segment(self, segment: PathSegment, path_id: str) -> None:
        """Release any partial holds we may have placed on a segment via the
        ResourceCoordinator facade.

        Only blocks this path still owns are handed to the coordinator (see
        `_blocks_still_reserved_by`). Switch rollback is scoped to this
        segment's primary switches and the flank locks they placed on
        neighbours, via `SwitchManager.release_segment_locks(...)`.

        Resources already committed to this path's live `resource_sequence` are
        excluded: a failed acquire/extend rolls back only the holds it newly
        placed, never the live path's existing authority. A blocked BEST_EFFORT
        `extend_path(...)` re-walks the seam block — the current authority-end,
        already reserved and already in `resource_sequence` — inside its
        extension segment; without this guard the rollback would release that
        seam's reservation while it stays in the authority, leaving a
        not-yet-entered authority block unreserved. design_new.md:1059 / :1062
        require a blocked extend to keep the live path unchanged, and :179 says
        the dispatcher owns the reservation for every not-yet-entered authority
        resource. For `create_path(...)` the path record is not yet in
        `paths_by_id` when its acquire-failure rollback runs, so the exclusion
        set is empty and the full segment is released as before.
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
        switch_ids: List[str] = []
        for pos in segment.required_switches.keys():
            switch_entity = self._resolve_switch_at_position(pos)
            if switch_entity is None:
                continue
            if switch_entity.switch_id in committed_resource_ids:
                continue
            switch_ids.append(switch_entity.switch_id)
        block_ids = [
            bid
            for bid in self._blocks_still_reserved_by(segment.blocks, path_id)
            if bid not in committed_resource_ids
        ]
        coordinator.rollback_segment(
            block_ids=block_ids,
            switch_ids=switch_ids,
            path_id=path_id,
        )

    def _compute_truncate_hint(
        self,
        segment: PathSegment,
        errors: List[ValidationError],
    ) -> Optional[int]:
        """Return the prefix length at which BEST_EFFORT should start the
        backward walk, per design_new.md ":938".

        The index of the first failing occupancy-class block in
        `segment.blocks` is the count of blocks that are still acquirable
        (validation would have flagged any earlier conflict in the same
        pass).

        For occupancy-class errors that carry no `block_id` (e.g.
        `SWITCH_LOCKED` records the failure as a tile position only), find
        the most recent block in `segment.blocks` whose ordered tiles
        appear before the failing switch position in `segment.tiles` and
        use that block's index. This avoids the previous O(N) backward
        walk against a still-locked switch on every BEST_EFFORT dispatch
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
        BEST_EFFORT truncate can land on the block before the contested
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
            block_id = getattr(track, "block_section_id", None)
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
        never restricts the follower.
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
        sc = self.simulation_controller
        route_executor = getattr(sc, "route_executor", None) if sc else None
        if route_executor is None:
            return False
        occ_target = route_executor.find_next_move_target(occ)
        if not occ_target:
            return False
        candidates = self._find_walk_candidates(next_block_id, occ_target, occ)
        if not candidates:
            return False
        occ_blocks = candidates[0].walk.blocks
        try:
            i = occ_blocks.index(next_block_id)
        except ValueError:
            return False
        return i + 1 < len(occ_blocks) and occ_blocks[i + 1] == terminal_block_id

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
        atd = getattr(block, "allowed_travel_directions", None)
        if atd is not None and len(atd) < 2:
            return False  # one-way: no opposing move possible, safe to stop
        # Locate the terminal block's first non-switch tile in path order.
        first_idx: Optional[int] = None
        for idx, tile in enumerate(segment.tiles):
            track = self.network.get_track(*tile)
            if track is None or track.is_switch:
                continue
            if track.block_section_id == terminal_block_id:
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
        per design_new.md ":938" ("use the first failing block/switch as
        the truncation point"). Default `None` preserves the full backward
        walk for callers that have no hint.
        """
        max_prefix = len(segment.blocks) - 1
        if start_at_block_count is None:
            start = max_prefix
        else:
            start = max(2, min(start_at_block_count, max_prefix))
        # design_new.md line 965: resolve this run's waypoint target blocks so the
        # loop never stores a live authority terminating on a waypoint target.
        route_executor = self._get_route_executor()
        waypoint_targets = (
            route_executor.waypoint_target_blocks(train_id)
            if route_executor is not None
            and hasattr(route_executor, "waypoint_target_blocks")
            else set()
        )
        for prefix_block_count in range(start, 1, -1):
            prefix_blocks = segment.blocks[:prefix_block_count]
            # Reversal-trap exclusion (design_new.md best-effort
            # `create_path()`, item 11): if the next, blocked block is occupied
            # by a stopped train whose next route move would leave it back
            # through this terminal block, parking here would trap the two
            # trains nose-to-nose. Truncate to a shorter prefix instead.
            # `prefix_block_count` can exceed the block count when `start` was
            # clamped up to the 2-block floor on a short segment; there is no
            # dropped "next" block then, so there is nothing to trap on.
            if (
                prefix_block_count < len(segment.blocks)
                and self._terminal_traps_reversing_occupant(
                    next_block_id=segment.blocks[prefix_block_count],
                    terminal_block_id=prefix_blocks[-1],
                    train_id=train_id,
                )
            ):
                continue
            # "Stay back one switch" exclusion: never park a truncated
            # best-effort authority on a TWO-WAY block that the path enters
            # straight off a switch. Stopping there fouls the junction and
            # invites a head-on (an opposing move can still come through that
            # switch). A ONE-WAY block (`allowed_travel_directions` restricted
            # to a single direction) is exempt — no opposing traversal is
            # allowed, so trains may pack tight right up to it (e.g. the
            # one-way S2<->S3 corridor). Unlike `_terminal_traps_reversing_
            # occupant`, this is occupancy-independent: it holds back from the
            # junction even before any opposing train has arrived, so it has no
            # timing window. Only applies when a further block was dropped
            # (`prefix_block_count < len(segment.blocks)`); the requested target
            # itself is never reached through this truncation branch.
            if (
                prefix_block_count < len(segment.blocks)
                and self._terminal_fouls_two_way_switch_block(
                    segment, prefix_blocks[-1]
                )
            ):
                continue
            # Compute prefix tile cutoff. Block tiles are kept while their
            # block_section_id is in `prefix_blocks`. A switch tile is kept
            # only when the next non-switch tile in path order belongs to a
            # kept block — i.e. the switch sits inside the prefix, not at
            # the boundary to a dropped block. Per design_new.md ":891" the
            # stored live authority is always block-terminated, and per
            # ":938" truncation must land "before that failed switch", so a
            # boundary switch must not appear in the truncated segment.
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
                            next_track.block_section_id in prefix_blocks
                        )
                        break
                    if next_in_prefix:
                        cutoff_tiles.append(tile)
                        i += 1
                        continue
                    # Boundary switch to a dropped block: truncate here.
                    break
                if track.block_section_id in prefix_blocks:
                    cutoff_tiles.append(tile)
                    i += 1
                else:
                    # Stop at first tile of an excluded block.
                    break
            cutoff_switches: Dict[Tuple[int, int], int] = {}
            for pos, st in segment.required_switches.items():
                if pos in cutoff_tiles:
                    cutoff_switches[pos] = st
            truncated = PathSegment(
                tiles=cutoff_tiles,
                distance_cm=0.0,  # not recomputed
                required_switches=cutoff_switches,
                blocks=prefix_blocks,
            )
            ok, reason, _ = self._acquire_segment_resources(
                truncated, path_id, train_id, direction,
                manual_override=manual_override,
                ordered_walk=self._slice_ordered_walk(ordered_walk, cutoff_tiles),
            )
            if ok:
                # design_new.md line 965: never store an authority whose terminal
                # block is a waypoint target. The loop is longest-first, so a
                # waypoint terminal here means no longer, past-the-waypoint prefix
                # was acquirable — roll back and fail (create_path's tail reports
                # BLOCKED_BEFORE_TARGET) rather than land short of the waypoint.
                if waypoint_targets and prefix_blocks[-1] in waypoint_targets:
                    self._rollback_segment(truncated, path_id)
                    return None
                return truncated
            self._rollback_segment(truncated, path_id)
        return None

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
            if track is None or not track.block_section_id:
                continue
            block_id = track.block_section_id
            if block_id in result:
                continue
            exit_edge = direction_from_delta(dx, dy)
            if exit_edge is None:
                continue
            # block_direction is "forward" or "reverse" — derived from the
            # block's ordered_tiles. We approximate "forward" since the
            # canonical algorithm requires Block.ordered_tiles inspection.
            block = self.network.blocks.get(block_id)
            block_direction = "forward"
            if block is not None and hasattr(block, "ordered_tiles"):
                ordered = block.ordered_tiles or []
                if ordered and ordered[-1] == curr:
                    block_direction = "forward"
                elif ordered and ordered[0] == curr:
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

    def _resolve_switch_entity(self, switch_id: str):
        """Resolve a Switch entity by its switch_id."""
        if hasattr(self.network, "switches") and isinstance(
            self.network.switches, dict
        ):
            return self.network.switches.get(switch_id)
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
            block_id = track.block_section_id
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
                        or track2.block_section_id != block_id
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
                if track2.block_section_id != block_id:
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

        For a full segment this returns the whole list; for a BEST_EFFORT
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
        # matches `Path.requested_destination_direction`; leaving it empty
        # would make completion impossible.
        if i > 0:
            entry = self._tile_entry_edge(tiles, i)
            if entry is not None:
                return OPPOSITE_DIRECTIONS.get(entry)
        return None

    def _flatten_resource_sequence_tiles(
        self, sequence: List[ResourceSequenceEntry]
    ) -> List[PathTileEntry]:
        flat: List[PathTileEntry] = []
        for entry in sequence:
            for t in entry.path_tiles:
                flat.append(t)
        return flat

    def _cab_block_direction(
        self, train_id: str, block
    ) -> Optional[str]:
        """Block-relative ("forward"/"reverse") direction the selected cab
        faces inside ``block``, or None when no cab is selected, the span has
        no geometry, or the outward edge does not map to a block direction.

        Used by same-tile synthetic-path disambiguation (design_new.md:961):
        the head end's outward compass edge is read from the controller span
        and mapped through ``Block.block_direction_for_compass_exit``.
        """
        ctrl = self._get_controller(train_id)
        if ctrl is None:
            return None
        # No cab selected -> no disambiguation signal for the same-tile edge.
        if getattr(ctrl.commands, "cab", None) is None:
            return None
        head_end = ctrl._head_end_label()
        facing = ctrl._orientation_for_head_end(head_end)
        if facing is None:
            return None
        edge = ctrl._direction_to_edge(facing)
        if not edge:
            return None
        return block.block_direction_for_compass_exit(edge)

    def _create_same_block_path(
        self,
        train,
        train_id: str,
        block_id: str,
        requested_destination_direction: str,
        now: float,
    ) -> PathAcquisitionResult:
        """Build a synthetic one-block local authority for same-block
        repositioning (design_new.md:958-:971).

        The requested destination block is already inside the train's current
        occupied span, so dispatcher does not run the candidate search (a block
        cannot be walked to itself) and does not newly reserve the block (the
        train already occupies it as its covered-span seed). It instead derives
        the local terminal frame from the block's own geometry plus
        ``requested_destination_direction``: ``"forward"`` traverses
        ``Block.ordered_tiles`` in increasing order, ``"reverse"`` in decreasing
        order. When the movement stays inside a single tile, the block geometry
        alone cannot anchor a direction, so a selected cab whose direction
        agrees with ``requested_destination_direction`` is required; otherwise
        the request is rejected with a defined reason.
        """
        block = self.network.blocks.get(block_id)
        ordered = list(getattr(block, "ordered_tiles", None) or []) if block else []
        if not block or not ordered:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
                requested_destination_block_id=block_id,
                requested_destination_direction=requested_destination_direction,
                reason=(
                    f"same-block synthetic movement requires block geometry "
                    f"for {block_id}"
                ),
            )

        # Block boundary compass edges for the requested traversal direction.
        # `_resolve_reservation_direction` maps block-relative "forward"/"reverse"
        # to the compass exit edge from `ordered_tiles`; the entry edge into the
        # block is the exit edge of the opposite direction (the other end).
        opposite_direction = (
            "reverse" if requested_destination_direction == "forward" else "forward"
        )
        block_exit_edge = block._resolve_reservation_direction(
            requested_destination_direction
        )
        block_entry_edge = block._resolve_reservation_direction(opposite_direction)

        # Traversal tile order: ordered_tiles increasing for forward, reversed
        # for reverse.
        tiles: List[Tuple[int, int]] = [(int(e[0]), int(e[1])) for e in ordered]
        if requested_destination_direction == "reverse":
            tiles = list(reversed(tiles))

        # Same-tile sub-case: a movement that stays inside one tile cannot be
        # disambiguated from the block geometry alone, so require a selected cab
        # whose direction agrees with the request (design_new.md:961).
        if len(tiles) <= 1:
            cab_direction = self._cab_block_direction(train_id, block)
            if cab_direction is None:
                return PathAcquisitionResult(
                    success=False,
                    failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                    requested_destination_block_id=block_id,
                    requested_destination_direction=requested_destination_direction,
                    reason=(
                        "same-tile synthetic movement requires a selected cab "
                        "direction"
                    ),
                )
            if cab_direction != requested_destination_direction:
                return PathAcquisitionResult(
                    success=False,
                    failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
                    requested_destination_block_id=block_id,
                    requested_destination_direction=requested_destination_direction,
                    reason=(
                        f"same-tile synthetic movement direction "
                        f"{requested_destination_direction!r} disagrees with "
                        f"the selected cab direction {cab_direction!r}"
                    ),
                )

        # Build per-tile entry/exit edges. Interior transitions come from
        # tile-to-tile deltas; the two ends use the block boundary edges.
        path_tiles: List[PathTileEntry] = []
        last_idx = len(tiles) - 1
        for i, (x, y) in enumerate(tiles):
            if i == 0:
                entry_edge = block_entry_edge or ""
            else:
                px, py = tiles[i - 1]
                heading_in = direction_from_delta(x - px, y - py)
                entry_edge = OPPOSITE_EDGE.get(heading_in or "", "") or ""
            if i == last_idx:
                exit_edge = block_exit_edge or ""
            else:
                nx, ny = tiles[i + 1]
                exit_edge = direction_from_delta(nx - x, ny - y) or ""
            path_tiles.append(
                PathTileEntry(
                    x=x,
                    y=y,
                    entry_edge=entry_edge,
                    exit_edge=exit_edge,
                    resource_sequence_index=0,
                )
            )

        path_id = self._generate_path_id(train_id, block_id)
        sequence = [
            ResourceSequenceEntry(
                sequence_index=0,
                resource_type="block",
                resource_id=block_id,
                path_tiles=path_tiles,
                entry_edge=block_entry_edge or "",
                exit_edge=block_exit_edge or "",
            )
        ]
        path = Path(
            path_id=path_id,
            status=PathStatus.ACTIVE,
            train_id=train_id,
            requested_destination_block_id=block_id,
            requested_destination_direction=requested_destination_direction,
            resource_sequence=sequence,
            current_resource_index=0,
            last_status_updated_time=now,
        )
        self.paths[path_id] = path
        return PathAcquisitionResult(
            success=True,
            result_kind=PathAcquisitionResultKind.REACHED_REQUESTED_TARGET,
            path_id=path_id,
            requested_destination_block_id=block_id,
            requested_destination_direction=requested_destination_direction,
            authority_end_block_id=path.get_authority_end_block_id(),
            resource_sequence=list(path.resource_sequence),
            reason="same-block synthetic authority",
        )

    def _build_path_from_segment(
        self,
        path_id: str,
        train_id: str,
        segment: PathSegment,
        requested_destination_block_id: str,
        requested_destination_direction: str,
        now: float,
        ordered_walk: Optional[List[GraphWalkStep]] = None,
    ) -> Path:
        """Construct a v1 Path entity from a successfully acquired PathSegment.

        ``ordered_walk`` (sliced to ``segment.tiles`` by the caller) carries the
        per-step geometry used to build ``resource_sequence`` without re-deriving
        edges from flat tiles.
        """
        sequence = self._segment_to_resource_sequence(
            segment, base_index=0, skip_first_block=False, ordered_walk=ordered_walk
        )
        # `path_tiles` is a derived `@property` over `resource_sequence`; do
        # not pass it as a constructor field. authority_end_block_id is also
        # derived from resource_sequence[-1].
        path = Path(
            path_id=path_id,
            status=PathStatus.ACTIVE,
            train_id=train_id,
            requested_destination_block_id=requested_destination_block_id,
            requested_destination_direction=requested_destination_direction,
            resource_sequence=sequence,
            current_resource_index=0,
            last_status_updated_time=now,
        )
        return path

    # ========================================================================
    # Phase 3 progress consumer
    # ========================================================================

    def on_controller_progress(self, report: ControllerProgressReport) -> None:
        """Phase 3 progress consumer."""
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
                # design_new.md fix #16: forward deviation_kind from report to cancel_path.
                self.cancel_path(
                    active_path.path_id,
                    initiator="deviation",
                    reason=report.deviation_reason or "",
                    deviation_kind=report.deviation_kind,
                )
            # design_new.md fix #2: do NOT return here — fall through so that
            # tail_vacated_resources for already-occupied resources still fire.

        # design_new.md fix #3: skip callbacks on fatal; fix #2: always run tail-vacates.
        if not fatal_in_this_report and not report.deviation:
            if active_path is not None:
                for event in list(report.entered_resources or []):
                    if event.sequence_index is not None:
                        if event.sequence_index > active_path.current_resource_index:
                            active_path.current_resource_index = event.sequence_index
                    self._fire_route_advanced_callback(
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

    def _fire_route_advanced_callback(
        self, train_id: str, path_id: str, event: ResourceEvent
    ) -> None:
        route_executor = self._get_route_executor()
        if route_executor is None:
            return
        # Populate the block-relative traversal_direction the head-advance
        # callback matches a waypoint against (design_new.md:462, 465). For a
        # block resource it is computed by the canonical block-direction helper
        # from the entered block's `resource_sequence` entry exit_edge against
        # `Block.ordered_tiles`; for switches it stays None. The controller's
        # ResourceEvent.exit_edge is None for entered blocks, so the direction
        # is derived from the dispatcher-owned path geometry, not the event.
        if event.resource_type == "block" and event.traversal_direction is None:
            event.traversal_direction = self._block_traversal_direction(
                path_id, event
            )
        route_executor.handle_train_advanced(train_id, path_id, event)

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

    def try_complete_active_path(
        self, train_id: str, event_time: float
    ) -> PathCompletionResult:
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

        stop_check = getattr(controller, "is_stopped_at_authority_end", None)
        if not callable(stop_check) or not stop_check():
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="controller not stopped at authority end",
            )

        if not path.get_authority_end_block_id():
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="path missing authority end",
            )
        if path.get_authority_end_block_id() != path.requested_destination_block_id:
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="authority end has not reached requested destination",
            )

        # Per design_new.md "Active-path completion procedure": completion
        # accepts only when the terminal authority traversal direction
        # equals `Path.requested_destination_direction`. Compute the
        # terminal direction from the final `resource_sequence` block entry
        # against the block's `ordered_tiles` and compare.
        requested_direction = (path.requested_destination_direction or "").strip().lower()
        if requested_direction not in ("forward", "reverse"):
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="path missing requested destination direction",
            )
        terminal_entry = path.resource_sequence[-1] if path.resource_sequence else None
        terminal_block = self.network.blocks.get(path.get_authority_end_block_id())
        if terminal_entry is None or terminal_block is None:
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="terminal authority resource missing",
            )
        terminal_block_direction = terminal_block.block_direction_for_compass_exit(
            terminal_entry.exit_edge
        )
        # Per design_new.md line 687, block-relative `"forward"` / `"reverse"`
        # are defined against the increasing/decreasing direction of
        # `Block.ordered_tiles`. A single-tile destination block has only
        # one entry in `ordered_tiles`, so there is no increasing direction
        # to anchor against and the pathfinder's terminal-direction derivation
        # (`RailwayPathfinder._terminal_direction`) and the block's own
        # `block_direction_for_compass_exit` use independent conventions that
        # need not agree. The head-in-destination block + authority-end
        # equality checks above already pin the physical arrival, so for
        # single-tile destinations the direction match is skipped.
        if (
            terminal_block_direction != requested_direction
            and len(terminal_block.ordered_tiles) > 1
        ):
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason=(
                    "terminal authority direction does not match requested "
                    "destination direction"
                ),
            )

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

        # Notify controller.
        on_complete = getattr(controller, "on_path_complete", None)
        if callable(on_complete):
            on_complete()

        # Deliver completion through the SimulationController surface, which
        # delegates to RouteExecutor.handle_path_completion(...) and emits the
        # canonical `path_completion` notification (design_new.md:240, 415,
        # 1553). Fall back to a direct RouteExecutor call only when no
        # SimulationController is wired (isolated unit tests) — there is no
        # NotificationCenter to emit through in that case.
        sc = self.simulation_controller
        sc_complete = getattr(sc, "handle_path_completion", None) if sc else None
        if callable(sc_complete):
            sc_complete(
                train_id,
                path_id,
                path.get_authority_end_block_id(),
                path.requested_destination_direction,
                event_time,
            )
        else:
            route_executor = self._get_route_executor()
            if route_executor is not None:
                route_executor.handle_path_completion(
                    train_id,
                    path_id,
                    path.get_authority_end_block_id(),
                    path.requested_destination_direction,
                    event_time,
                )

        # Per design_new.md ~line 80: completed paths remain in
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

        Per design_new.md ~line 80 cancelled/completed rows remain in
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

    def get_active_path_progress(self, train_id: str) -> Optional[Dict[str, Any]]:
        """Read-only projection of dispatcher state for a single train.

        Used by the API/UI layer per design_new.md "Data Flow To UI". Returns
        ``None`` when no active path exists for the train.
        """
        path = self.get_active_path(train_id)
        if path is None:
            return None
        return {
            "path_id": path.path_id,
            "status": path.status.value,
            "current_resource_index": path.current_resource_index,
            "authority_end_block_id": path.get_authority_end_block_id(),
            "requested_destination_block_id": path.requested_destination_block_id,
            "requested_destination_direction": path.requested_destination_direction,
            # Diagnostic for the "Show stuck" overlay: the block this train is
            # held waiting on (occupied/reserved ahead), or None when not stuck.
            "waiting_for_block_id": path.waiting_for_block_id,
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

    def get_paths_using_switch(self, position: Tuple[int, int]) -> List[Path]:
        """Return ACTIVE paths that traverse a switch at the given tile position.

        Terminal (COMPLETED/CANCELLED) rows retained in `paths_by_id` for
        history are not returned, since they no longer hold authority over
        the switch.
        """
        switch = self._resolve_switch_at_position(position)
        if switch is None:
            return []
        switch_id = getattr(switch, "switch_id", None)
        if switch_id is None:
            return []
        return [
            p
            for p in self.paths.values()
            if p.status == PathStatus.ACTIVE
            and switch_id in p.get_all_switches()
        ]

