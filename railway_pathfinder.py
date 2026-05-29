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

from enums import Direction
from track import OPPOSITE_EDGE
from path_utils import (
    DirectedBlockFrame,
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

        start_positions = self.network.get_block_positions(start_block_id)
        if not start_positions:
            return []

        # 1. Enumerate raw walks via BFS from all start-block border tiles.
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

        # 2. Deduplicate by (block sequence, switch settings).
        raw_walks = self._deduplicate_walks(raw_walks)

        # 3. Apply direction-aware filters; wrap survivors into
        #    GraphWalkCandidate with all new fields populated.
        direction_constraint = request.requested_destination_direction
        manual_override = request.manual_override
        candidates: List[GraphWalkCandidate] = []
        for rank, walk in enumerate(raw_walks):
            if not self._walk_passes_filters(
                walk, direction_constraint, manual_override=manual_override
            ):
                continue

            # Determine terminal block info.
            dest_block = self.network.blocks.get(end_block_id)
            terminal_dir = self._terminal_direction(walk, dest_block) if dest_block else None

            # Build terminal frame.
            terminal_frame = self._build_terminal_frame(
                walk, end_block_id, terminal_dir
            )

            # Build ordered_walk steps.
            ordered_walk_steps = self._build_ordered_walk(walk)

            # Build start/terminal locators.
            start_tile = walk.tiles[0] if walk.tiles else request.start_locator.tile
            terminal_tile = walk.tiles[-1] if walk.tiles else request.end_locator.tile
            start_loc = GraphLocator(
                tile=start_tile,
                block_id=start_block_id,
                resource_type="block",
                resource_id=start_block_id,
            )
            terminal_loc = GraphLocator(
                tile=terminal_tile,
                block_id=end_block_id,
                resource_type="block",
                resource_id=end_block_id,
                edge=terminal_frame.exit_edge if terminal_frame else None,
            )

            candidates.append(
                GraphWalkCandidate(
                    tile_count=len(walk.tiles),
                    walk_length_cm=walk.distance_cm,
                    candidate_rank=rank,
                    end_a="end_a",
                    walk=walk,
                    ordered_walk=ordered_walk_steps,
                    ordered_tiles=list(walk.tiles) if request.return_tiles else None,
                    ordered_blocks=list(walk.blocks) if request.return_blocks else None,
                    start_locator=start_loc,
                    terminal_locator=terminal_loc,
                    terminal_block_id=end_block_id,
                    terminal_block_direction=terminal_dir,
                    terminal_frame=terminal_frame,
                )
            )

        # 4. Sort by canonical comparison key.
        candidates.sort(key=lambda c: c.comparison_key())
        return candidates

    def measure_along_walk(
        self,
        ordered_walk: List["GraphWalkStep"],
        locator_a: GraphLocator,
        locator_b: GraphLocator,
    ) -> float:
        """Return the centimeter distance along a pre-chosen walk between two
        locators.

        Per design_new.md "Pathfinding home" (line 1940): in-walk centimeter
        distance projection between two locators on an already-chosen walk,
        used by lookahead and platform-end pair comparison.

        Both locators are matched to the closest step in ``ordered_walk`` by
        tile coordinate. The returned distance is always non-negative and
        measures the absolute span between the matched steps, including the
        fractional in-tile distance encoded in each locator's ``distance_cm``
        field.

        When a locator's tile is not found in the walk, the nearest walk
        endpoint is used as a fallback so the call never raises.

        Args:
            ordered_walk: List of ``GraphWalkStep`` from a
                ``GraphWalkCandidate.ordered_walk``.
            locator_a: First locator.
            locator_b: Second locator.

        Returns:
            Non-negative centimeter distance along the walk between the two
            locators.
        """
        if not ordered_walk:
            return 0.0

        def _find_step_idx(locator: GraphLocator) -> int:
            """Return the index of the first step whose tile matches, or the
            nearest endpoint index."""
            for i, step in enumerate(ordered_walk):
                if step.tile == locator.tile:
                    return i
            return 0

        idx_a = _find_step_idx(locator_a)
        idx_b = _find_step_idx(locator_b)

        if idx_a == idx_b:
            # Both locators on the same step tile.
            off_a = locator_a.distance_cm or 0.0
            off_b = locator_b.distance_cm or 0.0
            return abs(off_b - off_a)

        # Ensure a < b for accumulation.
        lo, hi = (idx_a, idx_b) if idx_a <= idx_b else (idx_b, idx_a)
        lo_locator = locator_a if idx_a <= idx_b else locator_b
        hi_locator = locator_b if idx_a <= idx_b else locator_a

        # Accumulate length_cm for steps lo..hi-1 (the steps between tiles).
        total = 0.0
        for i in range(lo, hi):
            total += ordered_walk[i].length_cm

        # Subtract the in-tile offset at lo_locator (distance already
        # consumed within the entry step's tile) and add the in-tile
        # offset at hi_locator.
        lo_offset = lo_locator.distance_cm or 0.0
        hi_offset = hi_locator.distance_cm or 0.0
        total = total - lo_offset + hi_offset
        return max(0.0, total)

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

    # ------------------------------------------------------------------ private

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

    def _build_terminal_frame(
        self,
        walk: PathSegment,
        end_block_id: str,
        block_direction: Optional[str],
    ) -> Optional["DirectedBlockFrame"]:
        """Build the ``DirectedBlockFrame`` for the terminal block of a walk.

        Identifies which tiles in the walk belong to ``end_block_id``, derives
        entry/exit edges from consecutive tile steps, and converts tile
        coordinates to path-tile tuples (with Direction inferred from step
        delta). Returns ``None`` when no terminal block tiles can be found.
        """
        # Collect tile slices that are in the terminal block.
        dest_tiles: List[Tuple[int, int]] = []
        for tile in walk.tiles:
            track = self.network.get_track(*tile)
            if track and track.block_section_id == end_block_id:
                dest_tiles.append(tile)

        if not dest_tiles:
            return None

        # Derive entry_edge: the edge on the first destination tile that the
        # walk entered from (i.e. the direction from the preceding tile to the
        # first destination tile).
        entry_edge = "N"  # fallback
        first_dest = dest_tiles[0]
        idx = walk.tiles.index(first_dest)
        if idx > 0:
            prev_tile = walk.tiles[idx - 1]
            dx = first_dest[0] - prev_tile[0]
            dy = first_dest[1] - prev_tile[1]
            _DELTA_TO_EDGE = {(0, 1): "N", (1, 0): "E", (0, -1): "S", (-1, 0): "W"}
            entry_edge = _DELTA_TO_EDGE.get((dx, dy), "N")

        # Derive exit_edge: the edge the walk left the last destination tile on.
        exit_edge = entry_edge  # fallback: same as entry for single-tile block
        last_dest = dest_tiles[-1]
        last_idx = walk.tiles.index(last_dest)
        if last_idx + 1 < len(walk.tiles):
            next_tile = walk.tiles[last_idx + 1]
            dx = next_tile[0] - last_dest[0]
            dy = next_tile[1] - last_dest[1]
            _DELTA_TO_EDGE = {(0, 1): "N", (1, 0): "E", (0, -1): "S", (-1, 0): "W"}
            exit_edge = _DELTA_TO_EDGE.get((dx, dy), entry_edge)
        else:
            # Walk ends at the last destination tile; use entry edge as exit.
            exit_edge = entry_edge

        # Convert tile sequence to (x, y, Direction) path-tile entries.
        _EDGE_TO_DIR = {
            "N": Direction.NORTH,
            "S": Direction.SOUTH,
            "E": Direction.EAST,
            "W": Direction.WEST,
        }
        entry_dir = _EDGE_TO_DIR.get(entry_edge, Direction.NORTH)
        path_tile_entries: List[Tuple[int, int, Direction]] = []
        for i, (tx, ty) in enumerate(dest_tiles):
            if i + 1 < len(dest_tiles):
                nx, ny = dest_tiles[i + 1]
                dx, dy = nx - tx, ny - ty
                _DELTA_TO_EDGE2 = {
                    (0, 1): "N",
                    (1, 0): "E",
                    (0, -1): "S",
                    (-1, 0): "W",
                }
                edge_str = _DELTA_TO_EDGE2.get((dx, dy), entry_edge)
                tile_dir = _EDGE_TO_DIR.get(edge_str, entry_dir)
            else:
                tile_dir = _EDGE_TO_DIR.get(exit_edge, entry_dir)
            path_tile_entries.append((tx, ty, tile_dir))

        return DirectedBlockFrame(
            block_id=end_block_id,
            block_direction=block_direction or "forward",
            path_tiles=path_tile_entries,
            entry_edge=entry_edge,
            exit_edge=exit_edge,
        )

    def _build_ordered_walk(
        self, walk: PathSegment
    ) -> List["GraphWalkStep"]:
        """Build a list of ``GraphWalkStep`` entries from a raw ``PathSegment``.

        Each tile in the walk becomes one step.  Entry/exit edges are derived
        from consecutive tile deltas.  Block and switch resource metadata is
        populated from the network's track registry.
        """
        steps: List[GraphWalkStep] = []
        tiles = walk.tiles
        required_switches = walk.required_switches

        _DELTA_TO_EDGE = {(0, 1): "N", (1, 0): "E", (0, -1): "S", (-1, 0): "W"}
        _OPPOSITE_LOCAL = {"N": "S", "S": "N", "E": "W", "W": "E"}

        for i, (tx, ty) in enumerate(tiles):
            track = self.network.get_track(tx, ty)

            # Derive entry edge from previous tile.
            if i == 0:
                entry_edge_here = "N"  # start tile: entry direction unknown; use fallback
            else:
                ptx, pty = tiles[i - 1]
                dx, dy = tx - ptx, ty - pty
                fwd_edge = _DELTA_TO_EDGE.get((dx, dy), "N")
                entry_edge_here = fwd_edge

            # Derive exit edge from next tile.
            if i + 1 < len(tiles):
                ntx, nty = tiles[i + 1]
                dx, dy = ntx - tx, nty - ty
                exit_edge_here = _DELTA_TO_EDGE.get((dx, dy), "N")
            else:
                # Last tile: use opposite of entry as a reasonable guess.
                exit_edge_here = _OPPOSITE_LOCAL.get(entry_edge_here, "N")

            block_id_here: Optional[str] = None
            switch_id_here: Optional[str] = None
            is_switch = False
            length_cm = 50.0  # default tile length if track not found

            if track:
                block_id_here = track.block_section_id
                switch_id_here = getattr(track, "switch_id", None)
                is_switch = getattr(track, "is_switch", False)
                length_cm = getattr(track, "length", 0.5) * 100.0

            if is_switch:
                resource_type_here = "switch"
                resource_id_here = switch_id_here or f"sw_{tx}_{ty}"
                req_state = required_switches.get((tx, ty))
            else:
                resource_type_here = "block"
                resource_id_here = block_id_here or f"tile_{tx}_{ty}"
                req_state = None

            # Determine block direction for block steps.
            block_dir: Optional[str] = None
            if not is_switch and block_id_here:
                dest_block = self.network.blocks.get(block_id_here)
                if dest_block:
                    # Build a minimal single-tile walk to reuse _terminal_direction.
                    dummy_walk = PathSegment(
                        tiles=[(tx, ty)],
                        distance_cm=length_cm,
                        required_switches={},
                        blocks=[block_id_here],
                    )
                    block_dir = self._terminal_direction(dummy_walk, dest_block)

            steps.append(
                GraphWalkStep(
                    step_index=i,
                    resource_type=resource_type_here,
                    resource_id=resource_id_here,
                    tile=(tx, ty),
                    entry_edge=entry_edge_here,
                    exit_edge=exit_edge_here,
                    block_id=block_id_here,
                    block_direction=block_dir,
                    switch_required_state=req_state,
                    length_cm=length_cm,
                )
            )

        return steps

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
