"""Models, sets and the id counters survive a restart in the locomotive file.

design_new.md "SpeedProfile", Where models, sets and instance profiles are
kept. The file holds every `LocomotiveModel`, every set and both counters; a
train's instance profile is never written to it. The server reads it once at
startup, before the scene is built, and rewrites it after a request that
changed the library.
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


MEASURED_CURVE = [(3.6, 20.0), (8.0, 70.0), (12.0, 90.0)]


def _network_with_a_set():
    network = RailwayNetwork()
    registry = network.speed_profiles
    model = registry.add_model("H100")
    instance = registry.create_instance(max_speed_kmh=90.0)
    instance.voltage_speed_curve = list(MEASURED_CURVE)
    instance.calibrated_at = "2026-11-08T03:37:42+00:00"
    the_set = registry.create_set(model.model_id, "1", instance)
    return network, model, the_set, instance


def test_a_written_file_restores_models_sets_and_counters(tmp_path):
    path = tmp_path / "locomotives.json"
    locomotive_file.enable(str(path))
    network, model, the_set, instance = _network_with_a_set()
    assert locomotive_file.save_if_changed(network)

    data = json.loads(path.read_text())
    assert [m["name"] for m in data["models"]] == ["H100"]
    assert [s["profile_id"] for s in data["sets"]] == [the_set.profile_id]
    # The instance profile lives with its train, never in the file.
    assert instance.profile_id not in [s["profile_id"] for s in data["sets"]]

    fresh = RailwayNetwork()
    assert locomotive_file.load_into(fresh)
    restored = fresh.speed_profiles.get(the_set.profile_id)
    assert restored.is_set and restored.set_name == "1"
    assert fresh.speed_profiles.get_model(model.model_id).name == "H100"
    assert [tuple(p) for p in restored.voltage_speed_curve] == MEASURED_CURVE
    # The counters come back, so a number given out before is not reused.
    assert fresh.speed_profiles.next_profile_id == network.speed_profiles.next_profile_id
    assert fresh.speed_profiles.allocate_profile_id() > instance.profile_id


def test_an_unchanged_network_writes_nothing(tmp_path):
    path = tmp_path / "locomotives.json"
    locomotive_file.enable(str(path))
    network, *_ = _network_with_a_set()
    assert locomotive_file.save_if_changed(network)
    assert not locomotive_file.save_if_changed(network)


def test_an_instance_profile_change_writes_nothing(tmp_path):
    path = tmp_path / "locomotives.json"
    locomotive_file.enable(str(path))
    network, _model, _set, instance = _network_with_a_set()
    assert locomotive_file.save_if_changed(network)
    instance.lighting_voltage = 1.2
    assert not locomotive_file.save_if_changed(network)


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
    assert [e["kind"] for e in recorder.events] == ["warning"]
    assert recorder.events[0]["entities"] == []
    assert recorder.events[0]["payload"]["file"] == "locomotives.json"
    assert recorder.events[0]["payload"]["renamed_to"] == "locomotives.json.unreadable"


def _state_for(network, sim, dispatcher, tmp_path):
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path)),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def test_a_model_rename_over_http_survives_a_restart(tmp_path):
    path = tmp_path / "locomotives.json"
    network, dispatcher, sim, _ = bootstrap_runtime(
        demo=False, locomotive_file_path=str(path)
    )
    model = network.speed_profiles.add_model("H100")
    app = create_app(_state_for(network, sim, dispatcher, tmp_path))
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.put(
            f"/api/locomotive-models/{model.model_id}", json={"name": "H100 Hybrid"}
        )
    assert response.status_code == 200, response.get_json()
    assert json.loads(path.read_text())["models"][0]["name"] == "H100 Hybrid"

    locomotive_file.disable()
    restarted, _, restarted_sim, _ = bootstrap_runtime(
        demo=False, locomotive_file_path=str(path)
    )
    assert restarted.speed_profiles.get_model(model.model_id).name == "H100 Hybrid"
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
