from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, TYPE_CHECKING
from enum import Enum

from train_config import DEFAULT_TILE_LENGTH_CM
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
    if hasattr(track, "is_switch") and track.is_switch:
        return 0.0

    if hasattr(track, "block_section_id") and track.block_section_id:
        block_id = track.block_section_id
        block = network.blocks.get(block_id)
        if block and hasattr(block, "length_cm"):
            if block.length_cm == 0:
                return 0.0
            tile_count = block_tile_counts.get(block_id, 1)
            if tile_count <= 0:
                tile_count = 1
            return block.length_cm / tile_count
        return DEFAULT_TILE_LENGTH_CM

    if hasattr(track, "station_id") and track.station_id:
        station_id = track.station_id
        station = network.stations.get(station_id)
        if station and hasattr(station, "length_cm"):
            if station.length_cm == 0:
                return 0.0
            tile_count = station_tile_counts.get(station_id, 1)
            if tile_count <= 0:
                tile_count = 1
            return station.length_cm / tile_count
        return DEFAULT_TILE_LENGTH_CM

    return DEFAULT_TILE_LENGTH_CM


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
) -> float:
    if not train.current_path:
        return 0.0

    physical_distance = 0.0

    for i in range(train.path_index):
        if i >= len(train.current_path):
            break
        px, py, _ = train.current_path[i]
        track = network.tracks.get((px, py))
        if track:
            tile_length = _get_tile_physical_length(
                network, track, block_tile_counts, station_tile_counts
            )
            physical_distance += tile_length

    return physical_distance


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

    train_absolute_cm = _calculate_absolute_position_to_tile_start(
        train, network, block_tile_counts, station_tile_counts
    )

    physical_distance = train_absolute_cm
    visited = set()
    x, y = current_x, current_y
    entry_edge = OPPOSITE_EDGE.get(travel_edge, "S")

    seen_blocks: Dict[str, float] = {}
    seen_stations: Dict[str, float] = {}
    sequence_counter = 0
    cumulative_distance = 0.0

    while cumulative_distance < max_distance_cm:
        if (x, y) in visited:
            break
        visited.add((x, y))

        track = network.tracks.get((x, y))
        if not track:
            break

        is_switch = hasattr(track, "is_switch") and track.is_switch

        _collect_signals_on_tile(
            network,
            track,
            x,
            y,
            travel_edge,
            cumulative_distance,
            result,
            sequence_counter,
        )

        item_added_this_tile = False

        if hasattr(track, "block_section_id") and track.block_section_id:
            block_id = track.block_section_id
            if block_id not in seen_blocks:
                seen_blocks[block_id] = cumulative_distance
                sequence_counter += 1
                block = network.blocks.get(block_id)
                block_length = 0.0
                if block and hasattr(block, "length_cm"):
                    block_length = block.length_cm
                else:
                    tile_count = block_tile_counts.get(block_id, 1)
                    block_length = tile_count * DEFAULT_TILE_LENGTH_CM

                _add_block_item(
                    network,
                    block_id,
                    cumulative_distance,
                    block_tile_counts,
                    result,
                    sequence_counter,
                )
                cumulative_distance += block_length
                item_added_this_tile = True

        if hasattr(track, "station_id") and track.station_id:
            station_id = track.station_id
            if station_id not in seen_stations:
                seen_stations[station_id] = cumulative_distance
                sequence_counter += 1
                station = network.stations.get(station_id)
                station_length = 0.0
                if station and hasattr(station, "length_cm"):
                    station_length = station.length_cm
                else:
                    tile_count = station_tile_counts.get(station_id, 1)
                    station_length = tile_count * DEFAULT_TILE_LENGTH_CM

                _add_station_item(
                    network,
                    station_id,
                    cumulative_distance,
                    station_tile_counts,
                    result,
                    sequence_counter,
                )
                cumulative_distance += station_length
                item_added_this_tile = True

        if is_switch:
            sequence_counter += 1
            _add_switch_item(track, x, y, cumulative_distance, result, sequence_counter)
            cumulative_distance += 1.0
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
    block = network.blocks.get(block_id)
    if not block:
        return

    tile_count = block_tile_counts.get(block_id, 1)

    if hasattr(block, "length_cm"):
        length_cm = block.length_cm
    else:
        length_cm = tile_count * DEFAULT_TILE_LENGTH_CM

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
    station = network.stations.get(station_id)
    if not station:
        return

    tile_count = station_tile_counts.get(station_id, 1)

    if hasattr(station, "length_cm"):
        length_cm = station.length_cm
    else:
        length_cm = tile_count * DEFAULT_TILE_LENGTH_CM

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
) -> None:
    switch_state = track.switch_state if hasattr(track, "switch_state") else 0
    position = "main" if switch_state == 0 else "diverging"

    item = LookaheadItem(
        item_type=LookaheadItemType.SWITCH,
        distance_cm=distance_cm,
        sequence=sequence,
        data={
            "x": x,
            "y": y,
            "position": position,
            "length_cm": 0.0,
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
                    "length_cm": 0.0,
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
    if not train.current_path or not train.current_pos:
        return 0.0, None

    cumulative_distance = 0.0
    seen_blocks = set()
    seen_stations = set()
    current_track = network.get_track(*train.current_pos)

    for i in range(train.path_index):
        if i >= len(train.current_path):
            break
        px, py, _ = train.current_path[i]
        track = network.tracks.get((px, py))
        if not track:
            continue

        item_added = False

        if hasattr(track, "block_section_id") and track.block_section_id:
            block_id = track.block_section_id
            if block_id not in seen_blocks:
                seen_blocks.add(block_id)
                block = network.blocks.get(block_id)
                if block and hasattr(block, "length_cm"):
                    cumulative_distance += block.length_cm
                else:
                    tile_count = block_tile_counts.get(block_id, 1)
                    cumulative_distance += tile_count * DEFAULT_TILE_LENGTH_CM
                item_added = True

        elif hasattr(track, "station_id") and track.station_id:
            station_id = track.station_id
            if station_id not in seen_stations:
                seen_stations.add(station_id)
                station = network.stations.get(station_id)
                if station and hasattr(station, "length_cm"):
                    cumulative_distance += station.length_cm
                else:
                    tile_count = station_tile_counts.get(station_id, 1)
                    cumulative_distance += tile_count * DEFAULT_TILE_LENGTH_CM
                item_added = True

        elif hasattr(track, "is_switch") and track.is_switch:
            cumulative_distance += 1.0
            item_added = True

        if not item_added:
            tile_length = _get_tile_physical_length(
                network, track, block_tile_counts, station_tile_counts
            )
            cumulative_distance += tile_length

    if current_track:
        current_block_id = getattr(current_track, "block_section_id", None)
        if current_block_id and current_block_id not in seen_blocks:
            block = network.blocks.get(current_block_id)
            if block and hasattr(block, "length_cm") and block.length_cm > 0:
                tile_count = block_tile_counts.get(current_block_id, 1)
                if tile_count > 0:
                    current_tile_index = 0
                    for i in range(train.path_index, len(train.current_path)):
                        px, py, _ = train.current_path[i]
                        track_in_block = network.tracks.get((px, py))
                        if (
                            track_in_block
                            and hasattr(track_in_block, "block_section_id")
                            and track_in_block.block_section_id == current_block_id
                        ):
                            if (px, py) == train.current_pos:
                                break
                            current_tile_index += 1

                    tiles_before_current = current_tile_index
                    per_tile_length = block.length_cm / tile_count
                    distance_to_current_tile_start = (
                        tiles_before_current * per_tile_length
                    )

                    fraction = (
                        train.distance_on_track / per_tile_length
                        if per_tile_length > 0
                        else 0.0
                    )
                    fraction = max(0.0, min(1.0, fraction))

                    distance_within_tile = fraction * per_tile_length
                    cumulative_distance += (
                        distance_to_current_tile_start + distance_within_tile
                    )

                    remaining_in_tile = (1.0 - fraction) * per_tile_length
                    remaining_tiles = tile_count - current_tile_index - 1
                    distance_to_next_block = remaining_in_tile + (
                        remaining_tiles * per_tile_length
                    )
                    return cumulative_distance, distance_to_next_block
            else:
                tile_length = _get_tile_physical_length(
                    network, current_track, block_tile_counts, station_tile_counts
                )
                # distance_on_track is now in cm, tile_length is also in cm
                fraction = (
                    train.distance_on_track / tile_length if tile_length > 0 else 0.0
                )
                fraction = max(0.0, min(1.0, fraction))
                cumulative_distance += fraction * tile_length
                distance_to_next_tile_cm = (1.0 - fraction) * tile_length
                return cumulative_distance, distance_to_next_tile_cm
        else:
            tile_length = _get_tile_physical_length(
                network, current_track, block_tile_counts, station_tile_counts
            )
            # distance_on_track is now in cm, tile_length is also in cm
            fraction = train.distance_on_track / tile_length if tile_length > 0 else 0.0
            fraction = max(0.0, min(1.0, fraction))
            cumulative_distance += fraction * tile_length
            distance_to_next_tile_cm = (1.0 - fraction) * tile_length
            return cumulative_distance, distance_to_next_tile_cm

    return cumulative_distance, None


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
) -> None:
    if not hasattr(track, "signal_ids") or not track.signal_ids:
        return

    for signal_id in track.signal_ids:
        signal = network.signals.get(signal_id)
        if not signal:
            continue

        if hasattr(signal, "detection_direction"):
            signal_edge = signal.detection_direction
            if signal_edge == travel_edge:
                item = LookaheadItem(
                    item_type=LookaheadItemType.SIGNAL,
                    distance_cm=distance_cm,
                    sequence=sequence,
                    data={
                        "id": signal_id,
                        "aspect": signal.state.value if signal.state else "unknown",
                        "direction": signal_edge,
                    },
                )
                result.lookahead_items.append(item)
