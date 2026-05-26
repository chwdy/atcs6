"""Pin the direction-aware CAUTION clear after a north-branch turnaround.

Regression for block_manager.py _reevaluate_caution_blocks() and its new
helper _forward_train_has_reversed(): when the OCCUPIED forward neighbor
of a CAUTION block holds a train whose current travel_direction is the
opposite of the recorded caution.exit_edge, the protection chain is stale
(train has flipped its cab and is no longer moving away in the protect
direction), so CAUTION must clear instead of being preserved.

Without the direction-aware clear, the end-of-turnaround end-state
showed BOTH BLOCK_BRANCH_1 (south of the station) and BLOCK_BRANCH_2
(north of the station) as CAUTION at once — visually wrong because the
"protection" on BRANCH_1 was no longer protecting anything (the train
in the station block had reversed cab and was now moving away from
BRANCH_1, not toward it).

Parametrized over three playback speeds (dt = 0.1 / 0.2 / 0.3).
"""

import pytest

from bootstrap import get_current_scene_id
from tests.journey.harness import JourneyHarness
from tests.journey.journeys.north_station_caution_turnaround_journey import (
    build_north_station_caution_turnaround_journey,
)
from tests.journey.scenes.north_station_caution_turnaround_scene import (
    NorthStationCautionTurnaroundScene,
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
def test_branch_1_caution_clears_after_north_station_turnaround(dt, label):
    # origin -> North Station -> BLOCK_BRANCH_2 -> back. No dwells, so the
    # whole sequence runs in ~850 ticks at dt=0.1; budget generously.
    base_max_ticks_at_0_1 = 1500
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    harness = JourneyHarness(
        scene=NorthStationCautionTurnaroundScene(),
        journey=build_north_station_caution_turnaround_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
