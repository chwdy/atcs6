"""
Maintenance counter tests.

Covers the durable usage/wear counters added per design_new.md
"Maintenance counters":
  - Switch.flip_count        (incremented only on a real alignment change)
  - Block.occupied_seconds   (accrued per tick while a train occupies the block)
  - Train.total_distance_cm / Train.total_run_time_seconds

Verifies the flip guard, persistence round-trips (layout dict -> from_dict, and
the live frontend to_dict), and live accumulation across a real demo run.
"""

import contextlib
import io

import pytest

from block import Block
from switch import Switch
from train import Train


# ---------------------------------------------------------------------------
# Switch.flip_count
# ---------------------------------------------------------------------------


def test_flip_count_increments_only_on_real_change():
    sw = Switch(
        switch_id="SW1", x=0, y=0, main_length_cm=10.0, diverge_length_cm=10.0
    )
    assert sw.flip_count == 0

    sw.apply_position_change(1)  # MAIN(0) -> DIVERGE(1): a real flip
    sw.apply_position_change(1)  # DIVERGE -> DIVERGE: no-op, must not count
    sw.apply_position_change(0)  # DIVERGE -> MAIN: a real flip

    assert sw.flip_count == 2


def test_switch_flip_count_persists_and_is_exposed():
    sw = Switch(
        switch_id="SW1", x=0, y=0, main_length_cm=10.0, diverge_length_cm=10.0
    )
    sw.apply_position_change(1)
    sw.apply_position_change(0)
    assert sw.flip_count == 2

    # Persisted with the layout (to_layout_dict is the save path).
    restored = Switch.from_dict(sw.to_layout_dict())
    assert restored.flip_count == 2
    # Also surfaced in the live frontend payload.
    assert sw.to_dict()["flip_count"] == 2


# ---------------------------------------------------------------------------
# Block.occupied_seconds
# ---------------------------------------------------------------------------


def test_block_occupied_seconds_persists_and_is_exposed():
    b = Block(block_id="B1", length_cm=100.0)
    b.occupied_seconds = 5.5

    restored = Block.from_dict(b.to_layout_dict())
    assert restored.occupied_seconds == 5.5
    assert b.to_dict()["occupied_seconds"] == 5.5


# ---------------------------------------------------------------------------
# Train.total_distance_cm / total_run_time_seconds
# ---------------------------------------------------------------------------


def test_train_counters_persist_through_to_dict():
    t = Train(train_id="T1")
    t.total_distance_cm = 123.0
    t.total_run_time_seconds = 45.0

    restored = Train.from_dict(t.to_dict())
    assert restored.total_distance_cm == 123.0
    assert restored.total_run_time_seconds == 45.0


# ---------------------------------------------------------------------------
# Live accumulation across a real demo simulation run
# ---------------------------------------------------------------------------


@pytest.mark.scene("demo_main")
def test_counters_accumulate_during_demo_run():
    from bootstrap import bootstrap_runtime

    with contextlib.redirect_stdout(io.StringIO()):
        network, _, sim_controller, _ = bootstrap_runtime(
            demo=True, setup_train=True
        )

    train = network.trains["DEMO_TRAIN"]
    assert train.total_distance_cm == 0.0
    assert train.total_run_time_seconds == 0.0

    for _ in range(200):
        sim_controller.update(0.3)

    # The demo train runs under power and moves down the line.
    assert train.total_run_time_seconds > 0.0, "powered-on run time should accrue"
    assert train.total_distance_cm > 0.0, "distance travelled should accrue"

    # At least one block accrued occupancy time while the train sat on it.
    assert any(
        b.occupied_seconds > 0.0 for b in network.blocks.values()
    ), "some block should accrue occupied_seconds"
