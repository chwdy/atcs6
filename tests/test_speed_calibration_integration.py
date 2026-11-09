"""Speed calibration end-to-end: the pass drives a real train on a real scene.

design_new.md "Speed calibration", Where it runs. The unit tests drive the state
machine with synthetic confirmations; this one proves the wiring around it — that
`SimulationController.update(...)` ticks the pass, that the pass sees the block
entries attribution credits to its train, that the step's power the pass reads
back through `get_propulsion_override(...)` drives the train through the ordinary
control-output path, and that it takes real authority so ATC does not hold the
train at a zero no-authority cap.
"""

import contextlib
import io
import threading
from datetime import datetime

import pytest

from constants import CALIBRATION_LIGHTING_TRIAL_START_V
from speed_calibration import PASS_LIGHTING_TRIAL, PASS_STARTING_TRIAL, PASS_SWEEP

from app_state import AppState
from bootstrap import bootstrap_runtime
from services import train_service
from enums import TrainStatus
from speed_calibration import PHASE_FAILED, PHASE_MEASURED

pytestmark = pytest.mark.scene("demo_6")


@pytest.fixture
def state():
    """The demo_6 scene behind an `AppState`, which is what the service takes."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, persistence = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
    # The scene's start-up commands run at once, so its ATO trains would start
    # moving on the first tick and throw points under the stretch a test picks.
    # Pause ATO on every train: nothing moves but the train under calibration.
    for controller in sim.controllers.values():
        controller.commands.ato_stop()
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=persistence,
        simulation_lock=threading.Lock(),
        # A calibration run opens only while the session runs.
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def _free_train(state):
    """A placed train with no route assigned at all — calibration refuses the
    rest, because a controlled pass must not hijack a train running a route.

    The route assignment has to go, not just the live run: Phase 1 rebuilds a
    run for any train a route still names, so removing the run alone leaves the
    train routed again one tick later.
    """
    executor = state.sim_controller.route_executor
    train_id = next(iter(state.network.trains))
    executor.remove_train_route_run(train_id)
    for route in executor.route_registry.values():
        assigned = getattr(route, "assigned_train_ids", None)
        if assigned and train_id in assigned:
            route.unassign_train(train_id)
    return train_id


def _ready_manual_train(sim, network, train_id):
    """Put a placed train under manual power-percent control, stopped."""
    controller = sim.get_or_create_controller(train_id)
    # The scene's start-up already ran; this helper sets power and mode
    # directly for calibration.
    controller.commands.power_on_cmd()
    controller.commands.set_mode("manual")
    controller.commands.set_manual_mode_type("power_percent")
    controller._apply_head_frame("end_a")
    return controller


def _first_calibratable_block(state, train_id):
    """A measured block whose stretch the train is already standing in.

    A leg starts from a stand in an outer block, so a block the train is not
    beside is not one it can measure across.
    """
    controller = state.sim_controller.get_or_create_controller(train_id)
    held = set(getattr(controller.get_current_span(), "occupied_blocks", []) or [])
    for block_id in state.network.blocks:
        try:
            outer, _length = train_service.resolve_calibration_stretch(state, block_id)
        except Exception:
            continue
        if held.intersection(outer):
            return block_id
    return None


def test_a_measured_block_resolves_two_outer_blocks(state):
    block_id = _first_calibratable_block(state, _free_train(state))
    assert block_id is not None, "demo_6 should have a three-block run somewhere"
    outer, length_cm = train_service.resolve_calibration_stretch(state, block_id)
    assert len(outer) == 2
    assert outer[0] != outer[1]
    assert length_cm > 0


def test_a_stretch_never_crosses_a_buffer_stop(state):
    """The outer blocks are found by walking the track out of each end of the
    measured block (design_new.md "Speed calibration", The operator names the
    measured block), so a block that ends at a buffer stop has no neighbour at
    that end.

    demo_6's Yard A 1 platform, PLT_YA_1_BLOCK, ends at a buffer stop on its
    north tile (24, 22). The grid cell beyond it, (24, 23), belongs to
    PLT_YA_8_APPROACH, but that tile's rails do not meet the edge they would
    have to cross, so the two blocks are not connected and the platform has a
    block at one end only.
    """
    from services import ValidationError

    with pytest.raises(ValidationError, match="block at both ends"):
        train_service.resolve_calibration_stretch(state, "PLT_YA_1_BLOCK")


def test_every_resolved_stretch_runs_through_its_measured_block(state):
    """Whatever stretch the resolver names, a train can run it end to end: the
    path planner finds a walk from one outer block to the other through the
    measured block and nothing else. A stretch that is not such a run would
    open a pass whose legs can never plan.

    A crossing block meets four blocks, two per track, and is left out here:
    which of its two tracks a stretch should run along is not decided yet.
    """
    from services import ValidationError

    dispatcher = state.path_dispatcher
    manager = state.network.block_manager
    not_a_run = []
    for block_id in state.network.blocks:
        if len(manager.adjacent_blocks(block_id)) > 2:
            continue
        try:
            outer, _length = train_service.resolve_calibration_stretch(
                state, block_id
            )
        except ValidationError:
            continue
        walks = dispatcher.find_paths(outer[0], outer[1])
        if not any(list(w.blocks) == [outer[0], block_id, outer[1]] for w in walks):
            not_a_run.append((block_id, outer))
    assert not_a_run == []


def test_a_train_beside_a_dead_end_platform_is_told_before_the_pass_opens(state):
    """The case seen on the dev server on 2026-10-18: a train standing in
    PLT_YA_8_APPROACH with PLT_YA_1_BLOCK named as the measured block. The
    readiness line used to say ready, Start armed a leg to PLT_YA_1_APPROACH,
    and the leg could never plan (`No path exists between start and
    destination`), because PLT_YA_8_APPROACH is not connected to the platform.
    The readiness line now names the problem and Start opens nothing.
    """
    from services import ConflictError

    sim = state.sim_controller
    # T_YA_8 stands in PLT_YA_8_BLOCK, next to the tile the new train goes on.
    # A train is deleted only once it is OFF.
    assert sim.controllers["T_YA_8"].commands.power_off_cmd().success
    for _ in range(5):
        sim.update(0.1)
    train_service.delete_train(state, "T_YA_8")
    train_service.create_train(
        state,
        train_id="T_PROBE",
        position={"x": 25, "y": 24},
        direction="WEST",
        length_cm=5.0,
    )
    controller = sim.get_or_create_controller("T_PROBE")
    assert controller.get_current_span().occupied_blocks == ["PLT_YA_8_APPROACH"]

    unmet = train_service.calibration_readiness(
        state, "T_PROBE", measured_block_id="PLT_YA_1_BLOCK"
    )
    assert any("block at both ends" in u for u in unmet)
    with pytest.raises(ConflictError, match="block at both ends"):
        train_service.calibrate_train(
            state, "T_PROBE", measured_block_id="PLT_YA_1_BLOCK"
        )
    assert sim.speed_calibration.runs.get("T_PROBE") is None


@pytest.mark.parametrize("head_end", ["end_a", "end_b"])
def test_the_first_leg_plans_whichever_way_the_train_faces(state, head_end):
    """The operator does not choose a direction (design_new.md "Speed
    calibration", The panel): a leg takes its direction from its leg route,
    per Reservation-side head derivation. Running this with the span framed
    each way covers a train whose head faces away from the measured block;
    either way the opening leg's path is created on the tick the leg arms.
    """
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    controller = _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)  # drain the setup commands
    controller._apply_head_frame(head_end)

    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)

    assert run.active_leg_route_id is not None
    assert sim.path_dispatcher.active_path_id_by_train(train_id) is not None


def test_a_block_with_no_recorded_length_is_refused(state):
    from services import ValidationError

    for block_id, block in state.network.blocks.items():
        if float(getattr(block, "length_cm", 0) or 0) <= 0:
            with pytest.raises(ValidationError):
                train_service.resolve_calibration_stretch(state, block_id)
            return
    pytest.skip("every demo_6 block has a length")


def test_opening_takes_control_of_a_stopped_ato_train(state):
    """Opening a run takes control of the train (design_new.md "Speed
    calibration", Opening a run takes control of the train): a stopped ATO train
    with no route is switched to manual and reads CALIBRATING."""
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    controller = sim.get_or_create_controller(train_id)
    controller.commands.set_mode("ato")
    assert controller.commands.control_mode == "ato"

    train_service.calibrate_train(state, train_id, measured_block_id=block_id)

    assert controller.commands.control_mode == "manual"
    assert controller.ops.train_status == TrainStatus.CALIBRATING


def test_opening_names_every_unmet_condition(state):
    from services import ConflictError

    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    controller = sim.get_or_create_controller(train_id)
    state.simulation_running = False
    controller.eb.activate()

    unmet = train_service.calibration_readiness(
        state, train_id, measured_block_id=block_id
    )
    assert any("not live" in u for u in unmet)
    assert any("emergency brake" in u for u in unmet)
    with pytest.raises(ConflictError, match="not live.*emergency brake"):
        train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    assert sim.speed_calibration.runs.get(train_id) is None


def test_a_held_result_blocks_a_new_run(state):
    """A finished pass waits for the operator to commit or discard it; no new run
    opens on that train meanwhile (design_new.md "What ends a calibration run",
    A finished pass is a held result, not an open run)."""
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    run.phase = PHASE_MEASURED

    controller = sim.controllers[train_id]
    assert controller.ops.train_status == TrainStatus.MANUAL
    unmet = train_service.calibration_readiness(
        state, train_id, measured_block_id=block_id
    )
    assert any("finished speed pass" in u for u in unmet)


def test_route_assignment_is_refused_while_calibrating(state):
    from services import ConflictError

    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    route_id = next(iter(sim.route_executor.route_registry))

    with pytest.raises(ConflictError, match="speed-calibration pass"):
        train_service.assign_route(state, train_id, route_id=route_id)
    with pytest.raises(ConflictError):
        train_service.assign_route(state, train_id, route_id=None)
    assert sim.speed_calibration.runs.get(train_id) is not None


def test_the_readiness_endpoint_lists_unmet_conditions(state):
    from app_factory import create_app

    train_id = _free_train(state)
    state.simulation_running = False
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get(
            f"/api/trains/{train_id}/calibrate/readiness?kind=lighting"
        )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ready"] is False
    assert any("not live" in u for u in body["unmet"])


def test_messages_and_the_stretch_name_blocks_by_their_names(state):
    """The panel shows blocks by the names the operator gave them, so the
    readiness line and the stretch line name the outer blocks that way, not by
    their ids (rig, 2026-11-01: "Stand the train in BLK_4F4CF0 or
    BLK_C37589A5")."""
    from app_factory import create_app

    train_id = _free_train(state)
    controller = state.sim_controller.get_or_create_controller(train_id)
    held = set(controller.get_current_span().occupied_blocks)
    measured, outer = None, ()
    for block_id in state.network.blocks:
        try:
            outer, _length = train_service.resolve_calibration_stretch(state, block_id)
        except Exception:
            continue
        if not held.intersection(outer) and (
            state.sim_controller.route_executor.calibration_move_walk(
                train_id, block_id, outer
            )
            is None
        ):
            measured = block_id
            break
    assert measured is not None
    state.network.blocks[outer[0]].name = "Right"
    state.network.blocks[outer[1]].name = "Left"

    unmet = train_service.calibration_readiness(
        state, train_id, measured_block_id=measured
    )
    assert any(
        u.startswith("No way leads from where the train stands into Right or Left")
        for u in unmet
    )

    state.simulation_running = False
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get(f"/api/trains/{train_id}/calibrate/stretch/{measured}")
    assert response.status_code == 200
    names = response.get_json()["block_names"]
    assert names[outer[0]] == "Right"
    assert names[outer[1]] == "Left"


def test_the_runtime_tick_arms_the_pass_as_a_one_off_route(state):
    sim = state.sim_controller
    network = state.network
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    assert block_id is not None

    controller = _ready_manual_train(sim, network, train_id)
    sim.update(0.1)  # drain the setup commands

    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    assert run.leg_armed is False
    assert run.active_leg_route_id is None
    # The pass opens in the lighting trial (design_new.md "Speed calibration",
    # The pass starts with two trial stages), whose legs run as one-off routes
    # like the sweep's.
    assert run.pass_stage == PASS_LIGHTING_TRIAL
    assert run.current_voltage == CALIBRATION_LIGHTING_TRIAL_START_V

    # Which outer block this opening leg runs toward — the one the train is not
    # standing in — names the far end the leg route's stop task lies in.
    outer, _length = train_service.resolve_calibration_stretch(state, block_id)
    held = set(getattr(controller.get_current_span(), "occupied_blocks", []) or [])
    far_block = next(b for b in outer if b not in held)

    # One tick arms the leg at Plan step `c9e1`: it builds a one-off route to the
    # far outer block, activates and assigns it, and the ordinary Plan steps
    # allocate its run and create its path.
    sim.update(0.1)
    assert run.leg_armed is True
    assert run.active_leg_route_id is not None

    from train_route import RouteMode

    executor = sim.route_executor
    leg_route = executor.route_registry.get(run.active_leg_route_id)
    assert leg_route is not None
    assert leg_route.mode == RouteMode.ONE_OFF
    assert train_id in leg_route.assigned_train_ids
    assert leg_route.manual_override is False
    # Two tasks: a waypoint at the measured block, then a stop at the last
    # block of the far end — the far outer block, or its last run-off block —
    # so the path crosses the measured block on its way.
    assert [t.task_kind for t in leg_route.tasks] == ["waypoint", "stop"]
    assert leg_route.tasks[0].task_location.block_id == block_id
    assert run.heading_toward_block_id == far_block
    assert leg_route.tasks[1].task_location.block_id == run.heading_end_block_id

    # The leg has real authority (a path), and the step's voltage drives the
    # train back through get_propulsion_override, not a zero no-authority cap.
    assert sim.path_dispatcher.active_path_id_by_train(train_id) is not None
    override = sim.speed_calibration.get_propulsion_override(train_id)
    assert override is not None
    assert override.track_voltage == run.current_voltage

    sim.update(0.1)
    assert run.phase != PHASE_FAILED or "authority" in run.failure_reason


def test_a_pass_writes_nothing_until_it_is_committed(state):
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    profile = state.network.speed_profiles.get(
        state.network.trains[train_id].speed_profile_id
    )
    before = list(profile.voltage_speed_curve)

    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    for _ in range(20):
        sim.update(0.1)

    assert profile.voltage_speed_curve == before
    assert profile.calibrated_at is None


def test_cancelling_a_pass_deactivates_its_leg_route(state):
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)

    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)  # arm the leg as a one-off route
    leg_route_id = run.active_leg_route_id
    assert leg_route_id is not None
    path_id = sim.path_dispatcher.active_path_id_by_train(train_id)
    assert path_id is not None

    train_service.calibrate_cancel(state, train_id)
    # The operator's Stop first brings the train to a stand inside its leg
    # (design_new.md "What ends a calibration run", An operator ending stops
    # the train before it lets it go): half the drive for its time, then the
    # run ends once the train stands.
    controller = sim.controllers[train_id]
    latch_seen = False
    for _ in range(30):
        sim.update(0.1)
        latch_seen = latch_seen or controller.route_hooks.phase_latches.route_deactivate
        if train_service.calibrate_status(state, train_id) is None:
            break
    assert train_service.calibrate_status(state, train_id) is None

    # Ending tears the leg route down through the ordinary route-deactivate
    # latch, not a direct dispatcher call; the inclusive latch also cancels the
    # still-live leg path.
    assert latch_seen or sim.route_executor.assigned_route_id_for(train_id) is None

    # The Action deactivation consumer removes the run and unassigns the route;
    # the path clears once the train has stopped (it commands `power_percent = 0`
    # but forces no stop, so the train first runs the authority it holds out).
    for _ in range(400):
        sim.update(0.1)
        path = sim.path_dispatcher.paths.get(path_id)
        route = sim.route_executor.route_registry.get(leg_route_id)
        path_gone = path is None or path.status.value != "active"
        route_gone = route is None or train_id not in route.assigned_train_ids
        if path_gone and route_gone:
            break
    path = sim.path_dispatcher.paths.get(path_id)
    assert path is None or path.status.value != "active"
    route = sim.route_executor.route_registry.get(leg_route_id)
    assert route is None or train_id not in route.assigned_train_ids
    assert sim.route_executor.execution_state_for(train_id) is None


def test_powering_off_mid_pass_ends_the_speed_run(state):
    """An ordinary power-off mid-pass ends the speed run on that same Action pass,
    so no spent run leaks (design_new.md "Speed calibration", Where it runs —
    step `26f5` runs after the power-off consumer, for an `OFF` train too, so it
    finds the pass invalid and ends it that tick). A leaked run would otherwise
    block a new pass at the readiness gate."""
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)  # arm the leg
    assert sim.speed_calibration.runs.get(train_id) is not None

    sim.controllers[train_id].commands.power_off_cmd()
    for _ in range(10):
        sim.update(0.1)
        if sim.speed_calibration.runs.get(train_id) is None:
            break
    assert sim.speed_calibration.runs.get(train_id) is None, (
        "a powered-off train must not leave an open calibration run behind"
    )
    # The reason names the condition that failed, so the panel can say why.
    result = train_service.last_speed_calibration_result(state, train_id)
    assert result["success"] is False
    assert result["reason"] == "the train was powered off"


