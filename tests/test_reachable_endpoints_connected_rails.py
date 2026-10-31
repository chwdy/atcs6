"""The reachability helpers step only between tiles whose rails connect.

`RailwayPathfinder.get_reachable_endpoints` and
`RailwayPathfinder.get_reachable_endpoints_for_span` walk the layout tile by
tile. Each step must go only where the rails of the two tiles meet, which is
what `RailwayNetwork.get_connection_edges` answers (design_new.md, the
`get_connection_edges` row near "Tunnels"). `RailwayNetwork.get_neighbor` alone
is not enough: it returns whatever tile sits in the next grid cell, whether or
not its rails meet ours, and across a tunnel the far tile's entry edge is not
the cardinal opposite of the exit edge.

demo_6: tile (24, 22) in PLT_YA_1_BLOCK ends in a buffer stop on its north
edge. The tile north of it, (24, 23) in PLT_YA_8_APPROACH, has rails on its N
and W edges only, so the two tiles do not connect, and the planner never routes
PLT_YA_1_BLOCK -> PLT_YA_8_APPROACH across that gap. Neither helper may offer
PLT_YA_8_APPROACH from there.

demo_4: the tunnel (1, 0, W) <-> (11, 10, N) links PLT_S1_S_BLOCK to
PLT_S4_S_BLOCK. Exit edge W and entry edge N are not opposites, so a walk that
assumes the cardinal opposite stops on the far tile instead of carrying on.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime


pytestmark = pytest.mark.scene("demo_6")


def _pathfinder(scene: str):
    with contextlib.redirect_stdout(io.StringIO()):
        _network, dispatcher, _sim, _persistence = bootstrap_runtime(
            demo=True, setup_train=False, scene=scene
        )
    return dispatcher._pathfinder


def test_block_reachability_does_not_cross_buffer_stop():
    pf = _pathfinder("demo_6")
    got = pf.get_reachable_endpoints("PLT_YA_1_BLOCK", include_stations=False)
    assert "PLT_YA_8_APPROACH" not in got
    # The block's other end (the west edge of (23, 21)) still leads on.
    assert got


def test_span_reachability_does_not_cross_buffer_stop():
    """A train on (23, 21)-(23, 22) whose front faces east toward the buffer
    stop has nowhere to go out of that end."""
    pf = _pathfinder("demo_6")
    got = pf.get_reachable_endpoints_for_span(
        [((23, 22), "E")],
        [(23, 21), (23, 22)],
        ["PLT_YA_1_BLOCK"],
        include_stations=False,
    )
    assert got == []


@pytest.mark.scene("demo_4")
def test_block_reachability_crosses_tunnel():
    pf = _pathfinder("demo_4")
    assert "PLT_S4_S_BLOCK" in pf.get_reachable_endpoints(
        "PLT_S1_S_BLOCK", include_stations=False
    )
    assert "PLT_S1_S_BLOCK" in pf.get_reachable_endpoints(
        "PLT_S4_S_BLOCK", include_stations=False
    )


@pytest.mark.scene("demo_4")
def test_span_reachability_continues_through_tunnel():
    """A train on PLT_S1_S_BLOCK whose front faces into the tunnel at
    (1, 0, W) comes out at (11, 10, N) and must be able to drive on past
    PLT_S4_S_BLOCK, not stop on the first tile beyond the tunnel."""
    pf = _pathfinder("demo_4")
    body = list(pf.network.get_block_positions("PLT_S1_S_BLOCK"))
    got = pf.get_reachable_endpoints_for_span(
        [((1, 0), "W")], body, ["PLT_S1_S_BLOCK"], include_stations=False
    )
    assert "PLT_S4_S_BLOCK" in got
    assert "BLK_X2_TO_S4_S" in got
