"""
Station module for the railway network system.

Stations are multi-tile entities representing passenger platforms with physical properties.
"""

from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any, Optional
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
    'NS': ('west', 'east'),           # NS track: left=west, right=east
    'EW': ('north', 'south'),         # EW track: left=north, right=south
    'NE_SW': ('northwest', 'southeast'),  # NE/SW: left=northwest (toward 0,0), right=southeast
    'NW_SE': ('southwest', 'northeast'),  # NW/SE: left=southwest, right=northeast (away from 0,0)
}


@dataclass
class Station:
    """
    Represents a railway station in the network.

    Stations can span multiple tiles and have physical properties independent
    of the number of tiles they occupy.

    Attributes:
        station_id: Unique identifier for the station (e.g., "STA_001")
        name: Human-readable station name
        positions: List of (x, y) tile positions that make up the station
        length_cm: Physical track length in cm (independent of tile count)
        platform_side: Which side(s) of the track have platforms
        top_speed_kmh: Optional speed limit through the station
        created_at: ISO timestamp of station creation
    """
    station_id: str
    name: str
    positions: List[Tuple[int, int]] = field(default_factory=list)
    length_cm: float = 0.0
    platform_side: PlatformSide = PlatformSide.BOTH
    top_speed_kmh: Optional[float] = None
    created_at: Optional[str] = None
    initial_orientation: Optional[str] = None  # 'NS', 'EW', 'NE_SW', or 'NW_SE' - set at creation

    def __post_init__(self):
        """Set creation timestamp if not provided."""
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Convert station to dictionary for JSON serialization."""
        return {
            'station_id': self.station_id,
            'name': self.name,
            'positions': [[x, y] for x, y in self.positions],
            'length_cm': self.length_cm,
            'platform_side': self.platform_side.value,
            'top_speed_kmh': self.top_speed_kmh,
            'created_at': self.created_at,
            'tile_count': len(self.positions),
            'initial_orientation': self.initial_orientation,
        }

    def to_dict_with_orientation(self, network: 'RailwayNetwork') -> Dict[str, Any]:
        """
        Convert station to dictionary with computed orientation data.

        This includes:
        - orientation: The track orientation group (NS, EW, NE_SW, NW_SE)
        - sorted_positions: Positions sorted along track direction (first to last)
        - platform_sides: Absolute compass directions for platform sides
        """
        base = self.to_dict()

        # Compute orientation from first tile's track
        orientation = self._get_orientation(network)
        base['orientation'] = orientation

        # Sort positions along track direction
        sorted_positions = self._sort_positions_by_orientation(orientation)
        base['sorted_positions'] = [[x, y] for x, y in sorted_positions]

        # Map platform_side to absolute directions
        if orientation and orientation in ORIENTATION_TO_SIDES:
            side1, side2 = ORIENTATION_TO_SIDES[orientation]
            if self.platform_side == PlatformSide.BOTH:
                base['platform_sides'] = [side1, side2]
            elif self.platform_side == PlatformSide.LEFT:
                base['platform_sides'] = [side1]
            else:  # RIGHT
                base['platform_sides'] = [side2]
        else:
            base['platform_sides'] = []

        return base

    def _get_orientation(self, network: 'RailwayNetwork') -> Optional[str]:
        """
        Get the station's track orientation group.

        Returns the stored initial_orientation (set at creation).

        Returns: 'NS', 'EW', 'NE_SW', or 'NW_SE'
        """
        return self.initial_orientation

    def _sort_positions_by_orientation(
        self,
        orientation: Optional[str]
    ) -> List[Tuple[int, int]]:
        """
        Sort positions along the track direction.

        The sort ensures the first position is the "start" of the station
        and the last position is the "end", based on the orientation.
        """
        if not self.positions or not orientation:
            return list(self.positions)

        # Define sort key based on orientation
        # We want to sort so that traversing the list goes along the track
        def sort_key(pos: Tuple[int, int]) -> float:
            x, y = pos
            if orientation == 'NS':
                # NS runs along Y axis, higher Y = more north
                return -y  # Sort north first
            elif orientation == 'EW':
                # EW runs along X axis, higher X = more east
                return -x  # Sort west first
            elif orientation == 'NE_SW':
                # NE and SW are in same group, diagonal along x+y
                return -(x + y)  # Sort toward NE first
            elif orientation == 'NW_SE':
                # NW and SE are in same group, diagonal along x-y
                return -(y - x)  # Sort toward NW first
            return 0

        return sorted(self.positions, key=sort_key)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Station':
        """Create a Station from a dictionary."""
        return cls(
            station_id=data['station_id'],
            name=data['name'],
            positions=[tuple(pos) for pos in data.get('positions', [])],
            length_cm=data.get('length_cm', 0.0),
            platform_side=PlatformSide(data.get('platform_side', 'both')),
            top_speed_kmh=data.get('top_speed_kmh'),
            created_at=data.get('created_at'),
            initial_orientation=data.get('initial_orientation'),
        )

    def add_position(self, x: int, y: int) -> None:
        """Add a new tile position to the station."""
        pos = (x, y)
        if pos not in self.positions:
            self.positions.append(pos)

    def remove_position(self, x: int, y: int) -> bool:
        """Remove a tile position from the station. Returns True if removed."""
        pos = (x, y)
        if pos in self.positions:
            self.positions.remove(pos)
            return True
        return False

    def contains_position(self, x: int, y: int) -> bool:
        """Check if station includes this tile position."""
        return (x, y) in self.positions

    def get_center_position(self) -> Optional[Tuple[float, float]]:
        """Calculate the center point of all station tiles."""
        if not self.positions:
            return None
        avg_x = sum(x for x, y in self.positions) / len(self.positions)
        avg_y = sum(y for x, y in self.positions) / len(self.positions)
        return (avg_x, avg_y)