def test_emergency_brake_mid_pass_ends_the_speed_run(state):
    """`eb.active` makes `run_still_valid` false, so step `26f5` ends the pass
    (design_new.md "Speed calibration", Where it runs). A run left open would
    block a new pass at the readiness gate."""
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    assert sim.speed_calibration.runs.get(train_id) is not None

    sim.controllers[train_id].eb.activate()
    # The predicate is false the moment EB latches, before any further tick.
    assert sim.speed_calibration.run_still_valid(train_id) is False
    assert sim.speed_calibration.run_invalid_reason(train_id) == "the emergency brake is on"
    for _ in range(10):
        sim.update(0.1)
        if sim.speed_calibration.runs.get(train_id) is None:
            break
    assert sim.speed_calibration.runs.get(train_id) is None
    result = train_service.last_speed_calibration_result(state, train_id)
    assert result["reason"] == "the emergency brake is on"


def test_entering_a_fatal_halt_ends_an_open_speed_run(state):
    """Entering a fatal halt goes through `enter_output_safe_state()`, which ends
    every open calibration run (design_new.md "What ends a calibration run")."""
    from runtime_payloads import FatalConflictKind, FatalPhysicalEntryConflict

    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)  # arm the leg
    assert sim.speed_calibration.runs.get(train_id) is not None

    sim.enter_failed_runtime(
        FatalPhysicalEntryConflict(
            resource_type="block",
            resource_id=block_id,
            incoming_train_id=train_id,
            prior_owner_train_id="OTHER",
            conflict_kind=FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN,
        )
    )
    assert sim.speed_calibration.runs.get(train_id) is None, (
        "entering a fatal halt must end every open calibration run"
    )


