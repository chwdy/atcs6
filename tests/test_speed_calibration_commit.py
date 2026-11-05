"""Speed calibration's two commit targets, through the service layer.

design_new.md "Speed calibration", What commit writes. **Save to this
locomotive** writes the two measured fields onto the instance profile the
calibrated train names, so that one locomotive runs on the new curve and no
other changes. **Promote to model** writes them onto the model profile the
instance derives from and clears them from the instance, so every locomotive of
that model whose instance leaves them unset runs on the new curve. Commit writes
`voltage_speed_curve` and `calibrated_at` only — never `model`, and never
`voltage_power_curve`.
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from services import ConflictError, ValidationError
from services import train_service
from simulation import SimulationController
from speed_calibration import PHASE_MEASURED, CalibrationSample
from speed_profile import SpeedProfile, make_instance_profile
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
    """T1 on its own instance profile with a finished pass open; T2 a sibling
    instance of the same model. Both instances derive from one model profile."""
    model = SpeedProfile(profile_id="ef66", name="Kato EF66", model="Kato EF66")
    inst1 = make_instance_profile("ef66", profile_id="ef66_i1", name="EF66 #1")
    inst2 = make_instance_profile("ef66", profile_id="ef66_i2", name="EF66 #2")
    for profile in (model, inst1, inst2):
        state.network.speed_profiles.register(profile)

    entry = RosterEntry(entry_id="R1", model="Kato EF66", speed_profile_id="ef66_i1")
    state.network.train_roster.add_entry(entry)

    train = Train(train_id="T1")
    train.speed_profile_id = "ef66_i1"
    train.roster_entry_id = "R1"
    state.network.trains["T1"] = train

    other = Train(train_id="T2")
    other.speed_profile_id = "ef66_i2"
    state.network.trains["T2"] = other

    cal = state.sim_controller.speed_calibration
    run = cal.start(train, inst1, measured_block_id="B", outer_block_ids=("A", "C"))
    run.samples = [
        CalibrationSample(power_percent=p, speed_kmh=p * 0.8) for p in run.power_steps
    ]
    run.phase = PHASE_MEASURED
    return state, train, model, inst1, inst2, entry, run


def test_save_to_locomotive_writes_the_instance_and_leaves_siblings_alone(calibrated):
    state, train, model, inst1, inst2, entry, _run = calibrated
    written = train_service.calibrate_commit(state, "T1", target="locomotive")

    # The calibrated train's own instance carries the measured curve ...
    assert written is inst1
    assert inst1.calibrated_at is not None
    assert inst1.voltage_speed_curve[-1] == (12.0, 80.0)
    # ... while the model and the sibling instance are untouched.
    assert model.calibrated_at is None
    assert inst2.calibrated_at is None
    assert inst2.voltage_speed_curve is None
    # T2 still resolves to the model's default curve, not T1's measurement.
    resolved_t2 = state.network.speed_profiles.resolve("ef66_i2")
    assert resolved_t2.power_percent_to_speed(100.0) == 100.0
    # Links are unchanged: each entry/train keeps its own instance.
    assert train.speed_profile_id == "ef66_i1"
    assert entry.speed_profile_id == "ef66_i1"
    assert state.network.trains["T2"].speed_profile_id == "ef66_i2"


def test_promote_to_model_writes_the_model_and_clears_the_instance(calibrated):
    state, _train, model, inst1, _inst2, _entry, _run = calibrated
    written = train_service.calibrate_commit(state, "T1", target="model")

    # The model profile carries the measured curve ...
    assert written is model
    assert model.calibrated_at is not None
    assert model.voltage_speed_curve[-1] == (12.0, 80.0)
    # ... and the calibrated train's instance is cleared, so it falls back to
    # following the model.
    assert inst1.voltage_speed_curve is None
    assert inst1.calibrated_at is None
    # Every locomotive of the model now runs on the new curve — the sibling ...
    assert state.network.speed_profiles.resolve("ef66_i2").voltage_speed_curve[-1] == (
        12.0,
        80.0,
    )
    # ... and the calibrated train itself, through its cleared instance.
    assert state.network.speed_profiles.resolve("ef66_i1").voltage_speed_curve[-1] == (
        12.0,
        80.0,
    )


def test_commit_never_writes_the_model_field_on_the_instance(calibrated):
    # design_new.md "What commit writes": commit writes voltage_speed_curve and
    # calibrated_at only — `model` is edited on the model profile, never here.
    state, _train, _model, inst1, _inst2, _entry, _run = calibrated
    train_service.calibrate_commit(state, "T1", target="locomotive")
    assert inst1.model == ""  # left unset on the instance


def test_a_pass_that_has_not_measured_every_step_writes_nothing(calibrated):
    state, _train, _model, inst1, _inst2, _entry, run = calibrated
    run.samples = run.samples[:3]
    run.phase = "leg"

    with pytest.raises(ConflictError):
        train_service.calibrate_commit(state, "T1", target="locomotive")
    assert inst1.calibrated_at is None


def test_target_must_be_locomotive_or_model(calibrated):
    state, *_ = calibrated
    with pytest.raises(ValidationError):
        train_service.calibrate_commit(state, "T1", target="bogus")


def test_committing_closes_the_run(calibrated):
    state, *_ = calibrated
    train_service.calibrate_commit(state, "T1", target="locomotive")
    assert train_service.calibrate_status(state, "T1") is None


def test_cancelling_drops_the_pass_and_leaves_the_profiles_alone(calibrated):
    state, _train, model, inst1, _inst2, _entry, _run = calibrated
    train_service.calibrate_cancel(state, "T1")
    assert train_service.calibrate_status(state, "T1") is None
    assert inst1.calibrated_at is None
    assert model.calibrated_at is None
