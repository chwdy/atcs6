"""TUNNEL_JOURNEY — verifies a train can traverse a tunnel.

Drives TUNNEL_TRAIN from BLOCK_WEST through the tunnel into BLOCK_EAST.
Asserts the head physically crosses from (2, 0) to (10, 0), the east
block becomes occupied, and travel_direction stays EAST throughout —
i.e., the tunneled edge is never seen as a track-end fallback.
"""

from enums import TrainState

from ..assertions import (
    assert_cab,
    assert_command_inbox_contains,
    assert_eb,
    assert_has_active_path,
    assert_head_at,
    assert_head_in,
    assert_no_active_path,
    assert_route_assigned,
    assert_speed_gt,
    assert_state,
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
    head_visited,
    predicate,
    train_stopped_for,
)


def build_tunnel_journey(
    train_id: str = "TUNNEL_TRAIN",
    east_block_tile: tuple = (10, 0),
    west_block_tile: tuple = (0, 0),
    route_id: str = "TUNNEL_ROUTE",
) -> Journey:
    """Tunnel journey factory. Triggers carry closure state, so always
    build a fresh journey per test invocation."""

    def travel_direction_is_east_while_moving(s):
        if s.state != TrainState.RUNNING_MOVING:
            return
        td = s.controller.travel_direction
        assert td == "E", (
            f"travel_direction={td!r} at tick {s.tick} while RUNNING_MOVING; "
            f"a tunneled edge should not fall back to track-end direction"
        )

    events = [
        Event(
            name="E0_pre_first_tick",
            trigger=at_tick(0),
            assertions=[
                assert_state(TrainState.OFF),
                assert_cab("A"),
                assert_eb(False),
                assert_head_in([west_block_tile]),
                assert_no_active_path(),
                assert_command_inbox_contains("power_on"),
                assert_route_assigned(route_id),
            ],
        ),
        Event(
            name="E1_after_power_on_drained",
            trigger=at_tick(1),
            assertions=[
                assert_state(TrainState.READY),
                assert_cab("A"),
                assert_eb(False),
            ],
        ),
        Event(
            name="E2_path_acquired_through_tunnel",
            trigger=predicate(
                lambda s: s.active_path_id is not None
            ),
            assertions=[
                assert_has_active_path(),
                lambda s: _assert_east_block_is_authority_end(s, east_block_tile),
            ],
        ),
        Event(
            name="E3_train_moving_forward",
            trigger=predicate(lambda s: s.speed_kmh > 0.0),
            assertions=[
                assert_state(TrainState.RUNNING_MOVING),
                assert_speed_gt(0.0),
            ],
        ),
        Event(
            name="E4_head_crossed_tunnel",
            trigger=head_visited(east_block_tile),
            assertions=[
                # The head literally jumped from (2, 0) to (10, 0). If the
                # tunnel fallback in network.get_neighbor wasn't wired
                # correctly, the train would have stopped at (2, 0)'s
                # dead-end safety margin and we'd never see this tile.
                assert_head_at(east_block_tile),
            ],
        ),
        Event(
            name="E5_east_block_entered",
            trigger=block_at_tile_entered(east_block_tile),
            assertions=[
                # Block ownership transitioned through the tunnel.
            ],
        ),
        Event(
            name="E6_train_stopped_at_target",
            trigger=train_stopped_for(settle_ticks=30, after_tick=10),
            assertions=[
                # The train has reached and stopped within the east block.
                lambda s: _assert_head_in_east_block(s, east_block_tile),
            ],
        ),
    ]

    invariants = [
        no_overspeed(),
        no_eb(),
        state_cab_pair_valid(),
        ato_running_implies_running_moving_with_cab(),
        no_pending_path_cancel_request(),
        no_pending_power_off(),
        no_pending_route_abort(),
        Invariant(
            name="travel_direction_east_while_moving",
            check=travel_direction_is_east_while_moving,
        ),
    ]

    return Journey(train_id=train_id, events=events, invariants=invariants)


def _assert_east_block_is_authority_end(s, east_tile):
    east_block_id = s.network.get_track(*east_tile).block_id
    assert s.active_path_authority_end_block_id == east_block_id, (
        f"path authority_end={s.active_path_authority_end_block_id!r}, "
        f"expected east block {east_block_id!r}"
    )


def _assert_head_in_east_block(s, east_tile):
    east_block_id = s.network.get_track(*east_tile).block_id
    head_track = s.network.get_track(*s.head_tile)
    head_block = getattr(head_track, "block_id", None)
    assert head_block == east_block_id, (
        f"train stopped with head in block {head_block!r}, "
        f"expected east block {east_block_id!r}"
    )