def test_a_session_stop_ends_an_open_speed_run(state):
    """A session stop enters the safe state, which ends every open run."""
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)  # arm the leg
    assert sim.speed_calibration.runs.get(train_id) is not None

    sim.enter_output_safe_state()

    assert sim.speed_calibration.runs.get(train_id) is None
    assert sim.controllers[train_id].ops.train_status == TrainStatus.MANUAL


def test_clearing_the_network_discards_a_held_result(state):
    from app_factory import create_app

    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    run.phase = PHASE_MEASURED
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        assert client.post("/api/network/clear").status_code == 200
    assert sim.speed_calibration.runs.get(train_id) is None


def test_deleting_the_train_discards_a_held_result(state):
    """Powering the train off keeps a finished pass; deleting the train discards
    it, as a network clear does, so a later train with the same id can open a
    run (design_new.md "Train Entity", Deleting a train; "What ends a
    calibration run", A finished pass is a held result, not an open run)."""
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    old_train = state.network.trains[train_id]
    profile_id = old_train.speed_profile_id
    controller = sim.controllers[train_id]
    old_span = controller.get_current_span()
    outer, _length = train_service.resolve_calibration_stretch(state, block_id)
    standing_in = next(b for b in old_span.occupied_blocks if b in outer)
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    run.phase = PHASE_MEASURED

    assert controller.commands.power_off_cmd().success
    for _ in range(5):
        sim.update(0.1)
    assert controller.train_status == TrainStatus.OFF
    assert sim.speed_calibration.runs.get(train_id) is run

    assert train_service.delete_train(state, train_id) is True
    assert sim.speed_calibration.runs.get(train_id) is None

    # Put the new train where the scene put the old one: head on the old head
    # tile, facing out of the yard road toward its approach block.
    new_train = sim.add_train(
        train_id, standing_in, "EAST",
        max_speed_kmh=old_train.max_speed, length_cm=old_train.length_cm,
        start_tile=old_span.end_a.tile,
    )
    # The deleted train's instance profile went with it; the new train owns
    # a fresh one (design_new.md "Train Entity", Deleting a train).
    assert state.speed_profile_registry.get(profile_id) is None
    assert train_service.calibration_readiness(
        state, train_id, measured_block_id=block_id
    ) == []
    new_run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    assert new_run is not run
    assert new_run.is_measuring


