"""
Track Segment Module - 6-bit track_bits system

Single source of truth:
- track_bits (0-63) encodes the physical rail segments on a tile.
- dead_end_ports stores buffer stops that terminate at an edge.

Design notes:
- Follows OpenTTD-style tile geometry (45° isometric UI), so the six
  possible segments are the only physical shapes.
- Switch points are inferred when an edge has 2+ exits; switch_states
  stores the active exit index per edge.

Curve Detection (connection-based):
- A CURVE is defined by orientation group CHANGE between connected paths
  in adjacent tiles, NOT by the geometric shape of a single path.
- 4 orientation groups:
    - 'NS': North-South paths
    - 'EW': East-West paths
    - 'NE_SW': North-East and South-West paths (same diagonal)
    - 'NW_SE': North-West and South-East paths (same diagonal)
- Example: NW path connecting to EW path = CURVE (different groups)
- Example: NW path connecting to SE path = STRAIGHT (same group: NW_SE)
- Use is_curve_connection(exit_edge, neighbor_segment) to check.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from enums import TrackType, Direction

# === Track bit constants ===
TRACK_NS = 1  # 0b000001 - North to South
TRACK_EW = 2  # 0b000010 - East to West
TRACK_NE = 4  # 0b000100 - North to East
TRACK_NW = 8  # 0b001000 - North to West
TRACK_SE = 16  # 0b010000 - South to East
TRACK_SW = 32  # 0b100000 - South to West

# Edge masks - which bits touch each edge
EDGE_MASKS: Dict[str, int] = {"N": 13, "E": 22, "S": 49, "W": 42}

# Bit index -> edge pair
BIT_TO_EDGES: Dict[int, tuple] = {
    0: ("N", "S"),  # TRACK_NS
    1: ("E", "W"),  # TRACK_EW
    2: ("N", "E"),  # TRACK_NE
    3: ("N", "W"),  # TRACK_NW
    4: ("S", "E"),  # TRACK_SE
    5: ("S", "W"),  # TRACK_SW
}

# Edge pair -> bit index (supports either ordering)
EDGES_TO_BIT: Dict[tuple, int] = {
    ("N", "S"): 0,
    ("S", "N"): 0,
    ("E", "W"): 1,
    ("W", "E"): 1,
    ("N", "E"): 2,
    ("E", "N"): 2,
    ("N", "W"): 3,
    ("W", "N"): 3,
    ("S", "E"): 4,
    ("E", "S"): 4,
    ("S", "W"): 5,
    ("W", "S"): 5,
}

# Direction <-> edge helpers
EDGE_TO_DIR = {
    "N": Direction.NORTH,
    "E": Direction.EAST,
    "S": Direction.SOUTH,
    "W": Direction.WEST,
}
DIR_TO_EDGE = {v: k for k, v in EDGE_TO_DIR.items()}

# Orientation groups for curve detection
# A path is a CURVE when connected to a neighbor path in a DIFFERENT group.
# A path is STRAIGHT when connected to a neighbor path in the SAME group.
# Groups:
#   - 'NS': North-South paths
#   - 'EW': East-West paths
#   - 'NE_SW': North-East and South-West paths (same diagonal orientation)
#   - 'NW_SE': North-West and South-East paths (same diagonal orientation)
ORIENTATION_GROUPS = {
    frozenset({"N", "S"}): "NS",
    frozenset({"E", "W"}): "EW",
    frozenset({"N", "E"}): "NE_SW",
    frozenset({"S", "W"}): "NE_SW",
    frozenset({"N", "W"}): "NW_SE",
    frozenset({"S", "E"}): "NW_SE",
}

# Bit index -> orientation group
BIT_TO_GROUP = {
    0: "NS",  # TRACK_NS
    1: "EW",  # TRACK_EW
    2: "NE_SW",  # TRACK_NE
    3: "NW_SE",  # TRACK_NW
    4: "NW_SE",  # TRACK_SE (same group as NW)
    5: "NE_SW",  # TRACK_SW (same group as NE)
}

# Opposite edges for neighbor lookup
OPPOSITE_EDGE = {"N": "S", "S": "N", "E": "W", "W": "E"}

# Forbidden combinations (see Reference_6Bit_Track_System.md)
FORBIDDEN_TRACK_BITS = frozenset(
    {
        13,
        22,
        42,
        49,  # 3-way switches
        15,
        23,
        43,
        51,  # Double slips with 3-way
        29,
        30,
        45,
        46,
        53,
        54,
        57,
        58,  # Complex 4-path with 3-way
        31,
        47,
        55,
        59,
        61,
        62,  # 5-path
        63,  # 6-path (full)
        28,
        44,
        52,
        56,
        60,  # Multi-diagonal without through route
    }
)


def is_square_corner_switch(bits: int) -> bool:
    """True if a switch configuration makes a 90° "square corner".

    A square corner joins a *horizontal* segment (NW or SE — screen angle 0°)
    and a *vertical* segment (NE or SW — screen angle 90°) with NO 45° diagonal
    (NS or EW) to ease the turn. On the rotated isometric screen this renders
    as a right-angle T-bone: a connecting segment meets a switch branch at 90°
    (e.g. a platform stub stuck straight onto a perpendicular ladder), which is
    unintuitive. A turnout that carries a diagonal NS/EW bit eases the branch
    (the swap rides a 45° rail, like the scissors crossovers) and is allowed.

    Screen angles on the rotated isometric projection:
        NW, SE -> 0° (horizontal)   NE, SW -> 90° (vertical)   NS, EW -> 45°.
    Mirror of the client check in static/js/constants.js (isSquareCornerSwitch).
    """
    has_horizontal = bool(bits & (TRACK_NW | TRACK_SE))
    has_vertical = bool(bits & (TRACK_NE | TRACK_SW))
    has_diagonal = bool(bits & (TRACK_NS | TRACK_EW))
    return has_horizontal and has_vertical and not has_diagonal


def _normalize_edge(edge: str) -> Optional[str]:
    """Normalize edge tokens to single-letter form (N/E/S/W)."""
    if not edge:
        return None
    upper = edge.upper()
    if upper in {"N", "NORTH"}:
        return "N"
    if upper in {"E", "EAST"}:
        return "E"
    if upper in {"S", "SOUTH"}:
        return "S"
    if upper in {"W", "WEST"}:
        return "W"
    return None


@dataclass
class Tile:
    """
    Railway track segment represented by a 6-bit geometry.
    track_bits and dead_end_ports are the only persistent connection data.
    All other properties are derived.
    """

    x: int
    y: int

    # === SINGLE SOURCE OF TRUTH ===
    track_bits: int = 0  # 0-63 geometry bits
    dead_end_ports: Set[str] = field(default_factory=set)  # Buffer stops (N/E/S/W)

    # === SWITCH STATE ===
    # Per design_new.md "Tile role and traversal" + "Switch entity":
    # `Switch.position` is the canonical live switch alignment; `Tile`
    # does not store switch state. The `_switch_state_local` field below is a
    # fallback used only when this segment is not attached to a `RailwayNetwork`
    # (e.g. unit tests that construct a bare Tile without a network).
    # When the segment is attached, the `switch_state` property resolves
    # through `network.switches[switch_id].position`.
    _switch_state_local: int = 0

    # === FEATURES ===
    signal_ids: List[str] = field(
        default_factory=list
    )  # Max 2 signals per tile (one per direction)
    # station_id / segment_id are exposed as @property below; the
    # _-prefixed fields are local fallbacks used only when the track is not
    # attached to a RailwayNetwork (e.g. unit tests that construct a bare
    # Tile). When the track is attached, the network's reverse
    # indices (tile_to_platform_id / tile_to_segment_id) are the source of
    # truth and these locals stay None.
    _station_id_local: Optional[str] = None
    _segment_id_local: Optional[str] = None
    switch_id: Optional[str] = None  # Reference to Switch.switch_id (if is_switch)
    # Back-reference to the owning RailwayNetwork. Set by
    # RailwayNetwork.add_track. Used by the station_id / segment_id
    # properties to resolve membership through the network reverse indices.
    network: Optional[Any] = None

    # === OTHER PROPERTIES ===
    length: float = 1.0
    # Per design_new.md line 566, `occupied_by` and `reserved_by` are removed
    # live runtime fields on Tile in v1. Physical occupancy lives on
    # the owning resource (`Block.occupied_by` / `Switch.occupied_by`) and
    # reservations live on `Block.reserved_for_train` /
    # `Block.reserved_by_path`. The `occupied_by` / `reserved_by` reads on
    # `Tile` are derived `@property` shims defined below. The
    # `_local` fallbacks are used only when the segment is not attached to a
    # `RailwayNetwork` (e.g. unit tests with bare Tiles).
    _occupied_by_local: Optional[str] = None
    _reserved_by_local: Optional[str] = None

    # === PATH LOCKING (Dispatcher System) ===
    # Per design_new.md "Track-segment role and traversal", switch lock
    # ownership lives entirely on `Switch.locks` (keyed by
    # `(path_id, source_switch_id)`). `Tile` stores no lock state and
    # exposes no lock surface; callers resolve the owning `Switch` via
    # `switch_id` and read `Switch.locks` / `Switch.is_locked()` directly.
    # Note: pre-v1 also stored `path_direction` on Tile. Per
    # design_new.md line 566/941 it is a removed field; traversal direction
    # is owned by `Block.reserved_direction` (dispatcher reservation) and
    # `Path.resource_sequence[*].entry_edge` (per-resource frame data). Live
    # consumers read those instead of any tile-stored direction.

    # === DERIVED-GEOMETRY CACHES (not part of the entity's identity) ===
    # `edges` and `switch_points` are pure functions of `track_bits` (and, for
    # `edges`, `dead_end_ports`). Both are read millions of times per simulation
    # run while the geometry stays constant, so each property memoizes its result
    # against the inputs it derives from and recomputes only when those inputs
    # change. init=False / compare=False keeps them out of __init__, __eq__, and
    # __repr__ so they never affect construction or equality.
    _edges_cache: Any = field(default=None, init=False, repr=False, compare=False)
    _switch_points_cache: Any = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self):
        # Normalize dead-end inputs
        self.dead_end_ports = {
            e for e in (_normalize_edge(p) for p in self.dead_end_ports) if e
        }
        # Ensure local fallback switch state is valid (0 or 1)
        self._switch_state_local = 0 if self._switch_state_local == 0 else 1

    # ========== Station / block membership (network-derived) ==========
    # Per design_new.md: Tile does not store station, platform, or
    # block membership. The owning RailwayNetwork holds the canonical
    # tile -> platform_id and tile -> block_id reverse indices. These
    # property shims keep the existing readers/writers working: when the
    # track is attached to a network, reads/writes go through the network;
    # when not attached (bare construction in tests), reads/writes fall
    # back to a local field stored on the dataclass.

    @property
    def station_id(self) -> Optional[str]:
        if self.network is not None:
            try:
                return self.network.tile_to_platform_id(self.x, self.y)
            except AttributeError:
                pass
        return self._station_id_local

    @station_id.setter
    def station_id(self, value: Optional[str]) -> None:
        if self.network is not None:
            try:
                self.network._set_tile_platform(self.x, self.y, value)
                self._station_id_local = None
                return
            except AttributeError:
                pass
        self._station_id_local = value

    @property
    def segment_id(self) -> Optional[str]:
        """Id of the `Segment` this tile belongs to, or None.

        A tile belongs to a segment (design_new.md "segment"); the segment is
        grouped into a `Block` by `BlockGroupingManager`. This is the stored
        membership — `block_id` below is derived from it.
        """
        if self.network is not None:
            try:
                return self.network.tile_to_segment_id(self.x, self.y)
            except AttributeError:
                pass
        return self._segment_id_local

    @segment_id.setter
    def segment_id(self, value: Optional[str]) -> None:
        if self.network is not None:
            try:
                self.network._set_tile_segment(self.x, self.y, value)
                self._segment_id_local = None
                return
            except AttributeError:
                pass
        self._segment_id_local = value

    @property
    def block_id(self) -> Optional[str]:
        """Id of the `Block` that owns this tile, or None.

        Derived, never stored: the tile belongs to a `Segment` and the segment
        is grouped into a `Block`, so this resolves tile -> segment -> block
        through `RailwayNetwork.tile_to_block_id`. Read-only — block membership
        is decided by grouping the segment (`BlockGroupingManager`), not by
        assigning a tile.

        For a detached tile (no network) there is no grouping information at
        all, so this reports the local segment fallback: with nothing to group
        into, a segment is its own block.
        """
        if self.network is not None:
            try:
                return self.network.tile_to_block_id(self.x, self.y)
            except AttributeError:
                pass
        return self._segment_id_local

    # ========== Computed properties ==========
    @property
    def edges(self) -> Set[str]:
        """Edges that are connected by either track paths or dead-ends.

        Memoized against ``(track_bits, dead_end_ports)`` — the only inputs it
        derives from — and recomputed only when either changes. Returns a
        ``frozenset`` so the shared cached value can never be mutated in place
        by a caller; all consumers use it read-only (membership / iteration /
        len / set algebra).
        """
        bits = self.track_bits
        deads = self.dead_end_ports
        cache = self._edges_cache
        if cache is not None and cache[0] == bits and cache[1] == deads:
            return cache[2]
        connected = {edge for edge, mask in EDGE_MASKS.items() if bits & mask}
        result = frozenset(connected | deads)
        # Snapshot dead_end_ports (set(...)) so a later in-place mutation of the
        # live set is detected by the `cache[1] == deads` check above.
        self._edges_cache = (bits, set(deads), result)
        return result

    @property
    def num_paths(self) -> int:
        """Number of physical rail segments (popcount)."""
        return bin(self.track_bits).count("1")

    @property
    def switch_points(self) -> Dict[str, List[str]]:
        """Map of switch edges -> sorted exits (only edges with 2+ exits).

        Memoized against ``track_bits`` — its only input — and recomputed only
        when the bits change. Consumers use the result read-only (`is_switch`,
        membership, iteration), so the shared cached dict is safe to return.
        """
        bits = self.track_bits
        cache = self._switch_points_cache
        if cache is not None and cache[0] == bits:
            return cache[1]
        edge_exits: Dict[str, List[str]] = {e: [] for e in "NESW"}
        for bit in range(6):
            if bits & (1 << bit):
                e1, e2 = BIT_TO_EDGES[bit]
                edge_exits[e1].append(e2)
                edge_exits[e2].append(e1)
        result = {
            edge: sorted(exits) for edge, exits in edge_exits.items() if len(exits) >= 2
        }
        self._switch_points_cache = (bits, result)
        return result

    @property
    def is_switch(self) -> bool:
        """Returns True if tile has any switch points."""
        return len(self.switch_points) > 0

    @property
    def has_long_path(self) -> bool:
        """Returns True if tile has NS or EW segment (the 'main' route)."""
        return bool(self.track_bits & 0b000011)  # NS=1, EW=2

    @property
    def has_short_path(self) -> bool:
        """Returns True if tile has any diagonal segment (NE, NW, SE, SW)."""
        return bool(self.track_bits & 0b111100)  # NE=4, NW=8, SE=16, SW=32

    @property
    def is_diagonal_only_switch(self) -> bool:
        """Returns True if this is a switch with only diagonal paths.

        For diagonal-only switches, MAIN/DIVERGE uses clockwise ordering
        (N, E, S, W) instead of long/short path distinction.
        """
        return self.is_switch and not self.has_long_path

    @property
    def long_path_type(self) -> Optional[str]:
        """Returns 'NS', 'EW', or None."""
        if self.track_bits & 1:
            return "NS"
        if self.track_bits & 2:
            return "EW"
        return None

    @property
    def switch_state(self) -> int:
        """Active switch alignment for this tile (0=MAIN, 1=DIVERGE).

        Per design_new.md, `Switch.position` is canonical. When this segment
        is attached to a `RailwayNetwork` and has an owning `Switch`, this
        property resolves from `network.switches[switch_id].position`. The
        local fallback `_switch_state_local` is used only when not attached
        (e.g. unit tests with bare Tiles).
        """
        if (
            self.network is not None
            and self.switch_id is not None
            and hasattr(self.network, "switches")
        ):
            switch = self.network.switches.get(self.switch_id)
            if switch is not None:
                return int(switch.position)
        return self._switch_state_local

    @switch_state.setter
    def switch_state(self, value: int):
        """Setter shim for legacy callers.

        When attached to a network with an owning `Switch`, write through to
        `Switch.position` so the canonical home stays in sync. Otherwise
        update only the local fallback.
        """
        normalized = 0 if value == 0 else 1
        if (
            self.network is not None
            and self.switch_id is not None
            and hasattr(self.network, "switches")
        ):
            switch = self.network.switches.get(self.switch_id)
            if switch is not None:
                from enums import SwitchState
                switch.position = SwitchState(normalized)
                # Keep the local fallback in step for callers that read it
                # directly (rare; should go through the property).
                self._switch_state_local = normalized
                return
        self._switch_state_local = normalized

    # ========== Physical occupancy / reservation (network-derived) ==========
    # Per design_new.md, physical occupancy lives on `Block.occupied_by` /
    # `Switch.occupied_by`, and dispatcher reservations live on
    # `Block.reserved_for_train`. `Tile` exposes derived read shims
    # so the existing UI / API surface (which serialises track tiles) can
    # show per-tile occupancy without storing it.

    @property
    def occupied_by(self) -> Optional[str]:
        """Train ID currently occupying this tile, derived from the owning
        `Switch.occupied_by` (when the tile is a switch) or
        `Block.occupied_by` (when the tile is a block member). Returns
        `_occupied_by_local` when the segment is not attached to a
        `RailwayNetwork`.
        """
        if self.network is None:
            return self._occupied_by_local
        # Switch tiles: prefer the owning switch's physical occupancy.
        if self.switch_id is not None and hasattr(self.network, "switches"):
            switch = self.network.switches.get(self.switch_id)
            if switch is not None:
                return getattr(switch, "occupied_by", None)
        # Block tiles: read through the owning block.
        bid = self.block_id
        if bid is not None and hasattr(self.network, "blocks"):
            block = self.network.blocks.get(bid)
            if block is not None:
                return getattr(block, "occupied_by", None)
        return self._occupied_by_local

    @occupied_by.setter
    def occupied_by(self, value: Optional[str]) -> None:
        """Setter shim for legacy callers / deserialization.

        Writes only the detached-mode fallback. When attached, the canonical
        store is `Block.occupied_by` / `Switch.occupied_by`; live writers
        route through `BlockManager` / `SwitchManager`.
        """
        self._occupied_by_local = value

    @property
    def reserved_by(self) -> Optional[str]:
        """Train ID this tile is reserved for, derived from the owning
        block's `Block.reserved_for_train`. Returns `_reserved_by_local`
        when the segment is not attached to a `RailwayNetwork`.

        Switches do not carry a "reserved for train" concept; future
        authority on a switch is `Switch.locks` (multi-owner), read
        directly from the owning `Switch` entity.
        """
        if self.network is None:
            return self._reserved_by_local
        bid = self.block_id
        if bid is not None and hasattr(self.network, "blocks"):
            block = self.network.blocks.get(bid)
            if block is not None:
                return getattr(block, "reserved_for_train", None)
        return self._reserved_by_local

    @reserved_by.setter
    def reserved_by(self, value: Optional[str]) -> None:
        """Setter shim for legacy callers / deserialization.

        Writes only the detached-mode fallback. When attached, the canonical
        store is `Block.reserved_for_train`; live writers route through
        `BlockManager`.
        """
        self._reserved_by_local = value

    @property
    def switch_states(self) -> Dict[str, int]:
        """Per-entry-port switch states for display/debugging.

        Maps each switch point entry edge to its active exit.
        All switches share the same global state (MAIN=0, DIVERGE=1).
        """
        if not self.is_switch:
            return {}
        return {entry: self.switch_state for entry in self.switch_points.keys()}

    @property
    def track_type(self) -> TrackType:
        """Classify track geometry (feature-aware).

        NOTE: This is GEOMETRIC classification only. Whether a path is a
        "curve" in the connection sense (orientation group changes at tile
        boundary) must be determined using is_curve_connection() with the
        neighbor segment.

        Geometric types:
        - STRAIGHT: NS or EW path (axial paths)
        - DIAGONAL: NE, NW, SE, SW paths (diagonal paths)
        - SWITCH/DIAMOND/WYE etc: multi-path configurations

        Connection-based curve: Use is_curve_connection(exit_edge, neighbor)
        to determine if the path at an edge forms a curve with the neighbor.
        """
        if self.track_bits in FORBIDDEN_TRACK_BITS:
            return TrackType.FORBIDDEN
        if self.signal_ids:
            return TrackType.SIGNAL
        if self.station_id is not None:
            return TrackType.STATION

        n = self.num_paths
        if n == 0:
            return TrackType.EMPTY
        if n == 1:
            # Single path: STRAIGHT for axial (NS/EW), DIAGONAL for diagonal paths
            # Note: Whether this forms a CURVE depends on the neighbor connection,
            # not the path shape itself. Use is_curve_connection() to check.
            if self.track_bits in (TRACK_NS, TRACK_EW):
                return TrackType.STRAIGHT
            else:
                return TrackType.DIAGONAL
        if n == 2:
            if self.track_bits in (3, 24, 36):
                return TrackType.DIAMOND
            return TrackType.SWITCH
        if n == 3:
            if self.track_bits in (14, 21, 41, 50):
                return TrackType.WYE
            return TrackType.COMPLEX
        if n == 4:
            if self.track_bits in (27, 39):
                return TrackType.SCISSORS
            if self.track_bits == 60:
                return TrackType.DOUBLE_CROSS
            return TrackType.COMPLEX
        return TrackType.COMPLEX

    @property
    def is_valid(self) -> bool:
        """Return True if geometry is allowed."""
        return (
            self.track_bits not in FORBIDDEN_TRACK_BITS
            and not is_square_corner_switch(self.track_bits)
        )

    # ========== Geometry mutations ==========
    def _can_set_bits(self, new_bits: int) -> bool:
        # Forbid explicitly listed invalid geometries, plus "square corner"
        # switches (a switch branch 90° to a connecting segment with no 45°
        # diagonal to ease it — see is_square_corner_switch). Dead-ends are
        # derived and recomputed after updates, so they do not block changes.
        return (
            new_bits not in FORBIDDEN_TRACK_BITS
            and not is_square_corner_switch(new_bits)
        )

    def set_track_bits(self, bits: int) -> bool:
        """Set geometry directly; returns False if forbidden or conflicting."""
        if not self._can_set_bits(bits):
            return False
        self.track_bits = bits
        self._sync_switch_states()
        return True

    def add_track_bit(self, bit: int) -> bool:
        """Add a track segment bit. Returns False if it would create a forbidden state."""
        new_bits = self.track_bits | bit
        if not self._can_set_bits(new_bits):
            return False
        self.track_bits = new_bits
        self._sync_switch_states()
        return True

    def remove_track_bit(self, bit: int) -> None:
        """Remove a track segment bit."""
        self.track_bits &= ~bit
        self._sync_switch_states()

    def _sync_switch_states(self) -> None:
        """Reset switch_state to MAIN (0) when track_bits change.

        This ensures all switches on the tile are synchronized:
        - MAIN (0): All switches point to long paths (NS or EW) if available,
                    or form a crossing pattern
        - DIVERGE (1): All switches point to short paths (diagonals)

        Called automatically when track_bits are modified via:
        - set_track_bits()
        - add_track_bit()
        - remove_track_bit()
        - toggle_track_bit()
        """
        # Always reset to MAIN (0) when geometry changes
        # This ensures consistent initial state and synced multi-switch behavior
        self.switch_state = 0

    # === Switch helpers (global tile-level state) ===
    def set_switch_state(self, state: int) -> bool:
        """Set global switch state. 0=MAIN, 1=DIVERGE."""
        if not self.is_switch:
            return False
        self.switch_state = 0 if state == 0 else 1
        return True

    def propagate_update(self, network: "RailwayNetwork") -> None:
        """Update derived data after topology change.

        Called by network.propagate_from_tile() when:
        - This tile's track_bits changes
        - This tile's switch_state changes
        - A neighbor tile is added/removed/modified

        Updates:
        - dead_end_ports (from neighbor connectivity)
        """
        # Recompute dead-ends based on neighbor connectivity
        self._update_dead_ends(network)

    def _update_dead_ends(self, network: "RailwayNetwork") -> None:
        """Recompute dead-ends based on neighbor connectivity.

        An edge is a dead-end if:
        1. ``track_bits`` has a path connecting to that edge, AND
        2. Either:
           - No reachable neighbour (grid or tunnel) exists through that edge, OR
           - A neighbour exists but does not connect back through the edge that
             links to us. For a grid neighbour the connecting edge is the
             cardinal opposite; for a tunnel it is the recorded far edge,
             which can be any edge.

        This is called by ``propagate_update()`` when network topology changes.
        """
        self.dead_end_ports.clear()

        for edge, mask in EDGE_MASKS.items():
            if not (self.track_bits & mask):
                continue

            neighbor = network.get_neighbor(self.x, self.y, edge)
            if neighbor is None:
                self.dead_end_ports.add(edge)
                continue

            # ``get_connection_edges`` returns the actual edge pair that
            # links the two tiles — cardinal opposites for grid neighbours,
            # the registered tunnel pair otherwise. If our edge isn't the
            # local side of that pair, the neighbour doesn't connect back
            # through it, so this edge is a dead-end.
            pair = network.get_connection_edges(
                (self.x, self.y), (neighbor.x, neighbor.y)
            )
            if pair is None or pair[0] != edge:
                self.dead_end_ports.add(edge)

    # === Exit resolution ===
    def enumerate_traversals(
        self, entry_edge: str
    ) -> List[Tuple[str, Optional[int]]]:
        """Enumerate every legal geometric traversal through this tile.

        Per design_new.md "Track-segment role and traversal" — pure-geometry
        helper used by `PathDispatcher` path construction/extension and BFS
        switch derivation. Returns one `(exit_edge, required_switch_state)`
        pair per legal traversal; for non-switch tiles `required_switch_state`
        is `None`. For switch tiles, the value is the `Switch.position`
        (0=MAIN, 1=DIVERGE) that aligns the tile to use that traversal.

        This helper MUST NOT consult live switch alignment. Callers that
        need the live-state filtered view use `get_active_exits(...)` /
        `get_active_entry_for_exit(...)` with an explicit resolver.

        Y-shape switches: branch-to-branch traversals are NOT yielded; v1
        only allows one branch active at a time.
        """
        entry_norm = _normalize_edge(entry_edge)
        if not entry_norm:
            return []

        # Geometric exits via track_bits (no switch_state).
        results: List[Tuple[str, Optional[int]]] = []
        for bit in range(6):
            if not (self.track_bits & (1 << bit)):
                continue
            e1, e2 = BIT_TO_EDGES[bit]
            if entry_norm == e1:
                exit_edge = e2
            elif entry_norm == e2:
                exit_edge = e1
            else:
                continue

            if not self.is_switch:
                results.append((exit_edge, None))
                continue

            # On a switch tile, derive which Switch.position aligns this
            # bit. Bits 0 (NS) and 1 (EW) are the MAIN segments; bits 2-5
            # (NE/NW/SE/SW) are diverge segments. For diverge-only switches
            # (no MAIN bit set), the two diverge segments correspond to
            # position 0 and 1 in bit-order.
            has_main = (self.track_bits & 0b11) != 0
            if has_main:
                required_state = 0 if bit <= 1 else 1
            else:
                # Diverge-only: lowest bit wins MAIN (position 0), next wins
                # DIVERGE (position 1).
                diverge_bits_present = sorted(
                    b for b in range(2, 6) if self.track_bits & (1 << b)
                )
                if not diverge_bits_present:
                    continue
                if bit == diverge_bits_present[0]:
                    required_state = 0
                elif len(diverge_bits_present) >= 2 and bit == diverge_bits_present[1]:
                    required_state = 1
                else:
                    # Third+ diverge segment: branch-to-branch path on a
                    # Y-shape; v1 disallows. Skip.
                    continue
            results.append((exit_edge, required_state))

        # Stable order on (exit_edge, required_state) for deterministic
        # consumers.
        results.sort(key=lambda item: (item[0], item[1] if item[1] is not None else -1))
        return results

    def get_active_exits(
        self, entry_edge: str, resolver: Optional[Any] = None
    ) -> List[str]:
        """Return exit edges currently reachable from `entry_edge`.

        Per design_new.md "Track-segment role and traversal" — the live-state
        helper. For switch tiles, `resolver` is a callable or network-like
        object exposing `.switches[switch_id]` so the helper can read
        `Switch.position` to filter the geometric enumeration. When no
        resolver is supplied and the segment is attached to a network
        (`self.network`), the network's switch registry is used.

        Returns the same edges as the legacy `get_exits(...)` helper but
        sourced from the canonical `Switch.position`, not the legacy
        `Tile._switch_state` cache.
        """
        # Fix #10 (design_new.md line 617): disabled switches have no
        # traversable exits; check before delegating to get_exits.
        if (
            self.is_switch
            and self.network is not None
            and self.switch_id is not None
            and hasattr(self.network, "switches")
        ):
            switch = self.network.switches.get(self.switch_id)
            if switch is not None and switch.disabled:
                del resolver
                return []

        # The live-state filter still uses the same code path as `get_exits`
        # for the moment (which reads `self.switch_state`). After the
        # switch-state migration completes, this helper resolves through
        # the supplied resolver / network.switches[switch_id].position.
        # `resolver` is reserved for the post-migration call shape.
        del resolver  # unused until _switch_state is removed (see follow-up)
        return self.get_exits(entry_edge, use_switch_state=True)

    def get_active_entry_for_exit(
        self, exit_edge: str, resolver: Optional[Any] = None
    ) -> Optional[str]:
        """Return the live-state entry edge that yields `exit_edge`.

        Per design_new.md "Track-segment role and traversal" — the inverse
        of `get_active_exits(...)`. Used for backward tracing during tail
        movement. `resolver` is reserved for the post-migration call shape.
        """
        del resolver
        return self.get_entry_for_exit(exit_edge, use_switch_state=True)

    def get_exits(
        self,
        entry_edge: str,
        use_switch_state: bool = True,
        switch_state_override: Optional[int] = None,
    ) -> List[str]:
        """Return exit edges for a given entry edge.

        Uses global switch_state if this is a switch tile:
        - 0 (MAIN): only allows entry/exit via long path (NS or EW)
        - 1 (DIVERGE): only allows entry/exit via short path (diagonals)

        IMPORTANT: When use_switch_state=True and this is a switch, the entry edge
        must be part of the ACTIVE segment. If a train enters from an edge that's
        not part of the active segment, it's a derailment scenario (switch set against them).

        `switch_state_override` answers "what would the exits be at state N?"
        without touching live state. The pathfinder needs that question; writing
        `switch_state` to ask it would drive the real point motor, because the
        setter writes through to the canonical `Switch.position`.
        """
        effective_switch_state = (
            self.switch_state if switch_state_override is None else switch_state_override
        )
        entry_norm = _normalize_edge(entry_edge)
        if not entry_norm:
            return []

        # First, find all geometrically possible exits (ignoring switch state)
        all_exits: List[str] = []
        for bit in range(6):
            if self.track_bits & (1 << bit):
                e1, e2 = BIT_TO_EDGES[bit]
                if entry_norm == e1:
                    all_exits.append(e2)
                elif entry_norm == e2:
                    all_exits.append(e1)

        if not all_exits:
            return []

        # If not using switch state, return all possible exits
        if not use_switch_state:
            return sorted(all_exits)

        # Check if this is a switch using the proper property
        # (which checks for switch_points, not just edge count)
        if not self.is_switch:
            return sorted(all_exits)

        # Fix #10 (design_new.md line 617): disabled switches have no
        # traversable exits. Resolve the owning Switch entity from the network
        # registry; if `switch.disabled` is True return [] immediately.
        if (
            self.network is not None
            and self.switch_id is not None
            and hasattr(self.network, "switches")
        ):
            switch = self.network.switches.get(self.switch_id)
            if switch is not None and switch.disabled:
                return []

        # For switches: categorize exits by segment type (main vs diverge)
        # AND verify the entry edge is part of an active segment
        #
        # - Long segments: NS (bit 0), EW (bit 1) - these are the "main" routes
        # - Short segments: NE, NW, SE, SW (bits 2-5) - these are "diverging" routes

        main_exits = []  # Exits via NS or EW segments
        diverge_exits = []  # Exits via diagonal segments

        for exit_edge in all_exits:
            # Find which segment connects entry to this exit
            segment_key = (entry_norm, exit_edge)
            bit_idx = EDGES_TO_BIT.get(segment_key)
            if bit_idx is None:
                continue

            # Bits 0 (NS) and 1 (EW) are long/main paths
            # Bits 2-5 (NE, NW, SE, SW) are short/diverge paths
            if bit_idx <= 1:
                main_exits.append(exit_edge)
            else:
                diverge_exits.append(exit_edge)

        # Apply switch state to determine which exits are ACTIVE
        #
        # Special case: DIVERGE-ONLY SWITCHES
        # If there are NO main paths in the entire track (bits 0-1 not set),
        # this is a diverge-only switch like bits 12, 20, 24, 36, 40, 48.
        # These have two diagonal segments sharing one edge.
        #
        # For these switches:
        # - State 0 activates the FIRST diverge segment (by bit order)
        # - State 1 activates the SECOND diverge segment
        # - Entry edge must be part of the active segment, otherwise return []

        has_main_paths = (self.track_bits & 0b11) != 0  # bits 0 (NS) or 1 (EW)

        if not has_main_paths:
            # This is a diverge-only switch - find which segments exist
            # Get all diverge segments present in this track
            diverge_segments = []  # List of (bit_index, edge1, edge2)
            for bit in range(2, 6):  # bits 2-5 are diverge segments
                if self.track_bits & (1 << bit):
                    e1, e2 = BIT_TO_EDGES[bit]
                    diverge_segments.append((bit, e1, e2))

            if len(diverge_segments) >= 2:
                # Sort by bit index for consistent ordering
                diverge_segments.sort(key=lambda x: x[0])

                # Determine which segment is active based on switch state
                active_segment = (
                    diverge_segments[0]
                    if effective_switch_state == 0
                    else diverge_segments[1]
                )
                active_bit, active_e1, active_e2 = active_segment

                # Check if entry edge is part of the active segment
                if entry_norm not in (active_e1, active_e2):
                    # Entry edge is NOT part of the active segment - switch is set against us
                    return []

                # Entry is valid - return the other edge of the active segment
                if entry_norm == active_e1:
                    return [active_e2]
                else:
                    return [active_e1]

        # Standard switch logic: main vs diverge
        # MAIN (0): only main segment exits are valid - entry from diverge path is BLOCKED
        # DIVERGE (1): only diverge segment exits are valid - entry from main path is BLOCKED
        #
        # The key insight: if entering from the WRONG path type, return [] (blocked)
        if effective_switch_state == 0:
            # MAIN state: only allow exits via main (NS/EW) segments
            # If there are no main_exits, it means entry is from diverge path -> blocked
            if main_exits:
                return [main_exits[0]]
            else:
                # Entry is via diverge path, but switch is set to MAIN -> blocked
                return []
        else:
            # DIVERGE state: only allow exits via diverge (diagonal) segments
            # If there are no diverge_exits, it means entry is from main path -> blocked
            if diverge_exits:
                # If there are multiple diverge exits, pick the appropriate one
                if len(diverge_exits) >= 2:
                    return [sorted(diverge_exits)[1]]  # Second diverge path
                else:
                    return [diverge_exits[0]]  # Only one diverge path
            else:
                # Entry is via main path, but switch is set to DIVERGE -> blocked
                return []

    def get_exit_directions(
        self, entry_direction: Direction, use_switch_state: bool = True
    ) -> List[Direction]:
        """Return exit Directions for a given entry Direction."""
        entry_edge = DIR_TO_EDGE.get(entry_direction)
        if not entry_edge:
            return []
        edges = self.get_exits(entry_edge, use_switch_state=use_switch_state)
        return [EDGE_TO_DIR[e] for e in edges if e in EDGE_TO_DIR]

    def get_entry_for_exit(
        self, exit_edge: str, use_switch_state: bool = True
    ) -> Optional[str]:
        """Get the entry edge for a given exit edge (inverse of get_exits).

        For switches, this respects the switch state: given the exit edge,
        determine which entry edge would lead to that exit through the active path.

        This is used for backward tracing: if a train exited through edge X,
        which edge did it enter from?

        Args:
            exit_edge: The edge the train exited through (N/E/S/W)
            use_switch_state: If True, only consider active path based on switch state

        Returns:
            Entry edge (N/E/S/W) or None if no valid path exists
        """
        exit_norm = _normalize_edge(exit_edge)
        if not exit_norm:
            return None

        # Try each possible entry edge and see which one has exit_norm as its exit
        for candidate_entry in "NESW":
            if candidate_entry == exit_norm:
                continue  # Can't enter and exit same edge
            exits = self.get_exits(candidate_entry, use_switch_state=use_switch_state)
            if exit_norm in exits:
                return candidate_entry

        return None

    # === Curve detection (connection-based) ===
    def get_path_group(self, edge1: str, edge2: str) -> Optional[str]:
        """Get orientation group for a path between two edges."""
        e1, e2 = _normalize_edge(edge1), _normalize_edge(edge2)
        if not e1 or not e2:
            return None
        return ORIENTATION_GROUPS.get(frozenset({e1, e2}))

    def get_path_at_edge(self, edge: str) -> Optional[tuple]:
        """Get the path (as edge pair) that uses the given edge.

        Returns (edge, other_edge) or None if no path uses this edge.
        For switches, returns the active path based on switch state.
        """
        edge_norm = _normalize_edge(edge)
        if not edge_norm:
            return None
        exits = self.get_exits(edge_norm, use_switch_state=True)
        if exits:
            return (edge_norm, exits[0])
        return None

    def is_curve_connection(
        self, exit_edge: str, neighbor_segment: "Tile"
    ) -> bool:
        """Check if connection to neighbor at exit_edge is a curve.

        A curve occurs when the current path and the neighbor's connected path
        are in DIFFERENT orientation groups.

        Args:
            exit_edge: The edge where this segment connects to neighbor
            neighbor_segment: The neighbor Tile

        Returns:
            True if this is a curve connection (different orientation groups)
        """
        exit_norm = _normalize_edge(exit_edge)
        if not exit_norm:
            return False

        # Get the path in this segment that uses exit_edge
        current_path = self.get_path_at_edge(exit_norm)
        if not current_path:
            return False

        # Get neighbor's entry edge (opposite of our exit)
        neighbor_entry = OPPOSITE_EDGE.get(exit_norm)
        if not neighbor_entry:
            return False

        # Get the path in neighbor that uses the entry edge
        neighbor_path = neighbor_segment.get_path_at_edge(neighbor_entry)
        if not neighbor_path:
            return False

        # Compare orientation groups
        current_group = self.get_path_group(*current_path)
        neighbor_group = neighbor_segment.get_path_group(*neighbor_path)

        if not current_group or not neighbor_group:
            return False

        return current_group != neighbor_group

    # ========== Serialization ==========
    def to_dict(self) -> dict:
        """Convert tile to dictionary for the live frontend payload.

        Includes runtime state (the dispatcher-driven `switch_state`) for UI
        rendering. Switch lock state is not serialized on the tile; UI readers
        resolve the owning `Switch` (via `switch_id`) and read `Switch.locks`.
        Persistence does NOT use this path; see `to_layout_dict()` instead.
        """
        return {
            "x": self.x,
            "y": self.y,
            "track_bits": self.track_bits,
            "dead_end_ports": sorted(self.dead_end_ports),
            "switch_state": self.switch_state if self.is_switch else None,
            "signal_ids": self.signal_ids,
            "station_id": self.station_id,
            # `block_section_id` is the wire name for the owning BLOCK id (the
            # frontend indexes the `blocks` payload with it). `segment_id` is
            # the tile's stored geometry membership; the two differ only for a
            # block that groups several segments.
            "block_section_id": self.block_id,
            "segment_id": self.segment_id,
            "switch_id": self.switch_id,  # NEW: Reference to Switch entity
            "length": self.length,
            # Computed
            "type": self.track_type.value,
            "edges": sorted(self.edges),
            "switch_points": self.switch_points,
            "is_switch": self.is_switch,
            "has_long_path": self.has_long_path,
            "has_short_path": self.has_short_path,
            "is_diagonal_only_switch": self.is_diagonal_only_switch,
            "long_path_type": self.long_path_type,
            "num_paths": self.num_paths,
            "is_valid": self.is_valid,
            "switch_states": self.switch_states if self.is_switch else None,
        }

    def to_layout_dict(self) -> dict:
        """Layout-only serialization used by persistence.

        Per design_new.md Out of Scope, the live `switch_state` mirror is
        excluded (switch locks are never tile-stored). Computed/derived reads
        used by the frontend are also dropped — they are recomputed from
        `track_bits` on load.
        """
        return {
            "x": self.x,
            "y": self.y,
            "track_bits": self.track_bits,
            "dead_end_ports": sorted(self.dead_end_ports),
            "signal_ids": self.signal_ids,
            "station_id": self.station_id,
            # Persistence stores the tile's segment membership. Block grouping
            # is restored from each `Block`'s own `segment_ids`.
            "segment_id": self.segment_id,
            "switch_id": self.switch_id,
            "length": self.length,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Tile":
        """Reconstruct tile from a layout snapshot.

        Runtime fields (the dispatcher `switch_state` mirror) are not loaded
        from disk; they default to state 0 and the engine repopulates them.
        """
        tile = cls(
            x=data["x"],
            y=data["y"],
            track_bits=data.get("track_bits", 0),
            dead_end_ports=set(data.get("dead_end_ports", [])),
        )
        tile.signal_ids = data.get("signal_ids", [])
        tile.station_id = data.get("station_id")
        tile.segment_id = data.get("segment_id")
        tile.switch_id = data.get("switch_id")
        tile.length = data.get("length", 1.0)
        return tile

    # ========== Utility ==========
    def get_required_switch_state(
        self, entry_edge: str, exit_edge: str
    ) -> Optional[int]:
        """
        Determine the required switch state for a given entry→exit traversal.

        This is used by route pathfinding to determine which switch state is
        needed to traverse this switch tile in a particular direction.

        Args:
            entry_edge: Edge where the path enters (N, E, S, W)
            exit_edge: Edge where the path exits (N, E, S, W)

        Returns:
            0 (MAIN) or 1 (DIVERGE) if this is a switch and the path exists,
            None if not a switch or the path doesn't exist
        """
        if not self.is_switch:
            return None

        # Probe each state without writing it. `switch_state`'s setter writes
        # through to the canonical `Switch.position`, which is only ever written
        # by `SwitchManager`'s validate -> write -> notify -> throw workflow, so
        # a pathfinding query must never assign it — not even transiently.
        for state in [0, 1]:
            exits = self.get_exits(
                entry_edge, use_switch_state=True, switch_state_override=state
            )
            if exit_edge in exits:
                return state

        return None  # Path doesn't exist at any state
