"""
Tests for the generalized ``/api/train-routes/check-viability`` endpoint.

The endpoint accepts canonical route-task targets — a dict carrying exactly one
of ``block_id`` / ``platform_id`` / ``station_id``, or a bare platform-id string
— and resolves each to a block id via the runtime ``RouteTargetResolver`` before
computing block-graph hop distance. These tests cover all three target variants,
the bare-string back-compat path, and an unresolvable target.

Run with:
    conda run -n general python -m pytest tests/test_route_viability_targets.py -v
"""

from datetime import datetime
import threading

import pytest

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from persistence import PersistenceManager
from speed_profile import SpeedProfileRegistry
from track import TrackSegment


@pytest.fixture
def app_state():
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        speed_profile_registry=SpeedProfileRegistry(),
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


@pytest.fixture
def connected_network(app_state, client):
    """Two adjacent EW blocks, a platform in each, and a station group on PLT_A.

    Layout (y=0):  [BLK_A: (0,0)(1,0)] [BLK_B: (2,0)(3,0)]
                     PLT_A @ (0,0)        PLT_B @ (2,0)
    BLK_A and BLK_B are adjacent (the (1,0)-(2,0) edge), so they are one hop
    apart in the block graph.
    """
    net = app_state.network
    for x in range(4):
        net.add_track(TrackSegment(x=x, y=0, track_bits=2))  # EW

    net.create_block_with_tracks(
        block_id="BLK_A", positions=[(0, 0), (1, 0)], name="Block A"
    )
    net.create_block_with_tracks(
        block_id="BLK_B", positions=[(2, 0), (3, 0)], name="Block B"
    )

    assert client.post(
        "/api/platform",
        json={"x": 0, "y": 0, "platform_id": "PLT_A", "name": "Platform A"},
    ).status_code == 200
    assert client.post(
        "/api/platform",
        json={"x": 2, "y": 0, "platform_id": "PLT_B", "name": "Platform B"},
    ).status_code == 200

    net.create_station_group(
        station_id="STA_A", name="Station A", platform_ids=["PLT_A"]
    )
    return app_state


def _viability(client, pairs):
    resp = client.post(
        "/api/train-routes/check-viability", json={"pairs": pairs}
    )
    assert resp.status_code == 200
    return resp.get_json()["results"]


def test_platform_and_block_targets_both_resolve(client, connected_network):
    # A platform target paired with a block target resolves on both sides
    # (no "could not resolve" error) and yields a non-negative hop count.
    results = _viability(client, [[{"platform_id": "PLT_A"}, {"block_id": "BLK_B"}]])
    assert len(results) == 1
    assert results[0]["viable"] is True
    assert isinstance(results[0]["hop_count"], int)
    assert results[0]["hop_count"] >= 0


def test_station_resolves_to_same_block_as_member_platform(client, connected_network):
    # STA_A's only member platform is PLT_A, so a station target and a platform
    # target for that platform resolve to the SAME block -> zero hops apart.
    # (Topology-independent: proves StationTarget resolution.)
    results = _viability(client, [[{"station_id": "STA_A"}, {"platform_id": "PLT_A"}]])
    assert len(results) == 1
    assert results[0]["viable"] is True
    assert results[0]["hop_count"] == 0


def test_bare_string_resolves_identically_to_platform_dict(client, connected_network):
    # Back-compat: a bare string is treated as a platform id, so it resolves to
    # the same block as the explicit {"platform_id": ...} dict -> zero hops.
    results = _viability(client, [[{"platform_id": "PLT_A"}, "PLT_A"]])
    assert len(results) == 1
    assert results[0]["viable"] is True
    assert results[0]["hop_count"] == 0


def test_distinct_places_have_positive_hops(client, connected_network):
    # Two different platforms in different blocks are at least one hop apart.
    results = _viability(client, [[{"platform_id": "PLT_A"}, {"platform_id": "PLT_B"}]])
    assert len(results) == 1
    assert results[0]["viable"] is True
    assert results[0]["hop_count"] >= 1


def test_unresolvable_target_is_not_viable(client, connected_network):
    results = _viability(client, [[{"block_id": "NOPE"}, {"block_id": "BLK_B"}]])
    assert len(results) == 1
    assert results[0]["viable"] is False
    assert "resolve" in results[0]["error"].lower()


