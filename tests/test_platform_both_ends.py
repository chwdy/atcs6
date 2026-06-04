"""M7 — platform target resolution must consider BOTH platform-end blocks.

design_new.md line 1247: for a multi-block platform, the candidate end blocks
are the first and last entries of `Platform.ordered_blocks`; the resolver picks
the shortest successful candidate using the canonical candidate comparison
(`tile_count`, then `walk_length_cm`, then `candidate_rank`, then `end_a`). For a
single-block platform both ends collapse to one block, so only that one block
participates and behavior is unchanged.

These tests drive `RouteTargetResolver.resolve_task_target_block_detailed(...)`
directly over a stub executor. The stub dispatcher's `find_paths(...)` returns a
canned candidate per `(start_block, end_block)` pair, standing in for the shared
`RailwayPathfinder` graph walk that `PathDispatcher.create_path(...)` uses — the
real ranking/geometry is covered elsewhere; here we only assert the resolver
compares the two ends and chooses the nearer one.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from route_target_resolver import RouteTargetResolver
from train_route import PlatformTarget, RouteTask


# ---------------------------------------------------------------------------
# Stubs (mirrors tests/test_route_target_resolver.py harness)
# ---------------------------------------------------------------------------


@dataclass
class _StubBlock:
    """Minimal block: the resolver only reads `disabled` / `manual` /
    `occupied_by` off blocks during availability filtering."""

    disabled: bool = False
    manual: bool = False
    occupied_by: Optional[str] = None


@dataclass
class _StubPlatform:
    """Multi-block platform whose `ordered_blocks` is derived from
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


class _StubTrack:
    def __init__(self, block_section_id: str) -> None:
        self.block_section_id = block_section_id


class _StubNetwork:
    def __init__(
        self,
        tile_blocks: Dict[Tuple[int, int], str],
        platforms: Dict[str, _StubPlatform],
        blocks: Dict[str, _StubBlock],
    ) -> None:
        self._tracks = {t: _StubTrack(b) for t, b in tile_blocks.items()}
        self.platforms = platforms
        self.blocks = blocks

    def get_track(self, x: int, y: int) -> Optional[_StubTrack]:
        return self._tracks.get((x, y))


@dataclass
class _StubSegment:
    """Stand-in for `PathSegment` — the resolver's candidate comparison reads
    `tiles` (tile_count) and `distance_cm` (walk_length_cm)."""

    tiles: List[Tuple[int, int]] = field(default_factory=list)
    distance_cm: float = 0.0


class _StubDispatcher:
    """`find_paths(start, end, ...)` returns the canned candidate list for that
    `(start_block, end_block)` pair, or `[]` when no walk was registered."""

    def __init__(
        self, walks: Dict[Tuple[str, str], List[_StubSegment]]
    ) -> None:
        self._walks = walks
        self.calls: List[Tuple[str, str, Optional[str]]] = []

    def find_paths(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
        manual_override: bool = False,
    ) -> List[_StubSegment]:
        self.calls.append((start_block_id, end_block_id, direction_constraint))
        return list(self._walks.get((start_block_id, end_block_id), []))


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


def _make_resolver(network, dispatcher, train) -> RouteTargetResolver:
    executor = _StubExecutor(network=network, path_dispatcher=dispatcher)
    head_tile = getattr(getattr(train, "position", None), "head_tile", None)
    controllers = {train.train_id: _StubController(head_tile=head_tile)}
    executor._controller_by_id = lambda tid: controllers.get(tid)
    return RouteTargetResolver(executor)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _multi_block_setup():
    """A two-block platform PLT spanning B0 (tiles 0..1) then B1 (tiles 2..3),
    with the train's head on (5,0) in its own start block START.

    Layout (block graph is abstract — the stub dispatcher supplies walks):
        START(5,0)  ...  B0[(0,0)(1,0)]  B1[(2,0)(3,0)]
    The walk START->B1 is SHORTER than START->B0, so the resolver must pick
    B1 (the nearer/last end), not B0 (ordered_blocks[0]).
    """
    platform = _StubPlatform(
        platform_id="PLT",
        positions=[(0, 0), (1, 0), (2, 0), (3, 0)],
        block_assignments={
            (0, 0): "B0",
            (1, 0): "B0",
            (2, 0): "B1",
            (3, 0): "B1",
        },
    )
    blocks = {
        "B0": _StubBlock(),
        "B1": _StubBlock(),
        "START": _StubBlock(),
    }
    network = _StubNetwork(
        tile_blocks={(5, 0): "START"},
        platforms={"PLT": platform},
        blocks=blocks,
    )
    train = _StubTrain(train_id="T1", position=_StubPosition(head_tile=(5, 0)))
    return platform, network, train


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_multi_block_platform_picks_nearer_end_block():
    """The train is closer to B1 than B0, so PlatformTarget resolution must
    return B1 — the last entry of ordered_blocks — not B0.

    Fails today: `platform_block_id` returns `ordered_blocks[0]` (B0)
    unconditionally."""
    platform, network, train = _multi_block_setup()
    # ordered_blocks fixture sanity: B0 is first, B1 is last.
    assert platform.ordered_blocks == ["B0", "B1"]

    dispatcher = _StubDispatcher({
        # START -> B0 is the far end: 5 tiles / 500cm.
        ("START", "B0"): [_StubSegment(
            tiles=[(5, 0), (4, 0), (3, 0), (2, 0), (1, 0)], distance_cm=500.0
        )],
        # START -> B1 is the near end: 3 tiles / 300cm (the winner).
        ("START", "B1"): [_StubSegment(
            tiles=[(5, 0), (4, 0), (3, 0)], distance_cm=300.0
        )],
    })
    resolver = _make_resolver(network, dispatcher, train)

    task = RouteTask(
        task_kind="stop",
        task_location=PlatformTarget(platform_id="PLT"),
        task_direction="either",
    )
    block_id, reason = resolver.resolve_task_target_block_detailed(
        task, train=train
    )
    assert reason is None
    assert block_id == "B1", (
        f"expected nearer end B1, got {block_id!r} (reason={reason!r})"
    )