def test_run_invalid_reason_names_the_first_condition_that_failed(state):
    """`run_invalid_reason` holds `run_still_valid`'s four checks and names the
    first one that fails, in order: the controller, power, EB, a pending
    power-off (design_new.md "Speed calibration", Where it runs). Step `26f5`
    reports that reason when it ends a pass, so the panel can say why."""
    sim = state.sim_controller
    calibration = sim.speed_calibration
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    assert calibration.run_invalid_reason(train_id) is None
    assert calibration.run_still_valid(train_id) is True

    controller = sim.controllers[train_id]
    controller.route_hooks.phase_latches.set_power_off()
    assert calibration.run_invalid_reason(train_id) == "a power-off is pending"
    assert calibration.run_still_valid(train_id) is False
    controller.eb.activate()
    assert calibration.run_invalid_reason(train_id) == "the emergency brake is on"

    controller = sim.controllers.pop(train_id)
    try:
        assert calibration.run_invalid_reason(train_id) == "the train's controller is gone"
    finally:
        sim.controllers[train_id] = controller


def test_run_still_valid_survives_the_leg_completion_window(state):
    """A clean multi-leg speed pass survives a leg boundary.

    On the tick a calibration leg completes, Plan settlement unassigns the leg's
    one-off route before `_prepare_calibration_leg` clears
    `CalibrationRun.active_leg_route_id`. `run_still_valid` reads no route
    assignment, only the controller, power, EB and a pending power-off
    (design_new.md "Speed calibration", Where it runs), so the pass stays open.
    """
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)  # arm the leg one-off route
    run = sim.speed_calibration.runs.get(train_id)
    assert run is not None and run.active_leg_route_id is not None
    leg_route_id = run.active_leg_route_id
    assert sim.route_executor.assigned_route_id_for(train_id) == leg_route_id
    assert sim.speed_calibration.run_still_valid(train_id) is True

    # Reproduce the completion-tick window: the leg's route is unassigned
    # (assigned -> None) but active_leg_route_id is not yet cleared.
    sim.route_executor.remove_train_route_run(train_id)
    assert sim.route_executor.assigned_route_id_for(train_id) is None
    assert run.active_leg_route_id == leg_route_id
    assert sim.speed_calibration.run_still_valid(train_id) is True


