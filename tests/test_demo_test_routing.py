"""Routing test set on the ``demo_test`` two-branch layout (curve + speed).

A 50cm train runs a one-off route from p1 to p2. Two branches connect them: a
SHORT one (``BLK_2FEE7D`` -> ``BLK_2FDE9B``) and a LONG one (seven blocks). With
no per-block constraints the pathfinder picks the SHORT branch on tile-count
then length.

Curve radius (hard filter): the train's minimum curve radius is 315mm. When the
SHORT branch's blocks are tightened below that, the pathfinder rejects every
candidate through the SHORT branch (the train physically cannot take those
curves) and routes down the LONG branch. A curve *wider* than the minimum never
excludes a branch -- only a tighter one does.

Speed limit (soft cost): the pathfinder ranks candidates by estimated travel
time. With both branches set to the same length, the branch with the higher
posted speed limit is faster and is chosen -- even when it is the LONG branch
with more tiles. Swapping which branch is faster swaps the winner, proving the
speed limit is the deciding factor.

Slope (hard filter): the train stalls on a climb steeper than its 15% max
gradient, so a block that steep uphill is excluded and the train reroutes. Slope
is directional -- the SHORT branch is traversed in reverse for p1->p2, so a
negative ``grade_percent`` is the climb. A descent of the same magnitude never
excludes (the train only gets stuck going up).

Each case is driven at three playback speeds; the routing decision is taken once
at path-acquisition time, so it must be identical across dt.
"""

import pytest

from enums import TrainState
from tests.journey.assertions import assert_speed_eq, assert_state
from tests.journey.events import Event, Journey
from tests.journey.harness import JourneyHarness
from tests.journey.invariants import no_eb, no_overspeed
from tests.journey.scenes.demo_test_scene import (
    DemoTestScene,
    LONG_BRANCH_BLOCKS,
    P2_BLOCK,
    SHORT_BRANCH_BLOCKS,
)
from tests.journey.triggers import block_entered, predicate

# 315mm minimum curve radius the test train can take == 31.5cm. The curve case
# tightens the SHORT branch to 280mm (< minimum, infeasible); the control case
# widens it to 350mm (> minimum, still fine).
TIGHT_RADIUS_CM = 28.0
WIDE_RADIUS_CM = 35.0

