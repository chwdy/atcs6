"""
LookaheadBuilder — owns the lookahead computation pulled out of
`TrainController`.

Per design_new.md "Lookahead": the lookahead is a pure derived projection of
`(current_span, cab, received_path_plan, network_state, signals,
current_speed_kmh, eb_active)` that recomputes on every read. This module
centralises the computation in one place so the controller does not also
own the underlying graph walks.

The builder holds a back-reference to `TrainController` because the walk
needs many controller-side helpers (`_get_current_block_id`,
`_get_remaining_distance_in_current_block`, `get_path_tiles`, etc.) that
themselves read `train.position` / `path_dispatcher`. Future PRs may
purify those helpers; for now the builder is the structural owner and
`TrainController` keeps thin forwarding shims that delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from enums import SignalState
from train_controller_types import LookaheadPoint

if TYPE_CHECKING:
    from train_controller import TrainController


class LookaheadBuilder:
    """Computes `LookaheadPoint` lists for a `TrainController`.

    The class is constructed once per controller and lazy-initialised on
    first use. All methods are read-only with respect to controller state.
    """

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    # ------------------------------------------------------------------
    # Top-level builder
    # ------------------------------------------------------------------

    def build(self) -> List[LookaheadPoint]:
        """Build the ordered list of `LookaheadPoint`s ahead of the train.

        Returns a list ordered by distance from the train head, starting
        with the current block at distance 0. The final entry carries
        ``is_path_end=True`` when the path runs out (or when there is no
        path and we hit the end-of-track fallback).
        """
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return []

        lookahead_points: List[LookaheadPoint] = []
        current_distance = 0.0

        current_block_id = c._get_current_block_id()
        if current_block_id:
            remaining_in_current = c._get_remaining_distance_in_current_block()
            current_block = c.network.blocks.get(current_block_id)
            if current_block:
                signal_state = self.signal_state_for_block(current_block_id)
                station_id = self.station_in_block(current_block_id)

                lookahead_points.append(
                    LookaheadPoint(
                        distance_cm=0.0,
                        speed_limit_kmh=current_block.speed_limit_kmh or float("inf"),
                        block_id=current_block_id,
                        signal_state=signal_state,
                        station_id=station_id,
                        is_path_end=False,
                    )
                )
                current_distance = remaining_in_current

        blocks_ahead = self.blocks_ahead()

        if not blocks_ahead:
            if current_block_id:
                lookahead_points.append(
                    LookaheadPoint(
                        distance_cm=current_distance,
                        speed_limit_kmh=0.0,  # Must stop at path end
                        block_id=current_block_id,
                        signal_state=None,
                        station_id=None,
                        is_path_end=True,
                    )
                )
            return lookahead_points

        for i, block_id in enumerate(blocks_ahead):
            block = c.network.blocks.get(block_id)
            if not block:
                continue

            signal_state = self.signal_state_for_block(block_id)
            station_id = self.station_in_block(block_id)
            is_last = i == len(blocks_ahead) - 1

            lookahead_points.append(
                LookaheadPoint(
                    distance_cm=current_distance,
                    speed_limit_kmh=block.speed_limit_kmh or float("inf"),
                    block_id=block_id,
                    signal_state=signal_state,
                    station_id=station_id,
                    is_path_end=is_last,
                )
            )

            current_distance += block.length_cm

        return lookahead_points

    # ------------------------------------------------------------------
    # Block-walk helpers (used by ATC clearance + lookahead build)
    # ------------------------------------------------------------------

    def blocks_ahead(self) -> List[str]:
        """Ordered block IDs ahead of the train.

        Prefers walking the active path's tile sequence (so non-trivial
        cab/path orientations resolve correctly) and falls back to the
        active path's reserved-block list when no tile sequence is
        available.
        """
        c = self._c
        train = c._get_train()
        if not train:
            return []

        path_tiles = c.get_path_tiles()
        blocks: List[str] = []

        current_block_id = c._get_current_block_id()

        if path_tiles:
            head_tile = c._get_head_tile()
            if head_tile:
                head_index = c._get_head_path_index(head_tile)
                if head_index >= 0:
                    scan_step = c._get_path_scan_step(train, head_index)
                    loop_start = head_index + scan_step
                    loop_end = len(path_tiles) if scan_step == 1 else -1

                    for i in range(loop_start, loop_end, scan_step):
                        path_entry = path_tiles[i]
                        track = c.network.get_track(path_entry[0], path_entry[1])
                        block_id = track.block_section_id if track else None
                        if not block_id or block_id == current_block_id:
                            continue
                        if block_id not in blocks:
                            blocks.append(block_id)

                    if blocks:
                        return blocks

        active_path = (
            c.path_dispatcher.get_active_path(c.train_id)
            if c.path_dispatcher is not None
            else None
        )
        if active_path is not None:
            path_blocks = active_path.get_all_blocks()
            if current_block_id and current_block_id in path_blocks:
                idx = path_blocks.index(current_block_id)
                scan_step = 1
                head_tile = c._get_head_tile()
                if head_tile and path_tiles:
                    head_index = c._get_head_path_index(head_tile)
                    if head_index >= 0:
                        scan_step = c._get_path_scan_step(train, head_index)
                if scan_step == -1:
                    blocks = list(reversed(path_blocks[:idx]))
                else:
                    blocks = path_blocks[idx + 1 :]
            elif path_blocks:
                blocks = list(path_blocks)

        return blocks

    # ------------------------------------------------------------------
    # Pure (network-only) helpers
    # ------------------------------------------------------------------

    def signal_state_for_block(self, block_id: str) -> Optional[SignalState]:
        """Return the signal state monitoring `block_id`, or None.

        Display-only read: walks `network.signals` looking for the first
        signal whose `monitored_block_id` matches.
        """
        if not self._c.network:
            return None
        for _signal_id, signal in self._c.network.signals.items():
            if (
                hasattr(signal, "monitored_block_id")
                and signal.monitored_block_id == block_id
            ):
                return signal.state
        return None

    def station_in_block(self, block_id: str) -> Optional[str]:
        """Return the station id contained in `block_id`, or None."""
        if not self._c.network:
            return None
        for track in self._c.network.tracks.values():
            if track.block_section_id == block_id and track.station_id:
                return track.station_id
        return None

    def current_block_speed_limit(self) -> float:
        """Block speed limit at the train head, or `inf` when unknown."""
        c = self._c
        head_tile = c._get_head_tile()
        if not head_tile or not c.network:
            return float("inf")

        track = c.network.tracks.get(head_tile)
        if not track:
            return float("inf")
        block = c.network.blocks.get(track.block_section_id)
        if not block:
            return float("inf")
        return block.speed_limit_kmh or float("inf")
