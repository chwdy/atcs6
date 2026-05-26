"""Tick-by-tick journey test for the demo train.

Drives DEMO_TRAIN through its full physical journey from (0,0) to East
Terminal. Asserts every observable event (head enters switch, tail clears
block, train stops, route completes) and a set of continuous invariants
(no overspeed, no EB, span ↔ resource consistency, valid (state, cab) pairs).

Runs at three playback speeds:
  - dt = 0.1 (1x baseline)
  - dt = 0.2 (2x)
  - dt = 0.3 (3x)
All three must pass identically — a regression in the demo or simulation
core that breaks any speed surfaces here with a structured failure report.
"""

import pytest

from bootstrap import get_current_scene_id
from tests.journey.harness import JourneyHarness
from tests.journey.journeys.demo_journey import build_demo_journey
from tests.journey.scenes.demo_scene import DemoScene

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
def test_demo_train_full_journey(dt, label):
    # Cover the same simulated time at every playback (~200 s of sim time).
    base_max_ticks_at_0_1 = 2000
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    # build_demo_journey() must be called per test — triggers carry closure
    # state and a shared Journey would leak that state across parametrize
    # variants.
    harness = JourneyHarness(
        scene=DemoScene(),
        journey=build_demo_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
