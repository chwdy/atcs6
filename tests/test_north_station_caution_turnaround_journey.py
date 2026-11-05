"""Pin that a cab flip does NOT retire a CAUTION margin after a north-branch
turnaround: `block_manager.py._reevaluate_caution_blocks()` reads only occupancy
and connectivity, never the occupant's `travel_direction`.

Per design_new.md, a reservation never changes a block's caution: a margin clears
only through the ordinary tail-vacate cascade, on clean entry, or by the
completion-time exception — not on the cab flip itself, and not when a path
reserves the block the other way (that train is simply not capped). So at the end
of the turnaround BLOCK_BRANCH_1's northbound CAUTION is preserved, while
BLOCK_BRANCH_2 still holds the train's rear: the return stop completes in place,
because the train stops at the dead end with its head already in the station.

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
