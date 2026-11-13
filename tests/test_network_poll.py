"""`GET /api/network` sends the layout only when it changed.

design_new.md "Data Flow To UI", **Layout version**: the reply names the layout
with `layout_version`, a hash of the layout part. When the request carries the
current version, the reply leaves the layout out and carries only the live
fields; any other request gets the full snapshot.
"""

import contextlib
import copy
import io
import threading
from datetime import datetime

import pytest

import bootstrap
from app_factory import create_app
from app_state import AppState
from network_poll import (
    BLOCK_LAYOUT_FIELDS,
    LAYOUT_SECTIONS,
    build_network_poll_reply,
    layout_version,
    split_network_payload,
)
from persistence import PersistenceManager
from simulation import SimulationController, build_network_payload

# The live train map every reply carries (design_v2.md "Live fields").
SIBLING_GROUPS = ("running_trains",)


@pytest.fixture
def client(tmp_path):
    with contextlib.redirect_stdout(io.StringIO()):
        network = bootstrap.create_demo_network("demo7_2")
    state = AppState(
        network=network,
        sim_controller=SimulationController(network),
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path)),
        simulation_lock=threading.Lock(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.state = state
        yield c


def test_request_without_version_gets_full_snapshot(client):
    body = client.get("/api/network").get_json()
    assert body["layout_unchanged"] is False
    assert len(body["layout_version"]) == 32
    assert body["tracks"] and body["blocks"]
    for key in LAYOUT_SECTIONS:
        assert key in body
    assert "live_blocks" not in body


def test_request_with_current_version_gets_live_fields_only(client):
    full = client.get("/api/network").get_json()
    body = client.get(
        f"/api/network?layout_version={full['layout_version']}"
    ).get_json()

    assert body["layout_unchanged"] is True
    assert body["layout_version"] == full["layout_version"]
    for key in (*LAYOUT_SECTIONS, "tracks", "blocks", "switches"):
        assert key not in body
    for key in SIBLING_GROUPS:
        assert key in body
    assert body["session_state"] == full["session_state"]

    assert set(body["live_blocks"]) == set(full["blocks"])
    for live in body["live_blocks"].values():
        assert not BLOCK_LAYOUT_FIELDS & set(live)
        assert "reads_occupied" in live and "train_id" in live
    assert set(body["live_switches"]) == set(full["switches"])

    live_tiles = {(t["x"], t["y"]) for t in body["live_tracks"]}
    expected = {
        (t["x"], t["y"])
        for t in full["tracks"]
        if t["is_switch"]
    }
    assert live_tiles == expected
    assert expected, "demo7_2 has switches"


def test_request_with_other_version_gets_full_snapshot(client):
    body = client.get("/api/network?layout_version=stale").get_json()
    assert body["layout_unchanged"] is False
    assert body["tracks"]


def test_layout_edit_changes_version(client):
    before = client.get("/api/network").get_json()["layout_version"]
    block = next(iter(client.state.network.blocks.values()))
    block.speed_limit_kmh = (block.speed_limit_kmh or 0) + 7
    body = client.get(f"/api/network?layout_version={before}").get_json()
    assert body["layout_unchanged"] is False
    assert body["layout_version"] != before


def _payload():
    with contextlib.redirect_stdout(io.StringIO()):
        network = bootstrap.create_demo_network("demo7_2")
    return build_network_payload(
        network, SimulationController(network)
    )


def test_live_fields_do_not_change_version():
    payload = _payload()
    version = layout_version(split_network_payload(payload)[0])

    changed = copy.deepcopy(payload)
    block = next(iter(changed["blocks"].values()))
    block.update(reads_occupied=True, train_id="T9", current_voltage=6.0)
    switch = next(iter(changed["switches"].values()))
    switch.update(position=1, held=True)
    tile = next(t for t in changed["tracks"] if t["is_switch"])
    tile["switch_state"] = 1

    assert layout_version(split_network_payload(changed)[0]) == version
    reply = build_network_poll_reply(changed, version)
    assert reply["layout_unchanged"] is True
    assert reply["live_blocks"][block["block_id"]]["train_id"] == "T9"


def test_layout_field_changes_version():
    payload = _payload()
    version = layout_version(split_network_payload(payload)[0])
    changed = copy.deepcopy(payload)
    next(iter(changed["blocks"].values()))["ordered_tiles"].append([99, 99, "E"])
    assert layout_version(split_network_payload(changed)[0]) != version
