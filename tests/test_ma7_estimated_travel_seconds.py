"""M-A7 — `estimated_travel_seconds` as the first candidate comparison key.

design_new.md "GraphWalkCandidate payload" (lines 2811 / 2816 / 2818 / 2820):
`GraphWalkCandidate.estimated_travel_seconds` is populated exactly when the
originating `GraphWalkQuery` carries `train_max_speed_kmh`, and is the FIRST
comparison key when populated. The in-query key is
`(estimated_travel_seconds, tile_count, walk_length_cm, candidate_rank)`; the
two cross-query sites use only the first three keys plus their own tie-break.

These tests pin:
  1. both comparison keys lead with `estimated_travel_seconds`, cross-query
     drops `candidate_rank`, and a `None` estimate falls through to the
     distance keys;
  2. the no-cab `create_path(...)` both-ends compare (a cross-query site) picks
     the lower-`estimated_travel_seconds` end even when that end has more tiles
     and a longer walk — proving the first key dominates;
  3. the shared query stores the estimate on every returned candidate only when
     the train's max speed is supplied, and never writes it onto the cached
     (max-speed-independent) candidate list a later geometry-only query reuses.
"""

import contextlib
import io
import types

import pytest

from bootstrap import bootstrap_runtime
from path_dispatcher import PathDispatcher
from path_utils import GraphWalkCandidate


# ---------------------------------------------------------------------------
# 1. Comparison-key semantics (unit)
# ---------------------------------------------------------------------------


def test_comparison_keys_lead_with_estimated_travel_seconds():
    # Faster (low estimate) but MORE tiles and a LONGER walk than the slow one.
    fast_long = GraphWalkCandidate(
        tile_count=20, walk_length_cm=5000.0,
        estimated_travel_seconds=10.0, candidate_rank=9,
    )
    slow_short = GraphWalkCandidate(
        tile_count=2, walk_length_cm=200.0,
        estimated_travel_seconds=100.0, candidate_rank=0,
    )
    # Estimate is the first key in BOTH the in-query and cross-query orderings,
    # so the faster candidate wins despite the tile/length disadvantage.
    assert fast_long.comparison_key() < slow_short.comparison_key()
    assert fast_long.cross_query_key() < slow_short.cross_query_key()

    # In-query key carries candidate_rank (4 elements); cross-query key drops it
    # (3 elements) because rank means nothing across two separate queries.
    assert len(fast_long.comparison_key()) == 4
    assert len(fast_long.cross_query_key()) == 3
    assert fast_long.comparison_key()[0] == 10.0
    assert fast_long.comparison_key()[3] == 9

    # No estimate (geometry-only): both keys fall through to the distance keys,
    # so the shorter / fewer-tile candidate wins.
    geo_short = GraphWalkCandidate(tile_count=2, walk_length_cm=200.0)
    geo_long = GraphWalkCandidate(tile_count=20, walk_length_cm=5000.0)
    assert geo_short.estimated_travel_seconds is None
    assert geo_short.comparison_key() < geo_long.comparison_key()
    assert geo_short.cross_query_key() < geo_long.cross_query_key()


# ---------------------------------------------------------------------------
# 2. No-cab create_path both-ends cross-query site (unit over a real dispatcher)
# ---------------------------------------------------------------------------


def _cabless_dispatcher(candidates_by_start):
    """A real `PathDispatcher` whose controller lookup and candidate query are
    stubbed so `_resolve_create_start_block(...)` exercises only the cross-query
    both-ends compare. The stub train is cabless with a 3-block span whose two
    physical ends are `BLK_A` (head) and `BLK_B`."""
    network = types.SimpleNamespace(blocks={}, trains={})
    dispatcher = PathDispatcher(network)
    span = types.SimpleNamespace(occupied_blocks=["BLK_A", "MID", "BLK_B"])
    ctrl = types.SimpleNamespace(
        commands=types.SimpleNamespace(cab=None),
        get_current_span=lambda: span,
    )
    dispatcher._get_controller = lambda train_id: ctrl

    def _fake_find(start, end, train_id=None, manual_override=False,
                   direction_constraint=None):
        return list(candidates_by_start.get(start, []))

    dispatcher._find_walk_candidates = _fake_find
    return dispatcher


def test_cabless_create_path_picks_lower_estimated_travel_seconds_end():
    # End A: few tiles, short walk, but SLOW (high estimate).
    # End B: 10x the tiles and 25x the walk length, but FAST (low estimate).
    cand_a = GraphWalkCandidate(
        tile_count=2, walk_length_cm=200.0, estimated_travel_seconds=100.0,
    )
    cand_b = GraphWalkCandidate(
        tile_count=20, walk_length_cm=5000.0, estimated_travel_seconds=10.0,
    )
    dispatcher = _cabless_dispatcher({"BLK_A": [cand_a], "BLK_B": [cand_b]})

    start = dispatcher._resolve_create_start_block(
        train=object(), train_id="T1", target_block_id="TARGET",
        head_block="BLK_A", manual_override=False,
    )
    # estimated_travel_seconds is the first cross-query key, so the faster end
    # (B) wins even though it has far more tiles and a far longer walk.
    assert start == "BLK_B"


def test_cabless_create_path_falls_back_to_distance_when_estimate_absent():
    # Neither candidate carries an estimate (train has no max speed): the
    # compare falls through to tile_count / walk_length_cm, so the shorter end
    # (A) wins. This isolates estimated_travel_seconds as the deciding key in
    # the test above.
    cand_a = GraphWalkCandidate(tile_count=2, walk_length_cm=200.0)
    cand_b = GraphWalkCandidate(tile_count=20, walk_length_cm=5000.0)
    dispatcher = _cabless_dispatcher({"BLK_A": [cand_a], "BLK_B": [cand_b]})

    start = dispatcher._resolve_create_start_block(
        train=object(), train_id="T1", target_block_id="TARGET",
        head_block="BLK_A", manual_override=False,
    )
    assert start == "BLK_A"


# ---------------------------------------------------------------------------
# 3. Shared query populates the estimate + never corrupts the cache
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.scene("demo_5")


def _bootstrap():
    with contextlib.redirect_stdout(io.StringIO()):
        network, _dispatcher, sim, _persistence = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_5"
        )
    return network, sim


def _connected_pair(network, pf):
    blocks = list(network.blocks.keys())
    for a in blocks:
        for b in blocks:
            if a != b and pf.find_ordered_walk(a, b):
                return a, b
    raise AssertionError("demo_5 contains no connected block pair")


def test_query_populates_estimate_only_with_max_speed_and_keeps_cache_clean():
    network, sim = _bootstrap()
    pf = sim.path_dispatcher._pathfinder
    a, b = _connected_pair(network, pf)

    # Geometry-only query: every candidate leaves the estimate None.
    geo = pf.find_ordered_walk(a, b)
    assert geo, "expected at least one walk for a connected pair"
    assert all(c.estimated_travel_seconds is None for c in geo)

    # With a train max speed: the estimate is populated (>= 0) on every
    # returned candidate.
    fast = pf.find_ordered_walk(a, b, train_max_speed_kmh=80.0)
    assert fast
    assert all(c.estimated_travel_seconds is not None for c in fast)
    assert all(c.estimated_travel_seconds >= 0.0 for c in fast)

    # The estimate is written onto per-query copies, never the shared cached
    # (max-speed-independent) candidate list, so a later geometry-only query
    # still sees None.
    geo_again = pf.find_ordered_walk(a, b)
    assert all(c.estimated_travel_seconds is None for c in geo_again)
