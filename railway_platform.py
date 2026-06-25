"""
Platform module for the railway network system.

Platforms are multi-tile entities representing passenger platforms with physical properties.
Formerly named 'Station' - renamed to distinguish from logical Station grouping entity.
"""

from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any, Optional, Set
from datetime import datetime

from enums import PlatformSide


# Orientation group to perpendicular sides mapping
# These are absolute compass directions perpendicular to the track
# Left/Right definition:
#   - NS tracks: left=west, right=east
#   - EW tracks: left=north, right=south
#   - NE_SW tracks: perpendicular is NW-SE axis; left=northwest, right=southeast
#   - NW_SE tracks: perpendicular is NE-SW axis; left=southwest, right=northeast
ORIENTATION_TO_SIDES = {
    "NS": ("west", "east"),  # NS track: left=west, right=east
    "EW": ("north", "south"),  # EW track: left=north, right=south
    "NE_SW": (
        "northwest",
        "southeast",
    ),  # NE/SW: left=northwest (toward 0,0), right=southeast
    "NW_SE": (
        "southwest",
        "northeast",
    ),  # NW/SE: left=southwest, right=northeast (away from 0,0)
}


@dataclass
class Platform:
    """
    Represents a railway platform in the network.

    Platforms can span multiple tiles and have physical properties independent
    of the number of tiles they occupy. Formerly named 'Station'.

    Multi-Block Support:
        Platforms can now have multiple blocks assigned to different sections,
        enabling granular train position tracking. For example, a 10-tile platform
        could have 3 blocks: tiles 0-3, 4-6, 7-9.

    Attributes:
        platform_id: Unique identifier for the platform (e.g., "PLT_001")
        name: Human-readable platform name
        positions: List of (x, y) tile positions that make up the platform
        length_cm: Physical track length in cm (independent of tile count)
        platform_side: Which side(s) of the track have platforms
        top_speed_kmh: Optional speed limit through the platform
        created_at: ISO timestamp of platform creation
        block_assignments: Dict mapping (x, y) positions to block IDs for multi-block support
    """

    platform_id: str
    name: str
    positions: List[Tuple[int, int]] = field(default_factory=list)
    length_cm: float = 0.0
    platform_side: PlatformSide = PlatformSide.BOTH
    top_speed_kmh: Optional[float] = None
    created_at: Optional[str] = None
    initial_orientation: Optional[str] = (
        None  # 'NS', 'EW', 'NE_SW', or 'NW_SE' - set at creation
    )
    block_assignments: Dict[Tuple[int, int], str] = field(
        default_factory=dict
    )  # (x, y) -> block_id
    disabled: bool = False
    """When True, the platform cannot be the resolved target of any route task.
    StationTarget candidate enumeration excludes it; PlatformTarget naming this
    platform fails at resolution time. No operator override. See the
    Platform And Station availability flags in design_new.md."""
    manual: bool = False
    """When True, automatic route target resolution treats the platform as if
    disabled. Operator-initiated paths can still land on the platform's
    constituent blocks via dispatcher-level manual_override (subject to each
    block's own flags). See the Platform And Station availability flags in
    design_new.md and the `disabled` and `manual` availability flags
    canonical home under TrackResource."""

    def __post_init__(self):
        """Set creation timestamp if not provided."""
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Convert platform to dictionary for JSON serialization."""
        return {
            "platform_id": self.platform_id,
            "name": self.name,
            "positions": [[x, y] for x, y in self.positions],
            "length_cm": self.length_cm,
            "platform_side": self.platform_side.value,
            "top_speed_kmh": self.top_speed_kmh,
            "created_at": self.created_at,
            "tile_count": len(self.positions),
            "initial_orientation": self.initial_orientation,
            "block_assignments": {
                f"{x},{y}": block_id
                for (x, y), block_id in self.block_assignments.items()
            },
            "disabled": self.disabled,
            "manual": self.manual,
        }

    def to_dict_with_orientation(self, network: "RailwayNetwork") -> Dict[str, Any]:
        """
        Convert platform to dictionary with computed orientation data.

        This includes:
        - orientation: The track orientation group (NS, EW, NE_SW, NW_SE)
        - sorted_positions: Positions sorted along track direction (first to last)
        - platform_sides: Absolute compass directions for platform sides
        - block_assignments_sorted: Block assignments keyed by sorted position index
        """
        base = self.to_dict()

        # Compute orientation from first tile's track
        orientation = self._get_orientation(network)
        base["orientation"] = orientation

        # Sort positions along track direction
        sorted_positions = self._sort_positions_by_orientation(orientation)
        base["sorted_positions"] = [[x, y] for x, y in sorted_positions]

        # Add block assignments indexed by sorted position
        block_assignments_sorted = {}
        for idx, (x, y) in enumerate(sorted_positions):
            if (x, y) in self.block_assignments:
                block_assignments_sorted[idx] = self.block_assignments[(x, y)]
        base["block_assignments_sorted"] = block_assignments_sorted

        # Map platform_side to absolute directions
        if orientation and orientation in ORIENTATION_TO_SIDES:
            side1, side2 = ORIENTATION_TO_SIDES[orientation]
            if self.platform_side == PlatformSide.BOTH:
                base["platform_sides"] = [side1, side2]
            elif self.platform_side == PlatformSide.EITHER:
                base["platform_sides"] = [side1, side2]
            elif self.platform_side == PlatformSide.LEFT:
                base["platform_sides"] = [side1]
            else:  # RIGHT
                base["platform_sides"] = [side2]
        else:
            base["platform_sides"] = []

        return base

    def _get_orientation(self, network: "RailwayNetwork") -> Optional[str]:
        """
        Get the platform's track orientation group.

        Returns the stored initial_orientation (set at creation).

        Returns: 'NS', 'EW', 'NE_SW', or 'NW_SE'
        """
        return self.initial_orientation

    def _sort_positions_by_orientation(
        self, orientation: Optional[str]
    ) -> List[Tuple[int, int]]:
        """
        Sort positions along the track direction.

        The sort ensures the first position is the "start" of the platform
        and the last position is the "end", based on the orientation.
        """
        if not self.positions or not orientation:
            return list(self.positions)

        # Define sort key based on orientation
        # We want to sort so that traversing the list goes along the track
        def sort_key(pos: Tuple[int, int]) -> float:
            x, y = pos
            if orientation == "NS":
                # NS runs along Y axis, higher Y = more north
                return -y  # Sort north first
            elif orientation == "EW":
                # EW runs along X axis, higher X = more east
                return -x  # Sort west first
            elif orientation == "NE_SW":
                # NE and SW are in same group, diagonal along x-y
                return -(x - y)  # Sort toward NE first
            elif orientation == "NW_SE":
                # NW and SE are in same group, diagonal along x+y
                return -(x + y)  # Sort toward NW first
            return 0

        return sorted(self.positions, key=sort_key)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Platform":
        """Create a Platform from a dictionary."""
        # Migration: accept both platform_id and legacy station_id
        _pid = data.get("platform_id")
        platform_id = _pid if _pid is not None else data.get("station_id", "")

        # Parse block_assignments from serialized format
        block_assignments_data = data.get("block_assignments", {})
        block_assignments = {}
        for pos_str, block_id in block_assignments_data.items():
            x, y = map(int, pos_str.split(","))
            block_assignments[(x, y)] = block_id

        if "length_cm" not in data:
            raise ValueError(
                f"Platform.from_dict: platform '{platform_id}' is missing "
                "required 'length_cm'. Length must be supplied explicitly (0 is "
                "allowed); there is no silent default."
            )
        return cls(
            platform_id=platform_id,
            name=data.get("name", ""),
            positions=[tuple(pos) for pos in data.get("positions", [])],
            length_cm=float(data["length_cm"]),
            platform_side=PlatformSide(data.get("platform_side", "both")),
            top_speed_kmh=data.get("top_speed_kmh"),
            created_at=data.get("created_at"),
            initial_orientation=data.get("initial_orientation"),
            block_assignments=block_assignments,
            disabled=bool(data.get("disabled", False)),
            manual=bool(data.get("manual", False)),
        )

    def add_position(self, x: int, y: int) -> None:
        """Add a new tile position to the platform."""
        pos = (x, y)
        if pos not in self.positions:
            self.positions.append(pos)

    def remove_position(self, x: int, y: int) -> bool:
        """Remove a tile position from the platform. Returns True if removed."""
        pos = (x, y)
        if pos in self.positions:
            self.positions.remove(pos)
            # Also remove block assignment for this position
            self.block_assignments.pop(pos, None)
            return True
        return False

    def get_center_position(self) -> Optional[Tuple[float, float]]:
        """Calculate the center point of all platform tiles."""
        if not self.positions:
            return None
        avg_x = sum(x for x, y in self.positions) / len(self.positions)
        avg_y = sum(y for x, y in self.positions) / len(self.positions)
        return (avg_x, avg_y)

    # Block assignment management methods

    def assign_tile_to_block(self, x: int, y: int, block_id: str) -> bool:
        """
        Assign a platform tile to a specific block.

        Args:
            x, y: Tile coordinates
            block_id: ID of the block to assign this tile to

        Returns:
            True if assignment succeeded, False if tile not in platform

        Example:
            platform.assign_tile_to_block(5, 10, "BLK_001")
        """
        pos = (x, y)
        if pos not in self.positions:
            return False
        self.block_assignments[pos] = block_id
        return True

    def unassign_tile_from_block(self, x: int, y: int) -> bool:
        """
        Remove block assignment from a platform tile.

        Args:
            x, y: Tile coordinates

        Returns:
            True if tile was unassigned, False if no assignment existed

        Example:
            platform.unassign_tile_from_block(5, 10)
        """
        pos = (x, y)
        if pos in self.block_assignments:
            del self.block_assignments[pos]
            return True
        return False

    def get_block_for_tile(self, x: int, y: int) -> Optional[str]:
        """
        Get the block ID assigned to a specific tile.

        Args:
            x, y: Tile coordinates

        Returns:
            Block ID if assigned, None otherwise

        Example:
            block_id = platform.get_block_for_tile(5, 10)
        """
        return self.block_assignments.get((x, y))

    def get_tiles_for_block(self, block_id: str) -> List[Tuple[int, int]]:
        """
        Get all tiles assigned to a specific block.

        Args:
            block_id: ID of the block

        Returns:
            List of (x, y) positions assigned to this block

        Example:
            tiles = platform.get_tiles_for_block("BLK_001")
            # Returns: [(5, 10), (6, 10), (7, 10)]
        """
        return [pos for pos, bid in self.block_assignments.items() if bid == block_id]

    def get_all_assigned_blocks(self) -> Set[str]:
        """
        Get set of all unique block IDs assigned to platform tiles.

        Returns:
            Set of block IDs that have at least one tile assigned

        Example:
            blocks = platform.get_all_assigned_blocks()
            # Returns: {"BLK_001", "BLK_002", "BLK_003"}
        """
        return set(self.block_assignments.values())

    @property
    def ordered_blocks(self) -> List[str]:
        """
        Canonical ordered list of block IDs in the platform per design_new.md
        "Platform and station model".

        Derived from `block_assignments`: blocks are listed in the order they
        first appear when iterating `positions` (the tile order supplied by
        the platform editor). Duplicates are removed while preserving first
        appearance, so a multi-tile platform with several blocks returns each
        block exactly once in physical traversal order. Identifying the two
        platform-end blocks for path planning is therefore
        `ordered_blocks[0]` and `ordered_blocks[-1]`.
        """
        seen: Set[str] = set()
        result: List[str] = []
        for pos in self.positions:
            block_id = self.block_assignments.get(pos)
            if block_id is None or block_id in seen:
                continue
            seen.add(block_id)
            result.append(block_id)
        return result
