"""Unit tests for the topology-only pathfinding-walk cache on RailwayNetwork.

``RailwayPathfinder.query_graph_walks`` caches its raw BFS walk enumeration on
the owning network. That enumeration is a pure function of the static graph --
the BFS ignores switch position and never reads dynamic block/switch
availability -- so the same (start, end) yields the same walks until the
topology changes. The per-tick create_path/extend_path storm reuses one BFS per
block pair instead of re-walking the graph on every call.

These tests pin the two properties the cache must hold:

  1. a query populates the cache and a warm (cached) query returns exactly what
     a cold (just-invalidated) query returns -- caching must not change the
     answer;
  2. every topology mutator drops the cache, so a query issued after an edit
     can never observe a stale walk.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime


pytestmark = pytest.mark.scene("demo_5")


def _bootstrap():
    with contextlib.redirect_stdout(io.StringIO()):
        network, _dispatcher, sim, _persistence = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_5"
        )
    return network, sim


def _connected_pair(network, pf):
    """First (start, end) block pair that has at least one walk."""
    blocks = list(network.blocks.keys())
    for a in blocks:
        for b in blocks:
            if a != b and pf.find_ordered_walk(a, b):
                return a, b
    raise AssertionError(
        "demo_5 contains no connected block pair; the cache test fixture no "
        "longer exercises pathfinding"
    )


def _block_lists(candidates):
    # `.walk` (a PathSegment) carries the ordered block sequence the dispatcher
    # consumes when building resource_sequence.
    return [c.walk.blocks for c in candidates]


def _ordered_walk_sig(candidates):
    # Identity of the per-step `ordered_walk` geometry: this is the cached,
    # statically-derived payload that must be byte-stable across warm/cold.
    return [
        [
            (s.resource_type, s.resource_id, s.tile, s.entry_edge, s.exit_edge,
             s.block_direction, s.switch_required_state)
            for s in (c.ordered_walk or [])
        ]
        for c in candidates
    ]


def test_walk_cache_populates_and_warm_matches_cold():
    network, sim = _bootstrap()
    pf = sim.path_dispatcher._pathfinder
    a, b = _connected_pair(network, pf)

    # Cold: force a miss, then the query must compute and store the candidates.
    network.invalidate_pathfinding_cache()
    assert (a, b) not in network._walk_cache
    cold = pf.find_ordered_walk(a, b)
    assert (a, b) in network._walk_cache, "query did not populate the cache"
    assert cold, "expected at least one walk for a connected pair"
    assert all(c.ordered_walk for c in cold), "ordered_walk must be populated"

    # Warm: served from the cache -- and identical to the cold computation,
    # including the per-step ordered_walk geometry (caching must not change it).
    warm = pf.find_ordered_walk(a, b)
    assert _block_lists(warm) == _block_lists(cold)
    assert _ordered_walk_sig(warm) == _ordered_walk_sig(cold)


# --- Each topology mutator must drop the cache. None of these run in the sim
#     tick loop, so clearing here never fights the per-tick reuse. -------------

def _mutate_direct_invalidate(network):
    network.invalidate_pathfinding_cache()


def _mutate_block_membership(network):
    # Re-assign one tile to its current block: a no-op *value* change that still
    # routes through the segment_id setter -> _set_tile_segment -> clear.
    _tile, track = next(iter(network.tracks.items()))
    track.segment_id = track.block_id


def _mutate_propagate_from_tile(network):
    tile = next(iter(network.tracks.keys()))
    network.propagate_from_tile(*tile)


def _mutate_remove_switch(network):
    network.remove_switch(next(iter(network.switches.keys())))


def _mutate_rebuild_ordered_tiles(network):
    # ordered_tiles feeds the cached per-step block_direction, so recomputing it
    # must drop the cache.
    bid = next(iter(network.blocks))
    network._get_segment_geometry_manager().rebuild_segment_ordered_tiles(bid)


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_direct_invalidate,
        _mutate_block_membership,
        _mutate_propagate_from_tile,
        _mutate_remove_switch,
        _mutate_rebuild_ordered_tiles,
    ],
)
def test_topology_mutation_clears_walk_cache(mutate):
    network, sim = _bootstrap()
    pf = sim.path_dispatcher._pathfinder
    a, b = _connected_pair(network, pf)
    pf.find_ordered_walk(a, b)
    assert network._walk_cache, "cache should be populated before the mutation"

    mutate(network)

    assert network._walk_cache == {}, (
        f"{mutate.__name__} left stale walks in the cache; every topology "
        f"mutator must call network.invalidate_pathfinding_cache()"
    )
