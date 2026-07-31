"""TrainPlacement — train placement geometry pulled out of `RailwayNetwork`.

Places a train on the grid (building the canonical `TrainSpan`), traces the
occupied tiles backward from the head, and exposes the per-tile "rich" segment
data the renderer uses. The calculator reads the network graph through the
back-reference `self._n`; `_get_tile_length_cm` and the entity dicts stay on
`RailwayNetwork`. `RailwayNetwork.place_train` / `get_occupied_tiles_rich` keep
thin forwarders so external callers (simulation, scenes, REST API, tests) are
unchanged.
"""
from __future__ import annotations

import random
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

from enums import Direction
from network import DEFAULT_TRAIN_LENGTH_CM
from track import OPPOSITE_EDGE

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from train_span import TrainSpan


class TrainPlacement:
    """Computes train placement + occupancy geometry for one `RailwayNetwork`."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    def place_train(
        self,
        train: "Train",
        tile_x: int,
        tile_y: int,
        direction: Optional[Direction] = None,
        *,
        head_offset_cm: float = 0.0,
        confine_to_blocks: Optional[Iterable[str]] = None,
    ) -> "TrainSpan":
        """
        Place a train on a tile with proper positioning based on direction and length.

        Train placement rules:
        1. Train HEAD (end_a) is placed `head_offset_cm` back from the EXIT edge
           of the specified tile — at the exit edge itself by default
        2. Train tail (end_b) extends backward - MUST fit on available track
        3. Builds the canonical TrainSpan with both physical ends tracked precisely
        4. Occupies all blocks the train covers

        Args:
            train: Train to place
            tile_x, tile_y: Target tile coordinates
            direction: Direction the train will be heading (random if None)
            head_offset_cm: How far back along the track from that exit edge the
                head sits. `0.0` (the default) is flush at the edge. Larger
                offsets walk backward through earlier tiles, so a caller can put
                the head anywhere on the track behind the exit edge instead of
                only at tile boundaries.
            confine_to_blocks: When given, the placed body must stay inside these
                blocks; a body reaching any other block is a `ValueError` raised
                before anything is occupied. Tiles belonging to no block (switch
                tiles carry no detection) are always allowed. Adoption passes the
                blocks the detector actually confirmed, so a declared consist can
                never claim track the detector reports clear.

        Returns:
            The canonical `TrainSpan` for the placed train (4-field schema).
            Callers seed `TrainController.physics_sync.current_span` from it
            (`add_train` / API re-placement).

        Raises:
            ValueError: If no track at position, if the head offset or the train
                doesn't fit on available track, or if the body leaves
                `confine_to_blocks`.
        """
        from train_span import SpanEndLocator, TrainSpan

        track = self._n.get_track(tile_x, tile_y)
        if not track:
            raise ValueError(f"No track at ({tile_x}, {tile_y})")

        # If no direction provided, pick one from available edges
        if direction is None:
            available_edges = list(track.edges)
            if not available_edges:
                raise ValueError(f"Track at ({tile_x}, {tile_y}) has no edges")
            edge = random.choice(available_edges)
            DIR_FROM_EDGE = {
                "N": Direction.NORTH,
                "E": Direction.EAST,
                "S": Direction.SOUTH,
                "W": Direction.WEST,
            }
            direction = DIR_FROM_EDGE[edge]

        DIR_TO_EDGE = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
        exit_edge = DIR_TO_EDGE.get(direction.name, "E")

        if exit_edge not in track.edges:
            raise ValueError(
                f"Track at ({tile_x}, {tile_y}) does not support direction {direction.name}"
            )

        neighbor = self._n.get_neighbor(tile_x, tile_y, exit_edge)
        if not neighbor:
            raise ValueError(
                f"Cannot place train facing {direction.name}: no track at the "
                f"neighbor of ({tile_x}, {tile_y}) via edge {exit_edge}"
            )

        train_length_cm = self._get_train_length_cm(train)
        tile_length_cm = self._n._get_tile_length_cm(tile_x, tile_y)

        # The entry edge is the OTHER end of the rail segment that has
        # `exit_edge` as its exit — NOT the strict cardinal opposite. For a
        # NW corner tile (N+W edges), exit=N means the train is sitting on
        # the N→W rail and the body extends through the W edge; using
        # OPPOSITE_EDGE[N]=S here would point at an edge the tile does not
        # actually have. `get_entry_for_exit` respects rail topology and
        # switch state.
        entry_edge = track.get_entry_for_exit(exit_edge, use_switch_state=True)
        if entry_edge is None:
            raise ValueError(
                f"Track at ({tile_x}, {tile_y}) has no rail segment that exits via {exit_edge}; "
                f"cannot place train facing {direction.name}."
            )

        # Head is at exit edge, so head_distance = tile_length
        head_tile = (tile_x, tile_y)
        head_distance_cm = tile_length_cm
        head_entry_edge = entry_edge

        # A head offset slides the head back along the track from that exit
        # edge. Walking it is the same backward trace the body uses, so an
        # offset longer than this tile lands correctly on an earlier one and an
        # offset with no track behind it raises the same "does not fit" error.
        if head_offset_cm > 0:
            (_, head_tile, head_distance_cm, head_entry_edge) = (
                self._calculate_occupied_tiles(
                    head_tile, head_distance_cm, head_entry_edge, head_offset_cm
                )
            )

        # Calculate occupied tiles and tail position
        # This will raise ValueError if train doesn't fit
        (occupied_tiles, tail_tile, tail_distance_cm, tail_entry_edge) = (
            self._calculate_occupied_tiles(
                head_tile, head_distance_cm, head_entry_edge, train_length_cm
            )
        )

        # Calculate occupied blocks from occupied tiles
        occupied_blocks = []
        seen_blocks = set()
        for tile in occupied_tiles:
            tile_track = self._n.get_track(*tile)
            if tile_track and tile_track.block_id:
                block_id = tile_track.block_id
                if block_id not in seen_blocks:
                    occupied_blocks.append(block_id)
                    seen_blocks.add(block_id)

        # Checked before the span is built and before anything is occupied, so a
        # body that reaches too far leaves the caller's world untouched.
        if confine_to_blocks is not None:
            allowed: Set[str] = set(confine_to_blocks)
            outside = [b for b in occupied_blocks if b not in allowed]
            if outside:
                raise ValueError(
                    f"A {train_length_cm:.0f} cm train with its front on tile "
                    f"{head_tile} facing {direction.name} reaches into "
                    f"{', '.join(sorted(outside))}, outside the track it may "
                    f"occupy ({', '.join(sorted(allowed)) or 'none'})."
                )

        # Build the canonical 4-field TrainSpan (design_new.md TrainSpan
        # schema). `_calculate_occupied_tiles` returns tiles tail-to-head; the
        # span orders `occupied_tiles` end_a -> end_b, and end_a is the
        # placement (head) tile, so reverse to head-to-tail (end_a -> end_b).
        occupied_tiles_end_a_to_b = list(reversed(occupied_tiles))
        span = TrainSpan(
            end_a=SpanEndLocator(
                tile=head_tile,
                distance_cm=head_distance_cm,
                entry_edge=head_entry_edge,
            ),
            end_b=SpanEndLocator(
                tile=tail_tile,
                distance_cm=tail_distance_cm,
                entry_edge=tail_entry_edge,
            ),
            occupied_tiles=occupied_tiles_end_a_to_b,
            occupied_blocks=occupied_blocks,
        )

        # Occupy all blocks the train covers
        if self._n.block_manager and occupied_blocks:
            self._n.block_manager.occupy_blocks(occupied_blocks, train.train_id)
        else:
            for block_id in occupied_blocks:
                block = self._n.blocks.get(block_id)
                if block:
                    block.occupy(train.train_id)

        return span

    def _get_train_length_cm(self, train: "Train") -> float:
        """Get train length in cm."""
        if train.consist:
            total_length = sum(car.length_cm for car in train.consist)
            return total_length
        return DEFAULT_TRAIN_LENGTH_CM

    def _calculate_occupied_tiles(
        self,
        head_tile: Tuple[int, int],
        head_distance_cm: float,
        head_entry_edge: str,
        train_length_cm: float,
        path: Optional[List[Tuple[int, int, Direction]]] = None,
        path_index: int = 0,
    ) -> Tuple[List[Tuple[int, int]], Tuple[int, int], float, str]:
        """
        Calculate all tiles occupied by a train given its head position and length.

        Traces backward from head position through connected tiles to find where
        the tail (end_b) is located.

        Args:
            head_tile: (x, y) coordinates of tile where head is located
            head_distance_cm: Distance from entry edge to head on that tile (cm)
            head_entry_edge: Edge where head entered the tile (N/E/S/W)
            train_length_cm: Total train length (cm)
            path: Optional train path for correct backward tracing through switches
            path_index: Current index in path (head position)

        Returns:
            Tuple of:
            - occupied_tiles: List of (x, y) from tail to head
            - tail_tile: (x, y) coordinates where tail is located
            - tail_distance_cm: Distance from entry edge to tail on tail tile
            - tail_entry_edge: Edge where tail tile was entered from

        Raises:
            ValueError: If train is too long for available track
        """
        DIR_TO_EDGE = {
            Direction.NORTH: "N",
            Direction.SOUTH: "S",
            Direction.EAST: "E",
            Direction.WEST: "W",
        }

        # Build path lookup: tile -> (entry_edge, exit_direction)
        # This helps us trace backward correctly through switches
        path_lookup: Dict[Tuple[int, int], str] = {}
        if path:
            for i, (px, py, pdir) in enumerate(path):
                if pdir and i <= path_index:
                    # Exit direction -> entry is opposite
                    exit_edge = DIR_TO_EDGE.get(pdir, "N")
                    entry_edge = OPPOSITE_EDGE.get(exit_edge, "S")
                    path_lookup[(px, py)] = entry_edge

        occupied = []
        accumulated_length = 0.0

        current_tile = head_tile
        current_distance = head_distance_cm
        current_entry_edge = head_entry_edge

        # First, account for the portion of the head tile behind the head
        head_tile_length = self._n._get_tile_length_cm(*head_tile)
        length_behind_head = current_distance  # Distance from entry to head

        occupied.append(current_tile)
        accumulated_length += length_behind_head

        if accumulated_length >= train_length_cm:
            # Tail is on same tile as head
            tail_distance = current_distance - train_length_cm
            return (occupied, current_tile, tail_distance, current_entry_edge)

        # Trace backward through tiles
        visited = {current_tile}
        max_iterations = 1000  # Safety limit

        for _ in range(max_iterations):
            # Get previous tile (in direction opposite to entry)
            backward_edge = current_entry_edge
            prev_track = self._n.get_neighbor(*current_tile, backward_edge)

            if not prev_track:
                # No track behind - train extends off layout
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"No track behind {current_tile} via edge {backward_edge}."
                )
            prev_coords = (prev_track.x, prev_track.y)

            # Check connectivity
            opposite_edge = OPPOSITE_EDGE[backward_edge]
            current_track = self._n.get_track(*current_tile)
            if not (
                backward_edge in current_track.edges
                and opposite_edge in prev_track.edges
            ):
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"Tiles {current_tile} and {prev_coords} are not connected."
                )

            if prev_coords in visited:
                # Circular track - train wraps around
                available = accumulated_length
                raise ValueError(
                    f"Train length {train_length_cm}cm exceeds available track {available}cm. "
                    f"Circular track detected at {prev_coords}."
                )

            visited.add(prev_coords)

            # Get length of previous tile
            prev_tile_length = self._n._get_tile_length_cm(*prev_coords)

            # Check if tail is in this previous tile
            remaining_length = train_length_cm - accumulated_length

            if remaining_length <= prev_tile_length:
                # Tail is in this tile. The train exited prev_tile through
                # opposite_edge to reach current_tile, so the entry edge is
                # whichever edge has opposite_edge as its exit per the tile's
                # rail topology — NOT the cardinal opposite, which is wrong
                # for corner tiles (NW/SE/NE/SW).
                if prev_coords in path_lookup:
                    tail_entry_edge = path_lookup[prev_coords]
                else:
                    tail_entry_edge = prev_track.get_entry_for_exit(
                        opposite_edge, use_switch_state=True
                    ) or opposite_edge
                tail_distance = prev_tile_length - remaining_length

                occupied.insert(0, prev_coords)

                return (occupied, prev_coords, tail_distance, tail_entry_edge)

            # Continue to next previous tile
            accumulated_length += prev_tile_length
            occupied.insert(0, prev_coords)

            # Update for next iteration
            current_tile = prev_coords

            # Determine entry edge for prev_tile
            # The train exited prev_tile through opposite_edge to reach current_tile.
            # We need to find: which edge did the train ENTER prev_tile from?
            #
            # For switches: use get_entry_for_exit() which respects switch state
            # For simple track: use get_entry_for_exit() as well (works for all tracks)
            #
            # First try path lookup (most accurate for switches with path info)
            if prev_coords in path_lookup:
                current_entry_edge = path_lookup[prev_coords]
            else:
                # Use the track's get_entry_for_exit method which respects switch state
                entry_edge = prev_track.get_entry_for_exit(
                    opposite_edge, use_switch_state=True
                )
                if entry_edge:
                    current_entry_edge = entry_edge
                else:
                    # Fallback: if no valid entry found (shouldn't happen for valid paths)
                    current_entry_edge = opposite_edge

        # Should never reach here
        raise ValueError(
            f"Unable to calculate tail position after {max_iterations} iterations"
        )

    def get_occupied_tiles_rich(
        self, span: "TrainSpan", head_is_a: bool
    ) -> List[Dict[str, Any]]:
        """
        Get rich information about all tiles occupied by a train.

        Returns detailed data for each tile the train occupies, useful for
        rendering train segments with correct proportions and block colors.

        Args:
            span: The train's `TrainSpan` (occupied_tiles ordered end_a -> end_b).
            head_is_a: True when the head end is end_a (cab "A"); False when it
                is end_b (cab "B"). Derived on the controller from
                (cab, current_span).

        Returns:
            List of dicts (ordered from end_a to end_b), each containing:
            - tile: (x, y) coordinates
            - block_id: Block ID if tile is in a block, else None
            - segment_start_cm: Distance from entry edge where train starts on this tile
            - segment_end_cm: Distance from entry edge where train ends on this tile
            - tile_length_cm: Total length of this tile
        """
        if span is None:
            return []

        occupied_tiles = span.occupied_tiles
        if not occupied_tiles:
            return []

        result = []
        num_tiles = len(occupied_tiles)

        # occupied_tiles is ordered from end_a to end_b. The head end is end_a
        # when head_is_a (cab "A"), else end_b.
        is_head_first = head_is_a
        head_distance = (span.end_a if head_is_a else span.end_b).distance_cm
        tail_distance = (span.end_b if head_is_a else span.end_a).distance_cm

        for i, tile in enumerate(occupied_tiles):
            tile_track = self._n.get_track(*tile)
            tile_length = self._n._get_tile_length_cm(*tile)

            block_id = tile_track.block_id if tile_track else None

            # Determine segment boundaries based on position in list
            is_first = i == 0
            is_last = i == num_tiles - 1

            if is_first and is_last:
                # Single tile - both ends on the same tile. The head/tail
                # in-tile offsets are symmetric here, so orientation does not
                # change the segment span.
                segment_start = tail_distance
                segment_end = head_distance
            elif is_first:
                # First tile in list (end_a's tile)
                if is_head_first:
                    # end_a is head - this is head tile
                    segment_start = 0.0
                    segment_end = head_distance
                else:
                    # end_a is tail - this is tail tile
                    segment_start = tail_distance
                    segment_end = tile_length
            elif is_last:
                # Last tile in list (end_b's tile)
                if is_head_first:
                    # end_b is tail - this is tail tile
                    segment_start = tail_distance
                    segment_end = tile_length
                else:
                    # end_b is head - this is head tile
                    segment_start = 0.0
                    segment_end = head_distance
            else:
                # Middle tile - train occupies entire tile
                segment_start = 0.0
                segment_end = tile_length

            result.append(
                {
                    "tile": tile,
                    "block_id": block_id,
                    "segment_start_cm": segment_start,
                    "segment_end_cm": segment_end,
                    "tile_length_cm": tile_length,
                }
            )

        return result
