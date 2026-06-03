"""
Railway Pathfinder

Per design_new.md "Pathfinding home" (line 605, line 1610-1693):
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
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

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

        Returns:
            List of ``GraphWalkCandidate`` sorted in winning order
            (``tile_count`` ASC, ``walk_length_cm`` ASC,
            ``candidate_rank`` ASC, ``end_a`` dead-heat). Empty when no
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
            switch_handling="topology_only",
            requested_destination_direction=direction_constraint,
            return_tiles=True,
            return_blocks=True,
            manual_override=manual_override,
        )
        candidates = self.query_graph_walks(request)
        if max_candidates is not None and max_candidates >= 0:
            candidates = candidates[:max_candidates]
        return candidates

    def query_graph_walks(
        self, request: GraphWalkQuery
    ) -> List[GraphWalkCandidate]:
        """Canonical operational graph walk query.

        Per design_new.md "Pathfinding home" (lines 1926–1942): accepts a
        ``GraphWalkQuery`` with tile-precise locators, skips disabled
        blocks/switches, respects or ignores current switch state according to
        ``request.switch_handling``, rejects candidates whose terminal
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
            ``(tile_count, walk_length_cm, candidate_rank)`` ascending.
        """
        start_block_id = request.start_locator.block_id
        end_block_id = request.end_locator.block_id

        if not start_block_id or start_block_id not in self.network.blocks:
            return []
        if not end_block_id or end_block_id not in self.network.blocks:
            return []

        use_switch_state = request.switch_handling == "respect_current_state"
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
            not use_switch_state and not exclude
            and request.return_tiles and request.return_blocks
        )
        candidates: Optional[List[GraphWalkCandidate]] = (
            self.network.get_cached_walks(start_block_id, end_block_id)
            if cacheable
            else None
        )
        if candidates is None:
            raw_walks = self._enumerate_walks(
                start_block_id, end_block_id, use_switch_state, exclude
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
                c.walk, direction_constraint, manual_override=manual_override
            )
        ]
        surviving.sort(key=lambda c: c.comparison_key())
        return surviving

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
                track.block_section_id
                if track is not None and not track.is_switch
                else None
            )
            if bid is None:
                i += 1
                continue
            j = i
            while j < n:
                t = net.get_track(*tiles[j])
                if t is None or t.is_switch or t.block_section_id != bid:
                    break
                j += 1
            direction = self._block_run_direction(bid, tiles[i])
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
                bid = track.block_section_id
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

    def _block_run_direction(
        self, block_id: str, run_entry_tile: Tuple[int, int]
    ) -> Optional[str]:
        """Block-relative direction for a block run entered at ``run_entry_tile``.

        Forward when the run enters at ``Block.ordered_tiles[0]``, reverse when
        it enters at the last ordered tile, else None. Mirrors
        ``_terminal_direction`` so the cached geometry and the live
        terminal-direction filter agree.
        """
        block = self.network.blocks.get(block_id)
        ordered = (getattr(block, "ordered_tiles", None) or []) if block else []
        if not ordered:
            return None
        if run_entry_tile == (ordered[0].x, ordered[0].y):
            return "forward"
        if run_entry_tile == (ordered[-1].x, ordered[-1].y):
            return "reverse"
        return None

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

        Walks through switch tiles (which have ``block_section_id = None``)
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
            for edge in track.edges:
                neighbor = self.network.get_neighbor(*pos, edge)
                if neighbor and neighbor.block_section_id != start_block_id:
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
                next_block_id = next_track.block_section_id
                if next_block_id and next_block_id not in occupied_block_set:
                    reachable_blocks.add(next_block_id)
                entry_edge = OPPOSITE_EDGE.get(exit_edge)
                if not entry_edge or entry_edge not in next_track.edges:
                    continue
                all_exits = next_track.get_exits(entry_edge, use_switch_state=False)
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

    # ------------------------------------------------------------------ private

    def _enumerate_walks(
        self,
        start_block_id: str,
        end_block_id: str,
        use_switch_state: bool,
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
            for edge in track.edges:
                neighbor = self.network.get_neighbor(*start_pos, edge)
                if neighbor and neighbor.block_section_id != start_block_id:
                    raw_walks.extend(
                        self._bfs_walks_with_switch_mode(
                            start_pos,
                            edge,
                            start_block_id,
                            end_block_id,
                            use_switch_state=use_switch_state,
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
    ) -> List[PathSegment]:
        """Thin shim kept for compatibility.  Delegates to the generalized
        BFS helper with topology-only mode (switch state ignored)."""
        return self._bfs_walks_with_switch_mode(
            start_pos,
            start_exit,
            start_block_id,
            end_block_id,
            use_switch_state=False,
            exclude_resources=set(),
        )

    def _bfs_walks_with_switch_mode(
        self,
        start_pos: Tuple[int, int],
        start_exit: str,
        start_block_id: str,
        end_block_id: str,
        use_switch_state: bool = False,
        exclude_resources: Optional[Set[str]] = None,
    ) -> List[PathSegment]:
        """BFS through the network from one start (position, exit) pair.

        Generalised form that supports switch-state-aware traversal and
        resource exclusion.  ``_bfs_walks`` delegates here with
        ``use_switch_state=False`` and an empty exclusion set to preserve
        the original topology-only behaviour.

        Args:
            start_pos: Starting tile coordinate.
            start_exit: Edge on ``start_pos`` to exit from first.
            start_block_id: Block ID of the starting tile.
            end_block_id: Block ID of the target tile.
            use_switch_state: When ``True``, only follows the switch throw
                that is currently active (``Switch.position``); when
                ``False``, explores all topological exits.
            exclude_resources: Optional set of block or switch IDs to skip.
                Any tile whose block_section_id or switch_id is in this set
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
            next_block_id = next_track.block_section_id
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
                    if fwd_track.block_section_id != end_block_id:
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
            all_exits = next_track.get_exits(
                entry_edge, use_switch_state=use_switch_state
            )
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
        walk: PathSegment,
        direction_constraint: Optional[str],
        manual_override: bool = False,
    ) -> bool:
        """Reject walks that traverse a disabled resource, traverse a manual
        resource without operator override, violate
        ``Block.allowed_travel_directions`` without operator override, or
        violate ``direction_constraint``.

        Per the `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource): disabled blocks/switches are
        always rejected; manual blocks/switches and the
        ``Block.allowed_travel_directions`` policy are rejected/enforced when
        ``manual_override`` is False. The ``direction_constraint`` (the
        operator's requested terminal direction) is always enforced — the
        override relaxes the block's policy, not the caller's request.
        """
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

        # Direction filter against the destination block.
        if not walk.blocks:
            return True
        dest_block_id = walk.blocks[-1]
        dest_block = self.network.blocks.get(dest_block_id)
        if dest_block is None:
            return True
        terminal_direction = self._terminal_direction(walk, dest_block)
        if not manual_override:
            allowed: Set[str] = (
                set(getattr(dest_block, "allowed_travel_directions", set()))
                or {"forward", "reverse"}
            )
            if terminal_direction is not None and terminal_direction not in allowed:
                return False
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
        treats that as "allowed").
        """
        ordered_tiles = getattr(dest_block, "ordered_tiles", None) or []
        if not ordered_tiles or len(walk.tiles) < 2:
            return None

        # `Block` carries its id as `block_id`; `block_section_id` is a
        # `TrackSegment` field pointing to the owning block. The original
        # code conflated the two — never noticed because `ordered_tiles`
        # used to be empty in v1 runtime, which short-circuits this method.
        dest_block_id = dest_block.block_id
        # Find the first tile that belongs to the destination block.
        first_dest_tile: Optional[Tuple[int, int]] = None
        for tile in walk.tiles:
            track = self.network.get_track(*tile)
            if track is not None and track.block_section_id == dest_block_id:
                first_dest_tile = tile
                break
        if first_dest_tile is None:
            return None

        forward_first = (ordered_tiles[0].x, ordered_tiles[0].y)
        forward_last = (ordered_tiles[-1].x, ordered_tiles[-1].y)
        if first_dest_tile == forward_first:
            return "forward"
        if first_dest_tile == forward_last:
            return "reverse"
        return None
