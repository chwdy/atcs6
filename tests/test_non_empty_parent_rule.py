"""
Tests for the Non-empty parent rule (design_new.md, "Tile" ownership
list): every grouping entity in the ownership chain always owns at least one
child, and removing the last child deletes the now-empty parent in the same
operation, cascading upward.

    Station group  >= 1 Platform   (network.station_groups, station.py)
    Platform       >= 1 tile/block (network.platforms, railway_platform.py)
    Block          >= 1 tile       (network.blocks)

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_non_empty_parent_rule.py -v
"""

import threading
from datetime import datetime

import pytest

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from services import platform_service
from simulation import SimulationController
from track import Tile


def _network_with_ew_tracks(n: int = 4) -> RailwayNetwork:
    """A bare network with `n` straight EW tracks along y=0 at x=0..n-1."""
    net = RailwayNetwork()
    for x in range(n):
        net.add_track(Tile(x=x, y=0, track_bits=2))  # EW straight
    return net


@pytest.fixture
def net() -> RailwayNetwork:
    return _network_with_ew_tracks(4)


@pytest.fixture
def app_state() -> AppState:
    network = _network_with_ew_tracks(4)
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


# --------------------------------------------------------------------------- #
# Block >= 1 tile
# --------------------------------------------------------------------------- #


class TestBlockMinimumOneTile:
    def test_multi_tile_block_survives_until_last_tile(self, net):
        net.create_block_with_tracks(
            block_id="BLK", positions=[(0, 0), (1, 0)], name="B"
        )
        assert "BLK" in net.blocks

        net.remove_tile_from_block(0, 0)
        assert "BLK" in net.blocks  # still owns (1, 0)

        net.remove_tile_from_block(1, 0)
        assert "BLK" not in net.blocks  # last tile gone -> block auto-deleted

    def test_single_tile_block_deleted_on_tile_removal(self, net):
        net.create_block_with_tracks(block_id="BLK1", positions=[(0, 0)], name="B")
        assert "BLK1" in net.blocks

        net.remove_tile_from_block(0, 0)
        assert "BLK1" not in net.blocks


# --------------------------------------------------------------------------- #
# Platform >= 1 tile / block
# --------------------------------------------------------------------------- #


class TestPlatformMinimumOneTile:
    def test_platform_deleted_when_last_tile_removed(self, app_state):
        net = app_state.network
        net.add_platform(0, 0, platform_id="PLT", name="P")
        assert "PLT" in net.platforms
        assert "PLT_BLOCK" in net.blocks  # its station block

        result = platform_service.remove_tile(app_state, "PLT", x=0, y=0)

        assert result["status"] == "deleted"
        assert "PLT" not in net.platforms  # platform auto-deleted
        assert "PLT_BLOCK" not in net.blocks  # and its now-empty block too

    def test_multi_tile_platform_survives_until_last_tile(self, app_state):
        net = app_state.network
        net.add_platform(0, 0, platform_id="PLT", name="P")
        net.extend_platform("PLT", [(1, 0)])
        assert len(net.platforms["PLT"].positions) == 2

        first = platform_service.remove_tile(app_state, "PLT", x=1, y=0)
        assert first["status"] == "removed"
        assert "PLT" in net.platforms  # still owns (0, 0)

        last = platform_service.remove_tile(app_state, "PLT", x=0, y=0)
        assert last["status"] == "deleted"
        assert "PLT" not in net.platforms


# --------------------------------------------------------------------------- #
# Station group >= 1 platform
# --------------------------------------------------------------------------- #


