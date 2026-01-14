from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, TYPE_CHECKING
from enum import Enum

# NOTE: DEFAULT_TILE_LENGTH_CM import removed - no longer using default fallback
from enums import BlockState, SignalState, Direction

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train


class LookaheadItemType(Enum):
    SIGNAL = "signal"
    BLOCK = "block"
    STATION = "station"
    SPEED_LIMIT = "speed_limit"
    SWITCH = "switch"


@dataclass
class LookaheadItem:
    item_type: LookaheadItemType
    distance_cm: float
    data: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "type": self.item_type.value,
            "distance_cm": self.distance_cm,
        }
        result.update(self.data)
        return result


@dataclass
class LookaheadResult:
    train_id: str
    current_speed_kmh: float
    target_speed_kmh: float
    max_speed_kmh: float
    facing_direction: Optional[str]
    travel_direction: str
    state: str
    lookahead_items: List[LookaheadItem] = field(default_factory=list)
    clearance_cm: float = 0.0
    atc_target_speed_kmh: float = 0.0
    warnings: List[Dict[str, Any]] = field(
        default_factory=list
    )  # NEW: Configuration warnings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "train_id": self.train_id,
            "current_speed_kmh": self.current_speed_kmh,
            "target_speed_kmh": self.target_speed_kmh,
            "max_speed_kmh": self.max_speed_kmh,
            "facing_direction": self.facing_direction,
            "travel_direction": self.travel_direction,
            "state": self.state,
            "lookahead_items": [item.to_dict() for item in self.lookahead_items],
            "clearance_cm": self.clearance_cm,
            "atc_target_speed_kmh": self.atc_target_speed_kmh,
            "warnings": self.warnings,  # NEW: Include warnings array
        }


OPPOSITE_EDGE = {"N": "S", "S": "N", "E": "W", "W": "E"}
DIRECTION_TO_EDGE = {
    Direction.NORTH: "N",
    Direction.SOUTH: "S",
    Direction.EAST: "E",
    Direction.WEST: "W",
}
EDGE_OFFSETS = {
    "N": (0, 1),
    "E": (1, 0),
    "S": (0, -1),
    "W": (-1, 0),
}


@dataclass
class TilePhysicalInfo:
    x: int
    y: int
    physical_length_cm: float
    block_id: Optional[str] = None
    station_id: Optional[str] = None
    is_switch: bool = False


def _get_tile_physical_length(
    network: "RailwayNetwork",
    track,
    block_tile_counts: Dict[str, int],
    station_tile_counts: Dict[str, int],
) -> float:
    """
    Calculate physical length of a track tile.

    NOTE: Returns 0.0 if length is unconfigured (no default fallback).
    This will trigger warnings in the lookahead system.
    """
    if hasattr(track, "is_switch") and track.is_switch:
        # Switches handled separately via Switch entity
        return 0.0

    if hasattr(track, "block_section_id") and track.block_section_id:
        block_id = track.block_section_id
        block = network.blocks.get(block_id)
        if block and hasattr(block, "length_cm"):
            if block.length_cm == 0:
                return 0.0  # Unconfigured - will trigger warning
            tile_count = block_tile_counts.get(block_id, 1)
            if tile_count <= 0:
                tile_count = 1
            return block.length_cm / tile_count
        return 0.0  # No block entity or no length - will trigger warning

    if hasattr(track, "station_id") and track.station_id:
        # Stations are markers, not physical distance.
        # Station length is metadata for display purposes only.
        # Physical distance should be determined by the block the station is in.
        return 0.0

    return 0.0  # Plain track with no block/station - length is 0


def _precompute_tile_counts(
    network: "RailwayNetwork",
) -> Tuple[Dict[str, int], Dict[str, int]]:
    block_tile_counts: Dict[str, int] = {}
    for block_id in network.blocks:
        positions = network.get_block_positions(block_id)
        block_tile_counts[block_id] = len(positions) if positions else 1

    station_tile_counts: Dict[str, int] = {}
    for station_id, station in network.stations.items():
        if hasattr(station, "positions"):
            station_tile_counts[station_id] = len(station.positions) or 1
        else:
            station_tile_counts[station_id] = 1

    return block_tile_counts, station_tile_counts


