"""Lighting-voltage calibration: candidate stepping, bounds, and the commit.

design_new.md "Lighting-voltage calibration": the pass puts a candidate voltage
on the rails and the operator says whether the locomotive moved — there is no
measurement step, because at this level the drive is too weak to load a current
channel and first movement is a creep of a few millimetres. The candidate stays
below the profile's `max_voltage` and below the Hardware Setup's
`current_sense_min_drive_v`, and committing moves the 0%-power anchor of
`voltage_power_curve` with it.
"""

import pytest

from constants import LIGHTING_VOLTAGE_STEP_V
from enums import TrainState
from hardware_config import DEFAULT_CURRENT_SENSE_MIN_DRIVE_V
from speed_calibration import SpeedCalibration
from speed_profile import make_default_speed_profile
from train_physics import TrainPhysics


class _StubTrain:
    train_id = "T1"


def _open_run(profile=None):
    profile = profile or make_default_speed_profile("T1", 100.0)
    cal = SpeedCalibration()
    run = cal.start_lighting(_StubTrain(), profile)
    return cal, profile, run


# --------------------------------------------------------------- stepping ---


def test_run_starts_at_the_profiles_stored_lighting_voltage():
    cal, profile, run = _open_run()
    assert run.candidate_voltage == pytest.approx(profile.lighting_voltage)
    assert cal.lighting_candidate("T1") == pytest.approx(profile.lighting_voltage)


def test_each_step_moves_one_constant_step():
    cal, profile, _run = _open_run()
    start = profile.lighting_voltage
    cal.step_lighting("T1", 1)
    assert cal.lighting_candidate("T1") == pytest.approx(start + LIGHTING_VOLTAGE_STEP_V)
    cal.step_lighting("T1", -1)
    assert cal.lighting_candidate("T1") == pytest.approx(start)


def test_step_up_stops_at_the_ceiling_and_down_stops_at_zero():
    """The panel can never show a value the commit would refuse."""
    cal, profile, run = _open_run()
    for _ in range(500):
        cal.step_lighting("T1", 1)
    assert cal.lighting_candidate("T1") == pytest.approx(run.ceiling_voltage)
    for _ in range(500):
        cal.step_lighting("T1", -1)
    assert cal.lighting_candidate("T1") == 0.0


def test_ceiling_sits_below_the_current_sense_drive_level():
    """`current_sense_min_drive_v` must stay above `lighting_voltage`, so a lit
    but motionless train lands in the lighting zone rather than reading as
    driven (design_new.md "Power", Occupancy source follows drive state)."""
    profile = make_default_speed_profile("T1", 100.0)
    ceiling = SpeedCalibration.lighting_ceiling(profile)
    assert ceiling < DEFAULT_CURRENT_SENSE_MIN_DRIVE_V
    assert ceiling < profile.max_voltage


def test_ceiling_follows_max_voltage_when_it_is_the_lower_bound():
    profile = make_default_speed_profile("T1", 100.0)
    profile.max_voltage = 2.0
    assert SpeedCalibration.lighting_ceiling(profile) == pytest.approx(
        2.0 - LIGHTING_VOLTAGE_STEP_V
    )


def test_stepping_without_an_open_run_is_a_no_op():
    cal = SpeedCalibration()
    assert cal.step_lighting("T1", 1) is None
    assert cal.lighting_candidate("T1") is None


# ----------------------------------------------------------------- commit ---


def test_commit_writes_the_candidate_and_closes_the_run():
    cal, profile, _run = _open_run()
    cal.step_lighting("T1", 1)
    candidate = cal.lighting_candidate("T1")
    assert cal.commit_lighting("T1", profile) == pytest.approx(candidate)
    assert profile.lighting_voltage == pytest.approx(candidate)
    assert cal.lighting_candidate("T1") is None


