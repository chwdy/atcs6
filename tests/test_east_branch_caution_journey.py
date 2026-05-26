"""Pin the directional CAUTION behavior for BLOCK_EAST_BRANCH_2 when the
demo train enters east branch Station through the diagonal exit tile.

Regression for block_manager.py `_next_block_along_direction` exit-tile
loop: the loop must skip tiles whose track has no edge in the protect
direction (e.g., a TRACK_NS tile when direction="E"). Before the fix, a
straight tile in the same block could be mis-picked as the exit tile,
the walk would step into empty space, return None, and the reevaluator
would force-clear the caution — making the block jump OCCUPIED → FREE
directly while the forward block was still OCCUPIED.

Parametrized over three playback speeds (dt = 0.1 / 0.2 / 0.3).
"""

import pytest

from bootstrap import get_current_scene_id
from tests.journey.harness import JourneyHarness
from tests.journey.journeys.east_branch_caution_journey import (
    build_east_branch_caution_journey,
)
from tests.journey.scenes.east_branch_terminal_scene import (
    EastBranchTerminalScene,
)

if get_current_scene_id() != "demo_main":
    pytest.skip(
        f"Test built for scene 'demo_main'; active scene is "
        f"{get_current_scene_id()!r}.",
        allow_module_level=True,
    )


@pytest.mark.parametrize(
    "dt,label",
    [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")],
)
def test_east_branch_2_enters_caution_on_diagonal_entry(dt, label):
    # Two stops back-to-back, origin → East Terminal → east branch Station.
    # Budget ~1500 ticks at dt=0.1: train arrives east branch ~1377, plus
    # 30 settle ticks for the trigger to fire.
    max_ticks = max(50, int(1500 * 0.1 / dt))

    harness = JourneyHarness(
        scene=EastBranchTerminalScene(),
        journey=build_east_branch_caution_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