def test_multi_block_platform_picks_other_end_when_reversed():
    """Symmetric check: when START -> B0 is the shorter walk, the resolver
    picks B0. Guards against the change hard-coding `ordered_blocks[-1]`."""
    platform, network, train = _multi_block_setup()
    dispatcher = _StubDispatcher({
        # Now B0 is nearer.
        ("START", "B0"): [_StubSegment(
            tiles=[(5, 0), (4, 0), (3, 0)], distance_cm=300.0
        )],
        ("START", "B1"): [_StubSegment(
            tiles=[(5, 0), (4, 0), (3, 0), (2, 0), (1, 0)], distance_cm=500.0
        )],
    })
    resolver = _make_resolver(network, dispatcher, train)

    task = RouteTask(
        task_kind="stop",
        task_location=PlatformTarget(platform_id="PLT"),
        task_direction="either",
    )
    block_id, reason = resolver.resolve_task_target_block_detailed(
        task, train=train
    )
    assert reason is None
    assert block_id == "B0"


def test_multi_block_platform_exact_tie_prefers_first_ordered_block():
    """When both ends are equidistant (same tile_count and walk_length_cm),
    the deterministic tie-break prefers `ordered_blocks[0]` (B0)."""
    platform, network, train = _multi_block_setup()
    tie = lambda: [_StubSegment(
        tiles=[(5, 0), (4, 0), (3, 0)], distance_cm=300.0
    )]
    dispatcher = _StubDispatcher({
        ("START", "B0"): tie(),
        ("START", "B1"): tie(),
    })
    resolver = _make_resolver(network, dispatcher, train)

    task = RouteTask(
        task_kind="stop",
        task_location=PlatformTarget(platform_id="PLT"),
        task_direction="either",
    )
    block_id, reason = resolver.resolve_task_target_block_detailed(
        task, train=train
    )
    assert reason is None
    assert block_id == "B0"


def test_multi_block_platform_falls_back_to_first_end_when_only_far_end_reachable():
    """If only ONE end has a successful walk, that end wins regardless of
    ordering — here only the far end B0 is reachable, so B0 is chosen."""
    platform, network, train = _multi_block_setup()
    dispatcher = _StubDispatcher({
        ("START", "B0"): [_StubSegment(
            tiles=[(5, 0), (4, 0), (3, 0), (2, 0), (1, 0)], distance_cm=500.0
        )],
        # No walk to B1 at all.
    })
    resolver = _make_resolver(network, dispatcher, train)

    task = RouteTask(
        task_kind="stop",
        task_location=PlatformTarget(platform_id="PLT"),
        task_direction="either",
    )
    block_id, reason = resolver.resolve_task_target_block_detailed(
        task, train=train
    )
    assert reason is None
    assert block_id == "B0"


def test_single_block_platform_unchanged():
    """Backward-compat: a single-block platform resolves to its one block,
    and the resolver does NOT need the dispatcher to do so (no graph walk is
    required when both ends collapse to the same block)."""
    platform = _StubPlatform(
        platform_id="PLT1",
        positions=[(0, 0), (1, 0)],
        block_assignments={(0, 0): "ONLY", (1, 0): "ONLY"},
    )
    assert platform.ordered_blocks == ["ONLY"]
    blocks = {"ONLY": _StubBlock(), "START": _StubBlock()}
    network = _StubNetwork(
        tile_blocks={(5, 0): "START"},
        platforms={"PLT1": platform},
        blocks=blocks,
    )
    train = _StubTrain(train_id="T1", position=_StubPosition(head_tile=(5, 0)))
    # Dispatcher with NO registered walks — proves single-block resolution
    # never depends on a graph walk between two distinct ends.
    dispatcher = _StubDispatcher({})
    resolver = _make_resolver(network, dispatcher, train)

    task = RouteTask(
        task_kind="stop",
        task_location=PlatformTarget(platform_id="PLT1"),
        task_direction="either",
    )
    block_id, reason = resolver.resolve_task_target_block_detailed(
        task, train=train
    )
    assert reason is None
    assert block_id == "ONLY"
    # Single-block path must not consult the dispatcher to compare two ends.
    assert dispatcher.calls == []


def test_single_block_platform_via_platform_block_id_no_train():
    """The `platform_block_id(platform_id)` forwarder (used by the viability /
    reachable HTTP endpoints with no train) keeps returning ordered_blocks[0]
    for a single-block platform."""
    platform = _StubPlatform(
        platform_id="PLT1",
        positions=[(0, 0)],
        block_assignments={(0, 0): "ONLY"},
    )
    network = _StubNetwork(
        tile_blocks={},
        platforms={"PLT1": platform},
        blocks={"ONLY": _StubBlock()},
    )
    train = _StubTrain(train_id="T1", position=_StubPosition(head_tile=(0, 0)))
    dispatcher = _StubDispatcher({})
    resolver = _make_resolver(network, dispatcher, train)
    assert resolver.platform_block_id("PLT1") == "ONLY"
