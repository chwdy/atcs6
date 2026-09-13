"""Assertion factories for journey events.

Each factory returns a `Snapshot -> None` callable that raises AssertionError
on failure. Messages are intentionally explicit so the failure report
identifies the exact mismatch.
"""

from typing import Callable, Iterable, Optional, Set, Tuple, Union

from enums import AtcLimitReasonCode, BlockState, TrainState

from .harness import Snapshot

Check = Callable[[Snapshot], None]


# --------------------------------------------------------------------------- #
# Controller scalar fields
# --------------------------------------------------------------------------- #


def assert_state(expected: TrainState) -> Check:
    def check(s: Snapshot) -> None:
        assert s.state == expected, (
            f"state expected {expected.value}, got {s.state.value}"
        )

    return check


def assert_cab(expected: Optional[str]) -> Check:
    def check(s: Snapshot) -> None:
        assert s.cab == expected, f"cab expected {expected!r}, got {s.cab!r}"

    return check


def assert_eb(active: bool = False) -> Check:
    def check(s: Snapshot) -> None:
        assert s.eb_active == active, (
            f"eb_active expected {active}, got {s.eb_active}"
        )

    return check


def assert_ato(running: bool) -> Check:
    def check(s: Snapshot) -> None:
        assert s.ato_engaged == running, (
            f"ato_engaged expected {running}, got {s.ato_engaged}"
        )

    return check


def assert_speed_le(value_kmh: float, epsilon: float = 0.5) -> Check:
    def check(s: Snapshot) -> None:
        assert s.speed_kmh <= value_kmh + epsilon, (
            f"speed_kmh {s.speed_kmh:.2f} exceeds {value_kmh:.2f} "
            f"(epsilon={epsilon})"
        )

    return check


def assert_speed_gt(value_kmh: float) -> Check:
    def check(s: Snapshot) -> None:
        assert s.speed_kmh > value_kmh, (
            f"speed_kmh {s.speed_kmh:.2f} not greater than {value_kmh:.2f}"
        )

    return check


def assert_speed_eq(value_kmh: float, epsilon: float = 0.5) -> Check:
    def check(s: Snapshot) -> None:
        assert abs(s.speed_kmh - value_kmh) <= epsilon, (
            f"speed_kmh {s.speed_kmh:.2f} != {value_kmh:.2f} (epsilon={epsilon})"
        )

    return check


# --------------------------------------------------------------------------- #
# Span / position
# --------------------------------------------------------------------------- #


def assert_head_at(tile: Tuple[int, int]) -> Check:
    target = tuple(tile)

    def check(s: Snapshot) -> None:
        assert s.head_tile == target, (
            f"head_tile expected {target}, got {s.head_tile}"
        )

    return check


def assert_head_in(tiles: Iterable[Tuple[int, int]]) -> Check:
    allowed: Set[Tuple[int, int]] = {tuple(t) for t in tiles}

    def check(s: Snapshot) -> None:
        assert s.head_tile in allowed, (
            f"head_tile expected one of {sorted(allowed)}, got {s.head_tile}"
        )

    return check


def assert_span_includes_tile(tile: Tuple[int, int]) -> Check:
    target = tuple(tile)

    def check(s: Snapshot) -> None:
        assert target in s.occupied_tiles, (
            f"span occupied_tiles {s.occupied_tiles} does not include {target}"
        )

    return check


# --------------------------------------------------------------------------- #
# Block ownership (resolved from tile)
# --------------------------------------------------------------------------- #


def _resolve_block_id_at_tile(s: Snapshot, tile: Tuple[int, int]) -> Optional[str]:
    track = s.network.get_track(*tile)
    if track is None:
        return None
    return getattr(track, "block_id", None)


def assert_block_at_tile_occupied_by(
    tile: Tuple[int, int], train_id: str
) -> Check:
    def check(s: Snapshot) -> None:
        block_id = _resolve_block_id_at_tile(s, tile)
        assert block_id is not None, f"no block at tile {tile}"
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.occupied_by == train_id, (
            f"block {block_id} (at {tile}) occupied_by={block.occupied_by!r}, "
            f"expected {train_id!r}"
        )

    return check


