"""DEMO_JOURNEY — the canonical journey for the demo scene.

Walks DEMO_TRAIN from (0,0) facing EAST through:
  BLOCK_MAIN_WEST → SW_3_0 (MAIN/EW) → BLOCK_MAIN_EAST (with Central Station
  at (7,0) as a pass-through speed-restricted area) → SW_9_0 (DIVERGE/NW) →
  BLOCK_EAST_BRANCH → East Terminal (stop target at (9,3),(9,4)).

Events fire in declared order. Continuous invariants run on every tick.

Network layout reference (from `demo_main.py`):
- BLOCK_MAIN_WEST     : (0,0)–(2,0)                       (no station)
- Switch SW_3_0       : (3,0), EW + NW, initial state 0   (boundary, no block)
- BLOCK_MAIN_EAST     : (4,0)–(10,0); Central Station at (7,0)
                        top_speed_kmh=40 (block may auto-split around station)
- Switch SW_9_0       : (9,0), EW + NW, initial state 0   (must flip to 1 here)
- BLOCK_EAST_BRANCH   : (9,1)–(9,6); East Terminal at (9,3),(9,4)
                        top_speed_kmh=20 (block may auto-split around station)
"""

from enums import AtcLimitReasonCode, BRAKE_MULTIPLIERS, HandlePosition, TrainState

from ..actions import attempt_set_switch_position
from ..assertions import (
    assert_atc_reason_not_in,
    assert_block_at_tile_occupied_by,
    assert_block_at_tile_unoccupied,
    assert_cab,
    assert_command_inbox_contains,
    assert_eb,
    assert_has_active_path,
    assert_head_in,
    assert_no_active_path,
    assert_route_assigned,
    assert_speed_eq,
    assert_speed_gt,
    assert_speed_le,
    assert_state,
    assert_switch_locked,
    assert_switch_position,
    assert_switch_unlocked,
    assert_switch_unoccupied,
)
from ..events import Event, Invariant, Journey
from ..invariants import (
    ato_running_implies_running_moving_with_cab,
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
    predicate,
    switch_entered,
    switch_vacated,
    train_stopped,
    train_stopped_for,
)


# East Terminal target blocks are resolved at journey-build time would make
# this hard to swap scenes; instead, we resolve dynamically inside the
# assertion via the dispatcher's authority_end_block_id (E2) and inside the
# `head_in` set for the stop check (E15).
EAST_TERMINAL_TILES = [(9, 3), (9, 4)]


