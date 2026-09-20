"""Track lifecycle service.

The HTTP layer in ``routes/tracks.py`` parses JSON and shapes responses;
this layer owns the track-tile mutations:

  * single-tile read / update (bits, dead-ends, switch state, station,
    block membership) with the orphan-impact pre-check and the auto-
    switch-creation that a bit change implies, and
  * bulk add / delete with the same impact aggregation.

Lookup raises ``NotFoundError`` and bad input raises ``ValidationError``.
The richer outcomes a single update can produce -- an orphan-impact that
needs operator confirmation, a switch locked by a live path, an empty
tile that gets removed -- are returned as a small tagged result dict so
the route can map each to its exact HTTP response without this layer
knowing any status codes:

    {"status": "bits_rejected", "message": str}     # invalid bit shape
    {"status": "needs_confirmation", "impact": ...}  # orphans; retry w/ force
    {"status": "switch_locked", "holder_path", "holder_state"}  # pure editor
    {"status": "switch_change_rejected", "reason": str}  # runtime refused throw
    {"status": "removed"}                            # tile became empty
    {"status": "updated", "track": <Tile>}
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app_state import AppState
from services.exceptions import NotFoundError, ValidationError
from services.segment_service import require_tiles_removable
from switch import Switch
from track import FORBIDDEN_TRACK_BITS, Tile, is_square_corner_switch

_SQUARE_CORNER_MESSAGE = (
    "Invalid switch placement: a switch branch is 90° to a connecting "
    "segment (square corner). Add a diagonal (NS/EW) leg so the branch "
    "eases at 45°."
)
_VALID_PORTS = {"N", "E", "S", "W"}


def _ensure_switch_for_tile(state: AppState, track, x: int, y: int) -> None:
    """Create + link a default Switch for a tile that just became a switch."""
    if track.is_switch and not track.switch_id:
        switch_id = f"SW_{x}_{y}"
        if not state.network.get_switch_at(x, y):
            state.network.add_switch(
                Switch(
                    switch_id=switch_id,
                    x=x,
                    y=y,
                    main_length_cm=0.0,
                    diverge_length_cm=0.0,
                )
            )
            track.switch_id = switch_id


def get_track(state: AppState, x: int, y: int):
    """Return the track at a tile, or None."""
    return state.network.get_track(x, y)


def require_track(state: AppState, x: int, y: int):
    """Return the track at a tile or raise NotFoundError."""
    track = state.network.get_track(x, y)
    if track is None:
        raise NotFoundError("Track not found")
    return track


def update_track(
    state: AppState, x: int, y: int, *, data: Any, force: bool = False
) -> Dict[str, Any]:
    """Apply an update to a single track tile.

    Returns a tagged result dict (see module docstring) for the richer
    outcomes; raises NotFoundError when the tile has no track and
    ValidationError for malformed input.
    """
    track = state.network.get_track(x, y)
    if not track:
        raise NotFoundError("Track not found")

    if not isinstance(data, dict):
        raise ValidationError("Invalid JSON body")

    # Impact pre-check for bit/dead-end changes. If the proposed change would
    # orphan a switch/signal/platform, surface the impact so the caller can
    # confirm and retry with force=True; the cleanup then happens below.
    if "track_bits" in data or "dead_end_ports" in data:
        new_bits = (
            int(data["track_bits"]) if "track_bits" in data else track.track_bits
        )
        if "dead_end_ports" in data:
            try:
                raw = set(data["dead_end_ports"])
                new_dead_ends = {p for p in raw if p in _VALID_PORTS}
            except Exception:
                raise ValidationError("dead_end_ports must be a list of N/E/S/W")
        else:
            new_dead_ends = track.dead_end_ports

        # A tile that becomes a switch, or is erased, leaves its segment. If it
        # is the segment's last tile the segment goes with it, so the edit is
        # bound by the rule for deleting a segment (design_new.md, "Segment
        # deletion keeps its block connected"). Judged here, before anything
        # below changes, so a rejection changes nothing.
        becomes_switch = Tile(x=x, y=y, track_bits=new_bits).is_switch
        is_erased = new_bits == 0 and not new_dead_ends
        if track.segment_id and (becomes_switch or is_erased):
            require_tiles_removable(state, [(x, y)])

        impact = state.network.compute_bit_change_impact(
            x, y, new_bits, new_dead_ends
        )
        has_impact = (
            impact["removes_switch"] is not None
            or bool(impact["removes_signals"])
            or impact["removes_platform"] is not None
        )
        if has_impact and not force:
            return {"status": "needs_confirmation", "impact": impact}

        # Confirmed (or no orphans). Clean up entities that would otherwise be
        # left dangling. When the tile is being fully removed we leave cleanup
        # to remove_track at the end.
        if not impact["tile_will_be_removed"]:
            for sid in impact["removes_signals"]:
                state.network.remove_signal(sid)
            if impact["removes_switch"]:
                state.network.remove_switch(impact["removes_switch"])

    if "track_bits" in data:
        new_bits = int(data["track_bits"])
        if is_square_corner_switch(new_bits):
            return {"status": "bits_rejected", "message": _SQUARE_CORNER_MESSAGE}
        if not track.set_track_bits(new_bits):
            return {
                "status": "bits_rejected",
                "message": f"Invalid track_bits: {new_bits}",
            }

        if track.is_switch and track.segment_id:
            state.network.remove_tile_from_segment(x, y)
        _ensure_switch_for_tile(state, track, x, y)

    if "dead_end_ports" in data:
        try:
            ports = set(data["dead_end_ports"])
            track.dead_end_ports = {p for p in ports if p in _VALID_PORTS}
        except Exception:
            raise ValidationError("dead_end_ports must be a list of N/E/S/W")

    if "switch_state" in data:
        switch_entity = (
            state.network.switches.get(track.switch_id)
            if track.switch_id is not None
            else None
        )
        switch_manager = getattr(state.network, "switch_manager", None)
        if switch_entity is not None and switch_manager is not None:
            # Runtime is wired: route the alignment change through the canonical
            # SwitchManager workflow (validate locks/occupancy/protection, write
            # Switch.position, notify BlockManager, drive the power throw +
            # continuity check) per design_new.md "Canonical `Switch.position` mutation workflow" — nothing writes
            # Switch.position directly while a runtime surface exists.
            success, message = switch_manager.request_position_change(
                track.switch_id, int(data["switch_state"])
            )
            if not success:
                return {"status": "switch_change_rejected", "reason": message}
        elif switch_entity is not None and switch_entity.is_locked():
            # Pure editor (no runtime surface): keep the lock guard before the
            # direct geometric write, which design_new.md "Canonical `Switch.position` mutation workflow" exempts.
            lock_items = list(switch_entity.locks.items())
            holder_path = lock_items[0][0][0] if lock_items else None
            holder_state = lock_items[0][1][1] if lock_items else None
            return {
                "status": "switch_locked",
                "holder_path": holder_path,
                "holder_state": holder_state,
            }
        else:
            track.switch_state = int(data["switch_state"])

    if "station_name" in data:
        track.station_name = data["station_name"]

    if "segment_id" in data:
        track.segment_id = data["segment_id"]

    state.network.propagate_from_tile(x, y)

    if "switch_state" in data or "segment_id" in data:
        state.network.update_signals_from_blocks()

    if track.track_bits == 0 and len(track.dead_end_ports) == 0:
        state.network.remove_track(x, y)
        # Connectivity changed: refresh stored switch flank protection so it
        # cannot go stale and corrupt acquisition (design_new.md, "Flank protection computation").
        state.network.recompute_all_flank_protection()
        return {"status": "removed"}

    # Connectivity changed: refresh stored switch flank protection
    # (design_new.md, "Flank protection computation").
    state.network.recompute_all_flank_protection()
    return {"status": "updated", "track": track}


def delete_track(state: AppState, x: int, y: int) -> bool:
    """Remove a track tile. Returns True.

    Raises:
        NotFoundError when the tile has no track.
        ValidationError when erasing the tile would empty a segment in the
        middle of its block (design_new.md, "Segment deletion keeps its block
        connected"). Nothing is changed.
    """
    if not state.network.get_track(x, y):
        raise NotFoundError("Track not found")
    require_tiles_removable(state, [(x, y)])
    state.network.remove_track(x, y)
    # Connectivity changed: refresh stored switch flank protection
    # (design_new.md, "Flank protection computation").
    state.network.recompute_all_flank_protection()
    return True


def add_tracks_bulk(
    state: AppState, *, tracks_data: List[Dict[str, Any]]
) -> Dict[str, List]:
    """Add or merge many track tiles at once.

    Each entry merges its bits into an existing tile or creates a new one;
    forbidden / square-corner results are skipped. Returns the affected
    track objects as ``{"added": [...], "updated": [...]}`` for the caller
    to serialize.
    """
    added: List[Tile] = []
    updated: List[Tile] = []

    # A tile that merges into a switch leaves its segment (a switch is never a
    # segment member). If it is the segment's last tile the segment goes with
    # it, so the tiles are bound by the rule for deleting a segment
    # (design_new.md, "Segment deletion keeps its block connected"). They are
    # judged together, before anything changes, so a rejection changes nothing.
    becoming_switches: List[Tuple[int, int]] = []
    for track_data in tracks_data:
        tx = track_data.get("x")
        ty = track_data.get("y")
        if tx is None or ty is None:
            continue
        existing = state.network.get_track(tx, ty)
        if existing is None or not existing.segment_id:
            continue
        merged = existing.track_bits | track_data.get("track_bits", 0)
        if merged in FORBIDDEN_TRACK_BITS or is_square_corner_switch(merged):
            continue
        if Tile(x=tx, y=ty, track_bits=merged).is_switch:
            becoming_switches.append((tx, ty))
    require_tiles_removable(state, becoming_switches)
    becoming_switches_set = frozenset(becoming_switches)

    for track_data in tracks_data:
        tx = track_data.get("x")
        ty = track_data.get("y")
        if tx is None or ty is None:
            continue

        new_bits = track_data.get("track_bits", 0)
        existing_track = state.network.get_track(tx, ty)

        if existing_track:
            merged_bits = existing_track.track_bits | new_bits
            if (
                merged_bits in FORBIDDEN_TRACK_BITS
                or is_square_corner_switch(merged_bits)
            ):
                continue
            existing_track.set_track_bits(merged_bits)

            if existing_track.is_switch and existing_track.segment_id:
                state.network.remove_tile_from_segment(
                    tx, ty, also_removing=becoming_switches_set
                )
            _ensure_switch_for_tile(state, existing_track, tx, ty)

            state.network.propagate_from_tile(tx, ty)
            updated.append(existing_track)
        else:
            if new_bits in FORBIDDEN_TRACK_BITS or is_square_corner_switch(new_bits):
                continue
            track = Tile(x=tx, y=ty, track_bits=new_bits)
            if track_data.get("station_name"):
                track.station_name = track_data["station_name"]
            state.network.add_track(track)
            _ensure_switch_for_tile(state, track, tx, ty)
            added.append(track)

    # Connectivity changed: refresh stored switch flank protection once for the
    # whole batch (design_new.md, "Flank protection computation").
    if added or updated:
        state.network.recompute_all_flank_protection()
    return {"added": added, "updated": updated}


def delete_tracks_bulk(
    state: AppState, *, positions: List[Any], force: bool = False
) -> Dict[str, Any]:
    """Delete many track tiles, aggregating the orphan impact first.

    Returns ``{"status": "needs_confirmation", "impact": {...}}`` when the
    deletion would orphan switches/signals/platforms and ``force`` is
    False, otherwise ``{"status": "done", "deleted": [[x, y], ...]}``.

    Raises:
        ValidationError when the erase would empty a segment in the middle of
        its block (design_new.md, "Segment deletion keeps its block
        connected"). The tiles are judged together, before anything is asked
        or erased, so a rejection changes nothing; erasing all of a block's
        track together is allowed.
    """
    doomed: List[Tuple[int, int]] = []
    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        if state.network.get_track(pos[0], pos[1]):
            doomed.append((pos[0], pos[1]))
    require_tiles_removable(state, doomed)
    doomed_set = frozenset(doomed)

    switches_set: List[str] = []
    signals_set: List[str] = []
    platforms_set: List[str] = []
    seen_switches: set = set()
    seen_signals: set = set()
    seen_platforms: set = set()

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        px, py = pos[0], pos[1]
        if not state.network.get_track(px, py):
            continue
        impact = state.network.compute_bit_change_impact(px, py, 0, set())
        if impact["removes_switch"] and impact["removes_switch"] not in seen_switches:
            seen_switches.add(impact["removes_switch"])
            switches_set.append(impact["removes_switch"])
        for sid in impact["removes_signals"]:
            if sid not in seen_signals:
                seen_signals.add(sid)
                signals_set.append(sid)
        if (
            impact["removes_platform"]
            and impact["removes_platform"] not in seen_platforms
        ):
            seen_platforms.add(impact["removes_platform"])
            platforms_set.append(impact["removes_platform"])

    if (switches_set or signals_set or platforms_set) and not force:
        return {
            "status": "needs_confirmation",
            "impact": {
                "removes_switches": switches_set,
                "removes_signals": signals_set,
                "removes_platforms": platforms_set,
            },
        }

    deleted: List[List[int]] = []
    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        px, py = pos[0], pos[1]
        if state.network.get_track(px, py):
            state.network.remove_track(px, py, also_removing=doomed_set)
            deleted.append([px, py])

    # Connectivity changed: refresh stored switch flank protection once for the
    # whole batch (design_new.md, "Flank protection computation").
    if deleted:
        state.network.recompute_all_flank_protection()
    return {"status": "done", "deleted": deleted}