def _calculate_absolute_position_to_tile_start(
    train: "Train",
    network: "RailwayNetwork",
    block_tile_counts: Dict[str, int],
    station_tile_counts: Dict[str, int],
) -> Tuple[float, set, float]:
    """
    Calculate physical distance from path start to the start of the current tile.

    Returns:
        Tuple of (physical_distance, seen_blocks, current_block_offset)
        current_block_offset: How far into the current block we are (if applicable)
    """
    if not train.current_path:
        return 0.0, set(), 0.0

    # Determine the block ID of the current tile (where the train head is)
    current_block_id = None
    if train.path_index < len(train.current_path):
        px, py, _ = train.current_path[train.path_index]
        track = network.tracks.get((px, py))
        if track:
            current_block_id = getattr(track, "block_section_id", None)

    physical_distance = 0.0
    current_block_offset = 0.0
    seen_blocks: set = set()

    for i in range(train.path_index):
        if i >= len(train.current_path):
            break
        px, py, _ = train.current_path[i]
        track = network.tracks.get((px, py))
        if not track:
            continue

        is_switch = hasattr(track, "is_switch") and track.is_switch
        item_added = False

        # Block handling
        if hasattr(track, "block_section_id") and track.block_section_id:
            block_id = track.block_section_id

            # Special handling for the block the train is currently in
            if block_id == current_block_id:
                # We are traversing tiles OF the current block BEFORE the current head position.
                # Do NOT add full block length. Do NOT mark as seen.
                # Add per-tile length instead.
                block = network.blocks.get(block_id)
                if block and hasattr(block, "length_cm") and block.length_cm > 0:
                    tile_count = block_tile_counts.get(block_id, 1)
                    per_tile_len = block.length_cm / tile_count
                    physical_distance += per_tile_len
                    current_block_offset += per_tile_len
                item_added = True

            # Normal handling for completed blocks
            elif block_id not in seen_blocks:
                seen_blocks.add(block_id)
                block = network.blocks.get(block_id)
                if block and hasattr(block, "length_cm") and block.length_cm > 0:
                    physical_distance += block.length_cm
                item_added = True
            else:
                # Already seen block (and not current) - skip adding length
                item_added = True

        # Switch handling (only if not part of a block)
        if is_switch and not item_added:
            switch_config = (
                network.get_switch_at(px, py)
                if hasattr(network, "get_switch_at")
                else None
            )
            if switch_config:
                physical_distance += switch_config.get_active_length(track.switch_state)
            item_added = True

        # Plain track without block - use per-tile length (usually 0 for unconfigured)
        # Stations are markers, don't add to distance

    return physical_distance, seen_blocks, current_block_offset


