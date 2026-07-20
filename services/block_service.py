"""Block lifecycle and track-membership service.

The HTTP layer in ``routes/blocks.py`` parses JSON and shapes the
read-only serialization responses (`to_dict`, validation status,
monitoring signals, sorted positions); this layer owns the block
mutations:

  * CRUD -- create, update (config + availability flags), delete.
  * Bulk construction -- create-with-tracks, extend.
  * Track membership -- add / remove tracks, with the edge-tile and
    continuity validation that protects block geometry.
  * ``can_extend`` -- the pure "may this tile join the block?" predicate.

Lookup raises ``NotFoundError``; bad input raises ``ValidationError``;
a disabled-toggle blocked by occupancy/reservation/CAUTION raises
``ConflictError``; the removed direct ``state`` mutation raises
``GoneError`` (HTTP 410). ``update_block`` takes ``state.simulation_lock``
(the PATCH route it replaces did); the other mutations do not.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import uuid

from app_state import AppState
from block_validation import crossing_disallowed_in_block
from services.exceptions import (
    ConflictError,
    GoneError,
    NotFoundError,
    ValidationError,
)
from services.validation import coerce_nonneg
from track import BIT_TO_EDGES


# --------------------------------------------------------------- lookups ----


def get_block(state: AppState, block_id: str):
    """Return the block, or None when not found."""
    return state.network.get_block(block_id)


def require_block(state: AppState, block_id: str):
    """Return the block or raise NotFoundError."""
    block = state.network.get_block(block_id)
    if block is None:
        raise NotFoundError("Block not found")
    return block


def list_blocks(state: AppState) -> List[Any]:
    """Return every block on the network."""
    return list(state.network.blocks.values())


# ------------------------------------------------------------------ CRUD ----


def create_block(
    state: AppState,
    *,
    block_id: Optional[str] = None,
    name: str = "",
    length_cm: Optional[Any] = None,
    min_radius_cm: Optional[Any] = None,
    grade_percent: Optional[Any] = None,
    speed_limit_kmh: Optional[Any] = None,
    max_speed_kmh: Optional[Any] = None,
    is_holding_point: Optional[Any] = None,
):
    """Create a block and set its optional config fields.

    ``speed_limit_kmh`` wins over ``max_speed_kmh`` when both are given.

    Raises:
        ValidationError when the network rejects the id/name or a numeric
            field does not parse.
    """
    resolved_id = block_id or f"BLK_{uuid.uuid4().hex[:8].upper()}"
    try:
        block = state.network.create_block(resolved_id, name)
        if length_cm is not None:
            block.length_cm = float(length_cm)
        if min_radius_cm is not None:
            block.min_radius_cm = min_radius_cm
        if grade_percent is not None:
            block.grade_percent = grade_percent
        if speed_limit_kmh is not None:
            block.speed_limit_kmh = speed_limit_kmh
        elif max_speed_kmh is not None:
            block.speed_limit_kmh = max_speed_kmh
        if is_holding_point is not None:
            block.is_holding_point = bool(is_holding_point)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return block


def update_block(state: AppState, block_id: str, *, data: Dict[str, Any]):
    """Update a block's config and/or availability flags.

    Mutations happen under ``state.simulation_lock`` so the per-tick phase
    loop cannot observe a torn read.

    Raises:
        NotFoundError when no block has that id.
        GoneError when the request carries the removed ``state`` field.
        ConflictError when enabling ``disabled`` while the block is
            occupied / reserved / in CAUTION.
        ValidationError when ``allowed_travel_directions`` is malformed, or a
            geometry field (``length_cm`` / ``min_radius_cm`` /
            ``speed_limit_kmh``) is non-numeric or negative, or ``length_cm``
            is sent as null.
    """
    data = dict(data or {})
    with state.simulation_lock:
        block = state.network.get_block(block_id)
        if not block:
            raise NotFoundError("Block not found")

        if "name" in data:
            block.name = data["name"] if data["name"] is not None else ""
        # Geometry fields. length_cm is required (a block must have a length);
        # min_radius_cm and speed_limit_kmh accept None = "no limit". All
        # non-None values must be numbers >= 0. The UI enters length / radius
        # as whole millimetres and converts to cm before sending.
        # A block that groups several segments has no single block-level value
        # for any of these, so `Block` rejects the write. Report that as a
        # validation error rather than letting it surface as a server fault.
        try:
            if "length_cm" in data:
                if data["length_cm"] is None:
                    raise ValidationError("length_cm is required and cannot be empty")
                block.length_cm = coerce_nonneg(data["length_cm"], "length_cm")
            if "min_radius_cm" in data:
                raw = data["min_radius_cm"]
                block.min_radius_cm = (
                    None if raw is None else coerce_nonneg(raw, "min_radius_cm")
                )
            if "grade_percent" in data:
                raw = data["grade_percent"]
                if raw is None:
                    block.grade_percent = None
                else:
                    try:
                        block.grade_percent = float(raw)
                    except (TypeError, ValueError):
                        raise ValidationError("grade_percent must be a number")
            if "speed_limit_kmh" in data:
                raw = data["speed_limit_kmh"]
                block.speed_limit_kmh = (
                    None if raw is None else coerce_nonneg(raw, "speed_limit_kmh")
                )
            elif "max_speed_kmh" in data:
                raw = data["max_speed_kmh"]
                block.speed_limit_kmh = (
                    None if raw is None else coerce_nonneg(raw, "max_speed_kmh")
                )
        except ValidationError:
            raise
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if "state" in data:
            # Block state is owned by BlockManager / ResourceCoordinator in
            # v1 and computed from `occupied_by` / `_is_caution` /
            # `reservation`; there is no admin-side direct-mutation surface.
            raise GoneError(
                "Direct block state mutation is not supported in v1. "
                "Block state is derived from occupied_by / _is_caution / "
                "reservation, written exclusively by BlockManager and "
                "ResourceCoordinator."
            )

        # Availability flags per design_new.md "Availability flag REST API
        # surface". Setting `disabled = True` runs the Block admin-reject
        # preconditions (occupancy / reservation / CAUTION); setting
        # `manual` always succeeds.
        if "disabled" in data:
            new_disabled = bool(data["disabled"])
            if new_disabled and not block.disabled:
                if block.occupied_by is not None:
                    raise ConflictError(f"Block is occupied by {block.occupied_by}")
                if block.reservation is not None:
                    raise ConflictError(
                        f"Block is reserved by {block.reserved_by_path}"
                    )
                if block.caution is not None:
                    raise ConflictError("Block is in CAUTION")
            block.disabled = new_disabled
        if "manual" in data:
            block.manual = bool(data["manual"])
        # Holding-point config flag (design_new.md "Block" line 848). No
        # precondition — it does not affect availability, reservation, or
        # runtime state; it only biases best-effort truncation.
        if "is_holding_point" in data:
            block.is_holding_point = bool(data["is_holding_point"])

        # Static block-level travel policy per design_new.md
        # `Block.allowed_travel_directions`. Must be a non-empty subset of
        # {"forward", "reverse"}; editing it has no occupancy precondition.
        if "allowed_travel_directions" in data:
            raw = data["allowed_travel_directions"]
            if not isinstance(raw, (list, tuple, set)):
                raise ValidationError(
                    "allowed_travel_directions must be a list of "
                    "'forward' / 'reverse'"
                )
            requested = {str(d) for d in raw}
            unknown = requested - {"forward", "reverse"}
            if unknown:
                raise ValidationError(
                    f"Unknown travel direction(s): {sorted(unknown)}. "
                    "Allowed: forward, reverse"
                )
            if not requested:
                raise ValidationError(
                    "allowed_travel_directions must include at least one of "
                    "'forward' / 'reverse'"
                )
            block.allowed_travel_directions = requested

        return block


def delete_block(state: AppState, block_id: str) -> bool:
    """Delete a block and clear its track references. Returns True.

    Raises:
        NotFoundError when no block has that id.
    """
    network = state.network
    if not network.delete_block(block_id):
        raise NotFoundError("Block not found")

    # Downward auto-delete cascade (design_new.md:644): drop the deleted block
    # from any owning Platform's `block_assignments`; if that leaves a platform
    # with no assigned blocks, delete the platform too (which cascades to an
    # emptied logical Station group). Done at the service layer rather than
    # inside `network.delete_block` because Station teardown itself calls
    # `network.delete_block` for its member blocks, and re-entering platform
    # deletion there would double-delete the platform.
    emptied_platform_ids: List[str] = []
    for platform in list(network.platforms.values()):
        assignments = getattr(platform, "block_assignments", None)
        if not assignments:
            continue
        stale_positions = [pos for pos, bid in assignments.items() if bid == block_id]
        if not stale_positions:
            continue
        for pos in stale_positions:
            del assignments[pos]
        if not platform.get_all_assigned_blocks():
            emptied_platform_ids.append(platform.platform_id)
    for platform_id in emptied_platform_ids:
        network.delete_platform(platform_id)
    return True


def create_block_with_tracks(
    state: AppState,
    *,
    block_id: str,
    positions: List[Any],
    name: str = "",
    length_cm: float = 0.0,
    speed_limit_kmh: Optional[Any] = None,
):
    """Create a block and assign the given tile positions to it.

    Raises:
        ValidationError when the network rejects the positions/geometry.
    """
    try:
        tuple_positions = [tuple(pos) for pos in positions]
        return state.network.create_block_with_tracks(
            block_id=block_id,
            positions=tuple_positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def extend_block(
    state: AppState,
    block_id: str,
    *,
    positions: List[Any],
    force: bool = False,
    force_overwrite: bool = False,
):
    """Extend a block with new tile positions.

    Returns the block; ``block._invalidated_signals`` (set by the network
    when signals were cleared) is left in place for the caller to surface.

    Raises:
        ValidationError when the network rejects the extension.
    """
    try:
        tuple_positions = [tuple(pos) for pos in positions]
        return state.network.extend_block(
            block_id, tuple_positions, force=force, force_overwrite=force_overwrite
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def add_tracks_to_block(
    state: AppState, block_id: str, *, positions: List[Any], force: bool = False
) -> Dict[str, Any]:
    """Assign tiles to a block, optionally reassigning from other blocks.

    Returns a result dict (``added`` / ``reassigned`` counts + details).

    Raises:
        NotFoundError when no block has that id.
    """
    require_block(state, block_id)

    added: List[List[int]] = []
    reassigned: List[Dict[str, Any]] = []

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]

        # Track already assigned to a different block?
        track = state.network.get_track(x, y)
        if track and track.block_section_id and track.block_section_id != block_id:
            if force:
                old_block_id = track.block_section_id
                track.block_section_id = None
                reassigned.append(
                    {"position": [x, y], "from": old_block_id, "to": block_id}
                )
            else:
                continue

        try:
            state.network.assign_track_to_block(x, y, block_id)
            added.append([x, y])
        except ValueError:
            pass

    return {
        "status": "success",
        "added": len(added),
        "reassigned": len(reassigned),
        "positions": added,
        "reassignment_details": reassigned,
        "block_id": block_id,
    }


def remove_tracks_from_block(
    state: AppState, block_id: str, *, positions: List[Any]
) -> Dict[str, Any]:
    """Remove tiles from a block, enforcing edge-tile + continuity rules.

    A tile may only be removed if it is an edge tile and its removal keeps
    the remaining block continuous; emptying the block deletes it. Returns
    a result dict (``removed`` / ``errors`` / ``block_deleted``).

    Raises:
        NotFoundError when no block has that id.
    """
    require_block(state, block_id)

    removed: List[List[int]] = []
    errors: List[str] = []

    current_positions = state.network.get_block_positions(block_id)
    position_set = set((x, y) for x, y in current_positions)

    for pos in positions:
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        x, y = pos[0], pos[1]

        track = state.network.get_track(x, y)
        if not track or track.block_section_id != block_id:
            errors.append(f"Position ({x}, {y}) is not part of block {block_id}")
            continue

        # Edge tiles have at least one exit that has no neighbor track, or a
        # neighbor track outside the block.
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
        original_block_id = track.block_section_id
        track.block_section_id = None

        remaining_tracks = state.network.get_block_tracks(block_id)
        if len(remaining_tracks) > 1:
            is_continuous, _validation_errors = (
                state.network.validate_block_continuity(block_id)
            )
            if not is_continuous:
                track.block_section_id = original_block_id
                errors.append(
                    f"Cannot remove tile ({x}, {y}): would break block continuity"
                )
                continue

        removed.append([x, y])
        position_set.discard((x, y))

    # Auto-delete the block when it has no tracks left. Route through the
    # service-level `delete_block` so the downward platform cascade (drop the
    # block from any owning Platform.block_assignments; delete a platform left
    # empty) runs here too — a bare `network.delete_block` would leave a dangling
    # block_assignments entry and an orphaned empty platform.
    block_deleted = False
    if len(state.network.get_block_tracks(block_id)) == 0:
        delete_block(state, block_id)
        block_deleted = True

    return {
        "status": "success" if removed else "partial" if errors else "no_changes",
        "removed": len(removed),
        "positions": removed,
        "errors": errors,
        "block_deleted": block_deleted,
    }


def can_extend_block(
    state: AppState, block_id: str, *, x: int, y: int
) -> Dict[str, Any]:
    """Validate (without mutating) whether a tile may join the block.

    Returns a result dict ``{can_extend: bool, ...}`` describing the
    verdict and, when blocked, the reason and any required force flag.

    Raises:
        NotFoundError when no block has that id.
    """
    require_block(state, block_id)

    track = state.network.get_track(x, y)
    if not track:
        return {"can_extend": False, "reason": "No track at position"}

    if crossing_disallowed_in_block(track.track_bits):
        return {
            "can_extend": False,
            "reason": "Cannot add crossing to block - crossings isolate paths",
        }

    if track.is_switch:
        return {
            "can_extend": False,
            "reason": "Cannot add switch to block - switches are block boundaries",
        }

    if track.signal_ids:
        return {
            "can_extend": False,
            "reason": "Tile has signal(s) - use force=true to extend past signal",
            "requires_force": True,
        }

    if track.block_section_id and track.block_section_id != block_id:
        return {
            "can_extend": False,
            "reason": (
                f"Tile already in block {track.block_section_id} - "
                "use force_overwrite=true"
            ),
            "requires_force_overwrite": True,
        }

    existing_positions = state.network.get_block_positions(block_id)
    is_adjacent = any(
        abs(x - old_x) + abs(y - old_y) == 1 for old_x, old_y in existing_positions
    )
    if not is_adjacent and len(existing_positions) > 0:
        return {
            "can_extend": False,
            "reason": "Position must be adjacent to existing block",
        }

    return {"can_extend": True}
