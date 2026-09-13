"""StationTarget resolution merges platform-end candidates across EVERY
platform in the station, not just the first available one.

design_new.md line 1667 / line 2818 (Planning-Kernel step `9c4f` target
resolution, cross-query comparison site 2 in the `GraphWalkCandidate` payload
home): when a `StationTarget` has no live path, the resolver queries both ends
of every platform in `Station.platform_ids`, merges all reachable platform-end
candidates into one list, and sorts them by the first three cross-query keys
`(estimated_travel_seconds, tile_count, walk_length_cm)` — travel time first
when the run's train max speed is supplied — with ties broken by platform-end
identity (the deterministic iteration order over `Station.platform_ids`, then
the two ends of each platform) rather than by `candidate_rank`. The globally
best end wins; it is NOT the first platform's end.

These tests drive `RouteTargetResolver.resolve_task_target_block_detailed(...)`
directly over a stub executor whose dispatcher returns a canned candidate per
`(start_block, end_block)` pair with a caller-set `estimated_travel_seconds`,
standing in for the shared `RailwayPathfinder` graph walk that populates the
estimate whenever the query carries the train's `train_max_speed_kmh`.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from route_target_resolver import RouteTargetResolver
from train_route import RouteTask, StationTarget


# ---------------------------------------------------------------------------
# Stubs (mirror tests/test_platform_both_ends.py, plus a station group)
# ---------------------------------------------------------------------------


@dataclass
class _StubBlock:
    disabled: bool = False
    manual: bool = False
    occupied_by: Optional[str] = None


@dataclass
class _StubPlatform:
    """Single- or multi-block platform whose `ordered_blocks` derives from
    `positions` + `block_assignments`, exactly like the real `Platform`."""

    platform_id: str
    positions: List[Tuple[int, int]] = field(default_factory=list)
    block_assignments: Dict[Tuple[int, int], str] = field(default_factory=dict)
    disabled: bool = False
    manual: bool = False

    @property
    def ordered_blocks(self) -> List[str]:
        seen = set()
        out: List[str] = []
        for pos in self.positions:
            bid = self.block_assignments.get(pos)
            if bid is None or bid in seen:
                continue
            seen.add(bid)
            out.append(bid)
        return out


@dataclass
class _StubStation:
    station_id: str
    platform_ids: List[str] = field(default_factory=list)
    disabled: bool = False
    manual: bool = False


class _StubTrack:
    def __init__(self, block_id: str) -> None:
        self.block_id = block_id


class _StubNetwork:
    def __init__(
        self,
        tile_blocks: Dict[Tuple[int, int], str],
        platforms: Dict[str, _StubPlatform],
        blocks: Dict[str, _StubBlock],
        station_groups: Dict[str, _StubStation],
    ) -> None:
        self._tracks = {t: _StubTrack(b) for t, b in tile_blocks.items()}
        self.platforms = platforms
        self.blocks = blocks
        self.station_groups = station_groups

    def get_track(self, x: int, y: int) -> Optional[_StubTrack]:
        return self._tracks.get((x, y))

    def blocks_for_platform(self, platform_id: str) -> List[str]:
        """The derived `platform -> blocks` index the resolver reads (real
        `RailwayNetwork.blocks_for_platform`); the stub mirrors it from the
        platform's own ordered block list."""
        platform = self.platforms.get(platform_id)
        return list(platform.ordered_blocks) if platform is not None else []


@dataclass
class _StubSegment:
    tiles: List[Tuple[int, int]] = field(default_factory=list)
    distance_cm: float = 0.0


@dataclass
class _StubCandidate:
    """Stand-in for `GraphWalkCandidate`. `estimated_travel_seconds` is the
    caller-set estimate the real query populates when it carries the train's
    max speed; `cross_query_key()` leads with it, then tile count, then
    distance — the exact key the resolver's cross-query merge reads."""

    walk: _StubSegment
    estimated_travel_seconds: Optional[float] = None

    def cross_query_key(self) -> Tuple[float, int, float]:
        est = self.estimated_travel_seconds
        return (
            est if est is not None else float("-inf"),
            len(self.walk.tiles),
            float(self.walk.distance_cm),
        )


class _StubDispatcher:
    def __init__(self, walks: Dict[Tuple[str, str], _StubCandidate]) -> None:
        self._walks = walks

    def find_walk_candidates(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
        manual_override: bool = False,
    ) -> List[_StubCandidate]:
        cand = self._walks.get((start_block_id, end_block_id))
        return [cand] if cand is not None else []


@dataclass
class _StubController:
    head_tile: Optional[Tuple[int, int]] = None

    def _get_head_tile(self) -> Optional[Tuple[int, int]]:
        return self.head_tile


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
class _Excl:
    """Only field the resolver reads off an exclude entry."""

    requested_destination_block_id: str


