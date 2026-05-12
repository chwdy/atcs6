"""Strict block-contract journey test for the demo train.

The default `test_demo_train_full_journey` test (in
`test_demo_train_journey.py`) skips the block ↔ span consistency invariants
because it was written to reflect *observed* simulation behavior rather than
the design contract.

This test asserts the design contract from `design_new.md` "Block" /
"BlockManager" sections instead. It currently FAILS (intentionally) because
the simulation core has two open divergences:

  1. `BLOCK_MAIN_WEST.occupied_by == DEMO_TRAIN` for the entire run — the
     initial block never receives a tail-vacate report.

  2. Sub-blocks of the original BLOCK_MAIN_EAST (e.g. BLOCK_MAIN_EAST_1
     covering tiles (4,0)/(5,0)/(6,0)) transition OCCUPIED → FREE *while*
     the train is still standing on one of their tiles (e.g. (5,0)).

Both stem from the same root cause: `TrainPosition.occupied_blocks` is
written once by `RailwayNetwork.place_train(...)` at boot and never
re-derived as the train moves. `TrainSpan.from_train_position(...)` blindly
forwards that stale list as `TrainSpan.occupied_blocks`, and
`TrainController.on_physics_updated(...)` uses it as `still_occupied_block_ids`
to gate tail-vacate emission:

    still_occupied_block_ids = set(new_span.occupied_blocks or [])
    ...
    if res_type == "block" and res_id in still_occupied_block_ids:
        continue

Because `still_occupied_block_ids` is permanently `{"BLOCK_MAIN_WEST"}`:
  - BLOCK_MAIN_WEST is wrongly suppressed (bug 1) — its tail-vacate event
    is filtered out every tick;
  - every other block is wrongly *not* suppressed (bug 2) — even when the
    train's single occupied tile is still inside the block, the controller
    emits tail-vacate the moment the previous tile leaves the span.

Fix sketch (not applied here): recompute `occupied_blocks` inside
`TrainSpan.from_train_position(...)` from the current `occupied_tiles` and
the network, or have `TrainPhysics._update_occupied_tiles(...)` rewrite
`train.position.occupied_blocks` on every move (the comment at
train_physics.py:1057 explicitly removed that mirror — restore it or move
the recompute into the span factory).
"""

import pytest

from tests.journey.harness import JourneyHarness
from tests.journey.journeys.demo_journey_strict import build_strict_demo_journey
from tests.journey.scenes.demo_scene import DemoScene


@pytest.mark.parametrize("dt,label", [(0.1, "1x")])
def test_demo_train_strict_block_contract(dt, label):
    """This test is expected to FAIL until the stale `occupied_blocks`
    issue is fixed; pytest output then carries the exact tick and block
    where the design contract first breaks.
    """
    base_max_ticks_at_0_1 = 2000
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    harness = JourneyHarness(
        scene=DemoScene(),
        journey=build_strict_demo_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()


def test_demo_block_main_east_1_not_released_while_train_inside():
    """Targeted failure mode for bug #2: BLOCK_MAIN_EAST_1 covers tiles
    (4,0)/(5,0)/(6,0). While the train is physically standing on any of
    those tiles, `Block.occupied_by` MUST equal `DEMO_TRAIN` per
    `design_new.md` "Block model".

    The current sim core emits a spurious tail-vacate for BLOCK_MAIN_EAST_1
    the tick after the train moves into (5,0) alone — because
    `TrainController.on_physics_updated()` filters tail-vacate using the
    stale `new_span.occupied_blocks` (which is permanently
    `['BLOCK_MAIN_WEST']`), and BLOCK_MAIN_EAST_1 is not in that stale list,
    so the suppression guard never trips.

    This test drives the demo scene tick-by-tick and asserts the contract
    *only at the failure moment* (no continuous invariants). The expected
    failure message points at the precise tick where BLOCK_MAIN_EAST_1
    transitions OCCUPIED → FREE while the train's single occupied tile is
    still inside the block.
    """
    import contextlib
    import io

    from bootstrap import bootstrap_runtime

    with contextlib.redirect_stdout(io.StringIO()):
        network, _path, sim_controller, _persistence = bootstrap_runtime(
            demo=True, setup_train=True
        )
    train_id = "DEMO_TRAIN"
    controller = sim_controller.get_or_create_controller(train_id)

    dt = 0.1
    saw_head_at_5_0 = False
    for tick in range(1, 2001):
        sim_controller.update(dt)
        span = controller.current_span
        if span is None or span.head is None:
            continue
        head_tile = tuple(span.head.tile)
        occupied_tiles = {tuple(t) for t in (span.occupied_tiles or [])}
        # Bug-2 window: every span tile that lies inside a block must have
        # block.occupied_by == DEMO_TRAIN. We isolate the (5,0) case because
        # it's the smallest reproduction; the broader invariant is covered
        # by `block_occupied_iff_span_covers_block`.
        if (5, 0) in occupied_tiles:
            saw_head_at_5_0 = True
            track = network.get_track(5, 0)
            block_id = track.block_section_id
            block = network.blocks.get(block_id)
            assert block is not None, f"missing block at (5,0)"
            assert block.occupied_by == train_id, (
                f"tick={tick}: train's span tile (5,0) lies inside block "
                f"{block_id} but block.occupied_by={block.occupied_by!r} "
                f"(expected {train_id!r}); state={block.state.value}, "
                f"head={head_tile}, span={sorted(occupied_tiles)}"
            )
        # Stop once train has been at terminal for a while.
        if tick > 200 and controller.current_speed_kmh <= 0.05:
            break

    assert saw_head_at_5_0, (
        "train never reached (5,0) — test setup or simulation regressed"
    )
