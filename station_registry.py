"""
StationRegistry — owns Platform / Station CRUD pulled out of
`RailwayNetwork`.

Per design_new.md "Platform And Station", physical platforms (concrete
station tiles with associated blocks) and logical station groups are two
distinct concepts. This module owns the registries
(`platforms`, `station_groups`) and the lifecycle methods (`add_station`,
`extend_station`, `delete_station`, etc.) plus the tile→platform reverse
index. `RailwayNetwork.add_station` / `add_platform` / etc. become thin
forwarders to keep external callers (UI, scenario setup, tests) working.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional, Tuple

from railway_platform import Platform
from station import Station

if TYPE_CHECKING:
    from network import RailwayNetwork


class StationRegistry:
    """Owns platform / station registries and CRUD for one `RailwayNetwork`."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    def tile_to_platform_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning Platform id, or None.

        Per design_new.md: Tile does not store platform membership.
        Returns the concrete physical Platform that owns this tile.
        """
        return self._n._tile_to_platform.get((x, y))

    def tile_to_station_id(self, x: int, y: int) -> Optional[str]:
        """Reverse index from tile coordinate to owning logical Station id.

        v1: a Station is a logical group of platforms. This resolves the
        platform first, then walks Station.platform_ids to find the group.
        Returns None if the tile has no platform or the platform is not in
        any station group.
        """
        platform_id = self._n._tile_to_platform.get((x, y))
        if platform_id is None:
            return None
        for station_id, station in self._n.station_groups.items():
            if platform_id in station.platform_ids:
                return station_id
        return None

    def _set_tile_platform(self, x: int, y: int, platform_id: Optional[str]) -> None:
        """Update the tile -> platform reverse index.

        Network-internal helper. Use this whenever a tile's platform
        membership changes (platform creation, extension, deletion).
        """
        key = (x, y)
        if platform_id is None:
            self._n._tile_to_platform.pop(key, None)
        else:
            self._n._tile_to_platform[key] = platform_id

    def add_station(
        self,
        x: int,
        y: int,
        station_id: Optional[str] = None,
        name: str = "",
        length_cm: float = 0.0,
        platform_side: str = "both",
        top_speed_kmh: Optional[float] = None,
    ) -> Platform:
        """
        Create and add a station at the specified position.

        Args:
            x, y: Grid coordinates for station placement
            station_id: Optional specific ID, auto-generated if not provided
            name: Human-readable station name
            length_cm: Physical track length in cm (independent of tile count)
            platform_side: 'left', 'right', or 'both'
            top_speed_kmh: Optional speed limit through the station

        Returns:
            Created Station instance

        Raises:
            ValueError: If position invalid, track is not straight, switch, signal exists, or station exists
        """

        # Check if track exists
        track = self._n.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # Cannot place station on switches
        if track.is_switch:
            raise ValueError(f"Cannot place station on switch at ({x}, {y})")

        # Stations can only be placed on straight paths (pure NS/EW or zigzag forming a straight line)
        if not self._n._is_track_on_straight_path(x, y):
            raise ValueError(
                f"Cannot place station at ({x}, {y}). Stations require tracks on straight paths (pure NS/EW or zigzag patterns forming straight lines)."
            )

        # Cannot place station on tile with signal
        if track.signal_ids:
            raise ValueError(f"Cannot place station on tile with signal at ({x}, {y})")

        # Check if tile is already part of another station
        if track.station_id is not None:
            raise ValueError(
                f"Tile ({x}, {y}) already belongs to station {track.station_id}"
            )

        # Generate station ID if not provided
        if station_id is None:
            station_id = f"STA_{uuid.uuid4().hex[:6].upper()}"

        # Handle segment splitting if track belongs to a segment.
        # Stations are block boundaries, so placing a station may split the
        # segment the tile sat in.
        original_segment_id = track.segment_id
        original_segment = None
        length_per_tile_cm = (
            None  # None means no segment, 0.0 means segment with zero length
        )

        if original_segment_id:
            # Get the original segment before removal
            original_segment = self._n.get_segment(original_segment_id)
            if original_segment:
                # Length is segment geometry, so spread the SEGMENT's length
                # over the SEGMENT's tiles.
                original_positions = self._n.get_segment_positions(
                    original_segment_id
                )
                if original_positions:
                    length_per_tile_cm = original_segment.length_cm / len(
                        original_positions
                    )
                    # Store this on the owning block so child segments can use it
                    original_block_id = self._n.segment_to_block_id(
                        original_segment_id
                    )
                    if original_block_id:
                        self._n.blocks[
                            original_block_id
                        ]._length_per_tile_cm = length_per_tile_cm

            # Remove this tile from its segment (triggers connectivity check/split)
            self._n.remove_tile_from_segment(x, y)

        # Create station block for this station
        station_block_id = f"{station_id}_BLOCK"

        # Calculate station block length:
        # - If station was on a block, use calculated length per tile × 1 tile (may be 0)
        # - If not on a block, use the input platform length
        if length_per_tile_cm is not None:
            # Was on a segment (even if that segment had zero length)
            station_block_length_cm = length_per_tile_cm
        else:
            # Was not on a segment, use input platform length
            station_block_length_cm = length_cm if length_cm > 0 else 0.0

        # Use station name in block name if provided, otherwise use station_id
        block_name = (
            f"Station Block - {name}" if name else f"Station Block - {station_id}"
        )

        # A station tile becomes its own segment, and that segment gets its own
        # block (nothing is grouped with it), so both take `station_block_id`.
        station_block = self._n.create_segment(
            segment_id=station_block_id, name=block_name
        )

        # Set the calculated length on the block
        station_block.length_cm = station_block_length_cm

        # Inherit speed limit from platform to station block
        # ATC/ATO uses block's speed_limit_kmh, not station's top_speed_kmh
        if top_speed_kmh is not None:
            station_block.speed_limit_kmh = top_speed_kmh

        # Assign station tile to the station segment
        track.segment_id = station_block_id

        # Refresh ordered_tiles for both the new station segment and the
        # parent segment we split from (its membership just changed).
        geom = self._n._get_segment_geometry_manager()
        if geom is not None:
            geom.rebuild_segment_ordered_tiles(station_block_id)
            if original_segment_id and self._n.get_segment(original_segment_id):
                geom.rebuild_segment_ordered_tiles(original_segment_id)

        # Create station
        from enums import PlatformSide

        platform_enum = PlatformSide(platform_side.lower())

        # Compute initial orientation from the first tile's track_bits
        from track import BIT_TO_GROUP

        initial_orientation = None
        for bit_idx, group in BIT_TO_GROUP.items():
            if track.track_bits & (1 << bit_idx):
                initial_orientation = group
                break

        # Record the tile -> station block on the Platform's canonical
        # block_assignments, mirroring track.segment_id set above so
        # ordered_blocks / get_all_assigned_blocks see this platform's block
        # without any tile scan.
        station = Platform(
            platform_id=station_id,
            name=name,
            positions=[(x, y)],
            length_cm=length_cm,
            platform_side=platform_enum,
            top_speed_kmh=top_speed_kmh,
            initial_orientation=initial_orientation,
            block_assignments={(x, y): station_block_id},
        )

        # Store physical platform (legacy station semantics)
        self._n.platforms[station_id] = station

        # Update track's station reference
        track.station_id = station_id

        return station

    def add_platform(
        self,
        x: int,
        y: int,
        platform_id: Optional[str] = None,
        name: str = "",
        length_cm: float = 0.0,
        platform_side: str = "both",
        top_speed_kmh: Optional[float] = None,
    ) -> Platform:
        """Compatibility wrapper: create physical platform via station implementation."""
        return self.add_station(
            x=x,
            y=y,
            station_id=platform_id,
            name=name,
            length_cm=length_cm,
            platform_side=platform_side,
            top_speed_kmh=top_speed_kmh,
        )

    def get_station(self, station_id: str) -> Optional[Platform]:
        """Get physical platform by legacy station ID."""
        return self._n.platforms.get(station_id)

    def get_platform(self, platform_id: str) -> Optional[Platform]:
        """Get physical platform by ID."""
        return self._n.platforms.get(platform_id)

    def delete_station(self, station_id: str) -> bool:
        """
        Delete station and clear references from tracks.
        Also deletes all blocks that are fully contained within this station.

        Args:
            station_id: ID of station to delete

        Returns:
            True if deleted, False if not found
        """
        if station_id not in self._n.platforms:
            return False

        station = self._n.platforms[station_id]

        # Collect all blocks that are fully within this station
        # A block is "within the station" if ALL its tiles have station_id matching this station
        blocks_to_delete = set()

        # Check all blocks to see if they're fully within this station
        for block_id in list(self._n.blocks.keys()):
            block_positions = self._n.get_block_positions(block_id)
            if not block_positions:
                continue

            # Check if ALL tiles in this block belong to this station
            all_in_station = True
            for bx, by in block_positions:
                track = self._n.get_track(bx, by)
                if not track or track.station_id != station_id:
                    all_in_station = False
                    break

            if all_in_station:
                blocks_to_delete.add(block_id)
                print(
                    f"[Station Delete] Block {block_id} is fully within station {station_id}, will be deleted"
                )

        # Delete all blocks that are fully within the station. Deleting the
        # station removes its track, so this deletes each member segment's
        # geometry rather than calling `delete_block`, which only dissolves the
        # power grouping and re-homes the segments (design_new.md "Block
        # grouping"). The block itself is dropped once its last member goes,
        # through the Non-empty parent rule.
        for block_id in blocks_to_delete:
            block = self._n.blocks.get(block_id)
            if block is None:
                continue
            for segment_id in list(block.segment_ids):
                self._n.delete_segment(segment_id)
            print(f"[Station Delete] Deleted block {block_id}")

        # Clear station_id from all tracks in this station and drop
        # the tile -> platform reverse index for each owned tile.
        for x, y in station.positions:
            track = self._n.get_track(x, y)
            if track and track.station_id == station_id:
                track.station_id = None
            self._set_tile_platform(x, y, None)

        # Delete from storage
        del self._n.platforms[station_id]

        # Remove this platform from any logical station groups, then drop any
        # group left with no platforms (Non-empty parent rule). Collect the
        # affected group ids first so we never mutate station_groups while
        # iterating it.
        emptied_group_ids = []
        for group_id, station_group in self._n.station_groups.items():
            if station_id in station_group.platform_ids:
                station_group.remove_platform(station_id)
                emptied_group_ids.append(group_id)
        for group_id in emptied_group_ids:
            self._prune_station_group_if_empty(group_id)
        return True

    def delete_platform(self, platform_id: str) -> bool:
        """Compatibility wrapper for physical platform deletion."""
        return self.delete_station(platform_id)

    def extend_station(
        self, station_id: str, new_positions: List[Tuple[int, int]]
    ) -> Platform:
        """
        Add adjacent continuous tiles to existing station.

        Args:
            station_id: ID of station to extend
            new_positions: List of (x, y) positions to add

        Returns:
            Updated Station instance

        Raises:
            ValueError: If validation fails or platform would be discontinuous
        """
        if station_id not in self._n.platforms:
            raise ValueError(f"Station {station_id} not found")

        station = self._n.platforms[station_id]

        # Validate each new position
        for x, y in new_positions:
            track = self._n.get_track(x, y)
            if not track:
                raise ValueError(f"No track at position ({x}, {y})")

            if track.is_switch:
                raise ValueError(f"Cannot add switch at ({x}, {y}) to station")

            # Use same validation as add_station: straight or simple diagonal tracks
            if not self._n._is_track_on_straight_path(x, y):
                raise ValueError(
                    f"Cannot add track at ({x}, {y}) to station. Stations require tracks on straight paths (pure NS/EW or simple diagonal tracks)."
                )

            # Check orientation matches station's initial_orientation exactly
            from track import BIT_TO_GROUP

            new_tile_orientation = None
            for bit_idx, group in BIT_TO_GROUP.items():
                if track.track_bits & (1 << bit_idx):
                    new_tile_orientation = group
                    break

            if (
                station.initial_orientation
                and new_tile_orientation != station.initial_orientation
            ):
                raise ValueError(
                    f"Cannot extend {station.initial_orientation} station to tile at ({x}, {y}). "
                    f"Tile has {new_tile_orientation} orientation. "
                    f"Station extension requires exact orientation match."
                )

            if track.signal_ids:
                raise ValueError(
                    f"Cannot add tile with signal at ({x}, {y}) to station"
                )

            if track.station_id is not None and track.station_id != station_id:
                raise ValueError(
                    f"Tile ({x}, {y}) already belongs to station {track.station_id}"
                )

        # Check if new positions are adjacent to existing station
        # (simplified check - at least one new position must be adjacent to existing)
        is_adjacent = False
        for new_x, new_y in new_positions:
            for old_x, old_y in station.positions:
                if abs(new_x - old_x) + abs(new_y - old_y) == 1:
                    is_adjacent = True
                    break
            if is_adjacent:
                break

        if not is_adjacent and len(station.positions) > 0:
            raise ValueError("New positions must be adjacent to existing station")

        # Validate continuity (all positions would form connected path)
        all_positions = station.positions + new_positions
        is_continuous, errors = self._n._validate_station_continuity(all_positions)
        if not is_continuous:
            raise ValueError(f"Station would not be continuous: {', '.join(errors)}")

        # Add positions to station
        station_block_id = f"{station_id}_BLOCK"

        affected_segments: set = set()
        for x, y in new_positions:
            if (x, y) not in station.positions:
                station.add_position(x, y)
                track = self._n.get_track(x, y)
                if track:
                    # Handle segment splitting if needed - use lifecycle method
                    if track.segment_id:
                        if track.segment_id != station_block_id:
                            affected_segments.add(track.segment_id)
                        self._n.remove_tile_from_segment(x, y)
                    # Assign to the station's segment, mirroring the owning
                    # block onto the Platform's canonical block_assignments so
                    # ordered_blocks / get_all_assigned_blocks see this tile's
                    # block without any tile scan.
                    if station_block_id in self._n.blocks:
                        track.segment_id = station_block_id
                        station.block_assignments[(x, y)] = station_block_id
                    track.station_id = station_id

        # Refresh ordered_tiles on the station segment plus any segment we
        # split tiles away from, so the geometry view stays in sync.
        geom = self._n._get_segment_geometry_manager()
        if geom is not None:
            geom.rebuild_segment_ordered_tiles(station_block_id)
            for sid in affected_segments:
                if self._n.get_segment(sid) is not None:
                    geom.rebuild_segment_ordered_tiles(sid)

        return station

    def extend_platform(
        self, platform_id: str, new_positions: List[Tuple[int, int]]
    ) -> Platform:
        """Compatibility wrapper for physical platform extension."""
        return self.extend_station(platform_id, new_positions)

    def get_station_for_platform(self, platform_id: str) -> Optional[str]:
        """Get logical station ID for a platform if station groups exist."""
        for station_id, station in self._n.station_groups.items():
            if platform_id in station.platform_ids:
                return station_id
        return None

    def get_platforms_for_station(self, station_id: str) -> List[Platform]:
        """Get physical platforms for a logical station."""
        station = self._n.station_groups.get(station_id)
        if not station:
            return []

        result = []
        for pid in station.platform_ids:
            platform = self._n.platforms.get(pid)
            if platform:
                result.append(platform)
        return result

    def create_station_group(
        self,
        station_id: Optional[str] = None,
        name: str = "",
        platform_ids: Optional[List[str]] = None,
        force: bool = False,
    ) -> Station:
        """Create a logical station group for one or more platforms.

        When ``force`` is True, a platform that already belongs to another
        station is reassigned: it is detached from its old station first, which
        auto-deletes that old station when it becomes empty (Non-empty parent
        rule). When ``force`` is False, such a platform raises.
        """
        if station_id is None:
            station_id = f"STA_{uuid.uuid4().hex[:6].upper()}"
        if station_id in self._n.station_groups:
            raise ValueError(f"Station {station_id} already exists")

        normalized_platform_ids: List[str] = []
        for platform_id in platform_ids or []:
            if platform_id not in self._n.platforms:
                raise ValueError(f"Platform {platform_id} not found")
            existing_station_id = self.get_station_for_platform(platform_id)
            if existing_station_id:
                if not force:
                    raise ValueError(
                        f"Platform {platform_id} already belongs to station {existing_station_id}"
                    )
                # Force-reassign: detach from the old station (auto-prunes the
                # old station when it becomes empty).
                self.remove_platform_from_station_group(
                    existing_station_id, platform_id
                )
            if platform_id not in normalized_platform_ids:
                normalized_platform_ids.append(platform_id)

        # Non-empty parent rule (design_new.md, Tile ownership list):
        # a Station always owns at least one Platform, so a group is never
        # created empty.
        if not normalized_platform_ids:
            raise ValueError(
                "A station group must contain at least one platform"
            )

        station = Station(
            station_id=station_id,
            name=name,
            platform_ids=normalized_platform_ids,
        )
        self._n.station_groups[station_id] = station
        return station

    def delete_station_group(self, station_id: str) -> bool:
        """Delete a logical station group."""
        if station_id not in self._n.station_groups:
            return False
        del self._n.station_groups[station_id]
        return True

    def _prune_station_group_if_empty(self, station_id: str) -> bool:
        """Delete a logical station group when it has no platforms left.

        Enforces the Non-empty parent rule (design_new.md, Tile
        ownership list): a Station always owns at least one Platform, so a
        group whose last platform just left is removed rather than persisted
        empty. No-op (returns False) when the group is missing or still has
        platforms. This is the single place that deletes an emptied group;
        the removal callers route through it.
        """
        group = self._n.station_groups.get(station_id)
        if group is not None and not group.platform_ids:
            del self._n.station_groups[station_id]
            return True
        return False

    def add_platform_to_station_group(
        self, station_id: str, platform_id: str, force: bool = False
    ) -> bool:
        """Add a platform to a logical station group.

        When ``force`` is True and the platform belongs to another station, it
        is reassigned (detached from its old station first, which auto-deletes
        the old station when it becomes empty, per the Non-empty parent rule).
        """
        station = self._n.station_groups.get(station_id)
        if not station:
            raise ValueError(f"Station {station_id} not found")
        if platform_id not in self._n.platforms:
            raise ValueError(f"Platform {platform_id} not found")

        existing_station_id = self.get_station_for_platform(platform_id)
        if existing_station_id and existing_station_id != station_id:
            if not force:
                raise ValueError(
                    f"Platform {platform_id} already belongs to station {existing_station_id}"
                )
            self.remove_platform_from_station_group(existing_station_id, platform_id)

        return station.add_platform(platform_id)

    def remove_platform_from_station_group(
        self, station_id: str, platform_id: str
    ) -> bool:
        """Remove a platform from a logical station group.

        Non-empty parent rule (design_new.md, Tile ownership list):
        if this removes the group's last platform, the now-empty group is
        deleted in the same call rather than left empty.
        """
        station = self._n.station_groups.get(station_id)
        if not station:
            raise ValueError(f"Station {station_id} not found")
        removed = station.remove_platform(platform_id)
        if removed:
            self._prune_station_group_if_empty(station_id)
        return removed

