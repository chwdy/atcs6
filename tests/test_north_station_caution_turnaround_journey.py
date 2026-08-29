"""Pin that a cab flip does NOT retire a CAUTION margin after a north-branch
turnaround: `block_manager.py._reevaluate_caution_blocks()` reads only occupancy
and connectivity, never the occupant's `travel_direction`.

Per design_new.md, a stale margin is retired only when the reversed train's new
path reserves the block the other way (the Opposing-traversal caution clear in
`reserve_blocks_for_path`) or the ordinary tail-vacate cascade clears it as the
train leaves the protected chain — not on the cab flip itself. So at the end of
the turnaround BLOCK_BRANCH_1's northbound CAUTION is preserved (the return route
never reserves it back), while BLOCK_BRANCH_2's southbound CAUTION is preserved
for the ordinary reason (an OCCUPIED forward block).

Parametrized over three playback speeds (dt = 0.1 / 0.2 / 0.3).
"""

import pytest

from tests.journey.harness import JourneyHarness
from tests.journey.journeys.north_station_caution_turnaround_journey import (
    build_north_station_caution_turnaround_journey,
)
from tests.journey.scenes.north_station_caution_turnaround_scene import (
    NorthStationCautionTurnaroundScene,
)

pytestmark = pytest.mark.scene("demo_main")


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