def build_lookahead(
    train: "Train",
    network: "RailwayNetwork",
    max_distance_cm: float = 100000.0,
) -> LookaheadResult:
    result = LookaheadResult(
        train_id=train.train_id,
        current_speed_kmh=train.current_speed,
        target_speed_kmh=train.target_speed,
        max_speed_kmh=train.max_speed,
        facing_direction=train.facing_direction.name
        if train.facing_direction
        else None,
        travel_direction=train.travel_direction,
        state=train.state.value if train.state else "off",
    )

    if not train.current_pos or not train.facing_direction:
        return result

    current_x, current_y = train.current_pos

    if train.travel_direction == "forward":
        travel_dir = train.facing_direction
    else:
        travel_dir = _opposite_direction(train.facing_direction)

    travel_edge = DIRECTION_TO_EDGE.get(travel_dir)
    if not travel_edge:
        return result

    block_tile_counts, station_tile_counts = _precompute_tile_counts(network)

    train_absolute_cm, already_seen_blocks, current_block_offset = (
        _calculate_absolute_position_to_tile_start(
            train, network, block_tile_counts, station_tile_counts
        )
    )

    # Determine current block ID for offset correction
    current_track = network.get_track(current_x, current_y)
    current_block_id = (
        getattr(current_track, "block_section_id", None) if current_track else None
    )

    visited = set()
    x, y = current_x, current_y
    entry_edge = OPPOSITE_EDGE.get(travel_edge, "S")

    # Initialize seen_blocks with blocks already counted before path_index
    # This prevents double-counting blocks that span multiple tiles
    seen_blocks: Dict[str, float] = {block_id: -1.0 for block_id in already_seen_blocks}
    seen_stations: Dict[str, float] = {}
    sequence_counter = 0
    # Start cumulative distance from train's absolute position along the path
    # This ensures lookahead item positions are in the same coordinate system
    # as distance_into_path_cm (both absolute from path start)
    cumulative_distance = train_absolute_cm

    while cumulative_distance < (train_absolute_cm + max_distance_cm):
        if (x, y) in visited:
            break
        visited.add((x, y))

        track = network.tracks.get((x, y))
        if not track:
            break

        is_switch = hasattr(track, "is_switch") and track.is_switch

        # Calculate this tile's physical length for signal positioning
        tile_length_for_signal = _get_tile_physical_length(
            network, track, block_tile_counts, station_tile_counts
        )
        # For switches not in blocks, get switch length
        if is_switch and tile_length_for_signal == 0.0:
            switch_config = (
                network.get_switch_at(x, y)
                if hasattr(network, "get_switch_at")
                else None
            )
            if switch_config:
                tile_length_for_signal = switch_config.get_active_length(
                    track.switch_state if hasattr(track, "switch_state") else 0
                )

        _collect_signals_on_tile(
            network,
            track,
            x,
            y,
            travel_edge,
            cumulative_distance,
            result,
            sequence_counter,
            tile_length_for_signal,
        )

        item_added_this_tile = False

        if hasattr(track, "block_section_id") and track.block_section_id:
            block_id = track.block_section_id
            block = network.blocks.get(block_id)
            tile_count = block_tile_counts.get(block_id, 1)
            block_length = (
                block.length_cm if block and hasattr(block, "length_cm") else 0.0
            )
            per_tile_length = block_length / tile_count if tile_count > 0 else 0.0

            if block_id not in seen_blocks:
                # First tile of this block - record it for lookahead display
                start_distance = cumulative_distance
                is_current_block = block_id == current_block_id

                if is_current_block and current_block_offset > 0:
                    # We are mid-block. The block actually started earlier.
                    # Adjust start distance backwards.
                    start_distance = cumulative_distance - current_block_offset

                seen_blocks[block_id] = start_distance
                sequence_counter += 1

                # Validation warnings
                if not block:
                    result.warnings.append(
                        {
                            "type": "block_not_found",
                            "block_id": block_id,
                            "position": (x, y),
                            "message": f"Block {block_id} not found in network",
                        }
                    )
                elif not hasattr(block, "length_cm") or block.length_cm == 0:
                    result.warnings.append(
                        {
                            "type": "block_length_not_configured",
                            "block_id": block_id,
                            "position": (x, y),
                            "message": f"Block {block_id} has no length configured. Please set length in Block Editor.",
                        }
                    )

                _add_block_item(
                    network,
                    block_id,
                    start_distance,
                    block_tile_counts,
                    result,
                    sequence_counter,
                )

            # Always add per-tile length for this tile (whether first or subsequent)
            # This ensures signals on later tiles get correct distances
            cumulative_distance += per_tile_length

            # Mark as handled so switches inside blocks don't double-count
            item_added_this_tile = True

        if hasattr(track, "station_id") and track.station_id:
            station_id = track.station_id
            if station_id not in seen_stations:
                seen_stations[station_id] = cumulative_distance
                sequence_counter += 1
                station = network.stations.get(station_id)

                # Validate station exists and has length configured
                if not station:
                    result.warnings.append(
                        {
                            "type": "station_not_found",
                            "station_id": station_id,
                            "position": (x, y),
                            "message": f"Station {station_id} not found in network",
                        }
                    )
                elif not hasattr(station, "length_cm") or station.length_cm == 0:
                    result.warnings.append(
                        {
                            "type": "station_length_not_configured",
                            "station_id": station_id,
                            "position": (x, y),
                            "message": f"Station {station_id} has no length configured. Please set length in Station Editor.",
                        }
                    )

                _add_station_item(
                    network,
                    station_id,
                    cumulative_distance,
                    station_tile_counts,
                    result,
                    sequence_counter,
                )
                # NOTE: Stations are just markers - they do NOT add to cumulative_distance.
                # Station length is metadata (for train monitor display), not physical track length.
                # Physical distance is determined by blocks, not stations.

        if is_switch and not item_added_this_tile:
            # Only handle switches that are NOT part of a block
            # If switch is in a block, block length already covers the physical distance
            sequence_counter += 1

            # NEW: Get switch configuration from network
            switch_config = (
                network.get_switch_at(x, y)
                if hasattr(network, "get_switch_at")
                else None
            )

            if switch_config:
                # Get length based on current switch state
                current_state = track.switch_state
                switch_length = switch_config.get_active_length(current_state)

                # Note: switch_length=0 is correct by design - switches don't add distance
                # The warning was removed as 0-length switches are intentional
            else:
                # No switch entity found - this shouldn't happen with auto-creation
                # but handle gracefully
                result.warnings.append(
                    {
                        "type": "switch_entity_missing",
                        "position": (x, y),
                        "message": f"Switch at ({x}, {y}) has no configuration entity",
                    }
                )
                switch_length = 0.0

            _add_switch_item(
                track,
                x,
                y,
                cumulative_distance,
                result,
                sequence_counter,
                switch_config,
            )
            cumulative_distance += switch_length
            item_added_this_tile = True

        if not item_added_this_tile:
            tile_length = _get_tile_physical_length(
                network, track, block_tile_counts, station_tile_counts
            )
            cumulative_distance += tile_length

        exit_edge = _get_exit_edge(track, entry_edge)
        if not exit_edge:
            break

        dx, dy = EDGE_OFFSETS.get(exit_edge, (0, 0))
        x, y = x + dx, y + dy
        entry_edge = OPPOSITE_EDGE.get(exit_edge, "S")

    result.clearance_cm = cumulative_distance

    return result


