"""Segment geometry service — the geometry axis of the track model.

`routes/segments.py` is a thin HTTP layer over this module. A `Segment` owns
geometry only (`ordered_tiles`, `length_cm`, `min_radius_cm`, `grade_percent`,
`speed_limit_kmh`); occupancy / reservation / power live on its owning `Block`
and are edited through `services.block_service`. This module owns the geometry
mutations:

  * Construction — create-with-tracks, extend.
  * Geometry config — length / radius / grade / speed-limit PATCH, addressed to
    one segment (so a segment inside a multi-segment block is edited without
    ambiguity).
  * Tile membership — add / remove tiles, with the edge-tile and continuity
    validation that protects segment geometry.
  * ``can_extend`` — the pure "may this tile join the segment?" predicate.
  * Whole-segment delete — with the downward platform cascade
    (design_new.md, "Non-empty parent rule"): a segment delete that empties its owning block drops
    that block from any owning `Platform.block_assignments`, deleting a platform
    left with no blocks.

Lookup raises ``NotFoundError``; bad input raises ``ValidationError``.
``update_segment`` takes ``state.simulation_lock`` so the per-tick phase loop
cannot observe a torn read; the other mutations run at edit time.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import uuid

from app_state import AppState
from segment_validation import crossing_disallowed_in_segment
from services.exceptions import NotFoundError, ValidationError
from services.validation import coerce_nonneg
from track import BIT_TO_EDGES


# --------------------------------------------------------------- lookups ----


def get_segment(state: AppState, segment_id: str):
    """Return the segment, or None when not found."""
    return state.network.get_segment(segment_id)


def require_segment(state: AppState, segment_id: str):
    """Return the segment or raise NotFoundError."""
    segment = state.network.get_segment(segment_id)
    if segment is None:
        raise NotFoundError("Segment not found")
    return segment


def list_segments(state: AppState) -> List[Any]:
    """Return every segment on the network."""
    return list(state.network.segments.values())


# --------------------------------------------------------------- create -----


def create_segment_with_tracks(
    state: AppState,
    *,
    segment_id: Optional[str] = None,
    positions: List[Any],
    name: str = "",
    length_cm: float = 0.0,
    speed_limit_kmh: Optional[Any] = None,
):
    """Create a segment holding the given tile positions.

    The tiles land in one fresh segment, and that segment gets its own block
    because nothing is grouped with it — so block and segment share `segment_id`
    (the default one-feed-per-segment layout). Returns the owning `Block`.

    Raises:
        ValidationError when the network rejects the positions/geometry.
    """
    resolved_id = segment_id or f"SEG_{uuid.uuid4().hex[:8].upper()}"
    try:
        tuple_positions = [tuple(pos) for pos in positions]
        return state.network.create_segment_with_tracks(
            segment_id=resolved_id,
            positions=tuple_positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


# ---------------------------------------------------------- geometry PATCH ---


def update_segment(state: AppState, segment_id: str, *, data: Dict[str, Any]):
    """Update a segment's geometry fields.

    Geometry lives on the `Segment`, so the write addresses the segment
    directly — a segment inside a multi-segment block is edited without the
    block-level ambiguity that `Block._geometry_write_target` guards against.
    Handles ``length_cm`` / ``min_radius_cm`` / ``grade_percent`` /
    ``speed_limit_kmh`` (and ``name``). All non-None numeric values must be
    numbers >= 0; ``length_cm`` may not be sent as null.

    Mutations happen under ``state.simulation_lock`` so the per-tick phase loop
    cannot observe a torn read.

    Raises:
        NotFoundError when no segment has that id.
        ValidationError when a numeric field is non-numeric or negative, or
            ``length_cm`` is sent as null.
    """
    data = dict(data or {})
    with state.simulation_lock:
        segment = state.network.get_segment(segment_id)
        if segment is None:
            raise NotFoundError("Segment not found")

        if "name" in data:
            segment.name = data["name"] if data["name"] is not None else ""
        try:
            if "length_cm" in data:
                if data["length_cm"] is None:
                    raise ValidationError("length_cm is required and cannot be empty")
                segment.length_cm = coerce_nonneg(data["length_cm"], "length_cm")
            if "min_radius_cm" in data:
                raw = data["min_radius_cm"]
                segment.min_radius_cm = (
                    None if raw is None else coerce_nonneg(raw, "min_radius_cm")
                )
            if "grade_percent" in data:
                raw = data["grade_percent"]
                if raw is None:
                    segment.grade_percent = None
                else:
                    try:
                        segment.grade_percent = float(raw)
                    except (TypeError, ValueError):
                        raise ValidationError("grade_percent must be a number")
            if "speed_limit_kmh" in data:
                raw = data["speed_limit_kmh"]
                segment.speed_limit_kmh = (
                    None if raw is None else coerce_nonneg(raw, "speed_limit_kmh")
                )
            elif "max_speed_kmh" in data:
                raw = data["max_speed_kmh"]
                segment.speed_limit_kmh = (
                    None if raw is None else coerce_nonneg(raw, "max_speed_kmh")
                )
        except ValidationError:
            raise
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        return segment


# ---------------------------------------------------------------- extend ----


def extend_segment(
    state: AppState,
    segment_id: str,
    *,
    positions: List[Any],
    force: bool = False,
    force_overwrite: bool = False,
):
    """Extend a segment with new tile positions.

    Returns the owning block; ``block._invalidated_signals`` (set by the network
    when signals were cleared) is left in place for the caller to surface.

    Raises:
        NotFoundError when no segment has that id.
        ValidationError when the network rejects the extension.
    """
    require_segment(state, segment_id)
    try:
        tuple_positions = [tuple(pos) for pos in positions]
        return state.network.extend_segment(
            segment_id,
            tuple_positions,
            force=force,
            force_overwrite=force_overwrite,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


# ------------------------------------------------------ tile membership -----


def add_tracks_to_segment(
    state: AppState, segment_id: str, *, positions: List[Any], force: bool = False
) -> Dict[str, Any]:
    """Assign tiles to a segment, optionally reassigning from other segments.

    Returns a result dict (``added`` / ``reassigned`` counts + details).

    Raises:
        NotFoundError when no segment has that id.
    """
    require_segment(state, segment_id)

    added: List[List[int]] = []
    reassigned: List[Dict[str, Any]] = []

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]

        # Track already assigned to a different segment?
        track = state.network.get_track(x, y)
        if track and track.segment_id and track.segment_id != segment_id:
            if force:
                old_segment_id = track.segment_id
                track.segment_id = None
                reassigned.append(
                    {"position": [x, y], "from": old_segment_id, "to": segment_id}
                )
            else:
                continue

        try:
            state.network.assign_track_to_segment(x, y, segment_id)
            added.append([x, y])
        except ValueError:
            pass

    return {
        "status": "success",
        "added": len(added),
        "reassigned": len(reassigned),
        "positions": added,
        "reassignment_details": reassigned,
        "segment_id": segment_id,
    }


def remove_tracks_from_segment(
    state: AppState, segment_id: str, *, positions: List[Any]
) -> Dict[str, Any]:
    """Remove tiles from a segment, enforcing edge-tile + continuity rules.

    A tile may only be removed if it is an edge tile and its removal keeps the
    remaining segment continuous; emptying the segment deletes it (and, when
    that was its owning block's last member, the block too — with the downward
    platform cascade). Returns a result dict (``removed`` / ``errors`` /
    ``segment_deleted``).

    Raises:
        NotFoundError when no segment has that id.
        ValidationError when emptying the segment would split its block, in
        which case no tile is removed.
    """
    require_segment(state, segment_id)

    removed: List[List[int]] = []
    errors: List[str] = []

    current_positions = state.network.get_segment_positions(segment_id)
    position_set = set((x, y) for x, y in current_positions)

    # Owning block captured before any delete so the platform cascade can tell
    # whether the block disappeared when the segment emptied.
    owning_block_id = state.network.segment_to_block_id(segment_id)

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]

        track = state.network.get_track(x, y)
        if not track or track.segment_id != segment_id:
            errors.append(f"Position ({x}, {y}) is not part of segment {segment_id}")
            continue

        # Edge tiles have at least one exit that has no neighbor track, or a
        # neighbor track outside the segment.
        exits = []
        for bit in range(6):
            if track.track_bits & (1 << bit):
                e1, e2 = BIT_TO_EDGES[bit]
                exits.extend([e1, e2])
        exits = list(set(exits))

        is_edge = False
        for exit_edge in exits:
            neighbor_pos = state.network._get_neighbor_coords(x, y, exit_edge)
            if not neighbor_pos:
                is_edge = True
                break
            nx, ny = neighbor_pos
            neighbor_track = state.network.get_track(nx, ny)
            if not neighbor_track or (nx, ny) not in position_set:
                is_edge = True
                break

        if not is_edge:
            errors.append(f"Cannot remove tile ({x}, {y}): not an edge tile")
            continue

        # Temporarily remove to check continuity.
        original_segment_id = track.segment_id
        track.segment_id = None

        remaining_tracks = state.network.get_segment_tracks(segment_id)
        if len(remaining_tracks) > 1:
            is_continuous, _validation_errors = (
                state.network.validate_segment_continuity(segment_id)
            )
            if not is_continuous:
                track.segment_id = original_segment_id
                errors.append(
                    f"Cannot remove tile ({x}, {y}): would break segment continuity"
                )
                continue

        removed.append([x, y])
        position_set.discard((x, y))

    # Auto-delete the segment when it has no tiles left. This deletes the
    # segment record and, when it was the owning block's last member, that
    # block too; the platform cascade below then tears down a platform the
    # block's disappearance left with no assigned blocks.
    segment_deleted = False
    if len(state.network.get_segment_tracks(segment_id)) == 0:
        try:
            delete_segment(state, segment_id)
        except ValidationError:
            # The emptied segment sits in the middle of its block, so the
            # delete is rejected and the edit with it (design_new.md, "Atomic
            # commit"): give the detached tiles back to the segment. Detaching
            # them was the only thing this call changed.
            for x, y in removed:
                state.network.get_track(x, y).segment_id = segment_id
            raise
        segment_deleted = True

    return {
        "status": "success" if removed else "partial" if errors else "no_changes",
        "removed": len(removed),
        "positions": removed,
        "errors": errors,
        "segment_deleted": segment_deleted,
        # Kept for the multi-segment case: the owning block when the edit began.
        "block_id": owning_block_id,
    }


def can_extend_segment(
    state: AppState, segment_id: str, *, x: int, y: int
) -> Dict[str, Any]:
    """Validate (without mutating) whether a tile may join the segment.

    Returns a result dict ``{can_extend: bool, ...}`` describing the verdict
    and, when blocked, the reason and any required force flag.

    Raises:
        NotFoundError when no segment has that id.
    """
    require_segment(state, segment_id)

    track = state.network.get_track(x, y)
    if not track:
        return {"can_extend": False, "reason": "No track at position"}

    if crossing_disallowed_in_segment(track.track_bits):
        return {
            "can_extend": False,
            "reason": "Cannot add crossing to segment - crossings isolate paths",
        }

    if track.is_switch:
        return {
            "can_extend": False,
            "reason": "Cannot add switch to segment - switches are block boundaries",
        }

    if track.signal_ids:
        return {
            "can_extend": False,
            "reason": "Tile has signal(s) - use force=true to extend past signal",
            "requires_force": True,
        }

    if track.segment_id and track.segment_id != segment_id:
        return {
            "can_extend": False,
            "reason": (
                f"Tile already in segment {track.segment_id} - "
                "use force_overwrite=true"
            ),
            "requires_force_overwrite": True,
        }

    existing_positions = state.network.get_segment_positions(segment_id)
    is_adjacent = any(
        abs(x - old_x) + abs(y - old_y) == 1 for old_x, old_y in existing_positions
    )
    if not is_adjacent and len(existing_positions) > 0:
        return {
            "can_extend": False,
            "reason": "Position must be adjacent to existing segment",
        }

    return {"can_extend": True}


# --------------------------------------------------------------- reverse ----


def reverse_segment(state: AppState, segment_id: str) -> bool:
    """Flip a segment's stored tile order end-for-end. Returns True when flipped.

    `Segment.ordered_tiles` is built per segment from a deterministic coordinate
    rule, so two segments sitting next to each other on the layout can be stored
    running towards each other. Grouping them into one block needs every member
    to run the same way, and grouping itself never rewrites geometry — this is
    the segment-axis write that aligns them.

    Returns False when the segment has fewer than two tiles, where direction is
    not meaningful.

    Raises:
        NotFoundError when no segment has that id.
    """
    if state.network.get_segment(segment_id) is None:
        raise NotFoundError(f"Segment {segment_id} not found")
    with state.simulation_lock:
        geometry = state.network._get_segment_geometry_manager()
        return geometry.reverse_segment_orientation(segment_id)


# ---------------------------------------------------------------- delete ----


def require_tiles_removable(state: AppState, positions: List[Any]) -> None:
    """Refuse an edit that would leave a block split in two.

    Any edit that takes a segment's last tile out of it deletes that segment
    in the same operation (design_new.md, "Non-empty parent rule", "Auto-delete
    on last-child removal"), so it is bound by the rule for deleting one: a
    segment in the middle of a multi-segment block stays (design_new.md,
    "Segment deletion keeps its block connected"), and the rejection rejects
    the whole edit ("Atomic commit"). Callers that change anything else before
    they reach the tile removal (erasing track, turning a tile into a switch,
    building a platform) call this first, so a rejection changes nothing.

    Raises:
        ValidationError when the edit would empty a segment in the middle of
        its block. Reads only.
    """
    try:
        state.network.check_tiles_removable(positions)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def delete_segment(state: AppState, segment_id: str) -> bool:
    """Delete a segment's geometry and clear its tile references. Returns True.

    Removing a segment fans out per the Non-empty parent rule
    (design_new.md, "Non-empty parent rule"): the segment leaves its owning `Block`, and if that was
    the block's last member the block is deleted too. When the block disappears,
    the downward platform cascade (design_new.md, "Non-empty parent rule") drops it from any owning
    `Platform.block_assignments` and deletes a platform left with no blocks.

    A segment in the middle of a multi-segment block is not deleted, because
    the block would be left as two pieces (design_new.md, "Segment deletion
    keeps its block connected").

    Raises:
        NotFoundError when no segment has that id.
        ValidationError when the segment is in the middle of its block. Nothing
        is changed.
    """
    owning_block_id = state.network.segment_to_block_id(segment_id)
    if owning_block_id is None:
        raise NotFoundError("Segment not found")

    try:
        state.network.delete_segment(segment_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    # Only cascade when the owning block itself disappeared (segment was its
    # last member). A block that still groups other members keeps its identity
    # and its platform assignments.
    if owning_block_id not in state.network.blocks:
        _platform_cascade_for_deleted_block(state, owning_block_id)
    return True


def _platform_cascade_for_deleted_block(state: AppState, block_id: str) -> None:
    """Downward platform cascade for a block that no longer exists.

    Drops `block_id` from any owning `Platform.block_assignments`; if that
    leaves a platform with no assigned blocks, deletes the platform too (which
    cascades to an emptied logical `Station` group). The logic is the network's
    `prune_platforms_for_deleted_block`, the one canonical home for the segment
    fan-out's platform half, shared with the manager-level `delete_segment` so
    every path that deletes a block prunes its platforms the same way.
    """
    state.network.prune_platforms_for_deleted_block(block_id)
