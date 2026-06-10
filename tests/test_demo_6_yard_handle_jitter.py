"""Regression: throat-stuck yard trains must not jitter their handle.

In demo_6 the yard stock departs throat-limited, so trains far back in the
queue come to rest against a best-effort-truncated authority end that lies a
few cm ahead (the operational speed curve collapses to ~0 within the lookahead
window even though the limit *at the head* is still healthy, e.g. ~8.8 km/h).

The bug: ``ATOComputer.compute_handle`` decided whether to power up from a
standstill by looking only at the distance-0 ceiling, blind to the curve
collapsing just ahead. It commanded a power notch (P2), the train lurched to
~0.12 km/h, the next tick's deadband coasted it back to N, friction halted it,
and it repeated forever — a 4-tick ``N <-> P2`` limit cycle (the visible handle
jitter). Worse, each 0.12 km/h lurch exceeded ``STOPPED_SPEED_THRESHOLD_KMH``
(0.1), so ``is_stopped()`` flickered ``False`` every cycle and reset
``RouteExecutor``'s ``idle_hold_ticks``; the idle-hold release could never reach
its budget, so the fouled authority was never freed.

The fix (``ato.py`` ``compute_handle``): when the train is stopped and the
operational curve collapses to <= the stop threshold anywhere in the lookahead
window, hold ``N`` instead of powering up. The train stays genuinely stopped,
the handle is steady, and the idle-hold release can recognize the stall and free
the held authority.

The test pins the bug signature — the precise stopped-power-up condition must
never occur — and self-checks that at least one train actually reaches the
stopped-at-collapsed-authority state, so the guard cannot pass vacuously.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime
from constants import STOPPED_SPEED_THRESHOLD_KMH

pytestmark = pytest.mark.scene("demo_6")


def _boot():
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
    return network, dispatcher, sim


def _is_power(handle) -> bool:
    return handle.value.startswith("P")


@pytest.mark.parametrize("dt,label", [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")])
def test_no_stopped_power_up_into_collapsed_authority(dt, label):
    """The bug signature must never occur for any train on any tick.

    Bug signature: the train is stopped (``speed <= STOPPED_SPEED_THRESHOLD_KMH``)
    AND its ATO lookahead shows the operational authority collapsing within the
    window to <= the stop threshold AND the commanded handle is a power notch.
    That is exactly the futile creep-into-a-wall the fix's hold branch prevents.

    This mirrors the fix's exact condition (``ato.py`` stores the *raw*
    ``min_target_in_window_kmh`` for that reason), so the only way to trip it is a
    regression that powers up where the fix would hold. A legitimate departure is
    excluded because it has real room ahead (``min_target_in_window`` above the
    threshold), and a mid-creep train sitting one tick at the boundary is
    excluded too because its raw ``min_target`` is above the threshold.
    """
    network, _disp, sim = _boot()
    train_ids = sorted(network.trains.keys())

    hold_branch_exercised = False
    n_ticks = int(round(250 * 0.1 / dt))
    for _ in range(n_ticks):
        sim.update(dt)
        for tid in train_ids:
            c = sim.get_or_create_controller(tid)
            debug = c.ato.debug
            if not debug or "min_target_in_window_kmh" not in debug:
                continue
            # Use the speed compute_handle *decided* on, not the post-physics
            # speed: a regressed power-up lurch would already have accelerated
            # the train above the threshold by the time we sample, hiding it.
            stopped = debug["current_speed_kmh"] <= STOPPED_SPEED_THRESHOLD_KMH
            collapsed = (
                debug["min_target_in_window_kmh"] <= STOPPED_SPEED_THRESHOLD_KMH
            )
            handle = c.ops.handle_position
            if stopped and collapsed:
                hold_branch_exercised = True
                assert not _is_power(handle), (
                    f"{tid} powered up ({handle.value}) while stopped at a "
                    f"collapsed authority end (min_target_in_window="
                    f"{debug['min_target_in_window_kmh']:.2f} km/h) — handle jitter "
                    f"regression. Expected a hold (N)."
                )

    # The scenario must actually be reached, or the invariant above is vacuous:
    # at least one throat-limited train must have hit the stopped-hold condition.
    assert hold_branch_exercised, (
        "no train reached the stopped-at-collapsed-authority condition in "
        f"{n_ticks} ticks — the regression guard never exercised the fix path"
    )
