from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, TYPE_CHECKING
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


@dataclass
class LookaheadItem:
    item_type: LookaheadItemType
    distance_cm: float
    data: Dict[str, Any] = field(default_factory=dict)

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

    distance_traveled = 0.0
    visited = set()
    x, y = current_x, current_y
    entry_edge = OPPOSITE_EDGE.get(travel_edge, "S")

    while distance_traveled < max_distance_cm:
        if (x, y) in visited:
            break
        visited.add((x, y))

        track = network.tracks.get((x, y))
        if not track:
            break

        tile_distance = DEFAULT_TILE_LENGTH_CM

        _collect_signals_on_tile(
            network, track, x, y, entry_edge, distance_traveled, result
        )

        _collect_block_info(network, track, distance_traveled, result)

        _collect_station_info(network, track, distance_traveled, result)

        exit_edge = _get_exit_edge(track, entry_edge)
        if not exit_edge:
            break

        distance_traveled += tile_distance

        dx, dy = EDGE_OFFSETS.get(exit_edge, (0, 0))
        x, y = x + dx, y + dy
        entry_edge = OPPOSITE_EDGE.get(exit_edge, "S")

    result.clearance_cm = distance_traveled

    return result


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
    entry_edge: str,
    distance_cm: float,
    result: LookaheadResult,
) -> None:
    if not hasattr(track, "signal_ids") or not track.signal_ids:
        return

    for signal_id in track.signal_ids:
        signal = network.signals.get(signal_id)
        if not signal:
            continue

        if hasattr(signal, "detection_direction"):
            signal_edge = signal.detection_direction
            if signal_edge == entry_edge:
                item = LookaheadItem(
                    item_type=LookaheadItemType.SIGNAL,
                    distance_cm=distance_cm,
                    data={
                        "id": signal_id,
                        "aspect": signal.state.value if signal.state else "unknown",
                        "direction": signal_edge,
                    },
                )
                result.lookahead_items.append(item)


def _collect_block_info(
    network: "RailwayNetwork",
    track,
    distance_cm: float,
    result: LookaheadResult,
) -> None:
    if not hasattr(track, "block_section_id") or not track.block_section_id:
        return

    block_id = track.block_section_id

    for item in result.lookahead_items:
        if (
            item.item_type == LookaheadItemType.BLOCK
            and item.data.get("id") == block_id
        ):
            return

    block = network.blocks.get(block_id)
    if not block:
        return

    item = LookaheadItem(
        item_type=LookaheadItemType.BLOCK,
        distance_cm=distance_cm,
        data={
            "id": block_id,
            "state": block.state.value if block.state else "unknown",
            "length_cm": block.length_cm if hasattr(block, "length_cm") else 0.0,
        },
    )
    result.lookahead_items.append(item)


def _collect_station_info(
    network: "RailwayNetwork",
    track,
    distance_cm: float,
    result: LookaheadResult,
) -> None:
    if not hasattr(track, "station_id") or not track.station_id:
        return

    station_id = track.station_id

    for item in result.lookahead_items:
        if (
            item.item_type == LookaheadItemType.STATION
            and item.data.get("id") == station_id
        ):
            return

    station = network.stations.get(station_id)
    if not station:
        return

    item = LookaheadItem(
        item_type=LookaheadItemType.STATION,
        distance_cm=distance_cm,
        data={
            "id": station_id,
            "name": station.name if hasattr(station, "name") else station_id,
            "platform_side": "right",
        },
    )
    result.lookahead_items.append(item)