def assert_block_at_tile_unoccupied(tile: Tuple[int, int]) -> Check:
    def check(s: Snapshot) -> None:
        block_id = _resolve_block_id_at_tile(s, tile)
        assert block_id is not None, f"no block at tile {tile}"
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.occupied_by is None, (
            f"block {block_id} (at {tile}) still occupied_by={block.occupied_by!r}"
        )

    return check


def assert_block_id_unoccupied(block_id: str) -> Check:
    def check(s: Snapshot) -> None:
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.occupied_by is None, (
            f"block {block_id} still occupied_by={block.occupied_by!r}"
        )

    return check


def assert_block_id_occupied_by(block_id: str, train_id: str) -> Check:
    def check(s: Snapshot) -> None:
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.occupied_by == train_id, (
            f"block {block_id} occupied_by={block.occupied_by!r}, "
            f"expected {train_id!r}"
        )

    return check


def assert_block_id_state(block_id: str, expected: BlockState) -> Check:
    """Assert the computed `state` property of a block.

    Per design_new.md "Block" — `state` is derived with priority
    OCCUPIED > CAUTION > RESERVED > FREE from the underlying primitives.
    """

    def check(s: Snapshot) -> None:
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.state == expected, (
            f"block {block_id} state={block.state.value}, "
            f"expected {expected.value} "
            f"(occupied_by={block.occupied_by!r}, "
            f"caution={block.caution!r}, "
            f"reservation={block.reservation!r})"
        )

    return check


# --------------------------------------------------------------------------- #
# Block reservation (path-side)
# --------------------------------------------------------------------------- #


def assert_block_id_reserved_by_path(
    block_id: str, path_id_or_none: Optional[str] = None
) -> Check:
    """Assert a block is held by an active path's dispatcher reservation.

    `path_id_or_none=None` accepts any non-None reservation path id; pass a
    concrete id to require an exact match.
    """

    def check(s: Snapshot) -> None:
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.reserved_by_path is not None, (
            f"block {block_id} has no reservation "
            f"(expected reserved_by_path != None)"
        )
        if path_id_or_none is not None:
            assert block.reserved_by_path == path_id_or_none, (
                f"block {block_id} reserved_by_path={block.reserved_by_path!r}, "
                f"expected {path_id_or_none!r}"
            )

    return check


def assert_block_id_reserved_for_train(block_id: str, train_id: str) -> Check:
    def check(s: Snapshot) -> None:
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.reserved_for_train == train_id, (
            f"block {block_id} reserved_for_train={block.reserved_for_train!r}, "
            f"expected {train_id!r}"
        )

    return check


def assert_block_id_not_reserved(block_id: str) -> Check:
    def check(s: Snapshot) -> None:
        block = s.network.blocks.get(block_id)
        assert block is not None, f"block {block_id} not found"
        assert block.reservation is None, (
            f"block {block_id} still reserved "
            f"(reserved_by_path={block.reserved_by_path!r}, "
            f"reserved_for_train={block.reserved_for_train!r})"
        )

    return check


def assert_block_at_tile_reserved_by_path(
    tile: Tuple[int, int], path_id_or_none: Optional[str] = None
) -> Check:
    def check(s: Snapshot) -> None:
        block_id = _resolve_block_id_at_tile(s, tile)
        assert block_id is not None, f"no block at tile {tile}"
        assert_block_id_reserved_by_path(block_id, path_id_or_none)(s)

    return check


# --------------------------------------------------------------------------- #
# Switch ownership / locks
# --------------------------------------------------------------------------- #


def assert_switch_unoccupied(switch_id: str) -> Check:
    def check(s: Snapshot) -> None:
        sw = s.network.switches.get(switch_id)
        assert sw is not None, f"switch {switch_id} not found"
        assert sw.occupied_by is None, (
            f"switch {switch_id} still occupied_by={sw.occupied_by!r}"
        )

    return check


def assert_switch_locked(
    switch_id: str, locked_state: Optional[int] = None
) -> Check:
    """Assert the switch holds at least one path lock (optionally requiring
    `locked_state`).
    """

    def check(s: Snapshot) -> None:
        sw = s.network.switches.get(switch_id)
        assert sw is not None, f"switch {switch_id} not found"
        assert sw.locks, (
            f"switch {switch_id} has no locks (expected at least one)"
        )
        if locked_state is not None:
            states = {state for (_train, state) in sw.locks.values()}
            assert locked_state in states, (
                f"switch {switch_id} locks {dict(sw.locks)} do not require "
                f"state={locked_state}"
            )

    return check


