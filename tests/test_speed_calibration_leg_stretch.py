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


def _walk_blocks(keys):
    blocks = []
    for resource_type, resource_id, _entry, _exit in keys:
        if resource_type == "block" and (not blocks or blocks[-1] != resource_id):
            blocks.append(resource_id)
    return blocks


def test_the_leg_route_carries_the_walk_it_was_checked_on(state):
    """design_new.md Planning Kernel step `66aa`, Required walk: the leg route
    stores the checked walk as `Route.required_walk_keys`, and every
    reservation for the leg follows it."""
    _place(state, (9, 13), "WEST")
    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    state.sim_controller.update(0.1)

    leg_route = state.sim_controller.route_executor.route_registry[
        run.active_leg_route_id
    ]
    assert _walk_blocks(leg_route.required_walk_keys) == [RIGHT, UP, LEFT]


def test_a_leg_that_loses_its_walk_fails_the_pass(state):
    """design_new.md "Speed calibration", A leg that loses its walk fails the
    pass: the measured block is made `manual` after the leg armed and before its
    path was acquired, so no walk lies on the leg's way any more. The pass ends
    at once rather than waiting with no step timeout running."""
    _place(state, (9, 13), "WEST")
    before = _switch_positions(state)
    rex = state.sim_controller.route_executor
    original_arm = rex._arm_calibration_leg

    def arm_then_lose_the_walk(*args, **kwargs):
        original_arm(*args, **kwargs)
        state.network.blocks[UP].manual = True

    rex._arm_calibration_leg = arm_then_lose_the_walk
    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    _leg_path_blocks(state, ticks=5)

    assert run.phase == PHASE_FAILED
    assert "lost its way" in run.failure_reason
    assert RIGHT in run.failure_reason and LEFT in run.failure_reason
    assert state.path_dispatcher.active_path_id_by_train(TRAIN) is None
    assert _switch_positions(state) == before


def test_a_failed_pass_with_no_leg_route_assigned_latches_no_teardown(state):
    """design_new.md "Speed calibration", How a run is ended: a failed pass
    deactivates the leg route only when one is assigned."""
    _place(state, (9, 13), "WEST")
    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    sim = state.sim_controller
    calibration = sim.speed_calibration
    controller = sim.get_or_create_controller(TRAIN)
    assert run.active_leg_route_id is None
    calibration.plan_tick = lambda train_id: calibration.fail(run, "a test fault")

    sim.route_executor._prepare_calibration_leg(TRAIN, controller)

    assert run.phase == PHASE_FAILED
    assert controller.route_hooks.phase_latches.route_deactivate is False


def test_any_cap_that_lowers_the_drive_discards_the_crossing():
    """design_new.md "A capped crossing is discarded": the reason the cap
    lowered the drive does not matter."""
    from types import SimpleNamespace

    from enums import AtcLimitReasonCode
    from train_controller import TrainController

    tainted = []
    fake = SimpleNamespace(
        train_id=TRAIN,
        _speed_calibration=SimpleNamespace(
            taint_current_crossing=lambda train_id, reason: tainted.append(reason)
        ),
        _calibration_power_percent_override=lambda: 40.0,
        atc=SimpleNamespace(_last_limit_reason_code=AtcLimitReasonCode.NO_RESERVATION),
    )

    TrainController._taint_calibration_crossing_on_cap(fake)

    assert len(tainted) == 1


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
    # This stretch is too short for the top steps (see
    # test_a_stretch_too_short_for_the_top_step_fails_at_once), so time three
    # low steps.
    run.power_steps = [30.0, 20.0, 10.0]
    _run_pass(state, run, until_step=3)

    assert run.phase != PHASE_FAILED, run.failure_reason
    assert [s.power_percent for s in run.samples] == [30.0, 20.0, 10.0]
    # The default profile's curve is linear, so each step reads its own percent.
    for sample in run.samples:
        assert sample.speed_kmh == pytest.approx(sample.power_percent, rel=0.05)
    # Every crossing of every step was timed, half each way.
    assert len(headings) == 3 * 2 * CALIBRATION_ROUND_TRIPS
    assert headings.count(LEFT) == headings.count(RIGHT)
    assert _switch_positions(state) == before


