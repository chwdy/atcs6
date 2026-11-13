"""`GET /api/network/stream` pushes the network snapshot to every open tab.

design_v2.md "Network stream": each message is the reply
`GET /api/network` would give, with the layout part only when it changed. The
server sends every `NETWORK_STREAM_INTERVAL_SECONDS` and right after a request
that can change state, and one build serves every open connection.
"""

import contextlib
import io
import json
import threading
import time
import urllib.request
from datetime import datetime

import pytest
from werkzeug.serving import make_server

import bootstrap
from app_factory import create_app
from app_state import AppState
from network_poll import LAYOUT_SECTIONS
from network_stream import NetworkStreamHub, stream_messages
from persistence import PersistenceManager
from simulation import SimulationController

# The live train map every reply carries (design_v2.md "Live fields").
SIBLING_GROUPS = ("running_trains",)


@pytest.fixture
def app(tmp_path):
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
    return app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def _parse(chunk) -> dict:
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert text.startswith("data: ") and text.endswith("\n\n"), text[:80]
    body = text[len("data: "):-2]
    assert "\n" not in body, "one message is one data line"
    return json.loads(body)


@contextlib.contextmanager
def _open_stream(client, query=""):
    response = client.get(f"/api/network/stream{query}", buffered=False)
    try:
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-cache"
        chunks = iter(response.response)
        yield lambda: _parse(next(chunks))
    finally:
        response.close()


# --- Message format through the route ---------------------------------------


def test_first_message_is_the_full_get_reply(client):
    reply = client.get("/api/network").get_json()
    with _open_stream(client) as read:
        first = read()

    assert first["layout_unchanged"] is False
    assert first["layout_version"] == reply["layout_version"]
    assert set(first) == set(reply)
    for key in (*LAYOUT_SECTIONS, "blocks", "switches", "tracks", *SIBLING_GROUPS):
        assert key in first
    assert first["tracks"] == reply["tracks"]
    assert "live_blocks" not in first


def test_later_messages_carry_live_fields_only(client):
    with _open_stream(client) as read:
        first = read()
        second = read()

    assert second["layout_unchanged"] is True
    assert second["layout_version"] == first["layout_version"]
    for key in (*LAYOUT_SECTIONS, "blocks", "switches", "tracks"):
        assert key not in second
    assert set(second["live_blocks"]) == set(first["blocks"])
    for key in SIBLING_GROUPS:
        assert key in second


def test_connection_naming_current_version_starts_live_only(client):
    version = client.get("/api/network").get_json()["layout_version"]
    with _open_stream(client, f"?layout_version={version}") as read:
        first = read()
    assert first["layout_unchanged"] is True
    assert first["layout_version"] == version
    assert "tracks" not in first


def test_connection_naming_other_version_starts_full(client):
    with _open_stream(client, "?layout_version=stale") as read:
        first = read()
    assert first["layout_unchanged"] is False
    assert first["tracks"]


def test_layout_edit_sends_the_full_snapshot_again(client):
    block_id = next(iter(client.application.extensions["state"].network.blocks))
    with _open_stream(client) as read:
        first = read()
        assert read()["layout_unchanged"] is True

        new_name = first["blocks"][block_id]["name"] + " renamed"
        patched = client.patch(f"/api/blocks/{block_id}", json={"name": new_name})
        assert patched.status_code == 200, patched.get_json()

        after = read()
        assert after["layout_unchanged"] is False
        assert after["layout_version"] != first["layout_version"]
        assert after["blocks"][block_id]["name"] == new_name

        # and then live fields only again, under the new version
        again = read()
        assert again["layout_unchanged"] is True
        assert again["layout_version"] == after["layout_version"]


def test_only_state_changing_requests_wake_the_stream(app, client):
    hub = app.extensions["network_stream"]
    before = hub._change_count
    client.get("/api/network")
    client.get("/api/health")
    assert hub._change_count == before
    client.post("/api/simulation/control", json={"action": "set_speed", "speed": 1.0})
    assert hub._change_count == before + 1


# --- The hub: when it builds and who shares a build -------------------------


def _counting_hub(interval_seconds, on_build=None):
    builds = []

    def build():
        builds.append(time.monotonic())
        if on_build:
            on_build(len(builds))
        return {"tracks": [], "blocks": {}, "switches": {}, "n": len(builds)}

    hub = NetworkStreamHub(build, json.dumps, interval_seconds=interval_seconds)
    return hub, builds


def test_one_build_serves_every_connection():
    hub, builds = _counting_hub(interval_seconds=10.0)
    tab_a = stream_messages(hub, None)
    tab_b = stream_messages(hub, None)

    assert _parse(next(tab_a))["n"] == 1
    assert _parse(next(tab_b))["n"] == 1
    assert len(builds) == 1

    hub.note_state_changed()
    assert _parse(next(tab_a))["n"] == 2
    assert _parse(next(tab_b))["n"] == 2
    assert len(builds) == 2


def test_messages_are_paced_by_the_interval():
    hub, builds = _counting_hub(interval_seconds=0.05)
    tab = stream_messages(hub, None)
    started = time.monotonic()
    for _ in range(3):
        next(tab)
    assert len(builds) == 3
    assert builds[2] - builds[1] >= 0.045
    assert time.monotonic() - started >= 0.09


def test_state_change_sends_without_waiting_for_the_interval():
    hub, builds = _counting_hub(interval_seconds=30.0)
    tab = stream_messages(hub, None)
    next(tab)

    threading.Timer(0.05, hub.note_state_changed).start()
    started = time.monotonic()
    assert _parse(next(tab))["n"] == 2
    assert time.monotonic() - started < 5.0


def test_request_finishing_during_a_build_makes_it_out_of_date():
    hub = None

    def during_first_build(count):
        if count == 1:
            hub.note_state_changed()

    hub, builds = _counting_hub(interval_seconds=30.0, on_build=during_first_build)
    tab = stream_messages(hub, None)
    next(tab)
    started = time.monotonic()
    assert _parse(next(tab))["n"] == 2
    assert time.monotonic() - started < 5.0


def test_a_failed_build_does_not_stall_the_next_one():
    calls = []

    def build():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"tracks": [], "blocks": {}, "switches": {}}

    hub = NetworkStreamHub(build, json.dumps, interval_seconds=30.0)
    with pytest.raises(RuntimeError):
        next(stream_messages(hub, None))
    assert _parse(next(stream_messages(hub, None)))["layout_unchanged"] is False


# --- On a real threaded server ----------------------------------------------


def test_two_tabs_stream_while_other_requests_are_served(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        tabs = [
            urllib.request.urlopen(f"{base}/api/network/stream", timeout=5)
            for _ in range(2)
        ]

        def read(tab):
            line = tab.readline().decode()
            assert tab.readline() == b"\n"
            return _parse(line + "\n")

        firsts = [read(tab) for tab in tabs]
        # Another request is answered while both streams are open.
        with urllib.request.urlopen(f"{base}/api/network", timeout=5) as reply:
            version = json.loads(reply.read())["layout_version"]

        for first in firsts:
            assert first["layout_unchanged"] is False
            assert first["layout_version"] == version
        for tab in tabs:
            assert read(tab)["layout_unchanged"] is True

        # One tab going away leaves the other streaming.
        tabs[0].close()
        assert read(tabs[1])["layout_version"] == version
        tabs[1].close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
