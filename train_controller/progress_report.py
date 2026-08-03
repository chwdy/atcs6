"""ProgressReportBuilder — turn a TrainPhysics span update into a Phase-3
progress report for one TrainController.

Extracted from train_controller/__init__.py with behavior unchanged. The
builder is constructed once per controller and reads controller state through
the back-reference ``self._c``; it writes only to ``physics_sync`` (the
per-tick progress queue and the detector entered/vacated slices), mirroring the
other controller sub-modules.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from enums import TrainState
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
        """Apply a TrainPhysics span_update and enqueue a Phase 3 progress report.

        Per design_new.md "Physical progress / deviation procedure" and
        "TrainController" sections:
        1. Atomically write current_span and current_speed_kmh from the update.
        2. Build a ControllerProgressReport from the tile delta (added/removed
           tiles -> entered/tail-vacated resource events).
        3. Append the report to ``self._c.physics_sync.queued_progress_reports``
           for SimulationController to drain in Phase 3.

        For powered-off transactions, the controller still applies span/speed
        but must not enqueue dispatcher/route progress reports because Phase 3
        is skipped. This method honors that rule: if power is off and the
        state is OFF, the queue write is skipped.
        """
        # 1. Storage update (canonical span + speed). The doc's TrainSpan is
        # owned by the train/controller side; until that schema lands here we
        # store the span on the controller (current_span) and let consumers
        # access it via the existing helpers.
        self._c.physics_sync.current_span = span_update.new_span
        self._c.physics_sync.current_speed_kmh = float(span_update.current_speed_kmh)

        # 1a. Reactive speed-based state evaluation (per design_new.md
        # "Reactive speed-based state evaluation"). Fires synchronously after
        # the new speed has been written and before building the progress
        # report, so any READY <-> RUNNING_MOVING transition lands in the same
        # tick that physics produced the speed change.
        if self._c.ops.state == TrainState.READY and not self._c.is_stopped():
            self._c.ops.state = TrainState.RUNNING_MOVING
            if self._c.commands.control_mode == "ato" and self._c.path_projection.received_path_plan is not None:
                # ato_go() is the canonical writer of ato_running = True.
                # From RUNNING_MOVING it is an idempotent success that skips
                # cab auto-selection.
                self._c.ato.go()
        elif (
            self._c.ops.state == TrainState.RUNNING_MOVING
            and self._c.is_stopped()
            and self._c.path_projection.received_path_plan is None
            and self._c.eb.active == False
        ):
            # No-authority braking completion: zero speed reached without
            # live authority. transition_to_ready() clears ato_running and
            # path-run bookkeeping in lockstep. Per design_new.md lines
            # 1783/1788, this reactive path must NOT fire while EB is latched
            # (eb.active == True): the controller stays in RUNNING_MOVING and
            # only TrainController.eb.release() drives the return to READY.
            self._c.transition_to_ready()

        # 2/3. Build progress report from tile delta and enqueue for Phase 3.
        if self._c.ops.state == TrainState.OFF:
            # Powered-off transactions append no dispatcher/route progress to the
            # mailbox BEYOND the block observations folded in from the Step-0
            # detected sets (design_new.md ":2181"). Those observations are the
            # only path by which a train that moves without power — pushed by
            # hand, or an unidentified train, which is always OFF — commits its block
            # entries and vacates, and Phase 3 runs for exactly those ticks.
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
        tile_xy_to_seq_index: Dict[Tuple[int, int], int] = {}
        # block_id -> sequence_index / forward exit edge from the live plan, used
        # to enrich the detector-sourced block events (design_new.md "Power ->
        # Block occupancy detection") the same way tile traversals enrich switch
        # events with their own sequence_index.
        block_to_seq_index: Dict[str, int] = {}
        block_to_exit_edge: Dict[str, str] = {}
        if plan is not None:
            for entry in plan.resource_sequence:
                for path_tile in entry.path_tiles:
                    tile_xy_to_seq_index[(path_tile.x, path_tile.y)] = (
                        entry.sequence_index
                    )
                if getattr(entry, "resource_type", None) == "block":
                    block_to_seq_index[entry.resource_id] = entry.sequence_index
                    if entry.exit_edge:
                        block_to_exit_edge[entry.resource_id] = entry.exit_edge

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
                    sequence_index=tile_xy_to_seq_index.get(tile.tile_xy),
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
                    sequence_index=tile_xy_to_seq_index.get(tile.tile_xy),
                    resource_type=res_type,
                    resource_id=res_id,
                    exit_edge=None,
                    block_id=tile.block_id,
                    entry_edge=tile.entry_edge,
                    switch_exit_edge=tile.exit_edge,
                )
            )

        # Block entered/vacated events come from the global current detector
        # (design_new.md "Power -> Block occupancy detection"), not the precise
        # tile diff. Emit this tick's attributed slice once, on the final
        # no-boundary callback (added and removed both empty), enriched with
        # sequence_index / exit edge from the live plan.
        if not span_update.added_tiles and not span_update.removed_tiles:
            self._emit_detected_block_events(
                entered_resources,
                tail_vacated_resources,
                block_to_seq_index,
                block_to_exit_edge,
            )

        # Deviation is one predicate: the head resource is not in
        # `resource_sequence`. There is no second classification — a head tile
        # that IS in the map cannot have an index past the map's own maximum.
        deviation = False
        deviation_error = None
        current_head_sequence_index: Optional[int] = None

        if plan is not None:
            # Look up the head tile in the sequence index map. current_span was
            # just written from span_update.new_span above, so the cab-derived
            # head reflects this update.
            head_tile_xy = self._c._get_head_tile()

            if head_tile_xy is not None:
                seq_idx = tile_xy_to_seq_index.get(head_tile_xy)
                if seq_idx is None:
                    # Head tile is not in the resource_sequence — out of sequence.
                    deviation = True
                    deviation_error = ErrorRecord(
                        "train head is not on its authorized path",
                        "out_of_sequence",
                    )
                    current_head_sequence_index = None
                else:
                    current_head_sequence_index = seq_idx

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

    def enqueue_detected_blocks_report(self, event_time: float) -> None:
        """Public entry to the detected-blocks-only report, for a caller that
        has no span update to apply.

        The Phase-2 physics callback reaches the same builder through the
        powered-off branch of `on_physics_updated`. The stopped-simulation
        hardware poll has no callback at all — it integrates no motion — so it
        enqueues through here instead. Same report either way: whatever the
        detector confirmed this pass, with no route progress and no deviation
        verdict.
        """
        self._enqueue_detected_blocks_only_report(event_time)

    def _enqueue_detected_blocks_only_report(self, event_time: float) -> None:
        """Queue a report carrying nothing but this tick's detected block events.

        The still-OFF form of the Phase-2 callback: no movement happened, so
        there are no tile deltas, and a powered-off transaction carries no route
        progress — the block events go out with no `sequence_index` and no
        deviation verdict. Enqueues nothing when Step 0 attributed no block change
        to this train.
        """
        ps = self._c.physics_sync
        if not ps.pending_detected_entered_blocks and (
            not ps.pending_detected_vacated_blocks
        ):
            return

        entered_resources: List[ResourceEvent] = []
        tail_vacated_resources: List[ResourceEvent] = []
        self._emit_detected_block_events(
            entered_resources, tail_vacated_resources, {}, {}
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
        block_to_seq_index: Dict[str, int],
        block_to_exit_edge: Dict[str, str],
    ) -> None:
        """Append this tick's detector-confirmed block entered/vacated events to
        the progress report being built (design_new.md "Power -> Block occupancy
        detection").

        Blocks are ordered by path sequence_index so a multi-block tick advances
        route waypoints in order. The vacate exit edge comes from the precise
        edge captured when the tail cleared the block (`pending_block_exit_edges`),
        falling back to the plan's forward exit edge. Clears the per-tick slice
        after draining it.
        """
        ps = self._c.physics_sync

        for block_id in sorted(
            ps.pending_detected_entered_blocks,
            key=lambda b: (block_to_seq_index.get(b, 0), b),
        ):
            entered_resources.append(
                ResourceEvent(
                    sequence_index=block_to_seq_index.get(block_id),
                    resource_type="block",
                    resource_id=block_id,
                    exit_edge=None,
                    block_id=block_id,
                )
            )

        for block_id in sorted(
            ps.pending_detected_vacated_blocks,
            key=lambda b: (block_to_seq_index.get(b, 0), b),
        ):
            exit_edge = ps.pending_block_exit_edges.pop(block_id, None)
            if exit_edge is None:
                exit_edge = block_to_exit_edge.get(block_id)
            tail_vacated_resources.append(
                ResourceEvent(
                    sequence_index=block_to_seq_index.get(block_id),
                    resource_type="block",
                    resource_id=block_id,
                    exit_edge=exit_edge,
                    block_id=block_id,
                )
            )

        ps.pending_detected_entered_blocks = set()
        ps.pending_detected_vacated_blocks = set()