def test_route_requests_for_a_calibrating_train_leave_no_route_behind(state):
    """A calibrating train refuses assignment (design_new.md "Route assignment
    writers"); the create-route and Quick Route requests refuse before they
    register a route, so nothing is left behind."""
    from app_factory import create_app

    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    routes_before = set(sim.route_executor.route_registry)
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        created = client.post(
            "/api/train-routes",
            json={
                "route_id": "R_CAL_REFUSED",
                "tasks": [{"task_kind": "stop", "task_location": {"block_id": block_id}}],
                "assigned_train_ids": [train_id],
            },
        )
        quick = client.post(
            "/api/paths",
            json={"train_id": train_id, "requested_destination_block_id": block_id},
        )
    assert created.status_code == 409
    assert quick.status_code == 400
    assert "calibration" in quick.get_json()["error"]
    assert set(sim.route_executor.route_registry) == routes_before


# ---- trial stages, It moves, and the ATO minimum test (design_new.md "Speed
# calibration", The pass starts with two trial stages; ATO minimum test) ----


def _with_instance(state, train_id):
    """The train's own instance profile (design_new.md "SpeedProfile", Every
    train owns exactly one instance profile)."""
    train = state.network.trains[train_id]
    return state.speed_profile_registry.get(train.speed_profile_id)


