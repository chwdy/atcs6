"""Speed profiles and the train roster survive a restart in the locomotive file.

design_new.md "SpeedProfile", Where profiles and the roster are kept. The bug
this guards: profiles lived only inside a layout snapshot, the server boots the
baked scene (which holds none of the operator's profiles) and never reads the
autosave back, so a measured curve vanished on every restart.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

import locomotive_file
from app_factory import create_app
from app_state import AppState
from bootstrap import bootstrap_runtime
from network import RailwayNetwork
from persistence import PersistenceManager
from speed_profile import SpeedProfile
from train_roster import RosterEntry


MEASURED_CURVE = [(3.6, 20.0), (8.0, 70.0), (12.0, 90.0)]


def _measured(profile_id="loco_A", model="Kato EF65"):
    return SpeedProfile(
        profile_id=profile_id,
        name="Measured",
        model=model,
        voltage_speed_curve=list(MEASURED_CURVE),
        calibrated_at="2026-11-07T10:00:00",
    )


def _network_with_measured_profile_and_roster():
    network = RailwayNetwork()
    network.speed_profiles.register(_measured())
    network.train_roster.add_entry(
        RosterEntry(entry_id="E1", name="EF65", model="Kato EF65",
                    speed_profile_id="loco_A")
    )
    return network


def test_a_written_file_restores_profiles_and_roster_into_a_fresh_network(tmp_path):
    path = tmp_path / "locomotives.json"
    locomotive_file.enable(str(path))
    assert locomotive_file.save_if_changed(_network_with_measured_profile_and_roster())

    fresh = RailwayNetwork()
    assert locomotive_file.load_into(fresh)

    restored = fresh.speed_profiles.get("loco_A")
    assert restored is not None
    assert restored.calibrated_at == "2026-11-07T10:00:00"
    assert [tuple(p) for p in restored.voltage_speed_curve] == MEASURED_CURVE
    assert fresh.train_roster.get_entry("E1").speed_profile_id == "loco_A"


def test_an_unchanged_network_writes_nothing(tmp_path):
    locomotive_file.enable(str(tmp_path / "locomotives.json"))
    network = _network_with_measured_profile_and_roster()
    assert locomotive_file.save_if_changed(network) is True
    assert locomotive_file.save_if_changed(network) is False
    network.train_roster.add_entry(RosterEntry(entry_id="E2"))
    assert locomotive_file.save_if_changed(network) is True


def test_a_file_record_replaces_the_scene_record_with_the_same_id(tmp_path):
    path = tmp_path / "locomotives.json"
    locomotive_file.enable(str(path))
    locomotive_file.save_if_changed(_network_with_measured_profile_and_roster())

    scene = RailwayNetwork()
    scene.speed_profiles.register(
        SpeedProfile(profile_id="loco_A", name="Seeded",
                     voltage_speed_curve=[(0.0, 0.0), (12.0, 100.0)])
    )
    scene.speed_profiles.register(SpeedProfile(profile_id="scene_only", name="S"))
    locomotive_file.load_into(scene)

    assert scene.speed_profiles.get("loco_A").calibrated_at == "2026-11-07T10:00:00"
    assert scene.speed_profiles.get("scene_only") is not None


def test_an_unreadable_file_is_renamed_and_reported(tmp_path):
    path = tmp_path / "locomotives.json"
    path.write_text("{not json")
    locomotive_file.enable(str(path))

    class _Recorder:
        def __init__(self):
            self.events = []

        def emit(self, **kwargs):
            self.events.append(kwargs)

    recorder = _Recorder()
    assert locomotive_file.load_into(RailwayNetwork(), recorder) is False
    assert not path.exists()
    assert (tmp_path / "locomotives.json.unreadable").read_text() == "{not json"
    assert [e["kind"] for e in recorder.events] == ["error"]


def test_a_layout_save_carries_neither_profiles_nor_roster():
    saved = _network_with_measured_profile_and_roster().to_layout_dict()
    assert "speed_profiles" not in saved
    assert "train_roster" not in saved


def test_loading_a_layout_and_clearing_keep_profiles_and_roster():
    network = _network_with_measured_profile_and_roster()
    network.from_dict(RailwayNetwork().to_layout_dict())
    assert network.speed_profiles.get("loco_A") is not None
    assert network.train_roster.get_entry("E1") is not None

    network.clear()
    assert network.speed_profiles.get("loco_A") is not None
    assert network.train_roster.get_entry("E1") is not None


def _train_dict(speed_profile_id):
    return {"id": "T9", "max_speed": 60.0, "speed_profile_id": speed_profile_id}


def test_a_loaded_train_whose_profile_is_not_stored_gets_the_seeded_pair():
    layout = RailwayNetwork().to_layout_dict()
    layout["trains"] = [_train_dict("gone__instance")]

    target = RailwayNetwork()
    target.from_dict(layout)

    loaded = target.trains["T9"]
    assert loaded.speed_profile_id == "default_T9__instance"
    instance = target.speed_profiles.get("default_T9__instance")
    assert instance is not None and instance.derived_from == "default_T9"
    assert target.speed_profiles.get("default_T9") is not None


def test_a_loaded_train_keeps_its_stored_profile():
    layout = RailwayNetwork().to_layout_dict()
    layout["trains"] = [_train_dict("loco_A")]

    target = _network_with_measured_profile_and_roster()
    target.from_dict(layout)
    assert target.trains["T9"].speed_profile_id == "loco_A"
    assert target.speed_profiles.get("default_T9") is None


def _state_for(network, sim_controller, dispatcher, tmp_path):
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def test_a_profile_created_over_http_survives_a_restart(tmp_path):
    path = tmp_path / "locomotives.json"

    network, dispatcher, sim, _ = bootstrap_runtime(
        demo=False, locomotive_file_path=str(path)
    )
    app = create_app(_state_for(network, sim, dispatcher, tmp_path))
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/speed-profiles", json=_measured("loco_B").to_dict()
        )
    assert response.status_code == 201, response.get_json()
    assert "loco_B" in json.loads(path.read_text())["speed_profiles"]

    # A restart: a new process boots a fresh network and reads the file.
    locomotive_file.disable()
    restarted, _, restarted_sim, _ = bootstrap_runtime(
        demo=False, locomotive_file_path=str(path)
    )
    assert restarted.speed_profiles.get("loco_B").calibrated_at == "2026-11-07T10:00:00"
    # The controller holds the same registry object, so it sees the loaded record.
    assert restarted_sim.speed_profile_registry is restarted.speed_profiles


def test_a_get_request_writes_nothing(tmp_path):
    path = tmp_path / "locomotives.json"
    network, dispatcher, sim, _ = bootstrap_runtime(
        demo=False, locomotive_file_path=str(path)
    )
    app = create_app(_state_for(network, sim, dispatcher, tmp_path))
    with app.test_client() as client:
        client.get("/api/speed-profiles")
    assert not path.exists()
