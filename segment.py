"""
Segment — track geometry unit.

Definition (design_v2.md "Segment", Segment model):
A `Segment` is a group of tiles with one unified geometry. It owns
`ordered_tiles`, `length_cm`, `speed_limit_kmh`, `min_radius_cm`, and
`grade_percent`. A segment stores NO occupancy, claim, availability, or
power state; those belong to its owning `Block`. One segment maps to many
real-world rail pieces that share its geometry.

Key concepts:
- Tiles reference their segment via `track.segment_id`.
- Physical properties are set at the segment level, not per-tile.
- `Segment` is a pure data container; geometry edits go through
  `SegmentGeometryManager`, and its owning `Block` reads its geometry.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional


class SegmentTileEntry(NamedTuple):
    """One tile entry in `Segment.ordered_tiles`.

    Each entry carries `(x, y, entry_edge)`, where `entry_edge` is the external
    edge through which forward traversal enters that tile.
    `Segment.ordered_tiles[0].entry_edge` is the canonical forward-entry anchor
    for the segment.

    Defined as a NamedTuple so code that unpacks `(x, y, edge)` keeps working
    while typed callers can reach `.x`, `.y`, `.entry_edge` by name.
    """

    x: int
    y: int
    entry_edge: str


@dataclass
class Segment:
    """A group of tiles with one unified geometry (design_v2.md "Segment").

    Owns geometry only. Occupancy, claim, availability, and power belong to
    the owning `Block`.
    """

    segment_id: str  # Unique ID (e.g., "SEG_001")
    name: str = ""  # Human-readable name

    # Physical properties (set on SEGMENT, not tiles)
    length_cm: float = 0.0  # Total track length in cm
    min_radius_cm: Optional[float] = None  # Tightest curve radius
    grade_percent: Optional[float] = None  # Track gradient as percent (+ uphill / - downhill)
    speed_limit_kmh: Optional[float] = None  # Speed limit for segment

    # Canonical local traversal order. Each entry is a `SegmentTileEntry`
    # (x, y, entry_edge); `ordered_tiles[0].entry_edge` is the forward-entry
    # anchor for the segment.
    ordered_tiles: List[SegmentTileEntry] = field(default_factory=list)

    # ========================================================================
    # Serialization
    # ========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert segment to dictionary for the live frontend payload."""
        return {
            "segment_id": self.segment_id,
            "name": self.name,
            "length_cm": self.length_cm,
            "min_radius_cm": self.min_radius_cm,
            "grade_percent": self.grade_percent,
            "speed_limit_kmh": self.speed_limit_kmh,
            "ordered_tiles": [
                [int(x), int(y), str(edge)] for (x, y, edge) in self.ordered_tiles
            ],
        }

    def to_layout_dict(self) -> Dict[str, Any]:
        """Layout-only serialization used by persistence.

        Per design_v2.md "Segment", A segment is geometry only, this writes
        segment geometry only. There is no runtime state on a segment to
        exclude.
        """
        return {
            "segment_id": self.segment_id,
            "name": self.name,
            "length_cm": self.length_cm,
            "min_radius_cm": self.min_radius_cm,
            "grade_percent": self.grade_percent,
            "speed_limit_kmh": self.speed_limit_kmh,
            "ordered_tiles": [
                [int(x), int(y), str(edge)] for (x, y, edge) in self.ordered_tiles
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Segment":
        """Reconstruct a segment from a layout snapshot."""
        raw_tiles = data.get("ordered_tiles") or []
        ordered_tiles: List[SegmentTileEntry] = []
        for entry in raw_tiles:
            if isinstance(entry, dict):
                x = int(entry["x"])
                y = int(entry["y"])
                edge = str(entry["entry_edge"])
            else:
                x = int(entry[0])
                y = int(entry[1])
                edge = str(entry[2])
            ordered_tiles.append(SegmentTileEntry(x=x, y=y, entry_edge=edge))

        if "length_cm" not in data:
            raise ValueError(
                f"Segment.from_dict: segment '{data.get('segment_id')}' is missing "
                "required 'length_cm'. Length must be supplied explicitly (0 is "
                "allowed); there is no silent default."
            )
        return cls(
            segment_id=data["segment_id"],
            name=data.get("name", ""),
            length_cm=float(data["length_cm"]),
            min_radius_cm=data.get("min_radius_cm"),
            grade_percent=data.get("grade_percent"),
            speed_limit_kmh=data.get("speed_limit_kmh", data.get("max_speed_kmh")),
            ordered_tiles=ordered_tiles,
        )
