"""Tests for configurable route-run start mode (design_new.md "Run start-mode
seeding").

Covers:
  * `RouteStartMode` enum + `Route` serialization round-trip / defaults.
  * `RouteTargetResolver.nearest_start_stop_index(...)` — the four-criterion
    ranking (no-reversal → free-first → nearest → lowest-index), directed-reach
    eligibility, tie-break, and FIRST fallback when no stop is reachable. (The
    no-reversal precedence is exercised end-to-end in test_demo_6_nearest_seed.py;
    these resolver-unit stubs hold every stop drive-on, so they pin the
    free-first / nearest / tie-break tiers.)
  * `RouteExecutor._seed_start_position(...)` allocation-time seeding — FIRST
    (no seed), SPECIFIC (+ invalid/non-stop fallback to FIRST), the per-
    assignment one-shot pending bundle, and ROUND_TRIP apex-start reflection.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from route_executor import RouteExecutor, RouteExecutionState
from route_target_resolver import RouteTargetResolver
from train_route import (
    BlockTarget,
    Route,
    RouteMode,
    RouteStartMode,
    RouteTask,
)


# ---------------------------------------------------------------------------
# Stubs for the resolver-level nearest-stop tests
# ---------------------------------------------------------------------------


class _Seg:
    def __init__(self, tiles: int, distance_cm: float) -> None:
        self.tiles = [0] * tiles
        self.distance_cm = distance_cm


class _Block:
    def __init__(self, occupied_by: Optional[str] = None) -> None:
        self.occupied_by = occupied_by
        self.disabled = False
        self.manual = False


class _Track:
    def __init__(self, block_id: str) -> None:
        self.block_id = block_id


class _Network:
    """Network stub: a `blocks` registry, a head-tile → start-block `get_track`,
    and empty platform/station registries (tests use `BlockTarget` tasks)."""

    def __init__(self, blocks: Dict[str, _Block]) -> None:
        self.blocks = blocks
        self.platforms: Dict[str, Any] = {}
        self.station_groups: Dict[str, Any] = {}

    def get_track(self, x: int, y: int) -> Optional[_Track]:
        return _Track("START") if (x, y) == (0, 0) else None


class _Dispatcher:
    """`find_paths` keyed by destination block: maps `end_block_id` to a
    `(tile_count, distance_cm)` pair, or to `None`/absent for unreachable."""

    def __init__(self, dist_by_block: Dict[str, Optional[Tuple[int, float]]]) -> None:
        self._d = dist_by_block

    def find_paths(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
    ) -> List[Any]:
        m = self._d.get(end_block_id)
        if not m:
            return []
        return [_Seg(*m)]


class _Controller:
    def __init__(self, head_tile: Optional[Tuple[int, int]]) -> None:
        self._head_tile = head_tile

    def _get_head_tile(self) -> Optional[Tuple[int, int]]:
        return self._head_tile


class _Executor:
    def __init__(self, network: Any, dispatcher: Any) -> None:
        self.network = network
        self.path_dispatcher = dispatcher

    def _controller_by_id(self, train_id: str) -> Any:
        return _Controller(head_tile=(0, 0))


def _stop(bid: str) -> RouteTask:
    return RouteTask(task_kind="stop", task_location=BlockTarget(bid))


def _make_nearest_resolver(
    dist_by_block: Dict[str, Optional[Tuple[int, float]]],
    occupied: Optional[Dict[str, str]] = None,
) -> RouteTargetResolver:
    occupied = occupied or {}
    blocks = {bid: _Block(occupied_by=occupied.get(bid)) for bid in dist_by_block}
    network = _Network(blocks)
    return RouteTargetResolver(_Executor(network, _Dispatcher(dist_by_block)))


# ---------------------------------------------------------------------------
# Route serialization + enum
# ---------------------------------------------------------------------------


def test_route_start_mode_defaults_and_roundtrip():
    r = Route(route_id="r1")
    assert r.start_mode == RouteStartMode.FIRST
    assert r.start_task_index is None

    d = r.to_dict()
    assert d["start_mode"] == "first" and d["start_task_index"] is None

    r2 = Route.from_dict({"route_id": "r2", "start_mode": "nearest", "start_task_index": "3"})
    assert r2.start_mode == RouteStartMode.NEAREST and r2.start_task_index == 3

    # Bad values fall back to safe defaults rather than raising in from_dict.
    r3 = Route.from_dict({"route_id": "r3", "start_mode": "bogus", "start_task_index": "x"})
    assert r3.start_mode == RouteStartMode.FIRST and r3.start_task_index is None


# ---------------------------------------------------------------------------
# nearest_start_stop_index
# ---------------------------------------------------------------------------


def test_nearest_picks_closest_stop():
    resolver = _make_nearest_resolver({"A": (5, 50.0), "B": (2, 20.0), "C": (9, 90.0)})
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B"), _stop("C")])
    train = SimpleNamespace(train_id="T1")
    # B is nearest (fewest tiles) → task index 1.
    assert resolver.nearest_start_stop_index(train, route) == 1


def test_nearest_free_first_outranks_a_nearer_taken_stop():
    # Criterion 2 (free-first): a stop taken by another train drops below the
    # free stops even though it is the nearest. Every stop here is drive-on
    # (reverse_penalty 0 in these stubs), so free-first decides, then nearest
    # breaks the tie among the free stops.
    resolver = _make_nearest_resolver(
        {"A": (5, 50.0), "B": (2, 20.0), "C": (4, 40.0)},
        occupied={"B": "OTHER"},  # nearest, but taken by a different train
    )
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B"), _stop("C")])
    train = SimpleNamespace(train_id="T1")
    # B is nearest but taken → drops below the free stops; nearest free is C (idx 2).
    assert resolver.nearest_start_stop_index(train, route) == 2


def test_nearest_self_occupancy_does_not_skip():
    # A stop occupied by THIS train is still a valid (and here, nearest) start.
    resolver = _make_nearest_resolver(
        {"A": (5, 50.0), "B": (2, 20.0)},
        occupied={"B": "T1"},
    )
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B")])
    train = SimpleNamespace(train_id="T1")
    assert resolver.nearest_start_stop_index(train, route) == 1


def test_nearest_all_taken_falls_through_to_nearest():
    # When every eligible stop is taken, criterion 2 (free-first) ties, so the
    # ranking falls through to criterion 3 (nearest). This is the tie-fallthrough,
    # not a special "all occupied" rule — eligibility still holds (both have a
    # viable path), so it does NOT fall back to FIRST.
    resolver = _make_nearest_resolver(
        {"A": (5, 50.0), "B": (2, 20.0)},
        occupied={"A": "OTHER", "B": "OTHER"},
    )
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B")])
    train = SimpleNamespace(train_id="T1")
    # Both taken → free-first ties → nearest wins: B (idx 1).
    assert resolver.nearest_start_stop_index(train, route) == 1


def test_nearest_tie_breaks_to_lowest_index():
    resolver = _make_nearest_resolver({"A": (3, 30.0), "B": (3, 30.0)})
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B")])
    train = SimpleNamespace(train_id="T1")
    assert resolver.nearest_start_stop_index(train, route) == 0


def test_nearest_ignores_non_stop_tasks():
    resolver = _make_nearest_resolver({"W": (1, 10.0), "S": (5, 50.0)})
    wp = RouteTask(task_kind="waypoint", task_location=BlockTarget("W"))
    route = Route(route_id="r", tasks=[wp, _stop("S")])
    train = SimpleNamespace(train_id="T1")
    # The waypoint (closer) is not a stop; only the stop S (idx 1) is eligible.
    assert resolver.nearest_start_stop_index(train, route) == 1


def test_nearest_none_when_no_stop_reachable():
    resolver = _make_nearest_resolver({"A": None, "B": None})
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B")])
    train = SimpleNamespace(train_id="T1")
    assert resolver.nearest_start_stop_index(train, route) is None


# ---------------------------------------------------------------------------
# nearest_start_stop_index — no-reverse preference + cross-train de-confliction
# ---------------------------------------------------------------------------


class _WalkSeg:
    """A `find_paths` candidate that also carries its block sequence, so the
    no-reverse predicate can read `blocks[1]` / `blocks[-2]`."""

    def __init__(self, blocks: List[str], tiles: int, distance_cm: float) -> None:
        self.blocks = list(blocks)
        self.tiles = [0] * tiles
        self.distance_cm = distance_cm


class _WalkDispatcher:
    """`find_paths` keyed by `(start_block_id, end_block_id)` → a
    `(blocks, tile_count, distance_cm)` triple, or `None`/absent for an
    unreachable pair. Unlike `_Dispatcher`, distinct start blocks give distinct
    walks, which the arrival-vs-onward reversal comparison needs."""

    def __init__(self, walks: Dict[Tuple[str, str], Optional[Tuple[List[str], int, float]]]) -> None:
        self._w = walks

    def find_paths(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
    ) -> List[Any]:
        m = self._w.get((start_block_id, end_block_id))
        if not m:
            return []
        return [_WalkSeg(*m)]


def _make_walk_resolver(
    walks: Dict[Tuple[str, str], Optional[Tuple[List[str], int, float]]],
    blocks: List[str],
) -> RouteTargetResolver:
    blk = {bid: _Block() for bid in blocks}
    network = _Network(blk)
    return RouteTargetResolver(_Executor(network, _WalkDispatcher(walks)))


# Only X and Z are reachable from START. X is the closer stop but a *reverse*
# stop (its next move X→Y backs out through M, the block it arrived through);
# Z is farther but drive-on (its next move Z→W leaves through P, not its
# arrival block N). Y and W are unreachable from START, so they never compete.
_BASE_WALKS = {
    ("START", "X"): (["START", "M", "X"], 2, 20.0),
    ("START", "Z"): (["START", "N", "Z"], 5, 50.0),
    ("START", "Y"): None,
    ("START", "W"): None,
    ("Z", "W"): (["Z", "P", "Q"], 2, 20.0),  # leaves through P → drive-on
}
_FOUR_STOPS = [_stop("X"), _stop("Y"), _stop("Z"), _stop("W")]


def test_nearest_prefers_no_reverse_stop_over_closer_reverse_stop():
    walks = dict(_BASE_WALKS)
    walks[("X", "Y")] = (["X", "M", "Q"], 2, 20.0)  # re-enters M → reverse stop
    resolver = _make_walk_resolver(walks, blocks=["X", "Y", "Z", "W"])
    route = Route(route_id="r", tasks=list(_FOUR_STOPS))
    train = SimpleNamespace(train_id="T1")
    # Closer X (idx 0) forces a reverse; farther Z (idx 2) is drive-on → Z wins.
    assert resolver.nearest_start_stop_index(train, route) == 2


def test_nearest_among_no_reverse_stops_takes_closest():
    walks = dict(_BASE_WALKS)
    walks[("X", "Y")] = (["X", "R", "Q"], 2, 20.0)  # leaves through R → drive-on
    resolver = _make_walk_resolver(walks, blocks=["X", "Y", "Z", "W"])
    route = Route(route_id="r", tasks=list(_FOUR_STOPS))
    train = SimpleNamespace(train_id="T1")
    # Both X and Z are now drive-on, so the closer one (X, idx 0) wins.
    assert resolver.nearest_start_stop_index(train, route) == 0


def test_nearest_skips_stop_targeted_by_another_train():
    # A (idx 0) is closest and drive-on, but another train already running this
    # route is heading to A's block, so A is "taken" → B (idx 1) is seeded.
    resolver = _make_nearest_resolver({"A": (2, 20.0), "B": (5, 50.0)})
    resolver._e.execution_states = {"OTHER": SimpleNamespace(route_id="r")}
    resolver.find_next_move_target = lambda tid: "A" if tid == "OTHER" else None
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B")])
    train = SimpleNamespace(train_id="T1")
    assert resolver.nearest_start_stop_index(train, route) == 1


# ---------------------------------------------------------------------------
# nearest_start_stop_index — directed viability gate (connectivity + direction)
# ---------------------------------------------------------------------------


class _DirDispatcher:
    """Direction-aware `find_paths` stub. `dirs_by_block` maps `end_block_id`
    to `{"forward": metric | None, "reverse": metric | None}`, each metric a
    `(tile_count, distance_cm)` pair. With no `direction_constraint` the walk is
    reachable when either direction is; with a constraint only that direction's
    metric is consulted, so a stop can be reachable one way and not the other."""

    def __init__(
        self, dirs_by_block: Dict[str, Dict[str, Optional[Tuple[int, float]]]]
    ) -> None:
        self._d = dirs_by_block

    def find_paths(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
    ) -> List[Any]:
        per_dir = self._d.get(end_block_id) or {}
        if direction_constraint is None:
            m = per_dir.get("forward") or per_dir.get("reverse")
        else:
            m = per_dir.get(direction_constraint)
        return [_Seg(*m)] if m else []


def _make_dir_resolver(
    dirs_by_block: Dict[str, Dict[str, Optional[Tuple[int, float]]]],
    occupied: Optional[Dict[str, str]] = None,
) -> RouteTargetResolver:
    occupied = occupied or {}
    blocks = {bid: _Block(occupied_by=occupied.get(bid)) for bid in dirs_by_block}
    network = _Network(blocks)
    return RouteTargetResolver(_Executor(network, _DirDispatcher(dirs_by_block)))


def _stop_dir(bid: str, direction: str) -> RouteTask:
    return RouteTask(
        task_kind="stop", task_location=BlockTarget(bid), task_direction=direction
    )


def test_nearest_skips_stop_not_viable_in_required_direction():
    # A is nearest by track, but its task requires `forward` and A is reachable
    # only going `reverse` — so A is not a viable start. B (farther, reachable
    # forward) is the nearest *viable* stop, so the ladder falls through to it.
    resolver = _make_dir_resolver(
        {
            "A": {"forward": None, "reverse": (2, 20.0)},
            "B": {"forward": (5, 50.0), "reverse": None},
        }
    )
    route = Route(
        route_id="r", tasks=[_stop_dir("A", "forward"), _stop_dir("B", "forward")]
    )
    train = SimpleNamespace(train_id="T1")
    assert resolver.nearest_start_stop_index(train, route) == 1


def test_nearest_either_stop_viable_in_reverse_is_eligible():
    # An `either` task is viable as long as *some* direction reaches it. A is
    # reachable only in reverse; with `either` it stays eligible (and nearest),
    # so it still wins over the farther B.
    resolver = _make_dir_resolver(
        {
            "A": {"forward": None, "reverse": (2, 20.0)},
            "B": {"forward": (5, 50.0), "reverse": None},
        }
    )
    route = Route(
        route_id="r", tasks=[_stop_dir("A", "either"), _stop_dir("B", "either")]
    )
    train = SimpleNamespace(train_id="T1")
    assert resolver.nearest_start_stop_index(train, route) == 0


def test_nearest_none_when_only_forbidden_direction_reachable():
    # Every stop is reachable only against its required direction → no viable
    # stop → fall back to FIRST (None).
    resolver = _make_dir_resolver(
        {
            "A": {"forward": None, "reverse": (2, 20.0)},
            "B": {"forward": None, "reverse": (5, 50.0)},
        }
    )
    route = Route(
        route_id="r", tasks=[_stop_dir("A", "forward"), _stop_dir("B", "forward")]
    )
    train = SimpleNamespace(train_id="T1")
    assert resolver.nearest_start_stop_index(train, route) is None


# ---------------------------------------------------------------------------
# nearest_start_stop_index — reverse penalty reads the *directed* arrival walk
# ---------------------------------------------------------------------------


class _DirWalkDispatcher:
    """`find_paths` keyed by `(start_block_id, end_block_id, direction_constraint)`
    → a `(blocks, tile_count, distance_cm)` triple, or `None`/absent for an
    unreachable lookup.

    Unlike `_WalkDispatcher`, a stop can be reached through *different blocks*
    depending on the block-relative direction, which is what separates a
    directed arrival walk from an undirected one: when a stop's forbidden
    opposite direction has a shorter walk, the two disagree on the
    arrived-through block (`blocks[-2]`)."""

    def __init__(
        self,
        walks: Dict[Tuple[str, str, Optional[str]], Optional[Tuple[List[str], int, float]]],
    ) -> None:
        self._w = walks

    def find_paths(
        self,
        start_block_id: str,
        end_block_id: str,
        train_id: Optional[str] = None,
        direction_constraint: Optional[str] = None,
    ) -> List[Any]:
        m = self._w.get((start_block_id, end_block_id, direction_constraint))
        if not m:
            return []
        return [_WalkSeg(*m)]


def _make_dir_walk_resolver(
    walks: Dict[Tuple[str, str, Optional[str]], Optional[Tuple[List[str], int, float]]],
    blocks: List[str],
) -> RouteTargetResolver:
    blk = {bid: _Block() for bid in blocks}
    network = _Network(blk)
    return RouteTargetResolver(_Executor(network, _DirWalkDispatcher(walks)))


def test_nearest_reverse_penalty_reads_directed_arrival_not_shorter_forbidden_walk():
    """The reverse penalty must be read off the stop's *directed* arrival walk
    (the block-relative direction its task requires), not an undirected walk
    that could approach through the forbidden — and here shorter — opposite
    direction.

    Stop A's required FORWARD approach is 2 tiles through MID; its forbidden
    REVERSE approach is a shorter 1 tile through SIDE. A's onward move A→C
    re-enters MID. So with the directed arrival walk:
      * required = "forward": arrives through MID, onward re-enters MID →
        A is a reverse stop. The farther-but-drive-on C must be seeded.
      * required = "reverse": arrives through SIDE, onward leaves through MID
        (a different block) → A is drive-on, and now also the nearest, so A
        is seeded.
    Same geometry, opposite task direction, opposite seed — which only holds
    when the arrival walk is constrained to the task's direction. An undirected
    arrival walk would always take the shorter SIDE approach and call A drive-on
    regardless of direction (the `(.., .., None)` entry below), which would seed
    A in both cases and fail the forward assertion.
    """
    walks = {
        ("START", "A", "forward"): (["START", "MID", "A"], 2, 20.0),
        ("START", "A", "reverse"): (["START", "SIDE", "A"], 1, 10.0),
        # What an undirected arrival walk (the pre-fix behaviour) returns: the
        # shorter reverse approach through SIDE. Present so a regression to
        # undirected find_paths flips the forward case and fails this test.
        ("START", "A", None): (["START", "SIDE", "A"], 1, 10.0),
        ("START", "C", "forward"): (["START", "N", "C"], 5, 50.0),
        ("A", "C", None): (["A", "MID", "C"], 2, 20.0),
    }
    train = SimpleNamespace(train_id="T1")

    fwd = _make_dir_walk_resolver(walks, blocks=["A", "C"])
    route_fwd = Route(
        route_id="r", tasks=[_stop_dir("A", "forward"), _stop_dir("C", "forward")]
    )
    # A is a reverse stop via its directed forward arrival → drive-on C wins.
    assert fwd.nearest_start_stop_index(train, route_fwd) == 1

    rev = _make_dir_walk_resolver(walks, blocks=["A", "C"])
    route_rev = Route(
        route_id="r", tasks=[_stop_dir("A", "reverse"), _stop_dir("C", "forward")]
    )
    # A is drive-on via its directed reverse arrival, and nearest → A wins.
    assert rev.nearest_start_stop_index(train, route_rev) == 0


# ---------------------------------------------------------------------------
# _seed_start_position (allocation-time seeding)
# ---------------------------------------------------------------------------


def _seed_executor() -> RouteExecutor:
    return RouteExecutor(network=SimpleNamespace(blocks={}, trains={}))


def _seeded_run(ex: RouteExecutor, route: Route) -> RouteExecutionState:
    es = RouteExecutionState(train_id="T1", route_id=route.route_id, started_at=100.0)
    ex._sync_task_runs(route, es)
    ex._seed_start_position(SimpleNamespace(train_id="T1"), route, es)
    return es


def _route(mode=RouteMode.ONE_OFF, start_mode=RouteStartMode.FIRST, idx=None, n=4) -> Route:
    return Route(
        route_id="r1",
        tasks=[_stop(f"b{i}") for i in range(n)],
        mode=mode,
        start_mode=start_mode,
        start_task_index=idx,
    )


def test_seed_first_does_not_skip():
    es = _seeded_run(_seed_executor(), _route(start_mode=RouteStartMode.FIRST))
    assert es.next_planning_index == 0
    assert all(t.finished_at is None for t in es.task_runs)
    assert es.start_mode == RouteStartMode.FIRST


def test_seed_specific_pre_finishes_prefix():
    es = _seeded_run(_seed_executor(), _route(start_mode=RouteStartMode.SPECIFIC, idx=2))
    assert [t.finished_at is not None for t in es.task_runs] == [True, True, False, False]
    assert es.next_planning_index == 2
    assert es.current_waypoint_index == 1
    # Seeded prefix carries the run's started_at and no delay_class.
    assert es.task_runs[0].finished_at == 100.0 and es.task_runs[0].delay_class is None
    assert es.start_mode == RouteStartMode.SPECIFIC


def test_seed_specific_out_of_range_falls_back_to_first():
    es = _seeded_run(_seed_executor(), _route(start_mode=RouteStartMode.SPECIFIC, idx=9))
    assert es.next_planning_index == 0 and es.start_mode == RouteStartMode.FIRST


def test_seed_specific_non_stop_falls_back_to_first():
    route = _route(start_mode=RouteStartMode.SPECIFIC, idx=1)
    route.tasks[1] = RouteTask(task_kind="waypoint", task_location=BlockTarget("bx"))
    es = _seeded_run(_seed_executor(), route)
    assert es.next_planning_index == 0 and es.start_mode == RouteStartMode.FIRST


def test_pending_override_beats_route_default_and_is_one_shot():
    ex = _seed_executor()
    route = _route(start_mode=RouteStartMode.FIRST)  # route default: FIRST
    ex._pending_start_overrides["T1"] = {
        "start_mode": RouteStartMode.SPECIFIC,
        "start_task_index": 3,
    }
    es = _seeded_run(ex, route)
    assert es.next_planning_index == 3 and es.start_mode == RouteStartMode.SPECIFIC
    assert "T1" not in ex._pending_start_overrides  # consumed (one-shot)

    # A second fresh run with no pending input falls back to the route default.
    es2 = _seeded_run(ex, route)
    assert es2.next_planning_index == 0 and es2.start_mode == RouteStartMode.FIRST


def test_seed_nearest_delegates_to_resolver(monkeypatch):
    ex = _seed_executor()
    route = _route(start_mode=RouteStartMode.NEAREST)
    # Stub the resolver's nearest lookup so this stays a pure seeding test.
    resolver = ex._get_route_target_resolver()
    monkeypatch.setattr(resolver, "nearest_start_stop_index", lambda train, r: 2)
    es = _seeded_run(ex, route)
    assert es.next_planning_index == 2 and es.start_mode == RouteStartMode.NEAREST


def test_seed_nearest_no_reachable_stop_falls_back_to_first(monkeypatch):
    ex = _seed_executor()
    route = _route(start_mode=RouteStartMode.NEAREST)
    resolver = ex._get_route_target_resolver()
    monkeypatch.setattr(resolver, "nearest_start_stop_index", lambda train, r: None)
    es = _seeded_run(ex, route)
    assert es.next_planning_index == 0 and es.start_mode == RouteStartMode.FIRST


def test_round_trip_specific_apex_start_reflects_correctly():
    """A SPECIFIC start at the apex (last task) of a ROUND_TRIP route keeps
    direction 'forward' until the apex completes, then reflects to task_count-2
    and flips to 'reverse' via the normal append/advance flow."""
    ex = _seed_executor()
    route = _route(mode=RouteMode.ROUND_TRIP, start_mode=RouteStartMode.SPECIFIC, idx=3, n=4)
    es = _seeded_run(ex, route)
    assert es.next_planning_index == 3 and es.direction == "forward"

    # Mimic the real apex-completion flow (route_executor.py:1314-1316):
    es.task_runs[3].finished_at = 200.0
    es._append_next_walk_task(route)
    ex._advance_to_next_task(route, es)

    assert es.task_runs[-1].task_index == 2  # reflected to task_count - 2
    assert es.direction == "reverse"
    assert es.next_planning_index == 2
