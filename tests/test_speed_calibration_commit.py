"""Speed calibration's commit choices, through the service layer.

design_new.md "Speed calibration", What commit writes; "SpeedProfile",
Promoting an instance profile. **Save to this train** writes
`voltage_speed_curve` and `calibrated_at` onto the train's instance profile.
**Promote** does the same, then writes the whole instance profile to a set: as
a new set (under an existing model or a new one), or over the set the instance
was copied from. No other train changes, because every train runs on its own
copy.
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
from train import Train


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
    )


def _train_on(state, train_id, source_profile_id=None):
    registry = state.network.speed_profiles
    train = Train(train_id=train_id, max_speed=80.0)
    train.speed_profile_id = registry.create_instance(
        source_profile_id=source_profile_id, max_speed_kmh=80.0
    ).profile_id
    state.network.trains[train_id] = train
    return train


def _finish_pass(state, train):
    cal = state.sim_controller.speed_calibration
    instance = state.network.speed_profiles.get(train.speed_profile_id)
    run = cal.start(train, instance, measured_block_id="B", outer_block_ids=("A", "C"))
    run.samples = [
        CalibrationSample(track_voltage=v, speed_kmh=v / 12.0 * 80.0)
        for v in (3.0, 6.0, 9.0, 12.0)
    ]
    run.phase = PHASE_MEASURED
    return run


@pytest.fixture
def from_set(state):
    """T1 and T2 both copied from set "1" of model "H100"; T1 has a finished
    pass waiting."""
    registry = state.network.speed_profiles
    model = registry.add_model("H100")
    seed = registry.get(0)
    the_set = registry.create_set(model.model_id, "1", seed)
    t1 = _train_on(state, "T1", the_set.profile_id)
    t2 = _train_on(state, "T2", the_set.profile_id)
    _finish_pass(state, t1)
    return state, model, the_set, t1, t2


def test_save_to_this_train_writes_only_the_instance(from_set):
    state, _model, the_set, t1, t2 = from_set
    registry = state.network.speed_profiles
    set_curve = list(the_set.voltage_speed_curve)

    written = train_service.calibrate_commit(state, "T1", target="train")

    assert written.profile_id == t1.speed_profile_id
    assert written.calibrated_at is not None
    assert written.voltage_speed_curve[-1] == (12.0, 80.0)
    assert registry.get(t1.speed_profile_id).voltage_speed_curve[-1] == (12.0, 80.0)
    # The set and the other train's copy are untouched.
    assert registry.get(the_set.profile_id).voltage_speed_curve == set_curve
    assert registry.get(the_set.profile_id).calibrated_at is None
    assert registry.get(t2.speed_profile_id).calibrated_at is None
    assert written.parent_profile_id == the_set.profile_id


def test_promote_as_new_set_under_a_new_model(from_set):
    state, _model, the_set, t1, _t2 = from_set
    registry = state.network.speed_profiles

    written = train_service.calibrate_commit(
        state, "T1", target="promote", promote_as="new_set",
        model_name="Siemens Charger", set_name="#2101",
    )

    new_set = registry.get(written.parent_profile_id)
    assert new_set.is_set and new_set.set_name == "#2101"
    assert registry.get_model(new_set.model_id).name == "Siemens Charger"
    assert new_set.voltage_speed_curve == written.voltage_speed_curve
    assert new_set.calibrated_at == written.calibrated_at
    assert new_set.profile_id not in (the_set.profile_id, t1.speed_profile_id)


def test_promote_as_new_set_under_an_existing_model(from_set):
    state, model, _the_set, _t1, _t2 = from_set
    registry = state.network.speed_profiles

    written = train_service.calibrate_commit(
        state, "T1", target="promote", promote_as="new_set",
        model_id=model.model_id, set_name="2",
    )

    new_set = registry.get(written.parent_profile_id)
    assert new_set.model_id == model.model_id
    assert sorted(p.set_name for p in registry.sets(model.model_id)) == ["1", "2"]


def test_promote_over_the_current_set(from_set):
    state, _model, the_set, _t1, t2 = from_set
    registry = state.network.speed_profiles

    written = train_service.calibrate_commit(
        state, "T1", target="promote", promote_as="current_set"
    )

    stored = registry.get(the_set.profile_id)
    assert stored.voltage_speed_curve == written.voltage_speed_curve
    assert stored.calibrated_at == written.calibrated_at
    assert written.parent_profile_id == the_set.profile_id
    # T2 runs on its own copy, which the promote does not reach.
    assert registry.get(t2.speed_profile_id).calibrated_at is None


def test_promote_over_the_current_set_is_refused_for_the_default(state):
    train = _train_on(state, "T1")
    _finish_pass(state, train)
    with pytest.raises(ConflictError, match="Default"):
        train_service.calibrate_commit(state, "T1", target="promote", promote_as="current_set")
    # Refused before anything was written: the pass is still waiting.
    assert state.sim_controller.speed_calibration.runs.get("T1") is not None
    assert state.network.speed_profiles.get(train.speed_profile_id).calibrated_at is None


def test_promote_over_a_deleted_set_is_refused(from_set):
    state, _model, the_set, _t1, _t2 = from_set
    state.network.speed_profiles.remove(the_set.profile_id)
    with pytest.raises(ConflictError, match="deleted"):
        train_service.calibrate_commit(state, "T1", target="promote", promote_as="current_set")


def test_a_new_set_needs_a_name_and_a_model(from_set):
    state, *_ = from_set
    with pytest.raises(ValidationError):
        train_service.calibrate_commit(
            state, "T1", target="promote", promote_as="new_set", model_name="H100"
        )
    with pytest.raises(ValidationError):
        train_service.calibrate_commit(
            state, "T1", target="promote", promote_as="new_set", set_name="3"
        )


def test_a_commit_clears_the_ato_minimum_voltage(from_set):
    state, _model, _the_set, t1, _t2 = from_set
    registry = state.network.speed_profiles
    instance = registry.get(t1.speed_profile_id)
    instance.ato_min_voltage = 6.0

    written = train_service.calibrate_commit(
        state, "T1", target="promote", promote_as="current_set"
    )

    assert written.ato_min_voltage is None
    assert registry.get(written.parent_profile_id).ato_min_voltage is None


def test_an_unfinished_pass_writes_nothing(from_set):
    state, _model, _the_set, t1, _t2 = from_set
    run = state.sim_controller.speed_calibration.runs["T1"]
    run.phase = "leg"
    with pytest.raises(ConflictError):
        train_service.calibrate_commit(state, "T1", target="train")
    assert state.network.speed_profiles.get(t1.speed_profile_id).calibrated_at is None


def test_a_hand_edit_of_a_calibrating_trains_profile_is_refused(from_set):
    """design_new.md "SpeedProfile", A train's instance profile is not edited
    while calibration holds it: refused while T1's finished pass waits. A set
    and another train's instance profile stay editable."""
    from services import speed_profile_service

    state, _model, the_set, t1, t2 = from_set
    with pytest.raises(ConflictError):
        speed_profile_service.update_profile(
            state, t1.speed_profile_id, data={"lighting_voltage": 0.5}
        )
    speed_profile_service.update_profile(
        state, t2.speed_profile_id, data={"lighting_voltage": 0.5}
    )
    speed_profile_service.update_profile(
        state, the_set.profile_id, data={"lighting_voltage": 0.5}
    )
    state.sim_controller.speed_calibration.stop("T1", reason="discarded")
    edited = speed_profile_service.update_profile(
        state, t1.speed_profile_id, data={"lighting_voltage": 0.5}
    )
    assert edited.lighting_voltage == 0.5


def test_a_commit_clears_the_ato_minimum_voltage_even_when_the_curve_is_unchanged(from_set):
    """design_new.md "Speed calibration", A commit clears the train's ATO
    minimum voltage: on every commit, not only when the curve differs."""
    state, _model, _the_set, t1, _t2 = from_set
    registry = state.network.speed_profiles
    run = state.sim_controller.speed_calibration.runs["T1"]
    instance = registry.get(t1.speed_profile_id)
    instance.voltage_speed_curve = list(
        state.sim_controller.speed_calibration.measured_curve(run)
    )
    instance.ato_min_voltage = instance.voltage_speed_curve[1][0]
    written = train_service.calibrate_commit(state, "T1", target="train")
    assert written.ato_min_voltage is None
