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
  current_resource_index, cancellation_kind, last_status_updated_time, status.

There is no v0 start_block_id / end_block_id / block_sequence /
current_block_index / priority / dispatcher_id / switch_positions /
flank_protection_switches / overlap_blocks / approach_locked_blocks /
blocks_cleared. All call sites here use the v1 fields.

PathSegment (geometry-only candidate from BFS) and the legacy ValidationResult
/ ValidationError schemas are imported from runtime_payloads.py — Wave 1 moved
them out of path.py.
"""

from collections import deque
from datetime import datetime
import time
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
    DIRECTION_OFFSETS,
    OPPOSITE_DIRECTIONS,
)
from track import OPPOSITE_EDGE
from model_scale import kmh_to_cm_per_sec
from runtime_payloads import (
    AcquisitionMode,
    CancellationResult,
    Command,
    ControllerProgressReport,
    ConflictResolution,
    ConflictResolutionType,
    DEFAULT_PATH_PRIORITY,
    ExtensionResult,
    PathAcquisitionFailureKind,
    PathAcquisitionResult,
    PathAcquisitionResultKind,
    PathCancellationKind,
    PathCompletionResult,
    PathResult,
    PathSegment,
    PendingPathCancelRequest,
    ResourceEvent,
    ValidationError,
    ValidationErrorType,
    ValidationResult,
)
from block_manager import ReservedBlockTraversal

if TYPE_CHECKING:
    from network import RailwayNetwork
    from block import Block
    from track import TrackSegment


# Edge offsets for navigation
EDGE_OFFSETS = DIRECTION_OFFSETS


class PathDispatcher:
    """
    Manages path creation, validation, lifecycle, and progress consumption.

    Public v1 API:
        create_path(train_id, requested_destination_block_id,
                    requested_destination_direction, acquisition_mode)
        extend_path(path_id, requested_destination_block_id,
                    requested_destination_direction, acquisition_mode)
        cancel_path(path_id, cancellation_kind, reason)
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
        get_and_clear_completion_events()
        update_approach_locking(path_id)  # no-op in v1
    """

    def __init__(self, network: "RailwayNetwork"):
        self.network = network
        self.paths: Dict[str, Path] = {}
        self.path_sequence_counter: int = 0
        self.completed_path_events: List[Dict[str, Any]] = []
        # Back-reference to SimulationController, wired by the runtime when
        # available. Used to resolve TrainController instances and the
        # NotificationCenter for progress callbacks.
        self.simulation_controller: Optional[Any] = None

    @property
    def active_paths(self) -> Dict[str, Path]:
        """Legacy alias used by older call sites that still say
        `dispatcher.active_paths`. Returns the same dict as `self.paths`.
        """
        return self.paths

    @property
    def paths_by_id(self) -> Dict[str, Path]:
        """Canonical alias from design_new.md."""
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

    def generate_path_id(self, start_block_id: str, end_block_id: str) -> str:
        """Legacy wrapper for callers that still pass start/end."""
        self.path_sequence_counter += 1
        start_part = self._get_block_display_name(start_block_id)
        end_part = self._get_block_display_name(end_block_id)
        return f"PATH_{start_part}_TO_{end_part}_{self.path_sequence_counter:03d}"

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

    def _get_route_executor(self):
        sc = self.simulation_controller
        if sc is not None:
            return getattr(sc, "route_executor", None)
        # Fallback: legacy wiring through power_controller.
        power_controller = getattr(self.network, "power_controller", None)
        if power_controller is None:
            return None
        sim = getattr(power_controller, "_sim_controller", None)
        if sim is None:
            return None
        return getattr(sim, "route_executor", None)

    def _get_controller(self, train_id: str):
        sc = self.simulation_controller
        if sc is not None:
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
        # Legacy wiring fallback.
        power_controller = getattr(self.network, "power_controller", None)
        if power_controller is None:
            return None
        sim = getattr(power_controller, "_sim_controller", None)
        if sim is None:
            return None
        getter = getattr(sim, "get_controller", None)
        if callable(getter):
            try:
                return getter(train_id)
            except Exception:
                return None
        return None

    def _get_current_block_id_for_train(self, train) -> Optional[str]:
        """Resolve the train's current block (head tile)."""
        if train is None:
            return None
        position = getattr(train, "position", None)
        if position is None:
            return None
        head_tile = getattr(position, "head_tile", None)
        if not head_tile:
            return None
        track = self.network.get_track(*head_tile)
        if track is None:
            return None
        return track.block_section_id

    # ========================================================================
    # Endpoint Discovery
    # ========================================================================

    def get_reachable_endpoints(
        self, start_block_id: str, include_stations: bool = True
    ) -> List[str]:
        """All blocks/stations reachable from a starting block via BFS."""
        if start_block_id not in self.network.blocks:
            return []

        start_positions = self.network.get_block_positions(start_block_id)
        if not start_positions:
            return []

        reachable_blocks: Set[str] = set()
        visited: Set[Tuple[int, int, str]] = set()

        for pos in start_positions:
            track = self.network.get_track(*pos)
            if not track:
                continue
            for edge in track.edges:
                neighbor = self.network.get_neighbor(*pos, edge)
                if neighbor and neighbor.block_section_id != start_block_id:
                    queue = deque([(pos[0], pos[1], edge)])
                    while queue:
                        curr_x, curr_y, exit_edge = queue.popleft()
                        state = (curr_x, curr_y, exit_edge)
                        if state in visited:
                            continue
                        visited.add(state)
                        if len(visited) > 1000:
                            break
                        offset = EDGE_OFFSETS.get(exit_edge)
                        if not offset:
                            continue
                        dx, dy = offset
                        next_x, next_y = curr_x + dx, curr_y + dy
                        next_track = self.network.get_track(next_x, next_y)
                        if not next_track:
                            continue
                        next_block_id = next_track.block_section_id
                        if next_block_id and next_block_id != start_block_id:
                            reachable_blocks.add(next_block_id)
                        entry_edge = OPPOSITE_EDGE.get(exit_edge)
                        if not entry_edge or entry_edge not in next_track.edges:
                            continue
                        all_exits = next_track.get_exits(
                            entry_edge, use_switch_state=False
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
        return result

    def get_reachable_endpoints_for_extension(
        self, path_id: str, include_stations: bool = True
    ) -> Tuple[List[str], Optional[str]]:
        """All blocks reachable in the FORWARD direction from path's end."""
        path = self.paths.get(path_id)
        if not path:
            return ([], f"Path {path_id} not found")
        if not path.can_be_extended():
            return ([], f"Path cannot be extended (status: {path.status.value})")
        if not path.path_tiles or len(path.path_tiles) < 2:
            return ([], "Path has insufficient tiles to determine direction")

        last_tile = path.path_tiles[-1]
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
                offset = EDGE_OFFSETS.get(curr_exit)
                if not offset:
                    continue
                dx, dy = offset
                next_x, next_y = curr_x + dx, curr_y + dy
                next_track = self.network.get_track(next_x, next_y)
                if not next_track:
                    continue
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
        self, start_block_id: str, end_block_id: str, train_id: Optional[str] = None
    ) -> List[PathSegment]:
        """BFS through the network to find all PathSegment candidates."""
        if start_block_id not in self.network.blocks:
            return []
        if end_block_id not in self.network.blocks:
            return []

        start_positions = self.network.get_block_positions(start_block_id)
        if not start_positions:
            return []

        paths: List[PathSegment] = []
        for start_pos in start_positions:
            track = self.network.get_track(*start_pos)
            if not track:
                continue
            for edge in track.edges:
                neighbor = self.network.get_neighbor(*start_pos, edge)
                if neighbor and neighbor.block_section_id != start_block_id:
                    found_paths = self._bfs_find_paths(
                        start_pos, edge, start_block_id, end_block_id
                    )
                    paths.extend(found_paths)

        unique_paths = self._deduplicate_paths(paths)
        unique_paths.sort(key=lambda p: p.distance_cm)
        return unique_paths

    def _bfs_find_paths(
        self,
        start_pos: Tuple[int, int],
        start_exit: str,
        start_block_id: str,
        end_block_id: str,
    ) -> List[PathSegment]:
        paths: List[PathSegment] = []
        queue: deque = deque()
        queue.append(
            (
                start_pos[0],
                start_pos[1],
                start_exit,
                [start_pos],
                [start_block_id],
                {},
                0.0,
            )
        )
        visited: Set[Tuple[int, int, str, frozenset]] = set()

        while queue:
            curr_x, curr_y, exit_edge, path_tiles, blocks, switches, distance = (
                queue.popleft()
            )
            visited_key = (curr_x, curr_y, exit_edge, frozenset(blocks))
            if visited_key in visited:
                continue
            visited.add(visited_key)
            if len(path_tiles) > 200:
                continue
            offset = EDGE_OFFSETS.get(exit_edge)
            if not offset:
                continue
            dx, dy = offset
            next_x, next_y = curr_x + dx, curr_y + dy
            next_track = self.network.get_track(next_x, next_y)
            if not next_track:
                continue
            segment_distance = next_track.length * 100.0
            new_distance = distance + segment_distance
            new_path_tiles = path_tiles + [(next_x, next_y)]
            new_blocks = list(blocks)
            next_block_id = next_track.block_section_id
            if next_block_id and next_block_id not in new_blocks:
                new_blocks.append(next_block_id)
            if next_block_id == end_block_id:
                paths.append(
                    PathSegment(
                        tiles=new_path_tiles,
                        distance_cm=new_distance,
                        required_switches=dict(switches),
                        blocks=new_blocks,
                    )
                )
                continue
            entry_edge = OPPOSITE_EDGE.get(exit_edge)
            if not entry_edge or entry_edge not in next_track.edges:
                continue
            all_exits = next_track.get_exits(entry_edge, use_switch_state=False)
            if not all_exits:
                continue
            if next_track.is_switch:
                for next_exit in all_exits:
                    new_switches = dict(switches)
                    required_state = next_track.get_required_switch_state(
                        entry_edge, next_exit
                    )
                    if required_state is not None:
                        new_switches[(next_x, next_y)] = required_state
                    queue.append(
                        (
                            next_x,
                            next_y,
                            next_exit,
                            new_path_tiles,
                            new_blocks,
                            new_switches,
                            new_distance,
                        )
                    )
            else:
                for next_exit in all_exits:
                    queue.append(
                        (
                            next_x,
                            next_y,
                            next_exit,
                            new_path_tiles,
                            new_blocks,
                            dict(switches),
                            new_distance,
                        )
                    )
        return paths

    def _deduplicate_paths(self, paths: List[PathSegment]) -> List[PathSegment]:
        seen: Set[Any] = set()
        unique = []
        for p in paths:
            key = (tuple(p.blocks), tuple(sorted(p.required_switches.items())))
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    # ========================================================================
    # Validation
    # ========================================================================

    def validate_path(
        self,
        path: PathSegment,
        path_id: str,
        train_id: Optional[str] = None,
    ) -> ValidationResult:
        """Validate a PathSegment for resource availability."""
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
            if not track.can_lock_for_path(path_id, required_state):
                lock_info = track.get_lock_info()
                errors.append(
                    ValidationError(
                        type=ValidationErrorType.SWITCH_LOCKED,
                        position=pos,
                        locked_by_path=lock_info["path_id"] if lock_info else None,
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
        acquisition_mode: AcquisitionMode = AcquisitionMode.ALL_OR_NOTHING,
        # Legacy kwargs accepted for transitional compatibility from
        # demo/test code that hasn't migrated yet:
        start_block_id: Optional[str] = None,
        end_block_id: Optional[str] = None,
        priority: int = DEFAULT_PATH_PRIORITY,
        dispatcher_id: str = "system",
    ) -> PathAcquisitionResult:
        """v1 create_path: build authority for a train to a destination block.

        Args:
            train_id: Train requesting authority. Required in v1.
            requested_destination_block_id: Caller's target block.
            requested_destination_direction: "forward" or "reverse".
            acquisition_mode: ALL_OR_NOTHING or BEST_EFFORT.

        Returns:
            PathAcquisitionResult.
        """
        # Legacy fallback: end_block_id provided in old kwargs form.
        if not requested_destination_block_id and end_block_id:
            requested_destination_block_id = end_block_id

        # Validate direction.
        if requested_destination_direction not in ("forward", "reverse"):
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_DIRECTION,
                reason=f"Invalid direction: {requested_destination_direction!r}",
            )

        if not train_id:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_TRAIN,
                reason="train_id required in v1",
            )

        train = self._get_train(train_id)
        if train is None:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_TRAIN,
                reason=f"Train {train_id} not found",
            )

        if not requested_destination_block_id:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_DESTINATION,
                reason="requested_destination_block_id required",
            )
        if requested_destination_block_id not in self.network.blocks:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_DESTINATION,
                reason=f"Unknown block {requested_destination_block_id}",
            )

        # One ACTIVE path per train.
        existing = self.get_active_path(train_id)
        if existing is not None:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.ALREADY_HAS_PATH,
                path_id=existing.path_id,
                reason=f"Train {train_id} already has active path {existing.path_id}",
            )

        # Resolve start block from train's current head tile.
        start_block_id_resolved = (
            start_block_id or self._get_current_block_id_for_train(train)
        )
        if not start_block_id_resolved:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_PATH_GEOMETRY,
                reason=f"Cannot resolve current block for train {train_id}",
            )

        # Find geometry candidates.
        candidates = self.find_paths(
            start_block_id_resolved, requested_destination_block_id, train_id
        )
        if not candidates:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_PATH_GEOMETRY,
                reason="No path exists between start and destination",
            )

        path_id = self._generate_path_id(train_id, requested_destination_block_id)
        now = self._now()

        # Try each candidate (shortest first).
        last_failed_resource_id: Optional[str] = None
        last_reason: str = ""
        for segment in candidates:
            validation = self.validate_path(segment, path_id, train_id)
            if not validation.valid:
                # Track first conflicting block for diagnostic.
                for err in validation.errors:
                    if err.block_id and not last_failed_resource_id:
                        last_failed_resource_id = err.block_id
                        last_reason = err.message
                continue

            # Acquire resources.
            acquire_ok, fail_reason, fail_resource = self._acquire_segment_resources(
                segment, path_id, train_id, requested_destination_direction
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
                    segment, path_id, train_id, requested_destination_direction
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
                )
                self.paths[path_id] = path
                self._populate_train_reserved_blocks(train_id, path)
                return PathAcquisitionResult(
                    success=True,
                    result_kind=PathAcquisitionResultKind.TRUNCATED_GREW,
                    path_id=path_id,
                    authority_end_block_id=path.authority_end_block_id,
                    granted_resource_sequence=list(path.resource_sequence),
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
            )
            self.paths[path_id] = path
            self._populate_train_reserved_blocks(train_id, path)
            return PathAcquisitionResult(
                success=True,
                result_kind=PathAcquisitionResultKind.CREATED,
                path_id=path_id,
                authority_end_block_id=path.authority_end_block_id,
                granted_resource_sequence=list(path.resource_sequence),
                reason="created",
            )

        # All candidates failed.
        return PathAcquisitionResult(
            success=False,
            result_kind=PathAcquisitionResultKind.BLOCKED_NO_GROWTH,
            failure_kind=PathAcquisitionFailureKind.RESOURCE_HELD,
            failed_resource_id=last_failed_resource_id,
            reason=last_reason or "All candidate paths blocked",
        )

    def extend_path(
        self,
        path_id: str = "",
        requested_destination_block_id: str = "",
        requested_destination_direction: str = "forward",
        acquisition_mode: AcquisitionMode = AcquisitionMode.ALL_OR_NOTHING,
        # Legacy
        new_end_block_id: Optional[str] = None,
    ) -> PathAcquisitionResult:
        """v1 extend_path: append more authority to an existing ACTIVE path."""
        if not requested_destination_block_id and new_end_block_id:
            requested_destination_block_id = new_end_block_id

        if requested_destination_direction not in ("forward", "reverse"):
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_DIRECTION,
                reason=f"Invalid direction: {requested_destination_direction!r}",
            )

        path = self.paths.get(path_id)
        if path is None or not path.can_be_extended():
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_DESTINATION,
                path_id=path_id,
                reason=f"Path {path_id} not extendable",
            )

        if not requested_destination_block_id:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_DESTINATION,
                path_id=path_id,
                reason="requested_destination_block_id required",
            )

        existing_blocks = path.get_all_blocks()
        if requested_destination_block_id in existing_blocks:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.INVALID_DESTINATION,
                path_id=path_id,
                reason=f"{requested_destination_block_id} is already in path",
            )

        train_id = path.train_id
        train = self._get_train(train_id) if train_id else None
        if train is None:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_TRAIN,
                path_id=path_id,
                reason="path has no train",
            )

        # Find extension geometry from current authority end.
        start_block_id = path.authority_end_block_id
        candidates = self.find_paths(
            start_block_id, requested_destination_block_id, train_id
        )
        if not candidates:
            return PathAcquisitionResult(
                success=False,
                failure_kind=PathAcquisitionFailureKind.NO_PATH_GEOMETRY,
                path_id=path_id,
                reason=f"No forward path from {start_block_id} to {requested_destination_block_id}",
            )

        now = self._now()
        last_reason = ""
        last_resource = None
        for segment in candidates:
            # Validate (pretend the extension uses the same path_id).
            validation = self.validate_path(segment, path_id, train_id)
            if not validation.valid:
                for err in validation.errors:
                    if err.block_id and last_resource is None:
                        last_resource = err.block_id
                        last_reason = err.message
                continue
            ok, fail_reason, fail_resource = self._acquire_segment_resources(
                segment, path_id, train_id, requested_destination_direction
            )
            if not ok:
                last_resource = fail_resource
                last_reason = fail_reason
                self._rollback_segment(segment, path_id)
                continue

            # Append to existing path.resource_sequence (skipping the first
            # entry which is already present as authority_end).
            base_index = len(path.resource_sequence)
            new_entries = self._segment_to_resource_sequence(
                segment, base_index, skip_first_block=True
            )
            path.resource_sequence.extend(new_entries)
            # Rebuild path_tiles.
            path.path_tiles = self._flatten_resource_sequence_tiles(
                path.resource_sequence
            )
            path.authority_end_block_id = requested_destination_block_id
            path.requested_destination_block_id = requested_destination_block_id
            path.requested_destination_direction = requested_destination_direction
            path._set_status(PathStatus.ACTIVE, now)

            self._populate_train_reserved_blocks(train_id, path)
            return PathAcquisitionResult(
                success=True,
                result_kind=PathAcquisitionResultKind.EXTENDED,
                path_id=path_id,
                authority_end_block_id=path.authority_end_block_id,
                granted_resource_sequence=list(path.resource_sequence),
                reason="extended",
            )

        return PathAcquisitionResult(
            success=False,
            result_kind=PathAcquisitionResultKind.BLOCKED_NO_GROWTH,
            failure_kind=PathAcquisitionFailureKind.RESOURCE_HELD,
            path_id=path_id,
            failed_resource_id=last_resource,
            reason=last_reason or "All extension paths blocked",
        )

    def cancel_path(
        self,
        path_id: str,
        cancellation_kind: Optional[PathCancellationKind] = None,
        reason: Optional[str] = None,
        force: bool = False,
    ) -> CancellationResult:
        """v1 cancel_path: release reservations, mark path CANCELLED."""
        path = self.paths.get(path_id)
        if path is None:
            return CancellationResult(
                success=False,
                path_id=path_id,
                cancellation_kind=cancellation_kind,
                reason=reason or "path_not_found",
                notes="path_not_found",
            )
        if path.status != PathStatus.ACTIVE:
            return CancellationResult(
                success=False,
                path_id=path_id,
                cancellation_kind=cancellation_kind,
                reason=reason or f"path_terminal_{path.status.value}",
                train_id=path.train_id,
                notes="terminal",
            )

        kind = cancellation_kind or PathCancellationKind.OPERATOR_CANCELLED
        now = self._now()

        # Release block reservations.
        all_blocks = path.get_all_blocks()
        block_manager = getattr(self.network, "block_manager", None)
        released_blocks: List[str] = []
        if block_manager is not None and hasattr(
            block_manager, "release_path_block_reservations"
        ):
            try:
                block_manager.release_path_block_reservations(
                    all_blocks, path_id, completed=False
                )
                released_blocks = list(all_blocks)
            except Exception:
                pass
        else:
            # Fallback: clear reserved primitives directly.
            for bid in all_blocks:
                blk = self.network.blocks.get(bid)
                if blk is not None and getattr(blk, "reserved_by_path", None) == path_id:
                    blk.reserved_by_path = None
                    blk.reserved_direction = None
                    blk.reserved_for_train = None
                    released_blocks.append(bid)

        # Release switch locks.
        released_switches: List[str] = []
        for switch_id in path.get_all_switches():
            switch = self._resolve_switch_entity(switch_id)
            if switch is None:
                continue
            # Iterate over a copy of locks (mutating during iteration).
            keys_to_release = [
                key for key in list(switch.locks.keys()) if key[0] == path_id
            ]
            for key in keys_to_release:
                _, claim_role, source_id = key
                if hasattr(switch, "release_lock"):
                    switch.release_lock(path_id, claim_role, source_id)
            if keys_to_release:
                released_switches.append(switch_id)

        # Mark path CANCELLED.
        path.cancellation_kind = kind
        path._set_status(PathStatus.CANCELLED, now)

        # Notify route executor.
        train_id = path.train_id
        if train_id:
            route_executor = self._get_route_executor()
            if route_executor is not None:
                handler = getattr(route_executor, "handle_path_cancellation", None)
                if callable(handler):
                    try:
                        handler(train_id, path_id, kind)
                    except TypeError:
                        try:
                            handler(train_id, path_id)
                        except Exception:
                            pass
                    except Exception:
                        pass

        # Drop from registry. (Per design_new.md the registry could keep
        # terminal rows for history; current behaviour matches old code.)
        if path_id in self.paths:
            del self.paths[path_id]

        return CancellationResult(
            success=True,
            path_id=path_id,
            cancellation_kind=kind,
            reason=reason or kind.value,
            released_block_ids=released_blocks,
            released_switch_ids=released_switches,
            train_id=train_id,
            notes="ok",
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
    ) -> Tuple[bool, str, Optional[str]]:
        """Reserve all blocks + lock all switches in a PathSegment.

        Returns (success, failure_reason, failed_resource_id).
        """
        # Compute traversal directions per block from path tiles.
        block_traversals = self._compute_block_traversals(segment)
        block_manager = getattr(self.network, "block_manager", None)
        if block_manager is None or not hasattr(
            block_manager, "reserve_blocks_for_path"
        ):
            return (False, "no_block_manager", None)

        # Skip the first block in the segment if it's the train's current
        # block (which the train already physically occupies). The reservation
        # API rejects occupied blocks.
        future_blocks = [
            bid for bid in segment.blocks if not self._train_already_in(train_id, bid)
        ]
        if future_blocks:
            traversal_map = {
                bid: block_traversals[bid]
                for bid in future_blocks
                if bid in block_traversals
            }
            try:
                result = block_manager.reserve_blocks_for_path(
                    future_blocks, path_id, train_id, traversal_map
                )
            except Exception as exc:
                return (False, str(exc), None)
            if not result.success:
                return (
                    False,
                    result.failure_reason or "reserve_failed",
                    result.failed_block_id,
                )

        # Lock switches.
        for pos, locked_state in segment.required_switches.items():
            switch_entity = self._resolve_switch_at_position(pos)
            if switch_entity is None:
                continue
            ok = switch_entity.acquire_lock(
                path_id=path_id,
                claim_role="primary",
                train_id=train_id,
                locked_state=int(locked_state),
                source_switch_id=None,
            )
            if not ok:
                return (
                    False,
                    "switch_locked",
                    getattr(switch_entity, "switch_id", None) or f"{pos[0]},{pos[1]}",
                )

        return (True, "", None)

    def _train_already_in(self, train_id: str, block_id: str) -> bool:
        train = self._get_train(train_id)
        if train is None:
            return False
        position = getattr(train, "position", None)
        if position is None:
            return False
        occupied = getattr(position, "occupied_blocks", None) or []
        return block_id in occupied

    def _rollback_segment(self, segment: PathSegment, path_id: str) -> None:
        """Release any partial holds we may have placed on a segment."""
        block_manager = getattr(self.network, "block_manager", None)
        if block_manager is not None and hasattr(
            block_manager, "release_path_block_reservations"
        ):
            try:
                block_manager.release_path_block_reservations(
                    list(segment.blocks), path_id, completed=False
                )
            except Exception:
                pass
        for pos in segment.required_switches.keys():
            switch_entity = self._resolve_switch_at_position(pos)
            if switch_entity is None:
                continue
            keys_to_release = [
                key for key in list(switch_entity.locks.keys()) if key[0] == path_id
            ]
            for key in keys_to_release:
                _, claim_role, source_id = key
                if hasattr(switch_entity, "release_lock"):
                    switch_entity.release_lock(path_id, claim_role, source_id)

    def _best_effort_truncate(
        self,
        segment: PathSegment,
        path_id: str,
        train_id: str,
        direction: str,
    ) -> Optional[PathSegment]:
        """Try to acquire a strictly shorter prefix of `segment`."""
        # Walk backwards from the full segment and try shorter prefixes.
        for prefix_block_count in range(len(segment.blocks) - 1, 1, -1):
            prefix_blocks = segment.blocks[:prefix_block_count]
            # Compute prefix tile cutoff: include all tiles whose track
            # belongs to one of `prefix_blocks` (or to a switch on the way).
            cutoff_tiles: List[Tuple[int, int]] = []
            for tile in segment.tiles:
                track = self.network.get_track(*tile)
                if track is None:
                    continue
                if track.is_switch:
                    cutoff_tiles.append(tile)
                    continue
                if track.block_section_id in prefix_blocks:
                    cutoff_tiles.append(tile)
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
                truncated, path_id, train_id, direction
            )
            if ok:
                return truncated
            self._rollback_segment(truncated, path_id)
        return None

    def _compute_block_traversals(
        self, segment: PathSegment
    ) -> Dict[str, ReservedBlockTraversal]:
        """Compute (exit_edge, block_direction) for each block in segment."""
        result: Dict[str, ReservedBlockTraversal] = {}
        if not segment.tiles or len(segment.tiles) < 2:
            return result
        for i in range(len(segment.tiles) - 1):
            curr = segment.tiles[i]
            nxt = segment.tiles[i + 1]
            track = self.network.get_track(*curr)
            if track is None or not track.block_section_id:
                continue
            block_id = track.block_section_id
            if block_id in result:
                continue
            dx = nxt[0] - curr[0]
            dy = nxt[1] - curr[1]
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
    ) -> List[ResourceSequenceEntry]:
        """Convert a PathSegment into ResourceSequenceEntry list."""
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
        if i + 1 >= len(tiles):
            return None
        curr = tiles[i]
        nxt = tiles[i + 1]
        dx = nxt[0] - curr[0]
        dy = nxt[1] - curr[1]
        return direction_from_delta(dx, dy)

    def _flatten_resource_sequence_tiles(
        self, sequence: List[ResourceSequenceEntry]
    ) -> List[PathTileEntry]:
        flat: List[PathTileEntry] = []
        for entry in sequence:
            for t in entry.path_tiles:
                flat.append(t)
        return flat

    def _build_path_from_segment(
        self,
        path_id: str,
        train_id: str,
        segment: PathSegment,
        requested_destination_block_id: str,
        requested_destination_direction: str,
        now: float,
    ) -> Path:
        """Construct a v1 Path entity from a successfully acquired PathSegment."""
        sequence = self._segment_to_resource_sequence(
            segment, base_index=0, skip_first_block=False
        )
        path_tiles = self._flatten_resource_sequence_tiles(sequence)
        authority_end = (
            segment.blocks[-1] if segment.blocks else requested_destination_block_id
        )
        path = Path(
            path_id=path_id,
            status=PathStatus.ACTIVE,
            train_id=train_id,
            requested_destination_block_id=requested_destination_block_id,
            requested_destination_direction=requested_destination_direction,
            authority_end_block_id=authority_end,
            resource_sequence=sequence,
            path_tiles=path_tiles,
            current_resource_index=0,
            cancellation_kind=None,
            last_status_updated_time=now,
        )
        return path

    def _populate_train_reserved_blocks(self, train_id: str, path: Path) -> None:
        train = self._get_train(train_id)
        if train is None:
            return
        try:
            train.reserved_blocks = path.get_all_blocks()
        except Exception:
            pass

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

        for event in list(report.entered_resources or []):
            self._record_clean_physical_entry(event, train_id)

        if report.deviation:
            kind = self._map_deviation_kind_to_cancellation(report.deviation_kind)
            if active_path is not None:
                self.cancel_path(
                    active_path.path_id,
                    cancellation_kind=kind,
                    reason=report.deviation_reason or "",
                )
            return

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

        for event in list(report.tail_vacated_resources or []):
            self._release_tail_vacated_resource(event, train_id)

    def _record_clean_physical_entry(
        self, event: ResourceEvent, train_id: str
    ) -> None:
        if event is None or not event.resource_id:
            return
        if event.resource_type == "block":
            block_manager = getattr(self.network, "block_manager", None)
            if block_manager is not None:
                try:
                    block_manager.occupy_block(event.resource_id, train_id)
                except Exception:
                    pass
            return
        if event.resource_type == "switch":
            switch = self._resolve_switch_entity(event.resource_id)
            if switch is None:
                return
            current = getattr(switch, "occupied_by", None)
            if not current or current == train_id:
                switch.occupied_by = train_id

    def _fire_route_advanced_callback(
        self, train_id: str, path_id: str, event: ResourceEvent
    ) -> None:
        route_executor = self._get_route_executor()
        if route_executor is None:
            return
        handler = getattr(route_executor, "handle_train_advanced", None)
        if not callable(handler):
            return
        try:
            handler(
                train_id,
                path_id,
                event.resource_type,
                event.resource_id,
                event.direction,
            )
        except TypeError:
            try:
                handler(train_id, path_id, event.resource_type, event.resource_id)
            except Exception:
                pass
        except Exception:
            pass

    def _release_tail_vacated_resource(
        self, event: ResourceEvent, train_id: str
    ) -> None:
        if event is None or not event.resource_id:
            return
        if event.resource_type == "block":
            block_manager = getattr(self.network, "block_manager", None)
            if block_manager is None:
                return
            handler = getattr(block_manager, "on_block_tail_vacated", None)
            if callable(handler):
                try:
                    handler(event.resource_id, train_id, event.direction)
                except TypeError:
                    try:
                        handler(event.resource_id, train_id)
                    except Exception:
                        pass
                except Exception:
                    pass
            return
        if event.resource_type == "switch":
            switch = self._resolve_switch_entity(event.resource_id)
            if switch is None:
                return
            if getattr(switch, "occupied_by", None) == train_id:
                switch.occupied_by = None
            release_protection = getattr(switch, "release_train_protection_from", None)
            if callable(release_protection):
                try:
                    release_protection(
                        source_switch_id=event.resource_id, train_id=train_id
                    )
                except TypeError:
                    try:
                        release_protection(event.resource_id, train_id)
                    except Exception:
                        pass
                except Exception:
                    pass

    def _map_deviation_kind_to_cancellation(
        self, deviation_kind: Optional[str]
    ) -> PathCancellationKind:
        if deviation_kind == "beyond_authorized_target":
            return PathCancellationKind.AUTHORITY_OVERSHOOT
        return PathCancellationKind.OUT_OF_SEQUENCE

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

        if not path.authority_end_block_id:
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="path missing authority end",
            )
        if path.authority_end_block_id != path.requested_destination_block_id:
            return PathCompletionResult(
                success=False,
                path_id=path_id,
                new_status=path.status,
                reason="authority end has not reached requested destination",
            )

        # Release reservations (completed=True).
        all_blocks = path.get_all_blocks()
        block_manager = getattr(self.network, "block_manager", None)
        if block_manager is not None and hasattr(
            block_manager, "release_path_block_reservations"
        ):
            try:
                block_manager.release_path_block_reservations(
                    all_blocks, path_id, completed=True
                )
            except Exception:
                pass

        # Release switch locks.
        for switch_id in path.get_all_switches():
            switch = self._resolve_switch_entity(switch_id)
            if switch is None:
                continue
            keys_to_release = [
                key for key in list(switch.locks.keys()) if key[0] == path_id
            ]
            for key in keys_to_release:
                _, claim_role, source_id = key
                if hasattr(switch, "release_lock"):
                    switch.release_lock(path_id, claim_role, source_id)

        # Mark COMPLETED.
        path._set_status(PathStatus.COMPLETED, event_time)

        # Record completion event.
        end_tile = (
            (path.path_tiles[-1].x, path.path_tiles[-1].y)
            if path.path_tiles
            else None
        )
        if end_tile is not None:
            self.completed_path_events.append(
                {
                    "train_id": train_id,
                    "path_id": path_id,
                    "end_tile": end_tile,
                    "time": datetime.now(),
                }
            )

        # Notify controller.
        on_complete = getattr(controller, "on_path_complete", None)
        if callable(on_complete):
            try:
                on_complete()
            except Exception:
                pass

        # Notify route executor.
        route_executor = self._get_route_executor()
        if route_executor is not None:
            handler = getattr(route_executor, "handle_path_completion", None)
            if callable(handler):
                try:
                    handler(
                        train_id,
                        path_id,
                        path.authority_end_block_id,
                        path.requested_destination_direction,
                        event_time,
                    )
                except TypeError:
                    try:
                        handler(train_id, path_id)
                    except Exception:
                        pass
                except Exception:
                    pass

        if path_id in self.paths:
            del self.paths[path_id]

        return PathCompletionResult(
            success=True,
            path_id=path_id,
            new_status=PathStatus.COMPLETED,
            reason=None,
        )

    # Legacy alias
    def complete_path(self, path_id: str) -> bool:
        path = self.paths.get(path_id)
        if path is None or path.train_id is None:
            return False
        result = self.try_complete_active_path(path.train_id, self._now())
        return result.success

    # ========================================================================
    # Query API
    # ========================================================================

    def get_path(self, path_id: str) -> Optional[Path]:
        return self.paths.get(path_id)

    def get_active_paths(self) -> List[Path]:
        return list(self.paths.values())

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

    def get_paths_using_switch(self, position: Tuple[int, int]) -> List[Path]:
        """Return paths that traverse a switch at the given tile position."""
        switch = self._resolve_switch_at_position(position)
        if switch is None:
            return []
        switch_id = getattr(switch, "switch_id", None)
        if switch_id is None:
            return []
        return [
            p for p in self.paths.values() if switch_id in p.get_all_switches()
        ]

    def get_and_clear_completion_events(self) -> List[Dict[str, Any]]:
        events = list(self.completed_path_events)
        self.completed_path_events.clear()
        return events

    def update_approach_locking(self, path_id: str) -> None:
        """Approach locking is recomputed by the train's overlap controller in v1.

        This call is retained as a no-op to keep legacy tick wiring compiling
        until the per-tick caller is removed in Wave 6.
        """
        return None
