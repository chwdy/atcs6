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

from enums import DIRECTION_OFFSETS
from track import OPPOSITE_EDGE
from path_utils import GraphWalkCandidate
from runtime_payloads import PathSegment

if TYPE_CHECKING:
    from network import RailwayNetwork


# Edge offsets for navigation (canonical map from cardinal direction to dx/dy).
EDGE_OFFSETS = DIRECTION_OFFSETS


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
    ) -> List[GraphWalkCandidate]:
        """Enumerate, filter, and rank directed walks between two blocks.

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
            ``candidate_rank`` ASC, ``end_a`` dead-heat). The list is
            empty when no walk satisfies every filter.
        """
        if start_block_id not in self.network.blocks:
            return []
        if end_block_id not in self.network.blocks:
            return []

        start_positions = self.network.get_block_positions(start_block_id)
        if not start_positions:
            return []

        # 1. Enumerate raw walks.
        walks: List[PathSegment] = []
        for start_pos in start_positions:
            track = self.network.get_track(*start_pos)
            if not track:
                continue
            for edge in track.edges:
                neighbor = self.network.get_neighbor(*start_pos, edge)
                if neighbor and neighbor.block_section_id != start_block_id:
                    walks.extend(
                        self._bfs_walks(
                            start_pos, edge, start_block_id, end_block_id
                        )
                    )

        # 2. Deduplicate by (block sequence, switch settings).
        walks = self._deduplicate_walks(walks)

        # 3. Apply direction-aware filters and wrap survivors.
        candidates: List[GraphWalkCandidate] = []
        for rank, walk in enumerate(walks):
            if not self._walk_passes_filters(walk, direction_constraint):
                continue
            candidates.append(
                GraphWalkCandidate(
                    tile_count=len(walk.tiles),
                    walk_length_cm=walk.distance_cm,
                    candidate_rank=rank,
                    end_a="end_a",
                    walk=walk,
                )
            )

        # 4. Sort by canonical comparison key.
        candidates.sort(key=lambda c: c.comparison_key())

        if max_candidates is not None and max_candidates >= 0:
            candidates = candidates[:max_candidates]
        return candidates

    # ------------------------------------------------------------------ private

    def _bfs_walks(
        self,
        start_pos: Tuple[int, int],
        start_exit: str,
        start_block_id: str,
        end_block_id: str,
    ) -> List[PathSegment]:
        """BFS through the network from one start (position, exit) pair.

        Returns the geometry of every walk that reaches ``end_block_id``.
        """
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
                walks.append(
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
    ) -> bool:
        """Reject walks that traverse a disabled resource or violate
        ``Block.allowed_travel_directions`` / ``direction_constraint``.
        """
        # Disabled-block filter.
        for block_id in walk.blocks:
            block = self.network.blocks.get(block_id)
            if block is None:
                continue
            if getattr(block, "disabled", False):
                return False

        # Disabled-switch filter.
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

        # Direction filter against the destination block.
        if not walk.blocks:
            return True
        dest_block_id = walk.blocks[-1]
        dest_block = self.network.blocks.get(dest_block_id)
        if dest_block is None:
            return True
        terminal_direction = self._terminal_direction(walk, dest_block)
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

        dest_block_id = dest_block.block_section_id
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
