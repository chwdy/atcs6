"""
Station module for the railway network system.

Station is a logical grouping entity that collects one or more Platforms under a single name.
Unlike Platform, Station has no physical tiles - it is purely a grouping concept.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Set, Tuple
from datetime import datetime


@dataclass
class Station:
    """
    Logical grouping of railway Platforms under a single station name.

    A Station has no physical tiles - it groups Platforms for unified naming
    and flexible routing. A single Platform can belong to
    at most one Station.

    Attributes:
        station_id: Unique identifier (e.g., "STA_001")
        name: Human-readable station name (e.g., "Central Station")
        platform_ids: Ordered list of Platform IDs belonging to this station
        created_at: ISO timestamp of creation
    """

    station_id: str
    name: str = ""
    platform_ids: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    disabled: bool = False
    """When True, no StationTarget task can resolve to this station; the
    station's platform_ids are excluded from candidate enumeration. Individual
    Platform records inside the station remain reachable via PlatformTarget
    subject to each platform's own flags. No operator override at the station
    level. See the Platform And Station availability flags in design_new.md."""
    manual: bool = False
    """When True, every StationTarget auto-resolution treats this station as
    if disabled (all of its platform_ids excluded from candidate enumeration)
    because automatic callers never set manual_override. Individual platforms
    inside it remain reachable through their own PlatformTarget rules subject
    to each platform's own flags. See the Platform And Station availability
    flags in design_new.md and the `disabled` and `manual` availability flags
    canonical home under TrackResource."""

    def __post_init__(self):
        """Set creation timestamp if not provided."""
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()

    def add_platform(self, platform_id: str) -> bool:
        """
        Add a Platform to this Station.

        Args:
            platform_id: Platform ID to add

        Returns:
            True if added, False if already present
        """
        if platform_id in self.platform_ids:
            return False
        self.platform_ids.append(platform_id)
        return True

    def remove_platform(self, platform_id: str) -> bool:
        """
        Remove a Platform from this Station.

        Args:
            platform_id: Platform ID to remove

        Returns:
            True if removed, False if not found
        """
        if platform_id not in self.platform_ids:
            return False
        self.platform_ids.remove(platform_id)
        return True

    def get_all_block_ids(self, network: "RailwayNetwork") -> Set[str]:
        """
        Get all block IDs across all Platforms in this Station.

        This is used for flexible platform routing - the executor checks
        availability across all blocks in a Station.

        Args:
            network: Railway network for Platform lookup

        Returns:
            Set of block IDs from all Platforms
        """
        block_ids = set()
        for platform_id in self.platform_ids:
            platform = network.platforms.get(platform_id)
            if platform:
                block_ids.update(platform.get_all_assigned_blocks())
        return block_ids

    def get_center_position(
        self, network: "RailwayNetwork"
    ) -> Optional[Tuple[float, float]]:
        """
        Calculate center position across all Platforms for label rendering.

        Args:
            network: Railway network for Platform lookup

        Returns:
            Average (x, y) across all Platform tiles, or None
        """
        all_positions = []
        for platform_id in self.platform_ids:
            platform = network.platforms.get(platform_id)
            if platform:
                all_positions.extend(platform.positions)

        if not all_positions:
            return None

        avg_x = sum(x for x, y in all_positions) / len(all_positions)
        avg_y = sum(y for x, y in all_positions) / len(all_positions)
        return (avg_x, avg_y)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "station_id": self.station_id,
            "name": self.name,
            "platform_ids": list(self.platform_ids),
            "created_at": self.created_at,
            "disabled": self.disabled,
            "manual": self.manual,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Station":
        """Reconstruct from dictionary after deserialization."""
        return cls(
            station_id=data["station_id"],
            name=data.get("name", ""),
            platform_ids=list(data.get("platform_ids", [])),
            created_at=data.get("created_at"),
            disabled=bool(data.get("disabled", False)),
            manual=bool(data.get("manual", False)),
        )
