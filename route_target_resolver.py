"""
RouteTargetResolver — owns the read-only "next task target" projection
extracted from `RouteExecutor`.

Per design_new.md "Canonical Next-Target Selection" the projection is a
pure read: it never writes `task_runs[i].resolved_target`; that field is
the exclusive write of Phase 1's authority-acquisition step. This module
keeps the resolution logic structurally separate so the executor's tick
loop can stay focused on state-machine transitions.

`RouteExecutor` keeps the doc-canonical method names
(`_find_next_task_target`, `resolve_task_target_block`,
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
            # A dwell resolves to no block target — it is served in place. But a
            # dwell whose `task_location` ID has left the network is BROKEN like
            # any other task: `missing_target` "applies to all task kinds,
            # including `dwell`" (design_new.md "RouteTaskRun schema"). Without
            # this, Phase 1 would enter RUNNING_AT_STATION and hold on the timer
            # of a station that no longer exists, instead of skipping it.
            if not self.task_target_present(task):
                from runtime_payloads import TargetResolutionFailureKind

                return ResolvedNextTaskProjection(
                    walk_position=walk_position,
                    task_kind="dwell",
                    direction=direction_str,
                    resolved_target=None,
                    failure_reason=(
                        f"dwell target {getattr(task, 'task_location', None)} "
                        f"missing from network"
                    ),
                    failure_kind=TargetResolutionFailureKind.MISSING_TARGET,
                )
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
            from runtime_payloads import TargetResolutionFailureKind

            # BROKEN (target ID absent) vs UNAVAILABLE (present but unusable):
            # the broken-vs-unreachable split per design_new.md "Unreachable
            # and broken task handling".
            failure_kind = (
                TargetResolutionFailureKind.MISSING_TARGET
                if not self.task_target_present(task)
                else TargetResolutionFailureKind.TARGET_UNAVAILABLE
            )
            return ResolvedNextTaskProjection(
                walk_position=walk_position,
                task_kind=task.task_kind,
                direction=direction_str,
                resolved_target=None,
                failure_reason=failure_reason,
                failure_kind=failure_kind,
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

    def _forward_move_tasks(self, route, start, *, skip_start):
        """Yield `(index, task)` for each non-`dwell` task walking forward from
        `start` — one lap, wrapping for CIRCULAR, stopping at the end for linear
        routes; with `skip_start=True` the walk begins strictly after `start`.

        The shared task-walk kernel behind `find_next_move_target(...)` (the
        train's live next move) and `_next_move_block_after_index(...)` (a
        hypothetical NEAREST seed position). Each caller keeps its own
        resolve-and-return policy; only the walk order / wrap / dwell-skip is
        shared here.
        """
        task_count = (
            route.get_task_count() if hasattr(route, "get_task_count") else 0
        )
        if task_count == 0:
            return
        is_circular = getattr(route, "mode", None) == RouteMode.CIRCULAR
        begin = 1 if skip_start else 0
        for step in range(begin, task_count + begin):
            if is_circular:
                idx = (start + step) % task_count
            else:
                idx = start + step
                if idx >= task_count:
                    return
            task = route.get_task_at(idx) if hasattr(route, "get_task_at") else None
            if task is None:
                return
            if getattr(task, "task_kind", None) == "dwell":
                continue
            yield idx, task

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

        for _idx, task in self._forward_move_tasks(route, start, skip_start=False):
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

    def task_target_present(self, task: "RouteTask") -> bool:
        """True when `task.task_location`'s target ID is present in the network
        — the "valid task" / non-broken test used by the broken-vs-unreachable
        split. A task whose ID is absent is BROKEN
        (failure_kind == MISSING_TARGET); a present-but-unusable target is
        UNAVAILABLE. Per design_new.md "Unreachable and broken task handling".
        """
        loc = getattr(task, "task_location", None)
        net = getattr(self._e, "network", None)
        if loc is None or net is None:
            return False
        if isinstance(loc, BlockTarget):
            return loc.block_id in (getattr(net, "blocks", {}) or {})
        if isinstance(loc, PlatformTarget):
            return loc.platform_id in (getattr(net, "platforms", {}) or {})
        if isinstance(loc, StationTarget):
            return loc.station_id in (getattr(net, "station_groups", {}) or {})
        return False

    def _route_manual_override(self, train: Optional["Train"]) -> bool:
        """Operator override flag from this train's assigned route.

        design_new.md line 1208 / line 640: `RouteExecutor` applies the
        assigned route's `manual_override` to manual target filtering. The flag
        is route-level (default False); an automatic route never sets it, so
        auto plans never resolve `manual` targets. Returns False when the train
        has no resolvable assigned route.
        """
        if train is None:
            return False
        e = self._e
        train_id = getattr(train, "train_id", None)
        if not train_id:
            return False
        # Degrade to False when the executor has no route context (e.g. a
        # minimal test stub): no resolvable route means no operator override.
        assigned = getattr(e, "assigned_route_id_for", None)
        if not callable(assigned):
            return False
        route_id = assigned(train_id)
        if not route_id:
            return False
        registry = getattr(e, "route_registry", None)
        if registry is None:
            return False
        route = registry.get(route_id)
        return bool(getattr(route, "manual_override", False))

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
        it directly onto `task_runs[walk_position].last_error` (inlined in
        `tick_train_phase1`) so the operator sees why the task stalled
        instead of an empty diagnostic.

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

        # Availability filters per design_new.md "Platform And Station" and the
        # `disabled` and `manual` availability flags canonical home under
        # TrackResource. The `manual` filter is relaxed when the train's
        # assigned route carries `manual_override == True` (design_new.md line
        # 1208 / line 640): an operator-initiated route may target `manual`
        # blocks/platforms/stations. `disabled` is never relaxed. An automatic
        # route leaves `manual_override == False`, so auto plans still treat
        # `manual` targets like disabled ones.
        manual_override = self._route_manual_override(train)
        blocks_registry = getattr(e.network, "blocks", {}) or {}

        def _block_unavailable_reason(block_id: str) -> Optional[str]:
            """Return None when block is targetable, else a reason string."""
            block = blocks_registry.get(block_id)
            if block is None:
                return f"Block {block_id} is not in the network"
            if getattr(block, "disabled", False):
                return f"Block {block_id} is disabled"
            if getattr(block, "manual", False) and not manual_override:
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
            if getattr(platform, "manual", False) and not manual_override:
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
            if getattr(station, "manual", False) and not manual_override:
                return None, f"Station {loc.station_id} is set to manual"
            platform_ids = getattr(station, "platform_ids", []) or []
            platforms = getattr(e.network, "platforms", {}) or {}
            this_train_id = getattr(train, "train_id", None) if train is not None else None

            fallback: Optional[str] = None
            for pid in platform_ids:
                platform = platforms.get(pid)
                if platform is None:
                    continue
                # Skip platforms that are themselves unavailable for routing.
                # `manual` is relaxed under an operator route's manual_override.
                if getattr(platform, "disabled", False) or (
                    getattr(platform, "manual", False) and not manual_override
                ):
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
        return getattr(track, "block_id", None)

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
            if track and track.block_id:
                return track.block_id
        return None

    def _find_paths_cached(
        self,
        dispatcher,
        start_block_id: str,
        dest_block_id: str,
        train_id: Optional[str],
        direction_constraint: Optional[str],
        cache: Optional[dict],
    ):
        """`dispatcher.find_paths(...)` memoized for one seed pass.

        NEAREST seeding asks for the same directed walk twice per stop — the
        distance metric (`_walk_metric_to_block`) and the reversal arrival walk
        (`_seed_stop_forces_reversal`) — so a per-pass cache keyed by
        `(start, dest, direction_constraint, train_id)` computes it once. A
        `None` cache disables memoization (callers outside the seed pass). Safe
        within one synchronous pass because network state does not change
        between the calls.
        """
        if cache is None:
            return dispatcher.find_paths(
                start_block_id,
                dest_block_id,
                train_id=train_id,
                direction_constraint=direction_constraint,
            )
        key = (start_block_id, dest_block_id, direction_constraint, train_id)
        if key not in cache:
            cache[key] = dispatcher.find_paths(
                start_block_id,
                dest_block_id,
                train_id=train_id,
                direction_constraint=direction_constraint,
            )
        return cache[key]

    def _walk_metric_to_block(
        self,
        train: Optional["Train"],
        target_block_id: str,
        direction_constraint: Optional[str] = None,
        _walk_cache: Optional[dict] = None,
    ) -> Optional[Tuple[int, float]]:
        """Best graph-walk metric `(tile_count, walk_length_cm)` from the
        train's current block to `target_block_id`, or `None` when no walk
        reaches it.

        Canonical "distance from the train head to a block" query, shared by
        `_closer_platform_end` (nearer multi-block platform end) and
        `nearest_start_stop_index` (NEAREST start-mode seeding). Uses the same
        `PathDispatcher.find_paths(...)` walk that `create_path(...)` uses;
        candidates come back pre-sorted in winning order, so `[0]` is the best
        walk.

        `direction_constraint` (default `None`) is forwarded to
        `find_paths(...)`. Left `None`, the walk is direction-agnostic — used by
        `_closer_platform_end`, where block-relative direction is resolved
        separately. Set to `"forward"` / `"reverse"`, the metric counts only
        walks the train can actually run in that block-relative direction —
        what `nearest_start_stop_index` uses to keep a stop reachable only in a
        direction its task forbids out of the NEAREST candidate set. Either way
        the pathfinder still enforces `Block.allowed_travel_directions`. Returns
        `None` when there is no train, no dispatcher, no resolvable start block,
        or the target is unreachable under the given constraint.
        """
        if train is None:
            return None
        dispatcher = getattr(self._e, "path_dispatcher", None)
        start_block_id = self._current_block_id(train)
        if dispatcher is None or not start_block_id:
            return None
        train_id = getattr(train, "train_id", None)
        try:
            walks = self._find_paths_cached(
                dispatcher,
                start_block_id,
                target_block_id,
                train_id,
                direction_constraint,
                _walk_cache,
            )
        except Exception:
            return None
        if not walks:
            return None
        best = walks[0]
        tiles = getattr(best, "tiles", None) or []
        return (len(tiles), float(getattr(best, "distance_cm", 0.0) or 0.0))

    def _closer_platform_end(
        self,
        train: Optional["Train"],
        end_a: str,
        end_b: str,
    ) -> Optional[str]:
        """Pick the nearer of a multi-block platform's two end blocks.

        Compares the graph-walk distance from the train's current block to each
        end via the shared `_walk_metric_to_block(...)` query and returns the
        end with the shorter walk (canonical metrics `(tile_count,
        walk_length_cm)`). Ties — including both ends equidistant — resolve to
        `end_a` (the platform's `ordered_blocks[0]`), matching the canonical
        `end_a` dead-heat tie-break. Returns `None` when the comparison cannot
        be evaluated (no train, no dispatcher, no resolvable start block, or
        neither end is reachable); the caller then falls back to
        `ordered_blocks[0]`.
        """
        key_a = self._walk_metric_to_block(train, end_a)
        key_b = self._walk_metric_to_block(train, end_b)
        if key_a is None and key_b is None:
            return None
        if key_b is None:
            return end_a
        if key_a is None:
            return end_b
        # Both reachable: shorter walk wins; exact tie -> end_a.
        return end_b if key_b < key_a else end_a

    def _next_move_block_after_index(
        self, route: "Route", train: Optional["Train"], idx: int
    ) -> Optional[str]:
        """Resolved block of the first `stop` / `waypoint` task after walk
        index `idx` (skipping `dwell` tasks, wrapping for CIRCULAR), or `None`.

        Mirrors `find_next_move_target(...)` but for a *hypothetical* seed
        position rather than the train's live next-planning index, so NEAREST
        can ask "if I started here, where would my next move go?" without
        mutating any run state. Shares the forward task-walk kernel
        (`_forward_move_tasks`); unlike `find_next_move_target` it skips
        non-dwell tasks that do not resolve to a block and keeps walking.
        """
        for _j, task in self._forward_move_tasks(route, idx, skip_start=True):
            block, _reason = self.resolve_task_target_block_detailed(task, train=train)
            if block:
                return block
        return None

    def _seed_stop_forces_reversal(
        self,
        train: Optional["Train"],
        route: "Route",
        idx: int,
        bid: str,
        arrival_direction: str,
        _walk_cache: Optional[dict] = None,
    ) -> bool:
        """True when a train seeded at stop `idx` (resolved block `bid`) would
        have to reverse to reach its next route move.

        A reversal at the stop means the next move leaves `bid` back through the
        very block the train arrived through — it turns around in place rather
        than driving on. We decide that geometrically by comparing two walks:
        the arrival walk (train's current block → `bid`) and the onward walk
        (`bid` → the next move's block). If the onward walk's first step
        re-enters the block immediately before `bid` on the arrival walk, the
        train backs out the way it came, so this is a reverse stop.

        `arrival_direction` is the stop's resolved block-relative direction
        (`resolve_task_direction(...)`, always `"forward"` / `"reverse"`, never
        `"either"`), threaded in from `nearest_start_stop_index` so the arrival
        walk is the SAME directed approach that gated the stop's eligibility and
        distance. It is passed as `find_paths(..., direction_constraint=...)`,
        which constrains the block-relative direction the walk enters `bid` (its
        destination). This matters when the stop's task pins an explicit
        `task_direction` whose forbidden opposite direction has a strictly
        shorter walk: an undirected arrival walk would return that shorter
        opposite approach, so `arr_blocks[-2]` would be the block arrived
        through going the direction the train will never take, and the reversal
        verdict would be read off a walk that never happens. (For `"either"`
        tasks there is no mismatch — `resolve_task_direction` already resolves
        `"either"` to the shorter direction, which equals the undirected
        shortest.)

        The onward walk is left undirected on purpose. `direction_constraint`
        filters the *terminal* direction at the onward walk's destination
        (`next_block_id`), a different block than `bid`, so it cannot describe
        which end the train departs `bid` through, and `arrival_direction` (a
        `bid`-relative direction) does not apply to it. We read only the onward
        walk's first block, so the shortest undirected onward walk is exactly
        the "nearest way onward" we want: if even that backs out through the
        arrival block, the seeded train reverses in place.

        Read-only. Returns `False` (no reversal) whenever the block sequences
        needed to decide are unavailable (no dispatcher, no resolvable start
        block, no next move, or a walk with fewer than two blocks), so callers
        degrade to pure-distance ranking exactly as before.
        """
        if train is None or not bid:
            return False
        dispatcher = getattr(self._e, "path_dispatcher", None)
        start_block_id = self._current_block_id(train)
        if dispatcher is None or not start_block_id:
            return False
        next_block_id = self._next_move_block_after_index(route, train, idx)
        if not next_block_id or next_block_id == bid:
            return False
        train_id = getattr(train, "train_id", None)
        try:
            arrival = self._find_paths_cached(
                dispatcher,
                start_block_id,
                bid,
                train_id,
                arrival_direction,
                _walk_cache,
            )
            onward = self._find_paths_cached(
                dispatcher, bid, next_block_id, train_id, None, _walk_cache
            )
        except Exception:
            return False
        if not arrival or not onward:
            return False
        arr_blocks = getattr(arrival[0], "blocks", None) or []
        onw_blocks = getattr(onward[0], "blocks", None) or []
        if len(arr_blocks) < 2 or len(onw_blocks) < 2:
            return False
        return onw_blocks[1] == arr_blocks[-2]

    def _claimed_target_blocks(
        self, train: Optional["Train"], route: "Route"
    ) -> set:
        """Blocks that other trains on this route are already heading to —
        their live next-move targets.

        Used so freshly-seeded trains spread across distinct stops instead of
        all piling onto the same nearest one. Reads each other assigned run's
        current next move via `find_next_move_target(...)`, so it reflects where
        trains are actually going right now (a train that has moved past its
        seed no longer claims it). Read-only; empty when the executor exposes no
        run table (the resolver-unit stubs), so it is a no-op there.
        """
        train_id = getattr(train, "train_id", None) if train is not None else None
        route_id = getattr(route, "route_id", None)
        states = getattr(self._e, "execution_states", None) or {}
        claimed: set = set()
        for other_id, st in states.items():
            if other_id == train_id:
                continue
            if getattr(st, "route_id", None) != route_id:
                continue
            block = self.find_next_move_target(other_id)
            if block:
                claimed.add(block)
        return claimed

    def nearest_start_stop_index(
        self, train: Optional["Train"], route: "Route"
    ) -> Optional[int]:
        """Task index of the best stop to seed the train at, for NEAREST
        start-mode seeding (per design_new.md "Run start-mode seeding").

        Considers only `task_kind == "stop"` tasks. A stop is *eligible* only
        when the train has a viable directed path to its resolved block in the
        block-relative direction the stop's task requires (resolved via
        `resolve_task_direction(...)`): connectivity plus block travel-direction
        plus the task's own `task_direction`. A stop the tracks merely touch but
        that can only be reached in a direction its task forbids is not
        eligible, so the ladder falls through to the next viable stop. Among
        eligible stops, ranks each by, in order:
          1. **No reversal first** — a stop the train can drive on from without
             turning around (`_seed_stop_forces_reversal(...)` is False) beats
             one that forces a reverse to reach the next route move.
          2. **Free first** — a stop not already taken (its block neither
             occupied by another train nor the live next-move target of another
             train on this route, see `_claimed_target_blocks(...)`) beats a
             taken one.
          3. **Nearest** — shorter track-distance walk wins, measured with the
             shared `_walk_metric_to_block(...)` query in the stop's required
             direction.
          4. **Lowest task index** breaks any remaining tie.

        Each rank only breaks ties left by the ones above it, so a no-reverse
        stop is preferred even when a reverse stop is a little closer, and a
        nearer-but-taken stop still loses to a no-reverse free one. Returns
        `None` when no stop has a viable directed path (the caller then falls
        back to FIRST).
        """
        tasks = getattr(route, "tasks", []) or []
        train_id = getattr(train, "train_id", None) if train is not None else None
        blocks_registry = getattr(self._e.network, "blocks", {}) or {}
        claimed_blocks = self._claimed_target_blocks(train, route)
        # One find_paths memo for the whole pass: each stop's directed arrival
        # walk is asked for twice (distance metric + reversal check); the cache
        # computes it once. Scoped to this call, so it never goes stale.
        walk_cache: dict = {}

        # Each candidate scores (reverse_penalty, taken_penalty, metric, idx);
        # tuple ordering gives the four-level preference above for free, and the
        # smallest score wins.
        best: Optional[Tuple[Tuple[int, int, Tuple[int, float], int], int]] = None

        for idx, task in enumerate(tasks):
            if getattr(task, "task_kind", None) != "stop":
                continue
            bid, _reason = self.resolve_task_target_block_detailed(task, train=train)
            if not bid or train is None:
                continue
            # Eligibility is a *directed* reach: a stop counts only when the
            # train has a viable path to it in the block-relative direction the
            # stop's task requires — the same directed walk a real create_path
            # to that stop would run (per design_new.md the stop-planning rule
            # under "RouteTask data"). A stop the tracks merely touch but that
            # can only be reached against its task's direction (or against block
            # travel-direction) is skipped, so the ladder falls through to the
            # next viable stop. That directed walk's metric is also the distance
            # used for the nearest comparison below.
            required_dir = self.resolve_task_direction(
                train, bid, getattr(task, "task_direction", "either"), 1
            )
            metric = self._walk_metric_to_block(
                train, bid, direction_constraint=required_dir, _walk_cache=walk_cache
            )
            if metric is None:
                continue
            reverse_penalty = (
                1
                if self._seed_stop_forces_reversal(
                    train, route, idx, bid, required_dir, _walk_cache=walk_cache
                )
                else 0
            )
            block = blocks_registry.get(bid)
            occupant = getattr(block, "occupied_by", None) if block is not None else None
            occupied_by_other = bool(occupant) and occupant != train_id
            taken_penalty = 1 if (occupied_by_other or bid in claimed_blocks) else 0
            score = (reverse_penalty, taken_penalty, metric, idx)
            if best is None or score < best[0]:
                best = (score, idx)

        return None if best is None else best[1]
