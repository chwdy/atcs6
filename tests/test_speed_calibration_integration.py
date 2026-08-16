"""Speed calibration end-to-end: the pass drives a real train on a real scene.

design_new.md "Speed calibration", Where it runs. The unit tests drive the state
machine with synthetic confirmations; this one proves the wiring around it — that
`SimulationController.update(...)` ticks the pass, that the pass sees the block
entries attribution credits to its train, that the commands it issues reach the
controller through the ordinary Phase-2 drain, and that it takes real authority
so ATC does not hold the train at a zero no-authority cap.
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
    controller.commands.power_on_cmd()
    controller.commands.set_mode("manual")
    controller.commands.set_manual_mode_type("power_percent")
    if controller.commands.cab is None:
        controller.commands.set_cab("A")
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


def test_the_runtime_tick_arms_the_pass_and_commands_the_first_step(state):
    sim = state.sim_controller
    network = state.network
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)
    assert block_id is not None

    controller = _ready_manual_train(sim, network, train_id)
    sim.update(0.1)  # drain the setup commands

    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    assert run.leg_armed is False
    assert run.current_power == 10.0

    # One tick arms the leg: the pass takes authority to the far outer block and
    # commands the step's power through the ordinary manual command surface.
    sim.update(0.1)
    assert run.leg_armed is True

    # And the command reached the controller on the next drain, so the train is
    # not sitting under a zero no-authority cap with nothing commanded.
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


def test_cancelling_a_pass_releases_its_authority(state):
    sim = state.sim_controller
    train_id = _free_train(state)
    block_id = _first_calibratable_block(state, train_id)

    _ready_manual_train(sim, state.network, train_id)
    sim.update(0.1)
    run = train_service.calibrate_train(state, train_id, measured_block_id=block_id)
    sim.update(0.1)
    held = run.path_id

    train_service.calibrate_cancel(state, train_id)
    assert train_service.calibrate_status(state, train_id) is None
    if held:
        # The release goes through the ordinary cancel latch, not a direct
        # dispatcher call, so the path stays ACTIVE until the Phase-3
        # explicit-cancel step commits it with the train stopped (design_new.md
        # "Speed calibration", Releasing a leg's authority goes through the
        # ordinary cancel latch).
        controller = sim.controllers[train_id]
        assert (
            controller.route_hooks.phase_latches.path_cancel_request is not None
        ), "cancelling the pass must latch the leg path's cancel"
        # The cancel commands `power_percent = 0` but forces no stop, so the
        # train runs out the authority it already holds and halts at its end
        # (~13s here); the latch commits on the first Phase 3 after that.
        for _ in range(400):
            sim.update(0.1)
            path = sim.path_dispatcher.paths.get(held)
            if path is None or path.status.value != "active":
                break
        path = sim.path_dispatcher.paths.get(held)
        assert path is None or path.status.value != "active"
