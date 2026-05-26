"""
RouteTargetResolver — owns the read-only "next task target" projection
extracted from `RouteExecutor`.

Per design_new.md "Canonical Next-Target Selection" the projection is a
pure read: it never writes `task_runs[i].resolved_target`; that field is
the exclusive write of Phase 1's authority-acquisition step. This module
keeps the resolution logic structurally separate so the executor's tick
loop can stay focused on state-machine transitions.

`RouteExecutor` keeps the doc-canonical method names
(`_find_next_task_target`, `_resolve_task_target_block`,
`_resolve_task_direction`, `_compute_next_task_index`, `_get_platform`,
`_get_platform_block_id`) as thin forwarders that delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

from train_route import (
    BlockTarget,
    PlatformTarget,
    RouteMode,
    StationTarget,
)

if TYPE_CHECKING:
    from route_executor import (
        ResolvedNextTaskProjection,
        ResolvedRouteTarget,
        RouteExecutionState,
        RouteExecutor,
    )
    from train import Train
    from train_route import Route, RouteTask


class RouteTargetResolver:
    """Owns target / direction / next-task-index resolution for one
    `RouteExecutor`."""

    def __init__(self, executor: "RouteExecutor") -> None:
        self._e = executor

    # ------------------------------------------------------------------
    # Top-level projection
    # ------------------------------------------------------------------

    def find_next_task_target(
        self,
        train_id: str,
        exclude: Optional[Iterable["ResolvedRouteTarget"]] = None,
    ) -> Optional["ResolvedNextTaskProjection"]:
        """Read-only projection of the next non-finished task.

        Returns `None` when no next task on the directed walk exists.
        Never writes `task_runs[i].resolved_target`; that is the exclusive
        write of Phase 1's authority-acquisition step.

        Dispatches on `task_kind` from `Route.tasks[i]`:
          * `"waypoint"` / `"stop"`: resolve a concrete target block + direction.
          * `"dwell"`: no dispatcher target; `resolved_target` is `None`.
        """
        from route_executor import ResolvedNextTaskProjection, ResolvedRouteTarget

        e = self._e
        train = (
            e.network.trains.get(train_id)
            if hasattr(e.network, "trains")
            else None
        )
        if train is None:
            return None
        route_id = e.assigned_route_id_for(train_id)
        if not route_id:
            return None
        route = e.route_registry.get(route_id)
        if route is None:
            return None
        exec_state = e.execution_states.get(train_id)
        if exec_state is None:
            return None
        task_count = route.get_task_count() if hasattr(route, "get_task_count") else 0
        if task_count == 0:
            return None

        next_task_index = self.compute_next_task_index(
            route, exec_state, task_count
        )
        if next_task_index is None:
            return None

        task = route.get_task_at(next_task_index)
        if task is None:
            return None

        # `walk_position` in the projection is the list index into
        # `exec_state.task_runs`, not the `Route.tasks` index — these
        # decouple after CIRCULAR / ROUND_TRIP wraparound (design_new.md
        # line 1104 + line 1267). The two values match only for the first
        # walk and for ONE_OFF routes. Canonical helper lives on
        # `RouteExecutionState.next_planning_walk_position`.
        walk_position = exec_state.next_planning_walk_position

        # Canonical direction store on RouteRun is the literal string;
        # compute a local signed-int form for callers that still need it.
        direction_str = exec_state.direction
        signed_direction = 1 if direction_str == "forward" else -1

        if task.task_kind == "dwell":
            return ResolvedNextTaskProjection(
                walk_position=walk_position,
                task_kind="dwell",
                direction=direction_str,
                resolved_target=None,
            )

        target_block = self.resolve_task_target_block(task, train=train)
        if target_block is None:
            return ResolvedNextTaskProjection(
                walk_position=walk_position,
                task_kind=task.task_kind,
                direction=direction_str,
                resolved_target=None,
            )

        resolved_dir = self.resolve_task_direction(
            train, target_block, task.task_direction, signed_direction
        )

        resolved = ResolvedRouteTarget(
            requested_destination_block_id=target_block,
            requested_destination_direction=resolved_dir,
        )

        if exclude:
            for ex in exclude:
                if (
                    ex.requested_destination_block_id
                    == resolved.requested_destination_block_id
                    and ex.requested_destination_direction
                    == resolved.requested_destination_direction
                ):
                    # Per design_new.md (fix #14, part b): return a sentinel
                    # projection with resolved_target=None so Phase 1 knows
                    # "no more candidates this tick" without claiming a
                    # terminal walk (which would finalize the run prematurely).
                    return ResolvedNextTaskProjection(
                        walk_position=walk_position,
                        task_kind=task.task_kind,
                        direction=direction_str,
                        resolved_target=None,
                    )

        return ResolvedNextTaskProjection(
            walk_position=walk_position,
            task_kind=task.task_kind,
            direction=direction_str,
            resolved_target=resolved,
        )

    # ------------------------------------------------------------------
    # Task-location → block resolution
    # ------------------------------------------------------------------

    def resolve_task_target_block(
        self,
        task: "RouteTask",
        train: Optional["Train"] = None,
    ) -> Optional[str]:
        """Resolve `task.task_location` to a concrete `block_id`.

        Per design_new.md line 1125, `task_location` is a tagged union of
        `BlockTarget | PlatformTarget | StationTarget`; this method
        dispatches on the variant rather than probing in order.

        For `StationTarget`, when `train` is supplied, prefer a platform
        whose block is not currently occupied by *another* train — this
        keeps multi-platform stations from funneling every train onto the
        first platform in the group. Falls back to the first resolvable
        platform when every candidate is occupied (the dispatcher will then
        report the conflict via `PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET`,
        same as today).
        """
        e = self._e
        loc = getattr(task, "task_location", None)
        if loc is None:
            return None

        if isinstance(loc, BlockTarget):
            blocks = getattr(e.network, "blocks", {}) or {}
            if loc.block_id in blocks:
                return loc.block_id
            return None

        if isinstance(loc, PlatformTarget):
            return self.platform_block_id(loc.platform_id)

        if isinstance(loc, StationTarget):
            station_groups = getattr(e.network, "station_groups", {}) or {}
            station = station_groups.get(loc.station_id)
            if station is None:
                return None
            platform_ids = getattr(station, "platform_ids", []) or []
            blocks = getattr(e.network, "blocks", {}) or {}
            this_train_id = getattr(train, "train_id", None) if train is not None else None

            fallback: Optional[str] = None
            for pid in platform_ids:
                bid = self.platform_block_id(pid)
                if not bid:
                    continue
                if fallback is None:
                    fallback = bid
                block = blocks.get(bid)
                if block is None:
                    continue
                occupant = getattr(block, "occupied_by", None)
                if occupant and occupant != this_train_id:
                    continue  # held by another train; try the next platform
                return bid
            return fallback

        # `task_location` is guaranteed to be one of the three tagged-union
        # variants by `RouteTask.__post_init__` -> `_coerce_task_location`;
        # any other type is a caller bug.
        raise TypeError(
            f"task_location must be BlockTarget / PlatformTarget / StationTarget; "
            f"got {type(loc).__name__}"
        )

    # ------------------------------------------------------------------
    # Direction resolution ("either" → forward / reverse)
    # ------------------------------------------------------------------

    def resolve_task_direction(
        self,
        train: "Train",
        block_id: str,
        task_direction: str,
        signed_direction: int,
    ) -> str:
        """Resolve a `RouteTask.task_direction` to `"forward"` /
        `"reverse"`.

        v1 contract: the dispatcher never receives `"either"`; this resolver
        must always eliminate it before any `create_path` / `extend_path`
        call.

        Per design_new.md line 1145, `"either"` on a block-relative target
        means "pick whichever block-relative direction is reachable under the
        same current-span comparison and allowed by
        `Block.allowed_travel_directions`." The pathfinder's
        `find_paths(...)` already encodes both constraints, so probe both
        directions and pick the one that returns a candidate. When both
        succeed, tie-break by the shortest candidate per the canonical
        candidate comparison (design_new.md line 1158). The route-walk
        fallback is used only when reachability cannot be evaluated
        (no dispatcher, train has no resolvable start block).
        """
        if task_direction in ("forward", "reverse"):
            return task_direction

        dispatcher = getattr(self._e, "path_dispatcher", None)
        start_block_id = self._current_block_id(train)
        if dispatcher is not None and start_block_id:
            train_id = getattr(train, "train_id", None)
            try:
                fwd = dispatcher.find_paths(
                    start_block_id,
                    block_id,
                    train_id=train_id,
                    direction_constraint="forward",
                )
                rev = dispatcher.find_paths(
                    start_block_id,
                    block_id,
                    train_id=train_id,
                    direction_constraint="reverse",
                )
            except Exception:
                fwd = []
                rev = []
            if fwd and not rev:
                return "forward"
            if rev and not fwd:
                return "reverse"
            if fwd and rev:
                def _len(segment) -> int:
                    return len(getattr(segment, "tiles", []) or [])
                return "forward" if _len(fwd[0]) <= _len(rev[0]) else "reverse"

        return "forward" if signed_direction >= 0 else "reverse"

    def _current_block_id(self, train: "Train") -> Optional[str]:
        """Train's current block from its head tile, or None."""
        position = getattr(train, "position", None)
        if not position:
            return None
        head_tile = getattr(position, "head_tile", None)
        if not head_tile:
            return None
        network = getattr(self._e, "network", None)
        if network is None:
            return None
        track = network.get_track(*head_tile)
        if not track:
            return None
        return getattr(track, "block_section_id", None)

    # ------------------------------------------------------------------
    # Next-task-index computation (pure)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_next_task_index(
        route: "Route",
        exec_state: "RouteExecutionState",
        task_count: int,
    ) -> Optional[int]:
        """Compute the next task index without mutating `exec_state`."""
        if task_count == 0:
            return None

        # Per design, `current_waypoint_index` is the largest finished
        # walk-position (Optional[int]); the next planning index is what we
        # want here.
        idx = exec_state.next_planning_index

        # ONE_OFF terminal-walk detection: `next_planning_index` returns the
        # *last entry's* task_index as a sentinel when every task_run is
        # finished (see its docstring). For ONE_OFF that means "no more
        # tasks to walk" — `_append_next_walk_task` is a no-op there. The
        # in-range check below would otherwise mistakenly resurrect the
        # already-finished last task as the "next" one and prevent Phase 1
        # from calling `finish_route_run(train_id)`.
        task_runs = getattr(exec_state, "task_runs", None) or []
        if route.mode == RouteMode.ONE_OFF and task_runs:
            last_run = task_runs[-1]
            if (
                getattr(last_run, "finished_at", None) is not None
                and idx == getattr(last_run, "task_index", None)
            ):
                return None

        if 0 <= idx < task_count:
            return idx

        if route.mode == RouteMode.ONE_OFF:
            if idx >= task_count:
                return None
            return idx

        if route.mode == RouteMode.CIRCULAR:
            return idx % task_count

        if route.mode == RouteMode.ROUND_TRIP:
            if idx >= task_count:
                return task_count - 2 if task_count > 1 else 0
            if idx < 0:
                return 1 if task_count > 1 else 0
            return idx
        return None

    # ------------------------------------------------------------------
    # Platform / block lookup helpers
    # ------------------------------------------------------------------

    def get_platform(self, platform_id: str) -> Optional[object]:
        """Return the `Platform` object for `platform_id`, or None."""
        platforms = getattr(self._e.network, "platforms", None)
        if isinstance(platforms, dict) and platform_id in platforms:
            return platforms[platform_id]
        return None

    def platform_block_id(self, platform_id: str) -> Optional[str]:
        """Primary block for a platform, derived from
        `Platform.ordered_blocks` with a tile-by-tile fallback for
        platforms whose block assignments have not been populated yet."""
        platform = self.get_platform(platform_id)
        if not platform:
            return None

        ordered_blocks = getattr(platform, "ordered_blocks", None)
        if ordered_blocks:
            return ordered_blocks[0]

        network = self._e.network
        for x, y in getattr(platform, "positions", []):
            track = network.get_track(x, y)
            if track and track.block_section_id:
                return track.block_section_id
        return None
