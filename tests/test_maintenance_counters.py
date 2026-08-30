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


@pytest.mark.scene("demo_main")
def test_occupied_seconds_tracks_wall_time_not_scaled_delta():
    """design_new.md "Maintenance counters": `Block.occupied_seconds` measures
    real elapsed wall time regardless of playback speed — never the
    playback-scaled `delta_seconds` the physics and route bookkeeping consume. A
    running pass at a non-1.0 playback scale hands `update()` a scaled delta that
    differs from the wall interval, and the counter must follow the wall
    interval, not the delta."""
    from bootstrap import bootstrap_runtime

    with contextlib.redirect_stdout(io.StringIO()):
        network, _, sim_controller, _ = bootstrap_runtime(
            demo=True, setup_train=True
        )

    # A placed train occupies its starting block from the first pass, so a short
    # warm-up is enough to get real occupancy committed (no motion required).
    for _ in range(20):
        sim_controller.update(0.3)
    occupied_before = [
        b for b in network.blocks.values() if b.occupied_by is not None
    ]
    assert occupied_before, "the demo train should occupy at least one block"

    before = sum(b.occupied_seconds for b in network.blocks.values())

    # Model slow playback: two full wall seconds elapsed while this tick advanced
    # only a tiny slice of scaled sim time. Plant the wall interval on the
    # previous-pass stamp, then run exactly one running pass with the small
    # scaled delta.
    wall_interval = 2.0
    scaled_delta = 0.1
    sim_controller._last_pass_wall_time -= wall_interval
    sim_controller.update(scaled_delta)

    accrued = sum(b.occupied_seconds for b in network.blocks.values()) - before

    # Had the running pass used the scaled delta (the bug), the whole layout
    # would add at most (blocks occupied during the pass) x scaled_delta. Allow
    # a couple of blocks of slack for anything newly entered; the wall-based
    # accrual clears that ceiling by a wide margin because it tracks the ~2.0s
    # wall interval, not the 0.1s delta.
    delta_based_ceiling = (len(occupied_before) + 2) * scaled_delta
    assert accrued > delta_based_ceiling, (
        f"occupied_seconds accrued {accrued:.3f}s; a scaled-delta accrual would "
        f"be at most {delta_based_ceiling:.3f}s, so the pass is not tracking wall "
        f"time"
    )
    assert accrued >= wall_interval - 0.3, (
        f"occupied_seconds accrued {accrued:.3f}s; expected at least ~"
        f"{wall_interval}s of wall time"
    )