def _add_block_item(
    network: "RailwayNetwork",
    block_id: str,
    distance_cm: float,
    block_tile_counts: Dict[str, int],
    result: LookaheadResult,
    sequence: int,
) -> None:
    """Add block item to lookahead. No default fallback - returns 0 if unconfigured."""
    block = network.blocks.get(block_id)
    if not block:
        return

    tile_count = block_tile_counts.get(block_id, 1)

    # No default fallback - use configured length or 0
    if hasattr(block, "length_cm"):
        length_cm = block.length_cm
    else:
        length_cm = 0.0

    item = LookaheadItem(
        item_type=LookaheadItemType.BLOCK,
        distance_cm=distance_cm,
        sequence=sequence,
        data={
            "id": block_id,
            "name": block.name if hasattr(block, "name") and block.name else block_id,
            "state": block.state.value if block.state else "unknown",
            "length_cm": length_cm,
            "tile_count": tile_count,
        },
    )
    result.lookahead_items.append(item)


def _add_station_item(
    network: "RailwayNetwork",
    station_id: str,
    distance_cm: float,
    station_tile_counts: Dict[str, int],
    result: LookaheadResult,
    sequence: int,
) -> None:
    """Add station item to lookahead. No default fallback - returns 0 if unconfigured."""
    station = network.stations.get(station_id)
    if not station:
        return

    tile_count = station_tile_counts.get(station_id, 1)

    # No default fallback - use configured length or 0
    if hasattr(station, "length_cm"):
        length_cm = station.length_cm
    else:
        length_cm = 0.0

    item = LookaheadItem(
        item_type=LookaheadItemType.STATION,
        distance_cm=distance_cm,
        sequence=sequence,
        data={
            "id": station_id,
            "name": station.name if hasattr(station, "name") else station_id,
            "length_cm": length_cm,
            "tile_count": tile_count,
            "platform_side": "right",
        },
    )
    result.lookahead_items.append(item)


def _add_switch_item(
    track,
    x: int,
    y: int,
    distance_cm: float,
    result: LookaheadResult,
    sequence: int,
    switch_config=None,  # NEW: Optional Switch entity
) -> None:
    """Add switch item to lookahead with physical properties from Switch entity."""
    switch_state = track.switch_state if hasattr(track, "switch_state") else 0
    position = "main" if switch_state == 0 else "diverge"

    # Get length from Switch entity (per-branch)
    if switch_config:
        length_cm = switch_config.get_active_length(switch_state)
        radius_cm = switch_config.get_active_radius(switch_state)
        switch_id = switch_config.switch_id
    else:
        length_cm = 0.0
        radius_cm = None
        switch_id = None

    item = LookaheadItem(
        item_type=LookaheadItemType.SWITCH,
        distance_cm=distance_cm,
        sequence=sequence,
        data={
            "x": x,
            "y": y,
            "position": position,
            "length_cm": length_cm,
            "radius_cm": radius_cm,
            "switch_id": switch_id,
        },
    )
    result.lookahead_items.append(item)


