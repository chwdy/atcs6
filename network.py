"""
Railway Network Management

Definition:
This module provides the RailwayNetwork class which manages the complete railway infrastructure
including tracks, switches and block sections. It serves as the central repository and
coordinator for all railway system components.
"""

from collections import deque
import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from track import Tile, BIT_TO_EDGES
from block import Block
from entity_ids import block_id_for_segment
from switch_manager import SwitchManager
from segment import Segment
from switch_power import SwitchOutputOwner


logger = logging.getLogger(__name__)


DEFAULT_TILE_LENGTH_CM = 50.0
DEFAULT_TRAIN_LENGTH_CM = 50.0


# Shared grid-adjacency edge offsets. Hoisted to module scope so the per-tile
# graph helpers (get_connection_edges / get_neighbor / _get_neighbor_coords) do
# not rebuild these constant dicts on every call — those helpers run millions of
# times per simulation run. Coordinate convention:
#   N (North) = Y+ ; E (East) = X+ ; S (South) = Y- ; W (West) = X-
_EDGE_OFFSETS: Dict[str, Tuple[int, int]] = {
    "N": (0, 1),
    "E": (1, 0),
    "S": (0, -1),
    "W": (-1, 0),
}
_OFFSET_TO_EDGE: Dict[Tuple[int, int], str] = {
    v: k for k, v in _EDGE_OFFSETS.items()
}
_OPPOSITE_EDGE: Dict[str, str] = {"N": "S", "S": "N", "E": "W", "W": "E"}


def _bits_form_switch(bits: int) -> bool:
    """True if the bit pattern alone would make a tile a switch (any port
    with 2+ exits). Mirrors `Tile.switch_points` without needing
    a live segment instance."""
    edge_exits = {"N": 0, "E": 0, "S": 0, "W": 0}
    for bit in range(6):
        if bits & (1 << bit):
            e1, e2 = BIT_TO_EDGES[bit]
            edge_exits[e1] += 1
            edge_exits[e2] += 1
    return any(c >= 2 for c in edge_exits.values())


