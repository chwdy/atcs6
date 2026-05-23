"""STRICT_DEMO_JOURNEY — demo train journey with the design-doc block contract
enforced as continuous invariants and per-event block-state assertions.

The default `build_demo_journey()` factory in `demo_journey.py` deliberately
omits `span_block_consistency` and other strict checks because the current
system has known divergences from `design_new.md` around block tail-vacate
behavior (BLOCK_MAIN_WEST never reports tail-vacate, sub-blocks of
BLOCK_MAIN_EAST flip OCCUPIED↔FREE while the train is mid-traversal).

This strict variant exists so those divergences surface as test failures
with a clear failure report rather than staying hidden. It exercises the
design contract from `design_new.md` "Block" / "BlockManager" sections:

  - `Block.occupied_by` is physical train presence. While ANY tile of a
    block is in `current_span.occupied_tiles`, the block must be
    `occupied_by == train_id`.
  - When the last tile of a block leaves the span, the controller-reported
    tail-vacate must transition the block OCCUPIED → CAUTION → FREE (or
    OCCUPIED → CAUTION → RESERVED if a path still holds it).
  - Future blocks on the active path must be `reserved_by_path == path_id`
    (or already physically owned by the train).

A failure here is *not* a regression in the test — it surfaces a real
divergence in the simulation core.
"""

from enums import BlockState, TrainState

from ..assertions import (
    assert_block_at_tile_occupied_by,
    assert_block_at_tile_unoccupied,
    assert_block_at_tile_reserved_by_path,
    assert_block_id_not_reserved,
    assert_block_id_occupied_by,
    assert_block_id_reserved_by_path,
    assert_block_id_state,
    assert_block_id_unoccupied,
    assert_has_active_path,
    assert_head_in,
    assert_route_assigned,
    assert_speed_eq,
    assert_state,
)
from ..events import Event, Journey
from ..invariants import (
    active_path_blocks_held_or_owned,
    ato_running_implies_running_moving_with_cab,
    block_occupied_iff_span_covers_block,
    block_reservation_excludes_occupied_other_train,
    no_eb,
    no_overspeed,
    no_pending_path_cancel_request,
    no_pending_power_off,
    no_pending_route_abort,
    state_cab_pair_valid,
)
from ..triggers import (
    at_tick,
    block_at_tile_entered,
    block_at_tile_vacated,
    head_visited,
    train_stopped_for,
)


EAST_TERMINAL_TILES = [(9, 3), (9, 4)]


