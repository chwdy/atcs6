"""
RouteTargetResolver — owns the read-only "next task target" projection
extracted from `RouteExecutor`.

Per design_new.md "Canonical Next-Target Selection" the projection is a
pure read: it never writes `task_runs[i].resolved_target`; that field is
the exclusive write of Plan's authority-acquisition step. This module
keeps the resolution logic structurally separate so the executor's tick
loop can stay focused on state-machine transitions.

`RouteExecutor` keeps the doc-canonical method names
(`_find_next_task_target`, `resolve_task_target_block`,
`_resolve_task_direction`, `_compute_next_task_index`, `_get_platform`,
`_get_platform_block_id`) as thin forwarders that delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Tuple

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
        after_walk_position: Optional[int] = None,
    ) -> Optional["ResolvedNextTaskProjection"]:
        """Read-only projection of the next non-finished task.

        Returns `None` when no next task on the directed walk exists.
        Never writes `task_runs[i].resolved_target`; that is the exclusive
        write of Plan's authority-acquisition step.

        Dispatches on `task_kind` from `Route.tasks[i]`:
          * `"waypoint"` / `"stop"`: resolve a concrete target block + direction.
          * `"dwell"`: no dispatcher target; `resolved_target` is `None`.

        `after_walk_position` returns the first non-finished walk-position
        strictly after that index on the directed walk, appending the next
        walk-position when the walk has not grown that far yet. It is how the
        **Waypoint lookahead** in Planning Kernel step `66aa` reaches past the
        task the train is physically working on (design_new.md "Canonical
        Next-Target Selection"). `None` (the default) returns the next
        non-finished walk-position.
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

        if after_walk_position is None:
            next_task_index = self.compute_next_task_index(
                route, exec_state, task_count
            )
            walk_position_override = None
        else:
            found = self._walk_position_after(exec_state, route, after_walk_position)
            if found is None:
                return None
            walk_position_override, next_task_index = found
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
        walk_position = (
            exec_state.next_planning_walk_position
            if walk_position_override is None
            else walk_position_override
        )

        # The visit direction is stored per-row on the target walk-position
        # (design_new.md line 1836): `_find_next_task_target` reports the row's
        # `walk_direction`, never the run-level `RouteRun.direction`. Fall back
        # to the run-level direction only when the row does not exist yet (an
        # empty walk before pre-allocation). A local signed-int form is computed
        # for callers that still need it.
        if 0 <= walk_position < len(exec_state.task_runs):
            direction_str = exec_state.task_runs[walk_position].walk_direction
        else:
            direction_str = exec_state.direction
        signed_direction = 1 if direction_str == "forward" else -1

        if task.task_kind == "dwell":
            # A dwell resolves to no block target — it is served in place. But a
            # dwell whose `task_location` ID has left the network is BROKEN like
            # any other task: `missing_target` "applies to all task kinds,
            # including `dwell`" (design_new.md "RouteTaskRun schema"). Without
            # this, Plan would hold on the dwell timer of a station that no
            # longer exists, instead of skipping it.
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
            task, train=train, exclude=exclude
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
                    # Every candidate for this task's location was already
                    # attempted this tick, so no concrete target remains (per
                    # design_new.md:1632). Return a sentinel with
                    # resolved_target=None so Plan knows "no more candidates this
                    # tick" without claiming a terminal walk (which would
                    # finalize the run prematurely), carrying the
                    # `target_unavailable` diagnostic. It is UNAVAILABLE, not
                    # MISSING_TARGET: the target IDs are present, just all tried.
                    from runtime_payloads import TargetResolutionFailureKind

                    return ResolvedNextTaskProjection(
                        walk_position=walk_position,
                        task_kind=task.task_kind,
                        direction=direction_str,
                        resolved_target=None,
                        failure_reason=(
                            "every candidate for this task was already "
                            "attempted this tick"
                        ),
                        failure_kind=TargetResolutionFailureKind.TARGET_UNAVAILABLE,
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

    def _walk_position_after(
        self,
        exec_state: "RouteExecutionState",
        route: "Route",
        after_walk_position: int,
    ) -> Optional[tuple]:
        """First non-finished `(walk_position, task_index)` strictly after
        `after_walk_position` on the directed walk.

        Grows `task_runs` when the walk has not reached that far yet, because
        the lookahead legitimately asks about a position the run has not
        appended: the served waypoint is still unfinished, so nothing has
        appended past it. Returns `None` when the walk is terminal there —
        a `ONE_OFF` route past its last task.
        """
        pos = after_walk_position + 1
        # One full route-length scan is the budget, the same bound every other
        # walk scan in Plan uses.
        for _ in range(max(1, route.get_task_count())):
            if pos >= len(exec_state.task_runs):
                before = len(exec_state.task_runs)
                # Continue the walk from the current tail (design_new.md
                # "`RouteRun._append_next_walk_task(from_walk_position)`").
                exec_state._append_next_walk_task(route, before - 1)
                if len(exec_state.task_runs) == before:
                    return None      # terminal walk; nothing to look ahead to
            row = exec_state.task_runs[pos]
            if row.finished_at is None:
                return (pos, row.task_index)
            pos += 1
        return None

    def find_next_move_target_from(
        self, walk_position: int, train_id: str
    ) -> Optional[str]:
        """The block of the next non-`dwell` move at or after `walk_position`.

        The seed-position variant of `find_next_move_target(...)`: both share
        one forward task-walk kernel — the walk order, `CIRCULAR` wrap, and
        `dwell` skip are defined once — and differ only in resolve-and-return
        policy. This one walks on past a move task it cannot resolve rather
        than stopping, because **Run start-mode seeding** asks the question
        from a hypothetical seed position where an unresolvable task says
        nothing about the ones after it (design_new.md, item 11 under
        "Path And PathDispatcher").
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
        if not (0 <= walk_position < len(exec_state.task_runs)):
            return None
        start = exec_state.task_runs[walk_position].task_index

        for _idx, task in self._forward_move_tasks(route, start, skip_start=False):
            block, _reason = self.resolve_task_target_block_detailed(
                task, train=train
            )
            if block is not None:
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
        drops the failure reason. Callers that need the reason (e.g. Plan
        building the route-run error event's `ErrorRecord` per the canonical
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
        exclude: Optional[Iterable["ResolvedRouteTarget"]] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve `task.task_location` to a concrete `block_id` and a
        diagnostic failure reason.

        Returns ``(block_id, None)`` on success.

        Returns ``(None, reason)`` when the resolver gives up because of an
        availability filter (every candidate block / platform / station is
        `disabled` or `manual` while RouteExecutor's auto-routing context
        cannot use manual_override). `reason` is a short human-readable
        string suitable for the error event's `ErrorRecord.text`; Plan
        emits it as one route-run error event (inlined in
        `tick_train_plan`) so the operator sees why the task stalled
        instead of an empty diagnostic.

        Per design_new.md line 1125, `task_location` is a tagged union of
        `BlockTarget | PlatformTarget | StationTarget`; this method
        dispatches on the variant rather than probing in order.

        For `StationTarget`, the resolver merges platform-end candidates
        across EVERY platform in `Station.platform_ids` and returns the
        globally best one by the cross-query comparison — travel time first,
        then tile count, then walk length — with ties broken by platform-end
        identity (design_new.md line 1667; see
        `_resolve_station_target_block`). It is not the first available
        platform's end.

        `exclude` names the `(block, direction)` pairs Plan already tried and
        failed this same pass. Only the `StationTarget` branch reads it: their
        blocks are skipped so Plan's same-pass retry loop walks down the sorted
        candidate list to the next distinct pair instead of re-offering the
        global best. `BlockTarget` / `PlatformTarget` resolve one fixed target,
        so `exclude` is applied by `find_next_task_target`'s own post-resolution
        check for them.
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
            return self._resolve_station_target_block(
                station,
                loc.station_id,
                train,
                manual_override,
                blocks_registry,
                _block_unavailable_reason,
                exclude,
            )

        # `task_location` is guaranteed to be one of the three tagged-union
        # variants by `RouteTask.__post_init__` -> `_coerce_task_location`;
        # any other type is a caller bug.
        raise TypeError(
            f"task_location must be BlockTarget / PlatformTarget / StationTarget; "
            f"got {type(loc).__name__}"
        )

    def _platform_end_blocks(self, platform: Any) -> List[str]:
        """The platform's candidate end blocks (deduped): both ends of a
        multi-block platform, or the single block both ends collapse to.

        The two platform-end blocks are the first and last of
        `RailwayNetwork.blocks_for_platform(platform_id)` — the derived
        `platform -> blocks` index (design_new.md "Platform And Station"); they
        collapse to one entry for a single-block platform, so only that block
        participates. Falls back to the first block found by walking `positions`
        only when the platform has no resolvable segment membership at all.
        """
        network = self._e.network
        ordered = network.blocks_for_platform(getattr(platform, "platform_id", ""))
        if ordered:
            end_a = ordered[0]
            end_b = ordered[-1]
            return [end_a] if end_a == end_b else [end_a, end_b]
        for x, y in getattr(platform, "positions", []):
            track = network.get_track(x, y)
            if track and getattr(track, "block_id", None):
                return [track.block_id]
        return []

    def _resolve_station_target_block(
        self,
        station: Any,
        station_id: str,
        train: Optional["Train"],
        manual_override: bool,
        blocks_registry: dict,
        block_unavailable_reason,
        exclude: Optional[Iterable["ResolvedRouteTarget"]],
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve a `StationTarget` to one concrete platform-end block by
        merging candidates across EVERY platform in `Station.platform_ids`.

        This is cross-query comparison site 2 in design_new.md's
        `GraphWalkCandidate` payload home (Planning-Kernel step `9c4f` target
        resolution, design_new.md line 1667 / line 2818): query both ends of
        every platform, merge all reachable platform-end candidates into one
        list, and sort by the first three cross-query keys
        `(estimated_travel_seconds, tile_count, walk_length_cm)` — estimated
        travel time first when the run's train max speed is supplied (the query
        carries `train_max_speed_kmh`, so the estimate is populated on the
        candidate) — with ties broken by platform-end identity, the
        deterministic iteration order over `Station.platform_ids` and then the
        two ends of each platform, rather than by `candidate_rank`, which
        carries no meaning across separate per-end queries.

        The globally best reachable-and-available (not held by another train)
        platform-end wins. Selection walks three ordered tiers in turn:

          1. reachable and not held by another train, ranked by the cross-query
             key then platform-end identity — the ordinary winner;
          2. reachable but held by another train, same ranking — the fallback
             so the dispatcher reports the block conflict via
             `PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET`, exactly as the
             first-platform resolver did before;
          3. ends with no travel key — no train/dispatcher context (read-only
             viability callers, early bootstrap) or an unreachable end — ranked
             by identity alone, matching the old `platform_block_id` ->
             `ordered_blocks[0]` fallback.

        `exclude` names `(block, direction)` pairs Plan already tried and failed
        this pass; their blocks are skipped so the same-pass retry walks down
        the sorted list to the next distinct pair (design_new.md line 1667:
        "try create_path once for each distinct resolved pair, stopping at the
        first success"). When every candidate block is excluded, the global
        best is returned so `find_next_task_target`'s own exclude check emits
        the clean "no more candidates this tick" sentinel.
        """
        platforms = getattr(self._e.network, "platforms", {}) or {}
        platform_ids = getattr(station, "platform_ids", []) or []
        this_train_id = (
            getattr(train, "train_id", None) if train is not None else None
        )
        excluded_blocks = {
            ex.requested_destination_block_id for ex in (exclude or [])
        }

        CrossKey = Tuple[float, int, float]
        Identity = Tuple[int, int]
        available: List[Tuple[CrossKey, Identity, str]] = []
        held: List[Tuple[CrossKey, Identity, str]] = []
        no_context: List[Tuple[Identity, str]] = []

        for pi, pid in enumerate(platform_ids):
            platform = platforms.get(pid)
            if platform is None:
                continue
            # Platform-level availability. `manual` is relaxed under an
            # operator route's manual_override; `disabled` never is.
            if getattr(platform, "disabled", False) or (
                getattr(platform, "manual", False) and not manual_override
            ):
                continue
            for ei, bid in enumerate(self._platform_end_blocks(platform)):
                if not bid or block_unavailable_reason(bid) is not None:
                    continue
                identity = (pi, ei)
                key = self._walk_cross_query_key_to_block(train, bid)
                if key is None:
                    # No reachable walk, or no train/dispatcher context. Ranked
                    # by identity and used only when nothing reachable wins; an
                    # unreachable end fails at create_path anyway, so its
                    # occupancy is moot and not consulted here.
                    no_context.append((identity, bid))
                    continue
                block = blocks_registry.get(bid)
                occupant = getattr(block, "occupied_by", None) if block else None
                # design_new.md line 1670: a platform is unavailable when any
                # linked block is held by another train's physical occupancy OR
                # another train/path's live reservation. The current train's own
                # occupancy / live authority never makes the platform unavailable
                # to itself; `CAUTION` alone does not count as held.
                reserver = (
                    getattr(block, "reserved_for_train", None) if block else None
                )
                held_by_other = (occupant and occupant != this_train_id) or (
                    reserver and reserver != this_train_id
                )
                if held_by_other:
                    held.append((key, identity, bid))
                else:
                    available.append((key, identity, bid))

        available.sort(key=lambda c: (c[0], c[1]))
        held.sort(key=lambda c: (c[0], c[1]))
        no_context.sort(key=lambda c: c[0])

        ordered = (
            [bid for _key, _id, bid in available]
            + [bid for _key, _id, bid in held]
            + [bid for _id, bid in no_context]
        )
        if not ordered:
            return (
                None,
                f"all platforms in station {station_id} are disabled, "
                f"manual, or otherwise unavailable",
            )
        for bid in ordered:
            if bid not in excluded_blocks:
                return bid, None
        # Every candidate has already been tried and excluded this pass. Return
        # the global best so `find_next_task_target`'s post-resolution exclude
        # check produces the clean sentinel (resolved_target=None) instead of a
        # spurious failure diagnostic.
        return ordered[0], None

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
        # already-finished last task as the "next" one and prevent Plan
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

        A platform's two platform-end blocks are the first and last of
        `RailwayNetwork.blocks_for_platform(platform_id)` — the derived
        `platform -> blocks` index (design_new.md "Platform And Station"). For a
        multi-block platform this picks the end whose graph walk from the
        train's current start block wins the cross-query comparison, using the
        same shared network-graph walk query that
        `PathDispatcher.create_path(...)` uses, ranked by the canonical
        candidate comparison defined in **`GraphWalkCandidate` payload**
        (`estimated_travel_seconds` first when the train carries a max speed,
        then `tile_count`, then `walk_length_cm`), tie-broken to the first end
        block.

        For a single-block platform both platform ends collapse to the same
        block, so only that one block participates and no graph walk is run —
        behavior is unchanged. When `train` is omitted, or the train's start
        block / dispatcher cannot be resolved (early bootstrap, the
        viability/reachable read-only callers), the resolver also falls back to
        the first end block so those single-end callers keep working.

        A tile-by-tile fallback covers a platform with no resolvable segment
        membership at all.
        """
        platform = self.get_platform(platform_id)
        if not platform:
            return None

        network = self._e.network
        ordered_blocks = network.blocks_for_platform(platform_id)
        if ordered_blocks:
            end_a = ordered_blocks[0]
            end_b = ordered_blocks[-1]
            if end_a == end_b:
                # Single-block platform (length 1, or both ends the same
                # block): both ends collapse, so no comparison is needed.
                return end_a
            chosen = self._closer_platform_end(train, end_a, end_b)
            return chosen if chosen is not None else end_a

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

        Canonical "distance from the train head to a block" query, used by
        `nearest_start_stop_index` (NEAREST start-mode seeding), whose ranking
        is track distance (never travel time). Multi-block platform-end
        selection instead uses the travel-time-first
        `_walk_cross_query_key_to_block(...)`. Uses the same
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

    def _walk_cross_query_key_to_block(
        self,
        train: Optional["Train"],
        target_block_id: str,
        direction_constraint: Optional[str] = None,
    ) -> Optional[Tuple[float, int, float]]:
        """Best candidate's cross-query comparison key
        `(estimated_travel_seconds, tile_count, walk_length_cm)` from the
        train's current block to `target_block_id`, or `None` when no walk
        reaches it.

        The travel-time-first counterpart to `_walk_metric_to_block`, used by
        `_closer_platform_end` so multi-block platform-end selection ranks by
        estimated travel time first (the StationTarget platform-end cross-query
        comparison in design_new.md **`GraphWalkCandidate` payload**). It reads
        through `PathDispatcher.find_walk_candidates(...)` rather than
        `find_paths(...)` because the estimate lives on the candidate, not on
        the unwrapped `PathSegment`. `estimated_travel_seconds` is populated
        when the train carries a max speed (threaded into the query as
        `train_max_speed_kmh`); with no max speed it is `None`, so the key falls
        through to the distance keys -- the same ordering `_walk_metric_to_block`
        gives. Returns `None` on the same conditions as that helper (no train,
        no dispatcher, no resolvable start block, or the target is unreachable).
        """
        if train is None:
            return None
        dispatcher = getattr(self._e, "path_dispatcher", None)
        start_block_id = self._current_block_id(train)
        if dispatcher is None or not start_block_id:
            return None
        train_id = getattr(train, "train_id", None)
        try:
            candidates = dispatcher.find_walk_candidates(
                start_block_id,
                target_block_id,
                train_id=train_id,
                direction_constraint=direction_constraint,
            )
        except Exception:
            return None
        if not candidates:
            return None
        return candidates[0].cross_query_key()

    def _closer_platform_end(
        self,
        train: Optional["Train"],
        end_a: str,
        end_b: str,
    ) -> Optional[str]:
        """Pick the better of a multi-block platform's two end blocks.

        Compares the graph walk from the train's current block to each end via
        the shared `_walk_cross_query_key_to_block(...)` query and returns the
        end whose best candidate wins the cross-query comparison
        `(estimated_travel_seconds, tile_count, walk_length_cm)`: estimated
        travel time first when the train carries a max speed, otherwise the
        distance keys alone. Ties — including both ends equal — resolve to
        `end_a` (the platform's first end block from `blocks_for_platform`), the
        platform-end-identity tie-break for this cross-query site. Returns `None`
        when the comparison cannot be evaluated (no train, no dispatcher, no
        resolvable start block, or neither end is reachable); the caller then
        falls back to the first end block.
        """
        key_a = self._walk_cross_query_key_to_block(train, end_a)
        key_b = self._walk_cross_query_key_to_block(train, end_b)
        if key_a is None and key_b is None:
            return None
        if key_b is None:
            return end_a
        if key_a is None:
            return end_b
        # Both reachable: the winning cross-query key wins; exact tie -> end_a.
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
