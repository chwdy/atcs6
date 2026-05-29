"""Tick-by-tick journey test for tunnel traversal.

Drives a train through the tunnel between BLOCK_WEST and BLOCK_EAST, in
a scene that has no shared state with the demo network. Runs at three
playback speeds so a regression in the tunnel-aware neighbor helpers,
the dead-end recompute, or train physics surfaces at all of them.
"""

import pytest

from tests.journey.harness import JourneyHarness
from tests.journey.journeys.tunnel_journey import build_tunnel_journey
from tests.journey.scenes.tunnel_scene import TunnelScene


@pytest.mark.parametrize(
    "dt,label",
    [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")],
)
def test_tunnel_train_full_journey(dt, label):
    base_max_ticks_at_0_1 = 2000
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    harness = JourneyHarness(
        scene=TunnelScene(),
        journey=build_tunnel_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