def _make_resolver(network, dispatcher, train) -> RouteTargetResolver:
    executor = _StubExecutor(network=network, path_dispatcher=dispatcher)
    head_tile = getattr(getattr(train, "position", None), "head_tile", None)
    controllers = {train.train_id: _StubController(head_tile=head_tile)}
    executor._controller_by_id = lambda tid: controllers.get(tid)
    return RouteTargetResolver(executor)


def _seg(tiles: int, dist: float) -> _StubSegment:
    return _StubSegment(tiles=[(i, 0) for i in range(tiles)], distance_cm=dist)


def _cand(tiles: int, dist: float, est: Optional[float]) -> _StubCandidate:
    return _StubCandidate(walk=_seg(tiles, dist), estimated_travel_seconds=est)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _two_single_block_platforms(walks):
    """Station STA with two single-block platforms, P1 first in platform_ids.
    P1 -> block BA, P2 -> block BB. Train head on START. `walks` maps
    (start, end) -> candidate for the stub dispatcher.
    """
    p1 = _StubPlatform(
        platform_id="P1", positions=[(0, 0)], block_assignments={(0, 0): "BA"}
    )
    p2 = _StubPlatform(
        platform_id="P2", positions=[(5, 0)], block_assignments={(5, 0): "BB"}
    )
    blocks = {"BA": _StubBlock(), "BB": _StubBlock(), "START": _StubBlock()}
    network = _StubNetwork(
        tile_blocks={(9, 0): "START"},
        platforms={"P1": p1, "P2": p2},
        blocks=blocks,
        station_groups={
            "STA": _StubStation(station_id="STA", platform_ids=["P1", "P2"])
        },
    )
    train = _StubTrain(train_id="T1", position=_StubPosition(head_tile=(9, 0)))
    dispatcher = _StubDispatcher(walks)
    return network, dispatcher, train


