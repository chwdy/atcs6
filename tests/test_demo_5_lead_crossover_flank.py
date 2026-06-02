"""Demo-5 lead-crossover flank triangle (SW_LEAD / SW_7_10 / SW_8_10).

The demo5_v2 layout adds a second diagonal crossover one row above the throat:
SW_7_10 (7,10) on the depot lead and SW_8_10 (8,10) on the N track. Together
with SW_LEAD (7,9) they form a switch triangle whose apex is SW_7_10 — its
common (facing) edge is the depot lead, and its two diverging legs reach
SW_LEAD (S) and SW_8_10 (E).

The loop's return leg (S3-N -> S2-N) runs the N-track mainline through BOTH
SW_LEAD and SW_8_10 as route points. Each flank-protects against SW_7_10, but
they demand OPPOSITE safe states (SW_LEAD wants SW_7_10 aimed away from it =
DIVERGE; SW_8_10 wants it aimed away = MAIN). No single position of the apex
satisfies both, so before the same-path conflict exemption the return-leg
segment acquisition was rejected on SW_7_10 and the train could never traverse
the switch combination.

Per design_new.md "switch-first acquisition step" the same-path conflict
exemption now leaves the un-protectable apex unlocked instead of failing the
segment, while unrelated flank protection (SW_XOVER_N -> SW_XOVER_S) is kept.
These tests fail if that exemption regresses.
"""

import contextlib
import io

import pytest

from bootstrap import get_current_scene_id, bootstrap_runtime
from switch_manager import SwitchLockRequest


if get_current_scene_id() != "demo_5":
    pytest.skip(
        f"Test built for scene 'demo_5'; active scene is "
        f"{get_current_scene_id()!r}.",
        allow_module_level=True,
    )


def _build():
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_5"
        )
    network.recompute_all_flank_protection()
    return network, dispatcher, sim


def test_triangle_flank_relationships_present():
    """The three triangle switches exist and SW_7_10 sits in BOTH neighbours'
    flank lists with OPPOSITE blocking states — the precondition for the
    conflict the exemption resolves."""
    net, _, _ = _build()
    for sid in ("SW_LEAD", "SW_7_10", "SW_8_10"):
        assert sid in net.switches, f"{sid} missing from demo_5"
    lead_fp = dict(net.switches["SW_LEAD"].flank_protection)
    xover_fp = dict(net.switches["SW_8_10"].flank_protection)
    assert "SW_7_10" in lead_fp and "SW_7_10" in xover_fp
    # Opposite blocking states -> opposite safe states demanded of SW_7_10.
    assert lead_fp["SW_7_10"] != xover_fp["SW_7_10"]


def test_return_leg_segment_acquires_with_unlocked_apex():
    """The loop return leg's route points (SW_8_10, SW_LEAD, SW_XOVER_N) lock
    atomically; the un-protectable apex SW_7_10 is skipped, and unrelated flank
    protection (SW_XOVER_N -> SW_XOVER_S) is still applied."""
    net, _, _ = _build()
    sm = net.resource_coordinator.switch_manager
    # Route points + the route each takes on the N-track-through return leg.
    reqs = [
        SwitchLockRequest("SW_8_10", 1, None, frozenset({"E", "S"})),
        SwitchLockRequest("SW_LEAD", 1, None, frozenset({"E", "S"})),
        SwitchLockRequest("SW_XOVER_N", 1, None, frozenset({"N", "W"})),
    ]
    res = sm.acquire_locks_for_segment(reqs, path_id="P_ret", train_id="TR")
    assert res.success, (
        f"return-leg acquisition rejected: {res.reason} @ {res.failed_switch_id}"
    )
    # Apex left unlocked — no single state protects both route points.
    assert not net.switches["SW_7_10"].locks
    # Unrelated, non-conflicting flank protection is preserved.
    assert ("P_ret", "SW_XOVER_N") in net.switches["SW_XOVER_S"].locks


def test_crossover_traversal_acquires():
    """A path that actually traverses the crossover (SW_7_10 DIVERGE + SW_8_10
    MAIN as route points) still acquires, and the single non-conflicting flank
    demand on SW_LEAD is kept (the route-point exemption, not the conflict
    exemption, applies here)."""
    net, _, _ = _build()
    sm = net.resource_coordinator.switch_manager
    reqs = [
        SwitchLockRequest("SW_7_10", 1, None, frozenset({"N", "E"})),  # DIVERGE/NE
        SwitchLockRequest("SW_8_10", 0, None, frozenset({"W", "E"})),  # MAIN/EW
    ]
    res = sm.acquire_locks_for_segment(reqs, path_id="P_x", train_id="TX")
    assert res.success, (
        f"crossover acquisition rejected: {res.reason} @ {res.failed_switch_id}"
    )
    assert ("P_x", "SW_7_10") in net.switches["SW_LEAD"].locks