def _opened_pass(state):
    sim = state.sim_controller
    train_id = _free_train(state)
    _with_instance(state, train_id)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    return sim, train_id, block_id, run


def _instance(state, train_id):
    train = state.network.trains[train_id]
    return state.speed_profile_registry.get(train.speed_profile_id)


def test_it_moves_saves_both_voltages_on_the_instance_and_starts_the_sweep(state):
    sim, train_id, _block_id, run = _opened_pass(state)
    train_service.calibrate_trial_voltage(state, train_id, track_voltage=1.2)
    train_service.calibrate_keep_lighting(state, train_id, lighting_voltage=1.2)
    train_service.calibrate_trial_voltage(state, train_id, track_voltage=2.4)
    train_service.calibrate_it_moves(state, train_id)

    record = _instance(state, train_id)
    assert record.lighting_voltage == 1.2
    assert record.starting_voltage == 2.4
    assert run.pass_stage == PASS_SWEEP
    assert run.starting_voltage == 2.4
    sim.update(0.1)
    assert run.is_measuring


def test_it_moves_is_refused_while_the_lighting_voltage_is_not_below_it(state):
    from services import ConflictError

    _sim, train_id, _block_id, run = _opened_pass(state)
    train_service.calibrate_keep_lighting(state, train_id, lighting_voltage=2.0)
    train_service.calibrate_trial_voltage(state, train_id, track_voltage=1.5)
    with pytest.raises(ConflictError, match="lighting voltage"):
        train_service.calibrate_it_moves(state, train_id)
    assert run.pass_stage == PASS_STARTING_TRIAL
    assert "lighting voltage you kept" in run.trial_save_error
    # Keep saved the lighting voltage, with a starting voltage just above it.
    assert _instance(state, train_id).lighting_voltage == 2.0
    assert _instance(state, train_id).starting_voltage == pytest.approx(2.1)


def test_keep_saves_the_lighting_voltage_at_once(state):
    """design_new.md "Speed calibration", The pass starts with two trial
    stages: Keep saves the lighting voltage; when the stored starting voltage
    is not above it, the same write stores one CALIBRATION_TRIAL_STEP_V above
    it, which It moves replaces. It stays saved when the pass ends."""
    from constants import CALIBRATION_TRIAL_STEP_V

    sim, train_id, _block_id, run = _opened_pass(state)
    train_service.calibrate_keep_lighting(state, train_id, lighting_voltage=2.2)
    record = _instance(state, train_id)
    assert record.lighting_voltage == 2.2
    assert record.starting_voltage == pytest.approx(2.2 + CALIBRATION_TRIAL_STEP_V)
    assert run.pass_stage == PASS_STARTING_TRIAL

    train_service.calibrate_cancel(state, train_id)
    for _ in range(30):
        sim.update(0.1)
    assert train_id not in sim.speed_calibration.runs
    assert _instance(state, train_id).lighting_voltage == 2.2