def assert_switch_unlocked(switch_id: str) -> Check:
    """Assert no future-authority locks AND no train-protection claims."""

    def check(s: Snapshot) -> None:
        sw = s.network.switches.get(switch_id)
        assert sw is not None, f"switch {switch_id} not found"
        assert not sw.locks, f"switch {switch_id} still has locks {dict(sw.locks)}"
        assert not sw.train_protection_claims, (
            f"switch {switch_id} still has train_protection_claims "
            f"{sw.train_protection_claims}"
        )

    return check


def assert_switch_position(switch_id: str, position: int) -> Check:
    def check(s: Snapshot) -> None:
        sw = s.network.switches.get(switch_id)
        assert sw is not None, f"switch {switch_id} not found"
        assert int(sw.position) == int(position), (
            f"switch {switch_id} position={int(sw.position)}, expected {position}"
        )

    return check


# --------------------------------------------------------------------------- #
# Path / route
# --------------------------------------------------------------------------- #


def assert_no_active_path() -> Check:
    def check(s: Snapshot) -> None:
        assert s.active_path_id is None, (
            f"expected no active path, got {s.active_path_id}"
        )

    return check


def assert_has_active_path() -> Check:
    def check(s: Snapshot) -> None:
        assert s.active_path_id is not None, "expected an active path, got None"

    return check


def assert_active_path_authority_end_in(allowed: Iterable[str]) -> Check:
    allowed_set = set(allowed)

    def check(s: Snapshot) -> None:
        assert s.active_path_id is not None, "no active path"
        assert s.active_path_reservation_end_block_id in allowed_set, (
            f"active path reservation_end_block_id="
            f"{s.active_path_reservation_end_block_id!r} not in {allowed_set}"
        )

    return check


def assert_route_assigned(route_id: str) -> Check:
    def check(s: Snapshot) -> None:
        assert s.assigned_route_id == route_id, (
            f"assigned_route_id={s.assigned_route_id!r}, expected {route_id!r}"
        )

    return check


def assert_route_not_assigned() -> Check:
    def check(s: Snapshot) -> None:
        assert s.assigned_route_id is None, (
            f"expected no assigned route, got {s.assigned_route_id!r}"
        )

    return check


# --------------------------------------------------------------------------- #
# Command inbox
# --------------------------------------------------------------------------- #


def assert_command_inbox_contains(kind: str) -> Check:
    def check(s: Snapshot) -> None:
        assert kind in s.command_inbox_kinds, (
            f"command_inbox {s.command_inbox_kinds} does not contain {kind!r}"
        )

    return check


def assert_command_inbox_empty() -> Check:
    def check(s: Snapshot) -> None:
        assert not s.command_inbox_kinds, (
            f"command_inbox not empty: {s.command_inbox_kinds}"
        )

    return check


# --------------------------------------------------------------------------- #
# ATC reason
# --------------------------------------------------------------------------- #


CodeOrName = Union[AtcLimitReasonCode, str]


def _normalize_codes(codes: Iterable[CodeOrName]) -> Set[str]:
    out: Set[str] = set()
    for c in codes:
        if isinstance(c, AtcLimitReasonCode):
            out.add(c.value)
        else:
            out.add(str(c))
    return out


def assert_atc_reason_in(allowed: Iterable[CodeOrName]) -> Check:
    allowed_set = _normalize_codes(allowed)

    def check(s: Snapshot) -> None:
        actual = (
            s.atc_reason_code.value
            if isinstance(s.atc_reason_code, AtcLimitReasonCode)
            else None
        )
        assert actual in allowed_set, (
            f"atc reason {actual!r} not in {allowed_set} "
            f"(speed_limit_reason={s.atc_reason_str!r})"
        )

    return check


def assert_atc_reason_not_in(forbidden: Iterable[CodeOrName]) -> Check:
    forbidden_set = _normalize_codes(forbidden)

    def check(s: Snapshot) -> None:
        actual = (
            s.atc_reason_code.value
            if isinstance(s.atc_reason_code, AtcLimitReasonCode)
            else None
        )
        if actual is None:
            return  # no current restriction → nothing to forbid
        assert actual not in forbidden_set, (
            f"atc reason {actual!r} is in forbidden set {forbidden_set} "
            f"(speed_limit_reason={s.atc_reason_str!r})"
        )

    return check
