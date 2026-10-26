"""ProgressReportBuilder — turn a TrainPhysics span update into an Observe
progress report for one TrainController.

Extracted from train_controller/__init__.py with behavior unchanged. The
builder is constructed once per controller and reads controller state through
the back-reference ``self._c``; it writes only to ``physics_sync`` (the
per-tick progress queue and the detector entered/vacated slices), mirroring the
other controller sub-modules.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from runtime_payloads import (
    ErrorRecord,
    ControllerProgressReport,
    PhysicsSpanUpdate,
    ResourceEvent,
)

if TYPE_CHECKING:
    from train_controller import TrainController


class ProgressReportBuilder:
    """Builds and enqueues `ControllerProgressReport`s for a `TrainController`."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    def on_physics_updated(
        self,
        event_time: float,
        span_update: PhysicsSpanUpdate,
    ) -> None:
        """Apply a TrainPhysics span_update and enqueue a progress report for Observe.

        Per design_new.md "Physical progress / deviation procedure" and
        "TrainController" sections:
        1. Atomically write current_span and current_speed_kmh from the update.
        2. Build a ControllerProgressReport from the tile delta (added/removed
           tiles -> entered/tail-vacated resource events).
        3. Append the report to ``self._c.physics_sync.queued_progress_reports``
           for SimulationController to drain in Observe.

        For powered-off transactions, the controller still applies span/speed
        but must not enqueue dispatcher/route progress reports because the
        Observe commit is skipped. This method honors that rule: if power is off and the
        state is OFF, the queue write is skipped.
        """
        # 1. Storage update (canonical span + speed). The doc's TrainSpan is
        # owned by the train/controller side; until that schema lands here we
        # store the span on the controller (current_span) and let consumers
        # access it via the existing helpers.
        self._c.physics_sync.current_span = span_update.new_span
        self._c.physics_sync.current_speed_kmh = float(span_update.current_speed_kmh)

        # `train_status` is derived from `powered` / `eb.active` /
        # `control_mode`, so writing the new speed needs no state re-evaluation
        # here (design_new.md "Controller state machine").

        # 2/3. Build progress report from tile delta and enqueue for Observe.
        if not self._c.powered:
            # Powered-off transactions append no dispatcher/route progress to the
            # mailbox BEYOND the block observations folded in from the
            # detector-read detected sets (design_new.md, "Movement feedback runs only when
            # movement is calculated; detection feedback always runs"). Those observations are the
            # only path by which a train that moves without power — pushed by
            # hand, or an unidentified train, which is always OFF — commits its block
            # entries and vacates, and the Observe commit runs for exactly those ticks.
            self._enqueue_detected_blocks_only_report(event_time)
            return

        # Build a tile_xy -> sequence_index lookup from the active path so
        # entered/tail-vacated events can carry the resource's sequence_index.
        # Per design_new.md "Physical progress / deviation procedure" and the
        # TileTraversal docstring in runtime_payloads.py, the controller is
        # the canonical site that derives sequence_index from the live
        # `received_path_plan.resource_sequence`; dispatcher must not search
        # by raw resource_id.
        plan = self._c.path_projection.received_path_plan
        # Head-mapping rule (design_new.md): one resource_id may appear
        # at more than one sequence_index in a single live resource_sequence (a
        # lapping path that takes authority back into a block it already passed
        # and released). Collect ALL occurrences per tile / block so the head and
        # every entered/vacated event resolve to the right occurrence against the
        # controller's progress anchor, instead of a last-wins map that always
        # returns the highest index and so mis-maps a duplicate's first visit.
        tile_xy_to_seq_indices: Dict[Tuple[int, int], List[int]] = {}
        # block_id -> sequence_indices / forward exit edge from the live plan,
        # used to enrich the detector-sourced block events (design_new.md "Power
        # -> Block occupancy detection") the same way tile traversals enrich
        # switch events with their own sequence_index.
        block_to_seq_indices: Dict[str, List[int]] = {}
        block_to_exit_edge: Dict[str, str] = {}
        if plan is not None:
            for entry in plan.resource_sequence:
                for path_tile in entry.path_tiles:
                    tile_xy_to_seq_indices.setdefault(
                        (path_tile.x, path_tile.y), []
                    ).append(entry.sequence_index)
                if getattr(entry, "resource_type", None) == "block":
                    block_to_seq_indices.setdefault(entry.resource_id, []).append(
                        entry.sequence_index
                    )
                    if entry.exit_edge:
                        block_to_exit_edge[entry.resource_id] = entry.exit_edge

        # The progress anchor for THIS report is the last index reported for the
        # current path_id (design_new.md "Head-mapping rule"): it resets to 0 on
        # a path_id change and is 0 before the head has been reported. Resolve
        # every occurrence list to the lowest index at or after it; when a
        # resource's only occurrences lie behind the anchor, resolve to the
        # nearest (highest) of them — a no-advance the monotonic
        # Path.current_resource_index rule leaves unchanged, never a deviation.
        ps_anchor = self._c.physics_sync
        if plan is not None:
            if ps_anchor.head_progress_anchor_path_id != plan.path_id:
                ps_anchor.head_progress_anchor = 0
                ps_anchor.head_progress_anchor_path_id = plan.path_id
        anchor = ps_anchor.head_progress_anchor

        def _resolve_index(indices: Optional[List[int]]) -> Optional[int]:
            if not indices:
                return None
            at_or_after = [i for i in indices if i >= anchor]
            return min(at_or_after) if at_or_after else max(indices)

        entered_resources: List[ResourceEvent] = []
        seen_entered: set = set()
        for tile in span_update.added_tiles:
            res_id = tile.resource_id
            res_type = tile.resource_type
            if res_id is None or res_type is None:
                continue
            # Block entry is detector-driven (current detection + debounce); only
            # switch entry comes from the precise tile diff. The detector's block
            # slice is appended below on the final no-boundary callback. Switch
            # events propagate entry_edge + switch_exit_edge so the dispatcher can
            # build the route key for route-keyed switch occupancy.
            if res_type != "switch":
                continue
            key = (res_type, res_id)
            if key in seen_entered:
                continue
            seen_entered.add(key)
            entered_resources.append(
                ResourceEvent(
                    sequence_index=_resolve_index(
                        tile_xy_to_seq_indices.get(tile.tile_xy)
                    ),
                    resource_type=res_type,
                    resource_id=res_id,
                    exit_edge=None,
                    block_id=tile.block_id,
                    entry_edge=tile.entry_edge,
                    switch_exit_edge=tile.exit_edge,
                )
            )

        # Compute the blocks still occupied by the train post-move so we
        # only emit "tail vacated R" events when R is fully vacated. Without
        # this guard, removing a single tile from a multi-tile block emits a
        # spurious tail-vacate for the block (the train still occupies other
        # tiles of the same block), which downstream causes BlockManager to
        # release the block reservation while the train is still on it —
        # which in turn flips the protecting signal to RED, truncates ATC
        # clearance, and fires the spurious overspeed EB.
        new_span = span_update.new_span
        still_occupied_block_ids: set = set(
            new_span.occupied_blocks if new_span and new_span.occupied_blocks else []
        )

        tail_vacated_resources: List[ResourceEvent] = []
        seen_vacated: set = set()
        for tile in span_update.removed_tiles:
            res_id = tile.resource_id
            res_type = tile.resource_type
            if res_id is None or res_type is None:
                continue
            if res_type == "block":
                # Block tail-vacate is detector-driven. When the tail fully
                # clears a block (no remaining occupied tiles), record the
                # precise forward exit edge so the later debounced vacate carries
                # the correct caution direction. Partial vacates (block still
                # occupied on another tile) are ignored, same as before.
                if res_id in still_occupied_block_ids:
                    continue
                self._c.physics_sync.pending_block_exit_edges[res_id] = tile.exit_edge
                continue
            key = (res_type, res_id)
            if key in seen_vacated:
                continue
            seen_vacated.add(key)
            tail_vacated_resources.append(
                ResourceEvent(
                    sequence_index=_resolve_index(
                        tile_xy_to_seq_indices.get(tile.tile_xy)
                    ),
                    resource_type=res_type,
                    resource_id=res_id,
                    exit_edge=None,
                    block_id=tile.block_id,
                    entry_edge=tile.entry_edge,
                    switch_exit_edge=tile.exit_edge,
                )
            )

        # Block entered/vacated events come from the global current detector
        # (design_new.md "Block occupancy detection"), not the precise
        # tile diff. Emit this tick's attributed slice once, on the final
        # no-boundary callback (added and removed both empty), enriched with
        # sequence_index / exit edge from the live plan.
        if not span_update.added_tiles and not span_update.removed_tiles:
            self._emit_detected_block_events(
                entered_resources,
                tail_vacated_resources,
                lambda b: _resolve_index(block_to_seq_indices.get(b)),
                block_to_exit_edge,
            )

        # Deviation is one predicate: the head resource appears nowhere in
        # `resource_sequence`. A head that resolves to an occurrence behind the
        # anchor is a no-advance under the Head-mapping rule, not a deviation.
        deviation = False
        deviation_error = None
        current_head_sequence_index: Optional[int] = None

        if plan is not None:
            # Look up the head tile in the sequence index map. current_span was
            # just written from span_update.new_span above, so the cab-derived
            # head reflects this update. `_resolve_index` picks the occurrence at
            # or after the progress anchor per the Head-mapping rule.
            head_tile_xy = self._c._get_head_tile()

            if head_tile_xy is not None:
                seq_idx = _resolve_index(
                    tile_xy_to_seq_indices.get(head_tile_xy)
                )
                if seq_idx is None:
                    # The tile map missed, so ask the resource. The predicate is
                    # about the head's RESOURCE, not its tile (design_new.md, "Head-mapping rule"
                    # — "the train head is in a resource that appears nowhere in
                    # the active resource_sequence"), and a path's stored
                    # `path_tiles` for a block hold only the tiles the train will
                    # traverse, not every tile of the block.
                    #
                    # A reverse departure is the case that makes the difference
                    # load-bearing. When the path leaves out the train's other
                    # end, `create_path` seeds the sequence starting at the exit
                    # tile of the block the train is standing on — a tile the
                    # train is not on. Tile-matching then reads its own start
                    # block as a deviation and cancels the path on the first tick
                    # after it was assigned, before the operator can even press
                    # GO: no authority, ATC limit 0, and a dead rail under a train
                    # the app is showing as ready to run. The head is on its
                    # authorized path; it is just not yet on the tile the path
                    # will take it out through.
                    head_block_id = self._c._get_current_block_id()
                    seq_idx = (
                        _resolve_index(block_to_seq_indices.get(head_block_id))
                        if head_block_id is not None
                        else None
                    )
                if seq_idx is None:
                    # Head resource is not in the resource_sequence — out of
                    # sequence.
                    deviation = True
                    deviation_error = ErrorRecord(
                        "train head is not on its authorized path",
                        "out_of_sequence",
                    )
                    current_head_sequence_index = None
                else:
                    current_head_sequence_index = seq_idx
                    # Advance the progress anchor to the reported index. Monotonic:
                    # a behind-anchor no-advance resolves to an index < anchor and
                    # leaves the anchor unchanged, so resolution can never slip
                    # back to an earlier occurrence on a later tick.
                    ps_anchor.head_progress_anchor = max(anchor, seq_idx)

        report = ControllerProgressReport(
            train_id=self._c.train_id,
            event_time=float(event_time),
            entered_resources=entered_resources,
            tail_vacated_resources=tail_vacated_resources,
            current_head_sequence_index=current_head_sequence_index,
            deviation=deviation,
            deviation_error=deviation_error,
        )
        self._c.physics_sync.queued_progress_reports.append(report)

    def build_detected_progress_report(self, event_time: float) -> None:
        """Public entry to the detected-blocks-only report, for a caller that
        has no span update to apply.

        The Observe physics callback reaches the same builder through the
        powered-off branch of `on_physics_updated`. The stopped-simulation
        hardware poll has no callback at all — it integrates no motion — so it
        enqueues through here instead. Same report either way: whatever the
        detector confirmed this pass, with no route progress and no deviation
        verdict.
        """
        self._enqueue_detected_blocks_only_report(event_time)

    def _enqueue_detected_blocks_only_report(self, event_time: float) -> None:
        """Queue a report carrying nothing but this tick's detected block events.

        The still-OFF form of the Observe physics callback: no movement happened, so
        there are no tile deltas, and a powered-off transaction carries no route
        progress — the block events go out with no `sequence_index` and no
        deviation verdict. Enqueues nothing when the global detector read attributed no block change
        to this train.
        """
        ps = self._c.physics_sync
        if not ps.pending_detected_entered_blocks and (
            not ps.pending_detected_vacated_blocks
        ):
            return

        entered_resources: List[ResourceEvent] = []
        tail_vacated_resources: List[ResourceEvent] = []
        # No live plan on the OFF path, so the block events carry no
        # sequence_index (the resolver returns None for every block).
        self._emit_detected_block_events(
            entered_resources, tail_vacated_resources, lambda b: None, {}
        )
        ps.queued_progress_reports.append(
            ControllerProgressReport(
                train_id=self._c.train_id,
                event_time=float(event_time),
                entered_resources=entered_resources,
                tail_vacated_resources=tail_vacated_resources,
                current_head_sequence_index=None,
                deviation=False,
            )
        )

    def _emit_detected_block_events(
        self,
        entered_resources: List[ResourceEvent],
        tail_vacated_resources: List[ResourceEvent],
        resolve_block_index: "Callable[[str], Optional[int]]",
        block_to_exit_edge: Dict[str, str],
    ) -> None:
        """Append this tick's detector-confirmed block entered/vacated events to
        the progress report being built (design_new.md "Block occupancy
        detection").

        Blocks are ordered by path sequence_index so a multi-block tick advances
        route waypoints in order; `resolve_block_index` maps a block to the
        occurrence at or after the progress anchor per the Head-mapping rule (it
        returns None on the powered-off path, which has no live plan). The vacate
        exit edge comes from the precise edge captured when the tail cleared the
        block (`pending_block_exit_edges`), falling back to the plan's forward
        exit edge. Clears the per-tick slice after draining it.
        """
        ps = self._c.physics_sync

        for block_id in sorted(
            ps.pending_detected_entered_blocks,
            key=lambda b: (resolve_block_index(b) or 0, b),
        ):
            # A fresh entry means any exit edge still held for this block is
            # left over from an earlier stay: its vacate committed before the
            # calculated tail left the block, so nothing consumed the edge the
            # tail recorded afterwards.
            ps.pending_block_exit_edges.pop(block_id, None)
            entered_resources.append(
                ResourceEvent(
                    sequence_index=resolve_block_index(block_id),
                    resource_type="block",
                    resource_id=block_id,
                    exit_edge=None,
                    block_id=block_id,
                )
            )

        for block_id in sorted(
            ps.pending_detected_vacated_blocks,
            key=lambda b: (resolve_block_index(b) or 0, b),
        ):
            exit_edge = ps.pending_block_exit_edges.pop(block_id, None)
            if exit_edge is None:
                exit_edge = block_to_exit_edge.get(block_id)
            tail_vacated_resources.append(
                ResourceEvent(
                    sequence_index=resolve_block_index(block_id),
                    resource_type="block",
                    resource_id=block_id,
                    exit_edge=exit_edge,
                    block_id=block_id,
                )
            )

        ps.pending_detected_entered_blocks = set()
        ps.pending_detected_vacated_blocks = set()
