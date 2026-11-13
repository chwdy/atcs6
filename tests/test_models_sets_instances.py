"""Models, sets and instance profiles.

design_new.md "SpeedProfile": Models, sets and instance profiles; Profile and
model ids are numbers; Where models, sets and instance profiles are kept; and
"Train Entity", Deleting a train. Every train owns exactly one instance
profile, a copy of the Default profile or a set; reset copies the parent back;
deleting a set or a model never breaks a train; the layout save carries each
train's instance profile, and a load gives it a fresh number.
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from services import ConflictError, NotFoundError, ValidationError
from services import speed_profile_service, train_service
from simulation import SimulationController
from speed_profile import DEFAULT_PROFILE_ID, SpeedProfileRegistry
from tests.segment_test_helpers import make_segment_block_with_tracks
from track import TRACK_EW, Tile


@pytest.fixture
def state(tmp_path):
    network = RailwayNetwork()
    for x in range(6):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(
        network, "B1", [(0, 0), (1, 0), (2, 0)], length_cm=150.0
    )
    make_segment_block_with_tracks(
        network, "B2", [(3, 0), (4, 0), (5, 0)], length_cm=150.0
    )
    dispatcher = PathDispatcher(network)
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=SimulationController(network, dispatcher),
        persistence_manager=None,
        simulation_lock=threading.Lock(),
    )


def _place(state, train_id="T1", block="B1", profile_id=None, max_speed=80.0):
    return state.sim_controller.add_train(
        train_id=train_id,
        starting_block_id=block,
        initial_direction="EAST",
        max_speed_kmh=max_speed,
        length_cm=40.0,
        speed_profile_id=profile_id,
    )


def _set(state, model_name="H100", set_name="1", **values):
    registry = state.network.speed_profiles
    model = next((m for m in registry.models() if m.name == model_name), None)
    model = model or registry.add_model(model_name)
    source = registry.get(DEFAULT_PROFILE_ID)
    the_set = registry.create_set(model.model_id, set_name, source)
    for key, value in values.items():
        setattr(the_set, key, value)
    return the_set


# -- numbering ---------------------------------------------------------------


def test_numbers_are_never_given_out_twice():
    registry = SpeedProfileRegistry()
    first = registry.create_instance()
    registry.remove(first.profile_id)
    second = registry.create_instance()
    assert first.profile_id == 1
    assert second.profile_id == 2
    model = registry.add_model("A")
    registry.remove_model(model.model_id)
    assert registry.add_model("B").model_id == model.model_id + 1


def test_the_default_profile_is_zero_and_cannot_be_removed():
    registry = SpeedProfileRegistry()
    assert registry.get(0).is_default
    assert registry.remove(0) is False


# -- giving a train a profile ------------------------------------------------


def test_placing_with_no_profile_copies_the_default_drawn_to_the_train(state):
    train = _place(state, max_speed=80.0)
    instance = state.network.speed_profiles.get(train.speed_profile_id)
    assert instance.is_instance
    assert instance.parent_profile_id == DEFAULT_PROFILE_ID
    assert instance.top_speed_kmh == 80.0


def test_placing_from_a_set_copies_it(state):
    the_set = _set(state, acceleration_rate=3.0)
    train = _place(state, profile_id=the_set.profile_id)
    instance = state.network.speed_profiles.get(train.speed_profile_id)
    assert instance.profile_id != the_set.profile_id
    assert instance.parent_profile_id == the_set.profile_id
    assert instance.acceleration_rate == 3.0
    # A copy, not a link: editing the set does not reach the train.
    the_set.acceleration_rate = 9.0
    assert instance.acceleration_rate == 3.0


def test_placing_on_an_instance_profile_or_no_profile_is_refused(state):
    first = _place(state)
    with pytest.raises(ValueError):
        _place(state, "T2", "B2", profile_id=first.speed_profile_id)
    with pytest.raises(ValueError):
        _place(state, "T2", "B2", profile_id=999)
    assert "T2" not in state.network.trains


def test_giving_a_train_a_set_copies_into_its_own_instance(state):
    train = _place(state)
    the_set = _set(state, acceleration_rate=3.0)
    instance_id = train.speed_profile_id
    train_service.assign_speed_profile(state, "T1", profile_id=the_set.profile_id)
    instance = state.network.speed_profiles.get(train.speed_profile_id)
    assert train.speed_profile_id == instance_id
    assert instance.parent_profile_id == the_set.profile_id
    assert instance.acceleration_rate == 3.0


def test_giving_a_train_an_instance_profile_is_refused(state):
    _place(state)
    other = _place(state, "T2", "B2")
    with pytest.raises(ValidationError):
        train_service.assign_speed_profile(state, "T1", profile_id=other.speed_profile_id)


def test_reset_copies_the_parent_back(state):
    the_set = _set(state, acceleration_rate=3.0)
    train = _place(state, profile_id=the_set.profile_id)
    instance = state.network.speed_profiles.get(train.speed_profile_id)
    instance.acceleration_rate = 7.0
    train_service.reset_speed_profile(state, "T1")
    assert state.network.speed_profiles.get(train.speed_profile_id).acceleration_rate == 3.0


def test_reset_is_refused_once_the_set_is_deleted(state):
    the_set = _set(state)
    train = _place(state, profile_id=the_set.profile_id)
    speed_profile_service.delete_profile(state, the_set.profile_id)
    # The train keeps its own copy unchanged.
    assert state.network.speed_profiles.get(train.speed_profile_id) is not None
    with pytest.raises(ConflictError):
        train_service.reset_speed_profile(state, "T1")


def test_a_profile_change_is_refused_while_a_pass_is_open(state):
    train = _place(state)
    the_set = _set(state)
    instance = state.network.speed_profiles.get(train.speed_profile_id)
    state.sim_controller.speed_calibration.start(
        train, instance, measured_block_id="B1", outer_block_ids=("B2",)
    )
    with pytest.raises(ConflictError):
        train_service.assign_speed_profile(state, "T1", profile_id=the_set.profile_id)
    with pytest.raises(ConflictError):
        train_service.reset_speed_profile(state, "T1")


# -- deleting ----------------------------------------------------------------


def test_deleting_a_train_removes_its_instance_profile(state):
    train = _place(state)
    pid = train.speed_profile_id
    state.sim_controller.remove_train("T1")
    assert state.network.speed_profiles.get(pid) is None


def test_deleting_a_model_removes_its_sets_only(state):
    a = _set(state, "H100", "1")
    b = _set(state, "H100", "2")
    other = _set(state, "Kato EF66", "1")
    removed = speed_profile_service.delete_model(state, a.model_id)
    registry = state.network.speed_profiles
    assert sorted(removed) == sorted([a.profile_id, b.profile_id])
    assert registry.get(other.profile_id) is not None
    assert registry.get_model(a.model_id) is None


def test_deleting_the_default_or_an_instance_is_refused(state):
    train = _place(state)
    with pytest.raises(ConflictError):
        speed_profile_service.delete_profile(state, DEFAULT_PROFILE_ID)
    with pytest.raises(ConflictError):
        speed_profile_service.delete_profile(state, train.speed_profile_id)
    with pytest.raises(NotFoundError):
        speed_profile_service.delete_profile(state, 999)


# -- hand edits --------------------------------------------------------------


def test_the_default_profile_cannot_be_edited(state):
    with pytest.raises(ConflictError):
        speed_profile_service.update_profile(state, 0, data={"acceleration_rate": 3.0})


def test_set_name_is_edited_only_on_a_set(state):
    the_set = _set(state)
    train = _place(state)
    updated = speed_profile_service.update_profile(
        state, the_set.profile_id, data={"set_name": " #2101 "}
    )
    assert updated.set_name == "#2101"
    with pytest.raises(ValidationError):
        speed_profile_service.update_profile(state, the_set.profile_id, data={"set_name": " "})
    with pytest.raises(ValidationError):
        speed_profile_service.update_profile(
            state, train.speed_profile_id, data={"set_name": "x"}
        )


def test_ids_are_not_edited_by_hand(state):
    train = _place(state)
    with pytest.raises(ValidationError):
        speed_profile_service.update_profile(
            state, train.speed_profile_id, data={"parent_profile_id": 99}
        )


def test_a_model_rename_moves_nothing(state):
    the_set = _set(state)
    speed_profile_service.rename_model(state, the_set.model_id, name="H100 Hybrid")
    assert state.network.speed_profiles.get(the_set.profile_id).model_id == the_set.model_id
    with pytest.raises(ValidationError):
        speed_profile_service.rename_model(state, the_set.model_id, name="  ")


# -- clearing, saving and loading --------------------------------------------


def test_clearing_the_network_drops_instances_and_keeps_sets(state):
    the_set = _set(state)
    train = _place(state, profile_id=the_set.profile_id)
    pid = train.speed_profile_id
    state.network.clear()
    assert state.network.speed_profiles.get(pid) is None
    assert state.network.speed_profiles.get(the_set.profile_id) is not None


def test_a_layout_round_trip_keeps_the_profile_under_a_fresh_number(state):
    the_set = _set(state)
    train = _place(state, profile_id=the_set.profile_id)
    instance = state.network.speed_profiles.get(train.speed_profile_id)
    instance.lighting_voltage = 0.8
    saved = state.network.to_layout_dict()
    old_id = train.speed_profile_id

    state.network.from_dict(saved)
    loaded = state.network.trains["T1"]
    record = state.network.speed_profiles.get(loaded.speed_profile_id)
    assert loaded.speed_profile_id != old_id
    assert record.lighting_voltage == 0.8
    # The parent is the same set here (same number, same names), so it stays.
    assert record.parent_profile_id == the_set.profile_id


def test_a_loaded_parent_with_other_names_is_cleared(state):
    the_set = _set(state)
    _place(state, profile_id=the_set.profile_id)
    saved = state.network.to_layout_dict()
    # Another machine: the same number names a different set.
    speed_profile_service.update_profile(state, the_set.profile_id, data={"set_name": "other"})

    state.network.from_dict(saved)
    record = state.network.speed_profiles.get(state.network.trains["T1"].speed_profile_id)
    assert record.parent_profile_id is None


def test_a_train_saved_without_a_profile_gets_a_default_copy(state):
    _place(state)
    saved = state.network.to_layout_dict()
    for train_data in saved["trains"]:
        train_data.pop("speed_profile", None)
        train_data.pop("speed_profile_parent", None)
    state.network.from_dict(saved)
    record = state.network.speed_profiles.get(state.network.trains["T1"].speed_profile_id)
    assert record.parent_profile_id == DEFAULT_PROFILE_ID


def test_the_layout_save_holds_no_library(state):
    _set(state)
    saved = state.network.to_layout_dict()
    assert "speed_profiles" not in saved
    assert "locomotive_models" not in saved