_DT_VARIANTS = [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")]


def _assert_branch(expected_blocks, forbidden_blocks, label):
    """Assert the train traversed every expected branch block and no forbidden
    one. Evaluated once the train has entered the p2 block, by which point the
    whole branch is recorded in the cumulative tracker."""
    expected = set(expected_blocks)
    forbidden = set(forbidden_blocks)

    def check(s):
        entered = set(s.tracker.blocks_entered)
        missing = expected - entered
        assert not missing, (
            f"{label}: train did not traverse expected branch blocks "
            f"{sorted(missing)}; entered={sorted(entered)}"
        )
        crossed = forbidden & entered
        assert not crossed, (
            f"{label}: train wrongly traversed the other branch "
            f"{sorted(crossed)}; entered={sorted(entered)}"
        )

    return check


def _build_journey(expected_blocks, forbidden_blocks, label):
    """Fresh journey per test variant so trigger `fired` state never leaks."""
    return Journey(
        train_id=DemoTestScene.train_id,
        events=[
            Event(
                name="reached_p2_on_expected_branch",
                trigger=block_entered(P2_BLOCK),
                assertions=[_assert_branch(expected_blocks, forbidden_blocks, label)],
            ),
            Event(
                name="settled_at_p2",
                # Fire once the train has fully settled into READY at p2 (the
                # one-off route's only stop). Keying on READY rather than a
                # speed threshold avoids firing mid-creep during the slow ATO
                # approach into the platform.
                trigger=predicate(
                    lambda s: P2_BLOCK in s.tracker.blocks_entered
                    and s.state == TrainState.READY
                ),
                assertions=[assert_state(TrainState.READY), assert_speed_eq(0.0)],
            ),
        ],
        invariants=[no_eb(), no_overspeed()],
    )


def _run(scene, expected_blocks, forbidden_blocks, label, dt):
    harness = JourneyHarness(
        scene=scene,
        journey=_build_journey(expected_blocks, forbidden_blocks, label),
        dt=dt,
        max_ticks=max(600, int(3000 * 0.1 / dt)),
    )
    result = harness.run()
    assert result.success, "\n" + result.report()


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_no_curve_limit_takes_short_branch(dt, label):
    """Baseline: with no per-block curve limit the train takes the SHORT branch."""
    _run(
        DemoTestScene(short_block_radius_cm=None),
        expected_blocks=SHORT_BRANCH_BLOCKS,
        forbidden_blocks=LONG_BRANCH_BLOCKS,
        label=f"baseline[{label}]",
        dt=dt,
    )


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_tight_short_branch_reroutes_to_long_branch(dt, label):
    """A 280mm curve on the SHORT branch is tighter than the train's 315mm
    minimum, so the pathfinder routes the train down the LONG branch."""
    _run(
        DemoTestScene(short_block_radius_cm=TIGHT_RADIUS_CM),
        expected_blocks=LONG_BRANCH_BLOCKS,
        forbidden_blocks=SHORT_BRANCH_BLOCKS,
        label=f"tight-curve[{label}]",
        dt=dt,
    )


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_wide_short_branch_still_takes_short_branch(dt, label):
    """Control: a 350mm curve is wider than the train's 315mm minimum, so the
    SHORT branch stays feasible and the train still takes it -- the filter
    excludes only curves tighter than the train can navigate."""
    _run(
        DemoTestScene(short_block_radius_cm=WIDE_RADIUS_CM),
        expected_blocks=SHORT_BRANCH_BLOCKS,
        forbidden_blocks=LONG_BRANCH_BLOCKS,
        label=f"wide-curve[{label}]",
        dt=dt,
    )


# Speed-limit (soft travel-time cost). Both branches are equalised to the same
# length, so the only difference is the posted speed limit. A slow branch is
# 30 km/h; the faster branch is 90 km/h.
SLOW_KMH = 30.0
FAST_KMH = 90.0


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_equal_length_faster_long_branch_wins(dt, label):
    """Same-length branches, higher speed limit on the LONG branch: it is the
    faster route, so the train takes it even though it has more tiles. The
    pathfinder minimises travel time, not distance, so speed overrides the
    tile-count preference that picks SHORT in the no-limit baseline."""
    _run(
        DemoTestScene(
            equalize_branch_lengths=True,
            short_branch_speed_kmh=SLOW_KMH,
            long_branch_speed_kmh=FAST_KMH,
        ),
        expected_blocks=LONG_BRANCH_BLOCKS,
        forbidden_blocks=SHORT_BRANCH_BLOCKS,
        label=f"long-faster[{label}]",
        dt=dt,
    )


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_equal_length_faster_short_branch_wins(dt, label):
    """Symmetric control: same equal-length branches, but now the SHORT branch
    carries the higher speed limit, so the winner swaps back to SHORT. Only the
    speed assignment changed between this and the previous test, which proves
    the posted speed limit is what decides the route."""
    _run(
        DemoTestScene(
            equalize_branch_lengths=True,
            short_branch_speed_kmh=FAST_KMH,
            long_branch_speed_kmh=SLOW_KMH,
        ),
        expected_blocks=SHORT_BRANCH_BLOCKS,
        forbidden_blocks=LONG_BRANCH_BLOCKS,
        label=f"short-faster[{label}]",
        dt=dt,
    )


# Slope (hard climb filter). The train climbs up to 15% (its baked max_slope).
# The SHORT branch is traversed in reverse for p1->p2, so the effective grade
# the train experiences is the negation of the block's grade_percent: a negative
# grade_percent is the climb, a positive one is a descent.
STEEP_CLIMB_GRADE = -20.0  # reverse traversal -> +20% climb, steeper than 15%
GENTLE_CLIMB_GRADE = -10.0  # reverse traversal -> +10% climb, within 15%
STEEP_DESCENT_GRADE = 20.0  # reverse traversal -> -20% descent, never excludes


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_steep_uphill_short_branch_reroutes_to_long(dt, label):
    """A climb on the SHORT branch steeper than the train's 15% max gradient is
    a stall, so the pathfinder excludes the SHORT branch and routes down the
    LONG branch -- the slope hard filter, mirroring curve radius."""
    _run(
        DemoTestScene(short_branch_grade_percent=STEEP_CLIMB_GRADE),
        expected_blocks=LONG_BRANCH_BLOCKS,
        forbidden_blocks=SHORT_BRANCH_BLOCKS,
        label=f"steep-uphill[{label}]",
        dt=dt,
    )


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_gentle_uphill_short_branch_takes_short(dt, label):
    """Control: a 10% climb is within the train's 15% max gradient, so the
    SHORT branch stays feasible and the train still takes it -- only a climb
    steeper than the train can manage excludes."""
    _run(
        DemoTestScene(short_branch_grade_percent=GENTLE_CLIMB_GRADE),
        expected_blocks=SHORT_BRANCH_BLOCKS,
        forbidden_blocks=LONG_BRANCH_BLOCKS,
        label=f"gentle-uphill[{label}]",
        dt=dt,
    )


@pytest.mark.parametrize("dt,label", _DT_VARIANTS)
def test_steep_downhill_short_branch_takes_short(dt, label):
    """Directional control: the same 20% magnitude as the reroute case, but now
    a descent rather than a climb. The train only stalls going up, so a steep
    downhill never excludes and the SHORT branch is still taken."""
    _run(
        DemoTestScene(short_branch_grade_percent=STEEP_DESCENT_GRADE),
        expected_blocks=SHORT_BRANCH_BLOCKS,
        forbidden_blocks=LONG_BRANCH_BLOCKS,
        label=f"steep-downhill[{label}]",
        dt=dt,
    )
