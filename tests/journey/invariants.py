"""Invariant factories for journey runs.

Invariants run on every tick. They guard the protocol-level rules from
design_new.md (allowed `(state, cab)` pairs, RUNNING_AT_STATION ⇒ no path,
no_overspeed, span ⇔ resource consistency).
"""

from typing import Callable, Dict, Set

from constants import OCCUPANCY_OFF_THRESHOLD
from enums import BlockState, TrainState

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
            block_id = getattr(track, "block_id", None)
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


# --------------------------------------------------------------------------- #
# Block state contract (per design_new.md "Block" section)
# --------------------------------------------------------------------------- #


def block_occupied_iff_span_covers_block() -> Invariant:
    """Per design_new.md "Block model" + "Power -> Block occupancy detection" —
    `Block.occupied_by` is physical train presence, now driven by the debounced
    current detector. Detection lags the precise span by the sensor/debounce
    latency, so the invariant accounts for the two debounce windows:

      (a) If any tile of a block is in the train's span, that block MUST be
          OCCUPIED by the train OR RESERVED for it. A block the head has just
          entered is part of the train's reserved-ahead corridor until the
          detector confirms current (entry latency); it is never free and never
          another train's.

      (b) If `block.occupied_by == train_id` but NO tile of that block is in the
          span, the block must be inside the fail-safe exit-debounce wake — the
          tail cleared it but the detector holds occupancy for up to
          OCCUPANCY_OFF_THRESHOLD ticks (riding through contact dropouts).
          Beyond that window with no span coverage, occupancy is stale.

    Direction (a) still catches an OCCUPIED→FREE transition while the train is
    physically on the block (it would be neither occupied nor reserved).
    Direction (b) still catches a tail-vacate that never fired — its
    `ticks_since` last coverage grows without bound past the fail-safe window.
    """

    last_span_tick: Dict[str, int] = {}
    # The detector reads end-of-last-tick positions (one tick of sensor latency)
    # before the OFF debounce starts counting, so allow a small margin over the
    # raw OFF threshold. A genuinely stuck occupancy grows unbounded past this.
    grace = OCCUPANCY_OFF_THRESHOLD + 3

    def check(s: Snapshot) -> None:
        train_id = s.fixture.train_id
        span_tiles = {tuple(t) for t in s.occupied_tiles}

        covered_blocks: Set[str] = set()
        for tile in span_tiles:
            track = s.network.get_track(*tile)
            if track is None:
                continue
            block_id = getattr(track, "block_id", None)
            if block_id:
                covered_blocks.add(block_id)
        for block_id in covered_blocks:
            last_span_tick[block_id] = s.tick

        # Direction (a): every span-covered block is OCCUPIED by, or RESERVED
        # for, this train (reserved = entry pending, detector not yet confirmed).
        for block_id in covered_blocks:
            block = s.network.blocks.get(block_id)
            if block is None:
                continue
            assert (
                block.occupied_by == train_id
                or block.reserved_for_train == train_id
            ), (
                f"span covers block {block_id} but block.occupied_by="
                f"{block.occupied_by!r} / reserved_for_train="
                f"{block.reserved_for_train!r} (expected occupied by, or "
                f"reserved for, {train_id!r}); block.state={block.state.value}, "
                f"caution={block.caution!r}, span={sorted(span_tiles)}"
            )

        # Direction (b): a block we own with no span coverage must be inside the
        # fail-safe exit-debounce wake (recently span-covered).
        for bid, block in s.network.blocks.items():
            if block.occupied_by != train_id:
                continue
            positions = s.network.get_block_positions(bid) or []
            block_tiles = {(p[0], p[1]) for p in positions}
            if not block_tiles:
                continue  # empty/legacy block; nothing to verify
            if block_tiles & span_tiles:
                continue
            ticks_since = s.tick - last_span_tick.get(bid, -(10**9))
            assert ticks_since <= grace, (
                f"block {bid} claims occupied_by={train_id} but no span tile "
                f"covers it and its last span coverage was {ticks_since} ticks "
                f"ago (> fail-safe exit-debounce window {grace}); stale physical "
                f"occupancy — tail-vacate likely never fired"
            )

    return Invariant(name="block_occupied_iff_span_covers_block", check=check)


def block_reservation_excludes_occupied_other_train() -> Invariant:
    """Per design_new.md "BlockManager" — `reserve_blocks_for_path(...)` must
    refuse to reserve a block whose `occupied_by` is not the requesting train.
    Equivalently: at every observation, if a block is `RESERVED`/`CAUTION`
    (state-wise) by some path for train T1, then `occupied_by` must be either
    None or T1 — never a different train.
    """

    def check(s: Snapshot) -> None:
        for bid, block in s.network.blocks.items():
            if block.reservation is None:
                continue
            res_train = block.reserved_for_train
            occ = block.occupied_by
            assert occ is None or occ == res_train, (
                f"block {bid} reserved for {res_train!r} but occupied_by="
                f"{occ!r} (state={block.state.value})"
            )

    return Invariant(
        name="block_reservation_excludes_occupied_other_train", check=check
    )


def active_path_blocks_held_or_owned() -> Invariant:
    """Per design_new.md "Path And PathDispatcher" — while a path is `ACTIVE`,
    every block in its forward authority must be either physically occupied
    by the path's train OR reserved by that same path. A block that is FREE
    while the path is active means the dispatcher lost its claim mid-flight
    (e.g. an unexpected `release_path_block_reservations` or a tail-vacate
    that should not have fired).

    Blocks that the train has already exited cleanly transition to CAUTION
    (and then FREE/RESERVED depending on neighbour reevaluation); they are
    no longer in the active forward authority, so we restrict the check to
    blocks whose tiles still appear in the path's resource sequence ahead of
    the train head.
    """

    def check(s: Snapshot) -> None:
        if s.active_path_id is None:
            return
        train_id = s.fixture.train_id
        sim = s.sim_controller
        dispatcher = sim.path_dispatcher
        if dispatcher is None:
            return
        path = dispatcher.get_active_path(train_id)
        if path is None:
            return

        # Resource sequence: a list of (resource_type, resource_id) the path
        # commits to traverse. Find every block past the head's current
        # block in that sequence; those are future authority blocks.
        seq = getattr(path, "resource_sequence", None) or []
        head_block = None
        if s.occupied_tiles:
            head = s.head_tile
            if head is not None:
                track = s.network.get_track(*head)
                head_block = getattr(track, "block_id", None) if track else None

        # Find head position in sequence; if not found, fall back to checking
        # every block in the sequence (still useful — none should be FREE).
        start_idx = 0
        if head_block is not None:
            for i, entry in enumerate(seq):
                rtype = getattr(entry, "resource_type", None)
                rid = getattr(entry, "resource_id", None)
                if rtype == "block" and rid == head_block:
                    start_idx = i
                    break

        for entry in seq[start_idx:]:
            rtype = getattr(entry, "resource_type", None)
            rid = getattr(entry, "resource_id", None)
            if rtype != "block" or not rid:
                continue
            block = s.network.blocks.get(rid)
            if block is None:
                continue
            owned = block.occupied_by == train_id
            reserved = block.reserved_by_path == s.active_path_id
            assert owned or reserved, (
                f"active path {s.active_path_id!r} has forward block {rid} "
                f"that is neither occupied by {train_id!r} nor reserved by "
                f"the same path "
                f"(occupied_by={block.occupied_by!r}, "
                f"reserved_by_path={block.reserved_by_path!r}, "
                f"state={block.state.value})"
            )

    return Invariant(name="active_path_blocks_held_or_owned", check=check)
