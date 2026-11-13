"""C3 — Block.is_holding_point: persistence + service write surface.

`is_holding_point` is a static, operator-set flag persisted with the layout
and never written by the runtime. Nothing in the runtime reads it.

These tests cover the persistence contract (serialization round-trip) and the
operator write surface (create / PATCH).
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from block import Block
from network import RailwayNetwork
from persistence import PersistenceManager
from simulation import SimulationController
from services import block_service
from tests.segment_test_helpers import make_segment_block


# --- persistence ----------------------------------------------------------


def test_is_holding_point_defaults_false():
    assert Block(block_id="BLK_X").is_holding_point is False


def test_to_dict_roundtrip():
    b = Block(block_id="BLK_HP", length_cm=10.0, is_holding_point=True)
    payload = b.to_dict()
    assert payload["is_holding_point"] is True
    assert Block.from_dict(payload).is_holding_point is True


def test_to_layout_dict_roundtrip():
    b = Block(block_id="BLK_HP", length_cm=10.0, is_holding_point=True)
    payload = b.to_layout_dict()
    assert payload["is_holding_point"] is True
    assert Block.from_dict(payload).is_holding_point is True


def test_from_dict_absent_defaults_false():
    block = Block.from_dict({"block_id": "BLK_Y", "length_cm": 10.0})
    assert block.is_holding_point is False


# --- service write surface ------------------------------------------------


@pytest.fixture
def state():
    network = RailwayNetwork()
    sim_controller = SimulationController(network)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    return AppState(
        network=network,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=threading.Lock(),
    )


def test_service_created_block_defaults_holding_point_false(state):
    # A fresh single-segment block starts with the flag off; it is set through
    # the block config PATCH surface, not at create time.
    block = make_segment_block(state.network, "BLK_HP")
    assert block.is_holding_point is False


def test_update_block_toggles_holding_point(state):
    make_segment_block(state.network, "BLK_HP")
    assert state.network.get_block("BLK_HP").is_holding_point is False
    block_service.update_block(state, "BLK_HP", data={"is_holding_point": True})
    assert state.network.get_block("BLK_HP").is_holding_point is True
    block_service.update_block(state, "BLK_HP", data={"is_holding_point": False})
    assert state.network.get_block("BLK_HP").is_holding_point is False
