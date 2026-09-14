"""
Railway Pathfinder

Per design_new.md "Pathfinding-home migration":
this module is the canonical operational pathfinding home that
`RouteExecutor` platform-end resolution, `PathDispatcher.create_path(...)`
start-side selection, and `PathDispatcher.extend_path(...)` suffix
selection all delegate to. It is the single source for:

  - candidate enumeration via direction-aware BFS,
  - direction filtering against ``Block.allowed_travel_directions`` and the
    caller's ``direction_constraint``,
  - rejection of candidates that traverse a disabled block or disabled
    switch,
  - candidate-length comparison and tie-breaking using
    ``GraphWalkCandidate.comparison_key()``.

Direction-aware filters live INSIDE the pathfinder so callers cannot
bypass them. Each surviving walk is wrapped into a
``GraphWalkCandidate`` whose ``walk`` field carries the geometry payload
(a ``PathSegment``) that callers use to build ``Path.resource_sequence``
without re-walking the network.
"""

from collections import deque
from dataclasses import replace
from typing import List, Optional, Set, Tuple, TYPE_CHECKING

from track import OPPOSITE_EDGE
from path_utils import (
    GraphLocator,
    GraphWalkCandidate,
    GraphWalkQuery,
    GraphWalkStep,
)
from runtime_payloads import PathSegment

if TYPE_CHECKING:
    from network import RailwayNetwork


