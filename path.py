"""
Path entity (dispatcher-owned authority record).

Per design_new.md "Path field schema", a Path is the dispatcher-owned
record of a single active authority for one train. It is born ACTIVE on a
successful PathDispatcher.create_path(...) and transitions to COMPLETED or
CANCELLED. Resource geometry is stored once in resource_sequence (the
single source of truth) plus a derived flat path_tiles geometry. There is
no v0 block_sequence / start_block_id / end_block_id / current_block_index;
authority extents are described by requested_destination_block_id +
requested_destination_direction (the caller's target) and
authority_end_block_id (the active-authority end).

See also:
- design_new.md "## Path And PathDispatcher" (Path field schema, path_tile
  entry schema, resource_sequence entry schema, status state machine).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from enums import PathStatus


# ============================================================================
# Path geometry building blocks
# ============================================================================


@dataclass
class PathTileEntry:
    """One canonical tile step inside Path.path_tiles or a
    resource_sequence entry's path_tiles slice.

    Carries the directed traversal frame (entry_edge / exit_edge) and the
    parent resource's sequence_index so consumers do not need to recover
    direction from neighbouring coordinates.
    """

    x: int
    y: int
    entry_edge: str  # "N" / "E" / "S" / "W"
    exit_edge: str  # "N" / "E" / "S" / "W"
    resource_sequence_index: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "entry_edge": self.entry_edge,
            "exit_edge": self.exit_edge,
            "resource_sequence_index": self.resource_sequence_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PathTileEntry":
        return cls(
            x=int(data["x"]),
            y=int(data["y"]),
            entry_edge=str(data["entry_edge"]),
            exit_edge=str(data["exit_edge"]),
            resource_sequence_index=int(data["resource_sequence_index"]),
        )


@dataclass
class ResourceSequenceEntry:
    """One entry in Path.resource_sequence.

    resource_sequence is the single source of truth for path geometry: an
    ordered list of block/switch resources from train tail to authority
    end. Each entry carries the contiguous tile slice that belongs to that
    resource plus the resource-level traversal frame.
    """

    sequence_index: int
    resource_type: str  # "block" or "switch"
    resource_id: str
    path_tiles: List[PathTileEntry] = field(default_factory=list)
    entry_edge: str = ""
    exit_edge: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence_index": self.sequence_index,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "path_tiles": [t.to_dict() for t in self.path_tiles],
            "entry_edge": self.entry_edge,
            "exit_edge": self.exit_edge,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceSequenceEntry":
        return cls(
            sequence_index=int(data["sequence_index"]),
            resource_type=str(data["resource_type"]),
            resource_id=str(data["resource_id"]),
            path_tiles=[
                PathTileEntry.from_dict(t) for t in data.get("path_tiles", [])
            ],
            entry_edge=str(data.get("entry_edge", "")),
            exit_edge=str(data.get("exit_edge", "")),
        )

    def resolve(self, network) -> Optional[Any]:
        """Return the live `TrackResource` (Block or Switch) for this entry,
        or None if the resource_id is not registered on the network.

        ResourceSequenceEntry stays the source of truth for path-local
        geometry (`path_tiles`, `entry_edge`, `exit_edge`); this helper is a
        pure registry read so callers that want polymorphic access to the
        underlying entity do not have to duplicate the
        `network.blocks.get(...)` / `network.switches.get(...)` branch.
        """
        if self.resource_type == "block":
            return network.blocks.get(self.resource_id)
        if self.resource_type == "switch":
            return network.switches.get(self.resource_id)
        return None


# ============================================================================
# Path
# ============================================================================


@dataclass
class Path:
    """Dispatcher-owned authority record for one live route through the
    network.

    A Path is born ACTIVE inside PathDispatcher.create_path(...) and
    transitions to COMPLETED or CANCELLED. resource_sequence holds the
    full authority geometry (blocks + switches in traversal order); the
    flat path_tiles list is the concatenation of the per-resource tile
    slices in sequence_index order. authority_end_block_id is the live
    authority end and may be shorter than the caller's
    requested_destination_block_id when best_effort acquisition truncated
    the path.
    """

    # Identity
    path_id: str

    # Lifecycle
    status: PathStatus = PathStatus.ACTIVE

    # Train ownership (mandatory in v1; an authority always belongs to a
    # specific train).
    train_id: Optional[str] = None

    # Caller-supplied target contract. requested_destination_direction is
    # always the resolved block-relative direction ("forward" or
    # "reverse"); RouteExecutor must already have resolved any "either" to
    # one concrete direction before calling dispatcher.
    requested_destination_block_id: str = ""
    requested_destination_direction: str = ""

    # Geometry. resource_sequence is the single source of truth; the flat
    # tile list is derived by the `get_path_tiles()` method, which
    # concatenates resource_sequence[*].path_tiles live on every call. There
    # is no stored copy.
    resource_sequence: List[ResourceSequenceEntry] = field(default_factory=list)

    # Head-based progress through resource_sequence. Stores the
    # sequence_index of the current path-progress anchor resource, not the
    # first matching raw resource ID.
    current_resource_index: int = 0

    # System-time Unix timestamp (seconds) of the most recent status
    # change. Set on creation and on every transition.
    last_status_updated_time: float = 0.0

    # ========================================================================
    # Status helpers
    # ========================================================================

    def _set_status(self, new_status: PathStatus, when: float) -> None:
        """Single writer for status + last_status_updated_time. Use this
        from every site that mutates status so the timestamp invariant is
        always preserved."""
        self.status = new_status
        self.last_status_updated_time = when

    def get_path_tiles(self) -> List[PathTileEntry]:
        """Derived ordered tile list — `resource_sequence[*].path_tiles`
        concatenated in `sequence_index` order. Per design_new.md "Path
        field schema": exposed as a function (not a property) — computed
        live on every call; not cached; no writers; no rebuild step. The
        function name signals "this is work, not a stored read."
        """
        tiles: List[PathTileEntry] = []
        for entry in self.resource_sequence:
            tiles.extend(entry.path_tiles)
        return tiles

    def get_authority_end_block_id(self) -> str:
        """Derived active authority end block.

        Per design_new.md "Path field schema": exposed as a function (not a
        property) returning `resource_sequence[-1].resource_id` on each
        call. Returns "" when the sequence is empty (e.g. before initial
        construction has populated it). Path invariants require the terminal
        entry to be a `block` resource; switch-terminated authority is not
        valid in v1 (see "Path invariants").
        """
        if not self.resource_sequence:
            return ""
        terminal = self.resource_sequence[-1]
        if getattr(terminal, "resource_type", None) != "block":
            return ""
        return str(getattr(terminal, "resource_id", "") or "")

    def is_active(self) -> bool:
        return self.status == PathStatus.ACTIVE

    def can_be_extended(self) -> bool:
        """Only an ACTIVE path may be extended in v1."""
        return self.status == PathStatus.ACTIVE

    # ========================================================================
    # Geometry queries
    # ========================================================================

    def get_all_blocks(self) -> List[str]:
        """All block resource_ids on this path in traversal order."""
        return [
            entry.resource_id
            for entry in self.resource_sequence
            if entry.resource_type == "block"
        ]

    # ========================================================================
    # Tile-level direction queries
    # ========================================================================

    def get_tile_directions(self) -> Dict[str, str]:
        """Map "x,y" -> exit_edge for every tile on the path.

        Each PathTileEntry already carries its own exit_edge; this helper
        is a convenience projection for UI/serialization consumers that
        previously inferred direction from neighbouring tile coordinates.
        """
        result: Dict[str, str] = {}
        for tile in self.get_path_tiles():
            key = f"{tile.x},{tile.y}"
            result[key] = tile.exit_edge
        return result

    def get_exit_direction(self) -> Optional[str]:
        """Direction the path is heading at its end (the last tile's
        exit_edge). None if the path has no tiles."""
        tiles = self.get_path_tiles()
        if not tiles:
            return None
        return tiles[-1].exit_edge or None

    def get_last_tile_entry_edge(self) -> Optional[str]:
        """Edge through which the path enters its last tile. Equal to the
        last tile's entry_edge."""
        tiles = self.get_path_tiles()
        if not tiles:
            return None
        return tiles[-1].entry_edge or None

    # ========================================================================
    # Serialization
    # ========================================================================

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path_id": self.path_id,
            "status": self.status.value,
            "train_id": self.train_id,
            "requested_destination_block_id": self.requested_destination_block_id,
            "requested_destination_direction": self.requested_destination_direction,
            "authority_end_block_id": self.get_authority_end_block_id(),
            "resource_sequence": [e.to_dict() for e in self.resource_sequence],
            "path_tiles": [t.to_dict() for t in self.get_path_tiles()],
            "current_resource_index": self.current_resource_index,
            "last_status_updated_time": self.last_status_updated_time,
            # Convenience projections for UI consumers.
            "tile_directions": self.get_tile_directions(),
            "exit_direction": self.get_exit_direction(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Path":
        return cls(
            path_id=str(data["path_id"]),
            status=PathStatus(data.get("status", PathStatus.ACTIVE.value)),
            train_id=data.get("train_id"),
            requested_destination_block_id=str(
                data.get("requested_destination_block_id", "")
            ),
            requested_destination_direction=str(
                data.get("requested_destination_direction", "")
            ),
            # `authority_end_block_id` and `path_tiles` are both derived from
            # `resource_sequence`; their serialized values are ignored on read.
            resource_sequence=[
                ResourceSequenceEntry.from_dict(e)
                for e in data.get("resource_sequence", [])
            ],
            current_resource_index=int(data.get("current_resource_index", 0)),
            last_status_updated_time=float(data.get("last_status_updated_time", 0.0)),
        )
