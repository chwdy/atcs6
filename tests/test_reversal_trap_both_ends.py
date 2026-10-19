"""The reversal-trap exclusion predicts a reservation-less occupant's exit from
both physical ends of its span (design_new.md "`create_path()` flow", the
reversal-trap exclusion; "Which train ends participate").

The walk queries are stubbed so each test pins which end wins the cross-query
comparison and where that end's walk first leaves the occupant's span.

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_reversal_trap_both_ends.py -q``
"""

from types import SimpleNamespace

from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from runtime_payloads import ResolvedRouteTarget
from tests.segment_test_helpers import make_segment_block


def _candidate(blocks, key):
    return SimpleNamespace(
        walk=SimpleNamespace(blocks=list(blocks)),
        cross_query_key=lambda: key,
    )


def _dispatcher(walks, target=ResolvedRouteTarget("X", "forward")):
    """Occupant OCC stands on B1 (end_a) and B2 (end_b), with no live path.
    The follower's candidate terminal T sits just outside B1."""
    network = RailwayNetwork()
    for block_id in ("B1", "B2"):
        make_segment_block(network, block_id)
    network.blocks["B1"].occupied_by = "OCC"
    network.blocks["B2"].occupied_by = "OCC"
    dispatcher = PathDispatcher(network)
    route_executor = SimpleNamespace(
        find_next_move_target=lambda train_id: target,
        assigned_route_id_for=lambda train_id: None,
        get_route=lambda route_id: None,
    )
    occupant = SimpleNamespace(
        get_current_span=lambda: SimpleNamespace(occupied_blocks=["B1", "B2"])
    )
    dispatcher.wire_dependencies(
        route_executor=route_executor, controllers={"OCC": occupant}
    )
    queries = []

    def find(start, end, train_id=None, direction_constraint=None, manual_override=False):
        queries.append((start, end, direction_constraint))
        return walks.get(start, [])

    dispatcher._find_walk_candidates = find
    return dispatcher, queries


def test_the_far_end_wins_when_its_walk_is_quicker():
    """End_b's walk is quicker and leaves forward, so T is not barred, although
    a walk from the block next to T would have come back through it."""
    dispatcher, queries = _dispatcher({
        "B1": [_candidate(["B1", "T", "X"], (10.0, 3, 30.0))],
        "B2": [_candidate(["B2", "B3", "X"], (5.0, 3, 30.0))],
    })

    assert dispatcher._terminal_traps_reversing_occupant("B1", "T", "FOLLOW") is False
    assert queries == [("B1", "X", "forward"), ("B2", "X", "forward")]


def test_an_exact_tie_goes_to_end_a():
    dispatcher, _queries = _dispatcher({
        "B1": [_candidate(["B1", "T", "X"], (5.0, 3, 30.0))],
        "B2": [_candidate(["B2", "B3", "X"], (5.0, 3, 30.0))],
    })

    assert dispatcher._terminal_traps_reversing_occupant("B1", "T", "FOLLOW") is True


def test_the_exit_is_the_first_block_outside_the_span():
    """A walk from end_b that runs back through the span still exits at the
    first block outside it."""
    dispatcher, _queries = _dispatcher({
        "B2": [_candidate(["B2", "B1", "T", "X"], (1.0, 4, 40.0))],
    })

    assert dispatcher._terminal_traps_reversing_occupant("B1", "T", "FOLLOW") is True


def test_no_walk_from_either_end_is_not_exempt():
    dispatcher, _queries = _dispatcher({})

    assert dispatcher._terminal_traps_reversing_occupant("B1", "T", "FOLLOW") is True
