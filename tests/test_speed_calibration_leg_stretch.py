"""A calibration leg runs through the measured block, with the points as set.

design_new.md "Speed calibration", Calibration leg: the leg's one-off route names
the measured block as a `waypoint` and the far outer block as its `stop`, each
with the direction the stretch crosses it in, so its path runs out of one outer
block, through the measured block, and into the other, and leaves every switch
where it is set.

Regression for the 2026-10-30 rig run on this scene's track plan (an oval with a
passing siding). The leg used to name only the far outer block, so the path
planner was free to reach it the other way round: measuring Up from Right, it
drove Right -> SW_11_9 -> Sideline -> SW_7_5 -> Left, threw both switches to
diverge, and never entered Up, so no crossing could ever be timed.
"""

from __future__ import annotations

import contextlib
import io
import threading
from datetime import datetime

import pytest

from app_state import AppState
from bootstrap import bootstrap_runtime
from services import train_service
from speed_calibration import PHASE_FAILED

pytestmark = pytest.mark.scene("demo7_1")

TRAIN = "T"
RIGHT = "BLK_4F4CF0"
UP = "BLK_4F5CE3"
MAIN = "BLK_4F6CD7"
SIDELINE = "BLK_4F7A2F"
LEFT = "BLK_C37589A5"


@pytest.fixture
def state():
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, persistence = bootstrap_runtime(
            demo=True, scene="demo7_1", setup_train=False
        )
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=persistence,
        simulation_lock=threading.Lock(),
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def _place(state, tile, direction):
    train_service.create_train(
        state,
        train_id=TRAIN,
        length_cm=10.0,
        position={"x": tile[0], "y": tile[1]},
        direction=direction,
    )
    state.sim_controller.update(0.1)


def _switch_positions(state):
    return {sid: int(sw.position) for sid, sw in state.network.switches.items()}


def _leg_path_blocks(state, ticks=10):
    """The blocks the leg's path reserves, in order, once it reaches the far
    outer block. The path first runs to the measured block (the waypoint) and
    grows to the far outer block as soon as it reaches it."""
    sim = state.sim_controller
    controller = sim.get_or_create_controller(TRAIN)
    blocks = []
    for _ in range(ticks):
        sim.update(0.1)
        plan = controller.path_projection.received_path_plan
        if plan is None:
            continue
        blocks = [
            e.resource_id for e in plan.resource_sequence if e.resource_type == "block"
        ]
    return blocks


def test_a_leg_across_up_runs_through_up_and_throws_no_points(state):
    _place(state, (9, 13), "WEST")
    before = _switch_positions(state)

    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    blocks = _leg_path_blocks(state)

    assert run.phase != PHASE_FAILED, run.failure_reason
    assert blocks == [RIGHT, UP, LEFT]
    assert _switch_positions(state) == before

    leg_route = state.sim_controller.route_executor.route_registry[
        run.active_leg_route_id
    ]
    kinds = [(t.task_kind, t.task_location.block_id) for t in leg_route.tasks]
    assert kinds == [("waypoint", UP), ("stop", LEFT)]
    assert all(t.task_direction in ("forward", "reverse") for t in leg_route.tasks)


def test_a_leg_across_main_keeps_off_the_sideline(state):
    """Main and Sideline both join Left to Right; with the points set for Main,
    the leg must not take the Sideline and throw both switches."""
    assert set(_switch_positions(state).values()) == {0}
    _place(state, (6, 4), "EAST")
    held = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert LEFT in held.occupied_blocks

    run = train_service.calibrate_train(state, TRAIN, measured_block_id=MAIN)
    blocks = _leg_path_blocks(state)

    assert run.phase != PHASE_FAILED, run.failure_reason
    assert blocks == [LEFT, MAIN, RIGHT]
    assert set(_switch_positions(state).values()) == {0}


def test_a_stretch_no_leg_can_drive_as_set_fails_the_pass(state):
    """When no path runs from the outer block through the measured block into the
    other one with the points as they are set, the pass fails and says so rather
    than sending the train another way."""
    _place(state, (9, 13), "WEST")
    before = _switch_positions(state)
    # A `manual` block is out of reach of every automatic path, the leg's too.
    state.network.blocks[UP].manual = True

    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    _leg_path_blocks(state, ticks=3)

    assert run.phase == PHASE_FAILED
    assert "through" in run.failure_reason
    assert state.sim_controller.speed_calibration.runs.get(TRAIN) is None
    assert state.path_dispatcher.active_path_id_by_train(TRAIN) is None
    assert _switch_positions(state) == before


def _run_pass(state, run, *, until_step, ticks=9000):
    sim = state.sim_controller
    for _ in range(ticks):
        sim.update(0.1)
        if not run.is_measuring or run.step_index >= until_step:
            return


def test_every_step_is_timed_in_both_directions(state):
    """design_new.md "Speed calibration", The pass turns the train around at each
    end: the train drives on until it is off the measured block before it stops,
    so the leg back enters that block afresh and is timed too. A 10 cm train
    braked the moment it entered the far block used to stop with its tail on Up,
    so every leg back went untimed and the slow steps read as a stall."""
    from constants import CALIBRATION_ROUND_TRIPS

    _place(state, (9, 13), "WEST")
    before = _switch_positions(state)
    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)

    headings = []
    original_record = state.sim_controller.speed_calibration.record_detected_entries

    def record(r, entered, now):
        if r.crossing_started_at is not None and r.heading_toward_block_id in entered:
            headings.append(r.heading_toward_block_id)
        return original_record(r, entered, now)

    state.sim_controller.speed_calibration.record_detected_entries = record
    _run_pass(state, run, until_step=3)

    assert run.phase != PHASE_FAILED, run.failure_reason
    assert [s.power_percent for s in run.samples] == [10.0, 20.0, 30.0]
    # The default profile's curve is linear, so each step reads its own percent.
    for sample in run.samples:
        assert sample.speed_kmh == pytest.approx(sample.power_percent, rel=0.05)
    # Every crossing of every step was timed, half each way.
    assert len(headings) == 3 * 2 * CALIBRATION_ROUND_TRIPS
    assert headings.count(LEFT) == headings.count(RIGHT)
    assert _switch_positions(state) == before


def test_a_pass_will_not_open_with_the_train_on_the_measured_block(state):
    """A leg is timed from the train's entry into the measured block, so a train
    already on it cannot start one (design_new.md "Speed calibration", The train
    must be standing in one of the two outer blocks)."""
    from services import ConflictError

    # Head in Right, body trailing back onto Up: the span covers both.
    _place(state, (9, 13), "EAST")
    span = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert {RIGHT, UP} <= set(span.occupied_blocks)

    unmet = train_service.calibration_readiness(state, TRAIN, measured_block_id=UP)
    assert any(f"off {UP}" in u for u in unmet)
    with pytest.raises(ConflictError):
        train_service.calibrate_train(state, TRAIN, measured_block_id=UP)


def test_a_pass_will_not_open_while_the_train_still_holds_the_measured_block(state):
    """The span has moved off the measured block but its detector has not read
    clear yet, so the train's block list still holds it."""
    _place(state, (9, 13), "WEST")
    span = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert UP not in span.occupied_blocks
    state.network.blocks[UP].occupied_by = TRAIN

    unmet = train_service.calibration_readiness(state, TRAIN, measured_block_id=UP)
    assert any(f"off {UP}" in u for u in unmet)
