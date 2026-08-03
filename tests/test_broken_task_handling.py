"""Tests for "Unreachable and broken task handling" (design_new.md) and the
force-reassign cascade used by the Station Editor panel.

Covers:
- ``RouteTargetResolver.task_target_present`` — the broken (missing target) vs
  valid (present target) test.
- ``RouteExecutor._walk_has_valid_task`` — ONE_OFF suffix scan vs
  CIRCULAR/ROUND_TRIP whole-route scan.
- ``RouteExecutor._skip_broken_task`` — records last_error, sets finished_at,
  appends the next walk task.
- Force-reassign: ``create_station_group(force=True)`` /
  ``add_platform_to_station_group(force=True)`` detach a platform from its old
  station, auto-deleting the old station when it becomes empty (Non-empty
  parent rule); ``force=False`` still raises.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_broken_task_handling.py -v
"""

import pytest

from network import RailwayNetwork
from track import Tile
from train_route import (
    BlockTarget,
    PlatformTarget,
    Route,
    RouteMode,
    RouteTask,
    RouteTaskRun,
    StationTarget,
)
from route_executor import RouteExecutor, RouteExecutionState
from route_target_resolver import RouteTargetResolver


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _net() -> RailwayNetwork:
    """A network with three platforms; STA_A=[PLT_A], STA_B=[PLT_B], PLT_C free."""
    net = RailwayNetwork()
    for x in range(8):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight
    net.add_platform(0, 0, platform_id="PLT_A", name="A")
    net.add_platform(2, 0, platform_id="PLT_B", name="B")
    net.add_platform(4, 0, platform_id="PLT_C", name="C")
    net.create_station_group(station_id="STA_A", name="SA", platform_ids=["PLT_A"])
    net.create_station_group(station_id="STA_B", name="SB", platform_ids=["PLT_B"])
    return net


def _stop(station_id: str) -> RouteTask:
    return RouteTask(task_kind="stop", task_location=StationTarget(station_id))


def _circular(*station_ids: str) -> Route:
    route = Route(
        route_id="R",
        name="r",
        mode=RouteMode.CIRCULAR,
        tasks=[_stop(s) for s in station_ids],
    )
    return route


def _exec_state(route: Route, finished_prefix: int = 0) -> RouteExecutionState:
    runs = []
    for i in range(len(route.tasks)):
        runs.append(
            RouteTaskRun(task_index=i, finished_at=(1.0 if i < finished_prefix else None))
        )
    return RouteExecutionState(
        train_id="T", route_id=route.route_id, started_at=0.0, task_runs=runs
    )


# --------------------------------------------------------------------------- #
# task_target_present (broken vs valid)
# --------------------------------------------------------------------------- #


class TestTaskTargetPresent:
    def test_present_station_platform_block(self):
        net = _net()
        resolver = RouteTargetResolver(RouteExecutor(net))
        assert resolver.task_target_present(_stop("STA_A")) is True
        assert (
            resolver.task_target_present(
                RouteTask(task_kind="stop", task_location=PlatformTarget("PLT_A"))
            )
            is True
        )
        existing_block = next(iter(net.blocks))
        assert (
            resolver.task_target_present(
                RouteTask(task_kind="waypoint", task_location=BlockTarget(existing_block))
            )
            is True
        )

    def test_missing_targets_are_not_present(self):
        net = _net()
        resolver = RouteTargetResolver(RouteExecutor(net))
        assert resolver.task_target_present(_stop("NOPE")) is False
        assert (
            resolver.task_target_present(
                RouteTask(task_kind="stop", task_location=PlatformTarget("NOPE"))
            )
            is False
        )
        assert (
            resolver.task_target_present(
                RouteTask(task_kind="waypoint", task_location=BlockTarget("NOPE"))
            )
            is False
        )

    def test_disabled_station_is_still_present(self):
        """A disabled/manual target exists -> present (it is unreachable, not
        broken; the train waits rather than skipping)."""
        net = _net()
        net.station_groups["STA_A"].disabled = True
        resolver = RouteTargetResolver(RouteExecutor(net))
        assert resolver.task_target_present(_stop("STA_A")) is True


# --------------------------------------------------------------------------- #
# _walk_has_valid_task
# --------------------------------------------------------------------------- #


