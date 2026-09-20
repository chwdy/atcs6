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

from app_state import AppState
from bootstrap import bootstrap_runtime
from services import train_service
from speed_calibration import PHASE_FAILED

pytestmark = pytest.mark.scene("demo_6")


@pytest.fixture
def state():
    """The demo_6 scene behind an `AppState`, which is what the service takes."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, persistence = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=persistence,
        simulation_lock=threading.Lock(),
        simulation_running=False,
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
    # Drop the scene's queued start-up commands (power_on + set_mode("ato")):
    # they would otherwise drain on a later tick and flip the train back to ATO,
    # which calibration refuses. This helper sets power and mode directly.
    controller.commands.command_inbox.clear()
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


def test_a_block_with_no_recorded_length_is_refused(state):
    from services import ValidationError

    for block_id, block in state.network.blocks.items():
        if float(getattr(block, "length_cm", 0) or 0) <= 0:
            with pytest.raises(ValidationError):
                train_service.resolve_calibration_stretch(state, block_id)
            return
    pytest.skip("every demo_6 block has a length")


def test_the_pass_is_refused_unless_the_train_is_manual_and_stopped(state):
    from services import ConflictError

    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    controller = sim.get_or_create_controller(train_id)
    controller.commands.power_on_cmd()
    controller.commands.set_mode("ato")

    with pytest.raises(ConflictError):
        train_service.calibrate_train(
            state, train_id, measured_block_id=block_id
        )


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
    assert run.current_power == 10.0

    # Which outer block this opening leg runs toward — the one the train is not
    # standing in — is the far end the leg route's stop task targets.
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
    assert leg_route.is_active is True
    assert train_id in leg_route.assigned_train_ids
    assert leg_route.manual_override is False
    # One task: a stop at the far outer block.
    assert len(leg_route.tasks) == 1
    assert leg_route.tasks[0].task_kind == "stop"
    assert leg_route.tasks[0].task_location.block_id == far_block

    # The leg has real authority (a path), and the step's power drives the train
    # back through get_propulsion_override, not a zero no-authority cap.
    assert sim.path_dispatcher.active_path_id_by_train(train_id) is not None
    override = sim.speed_calibration.get_propulsion_override(train_id)
    assert override is not None and override.kind == "power_percent"
    assert override.value == run.current_power

    sim.update(0.1)
    assert run.phase != PHASE_FAILED or "authority" in run.failure_reason


def test_a_pass_writes_nothing_until_it_is_committed(state):
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    profile = state.network.speed_profiles.get(
        state.network.trains[train_id].speed_profile_id
    )
    before = list(profile.power_speed_curve)

    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    for _ in range(20):
        sim.update(0.1)

    assert profile.power_speed_curve == before
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
    assert train_service.calibrate_status(state, train_id) is None

    # Cancelling abandons the pass and tears its leg route down through the
    # ordinary route-deactivate latch, not a direct dispatcher call (design_new.md
    # "Speed calibration", Pass-abandonment cases command a stop and deactivate
    # the leg route). The inclusive latch also cancels the still-live leg path.
    controller = sim.controllers[train_id]
    assert controller.route_hooks.phase_latches.route_deactivate is True

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
    so no spent run leaks (design_new.md "Speed calibration", Where it runs — the
    OFF gate is passed after the command drain, so step `26f5` finds the pass
    invalid and ends it that tick). A leaked run would otherwise block a new pass
    at the readiness gate."""
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
    for _ in range(10):
        sim.update(0.1)
        if sim.speed_calibration.runs.get(train_id) is None:
            break
    assert sim.speed_calibration.runs.get(train_id) is None


def test_fatal_halt_clear_ends_an_open_speed_run(state):
    """A fatal-halt clear ends every open calibration run directly during step
    `7c41`, not through an Action pass (design_new.md "Speed calibration", Where
    it runs)."""
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
    sim.clear_failed_runtime()
    assert sim.speed_calibration.runs.get(train_id) is None, (
        "a fatal-halt clear must end every open calibration run"
    )