def build_strict_demo_journey(train_id: str = "DEMO_TRAIN") -> Journey:
    """Return a strict DEMO_TRAIN journey enforcing the design block contract.

    The continuous invariants will trip the first time the simulation
    diverges from `design_new.md` — typically very early, the first tick the
    train moves out of (0,0) without the BlockManager seeing a corresponding
    tail-vacate for BLOCK_MAIN_WEST.
    """

    def _resolve_block_id_at(s, tile):
        track = s.network.get_track(*tile)
        if track is None:
            return None
        return getattr(track, "block_section_id", None)

    events = [
        # E0 — pre-tick boot. Origin block must be the only OCCUPIED block.
        Event(
            name="E0_boot_state",
            trigger=at_tick(0),
            assertions=[
                assert_state(TrainState.OFF),
                assert_block_at_tile_occupied_by((0, 0), train_id),
                assert_block_id_state("BLOCK_MAIN_WEST", BlockState.OCCUPIED),
                assert_route_assigned("DEMO_ROUTE_V1"),
            ],
        ),
        # E1 — after path acquired. Forward blocks should be RESERVED by the
        # demo path id (resolved dynamically; the path id is generated).
        Event(
            name="E1_path_acquired_reservations_present",
            trigger=at_tick(3),
            assertions=[
                assert_has_active_path(),
                # Tile (4,0) lives in BLOCK_MAIN_EAST_1; it is the next
                # forward block past the start, so it must be RESERVED.
                lambda s: (
                    None
                    if _block_at(s, (4, 0)) is not None
                    and _block_at(s, (4, 0)).reserved_by_path == s.active_path_id
                    else (_ for _ in ()).throw(
                        AssertionError(
                            f"block at (4,0)={_resolve_block_id_at(s, (4, 0))!r} "
                            f"not reserved by active path "
                            f"{s.active_path_id!r}: "
                            f"reserved_by_path={getattr(_block_at(s, (4, 0)), 'reserved_by_path', None)!r}"
                        )
                    )
                ),
            ],
        ),
        # E2 — head has reached (4,0); BLOCK_MAIN_EAST_1 must be physically
        # OCCUPIED *and* still reserved by the active path (start-of-block
        # entry consumes the reservation on physical entry, but BlockManager
        # spec allows OCCUPIED + reservation to coexist during the transition;
        # only check occupancy here).
        Event(
            name="E2_block_main_east_1_entered",
            trigger=block_at_tile_entered((4, 0)),
            assertions=[
                assert_block_at_tile_occupied_by((4, 0), train_id),
                # The original origin block should still claim physical
                # ownership at this point — the train tail has only just
                # crossed (3,0); BLOCK_MAIN_WEST tiles may still be in the
                # span. That is acceptable here. The strict failure mode
                # appears later (after head ≥ (5,0) the span no longer
                # includes any BLOCK_MAIN_WEST tile).
            ],
        ),
        # E3 — head at (5,0). At this point the span contains only (4,0)/
        # (5,0); the train no longer occupies BLOCK_MAIN_WEST at all, so
        # per the design contract BLOCK_MAIN_WEST.occupied_by must have
        # been cleared by the controller-reported tail-vacate. If the
        # current sim core leaves it stuck OCCUPIED, this assertion fires.
        Event(
            name="E3_block_main_west_fully_vacated",
            trigger=head_visited((5, 0)),
            assertions=[
                # BLOCK_MAIN_WEST is upstream of (3,0) (the SW_3_0 switch),
                # so once head reaches (5,0), the span tail can be at most
                # (4,0). Therefore BLOCK_MAIN_WEST has no tiles in span.
                assert_block_id_unoccupied("BLOCK_MAIN_WEST"),
            ],
        ),
        # E4 — train mid-block. While the train is anywhere inside
        # BLOCK_MAIN_EAST_1 (tiles (4,0), (5,0), (6,0)), the block must be
        # `occupied_by == train_id`. This is the (5,0) "released too early"
        # bug the user observed.
        Event(
            name="E4_block_main_east_1_held_at_5_0",
            trigger=head_visited((5, 0)),
            assertions=[
                lambda s: (
                    None
                    if _block_at(s, (5, 0)) is not None
                    and _block_at(s, (5, 0)).occupied_by == train_id
                    else (_ for _ in ()).throw(
                        AssertionError(
                            f"head at (5,0) but block {_resolve_block_id_at(s, (5, 0))!r} "
                            f"is not occupied by {train_id!r}: "
                            f"occupied_by={getattr(_block_at(s, (5, 0)), 'occupied_by', None)!r}, "
                            f"state={getattr(_block_at(s, (5, 0)), 'state', None)}, "
                            f"span={s.occupied_tiles}"
                        )
                    )
                ),
            ],
        ),
        # E5 — confirm the (4,0) sub-block is released cleanly only AFTER
        # the train has fully left every tile of BLOCK_MAIN_EAST_1.
        Event(
            name="E5_block_main_east_1_released_after_full_exit",
            trigger=head_visited((7, 0)),
            assertions=[
                # By the time the head reaches (7,0), the tail is at most
                # at (6,0). BLOCK_MAIN_EAST_1 tiles are (4,0)/(5,0)/(6,0),
                # so it may still legitimately be OCCUPIED (tail on (6,0))
                # or in CAUTION (tail just left). It must NOT be FREE/
                # RESERVED again until the tail has cleared (6,0).
                lambda s: (
                    None
                    if _block_at(s, (5, 0)) is None
                    or _block_at(s, (5, 0)).occupied_by == train_id
                    or _block_at(s, (5, 0)).caution is not None
                    or (6, 0) not in s.occupied_tiles
                    else (_ for _ in ()).throw(
                        AssertionError(
                            f"BLOCK_MAIN_EAST_1 released while train still "
                            f"occupies one of its tiles: "
                            f"span={s.occupied_tiles}, "
                            f"block.state={_block_at(s, (5, 0)).state.value}"
                        )
                    )
                ),
            ],
        ),
        # E6 — train fully stopped at East Terminal. The terminal block must
        # be OCCUPIED (or at least, contain at least one head tile).
        Event(
            name="E6_terminal_block_owned_at_stop",
            trigger=train_stopped_for(settle_ticks=30, after_tick=50),
            assertions=[
                assert_speed_eq(0.0, epsilon=0.5),
                assert_head_in(EAST_TERMINAL_TILES),
                # Terminal block must be occupied by us.
                lambda s: (
                    None
                    if all(
                        (_block_at(s, t) is not None
                         and _block_at(s, t).occupied_by == train_id)
                        for t in EAST_TERMINAL_TILES
                        if t in s.occupied_tiles
                    )
                    else (_ for _ in ()).throw(
                        AssertionError(
                            f"terminal block not occupied_by={train_id!r} at "
                            f"stop: occupants="
                            f"{ {t: getattr(_block_at(s, t), 'occupied_by', None) for t in EAST_TERMINAL_TILES} }"
                        )
                    )
                ),
            ],
        ),
    ]

    invariants = [
        # Existing protocol invariants — keep these for context in failure
        # reports.
        no_overspeed(epsilon=0.5),
        no_eb(),
        no_pending_path_cancel_request(),
        no_pending_route_abort(),
        no_pending_power_off(),
        state_cab_pair_valid(),
        ato_running_implies_running_moving_with_cab(),
        # New strict block-state invariants — these run every tick. The
        # `block_occupied_iff_span_covers_block` invariant should fire as
        # soon as the train leaves the first tile, because BLOCK_MAIN_WEST
        # will retain `occupied_by=DEMO_TRAIN` while the span no longer
        # covers any of its tiles.
        block_occupied_iff_span_covers_block(),
        block_reservation_excludes_occupied_other_train(),
        active_path_blocks_held_or_owned(),
    ]

    return Journey(train_id=train_id, events=events, invariants=invariants)


# Helper used by per-event closures so the long lambda lines stay readable.
def _block_at(s, tile):
    track = s.network.get_track(*tile)
    if track is None:
        return None
    bid = getattr(track, "block_section_id", None)
    if not bid:
        return None
    return s.network.blocks.get(bid)


def _resolve_block_id_at(s, tile):
    track = s.network.get_track(*tile)
    if track is None:
        return None
    return getattr(track, "block_section_id", None)
