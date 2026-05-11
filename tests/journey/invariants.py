"""Invariant factories for journey runs.

Invariants run on every tick. They guard the protocol-level rules from
design_new.md (allowed `(state, cab)` pairs, RUNNING_AT_STATION ⇒ no path,
no_overspeed, span ⇔ resource consistency).
"""

from typing import Callable

from enums import TrainState

from .events import Invariant
from .harness import Snapshot

Check = Callable[[Snapshot], None]


# --------------------------------------------------------------------------- #
# Speed / safety
# --------------------------------------------------------------------------- #


def no_overspeed(epsilon: float = 0.5) -> Invariant:
    """speed_kmh ≤ atc_limit + epsilon (when a positive ATC limit exists).

    A zero ATC limit with zero speed is fine; a zero ATC limit with positive
    speed would be an EB scenario already covered by the no_eb invariant.
    """

    def check(s: Snapshot) -> None:
        if s.atc_limit_kmh <= 0:
            return
        assert s.speed_kmh <= s.atc_limit_kmh + epsilon, (
            f"overspeed: speed={s.speed_kmh:.2f} > atc_limit="
            f"{s.atc_limit_kmh:.2f} + epsilon={epsilon} "
            f"(reason={s.atc_reason_str!r})"
        )

    return Invariant(name="no_overspeed", check=check)


def no_eb() -> Invariant:
    """eb_active must remain False for the entire run."""

    def check(s: Snapshot) -> None:
        assert not s.eb_active, (
            f"eb_active became True (handle_reason={s.handle_reason!r}, "
            f"speed={s.speed_kmh:.2f}, atc={s.atc_limit_kmh:.2f})"
        )

    return Invariant(name="no_eb", check=check)


def no_pending_path_cancel_request() -> Invariant:
    def check(s: Snapshot) -> None:
        assert not s.latch_path_cancel_request, (
            "phase_latches.path_cancel_request was armed during the run"
        )

    return Invariant(name="no_pending_path_cancel_request", check=check)


def no_pending_route_abort() -> Invariant:
    def check(s: Snapshot) -> None:
        assert not s.latch_route_abort, (
            "phase_latches.route_abort was armed during the run"
        )

    return Invariant(name="no_pending_route_abort", check=check)


def no_pending_power_off() -> Invariant:
    def check(s: Snapshot) -> None:
        assert not s.latch_power_off, (
            "phase_latches.power_off was armed during the run"
        )

    return Invariant(name="no_pending_power_off", check=check)


# --------------------------------------------------------------------------- #
# State machine consistency (per design_new.md)
# --------------------------------------------------------------------------- #


def state_cab_pair_valid() -> Invariant:
    """Per design_new.md "Allowed (state, cab) pairs":
        OFF                 → cab is None or pre-armed by bootstrap
        READY               → cab in {None, "A", "B"}
        RUNNING_MOVING      → cab in {"A", "B"}
        RUNNING_AT_STATION  → cab in {"A", "B"}

    Bootstrap is allowed to pre-set cab while OFF (verified by
    test_bootstrap_queues_power_on_for_first_tick_drain). We therefore allow
    OFF + any cab; the harder invariant is that RUNNING_* always has a cab.
    """

    def check(s: Snapshot) -> None:
        if s.state == TrainState.OFF:
            return
        if s.state == TrainState.READY:
            assert s.cab in (None, "A", "B"), f"READY with cab={s.cab!r}"
            return
        # RUNNING_MOVING or RUNNING_AT_STATION
        assert s.cab in ("A", "B"), (
            f"{s.state.value} requires cab in (A,B), got {s.cab!r}"
        )

    return Invariant(name="state_cab_pair_valid", check=check)


def at_station_implies_no_path() -> Invariant:
    """Per design_new.md:1632 — RUNNING_AT_STATION ⇒ received_path_plan is None."""

    def check(s: Snapshot) -> None:
        if s.state == TrainState.RUNNING_AT_STATION:
            assert s.active_path_id is None, (
                f"RUNNING_AT_STATION with active_path={s.active_path_id!r}"
            )

    return Invariant(name="at_station_implies_no_path", check=check)


def ato_running_implies_running_moving_with_cab() -> Invariant:
    """When ato_running is True, the controller must be RUNNING_MOVING and
    have a cab selected. (Path is grown by Phase 1, but ato_running can
    survive a short window between authority loss and braking — so we only
    check state + cab here.)
    """

    def check(s: Snapshot) -> None:
        if not s.ato_running:
            return
        assert s.state == TrainState.RUNNING_MOVING, (
            f"ato_running True but state={s.state.value}"
        )
        assert s.cab in ("A", "B"), f"ato_running True but cab={s.cab!r}"

    return Invariant(name="ato_running_implies_running_moving_with_cab", check=check)


# --------------------------------------------------------------------------- #
# Span ↔ resource consistency
# --------------------------------------------------------------------------- #


def span_block_consistency() -> Invariant:
    """Tile-based consistency between the span and BlockManager.

    Two directions:
      1. Every span tile that belongs to some block must be in a block whose
         `occupied_by == train_id`. (No tile can be in a block we don't own.)
      2. Every block claiming `occupied_by == train_id` must have at least
         one of its tiles in the span. (We don't pretend to own blocks we've
         already vacated.)

    We deliberately compare on TILES rather than on block-id sets — the span's
    `occupied_blocks` list and the BlockManager registry are updated in
    different phases (Phase 2 physics vs Phase 3 dispatcher progress) and can
    legitimately disagree on block IDs for one tick during a transition. The
    underlying tile ↔ ownership relation, however, must always agree.
    """

    def check(s: Snapshot) -> None:
        train_id = s.fixture.train_id
        span_tiles = {tuple(t) for t in s.occupied_tiles}

        # Direction 1: every span tile in a block ⇒ that block owned by us.
        for tile in span_tiles:
            track = s.network.get_track(*tile)
            if track is None:
                continue
            block_id = getattr(track, "block_section_id", None)
            if not block_id:
                continue  # switch / boundary tile — no block
            block = s.network.blocks.get(block_id)
            if block is None:
                continue
            assert block.occupied_by == train_id, (
                f"span tile {tile} is in block {block_id} but "
                f"block.occupied_by={block.occupied_by!r}, expected {train_id!r}"
            )

        # Direction 2: every owned block has at least one tile in the span.
        for bid, block in s.network.blocks.items():
            if block.occupied_by != train_id:
                continue
            positions = s.network.get_block_positions(bid) or []
            block_tiles = {(p[0], p[1]) for p in positions}
            assert block_tiles & span_tiles, (
                f"block {bid} claims occupied_by={train_id} but none of its "
                f"tiles {sorted(block_tiles)} appear in span occupied_tiles "
                f"{sorted(span_tiles)}"
            )

    return Invariant(name="span_block_consistency", check=check)


def span_switch_consistency() -> Invariant:
    """For every switch with `occupied_by == train_id`, the switch tile must
    appear in `current_span.occupied_tiles`.
    """

    def check(s: Snapshot) -> None:
        train_id = s.fixture.train_id
        tiles = {tuple(t) for t in s.occupied_tiles}
        for sid, sw in s.network.switches.items():
            if sw.occupied_by != train_id:
                continue
            tile = (sw.x, sw.y)
            assert tile in tiles, (
                f"switch {sid} claims occupied_by={train_id} but its tile "
                f"{tile} is not in span occupied_tiles {sorted(tiles)}"
            )

    return Invariant(name="span_switch_consistency", check=check)
