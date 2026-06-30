"""REST API tests for /api/power-districts (design_new.md "Power district").

The model is an id + name + member block ids; track voltage is runtime. These
tests exercise the route layer: create / list / get / patch / delete plus the
add-block / remove-block membership edits and the one-district-per-block
invariant (a block moves out of its prior district when added to another, and a
district auto-deletes when its last block leaves — the non-empty parent rule the
station groups already follow).

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_power_district_api.py -v
"""

import threading
from datetime import datetime

import pytest

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


def _network_with_blocks() -> RailwayNetwork:
    net = RailwayNetwork()
    for bid in ("BLK_A", "BLK_B", "BLK_C"):
        net.create_block(bid, bid[-1])
    return net


@pytest.fixture
def network() -> RailwayNetwork:
    return _network_with_blocks()


@pytest.fixture
def app_state(network) -> AppState:
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


@pytest.fixture
def client(app_state):
    app = create_app(app_state)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


# --- create ----------------------------------------------------------------


def test_create_returns_enriched_district(client):
    resp = client.post(
        "/api/power-districts",
        json={"district_id": "D1", "name": "Yard", "block_ids": ["BLK_A", "BLK_B"]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["district_id"] == "D1"
    assert data["name"] == "Yard"
    assert set(data["block_ids"]) == {"BLK_A", "BLK_B"}
    assert {b["block_id"] for b in data["blocks"]} == {"BLK_A", "BLK_B"}
    # No train holds it, so the shared feed reads idle.
    assert data["held_by"] is None
    assert data["voltage"] == 0.0


def test_create_generates_id_when_omitted(client):
    resp = client.post("/api/power-districts", json={"block_ids": ["BLK_A"]})
    assert resp.status_code == 200
    assert resp.get_json()["district_id"]  # non-empty generated id


def test_create_rejects_empty_block_list(client):
    resp = client.post("/api/power-districts", json={"block_ids": []})
    assert resp.status_code == 400


def test_create_rejects_unknown_block(client):
    resp = client.post("/api/power-districts", json={"block_ids": ["NOPE"]})
    assert resp.status_code == 400


def test_create_rejects_duplicate_id(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A"]})
    resp = client.post(
        "/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_B"]}
    )
    assert resp.status_code == 400


# --- list / get ------------------------------------------------------------


def test_list_and_get(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A"]})
    listing = client.get("/api/power-districts").get_json()["power_districts"]
    assert [d["district_id"] for d in listing] == ["D1"]

    got = client.get("/api/power-districts/D1")
    assert got.status_code == 200
    assert got.get_json()["district_id"] == "D1"

    assert client.get("/api/power-districts/MISSING").status_code == 404


# --- patch -----------------------------------------------------------------


def test_patch_renames(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A"]})
    resp = client.patch("/api/power-districts/D1", json={"name": "Main"})
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Main"
    assert client.patch("/api/power-districts/MISSING", json={"name": "x"}).status_code == 404


# --- add / remove block + invariant ----------------------------------------


def test_add_block(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A"]})
    resp = client.post("/api/power-districts/D1/add-block", json={"block_id": "BLK_B"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["added"] is True
    assert set(body["block_ids"]) == {"BLK_A", "BLK_B"}


def test_add_block_moves_it_out_of_prior_district(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A", "BLK_B"]})
    client.post("/api/power-districts", json={"district_id": "D2", "block_ids": ["BLK_C"]})
    # BLK_B currently lives in D1; adding it to D2 must move it (one district
    # per block).
    resp = client.post("/api/power-districts/D2/add-block", json={"block_id": "BLK_B"})
    assert resp.status_code == 200
    assert set(resp.get_json()["block_ids"]) == {"BLK_C", "BLK_B"}
    assert set(network.power_districts["D1"].block_ids) == {"BLK_A"}


def test_add_unknown_block_404(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A"]})
    assert (
        client.post("/api/power-districts/D1/add-block", json={"block_id": "NOPE"}).status_code
        == 404
    )


def test_remove_block(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A", "BLK_B"]})
    resp = client.post("/api/power-districts/D1/remove-block", json={"block_id": "BLK_B"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["removed"] is True
    assert body["district_deleted"] is False
    assert set(body["block_ids"]) == {"BLK_A"}
    # BLK_B is now ungrouped: its own single-block district.
    assert network.district_block_ids("BLK_B") == ["BLK_B"]


def test_remove_last_block_deletes_district(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A"]})
    resp = client.post("/api/power-districts/D1/remove-block", json={"block_id": "BLK_A"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["district_deleted"] is True
    assert "D1" not in network.power_districts


# --- delete ----------------------------------------------------------------


def test_delete(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["BLK_A"]})
    assert client.delete("/api/power-districts/D1").status_code == 200
    assert "D1" not in network.power_districts
    assert client.delete("/api/power-districts/D1").status_code == 404
