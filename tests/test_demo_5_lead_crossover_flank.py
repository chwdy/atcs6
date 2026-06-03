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

from bootstrap import bootstrap_runtime
from switch_manager import SwitchLockRequest


pytestmark = pytest.mark.scene("demo_5")


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


def test_split_segments_future_lock_timing_acquires():
    """The two route points whose flank claims conflict at the apex arrive in
    SEPARATE acquisition calls (what BEST_EFFORT truncation produces): segment 1
    locks SW_8_10 (flank-locking the apex SW_7_10), segment 2 locks SW_LEAD.

    Before the cross-segment fix the same-path conflict exemption only fired
    when both route points were in one call, so segment 2's SW_LEAD flank claim
    was rejected on the still-held SW_7_10 flank lock and the train could never
    traverse SW_LEAD. The exemption must also recognise the opposite-state
    demand already held on the apex by this same path."""
    net, _, _ = _build()
    sm = net.resource_coordinator.switch_manager
    pid = "P_split"

    seg1 = [SwitchLockRequest("SW_8_10", 1, None, frozenset({"E", "S"}))]
    r1 = sm.acquire_locks_for_segment(seg1, path_id=pid, train_id="T1")
    assert r1.success, f"segment 1 (SW_8_10) rejected: {r1.reason} @ {r1.failed_switch_id}"
    # Segment 1 flank-locked the apex (a future lock keyed by this path).
    assert (pid, "SW_8_10") in net.switches["SW_7_10"].locks

    seg2 = [SwitchLockRequest("SW_LEAD", 1, None, frozenset({"E", "S"}))]
    r2 = sm.acquire_locks_for_segment(seg2, path_id=pid, train_id="T1")
    assert r2.success, (
        f"segment 2 (SW_LEAD) rejected: {r2.reason} @ {r2.failed_switch_id} — "
        f"the cross-segment same-path conflict exemption regressed"
    )
    # SW_LEAD's own primary is locked; the apex keeps only SW_8_10's protection.
    assert (pid, None) in net.switches["SW_LEAD"].locks
    assert (pid, "SW_8_10") in net.switches["SW_7_10"].locks


def test_split_segments_train_protection_timing_acquires():
    """Same split, but the earlier route point (SW_8_10) has been physically
    entered before segment 2 runs, so its flank lock on the apex has converted
    to a train_protection_claim. Segment 2 (SW_LEAD) must still acquire — the
    exemption recognises the opposite-state demand held by this same train."""
    net, _, _ = _build()
    sm = net.resource_coordinator.switch_manager
    pid = "P_split2"

    seg1 = [SwitchLockRequest("SW_8_10", 1, None, frozenset({"E", "S"}))]
    assert sm.acquire_locks_for_segment(seg1, path_id=pid, train_id="T1").success

    # Train head enters SW_8_10: consumes its primary lock and converts the
    # apex's flank lock into train-owned protection.
    sm.on_physical_entry("SW_8_10", "T1", pid, frozenset({"E", "S"}))
    apex = net.switches["SW_7_10"]
    assert not apex.locks, "apex flank lock should have converted on entry"
    assert any(c[0] == "T1" and c[1] == "SW_8_10"
               for c in apex.train_protection_claims), (
        f"expected SW_8_10 protection on apex, got {apex.train_protection_claims}"
    )

    seg2 = [SwitchLockRequest("SW_LEAD", 1, None, frozenset({"E", "S"}))]
    r2 = sm.acquire_locks_for_segment(seg2, path_id=pid, train_id="T1")
    assert r2.success, (
        f"segment 2 (SW_LEAD) rejected: {r2.reason} @ {r2.failed_switch_id} — "
        f"the exemption must fold in train-owned protection from this train's "
        f"already-entered route point"
    )
    assert (pid, None) in net.switches["SW_LEAD"].locks


def test_split_segments_preserve_cross_train_flank_protection():
    """The cross-segment fold must NOT exempt the apex from a DIFFERENT train's
    genuine flank demand. If another train already protects the apex at one
    state, this train's opposing route-point flank claim is still rejected
    (real flank protection between trains is preserved)."""
    net, _, _ = _build()
    sm = net.resource_coordinator.switch_manager

    # Another train's path flank-locks the apex SW_7_10 via its SW_8_10 route
    # point at state 0.
    other = [SwitchLockRequest("SW_8_10", 1, None, frozenset({"E", "S"}))]
    assert sm.acquire_locks_for_segment(other, path_id="P_other", train_id="T2").success
    assert net.switches["SW_7_10"].locks[("P_other", "SW_8_10")][1] == 0

    # T1's SW_LEAD route point demands the opposite apex state. Because the
    # held demand belongs to a DIFFERENT path/train, it is a genuine conflict
    # and must be rejected (not exempted).
    mine = [SwitchLockRequest("SW_LEAD", 1, None, frozenset({"E", "S"}))]
    r = sm.acquire_locks_for_segment(mine, path_id="P_mine", train_id="T1")
    assert not r.success and r.failed_switch_id == "SW_7_10", (
        f"cross-train flank protection should block here, got "
        f"success={r.success} @ {r.failed_switch_id}"
    )