class RailwayNetwork(SwitchOutputOwner):
    """
    Central railway network management system.

    Manages all railway infrastructure components including tracks, switches,
    and block sections.
    """

    def __init__(self):
        self.tracks: Dict[Tuple[int, int], Tile] = {}
        self.blocks: Dict[str, Block] = {}
        self.switches: Dict[str, "Switch"] = {}  # NEW: Switch entity management
        # Segments are geometry; they live inside their owning `Block`
        # (design_new.md "Block": a block is a switch-free connected run of one
        # or more segments sharing one power feed). `self.segments` and
        # `segment_to_block_id(...)` below are the network-level registry reads
        # derived from `Block.segment_ids`, so they can never drift from the
        # blocks themselves.


        # Symmetric map of tunnel endpoints. A tunnel pairs two dead-end
        # edges so they behave as a single tile-to-tile connection. Keyed
        # by ``(x, y, edge)``; both directions of every pair are stored so
        # lookups are O(1). Written only via ``add_tunnel`` / ``remove_tunnel``
        # / ``_sweep_stale_tunnels``.
        self.tunnels: Dict[
            Tuple[int, int, str], Tuple[int, int, str]
        ] = {}

        self.switch_manager: Optional[SwitchManager] = None
        self.power_controller: Optional["BlockPowerController"] = None
        self.switch_output_interface: Optional["SwitchOutput"] = None
        # Owns the throw output above and the switch's rail feed; the only
        # caller of `SwitchOutput` (design_new.md "Power", Switch
        # power-controller role).
        self.switch_power_controller: Optional["SwitchPowerController"] = None

        # Reverse index from tile -> owning segment. A tile belongs to a
        # `Segment`; the segment is grouped into a `Block`. Maintained in sync
        # by the segment mutators. Readers call tile_to_segment_id (geometry)
        # and tile_to_block_id (power and occupancy) below.
        self._tile_to_segment: Dict[Tuple[int, int], str] = {}

        # Reverse index block id -> its Tiles, derived from
        # _tile_to_segment and built lazily by _block_tiles_index(). Turns
        # get_block_tracks() from an O(all tracks) scan into an O(block)
        # lookup. None
        # means "rebuild on next read"; invalidate_pathfinding_cache() resets
        # it on every topology change (none of which run in the tick loop).
        self._block_index_cache: Optional[Dict[str, List["Tile"]]] = None
        # Companion segment id -> Tiles index, built in the same pass as
        # _block_index_cache and dropped by the same invalidation.
        self._segment_index_cache: Optional[Dict[str, List["Tile"]]] = None


        # Helper modules (segment geometry, block grouping). Lazy-initialised
        # on first access to keep `__init__` cheap.
        self._segment_geometry_manager = None
        self._block_grouping_manager = None


    def _get_segment_geometry_manager(self):
        """Lazy-init `SegmentGeometryManager`. Segment geometry / topology
        mutation lives in `segment_geometry_manager.py`; this network
        keeps doc-canonical method names as forwarders."""
        if self._segment_geometry_manager is None:
            from segment_geometry_manager import SegmentGeometryManager

            self._segment_geometry_manager = SegmentGeometryManager(self)
        return self._segment_geometry_manager


    def _get_block_grouping_manager(self):
        """Lazy-init `BlockGroupingManager`. Block membership (`segment_ids`)
        CRUD lives there; the `blocks` dict that holds the grouping stays on the
        network; the manager reads / writes them through its back-reference.
        """
        if self._block_grouping_manager is None:
            from block_grouping_manager import BlockGroupingManager

            self._block_grouping_manager = BlockGroupingManager(self)
        return self._block_grouping_manager


    def _ensure_managers(self):
        """Construct the SwitchManager if not yet wired."""
        if self.switch_manager is None:
            self.switch_manager = SwitchManager(self.switches, network=self)

    # === Tile -> owning entity reverse indices (v1) ===

    def tile_to_segment_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning Segment id, or None.

        Per design_new.md: a tile belongs to a `Segment`. This is the stored
        membership; block membership is derived from it by `tile_to_block_id`.
        Geometry callers (tile ordering, continuity, length) want this one.
        """
        return self._tile_to_segment.get((x, y))

    def tile_to_block_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning Block id, or None.

        Resolves tile -> segment -> block: the tile's segment is looked up in
        `_tile_to_segment`, then the block that groups that segment is found
        via `segment_to_block_id`. For a block that owns a single segment the
        two ids are equal; for a block grouping several segments they differ,
        and this returns the grouping block.

        All "which block owns this tile?" lookups go through this method —
        power and occupancy are block concerns.
        """
        segment_id = self._tile_to_segment.get((x, y))
        if segment_id is None:
            return None
        return self.segment_to_block_id(segment_id)


    def _set_tile_segment(self, x: int, y: int, segment_id: Optional[str]) -> None:
        """Update the tile -> segment reverse index.

        Network-internal helper. Use this whenever a tile's segment membership
        changes (segment creation, extension, deletion, split, etc.).
        """
        key = (x, y)
        if segment_id is None:
            self._tile_to_segment.pop(key, None)
        else:
            self._tile_to_segment[key] = segment_id
        # Segment membership resolves to block membership, which feeds the BFS
        # walk enumeration; a change here makes any cached walk for the
        # affected blocks stale.
        self.invalidate_pathfinding_cache()


    # === Pathfinding walk cache (owned here; the static graph lives here) ===

    def invalidate_pathfinding_cache(self) -> None:
        """Drop the derived tile indices. Called by each topology mutator
        (block membership, track add/remove, switch add/remove, tunnel
        changes, full reload)."""
        # The block->tracks / segment->tracks reverse indices are derived from
        # tile->segment membership and the segment->block grouping, so they are
        # stale after any topology change. Drop them; the next
        # get_block_tracks() rebuilds both from the new topology.
        self._block_index_cache = None
        self._segment_index_cache = None

    def clear(self) -> None:
        """Reset the network to empty.

        Clears every entity dict AND the derived tile->segment reverse index and the walk/block caches. The reverse indices are not
        stored on the entity dicts, so emptying ``tracks`` / ``blocks`` alone
        leaves them stale: a new track drawn on a tile a previous layout used
        would read its old block via ``tile_to_block_id()`` and block creation
        would reject it. This is the single reset used by the "New Layout"
        action and by ``from_dict``'s "clear existing" step.
        """
        self.tracks.clear()
        self.blocks.clear()
        self.switches.clear()
        self.tunnels.clear()
        # Derived tile -> owner reverse indices. Not held on any entity dict,
        # so they must be cleared explicitly to avoid stale block membership on
        # reused tiles.
        self._tile_to_segment.clear()
        # Drops the block and segment tile indices.
        self.invalidate_pathfinding_cache()

    def add_track(self, track: Tile):
        """Add a track segment to the network and propagate updates"""
        key = (track.x, track.y)
        self.tracks[key] = track

        # Move any pre-attachment local-fallback segment_id into the network
        # reverse index so that reads via the property continue to see the
        # same value after attach.
        local_segment = track._segment_id_local
        track.network = self
        if local_segment is not None:
            track.segment_id = local_segment

        # Propagate to this tile and neighbors
        self.propagate_from_tile(track.x, track.y)

    def get_track(self, x: int, y: int) -> Optional[Tile]:
        """Get track segment at specified position"""
        return self.tracks.get((x, y))

    def get_neighbor(self, x: int, y: int, edge: str) -> Optional[Tile]:
        """Get neighbor tile in the specified direction.

        Args:
            x, y: Current tile coordinates
            edge: Direction to look ('N', 'E', 'S', 'W')

        Returns:
            Tile if neighbor exists, None otherwise. When the grid
            cell beyond ``edge`` is empty, the lookup falls through to
            ``self.tunnels`` and returns the far tile of a tunnel anchored
            at ``(x, y, edge)`` if one is registered.

        Coordinate system:
            N (North) = Y+ direction (0, +1)
            E (East)  = X+ direction (+1, 0)
            S (South) = Y- direction (0, -1)
            W (West)  = X- direction (-1, 0)
        """
        if edge not in _EDGE_OFFSETS:
            return None

        dx, dy = _EDGE_OFFSETS[edge]
        neighbor_key = (x + dx, y + dy)
        grid_neighbor = self.tracks.get(neighbor_key)
        if grid_neighbor is not None:
            return grid_neighbor

        tunnel_far = self.tunnels.get((int(x), int(y), edge))
        if tunnel_far is None:
            return None
        return self.tracks.get((tunnel_far[0], tunnel_far[1]))

    def get_connection_edges(
        self, tile_a: Tuple[int, int], tile_b: Tuple[int, int]
    ) -> Optional[Tuple[str, str]]:
        """Return the connecting edge pair for two tiles, either via grid
        adjacency or via a tunnel.

        Per design_new.md "Operational pathfinding home": answers two questions at once
        — are the two tiles track-connected, and if so, which edge on each
        tile connects to the other?

        Args:
            tile_a: (x, y) of the first tile.
            tile_b: (x, y) of the second tile.

        Returns:
            ``(edge_on_a, edge_on_b)`` when the tiles are track-connected,
            either because they are orthogonally adjacent on the grid with
            matching opposite edges in their ``track_bits``, or because a
            tunnel links them. Returns ``None`` otherwise.
        """
        ax, ay = tile_a
        bx, by = tile_b
        track_a = self.get_track(ax, ay)
        track_b = self.get_track(bx, by)
        if track_a is None or track_b is None:
            return None

        # 1. Grid-adjacent path.
        dx, dy = bx - ax, by - ay
        edge_a = _OFFSET_TO_EDGE.get((dx, dy))
        if edge_a is not None:
            edge_b = _OPPOSITE_EDGE[edge_a]
            if edge_a in track_a.edges and edge_b in track_b.edges:
                return (edge_a, edge_b)

        # 2. Tunnel-linked path.
        for edge in sorted(track_a.edges):
            far = self.tunnels.get((int(ax), int(ay), edge))
            if far is None:
                continue
            if (far[0], far[1]) == (bx, by) and far[2] in track_b.edges:
                return (edge, far[2])

        return None

    # ------------------------------------------------------------------
    # Tunnels: pair two dead-end edges so they act like neighbouring tiles.
    # ------------------------------------------------------------------

    _TUNNEL_OFFSETS: Dict[str, Tuple[int, int]] = {
        "N": (0, 1),
        "E": (1, 0),
        "S": (0, -1),
        "W": (-1, 0),
    }
    _TUNNEL_OPPOSITE: Dict[str, str] = {"N": "S", "S": "N", "E": "W", "W": "E"}


    def add_tunnel(
        self,
        a: Tuple[int, int, str],
        b: Tuple[int, int, str],
    ) -> None:
        """Pair two dead-end edges as a single tile-to-tile connection.

        Both endpoints must:
          - reference a tile that exists and includes the given edge in
            its ``track_bits``,
          - currently be a dead-end (no grid neighbour connecting back on
            the opposite edge),
          - not already participate in another tunnel.

        Endpoints must be distinct. After the write, both endpoint tiles
        get a topology propagation so derived state updates.
        """
        ep_a = self._normalize_tunnel_endpoint(a)
        ep_b = self._normalize_tunnel_endpoint(b)
        if ep_a == ep_b:
            raise ValueError(f"Tunnel endpoints must be distinct: got {ep_a}")
        self._validate_tunnel_endpoint(ep_a)
        self._validate_tunnel_endpoint(ep_b)

        self.tunnels[ep_a] = ep_b
        self.tunnels[ep_b] = ep_a

        self.propagate_from_tile(ep_a[0], ep_a[1])
        self.propagate_from_tile(ep_b[0], ep_b[1])

    def remove_tunnel(self, endpoint: Tuple[int, int, str]) -> bool:
        """Remove the tunnel containing ``endpoint``. Returns ``True`` if a
        tunnel was removed, ``False`` if the endpoint was not tunneled."""
        ep_a = self._normalize_tunnel_endpoint(endpoint)
        ep_b = self.tunnels.pop(ep_a, None)
        if ep_b is None:
            return False
        self.tunnels.pop(ep_b, None)
        self.propagate_from_tile(ep_a[0], ep_a[1])
        self.propagate_from_tile(ep_b[0], ep_b[1])
        return True

    def _normalize_tunnel_endpoint(self, ep) -> Tuple[int, int, str]:
        if not (isinstance(ep, (tuple, list)) and len(ep) == 3):
            raise ValueError(
                f"Tunnel endpoint must be (x, y, edge); got {ep!r}"
            )
        return (int(ep[0]), int(ep[1]), str(ep[2]).upper())

    def _validate_tunnel_endpoint(self, ep: Tuple[int, int, str]) -> None:
        x, y, edge = ep
        if edge not in self._TUNNEL_OFFSETS:
            raise ValueError(f"Tunnel edge must be N/E/S/W; got {edge!r}")
        if ep in self.tunnels:
            raise ValueError(
                f"Endpoint ({x},{y},{edge}) is already tunneled to "
                f"{self.tunnels[ep]}"
            )
        track = self.tracks.get((x, y))
        if track is None:
            raise ValueError(f"No track at ({x},{y}) for tunnel endpoint")
        if edge not in track.edges:
            raise ValueError(
                f"Edge {edge} not present at ({x},{y}); track has "
                f"{sorted(track.edges)}"
            )
        # Dead-end check uses grid arithmetic directly, not get_neighbor,
        # so it stays correct after get_neighbor consults tunnels in Phase C.
        dx, dy = self._TUNNEL_OFFSETS[edge]
        grid_neighbor = self.tracks.get((x + dx, y + dy))
        if (
            grid_neighbor is not None
            and self._TUNNEL_OPPOSITE[edge] in grid_neighbor.edges
        ):
            raise ValueError(
                f"Endpoint ({x},{y},{edge}) is not a dead-end — grid "
                f"neighbour ({x+dx},{y+dy}) connects back via "
                f"{self._TUNNEL_OPPOSITE[edge]}"
            )

    def _sweep_stale_tunnels(self) -> None:
        """Drop any tunnel whose endpoints no longer satisfy the
        dead-end invariants. Called from ``propagate_from_tile`` so that
        edits to track topology automatically clean the table."""
        if not self.tunnels:
            return
        stale: Set[Tuple[int, int, str]] = set()
        seen: Set[Tuple[int, int, str]] = set()
        for ep, other in list(self.tunnels.items()):
            if ep in seen:
                continue
            seen.add(ep)
            seen.add(other)
            for a in (ep, other):
                x, y, edge = a
                track = self.tracks.get((x, y))
                if track is None or edge not in track.edges:
                    stale.add(ep)
                    stale.add(other)
                    break
                dx, dy = self._TUNNEL_OFFSETS[edge]
                grid_neighbor = self.tracks.get((x + dx, y + dy))
                if (
                    grid_neighbor is not None
                    and self._TUNNEL_OPPOSITE[edge] in grid_neighbor.edges
                ):
                    stale.add(ep)
                    stale.add(other)
                    break
        for ep in stale:
            self.tunnels.pop(ep, None)

    def propagate_from_tile(self, x: int, y: int) -> None:
        """Propagate updates from a tile using BFS wave.

        This updates derived data (dead-ends, switch states) when:
        - A tile's track_bits changes
        - A tile is added or removed
        - A tile's switch_states changes

        The wave visits each affected tile once and calls propagate_update().
        """
        # Connectivity / track add / tunnel changes flow through here (and the
        # editor calls it after a track edit), so any cached walk is now stale.
        self.invalidate_pathfinding_cache()
        self._sweep_stale_tunnels()
        queue = deque([(x, y)])
        visited = set()

        while queue:
            tx, ty = queue.popleft()

            if (tx, ty) in visited:
                continue
            visited.add((tx, ty))

            tile = self.get_track(tx, ty)
            if not tile:
                continue

            # Update this tile's derived data
            tile.propagate_update(self)

            # Enqueue all neighbors for update
            for edge in ["N", "E", "S", "W"]:
                neighbor = self.get_neighbor(tx, ty, edge)
                if neighbor and (neighbor.x, neighbor.y) not in visited:
                    queue.append((neighbor.x, neighbor.y))

    def compute_bit_change_impact(
        self, x: int, y: int, new_bits: int, new_dead_ends: Optional[set] = None
    ) -> Dict[str, Any]:
        """Predict which entities on tile (x, y) would be orphaned if its
        track_bits were changed to `new_bits`.

        Rules:
            - The tile is fully removed when `new_bits == 0` and there are
              no dead-end ports.
            - A switch entity is orphaned when the tile is no longer a
              switch under `new_bits`.

        The caller decides whether to apply the change (cleanup) or surface
        a confirmation dialog.
        """
        track = self.get_track(x, y)
        if not track:
            return {
                "tile_will_be_removed": False,
                "removes_switch": None,
            }

        dead_ends = (
            new_dead_ends if new_dead_ends is not None else track.dead_end_ports
        )
        tile_will_be_removed = (new_bits == 0) and not dead_ends

        removes_switch: Optional[str] = None
        if track.switch_id and not _bits_form_switch(new_bits):
            removes_switch = track.switch_id

        return {
            "tile_will_be_removed": tile_will_be_removed,
            "removes_switch": removes_switch,
        }

    def remove_track(
        self, x: int, y: int, *, also_removing: Iterable[Tuple[int, int]] = ()
    ) -> bool:
        """Remove track segment and propagate updates to neighbors.

        Cleans up entities anchored to this tile so the layout stays
        consistent: the owning switch entity (if `track.switch_id` is set) is
        removed first.
        Segment membership is reconciled via `remove_tile_from_segment`.

        A tile that is a segment's last one takes the segment with it, so
        erasing it is bound by the rule for deleting that segment (design_new.md,
        "Segment deletion keeps its block connected"). That is judged first,
        before the switch is touched, so a rejection
        changes nothing. `also_removing` names the other tiles erased in the
        same edit; a caller erasing a whole block's track passes them all, so
        the block is judged as one edit and not one tile at a time.

        Raises:
            ValueError: when erasing this tile would empty a segment in the
                middle of its block. Nothing is changed.
        """
        key = (x, y)
        if key not in self.tracks:
            return False

        track = self.tracks[key]

        self.check_tile_removable(x, y, also_removing=also_removing)

        if track.switch_id:
            self.remove_switch(track.switch_id)

        if track.segment_id:
            # Block reconciliation can touch OTHER tiles in the block (a
            # split). If that work fails, we still want the
            # tile itself to be removed so the caller doesn't end up with
            # an orphan entry in self.tracks.
            try:
                self.remove_tile_from_segment(x, y, also_removing=also_removing)
            except Exception:
                logger.exception(
                    "Block reconciliation failed while removing track (%d, %d); "
                    "the tile will still be removed.",
                    x,
                    y,
                )

        del self.tracks[key]

        # Removed tile may have been a tunnel endpoint — clean those up
        # explicitly, since the propagate loop below only fires when a
        # grid neighbour exists.
        self._sweep_stale_tunnels()

        for edge, (dx, dy) in _EDGE_OFFSETS.items():
            neighbor_key = (x + dx, y + dy)
            if neighbor_key in self.tracks:
                self.propagate_from_tile(x + dx, y + dy)

        return True

    # === Centralized Block Management ===

    def create_segment(
        self, segment_id: str, name: str = "", block_id: Optional[str] = None
    ) -> Block:
        """Forward to `SegmentGeometryManager.create_segment(...)`."""
        return self._get_segment_geometry_manager().create_segment(
            segment_id, name, block_id=block_id
        )

    def create_segment_with_tracks(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager.create_segment_with_tracks(...)`."""
        return self._get_segment_geometry_manager().create_segment_with_tracks(
            *args, **kwargs
        )

    def extend_segment(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager.extend_segment(...)`."""
        return self._get_segment_geometry_manager().extend_segment(*args, **kwargs)

    def delete_segment(self, segment_id: str) -> bool:
        """Forward to `SegmentGeometryManager.delete_segment(segment_id)`.

        Returns False for an unknown segment. Raises ValueError, and changes
        nothing, when the segment is in the middle of a multi-segment block
        (design_new.md, "Segment deletion keeps its block connected").
        """
        return self._get_segment_geometry_manager().delete_segment(segment_id)

    def check_tiles_removable(self, positions: Iterable[Tuple[int, int]]) -> None:
        """Forward to `SegmentGeometryManager.check_tiles_removable(positions)`.

        Raises ValueError when taking these tiles out of their segments would
        empty a segment in the middle of a multi-segment block. Reads only.
        """
        return self._get_segment_geometry_manager().check_tiles_removable(positions)

    def check_tile_removable(
        self, x: int, y: int, *, also_removing: Iterable[Tuple[int, int]] = ()
    ) -> None:
        """Forward to `SegmentGeometryManager.check_tile_removable(x, y, ...)`.

        The one-tile form of `check_tiles_removable`, for a caller that removes
        a block's tiles one at a time after judging the whole edit up front.
        """
        return self._get_segment_geometry_manager().check_tile_removable(
            x, y, also_removing=also_removing
        )

    def create_block(self, block_id: str, segment_ids: List[str], name: str = "",
                     validate: bool = True) -> Block:
        """Forward to `BlockGroupingManager.create_block(...)`.

        The grouping-side create: it groups segments that already exist into
        one block sharing one power feed. Laying down new track geometry is
        `create_segment` / `create_segment_with_tracks`.
        """
        return self._get_block_grouping_manager().create_block(
            block_id, segment_ids, name, validate
        )

    def delete_block(self, block_id: str) -> bool:
        """Forward to `BlockGroupingManager.delete_block(block_id)`.

        The grouping-side delete: it dissolves the power grouping only. Every
        member segment survives and is re-homed into its own single-segment
        block, because a segment always belongs to exactly one block. Removing
        a segment's geometry is `delete_segment`.
        """
        return self._get_block_grouping_manager().delete_block(block_id)

    def get_block(self, block_id: str) -> Optional[Block]:
        return self.blocks.get(block_id)

    def _block_tiles_index(self) -> Dict[str, List[Tile]]:
        """Lazily-built reverse indices: block id -> Tiles, segment id -> Tiles.

        Built by a single pass over ``self.tracks`` (preserving
        ``tracks.values()`` order, so ``get_block_tracks`` returns tracks in
        the same order as the old full scan). A tile is filed under its own
        segment and under the block that groups that segment, so a block that
        groups several segments lists every member's tiles. Cached in
        ``_block_index_cache`` and invalidated by
        ``invalidate_pathfinding_cache`` on any topology change — none of which
        run inside the sim tick loop, so it is built once and reused across a
        whole ``update()`` storm.
        """
        idx = self._block_index_cache
        if idx is None:
            by_block: Dict[str, List[Tile]] = {}
            by_segment: Dict[str, List[Tile]] = {}
            for t in self.tracks.values():
                sid = t.segment_id
                if sid is None:
                    continue
                by_segment.setdefault(sid, []).append(t)
                bid = self.segment_to_block_id(sid)
                if bid is not None:
                    by_block.setdefault(bid, []).append(t)
            idx = by_block
            self._block_index_cache = idx
            self._segment_index_cache = by_segment
        return idx

    def _segment_tiles_index(self) -> Dict[str, List[Tile]]:
        """Lazily-built reverse index: segment id -> its Tiles.

        Built together with ``_block_tiles_index`` in one pass and dropped by
        the same invalidation.
        """
        self._block_tiles_index()
        return self._segment_index_cache or {}

    def get_block_tracks(self, block_id: str) -> List[Tile]:
        """Every tile owned by a block, across all its member segments
        (O(block) via the reverse index in ``_block_tiles_index``, not an
        O(all tracks) scan)."""
        return list(self._block_tiles_index().get(block_id, ()))

    def get_block_positions(self, block_id: str) -> List[Tuple[int, int]]:
        """Grid positions of every tile in a block, across all its segments."""
        return [(t.x, t.y) for t in self.get_block_tracks(block_id)]

    def get_segment_tracks(self, segment_id: str) -> List[Tile]:
        """Every tile of one segment. Geometry callers (tile ordering,
        continuity, length) want this rather than `get_block_tracks`."""
        return list(self._segment_tiles_index().get(segment_id, ()))

    def get_segment_positions(self, segment_id: str) -> List[Tuple[int, int]]:
        """Grid positions of every tile in one segment."""
        return [(t.x, t.y) for t in self.get_segment_tracks(segment_id)]

    def assign_track_to_segment(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager.assign_track_to_segment(...)`."""
        return self._get_segment_geometry_manager().assign_track_to_segment(
            *args, **kwargs
        )

    def validate_segment_continuity(self, segment_id: str) -> Tuple[bool, List[str]]:
        """Forward to `SegmentGeometryManager.validate_segment_continuity(...)`."""
        return self._get_segment_geometry_manager().validate_segment_continuity(
            segment_id
        )

    def _get_block_connected_neighbors(
        self, track: Tile
    ) -> List[Tuple[int, int]]:
        """
        Get positions of tracks connected via edges.

        A neighbor is connected if this track has an edge pointing to it
        and the neighbor has the opposite edge pointing back.
        """
        neighbors = []

        for edge in sorted(track.edges):
            if edge not in _EDGE_OFFSETS:
                continue
            dx, dy = _EDGE_OFFSETS[edge]
            neighbor_pos = (track.x + dx, track.y + dy)
            neighbor = self.get_track(*neighbor_pos)
            if neighbor:
                opp = _OPPOSITE_EDGE[edge]
                if opp in neighbor.edges:
                    neighbors.append(neighbor_pos)
        return neighbors

    def _get_neighbor_coords(self, x: int, y: int, edge: str) -> Tuple[int, int]:
        """Get coordinates of neighbor tile in given direction."""
        dx, dy = _EDGE_OFFSETS.get(edge, (0, 0))
        return (x + dx, y + dy)


    def remove_tile_from_segment(
        self, x: int, y: int, *, also_removing: Iterable[Tuple[int, int]] = ()
    ) -> List[str]:
        """Forward to `SegmentGeometryManager.remove_tile_from_segment(x, y)`.

        `also_removing` names the other tiles leaving their segments in the
        same edit, so the rule for emptying a segment is judged over all of
        them. Raises ValueError, changing nothing, when the edit would empty a
        segment in the middle of its block.
        """
        return self._get_segment_geometry_manager().remove_tile_from_segment(
            x, y, also_removing=also_removing
        )


    def _check_segment_connectivity(self, segment_id: str) -> List[str]:
        """Forward to `SegmentGeometryManager._check_segment_connectivity(...)`."""
        return self._get_segment_geometry_manager()._check_segment_connectivity(
            segment_id
        )

    def _find_connected_components(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager._find_connected_components(...)`."""
        return self._get_segment_geometry_manager()._find_connected_components(*args, **kwargs)

    def _get_track_connected_neighbors(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager._get_track_connected_neighbors(...)`."""
        return self._get_segment_geometry_manager()._get_track_connected_neighbors(*args, **kwargs)

    def _create_child_segments(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager._create_child_segments(...)`."""
        return self._get_segment_geometry_manager()._create_child_segments(
            *args, **kwargs
        )


    def add_switch(self, switch: "Switch") -> None:
        """
        Register a switch configuration at a track tile.

        Args:
            switch: Switch entity to add

        Raises:
            ValueError: If no track at position or track is not a switch
        """
        track = self.get_track(switch.x, switch.y)
        if not track:
            raise ValueError(f"No track at ({switch.x}, {switch.y})")
        if not track.is_switch:
            raise ValueError(f"Track at ({switch.x}, {switch.y}) is not a switch")

        # Store by ID
        self.switches[switch.switch_id] = switch
        # A new switch changes the exits the topology-only BFS can take.
        self.invalidate_pathfinding_cache()
        # Link track to switch
        track.switch_id = switch.switch_id

    def get_switch(self, switch_id: str) -> Optional["Switch"]:
        """Get switch configuration by ID."""
        return self.switches.get(switch_id)

    def get_switch_at(self, x: int, y: int) -> Optional["Switch"]:
        """Get switch configuration at a tile position."""
        for switch in self.switches.values():
            if switch.x == x and switch.y == y:
                return switch
        return None

    def remove_switch(self, switch_id: str) -> bool:
        """
        Remove switch configuration.

        Args:
            switch_id: Switch ID to remove

        Returns:
            True if removed, False if not found
        """
        switch = self.switches.get(switch_id)
        if not switch:
            return False

        # Clear reference on track tile
        track = self.get_track(switch.x, switch.y)
        if track:
            track.switch_id = None

        del self.switches[switch_id]
        # Removing a switch removes exits the topology-only BFS could take.
        self.invalidate_pathfinding_cache()
        return True

    def update_switch(self, switch_id: str, **kwargs) -> Optional["Switch"]:
        """
        Update switch configuration.

        Allowed updates:
            - main_length_cm: float
            - diverge_length_cm: float
            - main_radius_cm: float
            - diverge_radius_cm: float
            - name: str
            - notes: str
            - switch_type: str

        Args:
            switch_id: Switch to update
            **kwargs: Properties to update

        Returns:
            Updated Switch or None if not found
        """
        switch = self.switches.get(switch_id)
        if not switch:
            return None

        # Update allowed properties
        if "main_length_cm" in kwargs:
            switch.main_length_cm = float(kwargs["main_length_cm"])
        if "diverge_length_cm" in kwargs:
            switch.diverge_length_cm = float(kwargs["diverge_length_cm"])
        if "main_radius_cm" in kwargs:
            value = kwargs["main_radius_cm"]
            switch.main_radius_cm = float(value) if value is not None else None
        if "diverge_radius_cm" in kwargs:
            value = kwargs["diverge_radius_cm"]
            switch.diverge_radius_cm = float(value) if value is not None else None
        if "name" in kwargs:
            switch.name = kwargs["name"]
        if "notes" in kwargs:
            switch.notes = kwargs["notes"]
        if "switch_type" in kwargs:
            switch.switch_type = kwargs["switch_type"]

        # switch_type changes the exits the BFS can take; the *_length/_radius
        # changes feed walk distances. Both invalidate cached walks.
        self.invalidate_pathfinding_cache()
        return switch

    # ── Block grouping (design_new.md "Block grouping") ────────────────────

    @property
    def segments(self) -> Dict[str, Segment]:
        """Every `Segment` in the network, keyed by `segment_id`.

        Derived on read from `Block.segment_ids` — segments are stored inside
        their owning block, so this registry view can never drift from the
        blocks. `RailwayNetwork` owns the lookup; callers resolve segment
        membership through here rather than reaching into a block.
        """
        out: Dict[str, Segment] = {}
        for block in self.blocks.values():
            for seg in block.segments:
                out[seg.segment_id] = seg
        return out

    def get_segment(self, segment_id: str) -> Optional[Segment]:
        """Forward to `BlockGroupingManager.get_segment(segment_id)`."""
        return self._get_block_grouping_manager().get_segment(segment_id)

    def segment_to_block_id(self, segment_id: str) -> Optional[str]:
        """Forward to `BlockGroupingManager.segment_to_block_id(segment_id)`."""
        return self._get_block_grouping_manager().segment_to_block_id(segment_id)


    def add_segment_to_block(self, block_id: str, segment_id: str) -> bool:
        """Forward to `BlockGroupingManager.add_segment_to_block(...)`."""
        return self._get_block_grouping_manager().add_segment_to_block(
            block_id, segment_id
        )

    def remove_segment_from_block(self, block_id: str, segment_id: str) -> bool:
        """Forward to `BlockGroupingManager.remove_segment_from_block(...)`."""
        return self._get_block_grouping_manager().remove_segment_from_block(
            block_id, segment_id
        )

    def segments_form_switch_free_run(self, segment_ids: List[str]) -> bool:
        """Forward to `BlockGroupingManager.segments_form_switch_free_run(...)`."""
        return self._get_block_grouping_manager().segments_form_switch_free_run(
            segment_ids
        )

    def _reconcile_segment_tile_membership(self) -> None:
        """Re-establish tile -> segment ownership from a stored tile order.

        Membership lives on the tile (`Tile.segment_id`, indexed by
        `_tile_to_segment`); `Segment.ordered_tiles` is the derived read of it.
        An older save can carry the order but no tile ownership, so those
        segments would load owning nothing: the block exists and lists its
        tiles, yet `get_block_positions` is empty. Stamp the ownership back on
        from the order that survived.

        Only an unowned tile is claimed, so a tile already belonging to another
        segment is never stolen, and a save that already carries tile ownership
        is left untouched.

        A tile naming a segment that does not exist is repaired first. That is
        how a save written when a block and its member segment shared one id
        migrates: its tiles stored the shared id, which now names only the
        block, whose one member segment is the geometry that tile actually
        belongs to — so the reference is re-pointed there rather than dropped.
        Dropping it would lose the membership outright for a save that recorded
        it on the tile and nowhere else (an older save carries no
        `ordered_tiles` to re-claim from). A reference that resolves to neither
        a segment nor a single-segment block is released, leaving the tile for
        the claim below.
        """
        live_segment_ids = {
            segment.segment_id
            for block in self.blocks.values()
            for segment in block.segments
        }
        for track in self.tracks.values():
            stored = track.segment_id
            if stored is None or stored in live_segment_ids:
                continue
            block = self.blocks.get(stored)
            # Runs before `_fold_grouped_blocks`, so every legacy member is
            # still its own one-segment block and the mapping is unambiguous.
            if block is not None and len(block.segments) == 1:
                track.segment_id = block.segments[0].segment_id
            else:
                track.segment_id = None

        for block in self.blocks.values():
            for segment in block.segments:
                for entry in segment.ordered_tiles:
                    track = self.get_track(entry.x, entry.y)
                    if track is not None and track.segment_id is None:
                        track.segment_id = segment.segment_id

    def _fold_grouped_blocks(self, groups: List[dict]) -> None:
        """Fold each stored grouping record into the one block it describes.

        A save written before blocks grouped segments kept the power grouping in
        its own list, naming the blocks that shared one feed. Each of those
        members has already loaded as its own one-segment block, so folding
        means: take those members' segments, in a connected run order, and make
        them the member segments of a single block named after the grouping.
        The emptied member blocks are dropped by the grouping manager.

        The stored member list carries no traversal order, so the order is
        recovered from tile adjacency. A grouping whose members do not chain
        into one clean run is still folded, with a warning: dropping it would
        turn one single-train feed back into several independent feeds, which
        is the less safe reading of the same layout.
        """
        if not groups:
            return
        grouping = self._get_block_grouping_manager()
        for record in groups:
            group_id = str(record.get("district_id") or "")
            member_ids = [str(b) for b in record.get("block_ids", [])]
            # A record written when a block and its segment shared one id names
            # its members by that shared string. It still names the block on a
            # save whose blocks carried their own ids, and names the segment on
            # one where the id has since moved to the geometry axis — so try
            # the id as stored first, then as a segment whose block is derived
            # (`entity_ids`).
            members = []
            for member_id in member_ids:
                if member_id in self.blocks:
                    members.append(member_id)
                    continue
                derived = block_id_for_segment(member_id)
                if derived in self.blocks:
                    members.append(derived)
            if not group_id or not members:
                continue

            # An editor-authored block is saved with no tile order — its
            # membership rides on the tracks instead — so its segment has no
            # tiles to chain until the order is rebuilt from that membership.
            # Each member is still its own one-segment block here, so this is
            # the ordinary single-segment rebuild.
            geometry = self._get_segment_geometry_manager()
            for member_id in members:
                block = self.blocks[member_id]
                if not block.ordered_tiles:
                    for segment_id in block.segment_ids:
                        geometry.rebuild_segment_ordered_tiles(segment_id)

            segment_ids: List[str] = []
            for member_id in members:
                segment_ids.extend(self.blocks[member_id].segment_ids)
            try:
                grouping.group_into_block(
                    block_id=group_id,
                    segment_ids=segment_ids,
                    name=str(record.get("name", "")),
                )
            except ValueError as exc:
                # The stored grouping does not describe one connected
                # switch-free run. Fold it anyway: dropping it would turn one
                # single-train feed back into several independent feeds, which
                # is the less safe reading of the same layout.
                logger.warning(
                    "Grouping %s is not one connected switch-free run (%s); "
                    "folding it into a single block anyway to preserve its "
                    "one-train power domain.",
                    group_id,
                    exc,
                )
                ordered, _ = grouping.plan_run(segment_ids)
                try:
                    grouping.create_block(
                        block_id=group_id,
                        segment_ids=ordered,
                        name=str(record.get("name", "")),
                        validate=False,
                    )
                except ValueError as exc2:
                    logger.warning(
                        "Could not fold grouping %s into a block: %s. Its "
                        "member blocks stay independent feeds.",
                        group_id,
                        exc2,
                    )

    def _recorded_block_voltage(self, block_id: str) -> float:
        """The applied-output voltage recorded for `block_id`, or `0`.

        Read from the `BlockPowerController` (voltage's applied-output mirror is
        controller-owned, not a `Block` field); `0` when no controller is wired,
        as in bootstrap or isolated tests.
        """
        controller = getattr(self, "power_controller", None)
        if controller is None:
            return 0.0
        return float(controller.get_recorded_voltage(block_id))

    def to_dict(self) -> dict:
        """Convert network to dictionary for the live frontend payload.

        Persistence does NOT use this path; see `to_layout_dict()` instead.
        """
        return {
            "tracks": [t.to_dict() for t in self.tracks.values()],
            # `current_voltage` is the applied-output mirror owned by
            # `BlockPowerController`, not a `Block` field; inject it onto each
            # block's live payload here (design_new.md "Power", Block
            # power-controller role). It defaults to `0` when no controller is
            # wired (bootstrap / isolated tests).
            "blocks": {
                k: {**v.to_dict(), "current_voltage": self._recorded_block_voltage(k)}
                for k, v in self.blocks.items()
            },
            "switches": {
                k: v.to_dict() for k, v in self.switches.items()
            },
            "tunnels": self.tunnels_to_list(),
        }

    def tunnels_to_list(self) -> list:
        """Serialize ``self.tunnels`` to a list of pair records. The
        symmetric backing dict is collapsed to one record per pair."""
        out = []
        seen: Set[Tuple[int, int, str]] = set()
        for ep_a, ep_b in self.tunnels.items():
            if ep_a in seen:
                continue
            seen.add(ep_a)
            seen.add(ep_b)
            out.append({
                "a": {"x": ep_a[0], "y": ep_a[1], "edge": ep_a[2]},
                "b": {"x": ep_b[0], "y": ep_b[1], "edge": ep_b[2]},
            })
        return out

    def to_layout_dict(self) -> dict:
        """Layout-only serialization used by persistence.

        It writes the layout only: tiles, blocks, switches and tunnels.
        Trains are not saved (design_v2.md "Trains", Trains are not saved),
        and no runtime field is written.
        """
        return {
            "tracks": [t.to_layout_dict() for t in self.tracks.values()],
            "blocks": {k: v.to_layout_dict() for k, v in self.blocks.items()},
            "switches": {
                k: v.to_layout_dict() for k, v in self.switches.items()
            },
            "tunnels": self.tunnels_to_list(),
        }

    def from_dict(self, data: dict):
        """Reconstruct network from dictionary. A save may carry sections this
        version does not read (signals, platforms, station groups, trains);
        they are skipped (design_v2.md "Code map", Layout files)."""
        # Clear existing — entity dicts plus the tile->owner reverse indices
        # and caches, so a load can never inherit stale block
        # membership on tiles the previous layout occupied.
        self.clear()

        for block_id, block_data in data.get("blocks", {}).items():
            self.blocks[block_id] = Block.from_dict(block_data)

        self._ensure_managers()

        for t_data in data.get("tracks", []):
            track = Tile.from_dict(t_data)
            self.add_track(track)

        # Tile ownership is knowable only now that both the blocks and the
        # tracks are in place, so heal a save that stored only one of the two.
        self._reconcile_segment_tile_membership()

        # Rebuild switches
        from switch import Switch

        for switch_id, switch_data in data.get("switches", {}).items():
            switch = Switch.from_dict(switch_data)
            self.switches[switch_id] = switch

        # Fold any grouping from an older save. Those snapshots stored the
        # power grouping separately from the blocks it grouped, so each grouped
        # member loaded above as its own one-segment block; folding replaces
        # that set with the one block the grouping always described, owning each
        # member's geometry as a member segment. Runs after every block exists.
        self._fold_grouped_blocks(data.get("power_districts", []))

        # Rebuild tunnels (after all tracks are in place — add_tunnel
        # validates against the live track set). A malformed entry is
        # logged and skipped so one bad pair doesn't brick the load.
        for tunnel_data in data.get("tunnels", []):
            try:
                a = tunnel_data["a"]
                b = tunnel_data["b"]
                self.add_tunnel(
                    (a["x"], a["y"], a["edge"]),
                    (b["x"], b["y"], b["edge"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping malformed tunnel entry %r: %s",
                    tunnel_data,
                    exc,
                )

