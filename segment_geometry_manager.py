"""
SegmentGeometryManager — owns Block topology / geometry mutation pulled out of
`RailwayNetwork`.

Per design_new.md, `Block` is a data container; `BlockManager` is the sole
mutator for runtime state (`occupied_by`, `_is_caution`, `reservation`);
this module is the sole writer of geometry / topology fields
(`length_cm`, `speed_limit_kmh`, `ordered_tiles`) and of
`track.segment_id` for block lifecycle. The `RailwayNetwork` keeps
the doc-canonical method names as forwarders so existing callers (UI,
scenario setup, tests) continue to work without change.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Callable,
    Collection,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)

from block import Block
from entity_ids import block_id_for_segment, segment_id_for_block
from railway_signal import SignalState, SignalType
from segment import Segment, SegmentTileEntry

if TYPE_CHECKING:
    from network import RailwayNetwork
    from railway_signal import Signal


class SegmentGeometryManager:
    """Owns block topology / geometry mutation for one `RailwayNetwork`."""

    _EDGE_OFFSETS = {"N": (0, 1), "E": (1, 0), "S": (0, -1), "W": (-1, 0)}
    _OPPOSITE_EDGE = {"N": "S", "S": "N", "E": "W", "W": "E"}

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    # ------------------------------------------------------------------
    # ordered_tiles maintenance
    # ------------------------------------------------------------------

    def rebuild_segment_ordered_tiles(self, segment_id: str) -> None:
        """Recompute `Segment.ordered_tiles` from the segment's current tiles.

        Per design_new.md "segment": `ordered_tiles` is a list of
        `(x, y, entry_edge)` entries giving the canonical local traversal
        order. Each `entry_edge` is the external edge through which forward
        traversal enters that tile. `ordered_tiles[0]` is the canonical
        forward-entry anchor.

        Every geometry mutation that adds or removes a tile from a segment
        must call this so consumers (path completion direction check,
        block-direction helper, etc.) see the live order.

        This works on exactly one segment, so a block that groups several
        segments rebuilds correctly: each member is walked over its own tiles
        and written back to itself, and the block's `ordered_tiles` read stays
        the concatenation of its members.
        """
        from segment import SegmentTileEntry

        segment = self._n.get_segment(segment_id)
        if segment is None:
            return

        # `ordered_tiles` feeds the cached per-block traversal direction
        # (`block_direction`) in the pathfinding walk cache, so recomputing it
        # invalidates those cached walks. Editor/build-only; never in the tick.
        self._n.invalidate_pathfinding_cache()

        in_block = []
        for (x, y), track in self._n.tracks.items():
            if track is None or track.is_switch:
                continue
            if getattr(track, "segment_id", None) == segment_id:
                in_block.append((x, y))

        if not in_block:
            segment.ordered_tiles = []
            return

        # Build adjacency over same-block neighbours.
        in_block_set = set(in_block)
        neighbours = {t: [] for t in in_block}
        for tile in in_block:
            x, y = tile
            for edge, (dx, dy) in self._EDGE_OFFSETS.items():
                nbr = (x + dx, y + dy)
                if nbr in in_block_set:
                    neighbours[tile].append((nbr, edge))

        # Pick a deterministic starting endpoint: the tile with the fewest
        # in-block neighbours, breaking ties by sorted coordinate. For a
        # linear block this finds an end; for a single-tile block it picks
        # that tile.
        start = sorted(in_block, key=lambda t: (len(neighbours[t]), t))[0]

        # Walk from the start, recording each tile and the edge the train
        # entered it through.
        ordered = [start]
        prev = None
        while True:
            current = ordered[-1]
            options = [n for (n, _e) in neighbours[current] if n != prev]
            if not options:
                break
            # Deterministic next pick when a tile has multiple options
            # (only happens for non-linear members or branchy blocks; v1
            # validation forbids those, but stay safe here).
            next_tile = sorted(options)[0]
            ordered.append(next_tile)
            prev = current

        # Compute entry_edges.
        entries = []
        for i, tile in enumerate(ordered):
            if i == 0:
                entry_edge = self._external_entry_edge(tile, in_block_set)
            else:
                prev_tile = ordered[i - 1]
                dx = tile[0] - prev_tile[0]
                dy = tile[1] - prev_tile[1]
                edge_from_prev = self._edge_for_delta(dx, dy)
                entry_edge = self._OPPOSITE_EDGE.get(edge_from_prev or "", "") or ""
            entries.append(
                SegmentTileEntry(x=tile[0], y=tile[1], entry_edge=entry_edge)
            )
        segment.ordered_tiles = entries

    def reverse_segment_orientation(self, segment_id: str) -> bool:
        """Flip a segment's stored tile order end-for-end.

        `Segment.ordered_tiles` is built per segment from a deterministic
        coordinate rule, so two segments that sit next to each other on the
        layout can be stored running towards each other. Grouping them into one
        block needs every member to run the same way (design_new.md **Block
        grouping**: member orientations all align with increasing block order,
        and grouping itself never reverses a segment). This is the geometry-side
        write that makes them align, so grouping can keep its no-reversal rule.

        Each `entry_edge` is recomputed for the new direction. Returns False
        when the segment is unknown, has fewer than two tiles, or is not the
        sole member of BOTH its owning `Block` and any `Platform` that contains
        it (design_new.md "Segment", `reverse_segment_orientation`: reversing one
        member of a multi-segment block or platform would corrupt that parent's
        stored orientation).
        """
        segment = self._n.get_segment(segment_id)
        if segment is None or len(segment.ordered_tiles) < 2:
            return False

        # Reject unless the segment is the sole member of its owning Block: a
        # local reversal of one member would break the stored orientation the
        # rest of the block is grouped against (design_new.md "Segment",
        # reverse_segment_orientation).
        block_id = self._n.segment_to_block_id(segment_id)
        if block_id is not None:
            block = self._n.blocks.get(block_id)
            if block is not None and len(block.segment_ids) > 1:
                return False

        # Reject likewise unless the segment is the sole member of any Platform
        # that contains it (design_new.md "Segment", reverse_segment_orientation
        # requires sole membership of BOTH the owning Block and any containing
        # Platform). A multi-segment platform stores its segments in one ordered
        # run, and reversing one member would break the order the others are
        # ranked against for platform-end selection. Platform membership is read
        # live through the tile -> segment -> platform chain, so a platform whose
        # single segment was just split into several children is correctly seen
        # as multi-segment here.
        platform_id = self._n.segment_to_platform_id(segment_id)
        if platform_id is not None:
            platform = self._n.platforms.get(platform_id)
            if platform is not None and len(platform.ordered_segments(self._n)) > 1:
                return False

        tiles = [(e.x, e.y) for e in reversed(segment.ordered_tiles)]
        member_set = set(tiles)
        entries = []
        for i, tile in enumerate(tiles):
            if i == 0:
                entry_edge = self._external_entry_edge(tile, member_set)
            else:
                prev_tile = tiles[i - 1]
                edge_from_prev = self._edge_for_delta(
                    tile[0] - prev_tile[0], tile[1] - prev_tile[1]
                )
                entry_edge = self._OPPOSITE_EDGE.get(edge_from_prev or "", "") or ""
            entries.append(
                SegmentTileEntry(x=tile[0], y=tile[1], entry_edge=entry_edge)
            )
        segment.ordered_tiles = entries
        self._n.invalidate_pathfinding_cache()
        return True

    def _external_entry_edge(
        self, tile: Tuple[int, int], in_block_set: set
    ) -> str:
        """Pick the entry edge for the first tile in `ordered_tiles`.

        Forward traversal enters the block through this tile's track edge that
        does NOT face another tile in the same block. The choice is made only
        from the tile's real track edges, so we never name an edge the tile's
        geometry lacks: for a linear block's endpoint exactly one edge is
        external; for a single-tile block (both edges external) we break ties
        by compass order. Returns "" only when the tile has no track edges.
        """
        x, y = tile
        track = self._n.get_track(x, y)
        if track is None:
            return ""
        tile_edges = sorted(
            e
            for e in (str(p).upper() for p in track.edges)
            if e in self._EDGE_OFFSETS
        )
        external = [
            e
            for e in tile_edges
            if (x + self._EDGE_OFFSETS[e][0], y + self._EDGE_OFFSETS[e][1])
            not in in_block_set
        ]
        if external:
            return external[0]
        return tile_edges[0] if tile_edges else ""

    def _edge_for_delta(self, dx: int, dy: int) -> Optional[str]:
        if dx == 0 and dy > 0:
            return "N"
        if dx == 0 and dy < 0:
            return "S"
        if dy == 0 and dx > 0:
            return "E"
        if dy == 0 and dx < 0:
            return "W"
        return None

    def create_segment(
        self, segment_id: str, name: str = "", block_id: Optional[str] = None
    ) -> Block:
        """
        Create a new empty segment and the block that owns it.

        A fresh segment is not part of any grouping, so it gets its own block
        under the default one-feed-per-segment rule. That block's id is the
        segment's id derived onto the block axis (`entity_ids`), never the same
        string: the two are separate entities and one id naming both is
        ambiguous wherever either is displayed or bound. Grouping several
        segments under one block is a separate operation on
        `BlockGroupingManager`.

        `name` names the SEGMENT — this is the geometry-creating call, and its
        name is what the segment editor edits. The owning block is created
        unnamed and is named separately (the Blocks panel, `create_block`), so
        the two records never carry one name between them.

        Args:
            segment_id: Unique identifier for the segment
            name: Optional human-readable name for the new segment
            block_id: Pin the owning block's id instead of deriving it. For
                callers replaying ids that outside records already reference —
                a baked scene whose block ids appear in saved routes and in
                `hardware_config.json` bindings. Derivation is not reversible
                for a legacy id that predates the two-axis split, so those
                callers state the block id rather than round-tripping it.

        Returns:
            The owning Block. Callers configure geometry (length, speed limit)
            and block policy (holding point, disabled) through it; its geometry
            properties delegate to the single member segment.

        Raises:
            ValueError: If segment_id or the owning block's id already exists
        """
        resolved_block_id = block_id or block_id_for_segment(segment_id)
        if resolved_block_id in self._n.blocks:
            raise ValueError(f"Block {resolved_block_id} already exists")
        if self._n.get_segment(segment_id) is not None:
            raise ValueError(f"Segment {segment_id} already exists")
        block = Block(block_id=resolved_block_id, segment_id=segment_id)
        block.segments[0].name = name
        self._n.blocks[resolved_block_id] = block
        self._n._ensure_resource_managers()
        return block

    def create_segment_with_tracks(
        self,
        segment_id: str,
        positions: List[Tuple[int, int]],
        name: str = "",
        length_cm: float = 0.0,
        speed_limit_kmh: Optional[float] = None,
        validate: bool = True,
        block_id: Optional[str] = None,
    ) -> Block:
        """
        Create a segment holding `positions`, with full validation.

        This is the RECOMMENDED way to lay down track geometry. Validates:
        - Segment ID uniqueness
        - Segment properties are valid
        - All positions have valid tracks
        - No tracks are switches, signals, or stations
        - No tracks are already in another segment
        - All tracks form a continuous path

        Args:
            segment_id: Unique segment identifier
            positions: List of (x, y) track positions
            name: Human-readable name
            length_cm: Physical track length
            speed_limit_kmh: Optional speed limit
            validate: If True, perform full validation (recommended)
            block_id: Pin the owning block's id instead of deriving it; see
                `create_segment` for when a caller states it.

        Returns:
            The Block that owns the new segment. A fresh segment gets its own
            block under the default one-feed-per-segment rule, and that block's
            id is the segment's derived onto the block axis (`entity_ids`) —
            never the same string.

        Raises:
            ValueError: If validation fails

        Example:
            # Create a segment with validated tracks
            block = network.create_segment_with_tracks(
                segment_id="SEG_MAIN_1",
                positions=[(0,0), (1,0), (2,0)],
                name="Main Line Section 1",
                length_cm=150.0,
                speed_limit_kmh=80.0
            )
        """
        from segment_validation import (
            validate_segment_properties,
            validate_track_for_segment,
            validate_segment_assignment_continuity,
            validate_station_boundary,
        )

        # Validate segment properties
        if validate:
            is_valid, errors = validate_segment_properties(
                segment_id, name, length_cm, speed_limit_kmh
            )
            if not is_valid:
                raise ValueError(f"Invalid block properties: {', '.join(errors)}")

            # Validate station boundaries - segments cannot cross station boundaries
            valid, error = validate_station_boundary(positions, self._n)
            if not valid:
                raise ValueError(f"Station boundary violation: {error}")

        # Check the owning block's id doesn't already exist (the segment's own
        # id is checked in create_segment below)
        if (block_id or block_id_for_segment(segment_id)) in self._n.blocks:
            raise ValueError(
                f"Block {block_id or block_id_for_segment(segment_id)} already exists"
            )

        # Validate all tracks
        if validate:
            for x, y in positions:
                track = self._n.get_track(x, y)
                is_valid, error = validate_track_for_segment(track, self._n)
                if not is_valid:
                    raise ValueError(f"Track at ({x}, {y}): {error}")

                # Check not already in another segment
                if track.segment_id:
                    raise ValueError(
                        f"Track at ({x}, {y}) already in block {track.segment_id}"
                    )

            # Validate continuity
            is_continuous, errors = validate_segment_assignment_continuity(
                [], positions, self._n
            )
            if not is_continuous:
                raise ValueError(f"Block tracks not continuous: {', '.join(errors)}")

        # Create the segment and its owning block
        block = self.create_segment(segment_id, name, block_id=block_id)
        block.length_cm = length_cm
        if speed_limit_kmh:
            block.speed_limit_kmh = speed_limit_kmh

        # Assign tracks
        for x, y in positions:
            track = self._n.get_track(x, y)
            if track:
                track.segment_id = segment_id

        # Populate Segment.ordered_tiles per design_new.md "segment" so
        # path completion direction check and the block-direction helper
        # have geometry to work with.
        self.rebuild_segment_ordered_tiles(segment_id)

        return block

    def extend_segment(
        self,
        segment_id: str,
        new_positions: List[Tuple[int, int]],
        force: bool = False,
        force_overwrite: bool = False,
    ) -> Block:
        """
        Extend an existing segment with new track positions.

        Validates:
        - Segment exists
        - All new tracks are valid for segments (unless force=True for signals)
        - New tracks are adjacent to the existing segment
        - Extended segment remains continuous

        Args:
            segment_id: ID of segment to extend
            new_positions: List of (x, y) positions to add
            force: If True, removes signals from tiles to allow the extension.
                   This allows extending a segment past a signal boundary.
            force_overwrite: If True, allows overwriting tracks that belong to
                   other segments (reassigns them to this segment).

        Returns:
            The Block that owns the extended segment.

        Raises:
            ValueError: If validation fails
        """
        from segment_validation import (
            validate_track_for_segment,
            validate_segment_assignment_continuity,
            crossing_disallowed_in_segment,
        )

        segment = self._n.get_segment(segment_id)
        if segment is None:
            raise ValueError(f"Block {segment_id} not found")

        # Signals monitor blocks, so signal bookkeeping below is against the
        # block that owns this segment, not the segment itself.
        block_id = self._n.segment_to_block_id(segment_id)
        block = self._n.blocks[block_id]
        existing_positions = self._n.get_segment_positions(segment_id)

        # Track signals that need to be removed when force=True
        signals_to_remove = []
        # Track segments that will be affected by force_overwrite
        overwritten_segments = set()

        # Validate each new position
        for x, y in new_positions:
            track = self._n.get_track(x, y)
            if not track:
                raise ValueError(f"No track at position ({x}, {y})")

            # Disallowed crossings are never allowed, even with force. The
            # orthogonal NS+EW crossing may belong to a block.
            if crossing_disallowed_in_segment(track.track_bits):
                raise ValueError(
                    f"Track at ({x}, {y}): Cannot add crossing to block - crossings isolate paths"
                )

            # Check for switches (never allowed, even with force)
            if track.is_switch:
                raise ValueError(
                    f"Track at ({x}, {y}): Cannot add switch to block - switches are block boundaries"
                )

            # Check for signals - can be overridden with force
            # When force=True, we clear the signal's monitored block (shows warning triangle)
            # but we do NOT remove the signal itself
            # Only invalidate signals that monitor the block being extended (block_id)
            if track.signal_ids:
                if force:
                    # Collect signals to invalidate (clear monitored block)
                    # Only invalidate if the signal monitors the block we're extending
                    for signal_id in track.signal_ids:
                        signal = self._n.signals.get(signal_id)
                        if signal and signal.monitored_block_id == block_id:
                            signals_to_remove.append((signal_id, x, y))
                else:
                    raise ValueError(
                        f"Track at ({x}, {y}): Has signal(s) - use force=true to extend past signal"
                    )

            if track.segment_id and track.segment_id != segment_id:
                if force_overwrite:
                    # Track the segment that will lose this track
                    overwritten_segments.add(track.segment_id)
                else:
                    raise ValueError(
                        f"Track at ({x}, {y}) already in block {track.segment_id}"
                    )

        # Check adjacency
        is_adjacent = False
        for new_x, new_y in new_positions:
            for old_x, old_y in existing_positions:
                if abs(new_x - old_x) + abs(new_y - old_y) == 1:
                    is_adjacent = True
                    break
            if is_adjacent:
                break

        if not is_adjacent and len(existing_positions) > 0:
            raise ValueError("New positions must be adjacent to existing block")

        # Validate station boundaries - segment cannot cross station boundaries
        from segment_validation import validate_station_boundary

        all_positions = existing_positions + new_positions
        valid, error = validate_station_boundary(all_positions, self._n)
        if not valid:
            raise ValueError(f"Station boundary violation: {error}")

        # Invalidate signals in force mode - clear their monitored block
        # This causes them to show an orange warning triangle (signal needs reconfiguration)
        invalidated_signals = []
        for signal_id, x, y in signals_to_remove:
            if signal_id in self._n.signals:
                signal = self._n.signals[signal_id]
                # detection_direction can be either a string or an enum, handle both
                detection_direction = signal.detection_direction
                if hasattr(detection_direction, "name"):
                    detection_direction = detection_direction.name
                invalidated_signals.append(
                    {
                        "signal_id": signal_id,
                        "position": [x, y],
                        "detection_direction": detection_direction,
                        "approach_direction": signal.approach_direction,
                        "previous_block": signal.monitored_block_id,
                    }
                )
                # Clear the monitored block - signal will show warning triangle
                signal.monitored_block_id = None
                # Set signal to RED since it's no longer monitoring a block
                signal.state = SignalState.RED

        # Validate continuity
        is_continuous, errors = validate_segment_assignment_continuity(
            existing_positions, new_positions, self._n
        )
        if not is_continuous:
            raise ValueError(f"Extension would break continuity: {', '.join(errors)}")

        # Assign new tracks
        for x, y in new_positions:
            track = self._n.get_track(x, y)
            if track:
                track.segment_id = segment_id

        # Refresh ordered_tiles for both this segment and any segment we
        # overwrote tiles from, so the geometry view stays in sync.
        self.rebuild_segment_ordered_tiles(segment_id)
        for os_id in overwritten_segments:
            if os_id != segment_id and self._n.get_segment(os_id) is not None:
                self.rebuild_segment_ordered_tiles(os_id)

        # Store invalidated signals info on block for API response
        block._invalidated_signals = invalidated_signals

        return block

    def _require_run_stays_connected(
        self, block_id: str, going: Collection[str]
    ) -> None:
        """Raise ValueError when the segments in `going` leaving `block_id`
        would split it in two.

        design_new.md, **Tile** > **Non-empty parent rule**, "Segment deletion
        keeps its block connected": the members a block keeps after an edit
        must still be one connected run, so a segment can go only from either
        end of `Block.segments`, or when it is the block's only member. This is
        the same end-only rule `BlockGroupingManager.remove_segment_from_block`
        applies to grouping edits.

        `going` holds every segment of this block that the same edit empties.
        They are judged together, so an edit that takes away a whole block's
        track, or an end segment along with its neighbour, is allowed as long
        as the members left behind are next to each other. Reads only.
        """
        members = self._n.blocks[block_id].segment_ids
        if len(members) < 2:
            return
        kept = [i for i, member in enumerate(members) if member not in going]
        if not kept:
            return  # the whole block goes; nothing is left to split

        # Members that go from between two that stay are the ones that split
        # the block. A member going from either end never does.
        between = [
            members[i]
            for i in range(kept[0], kept[-1] + 1)
            if members[i] in going
        ]
        if not between:
            return

        if len(between) == 1:
            message = (
                f"Segment {between[0]} is in the middle of block {block_id}, "
                "so removing it would split the block in two; only an end "
                "segment can be removed. Take it out of the block's end "
                "first, or delete the block."
            )
        else:
            message = (
                f"Segments {', '.join(between)} are in the middle of block "
                f"{block_id}, so removing them would split the block in two; "
                "only end segments can be removed. Take them out of the "
                "block's ends first, or delete the block."
            )
        raise ValueError(message)

    def _check_block_edit(
        self, block_id: str, going: Callable[[Tuple[int, int]], bool]
    ) -> None:
        """Judge an edit against one block. `going(pos)` says whether the
        tile at `pos` leaves its segment in this edit."""
        members = self._n.blocks[block_id].segment_ids
        if len(members) < 2:
            return  # a lone segment cannot split its block
        emptied = [
            member
            for member in members
            if all(going(pos) for pos in self._n.get_segment_positions(member))
        ]
        if emptied:
            self._require_run_stays_connected(block_id, emptied)

    def check_tiles_removable(self, positions: Iterable[Tuple[int, int]]) -> None:
        """Raise ValueError when taking these tiles out of their segments
        would empty a segment whose block could not lose it.

        An emptied segment is deleted in the same operation (design_new.md,
        **Non-empty parent rule**, "Auto-delete on last-child removal"), so an
        edit that empties one is bound by the same rule as `delete_segment`,
        and a rejected delete rejects the whole edit ("Atomic commit"). Callers
        that change anything else before they reach the tile removal (erasing
        track, turning a tile into a switch, moving a tile into a platform) call
        this first, so a rejection changes nothing. Tiles that belong to no
        segment are ignored. Reads only.
        """
        leaving = frozenset((int(x), int(y)) for x, y in positions)
        judged: Set[str] = set()
        for x, y in leaving:
            track = self._n.get_track(x, y)
            if track is None or not track.segment_id:
                continue
            block_id = self._n.segment_to_block_id(track.segment_id)
            if block_id is None or block_id in judged:
                continue
            judged.add(block_id)
            self._check_block_edit(block_id, leaving.__contains__)

    def check_tile_removable(
        self, x: int, y: int, *, also_removing: Iterable[Tuple[int, int]] = ()
    ) -> None:
        """`check_tiles_removable` for one tile that leaves with `also_removing`.

        For a caller that removes a block's tiles one at a time after judging
        the whole edit up front. Only the block of this tile is judged, and only
        when this tile is its segment's last one, so the per-tile cost does not
        grow with the size of the edit. Pass `also_removing` as a `set` or
        `frozenset` to avoid copying it on every call. Reads only.
        """
        track = self._n.get_track(x, y)
        if track is None or not track.segment_id:
            return
        block_id = self._n.segment_to_block_id(track.segment_id)
        if block_id is None or len(self._n.blocks[block_id].segment_ids) < 2:
            # A lone segment cannot split its block. Almost every block is one,
            # so this settles the common case without reading any tile index.
            return
        key = (x, y)
        leaving = (
            also_removing
            if isinstance(also_removing, (set, frozenset))
            else frozenset(also_removing)
        )

        def going(pos: Tuple[int, int]) -> bool:
            return pos == key or pos in leaving

        segment_positions = self._n.get_segment_positions(track.segment_id)
        if not all(going(pos) for pos in segment_positions):
            return  # the segment keeps a tile, so it is not deleted
        self._check_block_edit(block_id, going)

    def delete_segment(self, segment_id: str) -> bool:
        """
        Delete a segment and clear its references from all tracks.

        The segment is removed from the block that owned it. If that was the
        block's last member the block goes too, and the signals monitoring it
        are disconnected; a block that still has other members survives and
        keeps its signals, because it is still a block those signals monitor.

        design_new.md, **Tile** > **Non-empty parent rule**, "Segment deletion
        keeps its block connected": a segment that is a non-terminal member of
        a multi-segment block is not deleted, because the block would be left
        as two pieces with a gap between them.

        Args:
            segment_id: ID of segment to delete

        Returns:
            True if deleted, False if not found

        Raises:
            ValueError: when the segment is in the middle of its block. Nothing
                is changed.
        """
        block_id = self._n.segment_to_block_id(segment_id)
        if block_id is None:
            return False
        self._require_run_stays_connected(block_id, {segment_id})
        return self._delete_segment_unchecked(segment_id)

    def _delete_segment_unchecked(self, segment_id: str) -> bool:
        """Delete a segment without judging its block; see `delete_segment`.

        For an edit that has already been judged as a whole by
        `check_tiles_removable`, where the segments an edit empties are deleted
        one after another and an earlier delete can leave a later one looking
        like a middle member.
        """
        block_id = self._n.segment_to_block_id(segment_id)
        if block_id is None:
            return False

        # Clear segment membership from all tracks in this segment
        for track in self._n.tracks.values():
            if track.segment_id == segment_id:
                track.segment_id = None

        block = self._n.blocks[block_id]
        block.segments = [s for s in block.segments if s.segment_id != segment_id]

        if not block.segments:
            # Disconnect signals monitoring this block (don't delete them)
            for signal in self._n.signals.values():
                if signal.monitored_block_id == block_id:
                    signal.monitored_block_id = None
            del self._n.blocks[block_id]
            # A segment has two parents; deleting the block covers one, and this
            # covers the other (design_new.md "Segment's two-parent fan-out"): a
            # platform built on the now-deleted block is dropped, and deleted if
            # that leaves it with no geometry. Without this, emptying a platform
            # tile's segment through remove_tile_from_segment (the switch-bit
            # edit in track_service) would leave the platform stored empty.
            self._n.prune_platforms_for_deleted_block(block_id)

        self._n.invalidate_pathfinding_cache()
        return True

    def assign_track_to_segment(
        self,
        x: int,
        y: int,
        segment_id: Optional[str],
        validate_continuity: bool = True,
    ) -> None:
        """
        Assign a track to a segment, validating membership and continuity.

        Args:
            x, y: Track position
            segment_id: Segment ID to assign (None to unassign)
            validate_continuity: If True, check that assignment maintains continuity

        Raises:
            ValueError: If validation fails with detailed error message
        """
        from segment_validation import (
            validate_track_for_segment,
            validate_segment_assignment_continuity,
        )

        track = self._n.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # If assigning to a segment
        if segment_id:
            # Check segment exists
            if self._n.get_segment(segment_id) is None:
                raise ValueError(f"Block {segment_id} does not exist")

            # Validate track can be in a segment
            is_valid, error = validate_track_for_segment(track, self._n)
            if not is_valid:
                raise ValueError(f"Cannot assign track at ({x}, {y}) to block: {error}")

            # Check if already assigned to a different segment
            if track.segment_id and track.segment_id != segment_id:
                raise ValueError(
                    f"Track at ({x}, {y}) already assigned to block {track.segment_id}. "
                    f"Unassign first or use reassign method."
                )

            # Validate continuity if requested
            if validate_continuity:
                existing_positions = self._n.get_segment_positions(segment_id)
                if len(existing_positions) > 0:
                    is_continuous, errors = validate_segment_assignment_continuity(
                        existing_positions, [(x, y)], self._n
                    )
                    if not is_continuous:
                        raise ValueError(
                            f"Adding track at ({x}, {y}) would create discontinuous block: "
                            f"{', '.join(errors)}"
                        )

        # Assignment is valid - update
        track.segment_id = segment_id

    def validate_segment_continuity(self, segment_id: str) -> Tuple[bool, List[str]]:
        """
        Check if a segment's tracks form a connected graph.

        Uses BFS to verify all tracks in the segment are reachable from
        any starting track via edge connections. Continuity is a per-segment
        property: a block groups segments that meet end to end, and that
        grouping is checked by `BlockGroupingManager.plan_run`.

        Args:
            segment_id: Segment ID to validate

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        tracks = self._n.get_segment_tracks(segment_id)
        if len(tracks) <= 1:
            return True, []

        positions = set((t.x, t.y) for t in tracks)
        visited = set()
        errors = []

        # BFS from first track
        start = tracks[0]
        to_visit = [(start.x, start.y)]

        while to_visit:
            pos = to_visit.pop()
            if pos in visited:
                continue
            visited.add(pos)

            track = self._n.get_track(*pos)
            if not track:
                continue

            # Get connected neighbors via edges
            for neighbor_pos in self._n._get_block_connected_neighbors(track):
                if neighbor_pos in positions and neighbor_pos not in visited:
                    to_visit.append(neighbor_pos)

        if len(visited) != len(positions):
            unvisited = positions - visited
            errors.append(f"Disconnected tracks at: {list(unvisited)}")
            return False, errors

        return True, []

    def remove_tile_from_segment(
        self, x: int, y: int, *, also_removing: Iterable[Tuple[int, int]] = ()
    ) -> List[str]:
        """
        Remove a tile from its segment and handle the connectivity check.

        This is the core lifecycle method for geometry management. When a tile
        is removed from a segment (due to station/signal placement, or track
        deletion), this method:
        1. Clears the tile's segment_id
        2. Checks if the remaining segment tiles are still connected
        3. If disconnected, splits into new child segments

        A segment left with no tiles is deleted, so taking a segment's last
        tile is bound by the rule for deleting it (see `delete_segment`). The
        edit is judged before anything changes, and a rejection changes nothing.

        Args:
            x, y: Tile position to remove from its segment
            also_removing: other tiles leaving their segments in the same
                edit. The rule is judged over the whole edit, so a caller that
                removes a block's tiles one at a time passes them all here.

        Returns:
            List of affected ids (the original + any new child ids created)

        Raises:
            ValueError: when this would empty a segment in the middle of its
                block. Nothing is changed.
        """
        track = self._n.get_track(x, y)
        if not track or not track.segment_id:
            return []

        self.check_tile_removable(x, y, also_removing=also_removing)

        segment_id = track.segment_id
        track.segment_id = None

        # Check and potentially split the segment
        return self._check_segment_connectivity(segment_id)

    def remove_tiles_from_segment(self, positions: List[Tuple[int, int]]) -> List[str]:
        """
        Remove multiple tiles from their segment(s) and handle connectivity.

        This is a batch version of remove_tile_from_segment for efficiency.

        The batch is judged as one edit before anything changes (see
        `check_tiles_removable`).

        Args:
            positions: List of (x, y) positions to remove

        Returns:
            List of affected ids

        Raises:
            ValueError: when the batch would empty a segment in the middle of
                its block. Nothing is changed.
        """
        if not positions:
            return []

        self.check_tiles_removable(positions)

        # Group positions by segment
        segments_affected = {}
        for x, y in positions:
            track = self._n.get_track(x, y)
            if track and track.segment_id:
                segment_id = track.segment_id
                if segment_id not in segments_affected:
                    segments_affected[segment_id] = []
                segments_affected[segment_id].append((x, y))
                track.segment_id = None

        # Check connectivity for each affected segment
        all_affected = []
        for segment_id in segments_affected:
            affected = self._check_segment_connectivity(segment_id)
            all_affected.extend(affected)

        return all_affected

    def _check_segment_connectivity(self, segment_id: str) -> List[str]:
        """
        Check segment connectivity and split if disconnected.

        After tiles are removed from a segment, this method:
        1. Gets remaining tile positions
        2. Finds connected components
        3. If multiple components, replaces the segment with child segments
        4. Remaps orphaned signals to nearest reachable child block

        Args:
            segment_id: Segment ID to check

        Returns:
            List of affected ids (may include new child ids)
        """
        segment = self._n.get_segment(segment_id)
        if segment is None:
            return []

        block_id = self._n.segment_to_block_id(segment_id)
        block = self._n.blocks[block_id]
        remaining_positions = self._n.get_segment_positions(segment_id)

        # Case 1: Segment is now empty
        if not remaining_positions:
            # delete_segment drops the owning block too when this was its last
            # member, and only then are its signals orphaned.
            was_last_member = len(block.segments) == 1
            if was_last_member:
                self._disconnect_signals_for_block_of_segment(block_id)
            # The edit that emptied it was judged as a whole before any tile
            # left (`check_tiles_removable`), so this delete does not judge the
            # block again: a batch that empties several segments deletes them
            # one after another, and an earlier delete can leave a later one
            # looking like a middle member.
            self._delete_segment_unchecked(segment_id)
            return [segment_id]

        # Find connected components
        components = self._find_connected_components(remaining_positions)

        # Case 2: Single component - segment is still connected
        if len(components) == 1:
            return [segment_id]

        # Case 3: Multiple components - split into child segments.
        # A block that groups several segments keeps its identity: the children
        # replace the parent segment inside it, so the operator's power grouping
        # and the signals monitoring that block both survive.
        if len(block.segments) > 1:
            new_ids = self._create_child_segments(
                segment, block, components, install_in_parent_block=True
            )
            block.segments = [
                sg for sg in block.segments if sg.segment_id != segment_id
            ]
            self._n.invalidate_pathfinding_cache()
            return [segment_id] + new_ids

        # The parent segment was the block's only member, so the block is
        # replaced by one fresh block per child.
        orphaned_signals = [
            signal
            for signal in self._n.signals.values()
            if signal.monitored_block_id == block_id
        ]

        new_segment_ids = self._create_child_segments(
            segment, block, components, install_in_parent_block=False
        )
        # The children are geometry ids; a signal monitors a BLOCK, so resolve
        # each child to the block that now owns it before remapping.
        new_block_ids = [
            owner
            for owner in (
                self._n.segment_to_block_id(child) for child in new_segment_ids
            )
            if owner is not None
        ]

        # Delete original block (but don't disconnect signals yet - we'll remap them)
        if block_id in self._n.blocks:
            del self._n.blocks[block_id]

        # Remap orphaned signals to nearest reachable child block
        self._remap_orphaned_signals(orphaned_signals, new_block_ids)

        return [segment_id] + new_segment_ids

    def _disconnect_signals_for_block_of_segment(self, block_id: str) -> None:
        """
        Disconnect all signals monitoring the specified block.

        Sets monitored_block_id = None for all signals monitoring this block.

        Args:
            block_id: Block ID to disconnect signals from
        """
        for signal in self._n.signals.values():
            if signal.monitored_block_id == block_id:
                signal.monitored_block_id = None

    # === Unified Segment Lifecycle Management ===

    def _create_child_segments(
        self,
        parent_segment: "Segment",
        parent_block: Block,
        components: List[List[Tuple[int, int]]],
        install_in_parent_block: bool,
    ) -> List[str]:
        """
        Create child segments from connected components, inheriting properties.

        Child segments inherit:
        - name: "{parent.name}_1", "{parent.name}_2", etc.
        - length_cm: Calculated based on tile count × length_per_tile_cm from parent
        - min_radius_cm, speed_limit_kmh (copied as-is)

        Args:
            parent_segment: Original segment being split
            parent_block: The block that owned it
            components: List of connected components (each is list of positions)
            install_in_parent_block: When True the children join `parent_block`,
                keeping its grouping and its signals. When False (the parent was
                the block's only member) each child gets its own fresh block,
                so occupancy resets to FREE — the parent's occupancy does not
                transfer cleanly across a split.

        Child ids are minted on the axis the children live on. Children that
        join `parent_block` are new geometry inside an existing block, so they
        suffix the parent SEGMENT's id. Children that each get their own block
        suffix the parent BLOCK's id — that is the id saved routes, tests, and
        hardware bindings name, so keeping the split's output on that axis keeps
        `PARENT_1` / `PARENT_2` readable — and each child segment then takes the
        matching geometry-axis id (`entity_ids`).

        Returns:
            List of new child SEGMENT ids
        """
        new_ids = []
        base_name = (
            parent_segment.name if parent_segment.name else parent_segment.segment_id
        )

        # Calculate length per tile from the parent segment
        # Check if parent has stored length_per_tile_cm (from station split)
        length_per_tile_cm = getattr(parent_block, "_length_per_tile_cm", None)

        if length_per_tile_cm is None:
            if parent_segment.length_cm is None or parent_segment.length_cm <= 0:
                parent_segment.length_cm = 0.0
            parent_positions = self._n.get_segment_positions(
                parent_segment.segment_id
            )
            if parent_positions and parent_segment.length_cm > 0:
                length_per_tile_cm = parent_segment.length_cm / len(parent_positions)
            else:
                total_tiles = sum(len(comp) for comp in components)
                if total_tiles > 0 and parent_segment.length_cm > 0:
                    length_per_tile_cm = parent_segment.length_cm / total_tiles
                else:
                    length_per_tile_cm = 0.0

        for i, component_positions in enumerate(components, start=1):
            # Generate child segment ID and name (see the id-axis rule above).
            if install_in_parent_block:
                child_id = f"{parent_segment.segment_id}_{i}"
                child_block_id = None
            else:
                child_block_id = f"{parent_block.block_id}_{i}"
                child_id = segment_id_for_block(child_block_id)
            child_name = f"{base_name}_{i}"

            # Calculate child length based on tile count
            child_length_cm = (
                len(component_positions) * length_per_tile_cm
                if length_per_tile_cm > 0
                else 0.0
            )

            child_segment = Segment(
                segment_id=child_id,
                name=child_name,
                length_cm=child_length_cm,  # Calculated based on tile count
                min_radius_cm=parent_segment.min_radius_cm,
                grade_percent=parent_segment.grade_percent,
                speed_limit_kmh=parent_segment.speed_limit_kmh,
            )

            if install_in_parent_block:
                parent_block.segments.append(child_segment)
            else:
                # v1: Block.state is a derived @property; do not pass it to the
                # constructor. occupied_by defaults to None which yields FREE.
                # `child_name` is derived from the parent SEGMENT's name, so it
                # names the child segment above; the child block stays unnamed.
                self._n.blocks[child_block_id] = Block(
                    block_id=child_block_id,
                    occupied_by=None,
                    segments=[child_segment],
                )
            self._n._ensure_resource_managers()

            for x, y in component_positions:
                track = self._n.get_track(x, y)
                if track:
                    track.segment_id = child_id

            # Populate ordered_tiles per design_new.md "segment".
            # Inherit each tile's `entry_edge` from the parent segment so the
            # child's local traversal direction matches the parent's (this
            # matters for single-tile child segments where the connectivity
            # graph alone is ambiguous — picking the first cardinal
            # neighbour can flip the forward direction).
            self._rebuild_child_ordered_tiles(child_segment, parent_segment)

            new_ids.append(child_id)

        return new_ids

    def _rebuild_child_ordered_tiles(
        self, child: "Segment", parent_segment: "Segment"
    ) -> None:
        """Rebuild a child segment's ordered_tiles using inherited entry_edges
        from `parent_segment.ordered_tiles` where possible.

        Falls back to the standard rebuild for any tile the parent didn't
        record an entry_edge for.
        """
        parent_entry_by_tile = {
            (entry.x, entry.y): entry.entry_edge
            for entry in (parent_segment.ordered_tiles or [])
        }

        child_tiles = []
        for (x, y), track in self._n.tracks.items():
            if track is None or track.is_switch:
                continue
            if getattr(track, "segment_id", None) == child.segment_id:
                child_tiles.append((x, y))

        if not child_tiles:
            child.ordered_tiles = []
            return

        # Walk in the parent's order: any of the child's tiles that the
        # parent listed first comes first here.
        parent_order = {
            (entry.x, entry.y): i
            for i, entry in enumerate(parent_segment.ordered_tiles or [])
        }
        child_tiles.sort(key=lambda t: (parent_order.get(t, 10**9), t))

        entries = []
        for tile in child_tiles:
            inherited = parent_entry_by_tile.get(tile)
            if inherited:
                entries.append(
                    SegmentTileEntry(x=tile[0], y=tile[1], entry_edge=inherited)
                )
            else:
                # Fallback for tiles the parent didn't record (shouldn't
                # happen, but stay safe).
                entries.append(
                    SegmentTileEntry(
                        x=tile[0],
                        y=tile[1],
                        entry_edge=self._external_entry_edge(tile, set(child_tiles)),
                    )
                )
        child.ordered_tiles = entries

    def _find_connected_components(
        self, positions: List[Tuple[int, int]]
    ) -> List[List[Tuple[int, int]]]:
        """
        Find connected components among tile positions using track connectivity.

        Two tiles are connected if:
        1. They are adjacent (Manhattan distance = 1)
        2. Their track_bits allow connection (one has edge pointing to other)

        Uses BFS to group tiles into connected components.

        Args:
            positions: List of (x, y) tile positions

        Returns:
            List of components, where each component is a list of positions
        """
        if not positions:
            return []

        position_set = set(positions)
        visited = set()
        components = []

        for start_pos in positions:
            if start_pos in visited:
                continue

            # BFS to find all connected tiles from this starting point
            component = []
            queue = [start_pos]
            visited.add(start_pos)

            while queue:
                current = queue.pop(0)
                component.append(current)

                # Find connected neighbors within the position set
                for neighbor in self._get_track_connected_neighbors(current):
                    if neighbor in position_set and neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

            components.append(component)

        return components

    def _get_track_connected_neighbors(
        self, pos: Tuple[int, int]
    ) -> List[Tuple[int, int]]:
        """
        Get positions of tracks connected via edges (track_bits connectivity).

        A neighbor is connected if:
        1. This track has an edge pointing toward the neighbor
        2. The neighbor has an edge pointing back

        Args:
            pos: Position to check neighbors for

        Returns:
            List of connected neighbor positions
        """
        track = self._n.get_track(*pos)
        if not track:
            return []

        neighbors = []
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        opposite_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}

        for edge in sorted(track.edges):
            if edge not in edge_offsets:
                continue
            dx, dy = edge_offsets[edge]
            neighbor_pos = (pos[0] + dx, pos[1] + dy)
            neighbor = self._n.get_track(*neighbor_pos)
            if neighbor:
                opp = opposite_edge[edge]
                if opp in neighbor.edges:
                    neighbors.append(neighbor_pos)

        return neighbors

    def _remap_orphaned_signals(
        self, orphaned_signals: List[Signal], new_block_ids: List[str]
    ) -> None:
        """Reassign signals from a split block to the closest of the new
        child blocks.

        Distance is measured by ``RailwayPathfinder.measure_distance`` in
        tile hops, ignoring switch state. A signal that can't reach any
        of the new blocks has its ``monitored_block_id`` cleared.
        """
        from railway_pathfinder import RailwayPathfinder

        pathfinder = RailwayPathfinder(self._n)
        for signal in orphaned_signals:
            best_block: Optional[str] = None
            best_dist: Optional[int] = None
            for block_id in new_block_ids:
                dist = pathfinder.measure_distance(signal.position, block_id)
                if dist is None:
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_block = block_id

            signal.monitored_block_id = best_block

            if signal.signal_type == SignalType.BLOCK:
                if signal.monitored_block_id:
                    signal.state = self._n._calculate_signal_state(signal)
                else:
                    signal.state = SignalState.RED

    # === Centralized Switch Management ===

