"""Journey test: turn-around at a no-platform dock.

Drives DEMO_TRAIN through a three-stop route that forces two reversals
at non-platform blocks (one east of the dock, one at the (0,0) dock
itself) before reaching North Station.

Pins the two design contracts that previously regressed:
  * design_new.md "`CAUTION` vs reservation independence" — CAUTION is only
    a speed cap; it must
    not block clearance into the cautioned block.
  * design_new.md "Canonical Next-Target Selection" — `task_direction ==
    "either"` for a BlockTarget
    must pick whichever block-relative direction is reachable from the
    train's current span (not the route walk direction).
"""

import pytest

from tests.journey.harness import JourneyHarness
from tests.journey.journeys.dock_turnaround_journey import (
    build_dock_turnaround_journey,
)
from tests.journey.scenes.dock_turnaround_scene import DockTurnaroundScene

pytestmark = pytest.mark.scene("demo_main")


@pytest.mark.parametrize(
    "dt,label",
    [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")],
)
def test_dock_turnaround_through_no_platform_block(dt, label):
    # Three legs (east, west to dock, north to station) plus two reversals
    # take longer than the single-stop demo; size the budget around
    # ~350 s of sim time at the 1x baseline.
    base_max_ticks_at_0_1 = 3500
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    harness = JourneyHarness(
        scene=DockTurnaroundScene(),
        journey=build_dock_turnaround_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