def build_lookahead_response(
    train: "Train",
    network: "RailwayNetwork",
    max_distance_cm: float = 100000.0,
) -> Dict[str, Any]:
    result = build_lookahead(train, network, max_distance_cm=max_distance_cm)
    response = result.to_dict()

    blocks = []
    signals = []
    stations = []
    switches = []

    for item in result.lookahead_items:
        if item.item_type == LookaheadItemType.BLOCK:
            block_length = item.data.get("length_cm", 0)
            tile_count = item.data.get("tile_count", 1)
            blocks.append(
                {
                    "id": item.data.get("id"),
                    "name": item.data.get("name", item.data.get("id")),
                    "state": item.data.get("state", "unknown"),
                    "start_cm": item.distance_cm,
                    "end_cm": item.distance_cm + block_length,
                    "length_cm": block_length,
                    "tile_count": tile_count,
                    "sequence": item.sequence,
                }
            )
        elif item.item_type == LookaheadItemType.SIGNAL:
            signals.append(
                {
                    "id": item.data.get("id"),
                    "state": item.data.get("aspect"),
                    "distance_cm": item.distance_cm,
                    "sequence": item.sequence,
                }
            )
        elif item.item_type == LookaheadItemType.STATION:
            station_length = item.data.get("length_cm", 0.0)
            tile_count = item.data.get("tile_count", 1)
            stations.append(
                {
                    "station_id": item.data.get("id"),
                    "name": item.data.get("name"),
                    "distance_cm": item.distance_cm,
                    "length_cm": station_length,
                    "tile_count": tile_count,
                    "sequence": item.sequence,
                }
            )
        elif item.item_type == LookaheadItemType.SWITCH:
            switches.append(
                {
                    "x": item.data.get("x"),
                    "y": item.data.get("y"),
                    "position": item.data.get("position"),
                    "distance_cm": item.distance_cm,
                    "length_cm": item.data.get("length_cm", 0.0),
                    "sequence": item.sequence,
                }
            )

    response["blocks"] = blocks
    response["signals"] = signals
    response["stations"] = stations
    response["switches"] = switches
    response["max_distance_cm"] = result.clearance_cm or max_distance_cm
    response["wait_reason"] = train.wait_reason

    speed_limit_reason = _get_speed_limit_reason(train, network)
    response["speed_limit_reason"] = speed_limit_reason

    block_tile_counts, station_tile_counts = _precompute_tile_counts(network)
    distance_into_path_cm, distance_to_next_tile_cm = (
        _calculate_train_physical_position(
            train, network, block_tile_counts, station_tile_counts
        )
    )

    response["distance_into_path_cm"] = distance_into_path_cm
    response["distance_to_next_tile_cm"] = distance_to_next_tile_cm

    return response


def _get_speed_limit_reason(
    train: "Train",
    network: "RailwayNetwork",
) -> Optional[str]:
    """Get the reason for the current speed limit from the train's cached reason."""
    if train.target_speed >= train.max_speed:
        return None

    if train.target_speed <= 0:
        if train.wait_reason:
            return train.wait_reason
        return "Stopped"

    # Use the cached reason from TrainATC.get_speed_limit() (calculated in simulation.py)
    if train.speed_limit_reason:
        return train.speed_limit_reason

    # Fallback for edge cases where reason wasn't set
    if train.target_speed < train.max_speed:
        return f"Speed limited to {train.target_speed:.0f} km/h"

    return None


