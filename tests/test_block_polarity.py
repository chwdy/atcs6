"""Block.polarity_reversed: persistence, the service write surface, and the
line it draws against the routing-policy field it sits next to.

design_new.md "Power -> Track polarity": a static, operator-set wiring flag
saying this block's two rails are wired the opposite way round from the
nominal-positive sense its geometry implies. The track driver reads it when
resolving the block's rail binding, so a positive commanded voltage drives the
other rail; nothing above the hardware interface sees it.

It is deliberately NOT `allowed_travel_directions`, which is a routing policy
the pathfinder reads and which never reaches hardware. The two live on the same
entity and one test below pins that they stay independent.

The flag is block-scoped because a block is fed as one unit: it covers every
member segment, and a segment has no polarity of its own.
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from block import Block
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from segment import Segment
from services import block_service
from services.exceptions import ValidationError
from simulation import SimulationController


# --- persistence ----------------------------------------------------------


def test_polarity_defaults_normal():
    assert Block(block_id="BLK_X").polarity_reversed is False


def test_to_dict_roundtrip():
    b = Block(block_id="BLK_P", length_cm=10.0, polarity_reversed=True)
    payload = b.to_dict()
    assert payload["polarity_reversed"] is True
    assert Block.from_dict(payload).polarity_reversed is True


def test_to_layout_dict_roundtrip():
    """Wiring config, so it belongs in the layout save (unlike runtime fields
    such as current_voltage, which to_layout_dict drops)."""
    b = Block(block_id="BLK_P", length_cm=10.0, polarity_reversed=True)
    payload = b.to_layout_dict()
    assert payload["polarity_reversed"] is True
    assert Block.from_dict(payload).polarity_reversed is True


def test_nested_segment_snapshot_carries_polarity():
    """The nested-`segments` from_dict branch is a separate constructor call
    from the inline-geometry one, so it needs its own coverage."""
    b = Block(
        block_id="BLK_P",
        polarity_reversed=True,
        segments=[Segment(segment_id="SEG_1", length_cm=10.0)],
    )
    payload = b.to_layout_dict()
    assert payload["segments"]                      # the nested branch, not inline
    assert Block.from_dict(payload).polarity_reversed is True


def test_legacy_snapshot_without_the_key_loads_normal():
    """A layout saved before this field existed reads as normally wired, which
    is the only safe default: it is what those layouts were running."""
    payload = Block(block_id="BLK_P", length_cm=10.0).to_layout_dict()
    del payload["polarity_reversed"]
    assert Block.from_dict(payload).polarity_reversed is False


# --- service write surface -------------------------------------------------


@pytest.fixture
def state():
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def test_service_created_block_defaults_normal(state):
    block = state.network.create_segment("BLK_P")
    assert block.polarity_reversed is False


def test_update_block_toggles_polarity(state):
    state.network.create_segment("BLK_P")
    block_service.update_block(state, "BLK_P", data={"polarity_reversed": True})
    assert state.network.get_block("BLK_P").polarity_reversed is True
    block_service.update_block(state, "BLK_P", data={"polarity_reversed": False})
    assert state.network.get_block("BLK_P").polarity_reversed is False


def test_polarity_is_not_a_geometry_field(state):
    """Geometry is rejected on the block PATCH and edited on the segment.
    Polarity is config, so it must go through here rather than 400."""
    state.network.create_segment("BLK_P")
    with pytest.raises(ValidationError):
        block_service.update_block(state, "BLK_P", data={"length_cm": 50.0})
    block_service.update_block(state, "BLK_P", data={"polarity_reversed": True})
    assert state.network.get_block("BLK_P").polarity_reversed is True


def test_polarity_has_no_occupancy_precondition(state):
    """Unlike `disabled`, flipping polarity under a held block is allowed: the
    driver reads it on the next write, so the worst case is that the train
    reverses — no state is corrupted, and refusing would strand an operator
    fixing wiring on exactly the block they are watching."""
    state.network.create_segment("BLK_P")
    state.network.get_block("BLK_P").occupied_by = "T1"
    block_service.update_block(state, "BLK_P", data={"polarity_reversed": True})
    assert state.network.get_block("BLK_P").polarity_reversed is True


def test_polarity_and_travel_direction_are_independent(state):
    """The two direction-ish fields on a block: one is wiring, one is routing
    policy. Writing either must leave the other alone."""
    state.network.create_segment("BLK_P")
    block_service.update_block(
        state, "BLK_P", data={"allowed_travel_directions": ["forward"]}
    )
    block_service.update_block(state, "BLK_P", data={"polarity_reversed": True})
    block = state.network.get_block("BLK_P")
    assert block.allowed_travel_directions == {"forward"}
    assert block.polarity_reversed is True

    block_service.update_block(
        state, "BLK_P", data={"allowed_travel_directions": ["forward", "reverse"]}
    )
    assert state.network.get_block("BLK_P").polarity_reversed is True
