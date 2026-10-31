"""The extend-path tool's reachability walk steps only between tiles whose
rails connect.

`PathDispatcher.get_reachable_endpoints_for_extension` lists the blocks a path
can be extended to by walking forward tile by tile from the path's last tile.
Each step must go only where the rails of the two tiles meet, which is what
`RailwayNetwork.get_connection_edges` answers (design_new.md, the
`get_connection_edges` row near "Tunnels"). `RailwayNetwork.get_neighbor` alone
is not enough: it returns whatever tile sits in the next grid cell, whether or
not its rails meet ours, and across a tunnel the far tile's entry edge is not
the cardinal opposite of the exit edge.

demo_6: tile (24, 22) in PLT_YA_1_BLOCK ends in a buffer stop on its north
edge. The tile north of it, (24, 23) in PLT_YA_8_APPROACH, has rails on its N
and W edges only, so the two tiles do not connect. A path whose last tile faces
north out of (24, 22) has nowhere to be extended to.

demo_4: the tunnel (1, 0, W) <-> (11, 10, N) links PLT_S1_S_BLOCK to
PLT_S4_S_BLOCK. Exit edge W and entry edge N are not opposites, so a walk that
assumes the cardinal opposite stops on the far tile instead of carrying on.

Each test hands the dispatcher a path record built from the block's own tiles.
No block is reserved for it, so the walk treats every block as a possible
destination; the assertions only name blocks beyond the path's end.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime
from enums import PathStatus
from path import Path, PathTileEntry, ResourceSequenceEntry


pytestmark = pytest.mark.scene("demo_6")


def _extension_endpoints(scene: str, block_id: str, steps):
    """Register a one-block path over `steps` — (x, y, entry_edge, exit_edge)
    per tile, in travel order — and return its extension endpoints."""
    with contextlib.redirect_stdout(io.StringIO()):
        _network, dispatcher, _sim, _persistence = bootstrap_runtime(
            demo=True, setup_train=False, scene=scene
        )
    tiles = [PathTileEntry(x, y, entry, exit_, 0) for (x, y, entry, exit_) in steps]
    path = Path(
        path_id="P_EXTEND_TEST",
        status=PathStatus.ACTIVE,
        train_id=None,
        resource_sequence=[
            ResourceSequenceEntry(
                0, "block", block_id, tiles, steps[0][2], steps[-1][3]
            )
        ],
    )
    dispatcher.paths[path.path_id] = path
    endpoints, error = dispatcher.get_reachable_endpoints_for_extension(
        path.path_id, include_stations=False
    )
    assert error is None
    return endpoints


def test_extension_does_not_cross_buffer_stop():
    """A path ending on (24, 22) facing north runs into the buffer stop."""
    got = _extension_endpoints(
        "demo_6",
        "PLT_YA_1_BLOCK",
        [(23, 21, "W", "N"), (23, 22, "S", "E"), (24, 22, "W", "N")],
    )
    assert "PLT_YA_8_APPROACH" not in got
    assert got == []


def test_extension_away_from_buffer_stop_still_leads_on():
    """The same block driven the other way leaves through the west edge of
    (23, 21), which does connect onward."""
    got = _extension_endpoints(
        "demo_6",
        "PLT_YA_1_BLOCK",
        [(24, 22, "N", "W"), (23, 22, "E", "S"), (23, 21, "N", "W")],
    )
    assert got


@pytest.mark.scene("demo_4")
def test_extension_continues_through_tunnel_west_to_north():
    """A path on PLT_S1_S_BLOCK whose last tile faces into the tunnel at
    (1, 0, W) comes out at (11, 10, N) and carries on past PLT_S4_S_BLOCK."""
    got = _extension_endpoints(
        "demo_4",
        "PLT_S1_S_BLOCK",
        [(2, 1, "N", "W"), (1, 1, "E", "S"), (1, 0, "N", "W")],
    )
    assert "PLT_S4_S_BLOCK" in got
    assert "BLK_X2_TO_S4_S" in got


@pytest.mark.scene("demo_4")
def test_extension_continues_through_tunnel_north_to_west():
    """The other way: a path on PLT_S4_S_BLOCK whose last tile faces into the
    tunnel at (11, 10, N) comes out at (1, 0, W) and carries on past
    PLT_S1_S_BLOCK."""
    got = _extension_endpoints(
        "demo_4",
        "PLT_S4_S_BLOCK",
        [(10, 9, "W", "N"), (10, 10, "S", "E"), (11, 10, "W", "N")],
    )
    assert "PLT_S1_S_BLOCK" in got
    assert "BLK_S1_S_TO_X1" in got
