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

from typing import TYPE_CHECKING, Iterable, Optional, Tuple

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

        target_block, failure_reason = self.resolve_task_target_block_detailed(
            task, train=train
        )
        if target_block is None:
            return ResolvedNextTaskProjection(
                walk_position=walk_position,
                task_kind=task.task_kind,
                direction=direction_str,
                resolved_target=None,
                failure_reason=failure_reason,
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

    def find_next_move_target(self, train_id: str) -> Optional[str]:
        """Resolve the block this train will path to on its next *move*,
        skipping any `dwell` tasks at the current walk position.

        Read-only, like `find_next_task_target(...)`. Returns the resolved
        target `block_id` of the first `stop` / `waypoint` task at or after the
        train's next planning index, or `None` when there is no such task or it
        cannot be resolved. Best-effort truncation uses this to tell whether a
        stopped occupant's next move would reverse back through a candidate
        terminal block (see the reversal-trap exclusion under best-effort
        `create_path()` in design_new.md).

        ROUND_TRIP direction flips at route ends are not modelled here; the
        walk steps forward (with CIRCULAR wraparound) from the next planning
        index, which is exact for the common `[stop, dwell, stop, ...]` layout
        and at worst returns `None`, leaving truncation at today's behavior.
        """
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
        start = self.compute_next_task_index(route, exec_state, task_count)
        if start is None:
            return None

        for step in range(task_count):
            if route.mode == RouteMode.CIRCULAR:
                idx = (start + step) % task_count
            else:
                idx = start + step
                if idx >= task_count:
                    break
            task = route.get_task_at(idx)
            if task is None:
                break
            if task.task_kind == "dwell":
                continue
            block, _reason = self.resolve_task_target_block_detailed(
                task, train=train
            )
            return block
        return None

    # ------------------------------------------------------------------
    # Task-location → block resolution
    # ------------------------------------------------------------------

    def resolve_task_target_block(
        self,
        task: "RouteTask",
        train: Optional["Train"] = None,
    ) -> Optional[str]:
        """Resolve `task.task_location` to a concrete `block_id`.

        Thin wrapper over `resolve_task_target_block_detailed(...)` that
        drops the failure reason. Callers that need the reason (e.g. Phase 1
        building a `task_runs[i].last_error` record per the canonical
        error-record writer rule in design_new.md) call the detailed
        variant directly.
        """
        block_id, _reason = self.resolve_task_target_block_detailed(task, train=train)
        return block_id

    def resolve_task_target_block_detailed(
        self,
        task: "RouteTask",
        train: Optional["Train"] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve `task.task_location` to a concrete `block_id` and a
        diagnostic failure reason.

        Returns ``(block_id, None)`` on success.

        Returns ``(None, reason)`` when the resolver gives up because of an
        availability filter (every candidate block / platform / station is
        `disabled` or `manual` while RouteExecutor's auto-routing context
        cannot use manual_override). `reason` is a short human-readable
        string suitable for `RouteTaskRun.last_error.text`; Phase 1 writes
        it via `_record_run_error(...)` so the operator sees why the task
        stalled instead of an empty diagnostic.

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
            return None, "task_location is missing"

        # Availability filters per design_new.md "Platform And Station"
        # and the `disabled` and `manual` availability flags canonical home
        # under TrackResource. RouteExecutor (auto) never sets
        # manual_override, so manual blocks/platforms/stations are treated
        # like disabled here. Operator-initiated paths bypass these filters
        # at the dispatcher layer; they never reach this resolver.
        blocks_registry = getattr(e.network, "blocks", {}) or {}

        def _block_unavailable_reason(block_id: str) -> Optional[str]:
            """Return None when block is targetable, else a reason string."""
            block = blocks_registry.get(block_id)
            if block is None:
                return f"Block {block_id} is not in the network"
            if getattr(block, "disabled", False):
                return f"Block {block_id} is disabled"
            if getattr(block, "manual", False):
                return f"Block {block_id} is set to manual"
            return None

        if isinstance(loc, BlockTarget):
            if loc.block_id not in blocks_registry:
                return None, f"Block {loc.block_id} is not in the network"
            reason = _block_unavailable_reason(loc.block_id)
            if reason is not None:
                return None, reason
            return loc.block_id, None

        if isinstance(loc, PlatformTarget):
            platforms = getattr(e.network, "platforms", {}) or {}
            platform = platforms.get(loc.platform_id)
            if platform is None:
                return None, f"Platform {loc.platform_id} is not in the network"
            if getattr(platform, "disabled", False):
                return None, f"Platform {loc.platform_id} is disabled"
            if getattr(platform, "manual", False):
                return None, f"Platform {loc.platform_id} is set to manual"
            bid = self.platform_block_id(loc.platform_id, train=train)
            if not bid:
                return None, f"Platform {loc.platform_id} has no block"
            reason = _block_unavailable_reason(bid)
            if reason is not None:
                return None, reason
            return bid, None

        if isinstance(loc, StationTarget):
            station_groups = getattr(e.network, "station_groups", {}) or {}
            station = station_groups.get(loc.station_id)
            if station is None:
                return None, f"Station {loc.station_id} is not in the network"
            if getattr(station, "disabled", False):
                return None, f"Station {loc.station_id} is disabled"
            if getattr(station, "manual", False):
                return None, f"Station {loc.station_id} is set to manual"
            platform_ids = getattr(station, "platform_ids", []) or []
            platforms = getattr(e.network, "platforms", {}) or {}
            this_train_id = getattr(train, "train_id", None) if train is not None else None

            fallback: Optional[str] = None
            for pid in platform_ids:
                platform = platforms.get(pid)
                if platform is None:
                    continue
                # Skip platforms that are themselves unavailable for auto-routing.
                if getattr(platform, "disabled", False) or getattr(platform, "manual", False):
                    continue
                bid = self.platform_block_id(pid, train=train)
                if not bid or _block_unavailable_reason(bid) is not None:
                    continue
                if fallback is None:
                    fallback = bid
                block = blocks_registry.get(bid)
                if block is None:
                    continue
                occupant = getattr(block, "occupied_by", None)
                if occupant and occupant != this_train_id:
                    continue  # held by another train; try the next platform
                return bid, None
            if fallback is not None:
                return fallback, None
            return (
                None,
                f"all platforms in station {loc.station_id} are disabled, "
                f"manual, or otherwise unavailable",
            )

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
        """Train's current block from its head tile, or None.

        Mirrors `TrainAtTargetPredicates.current_block_id`, but is kept inline:
        the resolver is unit-tested in isolation with a minimal stub executor
        that exposes only `_controller_by_id` / `network`, not the predicates
        object, so it resolves the head tile -> block directly here."""
        getter = getattr(self._e, "_controller_by_id", None)
        ctrl = getter(train.train_id) if callable(getter) else None
        head_tile = ctrl._get_head_tile() if ctrl is not None else None
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

    def platform_block_id(
        self,
        platform_id: str,
        train: Optional["Train"] = None,
    ) -> Optional[str]:
        """Resolve a platform to one concrete dispatcher-facing end block.

        Per design_new.md line 1247, a platform exposes its two platform-end
        blocks as `ordered_blocks[0]` and `ordered_blocks[-1]`. For a
        multi-block platform this picks the end whose graph walk from the
        train's current start block is shorter, using the same shared
        network-graph walk query (`PathDispatcher.find_paths(...)`, the
        block-ID adapter over `RailwayPathfinder`) that
        `PathDispatcher.create_path(...)` uses, ranked by the canonical
        candidate comparison defined in **`GraphWalkCandidate` payload**
        (`tile_count`, then `walk_length_cm`), tie-broken to `ordered_blocks[0]`.

        For a single-block platform both platform ends collapse to the same
        block, so only that one block participates and no graph walk is run —
        behavior is unchanged. When `train` is omitted, or the train's start
        block / dispatcher cannot be resolved (early bootstrap, the
        viability/reachable read-only callers), the resolver also falls back to
        `ordered_blocks[0]` so those single-end callers keep working.

        A tile-by-tile fallback covers platforms whose `block_assignments`
        have not been populated yet.
        """
        platform = self.get_platform(platform_id)
        if not platform:
            return None

        ordered_blocks = getattr(platform, "ordered_blocks", None)
        if ordered_blocks:
            end_a = ordered_blocks[0]
            end_b = ordered_blocks[-1]
            if end_a == end_b:
                # Single-block platform (length 1, or both ends the same
                # block): both ends collapse, so no comparison is needed.
                return end_a
            chosen = self._closer_platform_end(train, end_a, end_b)
            return chosen if chosen is not None else end_a

        network = self._e.network
        for x, y in getattr(platform, "positions", []):
            track = network.get_track(x, y)
            if track and track.block_section_id:
                return track.block_section_id
        return None

    def _closer_platform_end(
        self,
        train: Optional["Train"],
        end_a: str,
        end_b: str,
    ) -> Optional[str]:
        """Pick the nearer of a multi-block platform's two end blocks.

        Computes the graph-walk distance from the train's current start block
        to each end via `PathDispatcher.find_paths(...)` (the same shared
        walk query `create_path(...)` uses) and returns the end with the
        shorter walk, comparing by the canonical candidate metrics
        `(tile_count, walk_length_cm)`. `find_paths(...)` already returns
        candidates pre-sorted in winning order, so `[0]` is each end's best
        walk. Ties — including the case where both ends are equidistant —
        resolve to `end_a` (the platform's `ordered_blocks[0]`), matching the
        canonical `end_a` dead-heat tie-break. Returns `None` when the
        comparison cannot be evaluated (no train, no dispatcher, no resolvable
        start block, or neither end is reachable); the caller then falls back
        to `ordered_blocks[0]`.
        """
        if train is None:
            return None
        dispatcher = getattr(self._e, "path_dispatcher", None)
        start_block_id = self._current_block_id(train)
        if dispatcher is None or not start_block_id:
            return None

        train_id = getattr(train, "train_id", None)

        def _best(end_block_id: str) -> Optional[Tuple[int, float]]:
            """Best candidate's `(tile_count, walk_length_cm)` for this end,
            or None when no walk reaches it. Direction is left unconstrained:
            block-relative direction is resolved separately by
            `resolve_task_direction(...)`; this comparison only ranks reach
            distance, mirroring the no-direction-filter start-side comparison
            `create_path(...)` runs."""
            try:
                walks = dispatcher.find_paths(
                    start_block_id, end_block_id, train_id=train_id
                )
            except Exception:
                return None
            if not walks:
                return None
            best = walks[0]
            tiles = getattr(best, "tiles", None) or []
            return (len(tiles), float(getattr(best, "distance_cm", 0.0) or 0.0))

        key_a = _best(end_a)
        key_b = _best(end_b)
        if key_a is None and key_b is None:
            return None
        if key_b is None:
            return end_a
        if key_a is None:
            return end_b
        # Both reachable: shorter walk wins; exact tie -> end_a.
        return end_b if key_b < key_a else end_a
