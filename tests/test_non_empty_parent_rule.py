"""
Tests for the Non-empty parent rule (design_v2.md "Tile", Non-empty parent
rule): every grouping record always owns at least one child, and removing the
last child deletes the now-empty parent in the same operation.

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
from persistence import PersistenceManager
from services import track_service
from simulation import SimulationController
from track import Tile
from tests.segment_test_helpers import make_segment_block_with_tracks


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
    sim_controller = SimulationController(network)
    return AppState(
        network=network,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
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
        make_segment_block_with_tracks(net, "BLK", positions=[(0, 0), (1, 0)], name="B"
        )
        assert "BLK" in net.blocks

        net.remove_tile_from_segment(0, 0)
        assert "BLK" in net.blocks  # still owns (1, 0)

        net.remove_tile_from_segment(1, 0)
        assert "BLK" not in net.blocks  # last tile gone -> block auto-deleted

    def test_single_tile_block_deleted_on_tile_removal(self, net):
        make_segment_block_with_tracks(net, "BLK1", positions=[(0, 0)], name="B")
        assert "BLK1" in net.blocks

        net.remove_tile_from_segment(0, 0)
        assert "BLK1" not in net.blocks