def _station_task() -> RouteTask:
    return RouteTask(
        task_kind="stop",
        task_location=StationTarget(station_id="STA"),
        task_direction="either",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_station_picks_globally_fastest_end_not_first_platform():
    """The required case: a 2-platform station picks the end with the lowest
    `estimated_travel_seconds` across ALL platforms, even though that end
    (P2/BB) is NOT the first platform's end AND has more tiles and a longer
    walk than P1/BA. Travel time is the first cross-query key, so the slower
    first-platform end must lose.

    Fails on the old first-available-platform resolver, which returns BA
    because P1 comes first in `platform_ids`.
    """
    network, dispatcher, train = _two_single_block_platforms({
        # P1/BA: fewer tiles + shorter walk, but SLOWER (est 9.0).
        ("START", "BA"): _cand(tiles=2, dist=200.0, est=9.0),
        # P2/BB: more tiles + longer walk, but FASTER (est 4.0) -> global best.
        ("START", "BB"): _cand(tiles=4, dist=400.0, est=4.0),
    })
    resolver = _make_resolver(network, dispatcher, train)

    block_id, reason = resolver.resolve_task_target_block_detailed(
        _station_task(), train=train
    )
    assert reason is None
    assert block_id == "BB", (
        f"expected globally-fastest end BB (est 4.0 < 9.0), got {block_id!r}"
    )


def test_station_tie_breaks_by_platform_end_identity():
    """When two platform ends carry identical cross-query keys, the tie-break
    is platform-end identity — the deterministic iteration order over
    `Station.platform_ids` — so the first platform (P1/BA) wins, NOT
    candidate_rank or any distance heuristic."""
    tie = lambda: _cand(tiles=3, dist=300.0, est=5.0)
    network, dispatcher, train = _two_single_block_platforms({
        ("START", "BA"): tie(),
        ("START", "BB"): tie(),
    })
    resolver = _make_resolver(network, dispatcher, train)

    block_id, reason = resolver.resolve_task_target_block_detailed(
        _station_task(), train=train
    )
    assert reason is None
    assert block_id == "BA"


def test_station_excluded_pair_walks_to_next_candidate():
    """Plan's same-pass retry excludes a tried `(block, direction)` pair; the
    resolver then returns the NEXT-best candidate down the sorted list instead
    of re-offering the global best. With the global best BB excluded, the
    resolver returns BA. With BOTH excluded, it returns the global best BB so
    `find_next_task_target`'s own exclude check can emit its clean sentinel.
    """
    network, dispatcher, train = _two_single_block_platforms({
        ("START", "BA"): _cand(tiles=2, dist=200.0, est=9.0),
        ("START", "BB"): _cand(tiles=4, dist=400.0, est=4.0),
    })
    resolver = _make_resolver(network, dispatcher, train)
    task = _station_task()

    # Global best BB excluded -> next candidate BA.
    block_id, reason = resolver.resolve_task_target_block_detailed(
        task, train=train, exclude=[_Excl("BB")]
    )
    assert reason is None
    assert block_id == "BA"

    # Both excluded (candidates exhausted) -> the global best BB, which the
    # caller's post-resolution exclude check turns into the no-more-candidates
    # sentinel.
    block_id2, reason2 = resolver.resolve_task_target_block_detailed(
        task, train=train, exclude=[_Excl("BB"), _Excl("BA")]
    )
    assert reason2 is None
    assert block_id2 == "BB"


def test_station_merges_both_ends_of_every_platform():
    """Both ends of a multi-block platform participate in the global merge,
    and the sorted list interleaves ends across platforms by cost.

    P1 is a two-block platform (ends BA0, BA1); P2 is single-block BB. Costs:
    BA1 (est 3) < BB (est 5) < BA0 (est 8). Walking the exclude list must yield
    BA1, then BB (P2's end, interleaved BETWEEN P1's two ends), then BA0 —
    which a first-available-platform or per-platform-then-merge resolver could
    not produce.
    """
    p1 = _StubPlatform(
        platform_id="P1",
        positions=[(0, 0), (1, 0), (2, 0), (3, 0)],
        block_assignments={
            (0, 0): "BA0", (1, 0): "BA0", (2, 0): "BA1", (3, 0): "BA1"
        },
    )
    p2 = _StubPlatform(
        platform_id="P2", positions=[(7, 0)], block_assignments={(7, 0): "BB"}
    )
    assert p1.ordered_blocks == ["BA0", "BA1"]
    blocks = {
        "BA0": _StubBlock(), "BA1": _StubBlock(),
        "BB": _StubBlock(), "START": _StubBlock(),
    }
    network = _StubNetwork(
        tile_blocks={(9, 0): "START"},
        platforms={"P1": p1, "P2": p2},
        blocks=blocks,
        station_groups={
            "STA": _StubStation(station_id="STA", platform_ids=["P1", "P2"])
        },
    )
    train = _StubTrain(train_id="T1", position=_StubPosition(head_tile=(9, 0)))
    dispatcher = _StubDispatcher({
        ("START", "BA0"): _cand(tiles=2, dist=200.0, est=8.0),
        ("START", "BA1"): _cand(tiles=3, dist=300.0, est=3.0),  # global best
        ("START", "BB"): _cand(tiles=4, dist=400.0, est=5.0),
    })
    resolver = _make_resolver(network, dispatcher, train)
    task = _station_task()

    first, _ = resolver.resolve_task_target_block_detailed(task, train=train)
    assert first == "BA1"

    second, _ = resolver.resolve_task_target_block_detailed(
        task, train=train, exclude=[_Excl("BA1")]
    )
    assert second == "BB", "P2's end must interleave between P1's two ends"

    third, _ = resolver.resolve_task_target_block_detailed(
        task, train=train, exclude=[_Excl("BA1"), _Excl("BB")]
    )
    assert third == "BA0"


def test_station_prefers_available_over_end_held_by_other_train():
    """A reachable end held by ANOTHER train drops below every reachable and
    available end, even when the held end is faster. BB is held by T2, so the
    slower-but-free BA wins; the held end is only a last-resort fallback so the
    dispatcher can report the block conflict."""
    network, dispatcher, train = _two_single_block_platforms({
        ("START", "BA"): _cand(tiles=4, dist=400.0, est=9.0),  # free, slower
        ("START", "BB"): _cand(tiles=2, dist=200.0, est=2.0),  # fast but held
    })
    network.blocks["BB"].occupied_by = "T2"
    resolver = _make_resolver(network, dispatcher, train)

    block_id, reason = resolver.resolve_task_target_block_detailed(
        _station_task(), train=train
    )
    assert reason is None
    assert block_id == "BA"


def test_station_self_occupied_end_still_available():
    """The routing train's OWN occupancy never disqualifies an end (design_new
    line 1664). BB is occupied by T1 itself and is the fastest, so it still
    wins over BA."""
    network, dispatcher, train = _two_single_block_platforms({
        ("START", "BA"): _cand(tiles=4, dist=400.0, est=9.0),
        ("START", "BB"): _cand(tiles=2, dist=200.0, est=2.0),
    })
    network.blocks["BB"].occupied_by = "T1"  # the routing train itself
    resolver = _make_resolver(network, dispatcher, train)

    block_id, reason = resolver.resolve_task_target_block_detailed(
        _station_task(), train=train
    )
    assert reason is None
    assert block_id == "BB"