def _calculate_train_physical_position(
    train: "Train",
    network: "RailwayNetwork",
    block_tile_counts: Dict[str, int],
    station_tile_counts: Dict[str, int],
) -> Tuple[float, Optional[float]]:
    """
    Calculate train's physical position along the path and distance to next block.

    Uses per-tile calculation for the current block to ensure smooth position updates
    as train moves between tiles within the same block.
    """
    if not train.current_path or not train.current_pos:
        return 0.0, None

    cumulative_distance = 0.0
    seen_blocks: set = set()
    current_track = network.get_track(*train.current_pos)

    # Determine the block ID of the current tile (where the train head is)
    current_block_id = (
        getattr(current_track, "block_section_id", None) if current_track else None
    )

    # Sum distance for all tiles before current path_index
    for i in range(train.path_index):
        if i >= len(train.current_path):
            break
        px, py, _ = train.current_path[i]
        track = network.tracks.get((px, py))
        if not track:
            continue

        is_switch = hasattr(track, "is_switch") and track.is_switch
        item_added = False

        # Block handling
        if hasattr(track, "block_section_id") and track.block_section_id:
            block_id = track.block_section_id

            # Special handling for the block the train is currently in
            # Add per-tile length instead of full block length
            if block_id == current_block_id:
                block = network.blocks.get(block_id)
                if block and hasattr(block, "length_cm") and block.length_cm > 0:
                    tile_count = block_tile_counts.get(block_id, 1)
                    per_tile_len = block.length_cm / tile_count
                    cumulative_distance += per_tile_len
                item_added = True
            # Normal handling for completed blocks (not the current block)
            elif block_id not in seen_blocks:
                seen_blocks.add(block_id)
                block = network.blocks.get(block_id)
                if block and hasattr(block, "length_cm") and block.length_cm > 0:
                    cumulative_distance += block.length_cm
                item_added = True
            else:
                # Already seen block (and not current) - skip adding length
                item_added = True

        # Switch handling (only if not part of a block)
        if is_switch and not item_added:
            switch_config = (
                network.get_switch_at(px, py)
                if hasattr(network, "get_switch_at")
                else None
            )
            if switch_config:
                cumulative_distance += switch_config.get_active_length(
                    track.switch_state
                )
            item_added = True

        # Stations are just markers - don't add to cumulative distance
        # Plain track without block - length is 0 (unconfigured)

    # Now handle the current tile (where train head is)
    if current_track and current_block_id:
        block = network.blocks.get(current_block_id)
        if block and hasattr(block, "length_cm") and block.length_cm > 0:
            tile_count = block_tile_counts.get(current_block_id, 1)
            if tile_count > 0:
                per_tile_length = block.length_cm / tile_count

                # Fraction through current tile
                fraction = (
                    train.distance_on_track / per_tile_length
                    if per_tile_length > 0
                    else 0.0
                )
                fraction = max(0.0, min(1.0, fraction))

                distance_within_tile = fraction * per_tile_length
                cumulative_distance += distance_within_tile

                # Calculate remaining distance in block
                # Count how many tiles of this block remain after current position
                remaining_tiles = 0
                found_current = False
                for i in range(len(train.current_path)):
                    px, py, _ = train.current_path[i]
                    track_check = network.tracks.get((px, py))
                    if (
                        track_check
                        and getattr(track_check, "block_section_id", None)
                        == current_block_id
                    ):
                        if (px, py) == train.current_pos:
                            found_current = True
                        elif found_current:
                            remaining_tiles += 1

                remaining_in_tile = (1.0 - fraction) * per_tile_length
                distance_to_next_block = remaining_in_tile + (
                    remaining_tiles * per_tile_length
                )
                return cumulative_distance, distance_to_next_block

    # Return 0.0 for distance_to_next_tile when unconfigured (no block or no length)
    return cumulative_distance, 0.0


def _opposite_direction(direction: Direction) -> Direction:
    opposites = {
        Direction.NORTH: Direction.SOUTH,
        Direction.SOUTH: Direction.NORTH,
        Direction.EAST: Direction.WEST,
        Direction.WEST: Direction.EAST,
    }
    return opposites.get(direction, Direction.NORTH)


def _get_exit_edge(track, entry_edge: str) -> Optional[str]:
    if not hasattr(track, "track_bits") or track.track_bits == 0:
        return None

    from track import BIT_TO_EDGES

    exits = []
    for bit_idx in range(6):
        if track.track_bits & (1 << bit_idx):
            edge_a, edge_b = BIT_TO_EDGES[bit_idx]
            if edge_a == entry_edge:
                exits.append(edge_b)
            elif edge_b == entry_edge:
                exits.append(edge_a)

    if not exits:
        return None

    if len(exits) == 1:
        return exits[0]

    if hasattr(track, "switch_states") and entry_edge in track.switch_states:
        switch_idx = track.switch_states[entry_edge]
        if 0 <= switch_idx < len(exits):
            return exits[switch_idx]

    return exits[0]


def _collect_signals_on_tile(
    network: "RailwayNetwork",
    track,
    x: int,
    y: int,
    travel_edge: str,
    distance_cm: float,
    result: LookaheadResult,
    sequence: int,
    tile_length_cm: float = 0.0,
) -> None:
    """Collect signals on a tile and add them to lookahead.

    Args:
        distance_cm: Distance to tile START
        tile_length_cm: Physical length of this tile (for placing signal at middle)
    """
    if not hasattr(track, "signal_ids") or not track.signal_ids:
        return

    # Calculate signal position at tile middle
    signal_distance = distance_cm + (tile_length_cm / 2.0)

    for signal_id in track.signal_ids:
        signal = network.signals.get(signal_id)
        if not signal:
            continue

        if hasattr(signal, "detection_direction"):
            signal_edge = signal.detection_direction
            if signal_edge == travel_edge:
                item = LookaheadItem(
                    item_type=LookaheadItemType.SIGNAL,
                    distance_cm=signal_distance,
                    sequence=sequence,
                    data={
                        "id": signal_id,
                        "aspect": signal.state.value if signal.state else "unknown",
                        "direction": signal_edge,
                    },
                )
                result.lookahead_items.append(item)