def test_commit_moves_the_curves_zero_power_anchor_up():
    """Points at or below the new anchor name voltages that do not move this
    locomotive, so they are dropped."""
    profile = make_default_speed_profile("T1", 100.0)
    profile.voltage_power_curve = [(1.5, 0.0), (2.0, 20.0), (3.0, 50.0), (12.0, 100.0)]
    cal, profile, _run = _open_run(profile)
    for _ in range(10):  # 1.5 -> 2.5
        cal.step_lighting("T1", 1)
    anchor = cal.commit_lighting("T1", profile)
    assert anchor == pytest.approx(2.5)
    assert profile.voltage_power_curve == [(2.5, 0.0), (3.0, 50.0), (12.0, 100.0)]


def test_commit_moving_the_anchor_down_leaves_exactly_one_zero_power_point():
    """The old anchor sits above the new one, so dropping only points at or
    below the anchor would leave the curve with two 0%-power points."""
    profile = make_default_speed_profile("T1", 100.0)
    profile.voltage_power_curve = [(1.5, 0.0), (2.0, 20.0), (12.0, 100.0)]
    cal, profile, _run = _open_run(profile)
    for _ in range(5):  # 1.5 -> 1.0
        cal.step_lighting("T1", -1)
    anchor = cal.commit_lighting("T1", profile)
    zero_points = [pt for pt in profile.voltage_power_curve if pt[1] == 0.0]
    assert zero_points == [(anchor, 0.0)]
    assert profile.voltage_power_curve == [(1.0, 0.0), (2.0, 20.0), (12.0, 100.0)]


def test_committed_curve_still_round_trips_both_anchors():
    cal, profile, _run = _open_run()
    for _ in range(6):
        cal.step_lighting("T1", 1)
    anchor = cal.commit_lighting("T1", profile)
    assert profile.voltage_to_power(anchor) == pytest.approx(0.0)
    assert profile.voltage_to_power(profile.max_voltage) == pytest.approx(100.0)


def test_cancel_discards_the_candidate():
    cal, profile, _run = _open_run()
    before = profile.lighting_voltage
    cal.step_lighting("T1", 1)
    cal.cancel_lighting("T1")
    assert cal.lighting_candidate("T1") is None
    assert profile.lighting_voltage == pytest.approx(before)


# ---------------------------------------------------------------- physics ---


class _StubOps:
    state = TrainState.READY


class _StubController:
    """Enough of a controller for `compute_track_voltage` — including the
    `simulation_controller` back-reference physics reads the run through."""

    def __init__(self, simulation_controller=None):
        self.state = TrainState.READY
        self.ops = _StubOps()
        self.simulation_controller = simulation_controller


class _StubSim:
    def __init__(self, calibration):
        self.speed_calibration = calibration


def test_open_run_drives_its_candidate_in_place_of_the_lit_voltage():
    cal, profile, _run = _open_run()
    physics = TrainPhysics(train_id="T1")
    controller = _StubController(_StubSim(cal))

    for _ in range(4):
        cal.step_lighting("T1", 1)
    candidate = cal.lighting_candidate("T1")
    assert candidate != pytest.approx(profile.lighting_voltage)
    assert physics.compute_track_voltage(controller, profile) == pytest.approx(candidate)


def test_closing_the_run_returns_the_rail_to_the_stored_lit_voltage():
    cal, profile, _run = _open_run()
    physics = TrainPhysics(train_id="T1")
    controller = _StubController(_StubSim(cal))

    cal.cancel_lighting("T1")
    assert physics.compute_track_voltage(controller, profile) == pytest.approx(
        profile.lighting_voltage
    )


def test_a_powered_off_train_stays_dead_while_a_run_is_open():
    cal, profile, _run = _open_run()
    physics = TrainPhysics(train_id="T1")
    controller = _StubController(_StubSim(cal))
    controller.state = TrainState.OFF
    controller.ops.state = TrainState.OFF

    assert physics.compute_track_voltage(controller, profile) == 0.0


# --------------------------------------------------- service-layer gates ----


