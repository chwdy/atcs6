"""Power district entity.

design_new.md "Power district": a power district is a collection of blocks
wired to one power output, so every block in it carries the same `track
voltage`. It is the electrical counterpart of a `Station` (which groups blocks
for operations) — a grouping over blocks for power. A block belongs to exactly
one district; a block the operator has not grouped is its own single-block
district. The district adds only the shared power feed; each block keeps its own
tiles, params, occupancy, and current detector (detection isolation is separate
from power grouping).

The "one train per powered district" rule (the district carries one train's
voltage at a time) is enforced by `BlockManager` (reservation eligibility +
the `POWER_DISTRICT_INTRUSION` fatal physical-entry conflict); this entity is
just the block grouping those rules read.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class PowerDistrict:
    """A group of blocks sharing one power feed (one `track voltage`).

    Attributes:
        district_id: Unique identifier.
        block_ids: Member block ids. Every member shares the district's one
            track voltage. A block appears in at most one district.
        name: Optional human-readable name.
    """

    district_id: str
    block_ids: List[str] = field(default_factory=list)
    name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict (layout persistence)."""
        return {
            "district_id": self.district_id,
            "block_ids": list(self.block_ids),
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PowerDistrict":
        """Reconstruct from a dict."""
        return cls(
            district_id=str(data["district_id"]),
            block_ids=[str(b) for b in data.get("block_ids", [])],
            name=str(data.get("name", "")),
        )