def test_no_lamps_saves_no_lighting_voltage(state):
    from dataclasses import replace

    _sim, train_id, _block_id, _run = _opened_pass(state)
    registry = state.speed_profile_registry
    record = _instance(state, train_id)
    registry.register(replace(record, lighting_voltage=0.5, starting_voltage=2.0))
    train_service.calibrate_keep_lighting(state, train_id, lighting_voltage=None)
    stored = _instance(state, train_id)
    assert stored.lighting_voltage is None
    assert stored.starting_voltage == 2.0  # untouched


def test_a_refused_keep_stays_in_the_lighting_trial(state):
    from services import ConflictError

    _sim, train_id, _block_id, run = _opened_pass(state)
    top = state.speed_profile_registry.get(
        state.network.trains[train_id].speed_profile_id
    ).max_voltage
    with pytest.raises(ConflictError, match="max_voltage"):
        train_service.calibrate_keep_lighting(state, train_id, lighting_voltage=top - 0.05)
    assert run.pass_stage == PASS_LIGHTING_TRIAL
    assert _instance(state, train_id).lighting_voltage is None


def test_the_ato_minimum_test_commits_its_voltage_onto_the_instance(state):
    sim = state.sim_controller
    train_id = _free_train(state)
    _with_instance(state, train_id)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    resolved = state.speed_profile_registry.get(
        state.network.trains[train_id].speed_profile_id
    )
    voltage = resolved.first_point_voltage + 1.0
    run = train_service.calibrate_ato_minimum_test(
        state, train_id, measured_block_id=block_id, track_voltage=voltage
    )
    assert run.is_test and run.current_voltage == voltage
    sim.update(0.1)
    written = train_service.calibrate_ato_minimum_commit(state, train_id)
    assert written.ato_min_voltage == voltage
    assert _instance(state, train_id).ato_min_voltage == voltage
    # The commit ends the test once the train stands (An operator ending
    # stops the train before it lets it go).
    for _ in range(20):
        sim.update(0.1)
    assert train_id not in sim.speed_calibration.runs


def test_the_ato_minimum_commit_is_refused_when_the_curve_changed(state):
    from services import ConflictError, speed_profile_service

    sim = state.sim_controller
    train_id = _free_train(state)
    _with_instance(state, train_id)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    profile_id = state.network.trains[train_id].speed_profile_id
    resolved = state.speed_profile_registry.get(profile_id)
    train_service.calibrate_ato_minimum_test(
        state, train_id, measured_block_id=block_id,
        track_voltage=resolved.first_point_voltage,
    )
    speed_profile_service.update_profile(
        state, profile_id, data={"starting_voltage": resolved.starting_voltage + 0.1}
    )
    with pytest.raises(ConflictError, match="changed"):
        train_service.calibrate_ato_minimum_commit(state, train_id)


def test_a_curve_commit_clears_the_locomotives_ato_minimum_voltage(state):
    """design_new.md "Speed calibration", A commit clears the ATO minimum
    voltage of every locomotive whose curve it changes."""
    from dataclasses import replace

    from speed_calibration import PHASE_MEASURED, CalibrationSample

    _sim, train_id, _block_id, run = _opened_pass(state)
    registry = state.speed_profile_registry
    record = _instance(state, train_id)
    resolved = registry.get(record.profile_id)
    registry.register(replace(record, ato_min_voltage=resolved.first_point_voltage))
    top = resolved.max_voltage
    run.samples = [
        CalibrationSample(track_voltage=v, speed_kmh=v * 5.0)
        for v in (top / 4, top / 2, top)
    ]
    run.phase = PHASE_MEASURED
    train_service.calibrate_commit(state, train_id, target="train")
    stored = _instance(state, train_id)
    assert stored.ato_min_voltage is None
    assert stored.calibrated_at is not None


def test_the_trial_routes_drive_the_pass_into_its_sweep(state):
    """The panel's trial controls go through `/calibrate/trial-voltage`,
    `/calibrate/keep-lighting` and `/calibrate/it-moves`; a value the run
    refuses answers 409."""
    from app_factory import create_app

    sim, train_id, _block_id, run = _opened_pass(state)
    app = create_app(state)
    app.config["TESTING"] = True
    base = f"/api/trains/{train_id}/calibrate"
    with app.test_client() as client:
        r = client.post(f"{base}/trial-voltage", json={"track_voltage": 1.1})
        assert r.status_code == 200 and r.get_json()["trial_voltage"] == 1.1
        assert r.get_json()["pass_stage"] == "lighting_trial"
        r = client.post(f"{base}/keep-lighting", json={"lighting_voltage": 1.1})
        assert r.status_code == 200 and r.get_json()["pass_stage"] == "starting_trial"
        assert client.post(f"{base}/trial-voltage", json={"track_voltage": 99}).status_code == 409
        client.post(f"{base}/trial-voltage", json={"track_voltage": 2.2})
        r = client.post(f"{base}/it-moves")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["pass_stage"] == "sweep"
        assert r.get_json()["starting_voltage"] == 2.2
    assert run.pass_stage == PASS_SWEEP