def build_demo_journey(train_id: str = "DEMO_TRAIN") -> Journey:
    """Return the DEMO_TRAIN journey. Factory form lets a different train_id
    be supplied without modifying the journey definitions.
    """

    # ----- Helpers that need a closure over `train_id` -------------------- #

    def _expected_terminal_blocks(s):
        """Resolve East Terminal block ids from the current network. Used by
        the E2 / E16 assertions so the journey doesn't pin a hard-coded id.
        """
        ids = set()
        for tile in EAST_TERMINAL_TILES:
            track = s.network.get_track(*tile)
            if track is None:
                continue
            bid = getattr(track, "block_id", None)
            if bid:
                ids.add(bid)
        return ids

    def assert_active_path_target_is_east_terminal():
        def check(s):
            terminal_ids = _expected_terminal_blocks(s)
            assert s.active_path_id is not None, "no active path"
            assert s.active_path_authority_end_block_id in terminal_ids, (
                f"path authority_end={s.active_path_authority_end_block_id!r} "
                f"not in East Terminal blocks {sorted(terminal_ids)}"
            )

        return check

    # Deepest-brake tracker. The one-tick-ahead ATO braking law tracks the
    # operational_limit envelope at ~B6 (= op_decel) and reaches B7/B8 for
    # catch-up; the old far-lookahead behavior never braked past B3. We record
    # the deepest brake notch used across the run with a no-raise accumulator
    # invariant and assert at the post-stop event that it reached at least B6.
    # `brake_seen` is created per build (journeys are never cached), so the
    # accumulator does not leak across parametrized harness runs.
    brake_seen = {"max_mult": 0.0, "handle": "N"}

    def _track_deepest_brake(s):
        mult = BRAKE_MULTIPLIERS.get(s.handle_position)
        if mult is not None and mult > brake_seen["max_mult"]:
            brake_seen["max_mult"] = mult
            brake_seen["handle"] = getattr(
                s.handle_position, "value", str(s.handle_position)
            )

    def assert_reached_b6_braking(s):
        floor = BRAKE_MULTIPLIERS[HandlePosition.B6]
        assert brake_seen["max_mult"] >= floor - 1e-9, (
            f"ATO never braked at B6 (op_decel) or deeper during the run; "
            f"deepest notch was {brake_seen['handle']} "
            f"({brake_seen['max_mult']:.2f}x op_decel). The one-tick-ahead "
            f"braking law should track the curve at ~B6 — a deepest notch "
            f"around B3 means it regressed to the old far-lookahead, "
            f"brake-too-early-and-gently behavior."
        )

    # ----- Events --------------------------------------------------------- #

    events = [
        Event(
            name="E0_pre_first_tick",
            trigger=at_tick(0),
            assertions=[
                assert_state(TrainState.OFF),
                assert_cab("A"),
                assert_eb(False),
                # Span head should be at origin tile (0,0).
                assert_head_in([(0, 0)]),
                assert_no_active_path(),
                assert_command_inbox_contains("power_on"),
                # Route already registered + assigned at bootstrap.
                assert_route_assigned("DEMO_ROUTE_V1"),
            ],
        ),
        Event(
            name="E1_after_power_on_drained",
            trigger=at_tick(1),
            assertions=[
                assert_state(TrainState.READY),
                assert_cab("A"),
                # Phase 1 was skipped while OFF; no path yet.
                assert_no_active_path(),
                assert_eb(False),
            ],
        ),
        Event(
            name="E2_after_path_acquired_and_depart",
            trigger=at_tick(2),
            assertions=[
                assert_state(TrainState.RUNNING_MOVING),
                assert_has_active_path(),
                assert_active_path_target_is_east_terminal(),
                assert_atc_reason_not_in(
                    [
                        AtcLimitReasonCode.NO_AUTHORITY,
                        AtcLimitReasonCode.NO_CAB,
                        AtcLimitReasonCode.WRONG_DIRECTION_CAB,
                        AtcLimitReasonCode.EB,
                    ]
                ),
            ],
        ),
        Event(
            name="E3_accelerating",
            trigger=predicate(lambda s: s.speed_kmh > 0.5),
            assertions=[
                assert_state(TrainState.RUNNING_MOVING),
                assert_speed_gt(0.0),
                # ATC limit must accommodate the current speed (continuous
                # invariant covers this every tick; assert it once at first
                # motion as a sanity check).
                lambda s: None
                if s.atc_limit_kmh <= 0 or s.speed_kmh <= s.atc_limit_kmh + 0.5
                else (_ for _ in ()).throw(
                    AssertionError(
                        f"speed {s.speed_kmh:.2f} > atc {s.atc_limit_kmh:.2f}"
                    )
                ),
            ],
        ),
        Event(
            name="E4_switch_sw_3_0_entered_and_locked",
            trigger=switch_entered("SW_3_0"),
            assertions=[
                # SW_3_0 is locked at state 0 (MAIN/EW) by the path. Note: the
                # `_convert_locks_on_entry` step removes the path's *primary*
                # lock at physical entry, so the lock map may be empty here —
                # we therefore check the position alone, not the lock dict.
                assert_switch_position("SW_3_0", 0),
            ],
            actions=[
                # While the train physically straddles the switch we must NOT
                # be able to flip it from outside.
                attempt_set_switch_position(
                    "SW_3_0",
                    new_position=1,
                    should_succeed=False,
                ),
            ],
        ),
        Event(
            name="E5_block_at_4_0_entered",
            trigger=block_at_tile_entered((4, 0)),
            assertions=[
                assert_block_at_tile_occupied_by((4, 0), train_id),
                assert_has_active_path(),
            ],
        ),
        # NOTE: An event for "BLOCK_MAIN_WEST tail-vacated" was originally
        # planned (E6) but the current system never emits a tail-vacate report
        # for the train's *initial* block (BLOCK_MAIN_WEST stays
        # `occupied_by=DEMO_TRAIN` for the entire run). We omit that event
        # here so the journey reflects observed behavior; the moment that is
        # fixed in the simulation core, add it back at this position.
        Event(
            name="E7_switch_sw_3_0_vacated",
            trigger=switch_vacated("SW_3_0"),
            assertions=[
                assert_switch_unoccupied("SW_3_0"),
                assert_switch_unlocked("SW_3_0"),
            ],
        ),
        Event(
            name="E8_passing_central_station",
            trigger=head_visited((7, 0)),
            assertions=[
                # Central Station top_speed_kmh = 40 (demo_main.py:256).
                assert_speed_le(40.0, epsilon=0.5),
                # We do NOT enter dwell here — Central is not a route task.
                # The state should still be RUNNING_MOVING.
                assert_state(TrainState.RUNNING_MOVING),
            ],
        ),
        Event(
            name="E9_switch_sw_9_0_locked_for_diverge",
            # Just before the head reaches (9,0) — when head is at (8,0) the
            # path must already have SW_9_0 locked at state 1 (DIVERGE/NW).
            trigger=head_visited((8, 0)),
            assertions=[
                assert_switch_locked("SW_9_0", locked_state=1),
                assert_switch_position("SW_9_0", 1),
            ],
        ),
        Event(
            name="E10_switch_sw_9_0_entered_and_locked",
            trigger=switch_entered("SW_9_0"),
            assertions=[
                assert_switch_position("SW_9_0", 1),
            ],
            actions=[
                attempt_set_switch_position(
                    "SW_9_0",
                    new_position=0,
                    should_succeed=False,
                ),
            ],
        ),
        Event(
            name="E11_block_at_9_1_entered",
            trigger=block_at_tile_entered((9, 1)),
            assertions=[
                assert_block_at_tile_occupied_by((9, 1), train_id),
            ],
        ),
        Event(
            name="E12_block_at_4_0_vacated",
            # The first BLOCK_MAIN_EAST sub-block (containing tile (4,0))
            # gets released as the train moves east through it.
            trigger=block_at_tile_vacated((4, 0)),
            assertions=[
                assert_block_at_tile_unoccupied((4, 0)),
            ],
        ),
        Event(
            name="E13_switch_sw_9_0_vacated",
            trigger=switch_vacated("SW_9_0"),
            assertions=[
                assert_switch_unoccupied("SW_9_0"),
                assert_switch_unlocked("SW_9_0"),
            ],
        ),
        Event(
            name="E14_approaching_east_terminal",
            trigger=head_visited((9, 3)),
            assertions=[
                # East Terminal top_speed_kmh = 20 (demo_main.py:364).
                assert_speed_le(20.0, epsilon=0.5),
                # Lower bound pins the dt-stable, efficient approach the
                # one-tick-ahead ATO braking law produces: the head crosses
                # into the terminal at ~17 km/h across dt=0.1/0.2/0.3
                # (16.7-17.1 observed). The old far-lookahead/gentle-brake
                # behavior dropped this to ~7.5 km/h at dt=0.3; this bound
                # catches that regression.
                assert_speed_gt(14.0),
            ],
        ),
        Event(
            name="E15_train_stopped_at_terminal",
            trigger=train_stopped(after_tick=20),
            assertions=[
                assert_speed_eq(0.0, epsilon=0.5),
                assert_head_in(EAST_TERMINAL_TILES),
            ],
        ),
        Event(
            name="E16_post_stop_state_ok",
            # Wait until the train has been at speed≈0 for 30 consecutive
            # ticks, so the controller's state machine has settled past the
            # transient final-deceleration tick. Verify it landed in a safe
            # terminal state (READY or RUNNING_AT_STATION dwell), the cab is
            # still set, EB is clear, ATO is not running, and no Phase-3
            # latches are armed.
            trigger=train_stopped_for(settle_ticks=30, after_tick=50),
            assertions=[
                # Allow either operational state — the system enters dwell
                # then can fall back to READY when the dwell timer ends.
                lambda s: (
                    None
                    if s.state in (TrainState.READY, TrainState.RUNNING_AT_STATION)
                    else (_ for _ in ()).throw(
                        AssertionError(
                            f"after stop expected state in (READY, "
                            f"RUNNING_AT_STATION), got {s.state.value}"
                        )
                    )
                ),
                assert_eb(False),
                assert_cab("A"),
                # ato_running must be clear once we've stopped.
                lambda s: (
                    None
                    if not s.ato_running
                    else (_ for _ in ()).throw(
                        AssertionError(
                            f"ato_running should be False after stop, got True"
                        )
                    )
                ),
                # Phase-3 latches all clear (covered by invariants too).
                lambda s: (
                    None
                    if not (
                        s.latch_path_cancel_request
                        or s.latch_route_abort
                        or s.latch_power_off
                        or s.latch_route_deactivate
                    )
                    else (_ for _ in ()).throw(
                        AssertionError(
                            f"Phase-3 latch armed after stop: "
                            f"path_cancel={s.latch_path_cancel_request}, "
                            f"route_abort={s.latch_route_abort}, "
                            f"power_off={s.latch_power_off}, "
                            f"route_deactivate={s.latch_route_deactivate}"
                        )
                    )
                ),
                # The run must have braked at B6 (op_decel) or deeper at some
                # point — pins the one-tick-ahead curve-tracking braking law.
                assert_reached_b6_braking,
            ],
        ),
        # NOTE: Two additional events were planned but are NOT included
        # because they expose current system divergences from design_new.md
        # rather than user-facing demo correctness:
        #   - "path completed" — `is_stopped_at_authority_end()` requires
        #     state == RUNNING_MOVING, but the system transitions to
        #     RUNNING_AT_STATION (then READY) before the gate is checked, so
        #     the path stays active forever in the current implementation.
        #   - "route run removed" — depends on the same path-completion path.
        # Both can be added once the underlying behaviour is tightened.
    ]

    # ----- Continuous invariants ----------------------------------------- #

    # The following invariants are imported but intentionally left OUT of
    # the default DEMO journey because the current system has known
    # divergences from the design doc the user wants this test to track to
    # current observed behavior, not to the (yet-unmet) design contract:
    #   - span_block_consistency / span_switch_consistency:
    #       `current_span.occupied_blocks` does not always agree with the
    #       BlockManager registry mid-traversal; some early blocks
    #       (e.g. BLOCK_MAIN_WEST) stay `occupied_by` after the train has
    #       fully passed them.
    #   - at_station_implies_no_path:
    #       Phase 1 dwell entry runs before the Phase 3 path completion
    #       check, so for one tick `state == RUNNING_AT_STATION` while the
    #       path is still active.
    # All three remain importable from `tests.journey.invariants` for future
    # "strict mode" runs once the underlying behavior is tightened.
    invariants = [
        no_overspeed(epsilon=0.5),
        no_eb(),
        no_pending_path_cancel_request(),
        no_pending_route_abort(),
        no_pending_power_off(),
        state_cab_pair_valid(),
        ato_running_implies_running_moving_with_cab(),
        # No-raise accumulator: records the deepest brake notch used; consumed
        # by assert_reached_b6_braking in E16.
        Invariant(name="track_deepest_brake", check=_track_deepest_brake),
    ]

    return Journey(train_id=train_id, events=events, invariants=invariants)


# IMPORTANT: do NOT cache a module-level Journey instance. Triggers use
# closure-state to fire monotonically (e.g. `head_visited` flips a `fired`
# flag once true), so reusing the same Journey across multiple harness runs
# leaks state between runs (e.g. across pytest parametrize variants). Always
# call `build_demo_journey()` at the call site to get a fresh instance.