def test_route_tasks_create_save_reload_roundtrip(client, connected_network):
    """Exercise the editor's exact data path over HTTP.

    Regression guard for the original bug: the editor created routes with an
    empty body and then read/wrote a `waypoints` key the backend never spoke,
    so saved task content vanished and reloads always showed an empty route.
    The editor now POSTs/PUTs `tasks` and reads `route.tasks`.
    """
    # createNewRoute payload (editor sends an empty `tasks` list).
    create = client.post(
        "/api/train-routes",
        json={
            "route_id": "RT_TEST",
            "name": "RT",
            "mode": "one_off",
            "tasks": [],
            "assigned_train_ids": [],
            "is_active": False,
        },
    )
    assert create.status_code == 200

    # saveRoute payload: stop + dwell + waypoint, canonical task_location.
    tasks = [
        {
            "task_kind": "stop",
            "task_location": {"platform_id": "PLT_A"},
            "task_direction": "forward",
            "task_time": 60,
            "target_time": None,
        },
        {
            "task_kind": "dwell",
            "task_location": {"platform_id": "PLT_A"},
            "task_direction": "either",
            "task_time": 30,
            "target_time": None,
        },
        {
            "task_kind": "waypoint",
            "task_location": {"block_id": "BLK_B"},
            "task_direction": "either",
            "task_time": None,
            "target_time": None,
        },
    ]
    upd = client.put(
        "/api/train-routes/RT_TEST",
        json={"name": "RT", "mode": "one_off", "tasks": tasks},
    )
    assert upd.status_code == 200

    # The reload that used to come back empty must now echo the saved tasks.
    got = client.get("/api/train-routes/RT_TEST").get_json()
    assert len(got["tasks"]) == 3
    assert got["tasks"][0]["task_kind"] == "stop"
    assert got["tasks"][0]["task_location"] == {"platform_id": "PLT_A"}
    assert got["tasks"][0]["task_time"] == 60
    assert got["tasks"][2]["task_location"] == {"block_id": "BLK_B"}

    # activate -> is_active flips True (so Phase 1 will pick the route up).
    assert client.post("/api/train-routes/RT_TEST/activate").status_code == 200
    assert client.get("/api/train-routes/RT_TEST").get_json()["is_active"] is True


# --------------------------------------------------------------------------
# /api/train-routes/reachable-from  (feasible-tile highlighting source)
# --------------------------------------------------------------------------


def _reachable_from(client, target_type, target_id):
    resp = client.get(
        f"/api/train-routes/reachable-from?type={target_type}&id={target_id}"
    )
    return resp


def test_reachable_from_block_lists_adjacent_block(client, connected_network):
    resp = _reachable_from(client, "block", "BLK_A")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["block_id"] == "BLK_A"
    assert isinstance(body["reachable_endpoints"], list)
    assert body["count"] == len(body["reachable_endpoints"])
    # BLK_B is one hop away, so it must be reachable from BLK_A.
    assert "BLK_B" in body["reachable_endpoints"]


def test_reachable_from_platform_and_station_resolve_consistently(client, connected_network):
    # A platform target resolves to a block via the runtime resolver, and a
    # station target resolves through its only member platform (STA_A -> PLT_A),
    # so both must land on the SAME block. (Mirrors the check-viability resolver
    # contract without coupling to the platform-block naming scheme.)
    plat = _reachable_from(client, "platform", "PLT_A")
    sta = _reachable_from(client, "station", "STA_A")
    assert plat.status_code == 200 and sta.status_code == 200
    assert plat.get_json()["block_id"]  # resolved to some block
    assert plat.get_json()["block_id"] == sta.get_json()["block_id"]


def test_reachable_from_rejects_bad_type(client, connected_network):
    assert _reachable_from(client, "bogus", "BLK_A").status_code == 400


def test_reachable_from_unresolvable_id_is_404(client, connected_network):
    assert _reachable_from(client, "block", "NOPE").status_code == 404


# --------------------------------------------------------------------------
# /api/train-routes/<id>/history  (read-only run timeline)
# --------------------------------------------------------------------------


def test_history_shape_for_fresh_route(client, connected_network):
    client.post(
        "/api/train-routes",
        json={"route_id": "RT_HIST", "name": "H", "mode": "one_off", "tasks": []},
    )
    resp = client.get("/api/train-routes/RT_HIST/history")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) == {"route_id", "activated_at", "is_active", "runs"}
    # A route that has never run nor activated: no timestamp, no runs.
    assert body["activated_at"] is None
    assert body["is_active"] is False
    assert body["runs"] == []


def test_history_records_activation_time(client, connected_network):
    client.post(
        "/api/train-routes",
        json={
            "route_id": "RT_HIST2",
            "name": "H2",
            "mode": "one_off",
            "tasks": [
                {"task_kind": "waypoint", "task_location": {"block_id": "BLK_B"}}
            ],
        },
    )
    assert client.post("/api/train-routes/RT_HIST2/activate").status_code == 200
    body = client.get("/api/train-routes/RT_HIST2/history").get_json()
    assert body["is_active"] is True
    assert isinstance(body["activated_at"], (int, float))


def test_history_unknown_route_is_404(client, connected_network):
    assert client.get("/api/train-routes/NOPE/history").status_code == 404
