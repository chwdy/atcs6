"""Speed calibration's commit targets, through the service layer.

design_new.md "Speed calibration", What commit writes. Replace writes the
profile the calibrated train is linked to, so every train and roster entry
linked to it runs on the new curve — that is what a profile shared across one
model is for. Save as new forks a profile and relinks the calibrated train and
its roster entry to it, because the pass measured that locomotive and a profile
nothing named would be a measurement the layout never reads.
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from services import ConflictError
from services import train_service
from simulation import SimulationController
from speed_calibration import PHASE_MEASURED, CalibrationSample
from speed_profile import SpeedProfile
from train import Train
from train_roster import RosterEntry


@pytest.fixture
def state():
    network = RailwayNetwork()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=None,
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


@pytest.fixture
def calibrated(state):
    """A train linked to a shared model profile, with a finished pass open."""
    profile = SpeedProfile(
        profile_id="ef66", name="Kato EF66", model="Kato EF66"
    )
    state.network.speed_profiles.register(profile)

    entry = RosterEntry(entry_id="R1", model="Kato EF66", speed_profile_id="ef66")
    state.network.train_roster.add_entry(entry)

    train = Train(train_id="T1")
    train.speed_profile_id = "ef66"
    train.roster_entry_id = "R1"
    state.network.trains["T1"] = train

    # A second locomotive of the same model, sharing the one profile.
    other = Train(train_id="T2")
    other.speed_profile_id = "ef66"
    state.network.trains["T2"] = other

    cal = state.sim_controller.speed_calibration
    run = cal.start(train, profile, measured_block_id="B", outer_block_ids=("A", "C"))
    run.samples = [
        CalibrationSample(power_percent=p, speed_kmh=p * 0.8)
        for p in run.power_steps
    ]
    run.phase = PHASE_MEASURED
    return state, train, profile, entry, run


def test_replace_writes_the_shared_profile_so_every_loco_of_the_model_gets_it(
    calibrated,
):
    state, train, profile, entry, _run = calibrated
    written = train_service.calibrate_commit(
        state, "T1", target="replace", model="Kato EF66"
    )

    assert written is profile
    assert profile.calibrated_at is not None
    assert profile.power_speed_curve[-1] == (100.0, 80.0)
    # Both locomotives still name the one profile, so both run on the new curve.
    assert train.speed_profile_id == "ef66"
    assert state.network.trains["T2"].speed_profile_id == "ef66"
    assert entry.speed_profile_id == "ef66"


def test_save_as_new_forks_a_profile_and_relinks_the_train_and_its_entry(calibrated):
    state, train, profile, entry, _run = calibrated
    written = train_service.calibrate_commit(
        state, "T1", target="new", name="EF66 #2", model="Kato EF66"
    )

    assert written.profile_id != "ef66"
    assert written.name == "EF66 #2"
    assert written.calibrated_at is not None
    # The pass measured this locomotive, so the new profile is the one that
    # drives it — and its roster entry follows.
    assert train.speed_profile_id == written.profile_id
    assert entry.speed_profile_id == written.profile_id
    # The rest of the model is untouched.
    assert state.network.trains["T2"].speed_profile_id == "ef66"
    assert profile.calibrated_at is None
    assert state.network.speed_profiles.get(written.profile_id) is written


def test_save_as_new_copies_every_other_field_from_the_profile_it_ran_against(
    calibrated,
):
    state, _train, profile, _entry, _run = calibrated
    profile.acceleration_rate = 7.5
    profile.lighting_voltage = 2.25
    written = train_service.calibrate_commit(state, "T1", target="new")

    assert written.acceleration_rate == 7.5
    assert written.lighting_voltage == 2.25
    assert written.voltage_power_curve == profile.voltage_power_curve


def test_a_pass_that_has_not_measured_every_step_writes_nothing(calibrated):
    state, _train, profile, _entry, run = calibrated
    run.samples = run.samples[:3]
    run.phase = "leg"

    with pytest.raises(ConflictError):
        train_service.calibrate_commit(state, "T1", target="replace")
    assert profile.calibrated_at is None


def test_committing_closes_the_run(calibrated):
    state, _train, _profile, _entry, _run = calibrated
    train_service.calibrate_commit(state, "T1", target="replace")
    assert train_service.calibrate_status(state, "T1") is None


def test_cancelling_drops_the_pass_and_leaves_the_profile_alone(calibrated):
    state, _train, profile, _entry, _run = calibrated
    train_service.calibrate_cancel(state, "T1")
    assert train_service.calibrate_status(state, "T1") is None
    assert profile.calibrated_at is None
