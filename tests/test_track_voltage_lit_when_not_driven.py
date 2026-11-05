"""A powered train that nothing drives outputs `lighting_voltage`.

design_new.md "TrainPhysics", Voltage behavior: a speed of 0 is 0% power, and
0% power is the profile's `lighting_voltage`, or 0 V when it is unset. So a
powered train with a lighting voltage outputs it whether or not it holds a
reservation, in `manual` or `ato`, under a plain EB, and in ATO while not
engaged. Only an OFF train and an emergency cut-out output 0 V whatever the
profile holds. ATO and manual driving both take the voltage from
the train's current speed, never from a target speed it has not reached.

Regression for the 2026-10-23 rig run: assigning a route to a lit manual train
dropped its rails to 0 V, and switching it to ATO without Go put ~7.7 V on the
rails while the calculated span stood still, so the locomotive ran off by itself
and stopped dead when the detector found it in the next block.
"""

from __future__ import annotations

import contextlib
import io
import threading
from datetime import datetime

import pytest

from app_state import AppState
from bootstrap import bootstrap_runtime
from enums import TrainStatus
from persistence import PersistenceManager
from services import train_route_service, train_service

pytestmark = pytest.mark.scene("demo7_1")

TRAIN = "T"
START_BLOCK = "BLK_4F5CE3"
NEXT_BLOCK = "BLK_C37589A5"
DT = 0.12
LIGHTING_V = 1.2


@pytest.fixture
def world():
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _persistence = bootstrap_runtime(
            demo=True, scene="demo7_1", setup_train=False
        )
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    sim.add_train(TRAIN, START_BLOCK, "SOUTH", 100.0, 10.0, start_tile=(3, 8))
    controller = sim.get_or_create_controller(TRAIN)
    # The seeded profile sets no lighting voltage; give this locomotive one
    # below its starting voltage, as the operator does by hand.
    instance = network.speed_profiles.get(network.trains[TRAIN].speed_profile_id)
    instance.lighting_voltage = LIGHTING_V
    assert controller.commands.power_on_cmd().success
    return state, controller


def _tick(state, n: int = 1) -> None:
    for _ in range(n):
        state.sim_controller.update(DT, operating_delta_seconds=DT)


def _voltage(state, controller) -> float:
    physics = state.sim_controller.physics_instances[TRAIN]
    return physics.compute_track_voltage(controller, controller._get_speed_profile())


def _rail(state) -> float:
    """The magnitude actually commanded onto the block the train stands on."""
    power = state.network.power_controller
    return abs(float(power.get_recorded_voltage(START_BLOCK)))


def _lighting(controller) -> float:
    return controller._get_speed_profile().lighting_voltage


def _assign_route_to_next_block(state, controller) -> None:
    route = train_route_service.create_route(
        state,
        name="Quick",
        mode="one_off",
        tasks=[{
            "task_kind": "stop",
            "task_location": {"block_id": NEXT_BLOCK},
            "task_direction": "either",
        }],
    )
    train_service.assign_route(state, TRAIN, route_id=route.route_id)
    _tick(state, 2)
    assert controller.path_projection.received_path_plan is not None


def test_standing_train_stays_lit_when_its_route_reserves(world):
    state, controller = world
    _tick(state, 2)
    assert controller.path_projection.received_path_plan is None
    assert _voltage(state, controller) == pytest.approx(_lighting(controller))

    # A powered manual train with a route gets a reservation without Go. It is
    # still standing, so it is still lit.
    _assign_route_to_next_block(state, controller)
    assert controller.is_stopped()
    assert _voltage(state, controller) == pytest.approx(_lighting(controller))
    assert _rail(state) == pytest.approx(_lighting(controller))


def test_switching_to_ato_without_go_keeps_the_train_lit_and_still(world):
    state, controller = world
    _assign_route_to_next_block(state, controller)

    assert controller.commands.set_mode("ato").success
    assert controller.ops.train_status == TrainStatus.ATO
    assert controller.commands.engaged is False
    _tick(state, 10)

    # Not engaged: the ATO driving rule commands 0, so the rails carry the lit
    # voltage and nothing more.
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert _voltage(state, controller) == pytest.approx(_lighting(controller))
    assert _rail(state) == pytest.approx(_lighting(controller))
    assert controller.path_projection.received_path_plan is not None


def test_ato_voltage_follows_the_current_speed_not_the_target(world):
    state, controller = world
    _assign_route_to_next_block(state, controller)
    profile = controller._get_speed_profile()

    assert controller.commands.start_route().success
    _tick(state, 1)
    # ATO has notched up, but the train has not moved yet: still lit, not the
    # voltage for the lookahead target it is heading for.
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert float(controller.target_speed or 0.0) > 10.0
    assert _voltage(state, controller) == pytest.approx(_lighting(controller))

    _tick(state, 5)
    speed = controller.physics_sync.current_speed_kmh
    assert speed > 0.0
    assert _voltage(state, controller) == pytest.approx(
        profile.speed_to_voltage(speed)
    )
    assert _voltage(state, controller) < profile.speed_to_voltage(
        float(controller.target_speed)
    )


def test_plain_eb_is_lit_and_the_cut_out_is_dead(world):
    state, controller = world
    _tick(state, 1)

    assert controller.eb.activate().success
    _tick(state, 1)
    assert _voltage(state, controller) == pytest.approx(_lighting(controller))

    assert controller.eb.release().success
    controller.eb.activate_cut_out()
    _tick(state, 1)
    assert _voltage(state, controller) == 0.0


def test_off_train_is_dead(world):
    state, controller = world
    _tick(state, 1)
    assert controller.commands.power_off_cmd().success
    _tick(state, 2)
    assert controller.ops.train_status == TrainStatus.OFF
    assert _voltage(state, controller) == 0.0
    assert _rail(state) == 0.0


def test_zero_power_percent_is_lit(world):
    state, controller = world
    assert controller.commands.set_manual_mode_type("power_percent").success
    assert controller.commands.set_manual_power_percent(0).success
    _tick(state, 2)
    assert _voltage(state, controller) == pytest.approx(_lighting(controller))


def test_a_standing_train_without_a_lighting_voltage_sits_at_zero_volts(world):
    """A locomotive whose lamps light only once it moves sets no lighting
    voltage; standing and powered, its blocks carry 0 V (design_new.md
    "TrainPhysics", A powered train that nothing drives outputs
    `lighting_voltage`)."""
    state, controller = world
    instance = state.network.speed_profiles.get(
        state.network.trains[TRAIN].speed_profile_id
    )
    instance.lighting_voltage = None
    _tick(state, 2)
    assert controller.ops.train_status != TrainStatus.OFF
    assert _voltage(state, controller) == 0.0
    assert _rail(state) == 0.0