class RailwayPathfinder:
    """Canonical operational pathfinder.

    Public entry point: ``find_ordered_walk(...)`` returns the list of
    surviving directed walk candidates, sorted in winning order.
    """

    def __init__(self, network: "RailwayNetwork"):
        self.network = network

    # ------------------------------------------------------------------ public

    def find_ordered_walk(
        self,
        start_block_id: str,
        end_block_id: str,
        direction_constraint: Optional[str] = None,
        max_candidates: Optional[int] = None,
        manual_override: bool = False,
        train_min_radius_cm: Optional[float] = None,
        train_max_speed_kmh: Optional[float] = None,
        train_max_slope_percent: Optional[float] = None,
    ) -> List[GraphWalkCandidate]:
        """Thin adapter over ``query_graph_walks`` for block-ID callers.

        Builds a ``GraphWalkQuery`` from the block-level arguments and
        delegates all BFS, filtering, deduplication, ranking, and
        ``GraphWalkCandidate`` construction to ``query_graph_walks``.
        Existing callers are unaffected: the returned list has the same
        shape and ordering as before.

        Args:
            start_block_id: Block where the walk begins.
            end_block_id: Block where the walk terminates.
            direction_constraint: Optional ``"forward"`` / ``"reverse"``
                resolved block-relative direction the terminal traversal
                must satisfy on the destination block.
            max_candidates: Optional cap on the number of returned
                candidates after sorting.
            train_min_radius_cm: The train's minimum curve radius capability
                in centimetres, or None for geometry-only callers. When set,
                candidates that traverse a block or switch leg whose curve
                radius is tighter than this value are rejected.
            train_max_speed_kmh: The train's maximum speed in km/h, or None for
                geometry-only callers. When set, surviving candidates are
                ranked by estimated travel time (posted block speed limits
                capped at this max) instead of distance alone.
            train_max_slope_percent: The train's maximum climbable gradient
                (percent grade), or None / 0.0 for no limit. When set above
                zero, candidates that climb a block steeper than this in the
                traversed direction are rejected.

        Returns:
            List of ``GraphWalkCandidate`` sorted in winning order by
            ``GraphWalkCandidate.comparison_key()`` (estimated travel time ASC
            when ``train_max_speed_kmh`` is supplied, then ``tile_count`` ASC,
            ``walk_length_cm`` ASC, ``candidate_rank`` ASC). Empty when no
            walk satisfies every filter.
        """
        if start_block_id not in self.network.blocks:
            return []
        if end_block_id not in self.network.blocks:
            return []

        # Resolve start and end to one representative tile each.
        start_positions = self.network.get_block_positions(start_block_id)
        end_positions = self.network.get_block_positions(end_block_id)
        if not start_positions or not end_positions:
            return []

        # Use the first tile in each block as the locator tile; the BFS
        # inside query_graph_walks fans out from all tiles in the block.
        start_locator = GraphLocator(
            tile=next(iter(start_positions)),
            block_id=start_block_id,
            resource_type="block",
            resource_id=start_block_id,
        )
        end_locator = GraphLocator(
            tile=next(iter(end_positions)),
            block_id=end_block_id,
            resource_type="block",
            resource_id=end_block_id,
        )
        request = GraphWalkQuery(
            start_locator=start_locator,
            end_locator=end_locator,
            requested_destination_direction=direction_constraint,
            return_tiles=True,
            return_blocks=True,
            manual_override=manual_override,
            train_min_radius_cm=train_min_radius_cm,
            train_max_speed_kmh=train_max_speed_kmh,
            train_max_slope_percent=train_max_slope_percent,
        )
        candidates = self.query_graph_walks(request)
        if max_candidates is not None and max_candidates >= 0:
            candidates = candidates[:max_candidates]
        return candidates

    def query_graph_walks(
        self, request: GraphWalkQuery
    ) -> List[GraphWalkCandidate]:
        """Canonical operational graph walk query.

        Per design_new.md "Pathfinding-home migration": accepts a
        ``GraphWalkQuery`` with tile-precise locators, skips disabled
        blocks/switches, searches topology-only regardless of current switch
        position, rejects candidates whose terminal
        traversal does not satisfy ``request.requested_destination_direction``,
        also rejects candidates whose traversal violates
        ``Block.allowed_travel_directions`` when ``request.manual_override``
        is False, and returns comparable walk candidates with whatever ordered
        tile/block walk payload the request flags asked for.

        Args:
            request: ``GraphWalkQuery`` describing start/end locators and
                search options.

        Returns:
            List of ``GraphWalkCandidate`` sorted by
            ``GraphWalkCandidate.comparison_key()`` ascending:
            ``(estimated_travel_seconds, tile_count, walk_length_cm,
            candidate_rank)``, where ``estimated_travel_seconds`` is populated
            only when ``request.train_max_speed_kmh`` is supplied.
        """
        start_block_id = request.start_locator.block_id
        end_block_id = request.end_locator.block_id

        if not start_block_id or start_block_id not in self.network.blocks:
            return []
        if not end_block_id or end_block_id not in self.network.blocks:
            return []

        exclude: Set[str] = request.exclude_resources or set()

        # Steps 1-2 (BFS enumeration + dedup) are a pure function of the static
        # graph when the search is topology-only with no resource exclusions:
        # the BFS ignores switch position and never reads dynamic block/switch
        # availability, so the same (start, end) yields the same raw walks until
        # the topology changes. Reuse the network's cached enumeration in that
        # case so the per-tick create_path/extend_path storm runs one BFS per
        # (start, end) instead of re-walking the graph on every call. The
        # availability/direction filters and candidate construction below stay
        # live, so disabled/manual/direction changes still take effect at once.
        # The cache is dropped on any topology edit -- see
        # RailwayNetwork.invalidate_pathfinding_cache().
        # The candidate list (raw walk + per-step `ordered_walk` geometry) is a
        # pure function of the static graph, so it is built once per (start,end)
        # and cached. Cacheable only for the topology-only / no-exclusion / full-
        # payload search (the find_ordered_walk path); other callers build fresh.
        cacheable = (
            not exclude and request.return_tiles and request.return_blocks
        )
        candidates: Optional[List[GraphWalkCandidate]] = (
            self.network.get_cached_walks(start_block_id, end_block_id)
            if cacheable
            else None
        )
        if candidates is None:
            raw_walks = self._enumerate_walks(
                start_block_id, end_block_id, exclude
            )
            candidates = self._build_candidates(raw_walks, request)
            if cacheable:
                self.network.store_cached_walks(
                    start_block_id, end_block_id, candidates
                )

        # Per call: apply the dynamic direction/availability filter (reads live
        # disabled/manual state plus the caller's direction/manual_override) to
        # the cached candidates, then sort the survivors. The filter runs every
        # call; the geometry above never rebuilds. The fresh `surviving` list
        # means the cached candidate order is never mutated.
        direction_constraint = request.requested_destination_direction
        manual_override = request.manual_override
        surviving = [
            c
            for c in candidates
            if self._walk_passes_filters(
                c,
                direction_constraint,
                manual_override=manual_override,
                train_min_radius_cm=request.train_min_radius_cm,
                train_max_slope_percent=request.train_max_slope_percent,
            )
        ]
        # When the caller supplies the train's max speed, compute each
        # candidate's estimated travel time (posted block limits capped at that
        # max) and store it on the candidate as `estimated_travel_seconds`, the
        # first comparison key. A faster route then wins over an
        # equal-or-shorter-distance slow one, and a caller comparing this
        # candidate against one from a separate query reads the same stored
        # value. Geometry-only callers (no max speed) leave the field None, so
        # `comparison_key()` ranks on the distance keys alone.
        #
        # The estimate depends on the max speed, but the cached candidate list
        # is a max-speed-independent per-(start, end) artifact shared across
        # every query, so the estimate must never be written onto a cached
        # candidate. `replace(...)` returns a fresh copy per surviving candidate
        # to carry the per-query value, leaving the cached originals untouched.
        max_speed = request.train_max_speed_kmh
        if max_speed is not None and max_speed > 0:
            surviving = [
                replace(
                    c,
                    estimated_travel_seconds=self._candidate_travel_time(
                        c, max_speed
                    ),
                )
                for c in surviving
            ]
        surviving.sort(key=lambda c: c.comparison_key())
        return surviving

    def _candidate_travel_time(
        self, candidate: GraphWalkCandidate, max_speed_kmh: float
    ) -> float:
        """Estimated traversal-time cost for a candidate (lower is faster).

        ``query_graph_walks`` stores this value on the candidate as
        ``estimated_travel_seconds`` (its first comparison key) whenever the
        query carried the train's max speed.

        Sums each ordered-walk step's length divided by the effective speed
        there: the posted ``Block.speed_limit_kmh`` capped at the train's max
        speed, or the train's max speed where no limit is posted. Switch steps
        and unposted blocks use the train's max speed. The unit is
        cm / (km/h) -- not a real time unit, but consistent across candidates,
        so the value is only meaningful for comparison. A candidate with no
        ``ordered_walk`` returns 0.0.
        """
        total = 0.0
        for step in candidate.ordered_walk or ():
            speed = max_speed_kmh
            if step.resource_type == "block":
                block = self.network.blocks.get(step.block_id or step.resource_id)
                limit = getattr(block, "speed_limit_kmh", None) if block else None
                if limit is not None and limit > 0:
                    speed = min(speed, limit)
            if speed <= 0:
                speed = max_speed_kmh
            total += step.length_cm / speed
        return total

    def measure_along_walk(
        self,
        ordered_walk: List[GraphWalkStep],
        locator_a: GraphLocator,
        locator_b: GraphLocator,
    ) -> float:
        """In-walk centimeter distance between two locators on a chosen walk
        (the `RailwayPathfinder.measure_along_walk` row of the pathfinding
        function table in design_new.md "Path And PathDispatcher").

        ``ordered_walk`` is the directed per-step list of the already-chosen
        walk; each step carries its ``tile`` and physical ``length_cm``.
        ``locator_a`` is the start (its ``distance_cm``, when set, is the
        already-traversed in-tile offset on its own step and is netted out so the
        partial first tile is not double-counted); ``locator_b`` is the end. The
        result is the absolute forward distance from ``locator_a`` to
        ``locator_b``: the sum of step lengths from ``locator_a``'s step through
        ``locator_b``'s step inclusive, minus ``locator_a``'s in-tile offset,
        clamped at zero.

        Used by the canonical lookahead (head→authority-end clearance) and
        platform-end pair comparison. When either locator's tile is absent from
        the walk, the projection falls back to the whole-walk length so callers
        still get a non-negative measurement.
        """
        steps = ordered_walk or []
        if not steps:
            return 0.0

        def _index_of(tile: Optional[Tuple[int, int]]) -> int:
            if tile is None:
                return -1
            for i, step in enumerate(steps):
                if tuple(step.tile) == tuple(tile):
                    return i
            return -1

        i_a = _index_of(locator_a.tile)
        i_b = _index_of(locator_b.tile)
        if i_a < 0 or i_b < 0:
            # Locator tile not on the walk: best-effort whole-walk length.
            total = sum(float(step.length_cm) for step in steps)
            return max(0.0, total - float(locator_a.distance_cm or 0.0))
        lo, hi = (i_a, i_b) if i_a <= i_b else (i_b, i_a)
        total = sum(float(steps[i].length_cm) for i in range(lo, hi + 1))
        return max(0.0, total - float(locator_a.distance_cm or 0.0))

    def _build_candidates(
        self, raw_walks: List[PathSegment], request: GraphWalkQuery
    ) -> List[GraphWalkCandidate]:
        """Build the full candidate list (with `ordered_walk` populated) for a
        set of raw walks.

        Pure function of the static graph -- called once per (start, end) at
        cache-build time, NEVER inside the per-call filter loop, so the per-step
        enrichment cost amortizes across every query that reuses the candidate.
        `candidate_rank` is the enumeration index over the deterministic raw
        walks, so the sort order is stable. Candidates are built for ALL raw
        walks (no filtering here); the dynamic filter is applied by the caller.
        """
        candidates: List[GraphWalkCandidate] = []
        for rank, walk in enumerate(raw_walks):
            dest_block_id = walk.blocks[-1] if walk.blocks else None
            dest_block = (
                self.network.blocks.get(dest_block_id)
                if dest_block_id is not None
                else None
            )
            terminal_direction = (
                self._terminal_direction(walk, dest_block)
                if dest_block is not None
                else None
            )
            candidates.append(
                GraphWalkCandidate(
                    tile_count=len(walk.tiles),
                    walk_length_cm=walk.distance_cm,
                    candidate_rank=rank,
                    end_a="end_a",
                    walk=walk,
                    ordered_walk=self._build_ordered_walk(walk),
                    ordered_tiles=list(walk.tiles) if request.return_tiles else None,
                    ordered_blocks=list(walk.blocks) if request.return_blocks else None,
                    terminal_block_id=dest_block_id,
                    terminal_block_direction=terminal_direction,
                )
            )
        return candidates

    def _build_ordered_walk(self, walk: PathSegment) -> List[GraphWalkStep]:
        """Build the canonical per-step directed walk for a raw PathSegment.

        Pure function of the static graph. Per-tile entry/exit edges come from
        the tunnel-aware ``RailwayNetwork.get_connection_edges``; ``block_direction``
        is decided once per contiguous block run from ``Block.ordered_tiles``
        endpoints (matching ``_terminal_direction``'s convention); switch state
        comes from the walk's recorded ``required_switches``; ``length_cm`` from
        the tile length. Called only from ``_build_candidates`` at cache-build
        time -- never per query.
        """
        tiles = walk.tiles
        n = len(tiles)
        if n == 0:
            return []
        net = self.network

        # Tunnel-aware per-tile edges. exit[i] = edge on tile i toward tile i+1;
        # entry[i] = edge on tile i from tile i-1.
        entry_edges: List[Optional[str]] = [None] * n
        exit_edges: List[Optional[str]] = [None] * n
        for i in range(n - 1):
            edges = net.get_connection_edges(tiles[i], tiles[i + 1])
            if edges is not None:
                exit_edges[i] = edges[0]
                entry_edges[i + 1] = edges[1]
        # Terminal tile exit = opposite of its entry (forward velocity vector),
        # matching _tile_exit_edge's terminal fallback.
        if entry_edges[n - 1] is not None and exit_edges[n - 1] is None:
            exit_edges[n - 1] = OPPOSITE_EDGE.get(entry_edges[n - 1])
        # Start tile entry = opposite of its exit (the frame's back edge).
        if exit_edges[0] is not None and entry_edges[0] is None:
            entry_edges[0] = OPPOSITE_EDGE.get(exit_edges[0])

        # block_direction per contiguous block run (decided once from the run's
        # entry tile vs the block's ordered-tile endpoints).
        block_dir: List[Optional[str]] = [None] * n
        i = 0
        while i < n:
            track = net.get_track(*tiles[i])
            bid = (
                track.block_id
                if track is not None and not track.is_switch
                else None
            )
            if bid is None:
                i += 1
                continue
            j = i
            while j < n:
                t = net.get_track(*tiles[j])
                if t is None or t.is_switch or t.block_id != bid:
                    break
                j += 1
            direction = self._block_run_direction(bid, tiles[i], entry_edges[i])
            for k in range(i, j):
                block_dir[k] = direction
            i = j

        steps: List[GraphWalkStep] = []
        for idx, tile in enumerate(tiles):
            track = net.get_track(*tile)
            if track is None:
                continue
            entry_e = entry_edges[idx] or "N"
            exit_e = exit_edges[idx] or entry_e
            if track.is_switch:
                # Switch tile length is position-dependent. The path locks the
                # switch in its required state, so the length the train will
                # traverse is the active length for that state -- a static value
                # that matches `_get_tile_length_cm` at traversal time (when the
                # live switch_state equals the required state).
                req = walk.required_switches.get(tile)
                length_cm = self._switch_length_cm(tile, track, req)
                steps.append(
                    GraphWalkStep(
                        step_index=len(steps),
                        resource_type="switch",
                        resource_id=track.switch_id or f"sw_{tile[0]}_{tile[1]}",
                        tile=tile,
                        entry_edge=entry_e,
                        exit_edge=exit_e,
                        switch_required_state=req,
                        length_cm=length_cm,
                    )
                )
            else:
                # Block tile length matches `_get_tile_length_cm` (the
                # block-divided per-tile length); static topology.
                length_cm = self.network._get_tile_length_cm(tile[0], tile[1])
                bid = track.block_id
                steps.append(
                    GraphWalkStep(
                        step_index=len(steps),
                        resource_type="block",
                        resource_id=bid or f"tile_{tile[0]}_{tile[1]}",
                        tile=tile,
                        entry_edge=entry_e,
                        exit_edge=exit_e,
                        block_id=bid,
                        block_direction=block_dir[idx],
                        length_cm=length_cm,
                    )
                )
        return steps

    def _switch_length_cm(self, tile, track, required_state) -> float:
        """Active length (cm) the path traverses through the switch at ``tile``.

        Mirrors ``RailwayNetwork._get_tile_length_cm``'s switch branch, but uses
        the path's ``required_state`` (the position the path locks the switch
        into) rather than the live ``switch_state`` -- so the value is static
        and equals the runtime tile length while the train traverses the path.
        """
        get_switch_at = getattr(self.network, "get_switch_at", None)
        sw = get_switch_at(tile[0], tile[1]) if get_switch_at else None
        if sw is None:
            return 0.0
        state = required_state if required_state is not None else getattr(
            track, "switch_state", 0
        )
        return sw.get_active_length(state)

    def _block_relative_direction(
        self,
        block,
        entry_tile: Tuple[int, int],
        entry_edge: Optional[str],
    ) -> Optional[str]:
        """Block-relative direction (``"forward"``/``"reverse"``) for a run
        entering ``block`` at ``entry_tile`` through ``entry_edge``.

        Multi-tile blocks: forward when the run enters at
        ``Block.ordered_tiles[0]``, reverse when it enters at the last ordered
        tile. Single-tile blocks have the same tile at both endpoints, so the
        entry-tile test cannot tell the two directions apart; the verdict comes
        from the entry edge against the block's forward-entry anchor via
        ``Block.block_direction_for_compass_entry`` (which returns None for a
        missing/unrecognized edge). Returns None when geometry is insufficient.

        Single home for the rule shared by ``_block_run_direction`` (cached
        geometry) and ``_terminal_direction`` (live terminal-direction filter),
        so the two always agree.
        """
        ordered = (getattr(block, "ordered_tiles", None) or []) if block else []
        if not ordered:
            return None
        first = (ordered[0].x, ordered[0].y)
        last = (ordered[-1].x, ordered[-1].y)
        if first == last:
            return block.block_direction_for_compass_entry(entry_edge)
        if entry_tile == first:
            return "forward"
        if entry_tile == last:
            return "reverse"
        return None

    def _block_run_direction(
        self,
        block_id: str,
        run_entry_tile: Tuple[int, int],
        run_entry_edge: Optional[str] = None,
    ) -> Optional[str]:
        """Block-relative direction for a block run entered at ``run_entry_tile``
        through ``run_entry_edge``. Resolves the block by id and applies the
        shared ``_block_relative_direction`` rule."""
        block = self.network.blocks.get(block_id)
        return self._block_relative_direction(block, run_entry_tile, run_entry_edge)

    def measure_distance(self, a, b) -> Optional[int]:
        """Tile-hop distance between two endpoints, ignoring switch state.

        Each endpoint is one of:
            - ``(x, y)`` tuple — a single tile;
            - block_id string — every tile in that block (multi-source);
            - platform_id string — every tile in that platform;
            - signal_id string — the signal's tile.

        Returns the minimum number of track-adjacent hops between any
        source tile and any target tile, ``0`` if the footprints overlap,
        or ``None`` if no track-connected path exists. Walks the layout
        as an undirected graph (switch positions ignored).
        """
        src_tiles = self._resolve_to_tiles(a)
        dst_tiles = set(self._resolve_to_tiles(b))
        if not src_tiles or not dst_tiles:
            return None
        src_set = set(src_tiles)
        if src_set & dst_tiles:
            return 0

        visited: Set[Tuple[int, int]] = set(src_set)
        queue: deque = deque((pos, 0) for pos in src_set)
        while queue:
            (cx, cy), dist = queue.popleft()
            track = self.network.get_track(cx, cy)
            if not track:
                continue
            for edge in track.edges:
                neighbor = self.network.get_neighbor(cx, cy, edge)
                if not neighbor:
                    continue
                npos = (neighbor.x, neighbor.y)
                if npos in visited:
                    continue
                opposite = OPPOSITE_EDGE.get(edge)
                if not opposite or opposite not in neighbor.edges:
                    continue
                if npos in dst_tiles:
                    return dist + 1
                visited.add(npos)
                queue.append((npos, dist + 1))
        return None

    def _resolve_to_tiles(self, target) -> List[Tuple[int, int]]:
        """Turn a measure_distance endpoint into the list of tiles it
        covers. Raises ``ValueError`` for an unknown string id."""
        if isinstance(target, tuple) and len(target) == 2:
            return [(int(target[0]), int(target[1]))]
        if isinstance(target, str):
            if target in self.network.blocks:
                return list(self.network.get_block_positions(target))
            if target in self.network.platforms:
                return list(self.network.platforms[target].positions)
            if target in self.network.signals:
                return [self.network.signals[target].position]
            raise ValueError(f"Unknown id for measure_distance: {target!r}")
        raise TypeError(
            f"measure_distance endpoint must be (x,y) or str id, got {type(target).__name__}"
        )

    def get_reachable_endpoints(
        self, start_block_id: str, include_stations: bool = True
    ) -> List[str]:
        """All blocks (and optionally stations) topologically reachable
        from ``start_block_id`` via tile-by-tile BFS.

        Walks through switch tiles (which have ``block_id = None``)
        instead of treating them as barriers. Does not consult current
        switch state and does not apply ``Block.allowed_travel_directions``;
        use ``find_ordered_walk`` when direction filtering matters.
        """
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
            for edge in sorted(track.edges):
                neighbor = self.network.get_neighbor(*pos, edge)
                if neighbor and neighbor.block_id != start_block_id:
                    queue: deque = deque([(pos[0], pos[1], edge)])
                    while queue:
                        curr_x, curr_y, exit_edge = queue.popleft()
                        step = (curr_x, curr_y, exit_edge)
                        if step in visited:
                            continue
                        visited.add(step)
                        if len(visited) > 1000:
                            break
                        next_track = self.network.get_neighbor(
                            curr_x, curr_y, exit_edge
                        )
                        if not next_track:
                            continue
                        next_x, next_y = next_track.x, next_track.y
                        next_block_id = next_track.block_id
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

        result = sorted(reachable_blocks)
        if include_stations:
            for station_id, station in self.network.stations.items():
                for pos in station.positions:
                    track = self.network.get_track(*pos)
                    if track and track.block_id in reachable_blocks:
                        if station_id not in result:
                            result.append(station_id)
                        break
        return result

    def get_reachable_endpoints_for_span(
        self,
        head_seeds: List[Tuple[Tuple[int, int], str]],
        occupied_tiles: List[Tuple[int, int]],
        occupied_blocks: List[str],
        include_stations: bool = True,
    ) -> List[str]:
        """Span-aware reachability for a placed train.

        Unlike ``get_reachable_endpoints`` (which fans out from every edge
        of every tile of a block), this walk starts ONLY from the train's
        two physical span ends, each in its outward direction, and never
        steps onto a tile the train's own body occupies. So it answers the
        real question the Quick Route tool asks: where can this train go,
        driving forward out of its head or in reverse out of its tail,
        without routing back through itself or through a switch its body is
        sitting on.

        ``head_seeds`` is the list of ``(tile, outward_edge)`` pairs for the
        two span ends (``outward_edge`` is the network edge pointing away
        from the body at that end). The tile-by-tile switch traversal is the
        same as ``get_reachable_endpoints``: it steps through switch tiles
        without consulting current switch state. ``occupied_blocks`` are
        removed from the result so the train's own blocks are never offered
        as destinations.
        """
        occupied_tile_set: Set[Tuple[int, int]] = {
            (int(x), int(y)) for x, y in occupied_tiles
        }
        occupied_block_set: Set[str] = {b for b in occupied_blocks if b}

        reachable_blocks: Set[str] = set()
        visited: Set[Tuple[int, int, str]] = set()

        for (seed_tile, out_edge) in head_seeds:
            if not out_edge:
                continue
            queue: deque = deque([(int(seed_tile[0]), int(seed_tile[1]), out_edge)])
            while queue:
                curr_x, curr_y, exit_edge = queue.popleft()
                step = (curr_x, curr_y, exit_edge)
                if step in visited:
                    continue
                visited.add(step)
                if len(visited) > 1000:
                    break
                next_track = self.network.get_neighbor(curr_x, curr_y, exit_edge)
                if not next_track:
                    continue
                next_x, next_y = next_track.x, next_track.y
                if (next_x, next_y) in occupied_tile_set:
                    continue  # never route back through our own body
                next_block_id = next_track.block_id
                if next_block_id and next_block_id not in occupied_block_set:
                    reachable_blocks.add(next_block_id)
                entry_edge = OPPOSITE_EDGE.get(exit_edge)
                if not entry_edge or entry_edge not in next_track.edges:
                    continue
                all_exits = next_track.get_exits(entry_edge, use_switch_state=False)
                for next_exit in all_exits:
                    queue.append((next_x, next_y, next_exit))

        result = sorted(reachable_blocks)
        if include_stations:
            for station_id, station in self.network.stations.items():
                for pos in station.positions:
                    track = self.network.get_track(*pos)
                    if track and track.block_id in reachable_blocks:
                        if station_id not in result:
                            result.append(station_id)
                        break
        return result

    # ------------------------------------------------------------------ private

    def _enumerate_walks(
        self,
        start_block_id: str,
        end_block_id: str,
        exclude: Set[str],
    ) -> List[PathSegment]:
        """Steps 1-2 of ``query_graph_walks``: BFS from every start-block
        border tile, then deduplicate by (block sequence, switch settings).

        Factored out so ``query_graph_walks`` can cache the result for the
        topology-only / no-exclusion case (a pure function of the static
        graph). Returns an empty list when the start block has no tiles.
        """
        start_positions = self.network.get_block_positions(start_block_id)
        if not start_positions:
            return []
        raw_walks: List[PathSegment] = []
        for start_pos in start_positions:
            track = self.network.get_track(*start_pos)
            if not track:
                continue
            # Sorted: enumeration order feeds first-seen dedup and
            # candidate_rank, so it must not depend on str-hash set order.
            for edge in sorted(track.edges):
                neighbor = self.network.get_neighbor(*start_pos, edge)
                if neighbor and neighbor.block_id != start_block_id:
                    raw_walks.extend(
                        self._bfs_walks(
                            start_pos,
                            edge,
                            start_block_id,
                            end_block_id,
                            exclude_resources=exclude,
                        )
                    )
        return self._deduplicate_walks(raw_walks)

    def _bfs_walks(
        self,
        start_pos: Tuple[int, int],
        start_exit: str,
        start_block_id: str,
        end_block_id: str,
        exclude_resources: Optional[Set[str]] = None,
    ) -> List[PathSegment]:
        """BFS through the network from one start (position, exit) pair.

        Traverses topology only: every topological exit is explored,
        whatever the current switch position, with optional resource
        exclusion.

        Args:
            start_pos: Starting tile coordinate.
            start_exit: Edge on ``start_pos`` to exit from first.
            start_block_id: Block ID of the starting tile.
            end_block_id: Block ID of the target tile.
            exclude_resources: Optional set of block or switch IDs to skip.
                Any tile whose block_id or switch_id is in this set
                is not entered during BFS.

        Returns:
            List of ``PathSegment`` representing every walk that reaches
            ``end_block_id``.
        """
        _exclude: Set[str] = exclude_resources or set()
        walks: List[PathSegment] = []
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
            next_track = self.network.get_neighbor(curr_x, curr_y, exit_edge)
            if not next_track:
                continue
            next_x, next_y = next_track.x, next_track.y

            # Exclude-resources filter.
            next_block_id = next_track.block_id
            next_switch_id = getattr(next_track, "switch_id", None)
            if next_block_id and next_block_id in _exclude:
                continue
            if next_switch_id and next_switch_id in _exclude:
                continue

            segment_distance = next_track.length * 100.0
            new_distance = distance + segment_distance
            new_path_tiles = path_tiles + [(next_x, next_y)]
            new_blocks = list(blocks)
            if next_block_id and next_block_id not in new_blocks:
                new_blocks.append(next_block_id)
            if next_block_id == end_block_id:
                # Walk forward through the rest of the destination block so
                # the path covers every tile inside it. Without this, the
                # BFS stops at the entry tile and a multi-tile destination
                # (e.g. a platform spanning several tiles) ends up with
                # authority for only its first tile — the train then stops
                # near the start of the platform instead of the far end.
                #
                # The per-hop entry edge is resolved through
                # `network.get_connection_edges`, which knows about tunnels;
                # naive `OPPOSITE_EDGE[exit]` only works for grid-adjacent
                # pairs and silently breaks the moment the train enters the
                # destination block across a tunnel (e.g. demo_4's
                # `(1,0,W) ↔ (11,10,N)`), because exit "W" and entry "N" are
                # not cardinal opposites.
                ext_tiles = list(new_path_tiles)
                ext_distance = new_distance
                prev_tile = (curr_x, curr_y)
                cx, cy = next_x, next_y
                while True:
                    cur_track = self.network.get_track(cx, cy)
                    if cur_track is None or cur_track.is_switch:
                        break
                    edges = self.network.get_connection_edges(
                        prev_tile, (cx, cy)
                    )
                    if edges is None:
                        break
                    cur_entry = edges[1]
                    if cur_entry not in cur_track.edges:
                        break
                    # Always use topology-only for intra-destination extension.
                    cur_exits = cur_track.get_exits(
                        cur_entry, use_switch_state=False
                    )
                    if len(cur_exits) != 1:
                        break
                    fwd_exit = cur_exits[0]
                    fwd_track = self.network.get_neighbor(cx, cy, fwd_exit)
                    if fwd_track is None:
                        break
                    fx, fy = fwd_track.x, fwd_track.y
                    if fwd_track.block_id != end_block_id:
                        break
                    ext_distance += fwd_track.length * 100.0
                    ext_tiles.append((fx, fy))
                    prev_tile = (cx, cy)
                    cx, cy = fx, fy
                walks.append(
                    PathSegment(
                        tiles=ext_tiles,
                        distance_cm=ext_distance,
                        required_switches=dict(switches),
                        blocks=new_blocks,
                    )
                )
                continue
            # Tunnel-aware entry edge on the next tile. Falls back to the
            # cardinal opposite for grid-adjacent pairs (where the lookup
            # returns the same answer), so the change is a no-op there.
            connection_edges = self.network.get_connection_edges(
                (curr_x, curr_y), (next_x, next_y)
            )
            entry_edge = (
                connection_edges[1]
                if connection_edges
                else OPPOSITE_EDGE.get(exit_edge)
            )
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
        return walks

    def _deduplicate_walks(
        self, walks: List[PathSegment]
    ) -> List[PathSegment]:
        """Drop walks that share the same block sequence + switch settings."""
        seen: Set[Tuple] = set()
        unique: List[PathSegment] = []
        for w in walks:
            key = (tuple(w.blocks), tuple(sorted(w.required_switches.items())))
            if key not in seen:
                seen.add(key)
                unique.append(w)
        return unique

    def _walk_passes_filters(
        self,
        candidate: GraphWalkCandidate,
        direction_constraint: Optional[str],
        manual_override: bool = False,
        train_min_radius_cm: Optional[float] = None,
        train_max_slope_percent: Optional[float] = None,
    ) -> bool:
        """Reject candidates that traverse a disabled resource, traverse a
        manual resource without operator override, traverse any block against
        its ``Block.allowed_travel_directions`` without operator override,
        traverse a block or switch leg whose curve radius is tighter than
        ``train_min_radius_cm``, climb a block steeper than
        ``train_max_slope_percent``, or violate ``direction_constraint``.

        Per the `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource): disabled blocks/switches are
        always rejected; manual blocks/switches and the
        ``Block.allowed_travel_directions`` policy are rejected/enforced when
        ``manual_override`` is False. The travel-direction policy applies to
        every block the walk traverses (design_new.md create_path step 2 and
        pathfinding-home item 6), not only the destination: a candidate that
        crosses a one-way block against its direction can never be acquired,
        so it must not survive the search. The ``direction_constraint`` (the
        caller's requested terminal direction) is always enforced — the
        override relaxes the block's policy, not the caller's request.
        """
        walk = candidate.walk
        # Disabled-block and manual-block filters.
        for block_id in walk.blocks:
            block = self.network.blocks.get(block_id)
            if block is None:
                continue
            if getattr(block, "disabled", False):
                return False
            if getattr(block, "manual", False) and not manual_override:
                return False

        # Disabled-switch and manual-switch filters.
        switches = getattr(self.network, "switches", None) or {}
        for pos in walk.required_switches.keys():
            track = self.network.get_track(*pos)
            switch_id = getattr(track, "switch_id", None) if track else None
            switch = None
            if switch_id and isinstance(switches, dict):
                switch = switches.get(switch_id)
            if switch is None and isinstance(switches, dict):
                for s in switches.values():
                    if (s.x, s.y) == pos:
                        switch = s
                        break
            if switch is not None and getattr(switch, "disabled", False):
                return False
            if (
                switch is not None
                and getattr(switch, "manual", False)
                and not manual_override
            ):
                return False

        # Curve-radius feasibility filter over the whole walk. A train cannot
        # navigate a curve tighter than its minimum radius, so any candidate
        # that traverses a block (``Block.min_radius_cm``) or switch leg
        # (``Switch.get_active_radius`` for the step's required state) whose
        # radius is below ``train_min_radius_cm`` is rejected. This is a
        # physical limit, not an availability policy, so ``manual_override``
        # does not relax it. ``None`` radius means the resource carries no
        # curve limit; ``train_min_radius_cm`` None (geometry-only callers)
        # skips the filter entirely.
        if train_min_radius_cm is not None:
            for step in candidate.ordered_walk or ():
                if step.resource_type == "switch":
                    switch = switches.get(step.resource_id)
                    if switch is None or step.switch_required_state is None:
                        continue
                    radius = switch.get_active_radius(step.switch_required_state)
                else:
                    block = self.network.blocks.get(step.block_id or step.resource_id)
                    radius = getattr(block, "min_radius_cm", None) if block else None
                if radius is not None and radius < train_min_radius_cm:
                    return False

        # Slope (grade) feasibility filter over the whole walk. A train stalls
        # on a climb steeper than its maximum climbable gradient, so any
        # candidate that climbs such a block is rejected. ``Block.grade_percent``
        # is signed (+ uphill in the block's forward direction); a reverse
        # traversal climbs the opposite sign, so the effective uphill grade is
        # negated for reverse steps. Only an uphill climb steeper than the train
        # can manage rejects -- a descent never does. Like the curve filter this
        # is a physical limit, so ``manual_override`` does not relax it. Skipped
        # when the caller supplies no positive max gradient (``None`` or the 0.0
        # ``Train.max_slope`` "unspecified" sentinel), or a step has no
        # classified block direction (cannot tell uphill from downhill).
        if train_max_slope_percent is not None and train_max_slope_percent > 0:
            # The block the walk starts from is one the train already occupies (its
            # current block or its live authority end), so it is not "climbed
            # into" — its grade must not exclude the walk. It is also the one block
            # whose traversal frame is inferred rather than derived from a real
            # predecessor edge (a mid-route start has no entry edge, so its
            # `block_direction` can mis-sign the grade on a non-straight block).
            # Both reasons say the same thing: skip the start block; gate only the
            # blocks ahead that the train would move into.
            start_block_id: Optional[str] = None
            for step in candidate.ordered_walk or ():
                if step.resource_type == "block":
                    start_block_id = step.block_id or step.resource_id
                    break
            for step in candidate.ordered_walk or ():
                if step.resource_type != "block" or step.block_direction is None:
                    continue
                step_block_id = step.block_id or step.resource_id
                if step_block_id == start_block_id:
                    continue
                block = self.network.blocks.get(step_block_id)
                grade = getattr(block, "grade_percent", None) if block else None
                if grade is None:
                    continue
                effective = grade if step.block_direction == "forward" else -grade
                if effective > train_max_slope_percent:
                    return False

        # Travel-direction policy filter over the whole walk. Each step's
        # ``block_direction`` comes from the cached ``ordered_walk`` (decided
        # once per contiguous block run at cache-build time, tunnel-aware).
        # ``None`` means the run's geometry cannot classify a direction (for
        # example a start run beginning mid-block, or a block with no
        # ``ordered_tiles``); such runs are not rejected here — the
        # reservation-time check in ``BlockManager.reserve_blocks_for_path``
        # remains the acquisition guard for them.
        if not manual_override:
            prev_block_id: Optional[str] = None
            for step in candidate.ordered_walk or ():
                block_id = step.block_id
                if not block_id:
                    prev_block_id = None
                    continue
                if block_id == prev_block_id:
                    continue
                prev_block_id = block_id
                if step.block_direction is None:
                    continue
                block = self.network.blocks.get(block_id)
                if block is None:
                    continue
                allowed: Set[str] = (
                    set(getattr(block, "allowed_travel_directions", set()))
                    or {"forward", "reverse"}
                )
                if step.block_direction not in allowed:
                    return False

        # Requested terminal-direction filter. `terminal_block_direction` is
        # precomputed once per candidate at cache-build time (_build_candidates)
        # from static walk/block geometry -- never recomputed here.
        if not walk.blocks:
            return True
        terminal_direction = candidate.terminal_block_direction
        if (
            direction_constraint is not None
            and terminal_direction is not None
            and direction_constraint != terminal_direction
        ):
            return False
        return True

    def _terminal_direction(
        self, walk: PathSegment, dest_block
    ) -> Optional[str]:
        """Derive the block-relative terminal traversal direction.

        Compares the walk's first tile in the destination block against
        ``Block.ordered_tiles`` to decide ``"forward"`` / ``"reverse"``.
        Returns ``None`` when geometry is insufficient to decide (caller
        treats that as "allowed"). Pure function of static walk/block
        geometry -- called only from ``_build_candidates`` at cache-build
        time; ``_walk_passes_filters`` reads the result off
        ``GraphWalkCandidate.terminal_block_direction`` instead of calling
        this again per query.
        """
        ordered_tiles = getattr(dest_block, "ordered_tiles", None) or []
        if not ordered_tiles or len(walk.tiles) < 2:
            return None

        # `Block` carries its id as `block_id`; `block_id` is the
        # `Tile` field pointing to the owning block.
        dest_block_id = dest_block.block_id
        first_dest_tile: Optional[Tuple[int, int]] = None
        first_dest_idx: Optional[int] = None
        for idx, tile in enumerate(walk.tiles):
            track = self.network.get_track(*tile)
            if track is not None and track.block_id == dest_block_id:
                first_dest_tile = tile
                first_dest_idx = idx
                break
        if first_dest_tile is None:
            return None

        # Entry edge (needed only for single-tile blocks) from the walk step
        # entering the first destination tile; None when the walk starts inside
        # the block, which the shared rule treats as undecidable.
        entry_edge: Optional[str] = None
        if first_dest_idx:
            prev = walk.tiles[first_dest_idx - 1]
            edges = self.network.get_connection_edges(prev, first_dest_tile)
            if edges:
                entry_edge = str(edges[1]).upper()
        return self._block_relative_direction(dest_block, first_dest_tile, entry_edge)