def test_a_calibration_leg_completes_through_ordinary_stop_completion(state):
    """The `9d8b` gate, end to end: a calibration leg runs as a one-off route and
    finishes through ordinary stop completion — Observe step `9d8b`
    (`PathDispatcher.try_complete_active_path`) completes the leg route's path —
    rather than through a latch-cancel. This is the mechanism the "success =
    ordinary stop completion" model rests on (design_new.md "Speed calibration",
    A leg finishes through ordinary stop completion), and it fires for a
    manual-mode, override-driven train exactly as it does for an ATO one.

    demo_6's reachable calibration stretches all have a far outer block shorter
    than the stopping safety margin, so the train comes to rest at the leg
    route's authority end without entering the far block; the leg route still
    completes through `9d8b`, and the pass then fails "never reached" because no
    crossing was timed. What this test pins is the completion mechanism, not the
    measurement outcome — the far block being too short to enter is a property of
    the demo layout, not of the leg-route lifecycle.
    """
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    profile = state.network.speed_profiles.get(
        state.network.trains[train_id].speed_profile_id
    )
    curve_before = list(profile.power_speed_curve)

    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)

    armed_a_one_off_route = False
    completed_via_stop_completion = False
    for _ in range(500):
        if run.active_leg_route_id:
            armed_a_one_off_route = True
        sim.update(0.1)
        # A leg-route path that reached COMPLETED is the signature of `9d8b`
        # completing it (a cancelled/abandoned path never reads COMPLETED).
        for pid, path in sim.path_dispatcher.paths.items():
            if pid.startswith(f"PATH_{train_id}") and path.status.value == "completed":
                completed_via_stop_completion = True
        if completed_via_stop_completion:
            break

    assert armed_a_one_off_route, "the pass must arm at least one one-off leg route"
    assert completed_via_stop_completion, (
        "a calibration leg route must finish through ordinary stop completion "
        "(Observe step `9d8b`), not a latch-cancel"
    )
    # The pass writes nothing to the profile until an operator commits.
    assert profile.power_speed_curve == curve_before
    assert profile.calibrated_at is None


def test_run_still_valid_survives_the_leg_completion_window(state, monkeypatch):
    """A clean multi-leg speed pass must survive a leg boundary.

    On the tick a calibration leg completes, Plan settlement unassigns the leg's
    one-off route (so `RouteExecutor.assigned_route_id_for(train_id)` returns
    `None`) before `_prepare_calibration_leg` clears
    `CalibrationRun.active_leg_route_id` (that clear runs on the next Plan step
    `c9e1`). `run_still_valid` must treat `assigned is None` as "between legs" and
    keep the pass open; otherwise the pass abandons on its first clean leg
    boundary (design_new.md "Speed calibration", Where it runs — only a
    non-calibration route ends the pass).
    """
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)  # arm the leg one-off route
    run = sim.speed_calibration.runs.get(train_id)
    assert run is not None and run.active_leg_route_id is not None
    leg_route_id = run.active_leg_route_id
    # While the leg's own route is assigned, the pass is valid.
    assert sim.route_executor.assigned_route_id_for(train_id) == leg_route_id
    assert sim.speed_calibration.run_still_valid(train_id) is True

    # Reproduce the completion-tick window: the leg's route is unassigned
    # (assigned -> None) but active_leg_route_id is not yet cleared.
    sim.route_executor.remove_train_route_run(train_id)
    assert sim.route_executor.assigned_route_id_for(train_id) is None
    assert run.active_leg_route_id == leg_route_id  # cleared only on the next c9e1
    assert sim.speed_calibration.run_still_valid(train_id) is True, (
        "the pass must survive the leg-completion window (assigned None while "
        "active_leg_route_id is still set), not abandon on it"
    )

    # A genuinely non-calibration route assigned still ends the pass.
    monkeypatch.setattr(
        sim.speed_calibration, "_assigned_route_id", lambda _tid: "OTHER_ROUTE"
    )
    assert sim.speed_calibration.run_still_valid(train_id) is False
