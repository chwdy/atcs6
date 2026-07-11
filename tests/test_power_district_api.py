"""REST API tests for /api/power-districts (design_new.md "Power district").

The model is an id + name + member block ids; track voltage is runtime. These
tests exercise the route layer: create / list / get / patch / delete, the
add-block / remove-block membership edits, the one-district-per-block invariant
(a block moves out of its prior district when added to another; an emptied
district auto-deletes), and the connected-region rule — a district must be one
run of blocks directly joined to each other with no switch between them, the
electrical counterpart of a block's contiguous tiles.

Geometry: a straight EW line of connected tracks split into blocks A=(0,1),
B=(2,3), C=(4,5). A~B and B~C are directly adjacent; A is NOT adjacent to C
(B sits between them, standing in for a switch / non-member gap).

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
from track import Tile


def _network_with_line() -> RailwayNetwork:
    net = RailwayNetwork()
    for x in range(6):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight, all joined
    net.create_block_with_tracks("A", [(0, 0), (1, 0)], name="A")
    net.create_block_with_tracks("B", [(2, 0), (3, 0)], name="B")
    net.create_block_with_tracks("C", [(4, 0), (5, 0)], name="C")
    return net


@pytest.fixture
def network() -> RailwayNetwork:
    return _network_with_line()


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
        json={"district_id": "D1", "name": "Yard", "block_ids": ["A", "B"]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["district_id"] == "D1"
    assert data["name"] == "Yard"
    assert set(data["block_ids"]) == {"A", "B"}
    assert {b["block_id"] for b in data["blocks"]} == {"A", "B"}
    assert data["held_by"] is None
    assert data["voltage"] == 0.0


def test_create_generates_id_when_omitted(client):
    resp = client.post("/api/power-districts", json={"block_ids": ["A"]})
    assert resp.status_code == 200
    assert resp.get_json()["district_id"]


def test_create_rejects_empty_block_list(client):
    assert client.post("/api/power-districts", json={"block_ids": []}).status_code == 400


def test_create_rejects_unknown_block(client):
    assert client.post("/api/power-districts", json={"block_ids": ["NOPE"]}).status_code == 400


def test_create_rejects_duplicate_id(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    resp = client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["B"]})
    assert resp.status_code == 400


# --- connected-region rule -------------------------------------------------


def test_create_accepts_directly_adjacent_blocks(client):
    assert (
        client.post("/api/power-districts", json={"block_ids": ["A", "B", "C"]}).status_code
        == 200
    )


def test_create_rejects_disconnected_blocks(client):
    # A and C are not directly adjacent (B sits between them and is not a member).
    resp = client.post("/api/power-districts", json={"block_ids": ["A", "C"]})
    assert resp.status_code == 400
    assert "connected" in resp.get_json()["error"].lower()


def test_add_rejects_non_adjacent_block(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    resp = client.post("/api/power-districts/D1/add-block", json={"block_id": "C"})
    assert resp.status_code == 400
    assert "connected" in resp.get_json()["error"].lower()


def test_add_accepts_adjacent_block(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    resp = client.post("/api/power-districts/D1/add-block", json={"block_id": "B"})
    assert resp.status_code == 200
    assert set(resp.get_json()["block_ids"]) == {"A", "B"}


def test_add_blocks_bulk_validates_union_not_order(client):
    # Add {C, B} to {A} in one call: C is not adjacent to A, but the union
    # {A, B, C} is one connected run, so the order-independent union check
    # accepts it (a per-block check could reject C-before-B).
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    resp = client.post("/api/power-districts/D1/add-block", json={"block_ids": ["C", "B"]})
    assert resp.status_code == 200
    assert set(resp.get_json()["block_ids"]) == {"A", "B", "C"}


def test_add_blocks_bulk_rejects_disconnected_union(client):
    # Adding only C to {A} (no B) leaves a gap → rejected.
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    resp = client.post("/api/power-districts/D1/add-block", json={"block_ids": ["C"]})
    assert resp.status_code == 400


# --- list / get ------------------------------------------------------------


def test_list_and_get(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    listing = client.get("/api/power-districts").get_json()["power_districts"]
    assert [d["district_id"] for d in listing] == ["D1"]
    assert client.get("/api/power-districts/D1").status_code == 200
    assert client.get("/api/power-districts/MISSING").status_code == 404


# --- patch -----------------------------------------------------------------


def test_patch_renames(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    resp = client.patch("/api/power-districts/D1", json={"name": "Main"})
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Main"
    assert client.patch("/api/power-districts/MISSING", json={"name": "x"}).status_code == 404


# --- add / remove block + invariant ----------------------------------------


def test_add_block_moves_it_out_of_prior_district(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A", "B"]})
    client.post("/api/power-districts", json={"district_id": "D2", "block_ids": ["C"]})
    # B is in D1 and is adjacent to C (D2's member), so adding it to D2 moves it.
    resp = client.post("/api/power-districts/D2/add-block", json={"block_id": "B"})
    assert resp.status_code == 200
    assert set(resp.get_json()["block_ids"]) == {"C", "B"}
    assert set(network.power_districts["D1"].block_ids) == {"A"}


def test_add_unknown_block_404(client):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    assert (
        client.post("/api/power-districts/D1/add-block", json={"block_id": "NOPE"}).status_code
        == 404
    )


def test_remove_block(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A", "B"]})
    resp = client.post("/api/power-districts/D1/remove-block", json={"block_id": "B"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["removed"] is True
    assert body["district_deleted"] is False
    assert set(body["block_ids"]) == {"A"}
    assert network.district_block_ids("B") == ["B"]  # B is now its own feed


def test_remove_middle_block_rejected_when_it_would_split(client, network):
    # District A-B-C (a line). Removing the middle B would strand A from C.
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A", "B", "C"]})
    resp = client.post("/api/power-districts/D1/remove-block", json={"block_id": "B"})
    assert resp.status_code == 400
    assert "split" in resp.get_json()["error"].lower()
    # Nothing changed.
    assert set(network.power_districts["D1"].block_ids) == {"A", "B", "C"}


def test_remove_end_block_allowed(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A", "B", "C"]})
    resp = client.post("/api/power-districts/D1/remove-block", json={"block_id": "A"})
    assert resp.status_code == 200
    assert set(resp.get_json()["block_ids"]) == {"B", "C"}


def test_remove_last_block_deletes_district(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    resp = client.post("/api/power-districts/D1/remove-block", json={"block_id": "A"})
    assert resp.status_code == 200
    assert resp.get_json()["district_deleted"] is True
    assert "D1" not in network.power_districts


# --- delete ----------------------------------------------------------------


def test_delete(client, network):
    client.post("/api/power-districts", json={"district_id": "D1", "block_ids": ["A"]})
    assert client.delete("/api/power-districts/D1").status_code == 200
    assert "D1" not in network.power_districts
    assert client.delete("/api/power-districts/D1").status_code == 404
