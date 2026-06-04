"""Tests for configurable route-run start mode (design_new.md "Run start-mode
seeding").

Covers:
  * `RouteStartMode` enum + `Route` serialization round-trip / defaults.
  * `RouteTargetResolver.nearest_start_stop_index(...)` — nearest stop by track
    distance, occupancy skip + fallback ladder, tie-break, unreachable.
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
    def __init__(self, block_section_id: str) -> None:
        self.block_section_id = block_section_id


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


def test_nearest_skips_stop_occupied_by_other_train():
    resolver = _make_nearest_resolver(
        {"A": (5, 50.0), "B": (2, 20.0), "C": (4, 40.0)},
        occupied={"B": "OTHER"},  # nearest, but occupied by a different train
    )
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B"), _stop("C")])
    train = SimpleNamespace(train_id="T1")
    # B is closest but occupied by OTHER → skip to the next non-occupied: C (idx 2).
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


def test_nearest_all_occupied_falls_back_to_nearest():
    resolver = _make_nearest_resolver(
        {"A": (5, 50.0), "B": (2, 20.0)},
        occupied={"A": "OTHER", "B": "OTHER"},
    )
    route = Route(route_id="r", tasks=[_stop("A"), _stop("B")])
    train = SimpleNamespace(train_id="T1")
    # Every reachable stop is occupied → fall back to the nearest regardless: B.
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
