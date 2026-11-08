"""An operator points a train only at an instance profile no other train names.

design_new.md "SpeedProfile", An operator points a train only at an instance
profile no other train names. The edit panel's
`PATCH /api/trains/<train_id>/speed-profile`, placing a train, and adopting an
unidentified train all refuse a model profile and a profile another train names.
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from app_factory import create_app
from app_state import AppState
from bootstrap import bootstrap_runtime
from persistence import PersistenceManager
from services import ValidationError, train_service
from speed_profile import make_instance_profile, seed_default_profile_pair
from train import Train


def _state(tmp_path):
    network, dispatcher, sim, _ = bootstrap_runtime(demo=False)
    for tid in ("T1", "T2"):
        train = Train.from_dict({"id": tid, "max_speed": 80})
        train.speed_profile_id = seed_default_profile_pair(network.speed_profiles, tid, 80)
        network.trains[tid] = train
    # A roster locomotive's instance that no placed train names yet.
    network.speed_profiles.register(
        make_instance_profile("default_T1", profile_id="free_instance", name="Free")
    )
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


@pytest.mark.parametrize(
    "profile_id, why",
    [
        ("default_T1", "model profile"),
        ("default", "built-in default is a model profile"),
        ("default_T2__instance", "another train's instance"),
    ],
)
def test_the_edit_panel_request_refuses_a_profile_the_train_may_not_take(tmp_path, profile_id, why):
    state = _state(tmp_path)
    client = create_app(state).test_client()

    response = client.patch("/api/trains/T1/speed-profile", json={"profile_id": profile_id})

    assert response.status_code == 400, why
    assert state.network.trains["T1"].speed_profile_id == "default_T1__instance"


def test_the_edit_panel_request_accepts_a_free_instance_and_the_trains_own(tmp_path):
    state = _state(tmp_path)
    client = create_app(state).test_client()

    own = client.patch("/api/trains/T1/speed-profile", json={"profile_id": "default_T1__instance"})
    assert own.status_code == 200

    free = client.patch("/api/trains/T1/speed-profile", json={"profile_id": "free_instance"})
    assert free.status_code == 200
    assert state.network.trains["T1"].speed_profile_id == "free_instance"


def test_placing_a_train_refuses_another_trains_profile_before_placing_it(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValidationError):
        train_service.create_train(
            state, train_id="T3", speed_profile_id="default_T2__instance"
        )
    assert "T3" not in state.network.trains


def test_placing_a_train_refuses_a_model_profile(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValidationError):
        train_service.create_train(state, train_id="T3", speed_profile_id="default_T1")
    assert "T3" not in state.network.trains


def test_placing_a_train_accepts_a_free_instance(tmp_path):
    state = _state(tmp_path)
    train = train_service.create_train(state, train_id="T3", speed_profile_id="free_instance")
    assert train.speed_profile_id == "free_instance"


def test_adoption_refuses_another_trains_profile(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValidationError, match="already used by train 'T2'"):
        train_service.adopt_unidentified_train(
            state,
            "T1",
            length_cm=100.0,
            max_speed_kmh=80.0,
            speed_profile_id="default_T2__instance",
        )
