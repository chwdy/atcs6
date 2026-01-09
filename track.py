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
from typing import Dict, List, Optional, Set
import warnings

from enums import TrackType, Direction, SwitchPosition

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
class TrackSegment:
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

    # === SWITCH STATE (global per tile) ===
    # 0 = MAIN (long path active), 1 = DIVERGE (short paths active)
    _switch_state: int = 0

    # === FEATURES ===
    signal_ids: List[str] = field(
        default_factory=list
    )  # Max 2 signals per tile (one per direction)
    station_id: Optional[str] = None  # Reference to Station.station_id
    block_section_id: Optional[str] = None

    # === OTHER PROPERTIES ===
    length: float = 1.0
    curve_radius: Optional[float] = None  # Minimum curve radius (None = straight track)
    occupied_by: Optional[str] = None
    reserved_by: Optional[str] = None
    wear_level: float = 0.0
    last_maintenance: Optional[str] = None
    maintenance_count: int = 0

    # === PATH LOCKING (Dispatcher System) ===
    # Note: "path" = dispatcher's reserved route; "route" = train's travel plan
    locked_by_path: Optional[str] = None  # Path ID that locked this switch
    locked_state: Optional[int] = None  # The state the switch is locked to (0 or 1)
    path_direction: Optional[str] = (
        None  # Direction the path is traveling at this tile (N/E/S/W)
    )

    def __post_init__(self):
        # Normalize dead-end inputs
        self.dead_end_ports = {
            e for e in (_normalize_edge(p) for p in self.dead_end_ports) if e
        }
        # Ensure switch state is valid (0 or 1)
        self._switch_state = 0 if self._switch_state == 0 else 1

    # ========== Computed properties ==========
    @property
    def edges(self) -> Set[str]:
        """Edges that are connected by either track paths or dead-ends."""
        connected = {
            edge for edge, mask in EDGE_MASKS.items() if self.track_bits & mask
        }
        return connected | self.dead_end_ports

    @property
    def num_paths(self) -> int:
        """Number of physical rail segments (popcount)."""
        return bin(self.track_bits).count("1")

    @property
    def switch_points(self) -> Dict[str, List[str]]:
        """Map of switch edges -> sorted exits (only edges with 2+ exits)."""
        edge_exits: Dict[str, List[str]] = {e: [] for e in "NESW"}
        for bit in range(6):
            if self.track_bits & (1 << bit):
                e1, e2 = BIT_TO_EDGES[bit]
                edge_exits[e1].append(e2)
                edge_exits[e2].append(e1)
        return {
            edge: sorted(exits) for edge, exits in edge_exits.items() if len(exits) >= 2
        }

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
        """Global switch state: 0=MAIN, 1=DIVERGE"""
        return self._switch_state

    @switch_state.setter
    def switch_state(self, value: int):
        self._switch_state = 0 if value == 0 else 1

    @property
    def switch_states(self) -> Dict[str, int]:
        """Per-entry-port switch states for display/debugging.

        Maps each switch point entry edge to its active exit.
        All switches share the same global state (MAIN=0, DIVERGE=1).
        """
        if not self.is_switch:
            return {}
        return {entry: self._switch_state for entry in self.switch_points.keys()}

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
    def connections(self) -> Dict[Direction, List[Direction]]:
        """Get possible exit directions from entry direction (respects switch state)."""
        conn: Dict[Direction, List[Direction]] = {}
        for d in Direction:
            exits = self.get_exit_directions(d)
            if exits:
                conn[d] = exits
        return conn

    @property
    def is_valid(self) -> bool:
        """Return True if geometry is allowed."""
        return self.track_bits not in FORBIDDEN_TRACK_BITS

    # ========== Geometry mutations ==========
    def _can_set_bits(self, new_bits: int) -> bool:
        # Only forbid explicitly listed invalid geometries.
        # Dead-ends are derived and will be recomputed after updates,
        # so they should not block geometry changes.
        return new_bits not in FORBIDDEN_TRACK_BITS

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

    def toggle_track_bit(self, bit: int) -> bool:
        """Toggle a track segment. Returns False if toggling on would be forbidden."""
        if self.track_bits & bit:
            self.remove_track_bit(bit)
            return True
        return self.add_track_bit(bit)

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
        self._switch_state = 0

    # === Dead-ends ===
    def remove_dead_end(self, edge: str) -> None:
        norm = _normalize_edge(edge)
        if norm:
            self.dead_end_ports.discard(norm)

    def has_dead_end(self, edge: str) -> bool:
        norm = _normalize_edge(edge)
        return bool(norm and norm in self.dead_end_ports)

    # === Switch helpers (global tile-level state) ===
    def toggle_switch(self) -> bool:
        """Toggle global switch state between MAIN (0) and DIVERGE (1)."""
        if not self.is_switch:
            return False
        self._switch_state = 1 - self._switch_state
        return True

    def set_switch_state(self, state: int) -> bool:
        """Set global switch state. 0=MAIN, 1=DIVERGE."""
        if not self.is_switch:
            return False
        self._switch_state = 0 if state == 0 else 1
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
        1. track_bits has a path connecting to that edge, AND
        2. Either:
           - No neighbor tile exists in that direction, OR
           - Neighbor exists but has no path connecting back

        This is called by propagate_update() when network topology changes.
        """
        self.dead_end_ports.clear()

        # Check each edge
        for edge, mask in EDGE_MASKS.items():
            # Does this tile have a path to this edge?
            if not (self.track_bits & mask):
                continue  # No path on this edge

            # Check neighbor connectivity
            neighbor = network.get_neighbor(self.x, self.y, edge)

            if neighbor is None:
                # No neighbor tile - this is a dead-end
                self.dead_end_ports.add(edge)
            else:
                # Neighbor exists - check if it connects back
                opposite = OPPOSITE_EDGE[edge]

                # Does neighbor have a path on the opposite edge?
                if not (neighbor.track_bits & EDGE_MASKS[opposite]):
                    # Neighbor has no path connecting back - dead-end
                    self.dead_end_ports.add(edge)

    # === Exit resolution ===
    def get_exits(self, entry_edge: str, use_switch_state: bool = True) -> List[str]:
        """Return exit edges for a given entry edge.

        Uses global switch_state if this is a switch tile:
        - 0 (MAIN): only allows entry/exit via long path (NS or EW)
        - 1 (DIVERGE): only allows entry/exit via short path (diagonals)

        IMPORTANT: When use_switch_state=True and this is a switch, the entry edge
        must be part of the ACTIVE segment. If a train enters from an edge that's
        not part of the active segment, it's a derailment scenario (switch set against them).
        """
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
                    if self._switch_state == 0
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
        if self._switch_state == 0:
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

    def get_active_exit(self, entry_edge: str) -> Optional[str]:
        exits = self.get_exits(entry_edge, use_switch_state=True)
        return exits[0] if exits else None

    # === Traversal helpers ===
    def get_traversal_group(
        self, entry_direction: Direction, exit_direction: Direction
    ) -> Optional[str]:
        """Return orientation group for a traversal through this tile."""
        entry_edge = DIR_TO_EDGE.get(entry_direction)
        exit_edge = DIR_TO_EDGE.get(exit_direction)
        if not entry_edge or not exit_edge:
            return None
        return ORIENTATION_GROUPS.get(frozenset({entry_edge, exit_edge}))

    def is_turn(self, entry_direction: Direction, exit_direction: Direction) -> bool:
        """True if traversal uses a diagonal group (i.e., orientation changes)."""
        group = self.get_traversal_group(entry_direction, exit_direction)
        return group in {"NE_SW", "NW_SE"}

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
        self, exit_edge: str, neighbor_segment: "TrackSegment"
    ) -> bool:
        """Check if connection to neighbor at exit_edge is a curve.

        A curve occurs when the current path and the neighbor's connected path
        are in DIFFERENT orientation groups.

        Args:
            exit_edge: The edge where this segment connects to neighbor
            neighbor_segment: The neighbor TrackSegment

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

    def get_curve_type_at_edge(
        self, exit_edge: str, neighbor_segment: "TrackSegment"
    ) -> str:
        """Get the curve type for connection at the given edge.

        Returns:
            'STRAIGHT' if both paths are in the same orientation group
            'CURVE' if paths are in different orientation groups
            'UNKNOWN' if cannot determine (missing path data)
        """
        if self.is_curve_connection(exit_edge, neighbor_segment):
            return "CURVE"

        exit_norm = _normalize_edge(exit_edge)
        if exit_norm:
            current_path = self.get_path_at_edge(exit_norm)
            neighbor_entry = OPPOSITE_EDGE.get(exit_norm)
            if neighbor_entry:
                neighbor_path = neighbor_segment.get_path_at_edge(neighbor_entry)
                if current_path and neighbor_path:
                    return "STRAIGHT"

        return "UNKNOWN"

    # === Path helpers (directed-path API) ===
    def remove_path(self, port1: str, port2: str) -> bool:
        e1, e2 = _normalize_edge(port1), _normalize_edge(port2)
        if not e1 or not e2:
            return False
        bit_idx = EDGES_TO_BIT.get((e1, e2))
        if bit_idx is None:
            return False
        before = self.track_bits
        self.remove_track_bit(1 << bit_idx)
        return before != self.track_bits

    def has_path(self, port1: str, port2: str) -> bool:
        e1, e2 = _normalize_edge(port1), _normalize_edge(port2)
        if not e1 or not e2:
            return False
        bit_idx = EDGES_TO_BIT.get((e1, e2))
        if bit_idx is None:
            return False
        return bool(self.track_bits & (1 << bit_idx))

    # ========== Serialization ==========
    def to_dict(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "track_bits": self.track_bits,
            "dead_end_ports": sorted(self.dead_end_ports),
            "switch_state": self._switch_state if self.is_switch else None,
            "signal_ids": self.signal_ids,
            "station_id": self.station_id,
            "block_section_id": self.block_section_id,
            "length": self.length,
            # Path locking
            "locked_by_path": self.locked_by_path,
            "locked_state": self.locked_state,
            "path_direction": self.path_direction,
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
            "is_locked": self.is_locked_by_path(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackSegment":
        segment = cls(
            x=data["x"],
            y=data["y"],
            track_bits=data.get("track_bits", 0),
            dead_end_ports=set(data.get("dead_end_ports", [])),
            _switch_state=data.get("switch_state", 0) or 0,
        )
        # Load signal_ids list
        segment.signal_ids = data.get("signal_ids", [])
        segment.station_id = data.get("station_id")
        segment.block_section_id = data.get("block_section_id")
        segment.length = data.get("length", 1.0)
        # Path locking
        segment.locked_by_path = data.get("locked_by_path")
        segment.locked_state = data.get("locked_state")
        return segment

    # ========== Utility ==========
    def get_display_symbol(self) -> str:
        """Basic ASCII symbol for console visualization using track_bits."""
        if self.track_type == TrackType.STATION:
            return "🚉"
        if self.track_type == TrackType.SIGNAL:
            return "🚦"
        if self.track_type == TrackType.FORBIDDEN:
            return "✗"

        # Map track_bits to symbols (single segments)
        bit_symbols = {
            0: "•",  # Empty
            1: "│",  # NS
            2: "─",  # EW
            4: "╰",  # NE
            8: "╯",  # NW
            16: "╭",  # SE
            32: "╮",  # SW
        }

        # Common multi-segment combinations
        if self.track_bits == 3:  # NS + EW
            return "┼"
        if self.track_bits in bit_symbols:
            return bit_symbols[self.track_bits]

        # Multi-segment: show path count
        n = self.num_paths
        if n == 2:
            return "⊕"  # Junction
        if n >= 3:
            return "✦"  # Complex
        return "?"

    def degrade(self, amount: float):
        self.wear_level = min(1.0, self.wear_level + amount)

    def repair(self):
        self.wear_level = 0.0
        self.maintenance_count += 1
        from datetime import datetime

        self.last_maintenance = datetime.now().isoformat()

    def is_occupied(self) -> bool:
        return self.occupied_by is not None

    def is_reserved(self) -> bool:
        return self.reserved_by is not None

    def is_available_for(self, train_id: str) -> bool:
        if self.occupied_by and self.occupied_by != train_id:
            return False
        if self.reserved_by and self.reserved_by != train_id:
            return False
        return True

    # ========================================================================
    # Path Locking Methods (Dispatcher System)
    # ========================================================================

    def lock_for_path(self, path_id: str, state: int) -> bool:
        """
        Lock this switch in a specific state for a path.

        Only switches can be locked. The switch will be set to the specified
        state and locked so it cannot be changed until unlocked.

        Note: "path" = dispatcher's reserved route; "route" = train's travel plan

        Args:
            path_id: The path ID locking this switch
            state: The switch state to lock to (0=MAIN, 1=DIVERGE)

        Returns:
            True if lock successful, False if already locked by different path
        """
        if not self.is_switch:
            return False  # Only switches can be locked

        # Already locked by same path in same state - OK
        if self.locked_by_path == path_id and self.locked_state == state:
            return True

        # Already locked by different path
        if self.locked_by_path is not None and self.locked_by_path != path_id:
            return False

        # Lock the switch
        self.set_switch_state(state)
        self.locked_by_path = path_id
        self.locked_state = state
        return True

    def unlock_for_path(self, path_id: str) -> bool:
        """
        Unlock this switch if it was locked by the specified path.

        Args:
            path_id: The path ID that should own the lock

        Returns:
            True if unlocked, False if not locked by this path
        """
        if self.locked_by_path == path_id:
            self.locked_by_path = None
            self.locked_state = None
            return True
        return False

    def is_locked_by_path(self) -> bool:
        """Check if this switch is locked by any path."""
        return self.locked_by_path is not None

    def is_locked_by(self, path_id: str) -> bool:
        """Check if this switch is locked by a specific path."""
        return self.locked_by_path == path_id

    def get_lock_info(self) -> Optional[Dict[str, any]]:
        """
        Get information about the current lock.

        Returns:
            Dictionary with lock info, or None if not locked
        """
        if not self.locked_by_path:
            return None
        return {
            "path_id": self.locked_by_path,
            "locked_state": self.locked_state,
            "position": (self.x, self.y),
        }

    def can_lock_for_path(self, path_id: str, state: int) -> bool:
        """
        Check if this switch can be locked for a path without actually locking.

        Args:
            path_id: Path ID that would lock
            state: State that would be locked to

        Returns:
            True if locking would succeed
        """
        if not self.is_switch:
            return False

        # Not locked - can lock
        if self.locked_by_path is None:
            return True

        # Already locked by same path in same state
        if self.locked_by_path == path_id and self.locked_state == state:
            return True

        # Already locked by same path in different state - conflict
        if self.locked_by_path == path_id and self.locked_state != state:
            return False

        # Locked by different path
        return False

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

        # Check if this entry→exit path exists at each state
        for state in [0, 1]:
            old_state = self.switch_state
            self.switch_state = state
            exits = self.get_exits(entry_edge, use_switch_state=True)
            self.switch_state = old_state

            if exit_edge in exits:
                return state

        return None  # Path doesn't exist at any state
