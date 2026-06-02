"""Regression: a blocked BEST_EFFORT extend must not release the live path's
seam reservation.

Bug (demo_5): on T1's first leg S1 -> S2, T2 still holds PLT_S2_S, so
`create_path` truncates T1's authority at BLK_X1_TO_S2_S and reserves it. On the
next tick `extend_path` tries to grow past the boundary (segment ['BLK_X1_TO_S2_S',
'PLT_S2_S_BLOCK']); it can't (T2), so `_best_effort_truncate` -> `_rollback_segment`
runs. The rollback released BLK_X1_TO_S2_S — the *seam* block that is the live
path's already-committed authority-end — while leaving it in `resource_sequence`.
The block then sat in T1's forward authority as FREE for ~120 ticks, rendering
blue, and T1 eventually drove its head onto an unreserved block.

design_new.md:1059 / :1062 require a blocked extend to leave the live path
unchanged ("keep the live path and retry later"); :179 says PathDispatcher owns
the reservations for every not-yet-entered authority block. The canonical
`active_path_blocks_held_or_owned` invariant encodes that coupling and trips the
moment the seam goes FREE-but-forward (~tick 5), long before the daisy-chain
timing that makes the full S1->S4 journey test fragile — so this test is short
and deterministic.
"""

import pytest

from bootstrap import get_current_scene_id
from tests.journey.events import Event, Journey
from tests.journey.harness import JourneyHarness
from tests.journey.invariants import active_path_blocks_held_or_owned
from tests.journey.scenes.demo_5_scene import Demo5Scene
from tests.journey.triggers import at_tick


if get_current_scene_id() != "demo_5":
    pytest.skip(
        f"Test built for scene 'demo_5'; active scene is "
        f"{get_current_scene_id()!r}.",
        allow_module_level=True,
    )


@pytest.mark.parametrize("dt,label", [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")])
def test_blocked_extend_keeps_seam_reserved(dt, label):
    """Across T1's first-leg creep-and-hold against the truncation boundary,
    every forward block in T1's ACTIVE path must stay reserved by that path
    (or occupied by T1). The seam-over-release bug violates this within a few
    ticks of departure; the fix keeps the boundary block reserved.

    A short fixed budget is enough: the seam is released ~tick 4 and stays
    FREE-but-forward until T1 reaches it ~tick 126, so the invariant fails
    fast well inside this window.
    """
    end_tick = max(80, int(120 * 0.1 / dt))

    journey = Journey(
        train_id="T1",
        events=[Event(name="ran_clean", trigger=at_tick(end_tick), assertions=[])],
        invariants=[active_path_blocks_held_or_owned()],
    )

    harness = JourneyHarness(
        scene=Demo5Scene(),
        journey=journey,
        dt=dt,
        max_ticks=end_tick + 5,
        history_size=20,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
