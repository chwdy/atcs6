"""
Railway Network Management

Definition:
This module provides the RailwayNetwork class which manages the complete railway infrastructure
including tracks, trains, and block sections. It serves as the central repository and
coordinator for all railway system components.
"""

from collections import deque
import logging
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    TYPE_CHECKING,
)
import uuid
from track import Tile, EDGE_MASKS, BIT_TO_EDGES
from train import Train
from block import Block
from block_manager import BlockManager
from switch_manager import SwitchManager
from resource_coordinator import ResourceCoordinator
from enums import TrackType, Direction
from constants import FLANK_PROTECTION_DISTANCE_CM
from railway_signal import Signal, SignalType, SignalState
from station import Station
from segment import Segment
from train_roster import TrainRoster
from railway_platform import Platform
from speed_profile import SpeedProfileRegistry

if TYPE_CHECKING:
    from path_utils import GraphLocator, DirectedBlockFrame


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


class RailwayNetwork:
    """
    Central railway network management system.

    Manages all railway infrastructure components including tracks, trains,
    and block sections.
    """

    def __init__(self):
        self.tracks: Dict[Tuple[int, int], Tile] = {}
        self.trains: Dict[str, Train] = {}
        self.blocks: Dict[str, Block] = {}
        self.signals: Dict[str, Signal] = {}
        self.platforms: Dict[str, Platform] = {}
        self.stations = self.platforms
        self.station_groups: Dict[str, Station] = {}
        self.switches: Dict[str, "Switch"] = {}  # NEW: Switch entity management
        # Segments are geometry; they live inside their owning `Block`
        # (design_new.md "Block": a block is a switch-free connected run of one
        # or more segments sharing one power feed). `self.segments` and
        # `segment_to_block_id(...)` below are the network-level registry reads
        # derived from `Block.segment_ids`, so they can never drift from the
        # blocks themselves.
        # Train roster: the operator's catalog of owned locomotives (design_new.md
        # "Train Roster"). Configuration data persisted with the layout snapshot;
        # a placement may import an entry to seed a fresh Train's defaults.
        self.train_roster: TrainRoster = TrainRoster()

        # Lookahead memo coordination (perf; see TrainController.
        # _compute_canonical_lookahead). `_lookahead_world_version` is bumped by
        # the SimulationController at every tick phase boundary; the per-train
        # lookahead memo keys on it, so a new tick — or any intra-tick mutation
        # phase — invalidates every cached payload. `_lookahead_memo_active` is
        # True ONLY while a tick is running: the memo is read/written only then,
        # so every between-tick / paused / UI caller recomputes the lookahead
        # live and can never observe a stale payload.
        self._lookahead_world_version: int = 0
        self._lookahead_memo_active: bool = False

        # The one speed-profile store for the whole runtime. TrainPhysics /
        # TrainController._get_speed_profile read it to drive trains, and the
        # REST API + services read and write the same object through
        # AppState.speed_profile_registry (a property that returns this
        # registry). SpeedProfileRegistry seeds it with the undeletable
        # "default" profile.
        self.speed_profiles: SpeedProfileRegistry = SpeedProfileRegistry()

        # Symmetric map of tunnel endpoints. A tunnel pairs two dead-end
        # edges so they behave as a single tile-to-tile connection. Keyed
        # by ``(x, y, edge)``; both directions of every pair are stored so
        # lookups are O(1). Written only via ``add_tunnel`` / ``remove_tunnel``
        # / ``_sweep_stale_tunnels``.
        self.tunnels: Dict[
            Tuple[int, int, str], Tuple[int, int, str]
        ] = {}

        self.block_manager: Optional[BlockManager] = None
        self.switch_manager: Optional[SwitchManager] = None
        self.resource_coordinator: Optional[ResourceCoordinator] = None
        self.power_controller: Optional["BlockPowerController"] = None
        self.switch_output_interface: Optional["SwitchOutput"] = None

        # Reverse indices from tile -> owning segment / platform.
        # A tile belongs to a `Segment`; the segment is grouped into a `Block`.
        # Maintained in sync by network mutators (segment creation, deletion,
        # platform creation, platform deletion, etc.). Readers should call
        # tile_to_segment_id (geometry) / tile_to_block_id (power, occupancy,
        # reservation) / tile_to_platform_id / tile_to_station_id below.
        self._tile_to_segment: Dict[Tuple[int, int], str] = {}
        self._tile_to_platform: Dict[Tuple[int, int], str] = {}

        # Reverse index block id -> its Tiles, derived from
        # _tile_to_segment and built lazily by _block_tiles_index(). Turns
        # get_block_tracks() from an O(all tracks) scan into an O(block)
        # lookup; the lookahead calls it thousands of times per tick. None
        # means "rebuild on next read"; invalidate_pathfinding_cache() resets
        # it on every topology change (none of which run in the tick loop).
        self._block_index_cache: Optional[Dict[str, List["Tile"]]] = None
        # Companion segment id -> Tiles index, built in the same pass as
        # _block_index_cache and dropped by the same invalidation.
        self._segment_index_cache: Optional[Dict[str, List["Tile"]]] = None

        # Cache of built topology-only walk CANDIDATES, keyed by
        # (start_block_id, end_block_id). Populated lazily by
        # RailwayPathfinder.query_graph_walks and shared by every pathfinder
        # built from this network (the dispatcher's and any throwaway one).
        # The candidate geometry (raw walk + ordered_walk steps) is a pure
        # function of the static graph -- the BFS ignores switch position and
        # the per-step enrichment reads only static topology / Block.ordered_tiles
        # -- so it stays valid across sim ticks and is dropped only when the
        # topology itself changes. The per-call disabled/manual/direction FILTER
        # runs fresh in query_graph_walks, not here. See
        # invalidate_pathfinding_cache() and its callers.
        self._walk_cache: Dict[Tuple[str, str], List["GraphWalkCandidate"]] = {}

        # Helper modules (signal computation, station registry, block
        # geometry mutation). Lazy-initialised on first access to keep
        # `__init__` cheap.
        self._signal_calculator = None
        self._segment_geometry_manager = None
        self._station_registry = None
        self._block_grouping_manager = None
        self._flank_protection_calculator = None
        self._signal_registry = None
        self._train_placement = None

    def _ensure_block_manager(self):
        if self.block_manager is None:
            self.block_manager = BlockManager(self.blocks, network=self)

    def _get_signal_calculator(self):
        """Lazy-init `SignalCalculator`. The actual signal-state /
        reachability logic lives in `signal_calculator.py`; this network
        keeps thin forwarders so existing call sites stay unchanged."""
        if self._signal_calculator is None:
            from signal_calculator import SignalCalculator

            self._signal_calculator = SignalCalculator(self)
        return self._signal_calculator

    def _get_segment_geometry_manager(self):
        """Lazy-init `SegmentGeometryManager`. Segment geometry / topology
        mutation lives in `segment_geometry_manager.py`; this network
        keeps doc-canonical method names as forwarders."""
        if self._segment_geometry_manager is None:
            from segment_geometry_manager import SegmentGeometryManager

            self._segment_geometry_manager = SegmentGeometryManager(self)
        return self._segment_geometry_manager

    def _get_station_registry(self):
        """Lazy-init `StationRegistry`. Platform / station CRUD lives in
        `station_registry.py`; this network keeps doc-canonical method
        names as forwarders. The `platforms` / `station_groups` /
        `_tile_to_platform` registries are still owned by `RailwayNetwork`
        directly — `StationRegistry` reads / writes them through the
        back-reference."""
        if self._station_registry is None:
            from station_registry import StationRegistry

            self._station_registry = StationRegistry(self)
        return self._station_registry

    def _get_block_grouping_manager(self):
        """Lazy-init `BlockGroupingManager`. Block membership (`segment_ids`)
        CRUD lives there; the `blocks` dict that holds the grouping stays on the
        network; the manager reads / writes them through its back-reference.
        """
        if self._block_grouping_manager is None:
            from block_grouping_manager import BlockGroupingManager

            self._block_grouping_manager = BlockGroupingManager(self)
        return self._block_grouping_manager

    def _get_flank_protection_calculator(self):
        """Lazy-init `FlankProtectionCalculator`. The flank-protection graph
        walk lives there; it reads the network graph through its back-reference
        and writes each `Switch.flank_protection`.
        """
        if self._flank_protection_calculator is None:
            from flank_protection import FlankProtectionCalculator

            self._flank_protection_calculator = FlankProtectionCalculator(self)
        return self._flank_protection_calculator

    def _get_signal_registry(self):
        """Lazy-init `SignalRegistry`. Signal CRUD lives there; the `signals`
        dict stays on the network and the registry reads / writes it through its
        back-reference (signal STATE stays on the SignalCalculator forwarders).
        """
        if self._signal_registry is None:
            from signal_registry import SignalRegistry

            self._signal_registry = SignalRegistry(self)
        return self._signal_registry

    def _get_train_placement(self):
        """Lazy-init `TrainPlacement`. Train placement / occupancy geometry
        lives there; it reads the network graph through its back-reference
        (_get_tile_length_cm and the entity dicts stay on the network).
        """
        if self._train_placement is None:
            from train_placement import TrainPlacement

            self._train_placement = TrainPlacement(self)
        return self._train_placement

    def _ensure_resource_managers(self):
        """Construct BlockManager, SwitchManager, and ResourceCoordinator if not
        yet wired. The coordinator is the dispatcher-facing facade per
        design_new.md "ResourceCoordinator"; constructed last because it
        composes the other two managers.
        """
        self._ensure_block_manager()
        if self.switch_manager is None:
            self.switch_manager = SwitchManager(self.switches, network=self)
        if self.resource_coordinator is None:
            self.resource_coordinator = ResourceCoordinator(
                self.block_manager, self.switch_manager
            )

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
        occupancy, reservation, signalling, pathfinding, dispatch and physics
        are block concerns.
        """
        segment_id = self._tile_to_segment.get((x, y))
        if segment_id is None:
            return None
        return self.segment_to_block_id(segment_id)

    def tile_to_platform_id(self, x: int, y: int) -> Optional[str]:
        """Forward to `StationRegistry.tile_to_platform_id(x, y)`."""
        return self._get_station_registry().tile_to_platform_id(x, y)

    def tile_to_station_id(self, x: int, y: int) -> Optional[str]:
        """Forward to `StationRegistry.tile_to_station_id(x, y)`."""
        return self._get_station_registry().tile_to_station_id(x, y)

    def _set_tile_segment(self, x: int, y: int, segment_id: Optional[str]) -> None:
        """Update the tile -> segment reverse index.

        Network-internal helper. Use this whenever a tile's segment membership
        changes (segment creation, extension, deletion, station split, etc.).
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

    def _set_tile_platform(self, x: int, y: int, platform_id: Optional[str]) -> None:
        """Forward to `StationRegistry._set_tile_platform(x, y, platform_id)`."""
        return self._get_station_registry()._set_tile_platform(x, y, platform_id)

    # === Pathfinding walk cache (owned here; the static graph lives here) ===

    def get_cached_walks(
        self, start_block_id: str, end_block_id: str
    ) -> Optional[List["GraphWalkCandidate"]]:
        """Return the cached built walk candidates for this block pair, or None
        on a miss. The pathfinder owns what goes in (raw walk + ordered_walk
        geometry); this network only stores it so every pathfinder built from
        this graph shares one enrichment per (start, end) until the topology
        changes."""
        return self._walk_cache.get((start_block_id, end_block_id))

    def store_cached_walks(
        self, start_block_id: str, end_block_id: str,
        candidates: List["GraphWalkCandidate"],
    ) -> None:
        """Record the built walk candidates for this block pair."""
        self._walk_cache[(start_block_id, end_block_id)] = candidates

    def invalidate_pathfinding_cache(self) -> None:
        """Drop every cached walk. Called by each topology mutator (block
        membership, track add/remove, switch add/remove, tunnel changes, full
        reload). None of those run inside the sim tick loop, so the cache
        survives a whole `update()` storm and is rebuilt lazily after an edit."""
        # Defensive getattr: the segment_id setter routes through
        # _set_tile_segment -> here, which can fire on a Tile whose
        # network back-reference was attached before __init__ finished wiring
        # the cache (e.g. unusual deserialization orders).
        cache = getattr(self, "_walk_cache", None)
        if cache:
            cache.clear()
        # The block->tracks / segment->tracks reverse indices are derived from
        # tile->segment membership and the segment->block grouping, so they are
        # stale after any topology change. Drop them; the next
        # get_block_tracks() rebuilds both from the new topology.
        self._block_index_cache = None
        self._segment_index_cache = None

    def clear(self) -> None:
        """Reset the network to empty.

        Clears every entity dict AND the derived tile->block / tile->platform
        reverse indices and the walk/block caches. The reverse indices are not
        stored on the entity dicts, so emptying ``tracks`` / ``blocks`` alone
        leaves them stale: a new track drawn on a tile a previous layout used
        would read its old block via ``tile_to_block_id()`` and block creation
        would reject it. This is the single reset used by the "New Layout"
        action and by ``from_dict``'s "clear existing" step.
        """
        self.tracks.clear()
        self.trains.clear()
        self.blocks.clear()
        self.signals.clear()
        self.platforms.clear()  # self.stations is the same object
        self.station_groups.clear()
        self.switches.clear()
        self.tunnels.clear()
        self.train_roster.clear()
        # Derived tile -> owner reverse indices. Not held on any entity dict,
        # so they must be cleared explicitly to avoid stale block/platform
        # membership on reused tiles.
        self._tile_to_segment.clear()
        self._tile_to_platform.clear()
        # Drops _walk_cache + _block_index_cache.
        self.invalidate_pathfinding_cache()

    def add_track(self, track: Tile):
        """Add a track segment to the network and propagate updates"""
        key = (track.x, track.y)
        self.tracks[key] = track

        # Move any pre-attachment local-fallback values for station_id /
        # segment_id into the network reverse indices so that reads
        # via the property continue to see the same value after attach.
        local_segment = track._segment_id_local
        local_station = track._station_id_local
        track.network = self
        if local_segment is not None:
            track.segment_id = local_segment
        if local_station is not None:
            track.station_id = local_station

        # Propagate to this tile and neighbors
        self.propagate_from_tile(track.x, track.y)

    def bump_lookahead_world_version(self) -> None:
        """Invalidate every per-train lookahead memo (see ``__init__``).

        Called by the SimulationController at each tick phase boundary so a
        cached lookahead is never reused across a state change.
        """
        self._lookahead_world_version += 1

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

        Per design_new.md "Pathfinding home": answers two questions at once
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

    def project_authority_boundary_delta(
        self,
        head_locator: "GraphLocator",
        authority_end: "DirectedBlockFrame",
        safety_margin_cm: float,
    ) -> float:
        """Project the head's forward distance to the safety-margin-adjusted
        authority boundary (design_new.md:2161).

        ``head_locator`` carries the already-measured forward clearance from the
        head to ``authority_end`` in ``head_locator.distance_cm`` (the
        head→authority-end walk produced by the canonical lookahead, which steps
        ``authority_end``'s directed frame tile-by-tile). ``safety_margin_cm`` is
        the controller's effective safety margin. The boundary delta is the
        clearance minus that margin, clamped at zero so a head already inside the
        margin reports ``0`` rather than a negative overshoot. This is the same
        axis the ``speed_curves`` samples use while authority is live.
        """
        clearance_cm = max(0.0, float(head_locator.distance_cm or 0.0))
        return max(0.0, clearance_cm - float(safety_margin_cm))

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

    def get_tunnel(
        self, x: int, y: int, edge: str
    ) -> Optional[Tuple[int, int, str]]:
        """Far endpoint of the tunnel at ``(x, y, edge)``, or ``None`` if not
        tunneled. Reader only — does not consult grid neighbours."""
        return self.tunnels.get((int(x), int(y), str(edge).upper()))

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
              no dead-end ports. In that case any platform owning the tile
              is invalidated.
            - A signal is orphaned when no segment in `new_bits` touches
              its `detection_direction` port.
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
                "removes_signals": [],
                "removes_platform": None,
            }

        dead_ends = (
            new_dead_ends if new_dead_ends is not None else track.dead_end_ports
        )
        tile_will_be_removed = (new_bits == 0) and not dead_ends

        removes_switch: Optional[str] = None
        if track.switch_id and not _bits_form_switch(new_bits):
            removes_switch = track.switch_id

        removes_signals: List[str] = []
        for sid in list(track.signal_ids):
            signal = self.signals.get(sid)
            if signal is None:
                continue
            edge = signal.detection_direction
            mask = EDGE_MASKS.get(edge, 0)
            if not (new_bits & mask):
                removes_signals.append(sid)

        removes_platform: Optional[str] = None
        if tile_will_be_removed:
            plat_id = self._tile_to_platform.get((x, y))
            if plat_id:
                removes_platform = plat_id

        return {
            "tile_will_be_removed": tile_will_be_removed,
            "removes_switch": removes_switch,
            "removes_signals": removes_signals,
            "removes_platform": removes_platform,
        }

    def remove_track(self, x: int, y: int) -> bool:
        """Remove track segment and propagate updates to neighbors.

        Cleans up entities anchored to this tile so the layout stays
        consistent: any signals listed in `track.signal_ids`, the owning
        switch entity (if `track.switch_id` is set), and the owning
        platform (if `_tile_to_platform` has an entry) are removed first.
        Segment membership is reconciled via `remove_tile_from_segment`.
        """
        key = (x, y)
        if key not in self.tracks:
            return False

        track = self.tracks[key]

        for sid in list(track.signal_ids):
            self.remove_signal(sid)

        if track.switch_id:
            self.remove_switch(track.switch_id)

        plat_id = self._tile_to_platform.get(key)
        if plat_id:
            self.delete_station(plat_id)

        if track.segment_id:
            # Block reconciliation can touch signals on OTHER tiles in the
            # block (split + remap). If that work fails, we still want the
            # tile itself to be removed so the caller doesn't end up with
            # an orphan entry in self.tracks.
            try:
                self.remove_tile_from_segment(x, y)
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

    def create_segment(self, segment_id: str, name: str = "") -> Block:
        """Forward to `SegmentGeometryManager.create_segment(segment_id, name)`."""
        return self._get_segment_geometry_manager().create_segment(segment_id, name)

    def create_segment_with_tracks(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager.create_segment_with_tracks(...)`."""
        return self._get_segment_geometry_manager().create_segment_with_tracks(
            *args, **kwargs
        )

    def extend_segment(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager.extend_segment(...)`."""
        return self._get_segment_geometry_manager().extend_segment(*args, **kwargs)

    def delete_segment(self, segment_id: str) -> bool:
        """Forward to `SegmentGeometryManager.delete_segment(segment_id)`."""
        return self._get_segment_geometry_manager().delete_segment(segment_id)

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

    # === Centralized Signal Management ===

    def add_signal(
        self,
        x: int,
        y: int,
        detection_direction: str,
        signal_type: Union[SignalType, str] = SignalType.BLOCK,
        name: Optional[str] = None,
        auto_connect: bool = True,
        signal_id: Optional[str] = None,
    ) -> Signal:
        """Forward to `SignalRegistry.add_signal(...)`."""
        return self._get_signal_registry().add_signal(
            x, y, detection_direction, signal_type, name, auto_connect, signal_id
        )

    def get_signal(self, signal_id: str) -> Optional[Signal]:
        """Forward to `SignalRegistry.get_signal(signal_id)`."""
        return self._get_signal_registry().get_signal(signal_id)

    def get_signal_at(self, x: int, y: int) -> Optional[Signal]:
        """Forward to `SignalRegistry.get_signal_at(x, y)`."""
        return self._get_signal_registry().get_signal_at(x, y)

    def get_signal_at_direction(
        self, x: int, y: int, detection_direction: str
    ) -> Optional[Signal]:
        """Forward to `SignalRegistry.get_signal_at_direction(...)`."""
        return self._get_signal_registry().get_signal_at_direction(
            x, y, detection_direction
        )

    def remove_signal(self, signal_id: str) -> bool:
        """Forward to `SignalRegistry.remove_signal(signal_id)`."""
        return self._get_signal_registry().remove_signal(signal_id)

    def remove_signal_at(self, x: int, y: int) -> bool:
        """Forward to `SignalRegistry.remove_signal_at(x, y)`."""
        return self._get_signal_registry().remove_signal_at(x, y)

    def update_signal(self, signal_id: str, **kwargs) -> Optional[Signal]:
        """Forward to `SignalRegistry.update_signal(signal_id, **kwargs)`."""
        return self._get_signal_registry().update_signal(signal_id, **kwargs)

    def connect_signal_to_block(self, signal_id: str, block_id: str) -> bool:
        """Forward to `SignalRegistry.connect_signal_to_block(signal_id, block_id)`."""
        return self._get_signal_registry().connect_signal_to_block(signal_id, block_id)

    def _can_reach_block(self, signal: Signal, target_block_id: str) -> bool:
        """Forward to `SignalCalculator.can_reach_block(signal, target)`."""
        return self._get_signal_calculator().can_reach_block(signal, target_block_id)

    def get_reachable_blocks(self, signal_id: str) -> list:
        """Forward to `SignalCalculator.reachable_blocks(signal_id)`."""
        return self._get_signal_calculator().reachable_blocks(signal_id)

    def is_signal_on_entry_tile(self, signal: Signal) -> bool:
        """Forward to `SignalCalculator.is_signal_on_entry_tile(signal)`."""
        return self._get_signal_calculator().is_signal_on_entry_tile(signal)

    def update_signals_from_blocks(self) -> None:
        """Forward to `SignalCalculator.update_all()`."""
        self._get_signal_calculator().update_all()

    def _calculate_signal_state(self, signal: Signal) -> SignalState:
        """Forward to `SignalCalculator.compute_state(signal)`."""
        return self._get_signal_calculator().compute_state(signal)

    def add_train(self, train: Train):
        """Add a train to the network"""
        self.trains[train.train_id] = train

    def place_train(
        self,
        train: Train,
        tile_x: int,
        tile_y: int,
        direction: Optional[Direction] = None,
        *,
        head_offset_cm: float = 0.0,
        confine_to_blocks: Optional[Iterable[str]] = None,
    ) -> "TrainSpan":
        """Forward to `TrainPlacement.place_train(...)`."""
        return self._get_train_placement().place_train(
            train,
            tile_x,
            tile_y,
            direction,
            head_offset_cm=head_offset_cm,
            confine_to_blocks=confine_to_blocks,
        )

    def _get_tile_length_cm(self, x: int, y: int) -> float:
        """
        Get physical length of a tile in cm.

        Returns DEFAULT_TILE_LENGTH_CM if track is unconfigured.
        """
        track = self.get_track(x, y)
        if not track:
            return DEFAULT_TILE_LENGTH_CM

        if track.is_switch:
            switch_config = (
                self.get_switch_at(x, y) if hasattr(self, "get_switch_at") else None
            )
            if switch_config:
                return switch_config.get_active_length(track.switch_state)
            # Switches without entity should have 0 length by design
            # Do NOT fall back to DEFAULT_TILE_LENGTH_CM
            return 0.0

        # Per-tile length is the segment's length spread over its tiles:
        # length is segment geometry, so a grouped block must not average its
        # members' combined length across all of their tiles.
        if track.segment_id:
            segment = self.get_segment(track.segment_id)
            if segment and segment.length_cm > 0:
                positions = self.get_segment_positions(track.segment_id)
                tile_count = len(positions) if positions else 1
                return segment.length_cm / tile_count
            return DEFAULT_TILE_LENGTH_CM

        return DEFAULT_TILE_LENGTH_CM

    def _get_neighbor_coords(self, x: int, y: int, edge: str) -> Tuple[int, int]:
        """Get coordinates of neighbor tile in given direction."""
        dx, dy = _EDGE_OFFSETS.get(edge, (0, 0))
        return (x + dx, y + dy)

    def get_occupied_tiles_rich(
        self, span: "TrainSpan", head_is_a: bool
    ) -> List[Dict[str, Any]]:
        """Forward to `TrainPlacement.get_occupied_tiles_rich(span, head_is_a)`."""
        return self._get_train_placement().get_occupied_tiles_rich(span, head_is_a)

    def remove_train(self, train_id: str) -> bool:
        train = self.trains.get(train_id)
        if not train:
            return False

        # Release every block this train physically occupies. Physical
        # occupancy lives on `Block.occupied_by` (design_new.md); there is no
        # `train.position` in v1, so the block-ownership state itself is the
        # source of truth. Clear each owned block through the canonical writer.
        for block_id, block in list(self.blocks.items()):
            if getattr(block, "occupied_by", None) == train_id:
                if self.block_manager:
                    self.block_manager.clear_block(block_id)
                else:
                    block.release()

        self.trains.pop(train_id, None)
        self.update_signals_from_blocks()
        return True

    def get_train(self, train_id: str) -> Optional[Train]:
        return self.trains.get(train_id)

    def visualization(self):
        """Simple text-based visualization of the network"""
        if not self.tracks:
            print("Empty network")
            return

        min_x = min(pos[0] for pos in self.tracks.keys())
        max_x = max(pos[0] for pos in self.tracks.keys())
        min_y = min(pos[1] for pos in self.tracks.keys())
        max_y = max(pos[1] for pos in self.tracks.keys())

        print(f"\nNetwork Layout ({max_x - min_x + 1} x {max_y - min_y + 1}):")
        for y in range(max_y, min_y - 1, -1):
            row = ""
            for x in range(min_x, max_x + 1):
                track = self.get_track(x, y)
                if track:
                    if track.track_type == TrackType.STRAIGHT:
                        row += "-"
                    elif track.track_type == TrackType.CURVE:
                        row += "╭"
                    elif track.track_type == TrackType.SWITCH:
                        row += "S"
                    elif track.track_type == TrackType.SIGNAL:
                        row += "O"
                    elif track.track_type == TrackType.STATION:
                        row += "▦"
                    else:
                        row += "?"
                else:
                    row += " "
            print(row)

    # === Centralized Station Management ===

    def _is_track_on_straight_path(self, x: int, y: int) -> bool:
        """
        Check if a track tile is part of a logical straight path.

        This function allows stations to be placed on:
        1. Pure straight tracks (TRACK_NS or TRACK_EW)
        2. Simple diagonal tracks (NE, NW, SE, SW) that can form zigzag patterns

        Args:
            x, y: Grid coordinates of the track

        Returns:
            bool: True if the track is on a straight path (pure or diagonal)
        """
        from track import TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW

        track = self.get_track(x, y)
        if not track:
            return False

        # Pure straight tracks are always valid
        if track.track_bits in (TRACK_NS, TRACK_EW):
            return True

        # Simple diagonal tracks are also valid for zigzag patterns
        # The user can extend the station to cover multiple tiles forming the zigzag
        if track.track_bits in (TRACK_NE, TRACK_NW, TRACK_SE, TRACK_SW):
            return True

        # Complex tracks (multiple paths) are not valid
        return False

    def add_station(self, *args, **kwargs):
        """Forward to `StationRegistry.add_station(...)`."""
        return self._get_station_registry().add_station(*args, **kwargs)

    def add_platform(self, *args, **kwargs):
        """Forward to `StationRegistry.add_platform(...)`."""
        return self._get_station_registry().add_platform(*args, **kwargs)

    def get_station(self, station_id: str):
        """Forward to `StationRegistry.get_station(station_id)`."""
        return self._get_station_registry().get_station(station_id)

    def get_platform(self, platform_id: str):
        """Forward to `StationRegistry.get_platform(platform_id)`."""
        return self._get_station_registry().get_platform(platform_id)

    def delete_station(self, station_id: str) -> bool:
        """Forward to `StationRegistry.delete_station(station_id)`."""
        return self._get_station_registry().delete_station(station_id)

    def delete_platform(self, platform_id: str) -> bool:
        """Forward to `StationRegistry.delete_platform(platform_id)`."""
        return self._get_station_registry().delete_platform(platform_id)

    def extend_station(self, *args, **kwargs):
        """Forward to `StationRegistry.extend_station(...)`."""
        return self._get_station_registry().extend_station(*args, **kwargs)

    def extend_platform(self, *args, **kwargs):
        """Forward to `StationRegistry.extend_platform(...)`."""
        return self._get_station_registry().extend_platform(*args, **kwargs)

    def get_station_for_platform(self, platform_id: str) -> Optional[str]:
        """Forward to `StationRegistry.get_station_for_platform(platform_id)`."""
        return self._get_station_registry().get_station_for_platform(platform_id)

    def get_platforms_for_station(self, station_id: str):
        """Forward to `StationRegistry.get_platforms_for_station(station_id)`."""
        return self._get_station_registry().get_platforms_for_station(station_id)

    def create_station_group(self, *args, **kwargs):
        """Forward to `StationRegistry.create_station_group(...)`."""
        return self._get_station_registry().create_station_group(*args, **kwargs)

    def delete_station_group(self, station_id: str) -> bool:
        """Forward to `StationRegistry.delete_station_group(station_id)`."""
        return self._get_station_registry().delete_station_group(station_id)

    def add_platform_to_station_group(
        self, station_id: str, platform_id: str, force: bool = False
    ) -> bool:
        """Forward to `StationRegistry.add_platform_to_station_group(...)`."""
        return self._get_station_registry().add_platform_to_station_group(
            station_id, platform_id, force=force
        )

    def remove_platform_from_station_group(self, *args, **kwargs):
        """Forward to `StationRegistry.remove_platform_from_station_group(...)`."""
        return self._get_station_registry().remove_platform_from_station_group(*args, **kwargs)

    def _validate_station_continuity(
        self, positions: List[Tuple[int, int]]
    ) -> Tuple[bool, List[str]]:
        """
        Validate that station positions form a continuous path.

        Uses BFS similar to block continuity validation.

        Args:
            positions: List of (x, y) tile positions

        Returns:
            Tuple of (is_continuous, error_messages)
        """
        if len(positions) == 0:
            return True, []

        if len(positions) == 1:
            return True, []

        errors = []
        position_set = set(positions)

        # Start BFS from first position
        start = positions[0]
        visited = {start}
        queue = [start]

        while queue:
            x, y = queue.pop(0)
            track = self.get_track(x, y)
            if not track:
                continue

            # Check all four neighbors
            for dx, dy in [(0, 1), (1, 0), (0, -1), (-1, 0)]:
                nx, ny = x + dx, y + dy
                neighbor_pos = (nx, ny)

                # Only consider positions that are in the station
                if neighbor_pos in position_set and neighbor_pos not in visited:
                    # Check if tracks are actually connected via edges
                    neighbor = self.get_track(nx, ny)
                    if neighbor:
                        # Simplified: just check if neighbor exists in position set
                        visited.add(neighbor_pos)
                        queue.append(neighbor_pos)

        # Check if all positions were visited
        if len(visited) != len(positions):
            unvisited = position_set - visited
            errors.append(f"Disconnected tiles: {unvisited}")
            return False, errors

        return True, []

    def _disconnect_signals_for_block(self, block_id: str) -> None:
        """Forward to `SegmentGeometryManager._disconnect_signals_for_block_of_segment`."""
        return self._get_segment_geometry_manager()._disconnect_signals_for_block_of_segment(
            block_id
        )

    def remove_tile_from_segment(self, x: int, y: int) -> List[str]:
        """Forward to `SegmentGeometryManager.remove_tile_from_segment(x, y)`."""
        return self._get_segment_geometry_manager().remove_tile_from_segment(x, y)

    def remove_tiles_from_segment(self, positions: List[Tuple[int, int]]) -> List[str]:
        """Forward to `SegmentGeometryManager.remove_tiles_from_segment(positions)`."""
        return self._get_segment_geometry_manager().remove_tiles_from_segment(positions)

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

    def _remap_orphaned_signals(self, *args, **kwargs):
        """Forward to `SegmentGeometryManager._remap_orphaned_signals(...)`."""
        return self._get_segment_geometry_manager()._remap_orphaned_signals(*args, **kwargs)

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
        # A new switch can create flank relationships for itself and any
        # nearby switches (design_new.md:686 — recompute on topology change).
        self.recompute_all_flank_protection()

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
        # Drop any stale flank entries that referenced the removed switch and
        # recompute neighbours (design_new.md:686 — recompute on topology
        # change).
        self.recompute_all_flank_protection()
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
        # *_length changes can move a neighbour in/out of the flank radius.
        self.recompute_all_flank_protection()
        return switch

    # ------------------------------------------------------------------
    # Flank protection computation (design_new.md "Flank Protection
    # Computation", lines 679-688)
    # ------------------------------------------------------------------

    def recompute_all_flank_protection(self) -> None:
        """Forward to `FlankProtectionCalculator.recompute_all_flank_protection()`."""
        return self._get_flank_protection_calculator().recompute_all_flank_protection()

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
        A save written before platform blocks claimed their tiles carries the
        order but no tile ownership, so those segments would load owning
        nothing: the block exists and lists its tiles, yet
        `get_block_positions` is empty and the platform's `block_assignments`
        never populate. Stamp the ownership back on from the order that
        survived.

        Only an unowned tile is claimed, so a tile already belonging to another
        segment is never stolen, and a save that already carries tile ownership
        is left untouched.
        """
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
            members = [b for b in member_ids if b in self.blocks]
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

    def to_dict(self) -> dict:
        """Convert network to dictionary for the live frontend payload.

        Includes runtime state on every sub-entity (block occupancy,
        signal aspects, switch position/locks, track locks) for UI
        rendering. Persistence does NOT use this path; see
        `to_layout_dict()` instead.
        """
        # Enrich tracks with full signal data for frontend
        enriched_tracks = []
        for t in self.tracks.values():
            track_dict = t.to_dict()
            # Add full signal objects for frontend rendering
            track_dict["signals"] = [
                self.signals[sid].to_dict()
                for sid in t.signal_ids
                if sid in self.signals
            ]
            enriched_tracks.append(track_dict)

        return {
            "tracks": enriched_tracks,
            # Every train, unidentified trains included. This is the live UI payload, and a
            # unidentified train must appear in the train list and on the layout, marked apart
            # from the trains the operator controls (design_new.md "Unidentified train
            # trains", Shown as itself) — it is also how the operator reaches the
            # panel that adopts it. The save payload is the one that leaves them
            # out; see `to_layout_dict`.
            "trains": [t.to_dict(self) for t in self.trains.values()],
            "blocks": {k: v.to_dict() for k, v in self.blocks.items()},
            "signals": {k: v.to_dict() for k, v in self.signals.items()},
            "platforms": [
                p.to_dict_with_orientation(self) for p in self.platforms.values()
            ],
            "station_groups": [s.to_dict() for s in self.station_groups.values()],
            "train_roster": self.train_roster.to_dict(),
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

        Per design_new.md Out of Scope ("Persistence / save-load of
        runtime state | In-memory only"), this writes only editor /
        configuration data: track geometry, block / switch / signal
        definitions, platforms, station groups, and train config. Live
        runtime fields on every sub-entity are excluded.
        """
        return {
            "tracks": [t.to_layout_dict() for t in self.tracks.values()],
            # Unidentified trains are runtime-only: they are not written to saved
            # state and not restored, because a restart re-detects whatever is
            # still drawing current (design_new.md "Unidentified trains", Runtime only).
            "trains": [
                t.to_dict(self) for t in self.trains.values() if not t.is_unidentified
            ],
            "blocks": {k: v.to_layout_dict() for k, v in self.blocks.items()},
            "signals": {k: v.to_layout_dict() for k, v in self.signals.items()},
            "platforms": [
                p.to_dict_with_orientation(self) for p in self.platforms.values()
            ],
            "station_groups": [s.to_dict() for s in self.station_groups.values()],
            "train_roster": self.train_roster.to_dict(),
            "switches": {
                k: v.to_layout_dict() for k, v in self.switches.items()
            },
            "tunnels": self.tunnels_to_list(),
        }

    def from_dict(self, data: dict):
        """Reconstruct network from dictionary"""
        # Clear existing — entity dicts plus the tile->owner reverse indices
        # and caches, so a load can never inherit stale block/platform
        # membership on tiles the previous layout occupied.
        self.clear()

        for block_id, block_data in data.get("blocks", {}).items():
            self.blocks[block_id] = Block.from_dict(block_data)

        self._ensure_resource_managers()

        for t_data in data.get("tracks", []):
            track = Tile.from_dict(t_data)
            self.add_track(track)

        # Tile ownership is knowable only now that both the blocks and the
        # tracks are in place, so heal a save that stored only one of the two.
        self._reconcile_segment_tile_membership()

        # Rebuild signals
        for signal_id, signal_data in data.get("signals", {}).items():
            signal = Signal.from_dict(signal_data)
            self.signals[signal_id] = signal
            # Update track's signal_ids list
            track = self.get_track(*signal.position)
            if track and signal_id not in track.signal_ids:
                track.signal_ids.append(signal_id)

        # Rebuild switches
        from switch import Switch

        for switch_id, switch_data in data.get("switches", {}).items():
            switch = Switch.from_dict(switch_data)
            self.switches[switch_id] = switch

        # Rebuild physical platforms
        platforms_data = data.get("platforms", [])

        for platform_data in platforms_data:
            platform = Platform.from_dict(platform_data)
            self.platforms[platform.platform_id] = platform
            # Establish the canonical block_assignments index from the loaded
            # tile ownership when a platform was serialized without it, so
            # ordered_blocks / get_all_assigned_blocks resolve every platform's
            # block without any read-time tile scan. Tracks are already loaded
            # above, and this is a no-op when block_assignments came through.
            if not platform.block_assignments:
                for px, py in platform.positions:
                    ptrack = self.get_track(px, py)
                    if ptrack and ptrack.block_id:
                        platform.block_assignments[(px, py)] = ptrack.block_id

        # Rebuild logical station groups (optional in legacy saves)
        for station_data in data.get("station_groups", []):
            station = Station.from_dict(station_data)
            self.station_groups[station.station_id] = station

        # Fold any grouping from an older save. Those snapshots stored the
        # power grouping separately from the blocks it grouped, so each grouped
        # member loaded above as its own one-segment block; folding replaces
        # that set with the one block the grouping always described, owning each
        # member's geometry as a member segment. Runs after every block exists
        # (a grouping may name a platform block).
        self._fold_grouped_blocks(data.get("power_districts", []))

        # Rebuild the train roster (config; absent in legacy saves = empty).
        self.train_roster = TrainRoster.from_dict(data.get("train_roster", []))

        # Rebuild trains
        for train_data in data.get("trains", []):
            train = Train.from_dict(train_data)
            self.trains[train.train_id] = train

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

        # `Switch.from_dict` does not persist runtime `flank_protection`
        # (design_new.md: it is recomputed, not loaded). Compute it once the
        # full topology — tracks, switches, and tunnels — is in place.
        self.recompute_all_flank_protection()
