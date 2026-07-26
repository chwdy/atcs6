"""Platform lifecycle and tile-membership service.

The HTTP layer in ``routes/platforms.py`` parses JSON and serializes the
``to_dict_with_orientation`` responses; this layer owns the platform
mutations (create / update / delete / extend / remove-tile / assign- and
unassign-tile-to-block) and the two read-only placement predicates
(``validate_placement``, ``can_extend``).

Lookup raises ``NotFoundError``; bad input raises ``ValidationError``.
``remove_tile`` returns a small tagged result dict so the route can map
its outcomes (continuity rejection, empty-platform deletion, plain
removal) to their exact responses without this layer knowing status codes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app_state import AppState
from enums import PlatformSide
from services.exceptions import NotFoundError, ValidationError
from services.validation import coerce_nonneg


def get_platform(state: AppState, platform_id: str):
    """Return the platform, or None when not found."""
    return state.network.get_platform(platform_id)


def require_platform(state: AppState, platform_id: str):
    """Return the platform or raise NotFoundError."""
    platform = state.network.get_platform(platform_id)
    if platform is None:
        raise NotFoundError("Platform not found")
    return platform


def create_platform(
    state: AppState,
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    positions: Optional[List[Any]] = None,
    platform_id: Optional[str] = None,
    name: str = "",
    length_cm: float = 0.0,
    platform_side: str = "both",
    top_speed_kmh: Optional[Any] = None,
):
    """Create a platform spanning one or more tiles.

    The tile set comes from ``positions`` (a list of ``[x, y]`` pairs, used by
    the drag-to-create tool) when given; otherwise it falls back to the single
    ``(x, y)`` anchor. The first tile is the anchor that fixes the platform's
    orientation; any remaining tiles are added with the same rules the extend
    tool uses. If extending the anchor fails, the anchor is deleted so a failed
    multi-tile create leaves no stray single-tile platform behind.

    Raises:
        ValidationError when no position is supplied or the network rejects
            the placement.
    """
    if positions:
        tiles = [tuple(pos) for pos in positions]
    elif x is not None and y is not None:
        tiles = [(x, y)]
    else:
        raise ValidationError("Missing platform position")

    anchor_x, anchor_y = tiles[0]
    try:
        platform = state.network.add_platform(
            x=anchor_x,
            y=anchor_y,
            platform_id=platform_id,
            name=name,
            length_cm=length_cm,
            platform_side=platform_side,
            top_speed_kmh=top_speed_kmh,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    rest = tiles[1:]
    if rest:
        try:
            state.network.extend_platform(platform.platform_id, rest)
        except ValueError as exc:
            state.network.delete_platform(platform.platform_id)
            raise ValidationError(str(exc)) from exc

    return platform


def update_platform(state: AppState, platform_id: str, *, data: Dict[str, Any]):
    """Apply a partial update to a platform's config / availability flags.

    Raises:
        NotFoundError when no platform has that id.
        ValidationError when a supplied value is invalid (e.g. an
            unrecognized ``platform_side``).
    """
    platform = require_platform(state, platform_id)

    if "name" in data:
        platform.name = data["name"]
    # Geometry (the UI enters whole mm and sends cm). Length is required and
    # non-negative; top speed accepts None = "no limit".
    if "length_cm" in data:
        if data["length_cm"] is None:
            raise ValidationError("length_cm is required and cannot be empty")
        platform.length_cm = coerce_nonneg(data["length_cm"], "length_cm")
    if "platform_side" in data:
        try:
            platform.platform_side = PlatformSide(data["platform_side"])
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    if "top_speed_kmh" in data:
        raw = data["top_speed_kmh"]
        platform.top_speed_kmh = None if raw is None else coerce_nonneg(raw, "top_speed_kmh")
    # Availability flags per design_new.md "Availability flag REST API
    # surface": both succeed unconditionally -- they affect only future
    # route target resolution.
    if "disabled" in data:
        platform.disabled = bool(data["disabled"])
    if "manual" in data:
        platform.manual = bool(data["manual"])

    return platform


def delete_platform(state: AppState, platform_id: str) -> bool:
    """Delete a platform. Returns True.

    Raises:
        NotFoundError when no platform has that id.
    """
    if not state.network.delete_platform(platform_id):
        raise NotFoundError("Platform not found")
    return True


def validate_placement(state: AppState, x: int, y: int) -> Dict[str, Any]:
    """Read-only check of whether a platform may be placed on a tile."""
    track = state.network.get_track(x, y)
    if not track:
        return {"can_place": False, "error": f"No track at position ({x}, {y})"}
    if track.is_switch:
        return {"can_place": False, "error": "Cannot place platform on switch"}
    if track.signal_ids:
        return {
            "can_place": False,
            "error": "Cannot place platform on tile with signal",
        }
    if track.station_id is not None:
        return {
            "can_place": False,
            "error": f"Tile already belongs to platform {track.station_id}",
        }
    return {"can_place": True, "existing_block": track.block_id}


def extend_platform(state: AppState, platform_id: str, *, positions: List[Any]):
    """Extend a platform with new tile positions.

    Raises:
        ValidationError when the network rejects the extension.
    """
    try:
        return state.network.extend_platform(
            platform_id, [tuple(pos) for pos in positions]
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def remove_tile(state: AppState, platform_id: str, *, x: int, y: int) -> Dict[str, Any]:
    """Remove a tile from a platform, enforcing continuity.

    Returns a tagged result dict:
        {"status": "continuity_error", "errors": [...]}  # removal breaks it
        {"status": "deleted"}                            # platform emptied
        {"status": "removed", "platform": <Platform>}    # tile removed

    Raises:
        NotFoundError when no platform has that id.
        ValidationError when the tile is not part of the platform.
    """
    platform = require_platform(state, platform_id)

    if (x, y) not in platform.positions:
        raise ValidationError(
            f"Position ({x}, {y}) is not part of platform {platform_id}"
        )

    new_positions = [p for p in platform.positions if p != (x, y)]
    if len(new_positions) > 1:
        is_continuous, errors = state.network._validate_station_continuity(
            new_positions
        )
        if not is_continuous:
            return {"status": "continuity_error", "errors": errors}

    platform.remove_position(x, y)
    track = state.network.get_track(x, y)
    if track and track.station_id == platform_id:
        track.station_id = None
        if track.segment_id:
            state.network.remove_tile_from_segment(x, y)

    if len(platform.positions) == 0:
        state.network.delete_platform(platform_id)
        return {"status": "deleted"}

    return {"status": "removed", "platform": platform}


def can_extend(state: AppState, platform_id: str, *, x: int, y: int) -> Dict[str, Any]:
    """Read-only check of whether a tile may join the platform.

    Raises:
        NotFoundError when no platform has that id.
    """
    platform = require_platform(state, platform_id)

    track = state.network.get_track(x, y)
    if not track:
        return {"can_extend": False, "reason": "No track at position"}
    if track.is_switch:
        return {"can_extend": False, "reason": "Cannot add switch to platform"}
    if track.signal_ids:
        return {
            "can_extend": False,
            "reason": "Cannot add tile with signal to platform",
        }
    if track.station_id is not None and track.station_id != platform_id:
        return {
            "can_extend": False,
            "reason": f"Tile already belongs to platform {track.station_id}",
        }

    is_adjacent = any(
        abs(x - px) + abs(y - py) == 1 for px, py in platform.positions
    )
    if not is_adjacent and len(platform.positions) > 0:
        return {
            "can_extend": False,
            "reason": "Position must be adjacent to existing platform",
        }

    all_positions = platform.positions + [(x, y)]
    is_continuous, errors = state.network._validate_station_continuity(all_positions)
    if not is_continuous:
        return {
            "can_extend": False,
            "reason": f"Would break continuity: {', '.join(errors)}",
        }

    return {"can_extend": True}


def assign_tile(
    state: AppState, platform_id: str, *, x: int, y: int, block_id: str
):
    """Assign a platform tile to a block.

    Raises:
        NotFoundError when the platform or the block does not exist.
        ValidationError when the tile is not part of the platform.
    """
    platform = require_platform(state, platform_id)
    if not state.network.get_block(block_id):
        raise NotFoundError("Block not found")
    if not platform.assign_tile_to_block(x, y, block_id):
        raise ValidationError("Tile not in platform")
    return platform


def unassign_tile(state: AppState, platform_id: str, *, x: int, y: int):
    """Clear a platform tile's block assignment.

    Raises:
        NotFoundError when no platform has that id.
        ValidationError when the tile has no assignment or is not in the
            platform.
    """
    platform = require_platform(state, platform_id)
    if not platform.unassign_tile_from_block(x, y):
        raise ValidationError("Tile has no block assignment or not in platform")
    return platform
