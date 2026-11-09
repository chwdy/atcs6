"""Replay of the rig run of 2026-11-07: two locomotives standing on the layout at
activation, found as one unidentified train, then adopted one at a time.

The readings are the first 200 passes of that run's detector reading log
(`tests/replay/2026-11-07-two-locos-at-start.jsonl`, cut with
`tools/cut_reading_log.py`). The layout is the demo7_1 scene the rig runs. The
two adoptions are the requests the operator made, at the passes the event log
puts them: after pass 80 and after pass 129, with the head tile, facing and
length the event log recorded.

What the rig did, and what design_new.md "Unidentified trains" says must happen:

- BLK_4F4CF0 and BLK_4F6CD7 read current from the first pass. They touch, so
  they are one connected group and become ONE unidentified train (Creation and
  identity), confirmed on pass 2 (`on_threshold` 2).
- The first adoption declares a 10 cm locomotive standing on BLK_4F6CD7. Its
  body does not reach BLK_4F4CF0, which still reads current, so that block goes
  back to being unidentified, as a new unidentified train (What the consist does
  not reach goes back to being unidentified).
- The second adoption makes that one an ordinary train too.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

import pytest

from app_factory import create_app
from app_state import AppState
from bootstrap import create_demo_network
from enums import OperationMode
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from tools.reading_replay import ReadingPlayer, ReplayOccupancyInput, load_passes

RECORDING = Path(__file__).parent / "replay" / "2026-11-07-two-locos-at-start.jsonl"
WEST_BLOCK = "BLK_4F4CF0"  # the second locomotive's block
EAST_BLOCK = "BLK_4F6CD7"  # the first locomotive's block


@pytest.fixture
def rig(tmp_path):
    passes = load_passes(RECORDING)
    replay_input = ReplayOccupancyInput(passes)
    network = create_demo_network(scene="demo7_1")
    dispatcher = PathDispatcher(network)
    sim = SimulationController(
        network,
        dispatcher,
        operation_mode=OperationMode.REAL_HARDWARE,
        input_interface=replay_input,
    )
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path)),
        simulation_lock=threading.Lock(),
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield ReadingPlayer(sim, replay_input, passes), network, client


def held(network):
    """train id -> (is_unidentified, the blocks it holds)."""
    return {
        train_id: (
            train.is_unidentified,
            sorted(b for b, block in network.blocks.items() if block.occupied_by == train_id),
        )
        for train_id, train in network.trains.items()
    }


def unidentified_ids(network):
    return [train_id for train_id, train in network.trains.items() if train.is_unidentified]


def adopt(client, train_id, new_train_id, head_tile, direction):
    response = client.post(
        f"/api/trains/{train_id}/adopt",
        json={
            "new_train_id": new_train_id,
            "length_cm": 10,
            "max_speed": 100,
            "direction": direction,
            "head_tile": {"x": head_tile[0], "y": head_tile[1]},
        },
    )
    assert response.status_code == 200, response.get_json()


def test_the_recording_starts_with_both_blocks_reading_current(rig):
    player, _network, _client = rig
    first = player.passes[0]
    assert first.full == "start"
    assert first.present_blocks() == {WEST_BLOCK, EAST_BLOCK}


def test_two_touching_blocks_are_one_unidentified_train(rig):
    player, network, _client = rig

    player.play_to(1)
    assert held(network) == {}, "one read is not yet a confirmed entry"

    player.play_to(2)
    [train_id] = unidentified_ids(network)
    assert held(network) == {train_id: (True, [WEST_BLOCK, EAST_BLOCK])}

    player.play_to(80)
    assert held(network) == {train_id: (True, [WEST_BLOCK, EAST_BLOCK])}


def test_adopting_a_short_locomotive_hands_the_other_block_back(rig):
    player, network, client = rig
    player.play_to(80)
    [first] = unidentified_ids(network)

    adopt(client, first, "TRAIN_8948", head_tile=(7, 6), direction="SOUTH")

    trains = held(network)
    assert trains["TRAIN_8948"] == (False, [EAST_BLOCK])
    [second] = unidentified_ids(network)
    assert second != first, "an unidentified train_id is never handed out again"
    assert trains[second] == (True, [WEST_BLOCK])

    player.play_to(129)
    adopt(client, second, "TRAIN_7078", head_tile=(9, 13), direction="WEST")

    player.play_to(200)
    assert held(network) == {
        "TRAIN_8948": (False, [EAST_BLOCK]),
        "TRAIN_7078": (False, [WEST_BLOCK]),
    }
