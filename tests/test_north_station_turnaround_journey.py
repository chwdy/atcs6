"""Journey test: same-block route assigned while train is stopped at North Station.

Regression for route_executor.py `_covered_task_index_advance` (a5c6):
  * A stop task whose destination block equals the train's current block must
    be immediately satisfied when the train is stopped and has no active path.
  * Before the fix, `create_path(station, station)` returned NO_VIABLE_PATH
    (the BFS returns 0 candidates for same-block paths), so the route hung.

Parametrized over three playback speeds (dt = 0.1 / 0.2 / 0.3).
"""

import pytest

from bootstrap import get_current_scene_id
from tests.journey.harness import JourneyHarness
from tests.journey.journeys.north_station_turnaround_journey import (
    build_north_station_turnaround_journey,
)
from tests.journey.scenes.north_station_turnaround_scene import (
    NorthStationTurnaroundScene,
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
def test_same_block_route_assigned_at_north_station(dt, label):
    # The train travels origin → North Station (~250 s sim time), then the
    # turnaround route completes in one Phase 1 tick.  Budget 3500 ticks at
    # 0.1 s/tick (350 s sim time); scale proportionally for faster dt.
    base_max_ticks_at_0_1 = 3500
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    harness = JourneyHarness(
        scene=NorthStationTurnaroundScene(),
        journey=build_north_station_turnaround_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
