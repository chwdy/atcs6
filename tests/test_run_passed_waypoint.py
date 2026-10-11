"""A `waypoint` the train drives past while an earlier `run`'s timer is still
running must not strand the route walk.

design_new.md "Covered-task index advance": a `run` reserves toward its own
direction-seed target and only arms the following task once the authority
reaches that seed, so when the seed lies beyond an intervening `waypoint` the
head rolls past that waypoint while the `run` is still the next-non-finished
task. Head-entry (step `640a`) will not finish the waypoint then — the `run`,
not the waypoint, is the next task — and once the `run` finishes and the walk
reaches the waypoint, the train has already cleared its block, so the
covered-waypoint span test can no longer see it. Coverage must finish such a
"passed-behind" waypoint, or the walk stalls on a target behind the train
forever.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime
from track import TRACK_EW, Tile
from tests.segment_test_helpers import make_segment_block_with_tracks
from train_route import BlockTarget, Route, RouteMode, RouteTask

_N_BLOCKS = 9
_BLK = [f"B{i}" for i in range(_N_BLOCKS)]


def _linear_scene():
    """A straight west-to-east line of contiguous two-tile blocks, one train at
    the west end facing east, tuned to accelerate briskly so the run covers real
    ground within a modest tick budget."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, _pd, sim, _persist = bootstrap_runtime(demo=False, setup_train=False)
    for i in range(_N_BLOCKS):
        for dx in range(2):
            network.add_track(Tile(2 * i + dx, 0, track_bits=TRACK_EW))
    for i in range(_N_BLOCKS):
        make_segment_block_with_tracks(
            network,
            block_id=_BLK[i],
            positions=[(2 * i, 0), (2 * i + 1, 0)],
            name=_BLK[i],
            length_cm=40.0,
            speed_limit_kmh=40.0,
        )
    sim.add_train(
        train_id="T",
        starting_block_id=_BLK[0],
        initial_direction="EAST",
        max_speed_kmh=40.0,
        length_cm=15.0,
        initial_cab="A",
    )
    profile = network.speed_profiles.get(network.trains["T"].speed_profile_id)
    profile.power_speed_curve = [(0.0, 0.0), (100.0, 40.0)]
    profile.acceleration_rate = 500.0
    profile.deceleration_rate = 500.0
    profile.emergency_brake_rate = 800.0
    return network, sim


def _drive(sim, tasks, max_ticks=2500):
    ex = sim.route_executor
    route = Route(route_id="R", name="run-wp", mode=RouteMode.ONE_OFF)
    for task in tasks:
        route.add_task(task)
    ex.register_route(route)
    route.assign_train("T")
    controller = sim.get_or_create_controller("T")
    controller.commands.enqueue_command("power_on")
    controller.commands.enqueue_command("set_mode", {"mode": "ato"})
    controller.commands.enqueue_command("ato_go")

    run_finished = False
    waypoint_finished = False
    route_finished = False
    saw_run = False
    for _ in range(max_ticks):
        sim.update(0.2)
        es = ex.execution_states.get("T")
        if es is None:
            if not saw_run:
                # The RouteRun is allocated on a later tick than assignment;
                # give it a few ticks before treating an empty slot as terminal.
                continue
            # A ONE_OFF run that finishes is deleted from the slot.
            route_finished = True
            break
        saw_run = True
        for r in es.task_runs:
            kind = route.tasks[r.task_index].task_kind
            if r.finished_at is not None:
                if kind == "run":
                    run_finished = True
                elif kind == "waypoint":
                    waypoint_finished = True
    return run_finished, waypoint_finished, route_finished


@pytest.mark.parametrize("run_seed,waypoint_block,stop_block", [
    ("B7", "B3", "B8"),   # seed far past the waypoint — the reproduced stall
    ("B6", "B4", "B8"),   # a second passed-behind arrangement
])
def test_a_run_passed_waypoint_does_not_stall_the_walk(run_seed, waypoint_block, stop_block):
    _network, sim = _linear_scene()
    run_finished, waypoint_finished, route_finished = _drive(sim, [
        RouteTask(task_kind="run", task_location=BlockTarget(block_id=run_seed),
                  task_time=6.0, task_direction="forward"),
        RouteTask(task_kind="waypoint", task_location=BlockTarget(block_id=waypoint_block),
                  task_direction="forward"),
        RouteTask(task_kind="stop", task_location=BlockTarget(block_id=stop_block),
                  task_direction="forward"),
    ])
    assert run_finished, "the run's timer must elapse and the run must complete"
    assert waypoint_finished, (
        "the waypoint the train drove past during the run must be finished by "
        "coverage — not left unfinished behind the train"
    )
    assert route_finished, (
        "the route must reach its final stop; a passed-behind waypoint must not "
        "stall the walk on a target behind the train"
    )


def test_a_waypoint_within_the_runs_reach_still_completes_normally():
    """Control: when the run's seed is short of the waypoint, the run's lookahead
    arms the waypoint and the train reaches it head-on, so it completes through
    the ordinary in-span coverage / head-entry path. Guards against the fix
    over-finishing a waypoint the train has genuinely not passed."""
    _network, sim = _linear_scene()
    run_finished, waypoint_finished, route_finished = _drive(sim, [
        RouteTask(task_kind="run", task_location=BlockTarget(block_id="B2"),
                  task_time=6.0, task_direction="forward"),
        RouteTask(task_kind="waypoint", task_location=BlockTarget(block_id="B5"),
                  task_direction="forward"),
        RouteTask(task_kind="stop", task_location=BlockTarget(block_id="B8"),
                  task_direction="forward"),
    ])
    assert run_finished and waypoint_finished and route_finished
