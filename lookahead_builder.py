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
themselves read the controller's `current_span` / `path_dispatcher`. Future PRs may
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

        Walks the live `received_path_plan.resource_sequence` (tail→authority
        order per the schema) starting at the head's anchor resource and
        collects block resources in forward order. The current block is
        skipped so callers see only blocks past the head.
        """
        c = self._c
        train = c._get_train()
        if not train:
            return []

        plan = c.path_projection.received_path_plan
        resource_sequence = (
            getattr(plan, "resource_sequence", None) if plan else None
        )
        if not resource_sequence:
            return []

        current_block_id = c._get_current_block_id()
        head_tile = c._get_head_tile()

        head_resource_index = -1
        if head_tile:
            for r_idx, resource in enumerate(resource_sequence):
                for tile_entry in resource.path_tiles:
                    if (tile_entry.x, tile_entry.y) == head_tile:
                        head_resource_index = r_idx
                        break
                if head_resource_index >= 0:
                    break

        if head_resource_index < 0:
            # Head not located in resource_sequence — fall back to anchoring
            # at the current_block resource if present, otherwise the start.
            if current_block_id:
                for r_idx, resource in enumerate(resource_sequence):
                    if (
                        resource.resource_type == "block"
                        and resource.resource_id == current_block_id
                    ):
                        head_resource_index = r_idx
                        break
            if head_resource_index < 0:
                head_resource_index = 0

        blocks: List[str] = []
        for r_idx in range(head_resource_index, len(resource_sequence)):
            resource = resource_sequence[r_idx]
            if resource.resource_type != "block":
                continue
            if resource.resource_id == current_block_id:
                continue
            if resource.resource_id not in blocks:
                blocks.append(resource.resource_id)

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
