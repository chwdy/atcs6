"""The path planner never steps between tiles whose rails do not connect.

design_new.md (the `RailwayNetwork.get_connection_edges` rows near
"Tunnels") says traversal helpers use that helper to decide whether two tiles
are track-connected. `RailwayPathfinder._bfs_walks` used to check the
connection only for tiles outside the destination block: a step onto any tile
of the destination block was accepted straight away. So a walk could cross a
buffer stop into a grid neighbour that happens to belong to the destination.

Regression pinned here (demo_6): PLT_YA_1_BLOCK ends at a buffer stop on tile
(24, 22), north edge. The grid cell past it, (24, 23), belongs to
PLT_YA_8_APPROACH, but its rails (an N-W diagonal) do not meet that edge. The
planner returned PLT_YA_1_APPROACH -> PLT_YA_1_BLOCK -> PLT_YA_8_APPROACH,
a route over the buffer stop.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime

pytestmark = pytest.mark.scene("demo_6")

BUFFER_STOP_TILE = (24, 22)
PAST_BUFFER_STOP_TILE = (24, 23)

# Blocks on the yard approach that leads to the buffer stop. Planning from each
# of them to every block must never use the step over the buffer stop.
STARTS = ("PLT_YA_1_APPROACH", "PLT_YA_1_BLOCK", "BLK_58E4DA")


@pytest.fixture(scope="module")
def runtime():
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, _sim, _ = bootstrap_runtime(
            demo=True, setup_train=False, scene="demo_6"
        )
    return network, dispatcher


def _unconnected_steps(network, walk):
    return [
        (tuple(a), tuple(b))
        for a, b in zip(walk.tiles, walk.tiles[1:])
        if network.get_connection_edges(tuple(a), tuple(b)) is None
    ]


def test_scene_has_the_buffer_stop(runtime):
    """Guard the fixture: the two tiles are grid neighbours in different
    blocks, and their rails do not connect."""
    network, _ = runtime
    stop = network.get_track(*BUFFER_STOP_TILE)
    past = network.get_track(*PAST_BUFFER_STOP_TILE)
    assert stop.block_id == "PLT_YA_1_BLOCK"
    assert past.block_id == "PLT_YA_8_APPROACH"
    assert "N" in stop.dead_end_ports
    assert network.get_connection_edges(BUFFER_STOP_TILE, PAST_BUFFER_STOP_TILE) is None


def test_no_walk_over_the_buffer_stop_into_the_destination(runtime):
    network, dispatcher = runtime
    walks = dispatcher.find_paths("PLT_YA_1_APPROACH", "PLT_YA_8_APPROACH")
    for walk in walks:
        assert walk.blocks[-2:] != ["PLT_YA_1_BLOCK", "PLT_YA_8_APPROACH"], (
            f"walk {walk.blocks} enters PLT_YA_8_APPROACH from PLT_YA_1_BLOCK "
            "across the buffer stop at (24, 22)"
        )
        assert not _unconnected_steps(network, walk), walk.blocks


@pytest.mark.parametrize("start", STARTS)
def test_every_planned_walk_steps_only_between_connected_tiles(runtime, start):
    network, dispatcher = runtime
    bad = []
    for end in sorted(network.blocks):
        if end == start:
            continue
        for walk in dispatcher.find_paths(start, end):
            steps = _unconnected_steps(network, walk)
            if steps:
                bad.append((end, walk.blocks, steps))
    assert not bad, (
        f"walks from {start} step between tiles whose rails do not connect: "
        f"{bad[:5]}"
    )