class TestWalkHasValidTask:
    def test_circular_scans_whole_route(self):
        net = _net()
        ex = RouteExecutor(net)
        route = _circular("STA_A", "STA_B")
        exec_state = _exec_state(route)
        assert ex._walk_has_valid_task(exec_state, route, 0) is True

        # One broken, one valid -> still has a valid task.
        net.delete_station_group("STA_A")
        assert ex._walk_has_valid_task(exec_state, route, 0) is True

        # All broken -> no valid task.
        net.delete_station_group("STA_B")
        assert ex._walk_has_valid_task(exec_state, route, 0) is False

    def test_one_off_scans_only_suffix(self):
        net = _net()
        ex = RouteExecutor(net)
        route = Route(
            route_id="R",
            name="r",
            mode=RouteMode.ONE_OFF,
            tasks=[_stop("STA_A"), _stop("STA_B")],
        )
        exec_state = _exec_state(route, finished_prefix=1)  # task 0 done, at task 1
        assert ex._walk_has_valid_task(exec_state, route, 1) is True

        # Delete STA_B (the suffix target). STA_A is behind the cursor and is
        # NOT scanned for a ONE_OFF walk, so the suffix has no valid task.
        net.delete_station_group("STA_B")
        assert ex._walk_has_valid_task(exec_state, route, 1) is False


# --------------------------------------------------------------------------- #
# _skip_broken_task
# --------------------------------------------------------------------------- #


class TestSkipBrokenTask:
    def test_skip_records_error_and_finishes(self):
        net = _net()
        ex = RouteExecutor(net)
        route = _circular("STA_A", "STA_B")
        # tr0 already done; tr1 is the current (last) walk-position being skipped
        # — the canonical append fires only once the latest position finishes.
        exec_state = _exec_state(route, finished_prefix=1)
        before = len(exec_state.task_runs)

        ex._skip_broken_task(exec_state, route, 1)

        assert exec_state.task_runs[1].finished_at is not None
        # The diagnostic is an event, not entity state (design_new.md
        # "Error events"): no `RouteTaskRun` field holds an `ErrorRecord`.
        assert not hasattr(exec_state.task_runs[1], "last_error")
        # CIRCULAR appends the next walk task per the canonical append rule.
        assert len(exec_state.task_runs) == before + 1

    def test_skip_one_off_does_not_append(self):
        net = _net()
        ex = RouteExecutor(net)
        route = Route(
            route_id="R",
            name="r",
            mode=RouteMode.ONE_OFF,
            tasks=[_stop("STA_A"), _stop("STA_B")],
        )
        exec_state = _exec_state(route)
        before = len(exec_state.task_runs)

        ex._skip_broken_task(exec_state, route, 0)

        assert exec_state.task_runs[0].finished_at is not None
        assert len(exec_state.task_runs) == before  # ONE_OFF never appends


# --------------------------------------------------------------------------- #
# Force-reassign cascade (Station Editor backend)
# --------------------------------------------------------------------------- #


class TestForceReassign:
    def test_create_force_reassign_deletes_emptied_station(self):
        net = _net()  # STA_B = [PLT_B] (single platform)
        net.create_station_group(
            station_id="STA_C", name="SC", platform_ids=["PLT_B"], force=True
        )
        assert net.station_groups["STA_C"].platform_ids == ["PLT_B"]
        assert "STA_B" not in net.station_groups  # emptied -> auto-deleted

    def test_create_no_force_raises(self):
        net = _net()
        with pytest.raises(ValueError):
            net.create_station_group(
                station_id="STA_C", name="SC", platform_ids=["PLT_B"], force=False
            )
        assert "STA_C" not in net.station_groups
        assert "STA_B" in net.station_groups  # untouched

    def test_create_force_keeps_multiplatform_station(self):
        net = _net()
        net.add_platform_to_station_group("STA_A", "PLT_C")  # STA_A = [PLT_A, PLT_C]
        net.create_station_group(
            station_id="STA_C", name="SC", platform_ids=["PLT_A"], force=True
        )
        assert "STA_A" in net.station_groups  # survives (still owns PLT_C)
        assert net.station_groups["STA_A"].platform_ids == ["PLT_C"]
        assert net.station_groups["STA_C"].platform_ids == ["PLT_A"]

    def test_add_platform_force_reassign(self):
        net = _net()
        net.add_platform_to_station_group("STA_A", "PLT_B", force=True)
        assert "PLT_B" in net.station_groups["STA_A"].platform_ids
        assert "STA_B" not in net.station_groups  # emptied -> deleted

    def test_add_platform_no_force_raises(self):
        net = _net()
        with pytest.raises(ValueError):
            net.add_platform_to_station_group("STA_A", "PLT_B", force=False)
        assert "PLT_B" not in net.station_groups["STA_A"].platform_ids
        assert "STA_B" in net.station_groups
