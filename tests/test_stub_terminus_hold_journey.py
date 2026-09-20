"""Journey test: routing a train toward an occupied dead-end terminus.

Confirms the current v1 behaviour (not a regression fix — a behaviour record):

  1. divert        — StationTarget, best platform occupied, other free:
                     B re-berths at the free platform.
  2. hold_both     — StationTarget, BOTH platforms occupied:
                     B holds before the throat switch (switch stays clear).
  3. hold_platform — PlatformTarget to the occupied platform (a free platform
                     exists but is not this route's target):
                     B holds before the throat switch — no re-berth, because a
                     PlatformTarget names one block and never consults the
                     station's free-platform preference.

The scene is self-contained (`StubTerminusScene` builds its own network), so no
`@pytest.mark.scene` gate is needed. Parametrized over three playback speeds.
"""

import pytest

from tests.journey.harness import JourneyHarness
from tests.journey.journeys.stub_terminus_journey import build_stub_terminus_journey
from tests.journey.scenes.stub_terminus_scene import StubTerminusScene


# (case_id, occupy_north, target, expected_outcome)
_CASES = [
    ("divert", False, "station", "divert"),
    ("hold_both", True, "station", "hold"),
    ("hold_platform", False, "platform_east", "hold"),
]


@pytest.mark.parametrize("case_id,occupy_north,target,expected", _CASES)
@pytest.mark.parametrize("dt,label", [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")])
def test_occupied_terminus_behaviour(case_id, occupy_north, target, expected, dt, label):
    # B travels ~4 tiles to the switch/platform; budget generously and scale
    # inversely with dt so faster playback still gets the same sim-time window.
    max_ticks = max(80, int(400 * 0.2 / dt))

    harness = JourneyHarness(
        scene=StubTerminusScene(occupy_north=occupy_north, target=target),
        journey=build_stub_terminus_journey(expected),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, f"[{case_id} @ {label}]\n" + result.report()