def test_stop_mid_leg_brings_the_train_to_a_stand_before_letting_it_go(state):
    """Rig 2026-11-06: Stop at 8 V cut the drive and dropped the leg route on
    the same tick; the locomotive rolled on into blocks no train held and was
    released as lost. Stop now keeps the leg route until the train stands."""
    from tests.train_test_helpers import skip_calibration_trials

    sim, train_id, _block_id, run = _opened_pass(state)
    skip_calibration_trials(run)
    controller = sim.get_or_create_controller(train_id)
    for _ in range(40):
        sim.update(0.1)
    assert controller.physics_sync.current_speed_kmh > 0.0
    route_id = sim.route_executor.assigned_route_id_for(train_id)
    assert route_id is not None

    train_service.calibrate_cancel(state, train_id)
    sim.update(0.1)
    # Still ending, still holding its leg route, driving half the step.
    assert run.ending and train_id in sim.speed_calibration.runs
    assert sim.route_executor.assigned_route_id_for(train_id) == route_id
    for _ in range(30):
        sim.update(0.1)
    assert train_id not in sim.speed_calibration.runs
    assert controller.is_stopped()
    assert not controller.eb.active
    assert controller.powered
    assert controller.commands.control_mode == "manual"


def _held_blocks_reserved_for(state, train_id):
    return [
        block_id
        for block_id, block in state.network.blocks.items()
        if getattr(block, "reserved_for_train", None) == train_id
        or getattr(block, "locked_by_train", None) == train_id
    ]


def test_the_ato_minimum_commit_gives_an_ato_train_back_its_mode(state):
    """A run the operator ends gives the train back (design_new.md "What ends a
    calibration run"): an ATO train calibrated and committed reads ATO again,
    with no route and no reserved block left behind."""
    sim = state.sim_controller
    train_id = _free_train(state)
    _with_instance(state, train_id)
    block_id = _first_calibratable_block(state, train_id)
    controller = _ready_manual_train(sim, state.network, train_id)
    controller.commands.set_mode("ato")
    sim.update(0.1)
    resolved = state.speed_profile_registry.get(
        state.network.trains[train_id].speed_profile_id
    )
    run = train_service.calibrate_ato_minimum_test(
        state,
        train_id,
        measured_block_id=block_id,
        track_voltage=resolved.first_point_voltage + 1.0,
    )
    assert run.control_mode_before_opening == "ato"
    assert controller.commands.control_mode == "manual"
    for _ in range(20):
        sim.update(0.1)
    train_service.calibrate_ato_minimum_commit(state, train_id)
    for _ in range(60):
        sim.update(0.1)
        if train_id not in sim.speed_calibration.runs and not sim.speed_calibration.pending_give_backs:
            break
    sim.update(0.1)
    assert train_id not in sim.speed_calibration.runs
    assert controller.is_stopped()
    assert controller.commands.control_mode == "ato"
    assert controller.ops.train_status != TrainStatus.CALIBRATING
    assert sim.route_executor.assigned_route_id_for(train_id) is None
    assert controller.path_projection.received_path_plan is None
    assert _held_blocks_reserved_for(state, train_id) == []


def test_stopping_a_run_opened_on_an_off_train_powers_it_off_again(state):
    from tests.train_test_helpers import skip_calibration_trials

    sim = state.sim_controller
    train_id = _free_train(state)
    _with_instance(state, train_id)
    block_id = _first_calibratable_block(state, train_id)
    controller = _ready_manual_train(sim, state.network, train_id)
    controller.commands.power_off_cmd()
    sim.update(0.1)
    assert not controller.powered
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    assert controller.powered and run.powered_before_opening is False
    skip_calibration_trials(run)
    for _ in range(20):
        sim.update(0.1)
    train_service.calibrate_cancel(state, train_id)
    for _ in range(60):
        sim.update(0.1)
    assert train_id not in sim.speed_calibration.runs
    assert not controller.powered


def test_an_emergency_brake_ending_leaves_the_train_in_manual(state):
    """EB ends a run without giving the train back: another procedure owns it."""
    sim = state.sim_controller
    train_id = _free_train(state)
    _with_instance(state, train_id)
    block_id = _first_calibratable_block(state, train_id)
    controller = _ready_manual_train(sim, state.network, train_id)
    controller.commands.set_mode("ato")
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    controller.eb.activate()
    for _ in range(40):
        sim.update(0.1)
    assert train_id not in sim.speed_calibration.runs
    assert sim.speed_calibration.pending_give_backs == {}
    assert controller.commands.control_mode == "manual"
