"""BlockGroupingManager — the power-grouping axis of the track model.

Per design_new.md **Block grouping**: block membership — creating a block,
adding or removing a `Segment` from `segment_ids`, and deleting a block — lives
here. This manager edits only the power-grouping axis (`segment_ids`); it never
writes Block runtime state (that is `BlockManager`) and never writes segment
geometry (that is the geometry manager).

Rules this module enforces (all from design_new.md **Block grouping**):

- A block is a switch-free connected run of segments joined end-to-end, so an
  add or a remove may only happen at either END of `segment_ids`, and only when
  the result is still one connected switch-free walk whose member
  `Segment.ordered_tiles` orientations all align with increasing block order.
  Middle insertions, removals, reorders, and an end-add that would require
  reversing a stored segment order are rejected.
- Every segment belongs to exactly one block. A segment the operator has not
  grouped into a larger block is its own single-segment block — the default
  one-feed-per-segment layout. `add_segment_to_block` moves a segment out of its
  prior block (deleting that block if it thereby empties, per the Non-empty
  parent rule); `remove_segment_from_block` returns the segment to its own
  single-segment block; `delete_block` re-homes every member segment the same
  way.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from block import Block
from segment import Segment

if TYPE_CHECKING:
    from network import RailwayNetwork

logger = logging.getLogger(__name__)


class BlockGroupingManager:
    """Owns block membership (`segment_ids`) for one `RailwayNetwork`."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def segment_to_block_id(self, segment_id: str) -> Optional[str]:
        """Id of the block owning `segment_id`, or None when unknown.

        Derived from `Block.segment_ids` on every read, so it can never drift
        from the blocks themselves.

        An un-grouped segment is its own single-segment block sharing its id
        (see module docstring), so that block is looked up directly first —
        this resolves the overwhelmingly common case in O(1) instead of
        scanning every block. Only a segment that was moved into a grouped
        block, or rehomed under a suffixed id after a collision, falls
        through to the full scan.
        """
        same_id_block = self._n.blocks.get(segment_id)
        if same_id_block is not None and any(
            s.segment_id == segment_id for s in same_id_block.segments
        ):
            return segment_id
        for block_id, block in self._n.blocks.items():
            if segment_id in block.segment_ids:
                return block_id
        return None

    def get_segment(self, segment_id: str) -> Optional[Segment]:
        """The live `Segment` with this id, or None."""
        for block in self._n.blocks.values():
            for seg in block.segments:
                if seg.segment_id == segment_id:
                    return seg
        return None

    # ------------------------------------------------------------------
    # The four-method grouping surface
    # ------------------------------------------------------------------

    def create_block(
        self,
        block_id: str,
        segment_ids: List[str],
        name: str = "",
        validate: bool = True,
    ) -> Block:
        """Group existing segments into one block sharing one power feed.

        `segment_ids` is the ordered member list. The segments must form one
        connected switch-free run in the given order, each joined end-to-first
        (a closed loop is allowed — its last segment rejoins its first). Every
        listed segment is moved out of its prior block, and a prior block left
        with no segments is deleted (Non-empty parent rule).

        `validate=False` skips only the run check. It exists for loading a save
        whose stored grouping does not chain cleanly: dropping the grouping
        there would turn one single-train feed back into several independent
        ones, so the grouping is preserved and the caller logs the geometry
        problem instead.

        Raises ValueError when the id is taken, a segment is unknown, the list
        is empty, or (under `validate`) the segments do not form one such run.
        """
        if block_id in self._n.blocks:
            raise ValueError(f"Block {block_id} already exists")
        ids = list(dict.fromkeys(segment_ids))
        if not ids:
            raise ValueError("A block must group at least one segment")

        segments: List[Segment] = []
        for segment_id in ids:
            seg = self.get_segment(segment_id)
            if seg is None:
                raise ValueError(f"Segment {segment_id} not found")
            segments.append(seg)

        if validate and not self.segments_form_switch_free_run(ids):
            raise ValueError(
                f"Segments {ids} do not form one connected switch-free run in "
                "that order"
            )

        # Detach each segment from its prior block before the new block claims
        # it, so a segment is never in two blocks at once.
        for segment_id in ids:
            self._detach_segment(segment_id)

        block = Block(block_id=block_id, name=name, segments=segments)
        self._n.blocks[block_id] = block
        self._reindex_block_tiles(block)
        self._n._ensure_resource_managers()
        return block

    def delete_block(self, block_id: str) -> bool:
        """Dissolve a block grouping; every member segment becomes its own
        single-segment block (the default one-feed-per-segment layout).

        Deleting a block never deletes its segments. Returns False when the id
        is unknown.
        """
        block = self._n.blocks.get(block_id)
        if block is None:
            return False
        freed = list(block.segments)
        del self._n.blocks[block_id]
        for seg in freed:
            self._rehome_segment(seg)
        self._n.invalidate_pathfinding_cache()
        return True

    def add_segment_to_block(self, block_id: str, segment_id: str) -> bool:
        """Add `segment_id` to either END of `block_id`'s run.

        The add must keep the block one connected switch-free walk without
        reversing the segment's stored tile order: the segment either continues
        the run (its first tile joins the run's last tile) or precedes it (its
        last tile joins the run's first tile). Any other placement is rejected.

        Raises ValueError when the block or segment is unknown, or when the add
        would break the run. Returns True when added, False when the segment is
        already a member.
        """
        block = self._n.blocks.get(block_id)
        if block is None:
            raise ValueError(f"Block {block_id} not found")
        seg = self.get_segment(segment_id)
        if seg is None:
            raise ValueError(f"Segment {segment_id} not found")
        if segment_id in block.segment_ids:
            return False

        current = list(block.segments)
        if self._joins_end_to_first(current[-1], seg):
            position = len(current)
        elif self._joins_end_to_first(seg, current[0]):
            position = 0
        else:
            raise ValueError(
                f"Segment {segment_id} does not join either end of block "
                f"{block_id} in its stored order"
            )

        self._detach_segment(segment_id)
        # Re-read the block: detaching the segment's prior block cannot have
        # removed this one (a segment is never in two blocks), so the insert is
        # safe.
        block.segments.insert(position, seg)
        self._reindex_block_tiles(block)
        self._n.invalidate_pathfinding_cache()
        return True

    def remove_segment_from_block(self, block_id: str, segment_id: str) -> bool:
        """Remove `segment_id` from either END of `block_id`'s run.

        The segment returns to its own single-segment block. Removing a middle
        segment is rejected because it would split the run in two. Removing the
        block's last member deletes the block (Non-empty parent rule).

        Raises ValueError when the block is unknown or the removal is not at an
        end. Returns True when removed, False when the segment was not a member.
        """
        block = self._n.blocks.get(block_id)
        if block is None:
            raise ValueError(f"Block {block_id} not found")
        member_ids = block.segment_ids
        if segment_id not in member_ids:
            return False

        if len(member_ids) == 1:
            # Last member: the block would be left empty, so the block itself
            # is deleted and the segment is re-homed into its own block.
            return self.delete_block(block_id)

        if segment_id not in (member_ids[0], member_ids[-1]):
            raise ValueError(
                f"Segment {segment_id} is in the middle of block {block_id}; "
                "only an end segment may be removed"
            )

        seg = next(s for s in block.segments if s.segment_id == segment_id)
        block.segments = [s for s in block.segments if s.segment_id != segment_id]
        self._reindex_block_tiles(block)
        self._rehome_segment(seg)
        self._n.invalidate_pathfinding_cache()
        return True

    # ------------------------------------------------------------------
    # Run validation
    # ------------------------------------------------------------------

    def segments_form_switch_free_run(self, segment_ids: List[str]) -> bool:
        """True when `segment_ids`, in the given order, is one connected
        switch-free walk.

        Each consecutive pair must join end-to-first in their stored tile
        orders, and no member tile may be a switch tile. A single existing
        segment is trivially a run; an unknown segment is not.
        """
        ids = list(dict.fromkeys(segment_ids))
        segments: List[Segment] = []
        for segment_id in ids:
            seg = self.get_segment(segment_id)
            if seg is None:
                return False
            segments.append(seg)
        if not segments:
            return False
        for seg in segments:
            if self._has_switch_tile(seg):
                return False
        if len(segments) == 1:
            return True
        for prev, nxt in zip(segments, segments[1:]):
            if not self._joins_end_to_first(prev, nxt):
                return False
        return True

    def plan_run(self, segment_ids: List[str]) -> Tuple[List[str], List[str]]:
        """Work out the run order for `segment_ids`, and which of them are
        stored running the wrong way for it.

        Returns ``(ordered_ids, ids_to_reverse)``. This only plans — it writes
        nothing. Reversing a segment is a geometry write
        (`SegmentGeometryManager.reverse_segment_orientation`), so the caller
        applies the reversals and then calls `create_block`, which keeps its own
        no-reversal rule.

        Used when grouping a member list that carries no traversal order (a
        baked scene, or a save written before blocks grouped segments). Segments
        that cannot be chained at all are appended in their given order so a
        load never hard-fails; the caller checks the result and logs that case.
        """
        ids = list(dict.fromkeys(segment_ids))
        if len(ids) <= 1:
            return ids, []
        segments = {sid: self.get_segment(sid) for sid in ids}
        segments = {sid: s for sid, s in segments.items() if s is not None}
        if not segments:
            return ids, []

        # Start from the lowest id so the run does not depend on the order the
        # member list happened to arrive in. A baked scene and a loaded save
        # name the same members in different orders, and both must produce the
        # same run.
        remaining = dict(segments)
        start = min(remaining)
        chain: List[Tuple[str, bool]] = [(start, False)]
        del remaining[start]

        extended = True
        while extended and remaining:
            extended = False
            _, tail_last = self._endpoints(*chain[-1], segments)
            head_first, _ = self._endpoints(*chain[0], segments)
            for sid in list(remaining):
                for reverse in (False, True):
                    cand_first, cand_last = self._endpoints(sid, reverse, segments)
                    if cand_first is None:
                        continue
                    if tail_last is not None and self._tiles_connected(
                        tail_last, cand_first
                    ):
                        chain.append((sid, reverse))
                    elif head_first is not None and self._tiles_connected(
                        cand_last, head_first
                    ):
                        chain.insert(0, (sid, reverse))
                    else:
                        continue
                    del remaining[sid]
                    extended = True
                    break
                if extended:
                    break

        # A closed loop has no end, so every rotation of it is the same run.
        # Rotate to the lowest id to pick one, keeping the direction, so the
        # run is stable across a bake / load round-trip.
        if len(chain) > 2:
            _, tail_last = self._endpoints(*chain[-1], segments)
            head_first, _ = self._endpoints(*chain[0], segments)
            closed = (
                tail_last is not None
                and head_first is not None
                and self._tiles_connected(tail_last, head_first)
            )
            if closed:
                anchor = min(sid for sid, _ in chain)
                pivot = next(i for i, (sid, _) in enumerate(chain) if sid == anchor)
                chain = chain[pivot:] + chain[:pivot]

        ordered = [sid for sid, _ in chain]
        to_reverse = [sid for sid, rev in chain if rev]
        leftover = [sid for sid in ids if sid not in ordered]
        return ordered + leftover, to_reverse

    def _endpoints(
        self, segment_id: str, reverse: bool, segments: Dict[str, Segment]
    ) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
        """First and last tile of `segment_id` under the planned orientation."""
        seg = segments.get(segment_id)
        if seg is None or not seg.ordered_tiles:
            return None, None
        head = (seg.ordered_tiles[0].x, seg.ordered_tiles[0].y)
        tail = (seg.ordered_tiles[-1].x, seg.ordered_tiles[-1].y)
        return (tail, head) if reverse else (head, tail)

    def group_into_block(
        self, block_id: str, segment_ids: List[str], name: str = ""
    ) -> Block:
        """Plan the run, align any segment stored the wrong way, then group.

        The one entry point for turning an unordered member list into a block:
        `plan_run` decides the order and which members are stored backwards,
        `SegmentGeometryManager.reverse_segment_orientation` flips those, and
        `create_block` does the grouping under its ordinary validation. Used by
        baked scenes and by loading a save whose grouping was stored separately
        from the blocks it grouped.

        Raises ValueError when the members still do not form one connected
        switch-free run after alignment.
        """
        ordered, to_reverse = self.plan_run(segment_ids)
        geometry = self._n._get_segment_geometry_manager()
        for segment_id in to_reverse:
            geometry.reverse_segment_orientation(segment_id)
        return self.create_block(block_id=block_id, segment_ids=ordered, name=name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _joins_end_to_first(self, prev: Segment, nxt: Segment) -> bool:
        """True when `nxt`'s first tile is track-connected to `prev`'s last
        tile, so `nxt` continues `prev`'s forward walk without reversal."""
        if not prev.ordered_tiles or not nxt.ordered_tiles:
            return False
        last = prev.ordered_tiles[-1]
        first = nxt.ordered_tiles[0]
        return self._tiles_connected((last.x, last.y), (first.x, first.y))

    def _tiles_connected(
        self, a: Tuple[int, int], b: Tuple[int, int]
    ) -> bool:
        """True when tile `a` has a track edge into `b` and `b` points back."""
        track = self._n.get_track(*a)
        if track is None:
            return False
        return tuple(b) in {
            tuple(p) for p in self._n._get_block_connected_neighbors(track)
        }

    def _has_switch_tile(self, seg: Segment) -> bool:
        """True when any tile of `seg` carries a switch — a block is a
        switch-free run."""
        for entry in seg.ordered_tiles:
            track = self._n.get_track(entry.x, entry.y)
            if track is not None and getattr(track, "switch_id", None):
                return True
        return False

    def _detach_segment(self, segment_id: str) -> None:
        """Remove `segment_id` from whatever block currently holds it, deleting
        that block if it thereby empties (Non-empty parent rule). The caller
        installs the segment in its new block."""
        holder_id = self.segment_to_block_id(segment_id)
        if holder_id is None:
            return
        holder = self._n.blocks[holder_id]
        holder.segments = [
            s for s in holder.segments if s.segment_id != segment_id
        ]
        if not holder.segments:
            del self._n.blocks[holder_id]
        else:
            self._reindex_block_tiles(holder)

    def _rehome_segment(self, seg: Segment) -> None:
        """Give a freed segment its own single-segment block — the default
        one-feed-per-segment grouping. The new block takes the segment's id, so
        a segment freed from a grouping lands back on the block id it had
        before it was grouped."""
        block_id = seg.segment_id
        if block_id in self._n.blocks:
            # An id collision would silently merge two feeds; keep the segment
            # in its own distinctly-named block instead.
            suffix = 2
            while f"{block_id}_{suffix}" in self._n.blocks:
                suffix += 1
            block_id = f"{block_id}_{suffix}"
        block = Block(block_id=block_id, name=seg.name, segments=[seg])
        self._n.blocks[block_id] = block
        self._reindex_block_tiles(block)

    def _reindex_block_tiles(self, block: Block) -> None:
        """Point every tile of `block` at the member segment that holds it.

        A tile belongs to a segment, so the reverse index stores the segment
        id; "which block owns this tile?" is then resolved through
        `segment_to_block_id`. Writing the block id here would make every tile
        of a grouped block claim membership of a segment that does not exist.
        """
        for segment in block.segments:
            for entry in segment.ordered_tiles:
                self._n._set_tile_segment(entry.x, entry.y, segment.segment_id)
                track = self._n.get_track(entry.x, entry.y)
                if track is not None:
                    track.segment_id = segment.segment_id
