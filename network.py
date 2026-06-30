"""
Railway Network Management

Definition:
This module provides the RailwayNetwork class which manages the complete railway infrastructure
including tracks, trains, and block sections. It serves as the central repository and
coordinator for all railway system components.
"""

from collections import deque
import logging
from typing import Dict, List, Optional, Set, Tuple, Any, Union, TYPE_CHECKING
import uuid
from track import TrackSegment, EDGE_MASKS, BIT_TO_EDGES
from train import Train
from block import Block
from block_manager import BlockManager
from switch_manager import SwitchManager
from resource_coordinator import ResourceCoordinator
from enums import TrackType, Direction
from constants import FLANK_PROTECTION_DISTANCE_CM
from railway_signal import Signal, SignalType, SignalState
from station import Station
from power_district import PowerDistrict
from train_roster import TrainRoster
from railway_platform import Platform
from speed_profile import SpeedProfileRegistry
from signal_validation import validate_signal_placement

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
    with 2+ exits). Mirrors `TrackSegment.switch_points` without needing
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
        self.tracks: Dict[Tuple[int, int], TrackSegment] = {}
        self.trains: Dict[str, Train] = {}
        self.blocks: Dict[str, Block] = {}
        self.signals: Dict[str, Signal] = {}
        self.platforms: Dict[str, Platform] = {}
        self.stations = self.platforms
        self.station_groups: Dict[str, Station] = {}
        self.switches: Dict[str, "Switch"] = {}  # NEW: Switch entity management
        # Power districts: block groupings that share one power feed
        # (design_new.md "Power district"). `_block_to_district` is the derived
        # reverse index (block_id -> district_id) rebuilt from `power_districts`;
        # a block absent from it is its own single-block district.
        self.power_districts: Dict[str, PowerDistrict] = {}
        self._block_to_district: Dict[str, str] = {}
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
        self.switch_power_controller: Optional["SwitchPowerController"] = None

        # Reverse indices from tile -> owning block / platform.
        # Maintained in sync by network mutators (block creation, deletion,
        # platform creation, platform deletion, etc.). Readers should call
        # tile_to_block_id / tile_to_platform_id / tile_to_station_id below.
        self._tile_to_block: Dict[Tuple[int, int], str] = {}
        self._tile_to_platform: Dict[Tuple[int, int], str] = {}

        # Reverse index block id -> its TrackSegments, derived from
        # _tile_to_block and built lazily by _block_tiles_index(). Turns
        # get_block_tracks() from an O(all tracks) scan into an O(block)
        # lookup; the lookahead calls it thousands of times per tick. None
        # means "rebuild on next read"; invalidate_pathfinding_cache() resets
        # it on every topology change (none of which run in the tick loop).
        self._block_index_cache: Optional[Dict[str, List["TrackSegment"]]] = None

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
        self._block_geometry_manager = None
        self._station_registry = None
        self._power_district_registry = None

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

    def _get_block_geometry_manager(self):
        """Lazy-init `BlockGeometryManager`. Block geometry / topology
        mutation lives in `block_geometry_manager.py`; this network
        keeps doc-canonical method names as forwarders."""
        if self._block_geometry_manager is None:
            from block_geometry_manager import BlockGeometryManager

            self._block_geometry_manager = BlockGeometryManager(self)
        return self._block_geometry_manager

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

    def _get_power_district_registry(self):
        """Lazy-init `PowerDistrictRegistry`. Power-district CRUD lives there;
        the `power_districts` dict and `_block_to_district` index stay on the
        network; the registry reads / writes them through its back-reference.
        """
        if self._power_district_registry is None:
            from power_district_registry import PowerDistrictRegistry

            self._power_district_registry = PowerDistrictRegistry(self)
        return self._power_district_registry

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

    def tile_to_block_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning Block id, or None.

        Per design_new.md: TrackSegment does not store block membership.
        All "which block owns this tile?" lookups go through this method.
        """
        return self._tile_to_block.get((x, y))

    def tile_to_platform_id(self, x: int, y: int) -> Optional[str]:
        """Forward to `StationRegistry.tile_to_platform_id(x, y)`."""
        return self._get_station_registry().tile_to_platform_id(x, y)

    def tile_to_station_id(self, x: int, y: int) -> Optional[str]:
        """Forward to `StationRegistry.tile_to_station_id(x, y)`."""
        return self._get_station_registry().tile_to_station_id(x, y)

    def _set_tile_block(self, x: int, y: int, block_id: Optional[str]) -> None:
        """Update the tile -> block reverse index.

        Network-internal helper. Use this whenever a tile's block membership
        changes (block creation, extension, deletion, station split, etc.).
        """
        key = (x, y)
        if block_id is None:
            self._tile_to_block.pop(key, None)
        else:
            self._tile_to_block[key] = block_id
        # Block membership feeds the BFS walk enumeration; a change here makes
        # any cached walk for the affected blocks stale.
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
        # Defensive getattr: the block_section_id setter routes through
        # _set_tile_block -> here, which can fire on a TrackSegment whose
        # network back-reference was attached before __init__ finished wiring
        # the cache (e.g. unusual deserialization orders).
        cache = getattr(self, "_walk_cache", None)
        if cache:
            cache.clear()
        # The block->tracks reverse index is derived from tile->block
        # membership, so it is stale after any topology change. Drop it; the
        # next get_block_tracks() rebuilds it from the new topology.
        self._block_index_cache = None

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
        self.power_districts.clear()
        self.train_roster.clear()
        # Derived tile -> owner reverse indices. Not held on any entity dict,
        # so they must be cleared explicitly to avoid stale block/platform
        # membership on reused tiles.
        self._tile_to_block.clear()
        self._tile_to_platform.clear()
        self._block_to_district.clear()
        # Drops _walk_cache + _block_index_cache.
        self.invalidate_pathfinding_cache()

    def add_track(self, track: TrackSegment):
        """Add a track segment to the network and propagate updates"""
        key = (track.x, track.y)
        self.tracks[key] = track

        # Move any pre-attachment local-fallback values for station_id /
        # block_section_id into the network reverse indices so that reads
        # via the property continue to see the same value after attach.
        local_block = track._block_section_id_local
        local_station = track._station_id_local
        track.network = self
        if local_block is not None:
            track.block_section_id = local_block
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

    def get_track(self, x: int, y: int) -> Optional[TrackSegment]:
        """Get track segment at specified position"""
        return self.tracks.get((x, y))

    def get_neighbor(self, x: int, y: int, edge: str) -> Optional[TrackSegment]:
        """Get neighbor tile in the specified direction.

        Args:
            x, y: Current tile coordinates
            edge: Direction to look ('N', 'E', 'S', 'W')

        Returns:
            TrackSegment if neighbor exists, None otherwise. When the grid
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
        Block membership is reconciled via `remove_tile_from_block`.
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

        if track.block_section_id:
            # Block reconciliation can touch signals on OTHER tiles in the
            # block (split + remap). If that work fails, we still want the
            # tile itself to be removed so the caller doesn't end up with
            # an orphan entry in self.tracks.
            try:
                self.remove_tile_from_block(x, y)
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

    def create_block(self, block_id: str, name: str = "") -> Block:
        """Forward to `BlockGeometryManager.create_block(block_id, name)`."""
        return self._get_block_geometry_manager().create_block(block_id, name)

    def create_block_with_tracks(self, *args, **kwargs):
        """Forward to `BlockGeometryManager.create_block_with_tracks(...)`."""
        return self._get_block_geometry_manager().create_block_with_tracks(*args, **kwargs)

    def extend_block(self, *args, **kwargs):
        """Forward to `BlockGeometryManager.extend_block(...)`."""
        return self._get_block_geometry_manager().extend_block(*args, **kwargs)

    def delete_block(self, block_id: str) -> bool:
        """Forward to `BlockGeometryManager.delete_block(block_id)`."""
        return self._get_block_geometry_manager().delete_block(block_id)

    def get_block(self, block_id: str) -> Optional[Block]:
        return self.blocks.get(block_id)

    def _block_tiles_index(self) -> Dict[str, List[TrackSegment]]:
        """Lazily-built reverse index: block id -> its TrackSegments.

        Built by a single pass over ``self.tracks`` (preserving
        ``tracks.values()`` order, so ``get_block_tracks`` returns tracks in
        the same order as the old full scan). Cached in ``_block_index_cache``
        and invalidated by ``invalidate_pathfinding_cache`` on any topology
        change — none of which run inside the sim tick loop, so it is built
        once and reused across a whole ``update()`` storm.
        """
        idx = self._block_index_cache
        if idx is None:
            idx = {}
            for t in self.tracks.values():
                bid = t.block_section_id
                if bid is not None:
                    idx.setdefault(bid, []).append(t)
            self._block_index_cache = idx
        return idx

    def get_block_tracks(self, block_id: str) -> List[TrackSegment]:
        """Get all tracks belonging to a block (O(block) via the reverse
        index in ``_block_tiles_index``, not an O(all tracks) scan)."""
        return list(self._block_tiles_index().get(block_id, ()))

    def get_block_positions(self, block_id: str) -> List[Tuple[int, int]]:
        """Get grid positions of all tracks in block"""
        return [(t.x, t.y) for t in self.get_block_tracks(block_id)]

    def assign_track_to_block(self, *args, **kwargs):
        """Forward to `BlockGeometryManager.assign_track_to_block(...)`."""
        return self._get_block_geometry_manager().assign_track_to_block(*args, **kwargs)

    def validate_block_continuity(self, block_id: str) -> Tuple[bool, List[str]]:
        """Forward to `BlockGeometryManager.validate_block_continuity(block_id)`."""
        return self._get_block_geometry_manager().validate_block_continuity(block_id)

    def _get_block_connected_neighbors(
        self, track: TrackSegment
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
        """
        Create and add a signal at the specified position.

        Args:
            x, y: Grid coordinates for signal placement
            detection_direction: Direction trains travel TO (NORTH, SOUTH, EAST, WEST)
            signal_type: Type of signal (BLOCK, SWITCH, etc.)
            name: Optional human-readable name
            auto_connect: If True, automatically find and connect to block ahead
            signal_id: Optional specific ID, auto-generated if not provided

        Returns:
            Created Signal instance

        Raises:
            ValueError: If position invalid, direction invalid, or signal already exists
        """
        # Convert string signal_type to enum if needed
        if isinstance(signal_type, str):
            signal_type = SignalType(signal_type.lower())

        # Check if track exists
        track = self.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # Check if tile is part of a station (signals not allowed on platform tiles)
        if track.station_id is not None:
            raise ValueError(
                f"Cannot place signal on station tile at ({x}, {y}). "
                f"Tile belongs to station {track.station_id}"
            )

        # Validate signal placement
        is_valid, error_msg = validate_signal_placement(
            track.track_bits, detection_direction.upper()
        )
        if not is_valid:
            raise ValueError(error_msg)

        # Check for existing signal in this detection direction
        existing = self.get_signal_at_direction(x, y, detection_direction.upper())
        if existing:
            raise ValueError(
                f"Signal already exists at ({x}, {y}) with detection direction {detection_direction}: {existing.signal_id}"
            )

        # Determine max signals based on track type:
        # - Diagonal tracks (NE=4, NW=8, SE=16, SW=32): 1 signal max (single position at center)
        # - Straight tracks (NS=1, EW=2): 2 signals max (two positions)
        is_diagonal = track.track_bits in (4, 8, 16, 32)
        max_signals = 1 if is_diagonal else 2

        if len(track.signal_ids) >= max_signals:
            if is_diagonal:
                raise ValueError(
                    f"Diagonal track at ({x}, {y}) can only have 1 signal (center position)"
                )
            else:
                raise ValueError(
                    f"Cannot add more than 2 signals per tile at ({x}, {y})"
                )

        # Generate signal ID if not provided
        if signal_id is None:
            signal_id = f"SIG_{x}_{y}_{uuid.uuid4().hex[:6]}"

        # NOTE: Signals CAN be placed on tiles that belong to blocks.
        # The signal tile remains in its block. The signal can monitor:
        # - Any block AHEAD (normal case)
        # - Its OWN block, but ONLY if positioned on the entry tile
        # (The entry tile validation is enforced in get_reachable_blocks and connect_signal_to_block)

        # Create signal
        signal = Signal(
            signal_id=signal_id,
            position=(x, y),
            detection_direction=detection_direction.upper(),
            signal_type=signal_type,
            state=SignalState.RED,
            name=name,
        )

        # Store signal first (required for get_reachable_blocks to work)
        self.signals[signal_id] = signal

        # Auto-connect to block if requested using proper pathfinding
        if auto_connect and signal_type == SignalType.BLOCK:
            # Use the same pathfinding logic as the UI dropdown
            reachable_blocks = self.get_reachable_blocks(signal_id)
            if reachable_blocks:
                # Connect to the nearest reachable block (first in sorted list)
                signal.monitored_block_id = reachable_blocks[0]

        # Calculate signal state based on monitored block (if connected)
        if signal.monitored_block_id and signal_type == SignalType.BLOCK:
            signal.state = self._calculate_signal_state(signal)

        # Add signal ID to track's signal_ids list (max 2 per tile)
        if signal.signal_id not in track.signal_ids:
            track.signal_ids.append(signal.signal_id)

        return signal

    def get_signal(self, signal_id: str) -> Optional[Signal]:
        return self.signals.get(signal_id)

    def get_signal_at(self, x: int, y: int) -> Optional[Signal]:
        """Get first signal at the specified position."""
        for signal in self.signals.values():
            if signal.position == (x, y):
                return signal
        return None

    def get_signal_at_direction(
        self, x: int, y: int, detection_direction: str
    ) -> Optional[Signal]:
        """Get signal at position with a specific detection direction."""
        for signal in self.signals.values():
            if (
                signal.position == (x, y)
                and signal.detection_direction == detection_direction.upper()
            ):
                return signal
        return None

    def remove_signal(self, signal_id: str) -> bool:
        """
        Remove signal by ID.

        Returns:
            True if removed, False if not found
        """
        signal = self.signals.get(signal_id)
        if not signal:
            return False

        # Remove signal ID from track's signal_ids list
        track = self.get_track(*signal.position)
        if track and signal_id in track.signal_ids:
            track.signal_ids.remove(signal_id)

        del self.signals[signal_id]
        return True

    def remove_signal_at(self, x: int, y: int) -> bool:
        """
        Remove signal at position.

        Returns:
            True if removed, False if not found
        """
        signal = self.get_signal_at(x, y)
        if signal:
            return self.remove_signal(signal.signal_id)
        return False

    def update_signal(self, signal_id: str, **kwargs) -> Optional[Signal]:
        """
        Update signal properties.

        Allowed updates:
            - state: SignalState or string
            - name: str
            - monitored_block_id: str
            - monitored_switch_id: str
            - monitored_signal_id: str

        Args:
            signal_id: Signal to update
            **kwargs: Properties to update

        Returns:
            Updated Signal or None if not found

        Raises:
            ValueError: If trying to change immutable properties
        """
        signal = self.signals.get(signal_id)
        if not signal:
            return None

        # Prevent changing immutable properties
        immutable = {"signal_id", "position", "direction"}
        for key in kwargs:
            if key in immutable:
                raise ValueError(f"Cannot change immutable property: {key}")

        # Update allowed properties
        if "state" in kwargs:
            state_val = kwargs["state"]
            if isinstance(state_val, str):
                state_val = SignalState(state_val.lower())
            signal.state = state_val

        if "name" in kwargs:
            signal.name = kwargs["name"]

        if "monitored_block_id" in kwargs:
            new_block_id = kwargs["monitored_block_id"]

            # Validate: If monitoring own block, signal MUST be on entry tile
            if new_block_id:
                signal_track = self.get_track(*signal.position)
                if signal_track and signal_track.block_section_id == new_block_id:
                    if not self.is_signal_on_entry_tile(signal):
                        raise ValueError(
                            f"Signal cannot monitor block '{new_block_id}' from the middle. "
                            f"Signal at {signal.position} must be on the entry tile of the block to monitor it."
                        )

            signal.monitored_block_id = new_block_id

            # Recalculate signal state based on new monitored block
            if signal.signal_type == SignalType.BLOCK:
                if signal.monitored_block_id:
                    # Connected to a block - calculate state based on block occupancy
                    new_state = self._calculate_signal_state(signal)
                else:
                    # Disconnected - show RED (no block to monitor)
                    new_state = SignalState.RED
                signal.state = new_state

        if "monitored_switch_id" in kwargs:
            signal.monitored_switch_id = kwargs["monitored_switch_id"]

        if "monitored_signal_id" in kwargs:
            signal.monitored_signal_id = kwargs["monitored_signal_id"]

        return signal

    def connect_signal_to_block(self, signal_id: str, block_id: str) -> bool:
        """
        Manually connect a signal to monitor a specific block.

        Args:
            signal_id: Signal to connect
            block_id: Block to monitor

        Returns:
            True if successful

        Raises:
            ValueError: If signal/block not found, or no valid path exists, or
                       signal is in the middle of its own block (not on entry tile)
        """
        signal = self.signals.get(signal_id)
        if not signal:
            raise ValueError(f"Signal '{signal_id}' not found")

        if block_id not in self.blocks:
            raise ValueError(f"Block '{block_id}' not found")

        # Get the signal's own block (the block at the signal's position)
        signal_track = self.get_track(*signal.position)
        signal_own_block = signal_track.block_section_id if signal_track else None

        # Check if trying to monitor own block
        if signal_own_block and block_id == signal_own_block:
            # Can only monitor own block if on entry tile
            if not self.is_signal_on_entry_tile(signal):
                raise ValueError(
                    f"Signal cannot monitor block '{block_id}' from the middle. "
                    f"Signal at {signal.position} must be on the entry tile of the block to monitor it."
                )
            # Signal is on entry tile - allowed to monitor own block
            # Skip the path validation since we're already in the block
        else:
            # Validate that a path exists from signal to target block
            if not self._can_reach_block(signal, block_id):
                raise ValueError(
                    f"No valid path exists from signal at {signal.position} "
                    f"(facing {signal.approach_direction}, trains travel {signal.detection_direction}) to block '{block_id}'. "
                    f"Check track connectivity and switch geometry."
                )

        signal.monitored_block_id = block_id

        # Recalculate signal state based on new monitored block
        if signal.signal_type == SignalType.BLOCK:
            signal.state = self._calculate_signal_state(signal)

        if signal_track:
            signal_track.signal_block_id = block_id

        return True

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
    ) -> "TrainSpan":
        """
        Place a train on a tile with proper positioning based on direction and length.

        Train placement rules:
        1. Train HEAD (end_a) is always placed at the EXIT edge of the specified tile
        2. Train tail (end_b) extends backward - MUST fit on available track
        3. Builds the canonical TrainSpan with both physical ends tracked precisely
        4. Occupies all blocks the train covers

        Args:
            train: Train to place
            tile_x, tile_y: Target tile coordinates
            direction: Direction the train will be heading (random if None)

        Returns:
            The canonical `TrainSpan` for the placed train (4-field schema).
            Callers seed `TrainController.physics_sync.current_span` from it
            (`add_train` / API re-placement).

        Raises:
            ValueError: If no track at position, or train doesn't fit on available track
        """
        from train_span import SpanEndLocator, TrainSpan
        import random

        track = self.get_track(tile_x, tile_y)
        if not track:
            raise ValueError(f"No track at ({tile_x}, {tile_y})")

        # If no direction provided, pick one from available edges
        if direction is None:
            available_edges = list(track.edges)
            if not available_edges:
                raise ValueError(f"Track at ({tile_x}, {tile_y}) has no edges")
            edge = random.choice(available_edges)
            DIR_FROM_EDGE = {
                "N": Direction.NORTH,
                "E": Direction.EAST,
                "S": Direction.SOUTH,
                "W": Direction.WEST,
            }
            direction = DIR_FROM_EDGE[edge]

        DIR_TO_EDGE = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
        exit_edge = DIR_TO_EDGE.get(direction.name, "E")

        if exit_edge not in track.edges:
            raise ValueError(
                f"Track at ({tile_x}, {tile_y}) does not support direction {direction.name}"
            )

        neighbor = self.get_neighbor(tile_x, tile_y, exit_edge)
        if not neighbor:
            raise ValueError(
                f"Cannot place train facing {direction.name}: no track at the "
                f"neighbor of ({tile_x}, {tile_y}) via edge {exit_edge}"
            )

        train_length_cm = self._get_train_length_cm(train)
        tile_length_cm = self._get_tile_length_cm(tile_x, tile_y)

        # The entry edge is the OTHER end of the rail segment that has
        # `exit_edge` as its exit — NOT the strict cardinal opposite. For a
        # NW corner tile (N+W edges), exit=N means the train is sitting on
        # the N→W rail and the body extends through the W edge; using
        # OPPOSITE_EDGE[N]=S here would point at an edge the tile does not
        # actually have. `get_entry_for_exit` respects rail topology and
        # switch state.
        entry_edge = track.get_entry_for_exit(exit_edge, use_switch_state=True)
        if entry_edge is None:
            raise ValueError(
                f"Track at ({tile_x}, {tile_y}) has no rail segment that exits via {exit_edge}; "
                f"cannot place train facing {direction.name}."
            )

        # Head is at exit edge, so head_distance = tile_length
        head_tile = (tile_x, tile_y)
        head_distance_cm = tile_length_cm
        head_entry_edge = entry_edge

        # Calculate occupied tiles and tail position
        # This will raise ValueError if train doesn't fit
        (occupied_tiles, tail_tile, tail_distance_cm, tail_entry_edge) = (
            self._calculate_occupied_tiles(
                head_tile, head_distance_cm, head_entry_edge, train_length_cm
            )
        )

        # Calculate occupied blocks from occupied tiles
        occupied_blocks = []
        seen_blocks = set()
        for tile in occupied_tiles:
            tile_track = self.get_track(*tile)
            if tile_track and tile_track.block_section_id:
                block_id = tile_track.block_section_id
                if block_id not in seen_blocks:
                    occupied_blocks.append(block_id)
                    seen_blocks.add(block_id)

        # Build the canonical 4-field TrainSpan (design_new.md TrainSpan
        # schema). `_calculate_occupied_tiles` returns tiles tail-to-head; the
        # span orders `occupied_tiles` end_a -> end_b, and end_a is the
        # placement (head) tile, so reverse to head-to-tail (end_a -> end_b).
        occupied_tiles_end_a_to_b = list(reversed(occupied_tiles))
        span = TrainSpan(
            end_a=SpanEndLocator(
                tile=head_tile,
                distance_cm=head_distance_cm,
                entry_edge=head_entry_edge,
            ),
            end_b=SpanEndLocator(
                tile=tail_tile,
                distance_cm=tail_distance_cm,
                entry_edge=tail_entry_edge,
            ),
            occupied_tiles=occupied_tiles_end_a_to_b,
            occupied_blocks=occupied_blocks,
        )

        # Occupy all blocks the train covers
        if self.block_manager and occupied_blocks:
            self.block_manager.occupy_blocks(occupied_blocks, train.train_id)
        else:
            for block_id in occupied_blocks:
                block = self.blocks.get(block_id)
                if block:
                    block.occupy(train.train_id)

        return span

    def _get_train_length_cm(self, train: Train) -> float:
        """Get train length in cm."""
        if train.consist:
            total_length = sum(car.length_cm for car in train.consist)
            return total_length
        return DEFAULT_TRAIN_LENGTH_CM

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

        if track.block_section_id:
            block = self.blocks.get(track.block_section_id)
            if block and block.length_cm > 0:
                positions = self.get_block_positions(track.block_section_id)
                tile_count = len(positions) if positions else 1
                return block.length_cm / tile_count
            return DEFAULT_TILE_LENGTH_CM

        return DEFAULT_TILE_LENGTH_CM

    def _get_neighbor_coords(self, x: int, y: int, edge: str) -> Tuple[int, int]:
        """Get coordinates of neighbor tile in given direction."""
        dx, dy = _EDGE_OFFSETS.get(edge, (0, 0))
        return (x + dx, y + dy)

    def _calculate_occupied_tiles(
        self,
        head_tile: Tuple[int, int],
        head_distance_cm: float,
        head_entry_edge: str,
        train_length_cm: float,
        path: Optional[List[Tuple[int, int, Direction]]] = None,
        path_index: int = 0,
    ) -> Tuple[List[Tuple[int, int]], Tuple[int, int], float, str]:
        """
        Calculate all tiles occupied by a train given its head position and length.

        Traces backward from head position through connected tiles to find where
        the tail (end_b) is located.

        Args:
            head_tile: (x, y) coordinates of tile where head is located
            head_distance_cm: Distance from entry edge to head on that tile (cm)
            head_entry_edge: Edge where head entered the tile (N/E/S/W)
            train_length_cm: Total train length (cm)
            path: Optional train path for correct backward tracing through switches
            path_index: Current index in path (head position)

        Returns:
            Tuple of:
            - occupied_tiles: List of (x, y) from tail to head
            - tail_tile: (x, y) coordinates where tail is located
            - tail_distance_cm: Distance from entry edge to tail on tail tile
            - tail_entry_edge: Edge where tail tile was entered from

        Raises:
            ValueError: If train is too long for available track
        """
        DIR_TO_EDGE = {
            Direction.NORTH: "N",
            Direction.SOUTH: "S",
            Direction.EAST: "E",
            Direction.WEST: "W",
        }

        # Build path lookup: tile -> (entry_edge, exit_direction)
        # This helps us trace backward correctly through switches
        path_lookup: Dict[Tuple[int, int], str] = {}
        if path:
            for i, (px, py, pdir) in enumerate(path):
                if pdir and i <= path_index:
                    # Exit direction -> entry is opposite
                    exit_edge = DIR_TO_EDGE.get(pdir, "N")
                    entry_edge = _OPPOSITE_EDGE.get(exit_edge, "S")
                    path_lookup[(px, py)] = entry_edge

        occupied = []
        accumulated_length = 0.0

        current_tile = head_tile
        current_distance = head_distance_cm
        current_entry_edge = head_entry_edge

        # First, account for the portion of the head tile behind the head
        head_tile_length = self._get_tile_length_cm(*head_tile)
        length_behind_head = current_distance  # Distance from entry to head

        occupied.append(current_tile)
        accumulated_length += length_behind_head

        if accumulated_length >= train_length_cm:
            # Tail is on same tile as head
            tail_distance = current_distance - train_length_cm
            return (occupied, current_tile, tail_distance, current_entry_edge)

        # Trace backward through tiles
        visited = {current_tile}
        max_iterations = 1000  # Safety limit

        for _ in range(max_iterations):
            # Get previous tile (in direction opposite to entry)
            backward_edge = current_entry_edge
            prev_track = self.get_neighbor(*current_tile, backward_edge)

            if not prev_track:
                # No track behind - train extends off layout
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"No track behind {current_tile} via edge {backward_edge}."
                )
            prev_coords = (prev_track.x, prev_track.y)

            # Check connectivity
            opposite_edge = _OPPOSITE_EDGE[backward_edge]
            current_track = self.get_track(*current_tile)
            if not (
                backward_edge in current_track.edges
                and opposite_edge in prev_track.edges
            ):
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"Tiles {current_tile} and {prev_coords} are not connected."
                )

            if prev_coords in visited:
                # Circular track - train wraps around
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"Circular track detected at {prev_coords}."
                )

            visited.add(prev_coords)

            # Get length of previous tile
            prev_tile_length = self._get_tile_length_cm(*prev_coords)

            # Check if tail is in this previous tile
            remaining_length = train_length_cm - accumulated_length

            if remaining_length <= prev_tile_length:
                # Tail is in this tile. The train exited prev_tile through
                # opposite_edge to reach current_tile, so the entry edge is
                # whichever edge has opposite_edge as its exit per the tile's
                # rail topology — NOT the cardinal opposite, which is wrong
                # for corner tiles (NW/SE/NE/SW).
                if prev_coords in path_lookup:
                    tail_entry_edge = path_lookup[prev_coords]
                else:
                    tail_entry_edge = prev_track.get_entry_for_exit(
                        opposite_edge, use_switch_state=True
                    ) or opposite_edge
                tail_distance = prev_tile_length - remaining_length

                occupied.insert(0, prev_coords)

                return (occupied, prev_coords, tail_distance, tail_entry_edge)

            # Continue to next previous tile
            accumulated_length += prev_tile_length
            occupied.insert(0, prev_coords)

            # Update for next iteration
            current_tile = prev_coords

            # Determine entry edge for prev_tile
            # The train exited prev_tile through opposite_edge to reach current_tile.
            # We need to find: which edge did the train ENTER prev_tile from?
            #
            # For switches: use get_entry_for_exit() which respects switch state
            # For simple track: use get_entry_for_exit() as well (works for all tracks)
            #
            # First try path lookup (most accurate for switches with path info)
            if prev_coords in path_lookup:
                current_entry_edge = path_lookup[prev_coords]
            else:
                # Use the track's get_entry_for_exit method which respects switch state
                entry_edge = prev_track.get_entry_for_exit(
                    opposite_edge, use_switch_state=True
                )
                if entry_edge:
                    current_entry_edge = entry_edge
                else:
                    # Fallback: if no valid entry found (shouldn't happen for valid paths)
                    current_entry_edge = opposite_edge

        # Should never reach here
        raise ValueError(
            f"Unable to calculate tail position after {max_iterations} iterations"
        )

    def get_occupied_tiles_rich(
        self, span: "TrainSpan", head_is_a: bool
    ) -> List[Dict[str, Any]]:
        """
        Get rich information about all tiles occupied by a train.

        Returns detailed data for each tile the train occupies, useful for
        rendering train segments with correct proportions and block colors.

        Args:
            span: The train's `TrainSpan` (occupied_tiles ordered end_a -> end_b).
            head_is_a: True when the head end is end_a (cab "A"); False when it
                is end_b (cab "B"). Derived on the controller from
                (cab, current_span).

        Returns:
            List of dicts (ordered from end_a to end_b), each containing:
            - tile: (x, y) coordinates
            - block_id: Block ID if tile is in a block, else None
            - segment_start_cm: Distance from entry edge where train starts on this tile
            - segment_end_cm: Distance from entry edge where train ends on this tile
            - tile_length_cm: Total length of this tile
        """
        if span is None:
            return []

        occupied_tiles = span.occupied_tiles
        if not occupied_tiles:
            return []

        result = []
        num_tiles = len(occupied_tiles)

        # occupied_tiles is ordered from end_a to end_b. The head end is end_a
        # when head_is_a (cab "A"), else end_b.
        is_head_first = head_is_a
        head_distance = (span.end_a if head_is_a else span.end_b).distance_cm
        tail_distance = (span.end_b if head_is_a else span.end_a).distance_cm

        for i, tile in enumerate(occupied_tiles):
            tile_track = self.get_track(*tile)
            tile_length = self._get_tile_length_cm(*tile)

            block_id = tile_track.block_section_id if tile_track else None

            # Determine segment boundaries based on position in list
            is_first = i == 0
            is_last = i == num_tiles - 1

            if is_first and is_last:
                # Single tile - both ends on the same tile. The head/tail
                # in-tile offsets are symmetric here, so orientation does not
                # change the segment span.
                segment_start = tail_distance
                segment_end = head_distance
            elif is_first:
                # First tile in list (end_a's tile)
                if is_head_first:
                    # end_a is head - this is head tile
                    segment_start = 0.0
                    segment_end = head_distance
                else:
                    # end_a is tail - this is tail tile
                    segment_start = tail_distance
                    segment_end = tile_length
            elif is_last:
                # Last tile in list (end_b's tile)
                if is_head_first:
                    # end_b is tail - this is tail tile
                    segment_start = tail_distance
                    segment_end = tile_length
                else:
                    # end_b is head - this is head tile
                    segment_start = 0.0
                    segment_end = head_distance
            else:
                # Middle tile - train occupies entire tile
                segment_start = 0.0
                segment_end = tile_length

            result.append(
                {
                    "tile": tile,
                    "block_id": block_id,
                    "segment_start_cm": segment_start,
                    "segment_end_cm": segment_end,
                    "tile_length_cm": tile_length,
                }
            )

        return result

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
        """Forward to `BlockGeometryManager._disconnect_signals_for_block(block_id)`."""
        return self._get_block_geometry_manager()._disconnect_signals_for_block(block_id)

    def remove_tile_from_block(self, x: int, y: int) -> List[str]:
        """Forward to `BlockGeometryManager.remove_tile_from_block(x, y)`."""
        return self._get_block_geometry_manager().remove_tile_from_block(x, y)

    def remove_tiles_from_block(self, positions: List[Tuple[int, int]]) -> List[str]:
        """Forward to `BlockGeometryManager.remove_tiles_from_block(positions)`."""
        return self._get_block_geometry_manager().remove_tiles_from_block(positions)

    def _check_block_connectivity(self, block_id: str) -> List[str]:
        """Forward to `BlockGeometryManager._check_block_connectivity(block_id)`."""
        return self._get_block_geometry_manager()._check_block_connectivity(block_id)

    def _find_connected_components(self, *args, **kwargs):
        """Forward to `BlockGeometryManager._find_connected_components(...)`."""
        return self._get_block_geometry_manager()._find_connected_components(*args, **kwargs)

    def _get_track_connected_neighbors(self, *args, **kwargs):
        """Forward to `BlockGeometryManager._get_track_connected_neighbors(...)`."""
        return self._get_block_geometry_manager()._get_track_connected_neighbors(*args, **kwargs)

    def _create_child_blocks(self, *args, **kwargs):
        """Forward to `BlockGeometryManager._create_child_blocks(...)`."""
        return self._get_block_geometry_manager()._create_child_blocks(*args, **kwargs)

    def _remap_orphaned_signals(self, *args, **kwargs):
        """Forward to `BlockGeometryManager._remap_orphaned_signals(...)`."""
        return self._get_block_geometry_manager()._remap_orphaned_signals(*args, **kwargs)

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

    def _switch_at_tile(self, x: int, y: int) -> Optional["Switch"]:
        """Resolve the Switch entity anchored at a tile via the tile's
        `switch_id` back-reference, or None when the tile is not a switch."""
        track = self.get_track(x, y)
        if track is None or track.switch_id is None:
            return None
        return self.switches.get(track.switch_id)

    def compute_flank_protection_for(
        self, switch: "Switch"
    ) -> List[Tuple[str, int]]:
        """Compute the `(neighbour_switch_id, blocking_state)` flank list for
        one switch (design_new.md:683-687).

        Searches the network graph outward from the switch tile within
        `FLANK_PROTECTION_DISTANCE_CM` and, for every neighbour switch found
        within that radius, records the neighbour state whose active branch
        points back toward this switch — the `blocking_state` that would
        create a head-on flank approach. Acquisition later locks the
        neighbour to the safe state `1 - blocking_state` (design_new.md:990).

        The walk accumulates per-tile length via `_get_tile_length_cm`.
        Because switch tiles are ~0 cm and ordinary block tiles are
        `DEFAULT_TILE_LENGTH_CM` (50 cm), the small 5 cm budget captures
        directly track-connected neighbour switches (crossover throats,
        ladders) without crossing a full block — the doc's "nearby switches
        that require mutual locking".
        """
        origin = (switch.x, switch.y)
        budget = FLANK_PROTECTION_DISTANCE_CM
        # BFS over track connectivity, tracking accumulated cm at each tile.
        # Seed neighbours of the origin at distance 0 (we step *out* of the
        # zero-or-near-zero-length switch tile into its connected neighbours).
        visited: Set[Tuple[int, int]] = {origin}
        found: Dict[str, int] = {}
        queue: deque = deque()
        origin_track = self.get_track(*origin)
        if origin_track is None:
            return []
        # Sorted edge iteration: the first pop of a tile fixes its walk
        # distance for the budget prune, so visit order must not depend on
        # str-hash set order.
        for edge in sorted(origin_track.edges):
            neighbor_coords = self._get_neighbor_coords(origin[0], origin[1], edge)
            # `get_neighbor` also resolves tunnels; map back to coords.
            neighbor = self.get_neighbor(origin[0], origin[1], edge)
            if neighbor is None:
                continue
            ncoords = (neighbor.x, neighbor.y)
            if ncoords in visited:
                continue
            queue.append((ncoords, 0.0))

        while queue:
            (cx, cy), dist_so_far = queue.popleft()
            if (cx, cy) in visited:
                continue
            visited.add((cx, cy))

            neighbour_switch = self._switch_at_tile(cx, cy)
            if neighbour_switch is not None and neighbour_switch.switch_id != switch.switch_id:
                blocking_state = self._neighbour_blocking_state(
                    neighbour_switch, toward=origin
                )
                if blocking_state is not None:
                    # Keep the most-constraining (first found) entry per
                    # neighbour; a neighbour appears once in the list.
                    found.setdefault(neighbour_switch.switch_id, blocking_state)
                # A switch is a hazard boundary: do not search past it.
                continue

            # Extend the walk to this tile's neighbours while within budget.
            tile_len = self._get_tile_length_cm(cx, cy)
            next_dist = dist_so_far + tile_len
            if next_dist > budget:
                continue
            tile = self.get_track(cx, cy)
            if tile is None:
                continue
            for edge in sorted(tile.edges):
                nb = self.get_neighbor(cx, cy, edge)
                if nb is None:
                    continue
                ncoords = (nb.x, nb.y)
                if ncoords not in visited:
                    queue.append((ncoords, next_dist))

        return sorted(found.items())

    def _neighbour_blocking_state(
        self, neighbour: "Switch", *, toward: Tuple[int, int]
    ) -> Optional[int]:
        """Return the neighbour switch state (0/1) whose active branch points
        back toward the `toward` tile through the connecting edge — the
        `blocking_state` for flank protection (design_new.md:990).

        Returns None when the neighbour is not track-connected toward the
        origin, or when the connecting edge is active in BOTH states. The
        latter is the in-series-on-a-through-line case (e.g. a depot-throat
        pair where the connecting edge carries the main route in either
        position): the neighbour always points at the origin, so no setting
        of it provides flank protection — there is no safe state to lock — and
        recording an arbitrary one would be both meaningless and
        iteration-order-dependent. Flank protection is meaningful only when the
        neighbour points at the origin in exactly one state.
        """
        ncoords = (neighbour.x, neighbour.y)
        ntrack = self.get_track(*ncoords)
        if ntrack is None:
            return None
        # Which edge of the neighbour faces the walk that reached it. We look
        # for the connecting edge between the neighbour and any already-walked
        # tile in the direction of the origin; the immediate connection is the
        # edge whose neighbour tile is closer to `toward`. Use the connecting
        # edge to the tile we came from by scanning the neighbour's edges for
        # one whose far tile is on the path back to origin.
        facing_edge = self._edge_from_neighbour_toward(neighbour, toward)
        if facing_edge is None:
            return None
        # Collect the states in which an active branch is connected through the
        # facing edge — i.e. a train on the neighbour set to that state could
        # run out through that edge into the protected switch.
        # enumerate_traversals(entry) yields (exit_edge, required_state) per
        # legal traversal, so the facing edge is "active in state S" when it
        # appears as either the entry or the exit of a traversal legal in S.
        states_pointing: Set[int] = set()
        for entry in ntrack.edges:
            for exit_edge, required_state in ntrack.enumerate_traversals(entry):
                if required_state is None:
                    continue
                if entry == facing_edge or exit_edge == facing_edge:
                    states_pointing.add(int(required_state))
        # Exactly one state points at the origin -> that is the blocking state
        # (its binary complement is the safe state to lock). Both states (no
        # safe state) or neither (not a hazard) -> no flank relationship.
        if len(states_pointing) == 1:
            return next(iter(states_pointing))
        return None

    def _edge_from_neighbour_toward(
        self, neighbour: "Switch", toward: Tuple[int, int]
    ) -> Optional[str]:
        """The edge on the neighbour switch tile that connects, in one hop,
        toward the origin tile. Resolves the direct connection edge when the
        neighbour is adjacent to `toward`; otherwise the edge whose far tile
        is the nearest step back toward `toward` by Manhattan heuristic."""
        ncoords = (neighbour.x, neighbour.y)
        # Direct connection (adjacent or tunnel) to the origin.
        direct = self.get_connection_edges(ncoords, toward)
        if direct is not None:
            return direct[0]
        # Otherwise pick the connected edge whose far tile reduces Manhattan
        # distance to the origin the most (the step back along the walk).
        ntrack = self.get_track(*ncoords)
        if ntrack is None:
            return None
        best_edge: Optional[str] = None
        best_dist: Optional[int] = None
        for edge in sorted(ntrack.edges):
            nb = self.get_neighbor(ncoords[0], ncoords[1], edge)
            if nb is None:
                continue
            d = abs(nb.x - toward[0]) + abs(nb.y - toward[1])
            if best_dist is None or d < best_dist:
                best_dist = d
                best_edge = edge
        return best_edge

    def recompute_all_flank_protection(self) -> None:
        """Recompute `flank_protection` for every switch in the network.

        Called after any topology change that can alter switch adjacency
        (add/remove/update switch, bulk `from_dict` load). Cheap relative to
        the per-tick loop and run only on between-tick topology edits, which
        v1 forbids during simulation anyway (editor and runtime are binary
        modes per session)."""
        for switch in self.switches.values():
            switch.flank_protection = self.compute_flank_protection_for(switch)

    # ── Power districts (design_new.md "Power district") ───────────────────

    def district_block_ids(self, block_id: str) -> List[str]:
        """Forward to `PowerDistrictRegistry.district_block_ids(block_id)`."""
        return self._get_power_district_registry().district_block_ids(block_id)

    def create_power_district(
        self, district_id: str, block_ids: List[str], name: str = ""
    ) -> "PowerDistrict":
        """Forward to `PowerDistrictRegistry.create_power_district(...)`."""
        return self._get_power_district_registry().create_power_district(
            district_id, block_ids, name
        )

    def delete_power_district(self, district_id: str) -> bool:
        """Forward to `PowerDistrictRegistry.delete_power_district(district_id)`."""
        return self._get_power_district_registry().delete_power_district(district_id)

    def add_block_to_power_district(self, district_id: str, block_id: str) -> bool:
        """Forward to `PowerDistrictRegistry.add_block_to_power_district(...)`."""
        return self._get_power_district_registry().add_block_to_power_district(
            district_id, block_id
        )

    def remove_block_from_power_district(
        self, district_id: str, block_id: str
    ) -> bool:
        """Forward to `PowerDistrictRegistry.remove_block_from_power_district(...)`."""
        return self._get_power_district_registry().remove_block_from_power_district(
            district_id, block_id
        )

    def power_district_blocks_connected(self, block_ids: List[str]) -> bool:
        """Forward to `PowerDistrictRegistry.power_district_blocks_connected(...)`."""
        return self._get_power_district_registry().power_district_blocks_connected(
            block_ids
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
            "trains": [t.to_dict(self) for t in self.trains.values()],
            "blocks": {k: v.to_dict() for k, v in self.blocks.items()},
            "signals": {k: v.to_dict() for k, v in self.signals.items()},
            "platforms": [
                p.to_dict_with_orientation(self) for p in self.platforms.values()
            ],
            "station_groups": [s.to_dict() for s in self.station_groups.values()],
            "power_districts": [d.to_dict() for d in self.power_districts.values()],
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
            "trains": [t.to_dict(self) for t in self.trains.values()],
            "blocks": {k: v.to_layout_dict() for k, v in self.blocks.items()},
            "signals": {k: v.to_layout_dict() for k, v in self.signals.items()},
            "platforms": [
                p.to_dict_with_orientation(self) for p in self.platforms.values()
            ],
            "station_groups": [s.to_dict() for s in self.station_groups.values()],
            "power_districts": [d.to_dict() for d in self.power_districts.values()],
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
            track = TrackSegment.from_dict(t_data)
            self.add_track(track)

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
                    if ptrack and ptrack.block_section_id:
                        platform.block_assignments[(px, py)] = ptrack.block_section_id

        # Rebuild logical station groups (optional in legacy saves)
        for station_data in data.get("station_groups", []):
            station = Station.from_dict(station_data)
            self.station_groups[station.station_id] = station

        # Rebuild power districts (optional; absent in legacy saves = every
        # block is its own single-block district). Rebuild the reverse index
        # once all districts are loaded.
        for district_data in data.get("power_districts", []):
            district = PowerDistrict.from_dict(district_data)
            self.power_districts[district.district_id] = district
        self._get_power_district_registry()._rebuild_district_index()

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