def test_a_stretch_too_short_for_the_top_step_fails_at_once(state):
    """design_new.md "Speed calibration", The sweep runs down from `100%` in
    `CALIBRATION_POWER_STEP_PERCENT` steps: a stretch too short for the top step
    fails the pass at its first step. Every outer block here is 50 cm, too
    short to stop from the default profile's 100% speed, so every 100% crossing
    is capped and the pass fails before measuring anything."""
    _place(state, (9, 13), "WEST")
    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    _run_pass(state, run, until_step=1)

    assert run.phase == PHASE_FAILED
    assert "too short to measure 100%" in run.failure_reason
    assert run.samples == []


def _run_until(state, run, done, ticks=600):
    sim = state.sim_controller
    for _ in range(ticks):
        sim.update(0.1)
        if done() or not run.is_measuring:
            return


def test_a_train_in_the_sideline_is_moved_into_the_nearer_outer_block(state):
    """design_new.md "Speed calibration", The pass first moves the train into an
    outer block: the rig run of 2026-11-01 left the train in the Sideline, and
    Start stayed off until the operator drove it back by hand."""
    from speed_calibration import PHASE_LEG, PHASE_MOVING

    _place(state, (10, 7), "WEST")
    span = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert span.occupied_blocks == [SIDELINE]
    assert train_service.calibration_readiness(state, TRAIN, measured_block_id=UP) == []

    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    assert run.phase == PHASE_MOVING
    state.sim_controller.update(0.1)

    rex = state.sim_controller.route_executor
    move_route = rex.route_registry[run.active_leg_route_id]
    assert run.move_target_block_id == RIGHT
    assert UP not in _walk_blocks(move_route.required_walk_keys)
    assert [(t.task_kind, t.task_location.block_id) for t in move_route.tasks] == [
        ("stop", RIGHT)
    ]

    _run_until(state, run, lambda: run.phase == PHASE_LEG)
    assert run.phase == PHASE_LEG, run.failure_reason
    assert run.heading_toward_block_id == LEFT
    assert _leg_path_blocks(state) == [RIGHT, UP, LEFT]


def test_a_train_on_the_measured_block_is_moved_off_it_first(state):
    """A leg is timed from the train's entry into the measured block, so a train
    already on it is first moved into an outer block (design_new.md "Speed
    calibration", The pass first moves the train into an outer block)."""
    from speed_calibration import PHASE_LEG, PHASE_MOVING

    # Head in Right, body trailing back onto Up: the span covers both.
    _place(state, (9, 13), "EAST")
    span = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert {RIGHT, UP} <= set(span.occupied_blocks)

    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    assert run.phase == PHASE_MOVING
    _run_until(state, run, lambda: run.phase == PHASE_LEG)

    assert run.phase == PHASE_LEG, run.failure_reason
    controller = state.sim_controller.get_or_create_controller(TRAIN)
    assert UP not in controller.get_current_span().occupied_blocks
    assert run.heading_toward_block_id != run.move_target_block_id


def test_a_move_that_never_arrives_fails_the_pass(state, monkeypatch):
    import speed_calibration
    from constants import CALIBRATION_STEP_TIMEOUT_S

    monkeypatch.setattr(speed_calibration, "CALIBRATION_MOVE_POWER_PERCENT", 0.0)
    _place(state, (10, 7), "WEST")
    run = train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    _run_until(state, run, lambda: False, ticks=int(CALIBRATION_STEP_TIMEOUT_S * 10) + 50)

    assert run.phase == PHASE_FAILED
    assert "did not reach" in run.failure_reason
    assert state.path_dispatcher.active_path_id_by_train(TRAIN) is None


def test_with_no_way_into_an_outer_block_the_pass_does_not_open(state):
    from services import ConflictError

    _place(state, (10, 7), "WEST")
    # A `manual` block is out of reach of every automatic path.
    state.network.blocks[RIGHT].manual = True
    state.network.blocks[LEFT].manual = True

    unmet = train_service.calibration_readiness(state, TRAIN, measured_block_id=UP)
    assert any(u.startswith("No way leads from where the train stands") for u in unmet)
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
    assert any("off Up," in u for u in unmet)
