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
`_get_remaining_distance_in_current_block`, `_get_effective_path_tiles`, etc.) that
themselves read the controller's `current_span` / `path_dispatcher`. Future PRs may
purify those helpers; for now the builder is the structural owner and
`TrainController` keeps thin forwarding shims that delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from enums import AtcLimitReasonCode, BlockState, Direction, SignalState
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
    # Canonical lookahead payload (Train Monitor)
    # ------------------------------------------------------------------

    def compute_payload(self) -> Dict[str, Any]:
        from train_controller import EDGE_TO_DIRECTION_NAME

        train = self._c._get_train()
        if not train or not self._c.network:
            # Per design_new.md, the canonical lookahead payload: emit the distance-0 anchor with a
            # structured reason code so consumers always see `speed_curves`
            # with at least one entry rather than an empty list.
            return self._build_empty_monitor_lookahead(
                speed_limit_reason="no_train",
                reason_code=AtcLimitReasonCode.NO_TRAIN,
            )

        # There is no cab gate: the head end is derived from the live authority
        # (design_new.md "Reservation-side head derivation"). A train with no live
        # authority falls through to the `no_reservation` empty payload below.
        head_tile = self._c._get_head_tile()
        head_distance_cm = self._c._get_head_distance_cm()
        facing = self._c._facing_direction()
        travel_direction = facing.name if facing is not None else None

        if not head_tile:
            # No reason published — head_tile being None is a missing-position
            # case, not an ATC condition.
            return self._build_empty_monitor_lookahead(
                head_distance_cm=head_distance_cm,
                travel_direction=travel_direction,
                speed_limit_reason=None,
            )

        plan = self._c.path_projection.received_path_plan
        resource_sequence = (
            getattr(plan, "resource_sequence", None) if plan else None
        )
        if not plan or not resource_sequence:
            # Per design_new.md step `22a9` (canonical lookahead), without a
            # path plan the canonical payload is the no-authority empty form:
            # a single distance-0 anchor with atc_limit/operational_limit=0
            # and reason `no_reservation`. There is no block-bounded fallback;
            # movement authority is binary on `received_path_plan`.
            # EB is a higher-priority safety state — when EB is latched the
            # reason is stamped as `eb` even though authority is also absent
            # (the cancellation-during-EB sequence drops authority too, and
            # operators should see the safety state, not the latent
            # "no_reservation" condition).
            if self._c.eb.active:
                reason_code = AtcLimitReasonCode.EB
                reason_detail = "Emergency brake active"
            else:
                reason_code = AtcLimitReasonCode.NO_RESERVATION
                reason_detail = "no_reservation"
            return self._build_empty_monitor_lookahead(
                head_tile=head_tile,
                head_distance_cm=head_distance_cm,
                travel_direction=travel_direction,
                speed_limit_reason=reason_detail,
                reason_code=reason_code,
            )

        # Locate the head tile inside the resource_sequence (tail→authority
        # order per the canonical resource_sequence schema). The head must
        # live in some resource's path_tiles slice.
        head_resource_index = -1
        head_tile_index = -1
        for r_idx, resource in enumerate(resource_sequence):
            for t_idx, tile_entry in enumerate(resource.path_tiles):
                if (tile_entry.x, tile_entry.y) == head_tile:
                    head_resource_index = r_idx
                    head_tile_index = t_idx
                    break
            if head_resource_index >= 0:
                break

        head_track = self._c.network.get_track(head_tile[0], head_tile[1])
        head_block_id: Optional[str] = (
            head_track.block_id if head_track else None
        )

        # Recovery for the freshly-departed case: a Path's resource_sequence
        # describes only the authority *forward* of the head, so when the
        # head sits behind the first path tile (e.g. just after `depart()`)
        # its tile is not in any `path_tiles` slice. When the head's block
        # matches the first authority block we anchor the loop at path[0]'s
        # first tile, then below add the in-block portion behind that first
        # path tile to `block_back_length_cm` so the head's block still
        # reports its true length and the chart frame stays aligned to the
        # head's actual position. When the head's block doesn't match
        # path[0]'s block either, the path is structurally disconnected from
        # the head and the function publishes the no-authority empty payload
        # (per step `22a9`) so the train doesn't drive under fabricated
        # block-bounded authority.
        recovery_extra_back_cm = 0.0
        recovery_extra_back_tiles = 0
        if head_resource_index < 0:
            if not (
                head_block_id is not None
                and resource_sequence[0].resource_type == "block"
                and resource_sequence[0].resource_id == head_block_id
                and resource_sequence[0].path_tiles
            ):
                # Path exists but the head's tile is not in its
                # resource_sequence and the head's block doesn't match the
                # first authority block either — the head is off its own
                # authority. Per design_new.md step `22a9`, we publish the
                # empty distance-0 anchor rather than silently fabricating
                # block-bounded authority.
                #
                # Per design_new.md, the canonical lookahead payload: when a movement-derived
                # deviation cancel is latched (initiator "deviation"), the live
                # path stays ACTIVE with its locks while the head has run off
                # it, so the anchor reports `atc_limit = 0` with reason
                # `deviation`. ATC brakes the train and the overspeed branch in
                # step `826a` escalates a still-rolling train to EB; the gated
                # Action explicit-cancel step commits the latch once stopped.
                # The `no_reservation` reason remains for a genuinely-absent plan
                # (handled in the `not plan` branch above) and for any other
                # head-off-path case with no deviation latch pending.
                latched_cancel = (
                    self._c.route_hooks.phase_latches.path_cancel_request
                )
                # Key on the structured classification boolean, never on
                # anything inside `error`: design_new.md "Latch Table" makes
                # `deviation` the only cancellation classification runtime code
                # may read, and `ErrorRecord.origin` a diagnostic label nothing
                # branches on.
                if latched_cancel is not None and latched_cancel.deviation:
                    return self._build_empty_monitor_lookahead(
                        head_tile=head_tile,
                        head_distance_cm=head_distance_cm,
                        travel_direction=travel_direction,
                        speed_limit_reason="deviation",
                        reason_code=AtcLimitReasonCode.DEVIATION,
                    )
                return self._build_empty_monitor_lookahead(
                    head_tile=head_tile,
                    head_distance_cm=head_distance_cm,
                    travel_direction=travel_direction,
                    speed_limit_reason="no_reservation",
                    reason_code=AtcLimitReasonCode.NO_RESERVATION,
                )
            head_resource_index = 0
            head_tile_index = 0
            # Compute the head's position within its block and the position
            # of path[0]'s first tile within the same block. The gap between
            # them is the distance the head still has to travel before
            # reaching the path; the in-block prefix before path[0] becomes
            # extra `block_back` content the loop wouldn't otherwise see.
            first_pt = resource_sequence[0].path_tiles[0]
            first_pt_tile = (first_pt.x, first_pt.y)
            block_positions = (
                self._c.network.get_block_positions(head_block_id) or []
            )
            try:
                head_idx = block_positions.index(head_tile)
                first_pt_idx = block_positions.index(first_pt_tile)
            except ValueError:
                head_idx = -1
                first_pt_idx = -1
            if 0 <= head_idx < first_pt_idx:
                # Sum lengths of every block tile up to (but not including)
                # path[0]'s first tile — that is the back portion of the
                # head's block that the path doesn't enumerate.
                for prev_tile in block_positions[:first_pt_idx]:
                    recovery_extra_back_cm += self._c.network._get_tile_length_cm(
                        prev_tile[0], prev_tile[1]
                    )
                    recovery_extra_back_tiles += 1
                # Distance from head's actual position to start of path[0]'s
                # first tile (= "gap"). Equals back-in-block-up-to-path[0]
                # minus the head's already-traversed portion of the block.
                head_in_block_cm = sum(
                    self._c.network._get_tile_length_cm(t[0], t[1])
                    for t in block_positions[:head_idx]
                ) + head_distance_cm
                gap_to_first_path_tile_cm = max(
                    0.0, recovery_extra_back_cm - head_in_block_cm
                )
                # Set head_distance_cm so that the loop's
                # `cumulative_distance_cm = -head_distance_cm` initialization
                # places path[0]'s first tile at exactly `gap_to_first_path_tile_cm`.
                head_distance_cm = -gap_to_first_path_tile_cm
            else:
                # Couldn't resolve positions; fall back to placing path[0]
                # at the lookahead origin (chart shifted by ~one tile).
                head_distance_cm = 0.0

        blocks: List[Dict[str, Any]] = []
        signals: List[Dict[str, Any]] = []
        switches: List[Dict[str, Any]] = []
        stations: List[Dict[str, Any]] = []

        cumulative_distance_cm = -head_distance_cm

        current_block_id: Optional[str] = head_block_id

        # Sum the tail length of the head's block — tiles that share the
        # same block_id and lie before the head in tail→authority order. The
        # walk extends backward through the head resource, then through any
        # earlier resources that still belong to the same block.
        block_back_length_cm, block_back_tile_count = self._sum_matching_back_walk(
            resource_sequence,
            head_resource_index,
            head_tile_index,
            current_block_id,
            lambda t: t.block_id,
        )

        # When recovery picked up a same-block prefix that the path doesn't
        # enumerate (head sits behind path[0] in the same block), fold that
        # prefix into the back-walk totals so the first block's reported
        # length, tile_count and start_cm cover the entire block.
        if recovery_extra_back_cm > 0.0:
            block_back_length_cm += recovery_extra_back_cm
            block_back_tile_count += recovery_extra_back_tiles

        # Sum the tail length of the head's station — tiles before the head
        # in the path that share the same platform_id. Mirrors the block
        # back-walk above so the station rect's left edge can sit behind
        # the train marker once the head has crossed into the station,
        # instead of being pinned to the marker (distance_cm = 0).
        head_platform_id: Optional[str] = (
            head_track.station_id if head_track else None
        )
        station_back_length_cm, station_back_tile_count = self._sum_matching_back_walk(
            resource_sequence,
            head_resource_index,
            head_tile_index,
            head_platform_id,
            lambda t: t.station_id,
        )

        current_block_start_cm = (
            cumulative_distance_cm - block_back_length_cm
            if current_block_id
            else cumulative_distance_cm
        )

        seen_blocks: set[str] = set()
        seen_signals: set[str] = set()
        seen_switches: set[Tuple[int, int]] = set()
        seen_stations: set[str] = set()

        is_first_step = True

        for r_idx in range(head_resource_index, len(resource_sequence)):
            resource = resource_sequence[r_idx]
            tile_start = head_tile_index if r_idx == head_resource_index else 0

            for t_idx in range(tile_start, len(resource.path_tiles)):
                tile_entry = resource.path_tiles[t_idx]
                x, y = tile_entry.x, tile_entry.y
                track = self._c.network.get_track(x, y)
                tile_length = (
                    self._c.network._get_tile_length_cm(x, y) if track else 50.0
                )
                tile_block_id = track.block_id if track else None
                tile_travel_direction = EDGE_TO_DIRECTION_NAME.get(
                    tile_entry.exit_edge
                )

                point_element_start = max(0.0, cumulative_distance_cm)

                if tile_block_id and tile_block_id != current_block_id:
                    current_block_id = tile_block_id
                    current_block_start_cm = cumulative_distance_cm

                if tile_block_id and tile_block_id not in seen_blocks:
                    seen_blocks.add(tile_block_id)
                    block_length = 0.0
                    tile_count = 0
                    done = False
                    for fr_idx in range(r_idx, len(resource_sequence)):
                        forward_resource = resource_sequence[fr_idx]
                        ft_start = t_idx if fr_idx == r_idx else 0
                        for ft_idx in range(
                            ft_start, len(forward_resource.path_tiles)
                        ):
                            ft_entry = forward_resource.path_tiles[ft_idx]
                            ft_track = self._c.network.get_track(
                                ft_entry.x, ft_entry.y
                            )
                            ft_block_id = (
                                ft_track.block_id if ft_track else None
                            )
                            if ft_block_id != tile_block_id:
                                done = True
                                break
                            block_length += self._c.network._get_tile_length_cm(
                                ft_entry.x, ft_entry.y
                            )
                            tile_count += 1
                        if done:
                            break

                    if (
                        tile_block_id == current_block_id
                        and is_first_step
                        and block_back_length_cm > 0.0
                    ):
                        block_length += block_back_length_cm
                        tile_count += block_back_tile_count

                    block_state = None
                    block_speed_limit: Optional[float] = None
                    block = self._c.network.blocks.get(tile_block_id)
                    if block:
                        if block.state:
                            block_state = block.state.value
                        if (
                            block.speed_limit_kmh
                            and block.speed_limit_kmh < float("inf")
                        ):
                            block_speed_limit = block.speed_limit_kmh

                    blocks.append(
                        {
                            "id": tile_block_id,
                            "name": tile_block_id,
                            "state": block_state or BlockState.FREE.value,
                            "start_cm": current_block_start_cm,
                            "end_cm": current_block_start_cm + block_length,
                            "length_cm": block_length,
                            "tile_count": tile_count,
                            "speed_limit_kmh": block_speed_limit,
                        }
                    )

                if track and track.signal_ids and tile_travel_direction:
                    train_dir = tile_travel_direction.upper()
                    for signal_id in track.signal_ids:
                        if signal_id in seen_signals:
                            continue
                        signal = self._c.network.signals.get(signal_id)
                        if not signal:
                            continue
                        sig_dir = (signal.detection_direction or "").upper()
                        dir_matches = (
                            sig_dir == train_dir
                            or sig_dir == train_dir[:1]
                            or sig_dir[:1] == train_dir[:1]
                        )
                        if not dir_matches:
                            continue
                        seen_signals.add(signal_id)
                        signal_distance = (
                            cumulative_distance_cm
                            if is_first_step
                            else point_element_start
                        )
                        signals.append(
                            {
                                "id": signal_id,
                                "name": signal.name or signal_id,
                                "state": signal.state.value
                                if signal.state
                                else "unknown",
                                "distance_cm": signal_distance,
                            }
                        )

                if track and track.is_switch:
                    switch_key = (x, y)
                    if switch_key not in seen_switches:
                        seen_switches.add(switch_key)
                        switch_entity = (
                            self._c.network.get_switch_at(x, y)
                            if hasattr(self._c.network, "get_switch_at")
                            else None
                        )
                        switches.append(
                            {
                                "x": x,
                                "y": y,
                                "name": switch_entity.name
                                if switch_entity and switch_entity.name
                                else f"Switch ({x},{y})",
                                "position": "main"
                                if track.switch_state == 0
                                else "diverging",
                                "distance_cm": point_element_start,
                                "locked": switch_entity.is_locked()
                                if switch_entity is not None
                                else False,
                            }
                        )

                if track and track.station_id:
                    platform_id = track.station_id
                    if platform_id not in seen_stations:
                        seen_stations.add(platform_id)
                        station = self._c.network.platforms.get(platform_id)
                        used_canonical_length = bool(
                            station and station.length_cm
                        )
                        station_length = station.length_cm if station else 0.0
                        station_tile_count = 0
                        if not station_length:
                            done = False
                            for fr_idx in range(r_idx, len(resource_sequence)):
                                forward_resource = resource_sequence[fr_idx]
                                ft_start = t_idx if fr_idx == r_idx else 0
                                for ft_idx in range(
                                    ft_start, len(forward_resource.path_tiles)
                                ):
                                    ft_entry = forward_resource.path_tiles[ft_idx]
                                    ft_track = self._c.network.get_track(
                                        ft_entry.x, ft_entry.y
                                    )
                                    if (
                                        not ft_track
                                        or ft_track.station_id != platform_id
                                    ):
                                        done = True
                                        break
                                    station_length += (
                                        self._c.network._get_tile_length_cm(
                                            ft_entry.x, ft_entry.y
                                        )
                                    )
                                    station_tile_count += 1
                                if done:
                                    break
                        # When the head sits inside this station, anchor
                        # the rect's left edge behind the train marker so
                        # it slides off with the blocks above instead of
                        # being pinned to 0. If the length came from the
                        # forward walk (station.length_cm was missing),
                        # also fold the back-walk tiles in so the rect
                        # spans the station's full footprint.
                        use_back_walk = (
                            is_first_step
                            and platform_id == head_platform_id
                            and station_back_length_cm > 0.0
                        )
                        station_distance_cm = (
                            cumulative_distance_cm - station_back_length_cm
                            if use_back_walk
                            else point_element_start
                        )
                        if use_back_walk and not used_canonical_length:
                            station_length += station_back_length_cm
                            station_tile_count += station_back_tile_count
                        station_group_id: Optional[str] = None
                        if hasattr(self._c.network, "get_station_for_platform"):
                            try:
                                station_group_id = (
                                    self._c.network.get_station_for_platform(
                                        platform_id
                                    )
                                )
                            except Exception:
                                station_group_id = None
                        stations.append(
                            {
                                "platform_id": platform_id,
                                "station_id": station_group_id,
                                "name": station.name if station else platform_id,
                                "distance_cm": station_distance_cm,
                                "length_cm": station_length,
                                "tile_count": station_tile_count or 1,
                                "top_speed_kmh": station.top_speed_kmh
                                if station
                                else None,
                            }
                        )

                cumulative_distance_cm += tile_length
                is_first_step = False

        # The head→authority-end clearance is the in-walk locator-to-locator
        # projection owned by `RailwayPathfinder.measure_along_walk`
        # (design_new.md, "Pathfinding-home migration"): sum the forward tile lengths from the
        # head's tile through the authority end, netting out the head's already-
        # traversed in-tile offset. `head_distance_cm` carries that offset (and
        # is negative in the freshly-departed recovery anchor, which the helper
        # nets back in). This reproduces the incremental `cumulative_distance_cm`
        # the placement loop above tracks; that accumulation stays for per-tile
        # block/station/signal placement.
        clearance_cm = self._measure_head_to_authority_clearance(
            head_tile=head_tile,
            head_resource_index=head_resource_index,
            head_tile_index=head_tile_index,
            head_distance_cm=head_distance_cm,
            resource_sequence=resource_sequence,
        )

        # Calculate effective safety margin (5cm for platform, 25cm otherwise)
        effective_safety_margin = self._c.atc.effective_safety_margin()
        # Target stop point is clearance minus safety margin.
        # `reservation_boundary_delta_cm` is the forward distance from the head to
        # the safety-margin-adjusted authority boundary; with an active path,
        # that is the same axis as the speed_curves samples. The boundary
        # projection is owned by `RailwayNetwork.project_authority_boundary_delta`
        # (design_new.md, "Pathfinding-home migration"): the head→authority-end clearance just walked is
        # carried in the head locator, and the authority end is the final block
        # resource's directed frame.
        target_stop_cm = max(0.0, clearance_cm - effective_safety_margin)
        reservation_boundary_delta_cm = (
            self._project_authority_boundary_delta(
                head_tile=head_tile,
                head_block_id=head_block_id,
                clearance_cm=clearance_cm,
                resource_sequence=resource_sequence,
                safety_margin_cm=effective_safety_margin,
            )
        )

        # Determine override reason code for forced-zero ATC cases. EB and
        # wrong-direction-cab force ATC to zero regardless of geometry; in
        # those cases every speed_curves sample carries the canonical reason.
        # The EB trigger context ("ATC overspeed", "operator EB", etc.) is
        # published as a separate NotificationCenter event when EB activates;
        # the lookahead detail only carries the steady-state string.
        override_code: Optional[AtcLimitReasonCode] = None
        override_detail: Optional[str] = None
        if self._c.eb.active:
            override_code = AtcLimitReasonCode.EB
            override_detail = "Emergency brake active"

        speed_curves = self._c._get_speed_curve_calculator().calculate_speed_curves(
            max_distance_cm=clearance_cm if clearance_cm > 0 else 500.0,
            lookahead_blocks=blocks,
            lookahead_signals=signals,
            lookahead_stations=stations,
            target_stop_cm=target_stop_cm,
            override_reason_code=override_code,
            override_reason_detail=override_detail,
        )

        # Top-level `speed_limit_reason` is a convenience read of the
        # canonical position-0 detail; derive it locally to avoid recursing
        # back through the `speed_limit_reason` property.
        top_reason: Optional[str] = None
        if speed_curves:
            top_reason = (
                speed_curves[0].get("atc_limit_reason_detail")
                or (
                    str(speed_curves[0].get("atc_limit_reason_code"))
                    if speed_curves[0].get("atc_limit_reason_code")
                    else None
                )
            )

        return {
            "train_id": self._c.train_id,
            "head_tile": [head_tile[0], head_tile[1]],
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": ("B" if self._c._framed_head_end == "end_b" else "A"),
            "speed_limit_reason": top_reason,
            "lookahead": {
                "blocks": blocks,
                "signals": signals,
                "switches": switches,
                "stations": stations,
                "clearance_cm": clearance_cm,
                "safety_margin_cm": effective_safety_margin,
                "target_stop_cm": target_stop_cm,
                "reservation_boundary_delta_cm": reservation_boundary_delta_cm,
                "speed_curves": speed_curves,
            },
        }

    def _build_empty_monitor_lookahead(
        self,
        *,
        head_tile: Optional[Tuple[int, int]] = None,
        head_distance_cm: float = 0.0,
        travel_direction: Optional[str] = None,
        speed_limit_reason: Optional[str] = None,
        reason_code: Optional[AtcLimitReasonCode] = None,
    ) -> Dict[str, Any]:
        """Build a consistent empty lookahead payload for Train Monitor.

        Per design_new.md "canonical lookahead payload", the empty form
        preserves the full top-level and nested key shape with empty lists and
        zero distances. When `reason_code` is provided, a single distance-0
        anchor is stamped onto `speed_curves` so consumers reading the
        canonical reason at distance 0 (target_speed / speed_limit_reason
        properties) see the structured `AtcLimitReasonCode` without a separate
        controller-owned cache. Used for the no-cab, no-authority (no path),
        and head-off-path cases.
        """
        payload: Dict[str, Any] = {
            "train_id": self._c.train_id,
            "head_tile": list(head_tile) if head_tile is not None else None,
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": ("B" if self._c._framed_head_end == "end_b" else "A"),
            "speed_limit_reason": speed_limit_reason,
            "lookahead": {
                "blocks": [],
                "signals": [],
                "switches": [],
                "stations": [],
                "clearance_cm": 0.0,
                "safety_margin_cm": 0.0,
                "reservation_boundary_delta_cm": 0.0,
                "target_stop_cm": 0.0,
                "speed_curves": [],
            },
        }
        if reason_code is not None:
            payload["lookahead"]["speed_curves"] = [
                {
                    "distance_cm": 0.0,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                    "atc_limit_reason_code": reason_code.value,
                    "atc_limit_reason_detail": speed_limit_reason,
                }
            ]
        return payload

    def _measure_head_to_authority_clearance(
        self,
        head_tile: Optional[Tuple[int, int]],
        head_resource_index: int,
        head_tile_index: int,
        head_distance_cm: float,
        resource_sequence: List[Any],
    ) -> float:
        """Measure the head→authority-end clearance via
        `RailwayPathfinder.measure_along_walk` (design_new.md, "Pathfinding-home migration").

        Builds the forward directed walk (one `GraphWalkStep` per tile, from the
        head's position through the authority end) out of `resource_sequence`,
        anchors the head locator with its in-tile offset (`head_distance_cm`),
        and delegates the cumulative tile-length projection to the shared helper.
        The result equals the inline `max(0, cumulative_distance_cm)` the
        placement loop tracks.
        """
        from path_utils import GraphLocator, GraphWalkStep
        from railway_pathfinder import RailwayPathfinder

        ordered_walk: List[GraphWalkStep] = []
        step_index = 0
        last_tile: Optional[Tuple[int, int]] = None
        for r_idx in range(head_resource_index, len(resource_sequence)):
            resource = resource_sequence[r_idx]
            tile_start = head_tile_index if r_idx == head_resource_index else 0
            for t_idx in range(tile_start, len(resource.path_tiles)):
                tile_entry = resource.path_tiles[t_idx]
                x, y = tile_entry.x, tile_entry.y
                track = self._c.network.get_track(x, y)
                tile_length = (
                    self._c.network._get_tile_length_cm(x, y) if track else 50.0
                )
                ordered_walk.append(
                    GraphWalkStep(
                        step_index=step_index,
                        resource_type=resource.resource_type,
                        resource_id=resource.resource_id,
                        tile=(x, y),
                        entry_edge=tile_entry.entry_edge,
                        exit_edge=tile_entry.exit_edge,
                        length_cm=tile_length,
                    )
                )
                last_tile = (x, y)
                step_index += 1

        if not ordered_walk or head_tile is None or last_tile is None:
            return 0.0

        pathfinder = RailwayPathfinder(self._c.network)
        head_locator = GraphLocator(tile=head_tile, distance_cm=head_distance_cm)
        end_locator = GraphLocator(tile=last_tile)
        return pathfinder.measure_along_walk(
            ordered_walk, head_locator, end_locator
        )

    def _project_authority_boundary_delta(
        self,
        head_tile: Optional[Tuple[int, int]],
        head_block_id: Optional[str],
        clearance_cm: float,
        resource_sequence: List[Any],
        safety_margin_cm: float,
    ) -> float:
        """Build the head locator + authority-end frame and route the boundary
        projection through `RailwayNetwork.project_authority_boundary_delta`
        (design_new.md, "Pathfinding-home migration").

        The head→authority-end clearance the lookahead walk just produced is
        carried on the head locator's `distance_cm`; the authority end is the
        final block resource of `resource_sequence`. The network helper owns the
        safety-margin subtraction and the at-rest clamp.
        """
        from path_utils import GraphLocator, DirectedBlockFrame

        head_locator = GraphLocator(
            tile=head_tile if head_tile is not None else (0, 0),
            resource_type="block" if head_block_id else None,
            resource_id=head_block_id,
            block_id=head_block_id,
            distance_cm=clearance_cm,
        )

        # Authority end = the last block resource in the tail→authority walk.
        authority_resource = None
        for entry in reversed(resource_sequence):
            if getattr(entry, "resource_type", None) == "block":
                authority_resource = entry
                break

        authority_end: Optional[DirectedBlockFrame] = None
        if authority_resource is not None:
            edge_to_direction = {
                "N": Direction.NORTH,
                "E": Direction.EAST,
                "S": Direction.SOUTH,
                "W": Direction.WEST,
            }
            tiles = [
                (t.x, t.y, edge_to_direction.get(t.exit_edge))
                for t in authority_resource.path_tiles
            ]
            authority_end = DirectedBlockFrame(
                block_id=authority_resource.resource_id,
                block_direction="forward",
                path_tiles=tiles,
                entry_edge=authority_resource.entry_edge or "N",
                exit_edge=authority_resource.exit_edge or "N",
            )

        if authority_end is None:
            # No block resource in the walk — preserve the inline arithmetic.
            return max(0.0, max(0.0, clearance_cm) - safety_margin_cm)

        return self._c.network.project_authority_boundary_delta(
            head_locator=head_locator,
            authority_end=authority_end,
            safety_margin_cm=safety_margin_cm,
        )

    def _sum_matching_back_walk(
        self,
        resource_sequence: List[Any],
        head_resource_index: int,
        head_tile_index: int,
        match_id: Optional[str],
        key_fn,
    ) -> Tuple[float, int]:
        """Sum tile lengths walking backward from the head while a tile's id
        (via `key_fn`) stays equal to `match_id`; stop at the first mismatch.

        Returns `(length_cm, tile_count)`. Used for both the head block's tail
        (`key_fn = block_id`) and the head station's tail (`key_fn =
        station_id`): the tiles before the head that share its block / platform,
        so the chart frame can sit behind the train marker instead of pinned to
        it. Walks the head resource's pre-head tiles first, then earlier
        resources, stopping as soon as a tile's id differs.
        """
        length_cm = 0.0
        tile_count = 0
        if not match_id:
            return length_cm, tile_count
        stop = False
        for prev_tile in reversed(
            resource_sequence[head_resource_index].path_tiles[:head_tile_index]
        ):
            prev_track = self._c.network.get_track(prev_tile.x, prev_tile.y)
            if (key_fn(prev_track) if prev_track else None) != match_id:
                stop = True
                break
            length_cm += self._c.network._get_tile_length_cm(
                prev_tile.x, prev_tile.y
            )
            tile_count += 1
        if not stop:
            for r_idx in range(head_resource_index - 1, -1, -1):
                earlier = resource_sequence[r_idx]
                for prev_tile in reversed(earlier.path_tiles):
                    prev_track = self._c.network.get_track(prev_tile.x, prev_tile.y)
                    if (key_fn(prev_track) if prev_track else None) != match_id:
                        stop = True
                        break
                    length_cm += self._c.network._get_tile_length_cm(
                        prev_tile.x, prev_tile.y
                    )
                    tile_count += 1
                if stop:
                    break
        return length_cm, tile_count

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
        # Use the block->tracks reverse index (get_block_tracks) instead of an
        # O(all tracks) scan; this runs per-block in the per-tick lookahead.
        for track in self._c.network.get_block_tracks(block_id):
            if track.station_id:
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
        block = c.network.blocks.get(track.block_id)
        if not block:
            return float("inf")
        return block.speed_limit_kmh or float("inf")
