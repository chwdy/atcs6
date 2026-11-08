"""A calibration leg runs through the measured block, with the points as set.

design_new.md "Speed calibration", Calibration leg: the leg's one-off route names
the measured block as a `waypoint` and the far end's last block as its `stop`,
each with the direction the stretch crosses it in, so its path runs out of one
end, through the measured block, and out through the other end, and leaves
every switch where it is set.

On this oval, Main (or Sideline, as the points are set) lies past both outer
blocks of Up, and Up past both outer blocks of Main. Each leg finds the far
end's run-off blocks with only the blocks the train stands in taken, so each
end takes that block in turn as its run-off block (design_new.md "Speed
calibration", Each end of the stretch is an outer block and the run-off blocks
past it).

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

from tests.train_test_helpers import skip_calibration_trials

from app_state import AppState
from bootstrap import bootstrap_runtime
from services import segment_service, train_service
from speed_calibration import PHASE_FAILED, PHASE_LEG

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


def _place(state, tile, direction, length_cm=10.0):
    train_service.create_train(
        state,
        train_id=TRAIN,
        length_cm=length_cm,
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

    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
    blocks = _leg_path_blocks(state)

    assert run.phase != PHASE_FAILED, run.failure_reason
    # The train stands in Right alone, so the far end past Left takes Main as
    # the points are set, and no switch moves.
    assert blocks == [RIGHT, UP, LEFT, MAIN]
    assert run.runoff_switch_positions == {}
    assert _switch_positions(state) == before

    leg_route = state.sim_controller.route_executor.route_registry[
        run.active_leg_route_id
    ]
    kinds = [(t.task_kind, t.task_location.block_id) for t in leg_route.tasks]
    assert kinds == [("waypoint", UP), ("stop", MAIN)]
    assert all(t.task_direction in ("forward", "reverse") for t in leg_route.tasks)


def test_a_leg_across_main_keeps_off_the_sideline(state):
    """Main and Sideline both join Left to Right; with the points set for Main,
    the leg must not take the Sideline and throw both switches."""
    assert set(_switch_positions(state).values()) == {0}
    _place(state, (6, 4), "EAST")
    held = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert LEFT in held.occupied_blocks

    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=MAIN))
    blocks = _leg_path_blocks(state)

    assert run.phase != PHASE_FAILED, run.failure_reason
    # Right's end takes Up as its run-off block.
    assert blocks == [LEFT, MAIN, RIGHT, UP]
    assert set(_switch_positions(state).values()) == {0}


def test_a_stretch_no_leg_can_drive_as_set_fails_the_pass(state):
    """When no path runs from the outer block through the measured block into the
    other one with the points as they are set, the pass fails and says so rather
    than sending the train another way."""
    _place(state, (9, 13), "WEST")
    before = _switch_positions(state)
    # A `manual` block is out of reach of every automatic path, the leg's too.
    state.network.blocks[UP].manual = True

    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
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
    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
    state.sim_controller.update(0.1)

    leg_route = state.sim_controller.route_executor.route_registry[
        run.active_leg_route_id
    ]
    # The walk starts at the near end's last block — Right, since the train
    # stands in no block past it — and ends at the far end's, Main past Left.
    assert _walk_blocks(leg_route.required_walk_keys) == [RIGHT, UP, LEFT, MAIN]


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
    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
    _leg_path_blocks(state, ticks=5)

    assert run.phase == PHASE_FAILED
    assert "lost its way" in run.failure_reason
    assert RIGHT in run.failure_reason and MAIN in run.failure_reason
    assert state.path_dispatcher.active_path_id_by_train(TRAIN) is None
    assert _switch_positions(state) == before


def test_a_failed_pass_with_no_leg_route_assigned_latches_no_teardown(state):
    """design_new.md "Speed calibration", How a run is ended: a failed pass
    deactivates the leg route only when one is assigned."""
    _place(state, (9, 13), "WEST")
    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
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
        _calibration_drive_voltage_override=lambda: 4.8,
        atc=SimpleNamespace(_last_limit_reason_code=AtcLimitReasonCode.NO_RESERVATION),
    )

    TrainController._taint_calibration_crossing_on_cap(fake)

    assert len(tainted) == 1


def _run_pass(state, run, *, until_samples, ticks=9000):
    sim = state.sim_controller
    for _ in range(ticks):
        sim.update(0.1)
        if not run.is_measuring or len(run.samples) >= until_samples:
            return


def test_every_step_is_timed_in_both_directions(state, monkeypatch):
    """design_new.md "Speed calibration", The pass turns the train around at each
    end: the train drives on until it is off the measured block before it stops,
    so the leg back enters that block afresh and is timed too. A 10 cm train
    braked the moment it entered the far block used to stop with its tail on Up,
    so every leg back went untimed and the slow steps read as a stall."""
    from constants import CALIBRATION_ROUND_TRIPS

    _place(state, (9, 13), "WEST")
    before = _switch_positions(state)
    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))

    headings = []
    original_record = state.sim_controller.speed_calibration.record_detected_entries

    def record(r, entered, now):
        if r.crossing_started_at is not None and r.heading_toward_block_id in entered:
            headings.append(r.heading_toward_block_id)
        return original_record(r, entered, now)

    state.sim_controller.speed_calibration.record_detected_entries = record
    # This stretch is too short for the top steps (see
    # test_a_stretch_too_short_for_the_top_step_fails_at_once), so time three
    # low coarse steps, all above the seeded starting voltage (1.5 V).
    import speed_calibration

    monkeypatch.setattr(speed_calibration, "CALIBRATION_VOLTAGE_STEP_V", 0.6)
    run.current_step_voltage = 3.6
    _run_pass(state, run, until_samples=3)

    assert run.phase != PHASE_FAILED, run.failure_reason
    assert [round(s.track_voltage, 3) for s in run.samples] == [3.6, 3.0, 2.4]
    # The seeded curve lies on the straight line from 0 V to 100 km/h at
    # 12 V, so a step at v volts reads v / 12 * 100 km/h.
    for sample in run.samples:
        assert sample.speed_kmh == pytest.approx(sample.track_voltage / 12.0 * 100.0, rel=0.05)
    # Every crossing of every step was timed, half each way.
    assert len(headings) == 3 * 2 * CALIBRATION_ROUND_TRIPS
    assert headings.count(LEFT) == headings.count(RIGHT)
    # Main lies past both Right and Left as the points are set, and each leg
    # runs off into it, so no switch moves.
    assert _switch_positions(state) == before


def test_a_stretch_too_short_for_the_top_step_fails_at_once(state):
    """design_new.md "Speed calibration", The sweep runs down from
    `max_voltage` in `CALIBRATION_VOLTAGE_STEP_V` steps: a stretch too short for
    the top step fails the pass at its first step. Outer blocks of 50 cm are too
    short to stop from the default profile's top speed, so every crossing at
    `max_voltage` is capped and the pass fails before measuring anything."""
    for block_id in (LEFT, RIGHT):
        (segment_id,) = state.network.blocks[block_id].segment_ids
        segment_service.update_segment(state, segment_id, data={"length_cm": 50.0})
    # No run-off block either side: Main and the Sideline are held `manual`.
    state.network.blocks[MAIN].manual = True
    state.network.blocks[SIDELINE].manual = True
    _place(state, (9, 13), "WEST")
    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
    _run_pass(state, run, until_samples=1)

    assert run.phase == PHASE_FAILED
    assert "too short to measure 12 V" in run.failure_reason
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

    # SW_7_5 is held `manual`, so the Sideline cannot be Left's run-off block
    # and the train stands outside the stretch.
    state.network.switches["SW_7_5"].manual = True
    _place(state, (10, 7), "WEST")
    span = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert span.occupied_blocks == [SIDELINE]
    assert train_service.calibration_readiness(state, TRAIN, measured_block_id=UP) == []

    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
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
    # The ends are found again when the leg arms. Left's only way on is across
    # the manual SW_7_5, which no leg may reserve, so Left's end has no run-off
    # block.
    assert _leg_path_blocks(state) == [RIGHT, UP, LEFT]


def test_a_train_on_the_measured_block_is_moved_off_it_first(state):
    """A leg is timed from the train's entry into the measured block, so a train
    already on it is first moved into an outer block (design_new.md "Speed
    calibration", The pass first moves the train into an outer block)."""
    from speed_calibration import PHASE_LEG, PHASE_MOVING

    # Head in Right, body trailing back onto Up: the span covers both.
    _place(state, (9, 13), "EAST", length_cm=20.0)
    span = state.sim_controller.get_or_create_controller(TRAIN).get_current_span()
    assert {RIGHT, UP} <= set(span.occupied_blocks)

    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
    assert run.phase == PHASE_MOVING
    _run_until(state, run, lambda: run.phase == PHASE_LEG)

    assert run.phase == PHASE_LEG, run.failure_reason
    controller = state.sim_controller.get_or_create_controller(TRAIN)
    assert UP not in controller.get_current_span().occupied_blocks
    assert run.heading_toward_block_id != run.move_target_block_id


def test_a_move_that_never_arrives_fails_the_pass(state, monkeypatch):
    import speed_calibration
    from constants import CALIBRATION_STEP_TIMEOUT_S

    monkeypatch.setattr(speed_calibration, "CALIBRATION_MOVE_VOLTAGE_V", 0.0)
    # The Sideline cannot be Left's run-off block, so the train must move.
    state.network.switches["SW_7_5"].manual = True
    _place(state, (10, 7), "WEST")
    run = skip_calibration_trials(train_service.calibrate_train(state, TRAIN, measured_block_id=UP))
    _run_until(state, run, lambda: False, ticks=int(CALIBRATION_STEP_TIMEOUT_S * 10) + 50)

    assert run.phase == PHASE_FAILED
    assert "did not reach" in run.failure_reason
    assert state.path_dispatcher.active_path_id_by_train(TRAIN) is None


def test_with_no_way_into_an_outer_block_the_pass_does_not_open(state):
    from services import ConflictError

    _place(state, (10, 7), "WEST")
    # A `manual` block or switch is out of reach of every automatic path.
    state.network.blocks[RIGHT].manual = True
    state.network.blocks[LEFT].manual = True
    for switch_id in ("SW_7_5", "SW_11_9"):
        state.network.switches[switch_id].manual = True

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


# --- run-off blocks -------------------------------------------------------------
# design_new.md "Speed calibration", Each end of the stretch is an outer block and
# the run-off blocks past it; The run-off lasts until the train is off B; The
# pass ends each leg once the train stands off B.


def test_both_ends_of_a_curved_outer_block_are_walked(state):
    """Right curves round the end of the oval, so both its ends leave by the
    same side; the walk out of it still finds Main past it as well as Up."""
    manager = state.network.block_manager
    assert set(manager.adjacent_blocks(RIGHT)) == {UP, MAIN}
    assert set(manager.adjacent_blocks(LEFT)) == {UP, MAIN}


def test_each_end_takes_run_off_blocks_for_the_train_length(state):
    """Up's ends, each as a leg runs toward it: Right takes Main, one block past
    a Right that already holds the train, and Left takes Main too, because a
    leg runs toward one end at a time."""
    outer, _length = train_service.resolve_calibration_stretch(state, UP)
    runoff, positions = train_service.resolve_calibration_runoff(state, UP, outer, 10.0)
    ends = dict(zip(outer, runoff))
    assert ends == {RIGHT: (MAIN,), LEFT: (MAIN,)}
    assert positions == {}


def _throw(state, switch_id, position):
    state.network.switches[switch_id].position = position


def test_a_train_longer_than_the_outer_block_is_timed_both_ways(state, monkeypatch):
    """A 120 cm train does not fit in a 99.6 cm outer block past Up. With the
    points set so each end has a run-off block — Main past Right, the Sideline
    past Left — it runs off into them, every leg ends off Up, and every crossing
    of both directions is timed."""
    from constants import CALIBRATION_ROUND_TRIPS

    import speed_calibration

    _throw(state, "SW_7_5", 1)
    _place(state, (9, 13), "WEST", length_cm=120.0)
    run = skip_calibration_trials(
        train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    )
    assert {run.end_block_ids(0), run.end_block_ids(1)} == {
        (RIGHT, MAIN),
        (LEFT, SIDELINE),
    }
    monkeypatch.setattr(speed_calibration, "CALIBRATION_VOLTAGE_STEP_V", 0.6)
    run.current_step_voltage = 3.6
    _run_pass(state, run, until_samples=1)

    assert run.phase != PHASE_FAILED, run.failure_reason
    assert len(run.samples) == 1
    used = [c.toward_block_id for c in run.crossing_log if c.outcome == "used"]
    assert used.count(LEFT) == used.count(RIGHT) == CALIBRATION_ROUND_TRIPS


def test_the_pass_tears_each_leg_down_once_the_train_stands_off_b(state):
    """The run-off stops the train in Left, short of the leg's stop, so the pass
    itself tears the leg route down and the next leg runs back toward Right."""
    _place(state, (9, 13), "WEST")
    run = skip_calibration_trials(
        train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    )
    sim = state.sim_controller
    first_leg = None
    for _ in range(3000):
        sim.update(0.1)
        if first_leg is None and run.active_leg_route_id:
            first_leg = run.active_leg_route_id
            assert run.heading_toward_block_id == LEFT
        if first_leg and run.active_leg_route_id not in (None, first_leg):
            break
    assert run.phase != PHASE_FAILED, run.failure_reason
    assert run.active_leg_route_id not in (None, first_leg)
    assert run.heading_toward_block_id == RIGHT
    assert first_leg not in sim.route_executor.route_registry


def test_the_stretch_preview_names_each_end_and_its_length(state):
    """The panel's stretch line shows each end's blocks and length, sized for
    the named train (design_new.md "Speed calibration", The panel, Before the
    pass starts the panel holds three things)."""
    from app_factory import create_app

    _place(state, (9, 13), "WEST", length_cm=45.0)
    state.simulation_running = False
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get(f"/api/trains/{TRAIN}/calibrate/stretch/{UP}")
    assert response.status_code == 200
    body = response.get_json()
    ends = dict(zip(body["outer_block_ids"], body["runoff_block_ids"]))
    assert ends == {RIGHT: [MAIN], LEFT: [MAIN]}
    lengths = dict(zip(body["outer_block_ids"], body["end_lengths_cm"]))
    assert lengths[RIGHT] == pytest.approx(99.6 + 84.0)
    assert lengths[LEFT] == pytest.approx(99.6 + 84.0)
    assert body["runoff_switch_positions"] == {}
    assert body["train_length_cm"] == 45.0
    assert body["block_names"][MAIN] == "Main"


@pytest.mark.parametrize("start", [((9, 13), "WEST"), ((6, 4), "EAST")])
def test_a_run_off_block_another_train_stands_in_is_passed_over(state, monkeypatch, start):
    """design_new.md "Speed calibration", A run-off block holds no other train:
    with another train standing in Main, each end looks the other way through
    its switch and takes the Sideline — Right through SW_11_9, Left through
    SW_7_5 — and each leg throws and locks its own far end's switch when it
    reserves into the Sideline, whichever end the train starts in.

    Regression for the 2026-11-07 rig: the end walked first took the Sideline,
    so the other end had no run-off block and its switch was never locked."""
    from constants import CALIBRATION_ROUND_TRIPS

    import speed_calibration

    train_service.create_train(
        state, train_id="OTHER", length_cm=10.0,
        position={"x": 7, "y": 6}, direction="SOUTH",
    )
    _place(state, *start)
    assert state.network.blocks[MAIN].occupied_by == "OTHER"
    outer, _length = train_service.resolve_calibration_stretch(state, UP)
    runoff, positions = train_service.resolve_calibration_runoff(
        state, UP, outer, 10.0, TRAIN
    )
    assert dict(zip(outer, runoff)) == {RIGHT: (SIDELINE,), LEFT: (SIDELINE,)}
    assert positions == {"SW_11_9": 1, "SW_7_5": 1}
    assert _switch_positions(state) == {"SW_11_9": 0, "SW_7_5": 0}

    run = skip_calibration_trials(
        train_service.calibrate_train(state, TRAIN, measured_block_id=UP)
    )
    monkeypatch.setattr(speed_calibration, "CALIBRATION_VOLTAGE_STEP_V", 0.6)
    run.current_step_voltage = 3.6
    switch_into_runoff = {RIGHT: "SW_11_9", LEFT: "SW_7_5"}
    locked_toward = {RIGHT: set(), LEFT: set()}
    sim = state.sim_controller
    for _ in range(9000):
        sim.update(0.1)
        if run.active_leg_route_id and run.phase == PHASE_LEG:
            for switch_id, switch in state.network.switches.items():
                for _train_id, position in switch.locks.values():
                    locked_toward[run.heading_toward_block_id].add((switch_id, position))
        if not run.is_measuring or len(run.samples) >= 1:
            break

    assert run.phase != PHASE_FAILED, run.failure_reason
    used = [c.toward_block_id for c in run.crossing_log if c.outcome == "used"]
    assert used.count(LEFT) == used.count(RIGHT) == CALIBRATION_ROUND_TRIPS
    for toward, switch_id in switch_into_runoff.items():
        assert locked_toward[toward] == {(switch_id, 1)}
    assert _switch_positions(state) == {"SW_11_9": 1, "SW_7_5": 1}
    assert state.network.blocks[MAIN].occupied_by == "OTHER"