class TestStationGroupMinimumOnePlatform:
    @staticmethod
    def _two_platforms(net):
        net.add_platform(0, 0, platform_id="PLT_A", name="A")
        net.add_platform(2, 0, platform_id="PLT_B", name="B")

    def test_group_deleted_when_last_platform_removed(self, net):
        self._two_platforms(net)
        net.create_station_group(
            station_id="STA", name="S", platform_ids=["PLT_A", "PLT_B"]
        )

        assert net.remove_platform_from_station_group("STA", "PLT_B") is True
        assert "STA" in net.station_groups  # still owns PLT_A
        assert net.station_groups["STA"].platform_ids == ["PLT_A"]

        assert net.remove_platform_from_station_group("STA", "PLT_A") is True
        assert "STA" not in net.station_groups  # last platform gone -> deleted

    def test_group_deleted_when_member_platform_deleted(self, net):
        net.add_platform(0, 0, platform_id="PLT_A", name="A")
        net.create_station_group(station_id="STA", name="S", platform_ids=["PLT_A"])

        net.delete_platform("PLT_A")  # cascades up through delete_station

        assert "STA" not in net.station_groups

    def test_group_survives_when_one_of_many_platforms_deleted(self, net):
        self._two_platforms(net)
        net.create_station_group(
            station_id="STA", name="S", platform_ids=["PLT_A", "PLT_B"]
        )

        net.delete_platform("PLT_A")

        assert "STA" in net.station_groups
        assert net.station_groups["STA"].platform_ids == ["PLT_B"]

    def test_create_rejects_empty_group(self, net):
        self._two_platforms(net)  # platforms exist, but we group none of them
        with pytest.raises(ValueError):
            net.create_station_group(station_id="STA", name="S", platform_ids=[])
        with pytest.raises(ValueError):
            net.create_station_group(station_id="STA2", name="S2", platform_ids=None)
        with pytest.raises(ValueError):
            net.create_station_group(station_id="STA3", name="S3")
        assert "STA" not in net.station_groups
        assert "STA2" not in net.station_groups
        assert "STA3" not in net.station_groups


# --------------------------------------------------------------------------- #
# Full upward cascade: tile -> platform -> station group
# --------------------------------------------------------------------------- #


class TestFullUpwardCascade:
    def test_last_tile_removal_cascades_to_platform_and_group(self, app_state):
        net = app_state.network
        net.add_platform(0, 0, platform_id="PLT_A", name="A")
        net.create_station_group(station_id="STA", name="S", platform_ids=["PLT_A"])
        assert "STA" in net.station_groups

        # Removing the platform's only tile deletes the platform (now blockless),
        # which removes it from the group, which leaves the group empty and so
        # deletes the group too — one operation, three entities pruned.
        result = platform_service.remove_tile(app_state, "PLT_A", x=0, y=0)

        assert result["status"] == "deleted"
        assert "PLT_A" not in net.platforms
        assert "PLT_A_BLOCK" not in net.blocks
        assert "STA" not in net.station_groups


# --------------------------------------------------------------------------- #
# HTTP route surface (routes/stations.py)
# --------------------------------------------------------------------------- #


class TestStationGroupHTTPEndpoints:
    def test_create_empty_group_rejected(self, client, app_state):
        app_state.network.add_platform(0, 0, platform_id="PLT_A", name="A")
        resp = client.post(
            "/api/station",
            json={"station_id": "STA", "name": "S", "platform_ids": []},
        )
        assert resp.status_code == 400
        assert "at least one platform" in resp.get_json()["error"]

    def test_remove_nonlast_platform_keeps_group(self, client, app_state):
        net = app_state.network
        net.add_platform(0, 0, platform_id="PLT_A", name="A")
        net.add_platform(2, 0, platform_id="PLT_B", name="B")
        net.create_station_group(
            station_id="STA", name="S", platform_ids=["PLT_A", "PLT_B"]
        )

        resp = client.post(
            "/api/station/STA/remove-platform", json={"platform_id": "PLT_B"}
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["station_deleted"] is False
        assert body["platform_ids"] == ["PLT_A"]
        assert "STA" in net.station_groups

    def test_remove_last_platform_deletes_group(self, client, app_state):
        net = app_state.network
        net.add_platform(0, 0, platform_id="PLT_A", name="A")
        net.create_station_group(station_id="STA", name="S", platform_ids=["PLT_A"])

        resp = client.post(
            "/api/station/STA/remove-platform", json={"platform_id": "PLT_A"}
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["station_deleted"] is True
        assert body["removed"] is True
        assert "STA" not in net.station_groups
        listing = client.get("/api/station").get_json()
        assert all(g["station_id"] != "STA" for g in listing["stations"])

    def test_patch_to_empty_rejected(self, client, app_state):
        net = app_state.network
        net.add_platform(0, 0, platform_id="PLT_A", name="A")
        net.create_station_group(station_id="STA", name="S", platform_ids=["PLT_A"])

        resp = client.patch("/api/station/STA", json={"platform_ids": []})

        assert resp.status_code == 400
        assert "STA" in net.station_groups  # rejected, group unchanged
        assert net.station_groups["STA"].platform_ids == ["PLT_A"]