@pytest.fixture
def state_with_train():
    """AppState with one placed train on its own SpeedProfile."""
    import threading
    from datetime import datetime

    from app_state import AppState
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from persistence import PersistenceManager
    from simulation import SimulationController
    from train import Train, TrainCar

    network = RailwayNetwork()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    profile = make_default_speed_profile("PROF_A", 80.0)
    network.speed_profiles.register(profile)
    train = Train(
        train_id="TRAIN_A",
        consist=[TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)],
        max_speed=80.0,
    )
    train.speed_profile_id = profile.profile_id
    network.add_train(train)
    return state


def _ready_manual_with_cab(state):
    """Bring the fixture train to the one state a run may open from."""
    from services import train_service

    controller = state.sim_controller._get_or_create_controller(
        state.network.trains["TRAIN_A"]
    )
    controller.commands.set_mode("manual")
    controller.commands.power_on_cmd()
    controller.commands.set_cab("A")
    assert controller.ops.state is TrainState.READY
    assert controller.commands.cab == "A"
    return train_service, controller


def test_service_opens_a_run_from_ready_manual_with_a_cab(state_with_train):
    train_service, _controller = _ready_manual_with_cab(state_with_train)
    run = train_service.start_lighting_calibration(state_with_train, "TRAIN_A")
    assert run.train_id == "TRAIN_A"
    assert (
        state_with_train.sim_controller.speed_calibration.lighting_candidate("TRAIN_A")
        is not None
    )


def test_service_rejects_a_run_without_a_cab(state_with_train):
    """The sign put on each block is the direction the cab picked, so without
    one the operator cannot know which way the locomotive will creep."""
    from services import ConflictError

    train_service, controller = _ready_manual_with_cab(state_with_train)
    controller.commands.set_cab(None)

    with pytest.raises(ConflictError, match="cab"):
        train_service.start_lighting_calibration(state_with_train, "TRAIN_A")


def test_service_rejects_a_run_in_ato_mode(state_with_train):
    from services import ConflictError

    train_service, controller = _ready_manual_with_cab(state_with_train)
    controller.commands.set_mode("ato")

    with pytest.raises(ConflictError, match="manual"):
        train_service.start_lighting_calibration(state_with_train, "TRAIN_A")


def test_service_rejects_a_run_while_a_speed_pass_is_open(state_with_train):
    from services import ConflictError

    train_service, _controller = _ready_manual_with_cab(state_with_train)
    calibration = state_with_train.sim_controller.speed_calibration
    profile = state_with_train.network.speed_profiles.get(
        state_with_train.network.trains["TRAIN_A"].speed_profile_id
    )
    calibration.start(state_with_train.network.trains["TRAIN_A"], profile, mode="manual")

    with pytest.raises(ConflictError, match="speed-calibration"):
        train_service.start_lighting_calibration(state_with_train, "TRAIN_A")


def test_step_and_save_need_an_open_run(state_with_train):
    from services import ConflictError

    train_service, _controller = _ready_manual_with_cab(state_with_train)
    with pytest.raises(ConflictError, match="No lighting-voltage run"):
        train_service.step_lighting_calibration(state_with_train, "TRAIN_A", 1)
    with pytest.raises(ConflictError, match="No lighting-voltage run"):
        train_service.save_lighting_calibration(state_with_train, "TRAIN_A")


def test_service_save_writes_the_profile_and_closes_the_run(state_with_train):
    train_service, _controller = _ready_manual_with_cab(state_with_train)
    train_service.start_lighting_calibration(state_with_train, "TRAIN_A")
    train_service.step_lighting_calibration(state_with_train, "TRAIN_A", 1)

    saved = train_service.save_lighting_calibration(state_with_train, "TRAIN_A")
    profile = state_with_train.network.speed_profiles.get(
        state_with_train.network.trains["TRAIN_A"].speed_profile_id
    )
    assert profile.lighting_voltage == pytest.approx(saved)
    assert (
        state_with_train.sim_controller.speed_calibration.lighting_candidate("TRAIN_A")
        is None
    )
