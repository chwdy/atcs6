"""Regression: NEAREST seeding prefers no-reverse stops and de-conflicts trains.

The four NW-Yard branch trains (`T_NWY_1..4`) start packed in adjacent yard
roads. Before the no-reverse + de-conflict seeding rule (design_new.md "Run
start-mode seeding") they all resolved to the same stop, `PLT_BR_4_A` — a
*reverse* stop, because the branch route's next stop after it (`PLT_BR_5_A`)
is back the way the train came, so the train must turn around in place. Two
trains aimed at one reverse stop deadlock (one turns back into the follower)
and the follower is left stuck in the yard.

They must now seed to distinct *drive-on* stops on the B-leg, where the route
continues forward without a reversal. The nearest free drive-on stop is
`PLT_BR_4_B`; the rest spread to `PLT_BR_3_B`, `PLT_BR_2_B`, `PLT_BR_1_B`.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime
from train_route import StationTarget

pytestmark = pytest.mark.scene("demo_6")

_NW_TRAINS = ["T_NWY_1", "T_NWY_2", "T_NWY_3", "T_NWY_4"]


def _seed_branch_trains():
    """Build demo_6 and drive run-seeding for the four NW-Yard branch trains in
    order, returning `{train_id: platform_id}` for each train's seeded stop."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, _disp, sim, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
    ex = sim.route_executor
    route = ex.route_registry.get("ROUTE_BRANCH")
    assert route is not None, "demo_6 must register ROUTE_BRANCH"
    seeded = {}
    for tid in _NW_TRAINS:
        train = network.trains.get(tid)
        assert train is not None, f"demo_6 must place {tid}"
        exec_state = ex._ensure_run_for(train, route)
        idx = exec_state.next_planning_index
        seeded[tid] = route.tasks[idx].task_location.platform_id
    return seeded


def test_nw_yard_branch_trains_seed_to_distinct_drive_on_stops():
    seeded = _seed_branch_trains()
    platforms = list(seeded.values())

    # De-confliction: no two NW trains seed to the same platform.
    assert len(set(platforms)) == len(platforms), seeded

    # No-reverse preference: every seeded stop is a B-leg (drive-on) platform,
    # never an A-leg reverse stop like the old PLT_BR_4_A.
    assert all(p.endswith("_B") for p in platforms), seeded

    # The exact spread: the nearest free drive-on stop and the next three.
    assert set(platforms) == {
        "PLT_BR_4_B",
        "PLT_BR_3_B",
        "PLT_BR_2_B",
        "PLT_BR_1_B",
    }, seeded

    # The first-seeded (nearest) train lands on PLT_BR_4_B specifically.
    assert seeded["T_NWY_1"] == "PLT_BR_4_B", seeded


def test_demo_routes_have_no_broken_stops():
    """Every stop in the demo routes resolves to a real target.

    Regression: `MAIN_ROUTE_STOPS` lists the M1 turnaround as the station group
    `STN_ML_1`, but `_build_route` used to wrap every id as a `PlatformTarget`.
    A `PlatformTarget("STN_ML_1")` points at a platform that does not exist, so
    the resolver flags the task `MISSING_TARGET` (broken) and Phase 1 skips it
    every lap with a 0 s dwell — the train never serves M1. Station ids must
    become `StationTarget`s so the stop resolves to one of the group's
    platforms. See "Unreachable and broken task handling" in design_new.md.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        network, _disp, sim, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
    ex = sim.route_executor
    resolver = ex._get_route_target_resolver()

    for route_id in ("ROUTE_MAIN", "ROUTE_BRANCH"):
        route = ex.route_registry.get(route_id)
        assert route is not None, f"demo_6 must register {route_id}"
        for i, task in enumerate(route.tasks):
            assert resolver.task_target_present(task), (
                f"{route_id} task {i} ({task.task_location}) is a broken task — "
                f"its target id is absent from the network, so it is skipped"
            )
            block, reason = resolver.resolve_task_target_block_detailed(task)
            assert block is not None, f"{route_id} task {i}: {reason}"

    # The M1 turnaround specifically: a StationTarget that resolves to one of
    # the station's two platforms (either-platform service).
    main = ex.route_registry.get("ROUTE_MAIN")
    t0 = main.tasks[0]
    assert isinstance(t0.task_location, StationTarget), t0.task_location
    assert t0.task_location.station_id == "STN_ML_1", t0.task_location
    block, _reason = resolver.resolve_task_target_block_detailed(t0)
    assert block in ("PLT_ML_1_A_BLOCK", "PLT_ML_1_B_BLOCK"), block
