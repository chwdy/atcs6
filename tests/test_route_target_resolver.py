"""Unit tests for `route_target_resolver.RouteTargetResolver`.

Pure helpers (`resolve_task_direction`, `compute_next_task_index`) are
exercised directly. End-to-end `find_next_task_target` behaviour is
covered by `tests/test_route_execution.py` through the
`RouteExecutor._find_next_task_target` forwarder.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from route_target_resolver import RouteTargetResolver
from train_route import RouteMode


# ---------------------------------------------------------------------------
# Stubs for resolve_task_direction
# ---------------------------------------------------------------------------


class _StubTrack:
    def __init__(self, block_section_id: str) -> None:
        self.block_section_id = block_section_id


class _StubNetwork:
    def __init__(self, tile_blocks: Dict[Tuple[int, int], str]) -> None:
        self._tracks = {
            tile: _StubTrack(bid) for tile, bid in tile_blocks.items()
        }

    def get_track(self, x: int, y: int) -> Optional[_StubTrack]:
        return self._tracks.get((x, y))


class _StubDispatcher:
    def __init__(self, candidates: Dict[Optional[str], List[Any]]) -> None:
        self._candidates = candidates

    def find_paths(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
    ) -> List[Any]:
        return list(self._candidates.get(direction_constraint, []))


@dataclass
class _StubExecutor:
    network: Any = None
    path_dispatcher: Any = None


@dataclass
class _StubPosition:
    head_tile: Tuple[int, int]


@dataclass
class _StubTrain:
    train_id: str = "T1"
    position: Any = None


@dataclass
class _StubSegment:
    tiles: List[Tuple[int, int]] = field(default_factory=list)


def _make_resolver(
    network: Any = None, dispatcher: Any = None
) -> RouteTargetResolver:
    return RouteTargetResolver(
        _StubExecutor(network=network, path_dispatcher=dispatcher)
    )


# ---------------------------------------------------------------------------
# resolve_task_direction
# ---------------------------------------------------------------------------


def test_resolve_task_direction_passes_through_explicit_forward():
    resolver = _make_resolver()
    assert resolver.resolve_task_direction(
        train=None,
        block_id="BLK_X",
        task_direction="forward",
        signed_direction=1,
    ) == "forward"


def test_resolve_task_direction_passes_through_explicit_reverse():
    resolver = _make_resolver()
    assert resolver.resolve_task_direction(
        train=None,
        block_id="BLK_X",
        task_direction="reverse",
        signed_direction=1,
    ) == "reverse"


def test_resolve_task_direction_either_picks_only_reachable_direction():
    """Per design_new.md:1145, `task_direction == "either"` on a
    BlockTarget picks whichever block-relative direction is reachable
    from the train's current span. When only "reverse" yields a
    candidate, "either" must resolve to "reverse" even if the route
    walk direction would prefer "forward"."""
    dispatcher = _StubDispatcher({
        "forward": [],
        "reverse": [_StubSegment(tiles=[(2, 0), (1, 0), (0, 0)])],
    })
    network = _StubNetwork({(5, 0): "BLOCK_MAIN_EAST"})
    train = _StubTrain(
        train_id="T1", position=_StubPosition(head_tile=(5, 0))
    )
    resolver = _make_resolver(network=network, dispatcher=dispatcher)
    assert resolver.resolve_task_direction(
        train=train,
        block_id="BLOCK_MAIN_WEST",
        task_direction="either",
        signed_direction=1,
    ) == "reverse"


def test_resolve_task_direction_either_picks_forward_when_only_forward_reachable():
    dispatcher = _StubDispatcher({
        "forward": [_StubSegment(tiles=[(0, 0), (1, 0), (2, 0)])],
        "reverse": [],
    })
    network = _StubNetwork({(0, 0): "BLOCK_MAIN_WEST"})
    train = _StubTrain(
        train_id="T1", position=_StubPosition(head_tile=(0, 0))
    )
    resolver = _make_resolver(network=network, dispatcher=dispatcher)
    assert resolver.resolve_task_direction(
        train=train,
        block_id="BLOCK_MAIN_EAST",
        task_direction="either",
        signed_direction=-1,
    ) == "forward"


def test_resolve_task_direction_either_tie_breaks_by_shortest_candidate():
    """Per design_new.md:1158, when both directions yield candidates the
    shortest allowed candidate wins."""
    forward_long = _StubSegment(
        tiles=[(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
    )
    reverse_short = _StubSegment(tiles=[(2, 0), (1, 0), (0, 0)])
    dispatcher = _StubDispatcher({
        "forward": [forward_long],
        "reverse": [reverse_short],
    })
    network = _StubNetwork({(2, 0): "BLOCK_X"})
    train = _StubTrain(
        train_id="T1", position=_StubPosition(head_tile=(2, 0))
    )
    resolver = _make_resolver(network=network, dispatcher=dispatcher)
    assert resolver.resolve_task_direction(
        train=train,
        block_id="BLOCK_Y",
        task_direction="either",
        signed_direction=1,
    ) == "reverse"


def test_resolve_task_direction_either_falls_back_to_signed_direction_without_dispatcher():
    """When the dispatcher isn't available (early bootstrap, unit-test
    isolation), the resolver falls back to the route walk direction so
    the caller still gets a concrete forward/reverse string to hand to
    create_path / extend_path."""
    resolver = _make_resolver()  # no dispatcher, no network
    assert resolver.resolve_task_direction(
        train=None,
        block_id="BLK_X",
        task_direction="either",
        signed_direction=1,
    ) == "forward"
    assert resolver.resolve_task_direction(
        train=None,
        block_id="BLK_X",
        task_direction="either",
        signed_direction=0,
    ) == "forward"
    assert resolver.resolve_task_direction(
        train=None,
        block_id="BLK_X",
        task_direction="either",
        signed_direction=-1,
    ) == "reverse"


# ---------------------------------------------------------------------------
# compute_next_task_index (static, pure)
# ---------------------------------------------------------------------------


@dataclass
class _Route:
    mode: RouteMode


@dataclass
class _ExecState:
    """Test fixture matching the post-D3 semantics: `next_planning_index`
    is the next task to plan. The legacy `current_waypoint_index` is now
    `Optional[int]` (largest finished, or None) and is not what the
    `compute_next_task_index` helper reads.
    """
    next_planning_index: int = 0


def test_compute_next_task_index_zero_count_returns_none():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ONE_OFF), _ExecState(0), task_count=0
    )
    assert res is None


def test_compute_next_task_index_in_range_returns_idx():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ONE_OFF), _ExecState(2), task_count=5
    )
    assert res == 2


def test_compute_next_task_index_one_off_past_end_returns_none():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ONE_OFF), _ExecState(7), task_count=5
    )
    assert res is None


def test_compute_next_task_index_circular_wraps():
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.CIRCULAR), _ExecState(7), task_count=5
    )
    assert res == 2  # 7 % 5 == 2


def test_compute_next_task_index_round_trip_past_end_reflects():
    """ROUND_TRIP past the end → task_count - 2 (second-to-last)."""
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ROUND_TRIP), _ExecState(5), task_count=5
    )
    assert res == 3


def test_compute_next_task_index_round_trip_negative_reflects():
    """ROUND_TRIP below 0 → 1 (second tile)."""
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ROUND_TRIP), _ExecState(-1), task_count=5
    )
    assert res == 1


def test_compute_next_task_index_round_trip_single_task():
    """Single-task ROUND_TRIP edge case — overflow reflects to 0."""
    res = RouteTargetResolver.compute_next_task_index(
        _Route(RouteMode.ROUND_TRIP), _ExecState(2), task_count=1
    )
    assert res == 0
