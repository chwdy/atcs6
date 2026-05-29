"""Tunnel-emerging train must clear the full destination block.

Regression test for two interlocking bugs the demo_4 S-track tunnel
exposed:

  1. `convert_tiles_to_train_path` and `_get_travel_direction` used
     `(dx, dy)` between consecutive path tiles to infer the cardinal
     direction. For a tunnel hop the delta is meaningless, so a SOUTH-
     bound train emerging from a `(1,0,W) ↔ (11,10,N)` tunnel was
     reported as NORTH.
  2. `_bfs_walks_with_switch_mode`'s "extend through the destination
     block" loop used `OPPOSITE_EDGE[exit]` to derive the entry edge on
     the next tile. Across a W↔N tunnel the actual entry ("N") is not
     the opposite of the previous exit ("W"), so the loop broke
     immediately. The resulting path covered only the entry tile of the
     destination block, lookahead reported a one-tile clearance, and
     the train braked to a stop near the tunnel mouth.

Both fixes route the per-hop edge math through
`RailwayNetwork.get_connection_edges`, which knows about tunnels.

Three playback speeds: a bug that only shows at 3x is still a bug.
"""

import pytest

from tests.journey.harness import JourneyHarness
from tests.journey.events import Event, Journey
from tests.journey.assertions import assert_head_at
from tests.journey.triggers import head_visited, train_stopped_for


def _build_wn_journey():
    """Two checkpoints:

    1. head_visited((11, 10)) — proves the head crossed the tunnel at all.
       Even pre-fix the train still reached (11, 10) on its first
       movement (the head was already at the tunnel mouth), so this
       alone doesn't detect the bug.
    2. train_stopped_for at the far end of the destination block —
       pre-fix the train stalled near (11, 10) because the path covered
       only the entry tile, so this is the regression guard.
    """
    return Journey(
        train_id="WN_TRAIN",
        events=[
            Event(
                name="E1_crossed_tunnel",
                trigger=head_visited((11, 10)),
                assertions=[assert_head_at((11, 10))],
            ),
            Event(
                name="E2_stopped_at_far_end_of_destination_block",
                trigger=train_stopped_for(settle_ticks=40, after_tick=10),
                assertions=[assert_head_at((11, 8))],
            ),
        ],
        invariants=[],
    )


@pytest.mark.parametrize(
    "dt,label",
    [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")],
)
def test_train_clears_full_destination_block_past_wn_tunnel(dt, label):
    from tests.journey.scenes.tunnel_wn_scene import TunnelWNScene

    base_max_ticks_at_0_1 = 1500
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    harness = JourneyHarness(
        scene=TunnelWNScene(),
        journey=_build_wn_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
