"""Block grouping + config service — the power/occupancy axis of the model.

The HTTP layer in ``routes/blocks.py`` parses JSON and shapes the read-only
serialization responses (`to_dict`, monitoring signals, sorted positions);
this layer owns the block-side mutations:

  * Grouping — create a block from a `segment_ids` list, dissolve (ungroup) a
    block, and add / remove a member `Segment` at either end. These are the
    four-method `BlockGroupingManager` surface (design_new.md **Block
    grouping**), the replacement for the old power-district grouping API.
  * Config + availability — name, holding-point flag, `disabled` / `manual`
    availability flags, and the static `allowed_travel_directions` policy.

Geometry (length / radius / grade / speed-limit / tiles) is NOT edited here —
it lives on the member segments and is written through
``services.segment_service``. A config PATCH that carries a geometry field is
rejected so the caller addresses the segment it means.

Lookup raises ``NotFoundError``; bad input raises ``ValidationError``; a
disabled-toggle blocked by occupancy/reservation/CAUTION raises
``ConflictError``; the removed direct ``state`` mutation raises ``GoneError``
(HTTP 410). ``update_block`` takes ``state.simulation_lock`` so the per-tick
phase loop cannot observe a torn read.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import uuid

from app_state import AppState
from services.exceptions import (
    ConflictError,
    GoneError,
    NotFoundError,
    ValidationError,
)


# Geometry lives on the member segments; a block config PATCH that carries one
# of these is rejected and pointed at the segment endpoint.
_GEOMETRY_FIELDS = frozenset(
    {
        "length_cm",
        "min_radius_cm",
        "grade_percent",
        "speed_limit_kmh",
        "max_speed_kmh",
        "ordered_tiles",
    }
)


# --------------------------------------------------------------- lookups ----


def get_block(state: AppState, block_id: str):
    """Return the block, or None when not found."""
    return state.network.get_block(block_id)



def list_blocks(state: AppState) -> List[Any]:
    """Return every block on the network."""
    return list(state.network.blocks.values())


# -------------------------------------------------------------- grouping ----


def create_block(
    state: AppState,
    *,
    block_id: Optional[str] = None,
    segment_ids: List[str],
    name: str = "",
):
    """Group existing segments into one block sharing one power feed.

    `segment_ids` is the member list. The caller does not have to supply it in
    run order: this works the order out itself (`BlockGroupingManager.plan_run`),
    because an operator selecting segments on the canvas has no canonical order
    to give.

    Ordering is the only thing inferred. Grouping edits the power-grouping axis
    (`segment_ids`) and nothing else — it must never rewrite a member's stored
    geometry. So when a member is stored running the opposite way round the run,
    this rejects rather than silently reversing its `ordered_tiles`: reversing is
    a geometry write and belongs on the segment axis
    (`POST /api/segments/<segment_id>/reverse`).

    Raises:
        ValidationError when the list is empty, a segment is unknown, the id is
            taken, a member is stored in the opposite direction, or the segments
            do not form one connected switch-free run.
    """
    resolved_id = block_id or f"BLK_{uuid.uuid4().hex[:8].upper()}"
    ids = list(segment_ids or [])
    if not ids:
        raise ValidationError("A block must group at least one segment")

    grouping = state.network._get_block_grouping_manager()
    ordered, to_reverse = grouping.plan_run(ids)
    if to_reverse:
        raise ValidationError(
            "These segments are stored running the opposite way round the run: "
            f"{', '.join(sorted(to_reverse))}. Grouping only edits block "
            "membership, so it will not rewrite their geometry — reverse each "
            "with POST /api/segments/<segment_id>/reverse, then group again."
        )
    try:
        return state.network.create_block(resolved_id, ordered, name)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def delete_block(state: AppState, block_id: str) -> bool:
    """Dissolve a block grouping (ungroup). Returns True.

    Every member segment is re-homed into its own single-segment block, the
    default one-feed-per-segment layout (design_new.md, "Non-empty parent rule") —
    `BlockGroupingManager.delete_block`. This never deletes geometry; deleting a
    segment's tiles is a segment-geometry operation
    (``services.segment_service.delete_segment``).

    Raises:
        NotFoundError when no block has that id.
    """
    grouping = state.network._get_block_grouping_manager()
    if not grouping.delete_block(block_id):
        raise NotFoundError("Block not found")
    return True


def add_segment_to_block(state: AppState, block_id: str, segment_id: str):
    """Add `segment_id` to either end of `block_id`'s run. Returns the block.

    Raises:
        NotFoundError when the block or the segment is unknown.
        ValidationError when the add would break the switch-free run.
    """
    if state.network.get_block(block_id) is None:
        raise NotFoundError("Block not found")
    if state.network.get_segment(segment_id) is None:
        raise NotFoundError("Segment not found")
    try:
        state.network.add_segment_to_block(block_id, segment_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return state.network.get_block(block_id)


def remove_segment_from_block(
    state: AppState, block_id: str, segment_id: str
) -> bool:
    """Remove `segment_id` from either end of `block_id`'s run.

    The segment returns to its own single-segment block; removing the block's
    last member dissolves the block (design_new.md **Block grouping**). Returns
    True when removed, False when the segment was not a member.

    Raises:
        NotFoundError when no block has that id.
        ValidationError when the removal is not at an end (a middle segment).
    """
    if state.network.get_block(block_id) is None:
        raise NotFoundError("Block not found")
    try:
        return state.network.remove_segment_from_block(block_id, segment_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


# -------------------------------------------------------- config + flags ----


def update_block(state: AppState, block_id: str, *, data: Dict[str, Any]):
    """Update a block's config and/or availability flags (NOT geometry).

    Handles ``name``, ``is_holding_point``, the
    ``disabled`` / ``manual`` availability flags, and the static
    ``allowed_travel_directions`` policy. Geometry fields are rejected — they live on the member segments and are
    written through ``services.segment_service.update_segment``.

    Mutations happen under ``state.simulation_lock`` so the per-tick phase loop
    cannot observe a torn read.

    Raises:
        NotFoundError when no block has that id.
        GoneError when the request carries the removed ``state`` field.
        ConflictError when enabling ``disabled`` while the block is
            occupied / reserved / in CAUTION.
        ValidationError when a geometry field is present, or
            ``allowed_travel_directions`` is malformed.
    """
    data = dict(data or {})

    geometry_present = _GEOMETRY_FIELDS & set(data)
    if geometry_present:
        raise ValidationError(
            f"Geometry field(s) {sorted(geometry_present)} are edited on the "
            "segment, not the block. Use PATCH /api/segments/<segment_id>."
        )

    with state.simulation_lock:
        block = state.network.get_block(block_id)
        if not block:
            raise NotFoundError("Block not found")

        if "name" in data:
            block.name = data["name"] if data["name"] is not None else ""

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
        # Holding-point config flag (design_new.md "Block"). No
        # precondition — it does not affect availability, reservation, or
        # runtime state; it only biases best-effort truncation.
        if "is_holding_point" in data:
            block.is_holding_point = bool(data["is_holding_point"])

        # Rail polarity moved onto the block's `block_track` hardware binding
        # (design_new.md "Power", Rail polarity lives on the rail binding). It
        # is wiring, so it is edited and auto-persisted with the wiring — PUT
        # /api/hardware/binding — not through the block PATCH. Reject rather
        # than silently drop, so a stale client learns where the write went.
        if "polarity_reversed" in data:
            raise ValidationError(
                "polarity_reversed lives on the block's rail binding now. "
                "Set it via PUT /api/hardware/binding (map 'block_track')."
            )

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
