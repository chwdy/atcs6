"""Tests for the network-level flank-protection producer.

design_new.md "Flank Protection Computation" (lines 679-688): at switch
placement / topology change, search the network graph outward from each
switch within FLANK_PROTECTION_DISTANCE_CM and record
`(neighbour_switch_id, blocking_state)` on `Switch.flank_protection` for
every nearby switch whose active branch could point back toward it.
Acquisition then locks the neighbour to the safe state `1 - blocking_state`
(design_new.md:990). Regression guard for gap #1 (the producer was missing
entirely; the field stayed `[]` and every flank consumer no-opped).
"""

import pytest

from track import TrackSegment, TRACK_EW, TRACK_NW, TRACK_SE, TRACK_NS
from network import RailwayNetwork
from switch import Switch
from switch_manager import SwitchLockRequest
from constants import FLANK_PROTECTION_DISTANCE_CM


def _demo5():
    from demo_5 import create_comprehensive_demo

    return create_comprehensive_demo()


class TestFlankProducer:
    def test_adjacent_throat_switches_flank_protect_each_other(self):
        """The demo_5 crossover throat: SW_XOVER_N(7,8) and SW_XOVER_S(8,8)
        share the E/W edge, so each must appear in the other's
        flank_protection list."""
        net = _demo5()
        n = net.switches["SW_XOVER_N"]
        s = net.switches["SW_XOVER_S"]

        n_neighbours = {nb for nb, _ in n.flank_protection}
        s_neighbours = {nb for nb, _ in s.flank_protection}

        assert "SW_XOVER_S" in n_neighbours, (
            f"SW_XOVER_N should flank-protect its E neighbour SW_XOVER_S; "
            f"got {n.flank_protection}"
        )
        assert "SW_XOVER_N" in s_neighbours, (
            f"SW_XOVER_S should flank-protect its W neighbour SW_XOVER_N; "
            f"got {s.flank_protection}"
        )

    def test_blocking_state_is_binary(self):
        """Every stored blocking_state is a valid binary switch state."""
        net = _demo5()
        for sid, sw in net.switches.items():
            for nb, blocking_state in sw.flank_protection:
                assert blocking_state in (0, 1), (
                    f"{sid} stored non-binary blocking_state {blocking_state} "
                    f"for neighbour {nb}"
                )

    def test_distant_switches_have_no_flank_relationship(self):
        """The scissors crossovers SW_X1(2,3) and SW_X2(12,13) are far apart
        (separated by full 50cm blocks, well beyond the 5cm radius), so they
        must not flank-protect each other."""
        net = _demo5()
        x1 = net.switches["SW_X1"]
        x2 = net.switches["SW_X2"]
        assert "SW_X2" not in {nb for nb, _ in x1.flank_protection}
        assert "SW_X1" not in {nb for nb, _ in x2.flank_protection}

    def test_in_series_through_line_pair_drops_degenerate(self):
        """A neighbour that connects toward the protected switch in BOTH of its
        states has no safe state to lock, so the producer records no flank
        relationship in that direction.

        The depot throat: SW_LEAD(7,9) carries its main route through the edge
        facing SW_XOVER_N(7,8) in both DIVERGE (S<->E) and MAIN (N<->S), so
        SW_XOVER_N must NOT flank-protect SW_LEAD. The other direction is
        non-degenerate (SW_XOVER_N points its diverging branch at SW_LEAD only
        in state 1), so SW_LEAD keeps SW_XOVER_N with blocking_state 1."""
        net = _demo5()
        xn_flank = dict(net.switches["SW_XOVER_N"].flank_protection)
        lead_flank = dict(net.switches["SW_LEAD"].flank_protection)

        assert "SW_LEAD" not in xn_flank, (
            f"SW_XOVER_N must not flank-protect the in-series SW_LEAD (no safe "
            f"state exists); got {net.switches['SW_XOVER_N'].flank_protection}"
        )
        assert lead_flank.get("SW_XOVER_N") == 1, (
            f"SW_LEAD should keep SW_XOVER_N at blocking_state 1; got "
            f"{net.switches['SW_LEAD'].flank_protection}"
        )

    def test_blocking_state_is_deterministic(self):
        """The producer is order-independent: rebuilding the scene yields the
        identical flank_protection on every switch. (Pre-fix, a neighbour
        active toward the origin in both states returned whichever edge the
        traversal iteration hit first, so the stored blocking_state could
        flip between builds.)"""
        a = {sid: sorted(sw.flank_protection) for sid, sw in _demo5().switches.items()}
        b = {sid: sorted(sw.flank_protection) for sid, sw in _demo5().switches.items()}
        assert a == b

    def test_acquiring_primary_lock_acquires_neighbour_flank_lock_at_safe_state(self):
        """Locking a throat switch must also acquire a flank lock on its
        neighbour at the SAFE state = 1 - blocking_state (design_new.md:990)."""
        net = _demo5()
        sm = net.switch_manager
        n = net.switches["SW_XOVER_N"]
        s = net.switches["SW_XOVER_S"]

        flank_map = dict(n.flank_protection)
        assert "SW_XOVER_S" in flank_map, "precondition: SW_XOVER_N flanks SW_XOVER_S"
        blocking_state = flank_map["SW_XOVER_S"]
        expected_safe = 1 - blocking_state

        ntrack = net.get_track(7, 8)
        edges = sorted(ntrack.edges)
        route = frozenset({edges[0], edges[1]})
        req = SwitchLockRequest(
            switch_id="SW_XOVER_N",
            locked_state=int(n.position),
            source_switch_id=None,
            route=route,
        )
        result = sm.acquire_locks_for_segment(
            [req], path_id="P_FLANK", train_id="T1", manual_override=False
        )
        assert result.success, f"acquire failed: {result.reason}"

        flank_locks = [
            (k, v) for k, v in s.locks.items() if k[1] == "SW_XOVER_N"
        ]
        assert flank_locks, (
            "expected a flank lock on SW_XOVER_S sourced from SW_XOVER_N"
        )
        (_path_id, _source), (_train, locked_state) = flank_locks[0]
        assert locked_state == expected_safe, (
            f"flank lock should be at safe_state {expected_safe} "
            f"(= 1 - blocking_state {blocking_state}), got {locked_state}"
        )


class TestRoutePointExemption:
    """A switch the path traverses as a main-usage claim (a route point) is
    set and locked by its own main claim; it must not also be flank-locked by
    a sibling switch on the same path (design_new.md "Flank Protection
    Computation" + the route-point exemption under the switch-first
    acquisition step). Regression for the depot-throat acquisition deadlock:
    SW_XOVER_N and SW_LEAD sit in series on every route onto the depot lead
    (and on the normal S3_N->S2_N return leg). SW_LEAD flank-protects the
    in-series SW_XOVER_N, which the route also traverses as a primary, so
    without the exemption SW_LEAD's flank claim pins SW_XOVER_N to the opposite
    state and acquire_lock rejects SW_XOVER_N's own primary -- no path through
    the throat can ever form."""

    def test_in_series_route_points_acquire_together(self):
        net = _demo5()
        sm = net.switch_manager
        xn = net.switches["SW_XOVER_N"]
        lead = net.switches["SW_LEAD"]

        # precondition: SW_LEAD flank-protects SW_XOVER_N (the legitimate,
        # non-degenerate direction the producer keeps). SW_XOVER_N does NOT
        # flank SW_LEAD -- the connecting edge is active in both of SW_LEAD's
        # states, so that direction is degenerate and dropped (pinned by
        # TestFlankProducer.test_in_series_through_line_pair_drops_degenerate).
        assert "SW_XOVER_N" in {nb for nb, _ in lead.flank_protection}

        # depot approach primaries: SW_XOVER_N through onto the lead (state 1,
        # W->N) then SW_LEAD climbing the lead (state 0, S->N). The two main
        # claims demand opposite states -- exactly the pair that the cross
        # flank locks would otherwise contradict.
        reqs = [
            SwitchLockRequest(
                switch_id="SW_XOVER_N", locked_state=1,
                source_switch_id=None, route=frozenset({"W", "N"})),
            SwitchLockRequest(
                switch_id="SW_LEAD", locked_state=0,
                source_switch_id=None, route=frozenset({"S", "N"})),
        ]
        result = sm.acquire_locks_for_segment(
            reqs, path_id="P_THROAT", train_id="T1", manual_override=False)

        assert result.success, (
            f"in-series route points must acquire together; got failure at "
            f"{result.failed_switch_id!r}: {result.reason}"
        )
        # each switch physically aligned to its OWN primary state
        assert int(xn.position) == 1, "SW_XOVER_N should be at its primary state 1"
        assert int(lead.position) == 0, "SW_LEAD should be at its primary state 0"
        # neither carries an opposing flank lock sourced from the sibling primary
        assert ("P_THROAT", "SW_LEAD") not in xn.locks, (
            "SW_XOVER_N must not be flank-locked by sibling route point SW_LEAD"
        )
        assert ("P_THROAT", "SW_XOVER_N") not in lead.locks, (
            "SW_LEAD must not be flank-locked by sibling route point SW_XOVER_N"
        )

    def test_flank_still_applies_to_non_route_neighbour(self):
        """The exemption is scoped to route points only: when a primary's
        flank neighbour is NOT itself a main-usage claim on the path, the
        flank lock is still issued at safe_state. Acquiring SW_XOVER_N alone
        still flank-locks its non-route neighbour SW_XOVER_S."""
        net = _demo5()
        sm = net.switch_manager
        xn = net.switches["SW_XOVER_N"]
        s = net.switches["SW_XOVER_S"]

        flank_map = dict(xn.flank_protection)
        assert "SW_XOVER_S" in flank_map, "precondition: SW_XOVER_N flanks SW_XOVER_S"
        expected_safe = 1 - flank_map["SW_XOVER_S"]

        req = SwitchLockRequest(
            switch_id="SW_XOVER_N", locked_state=int(xn.position),
            source_switch_id=None, route=frozenset({"W", "N"}))
        result = sm.acquire_locks_for_segment(
            [req], path_id="P_ONE", train_id="T1", manual_override=False)
        assert result.success, f"acquire failed: {result.reason}"

        flank_locks = [v for k, v in s.locks.items() if k[1] == "SW_XOVER_N"]
        assert flank_locks, (
            "SW_XOVER_S is not a route point here, so its flank lock must "
            "still be issued"
        )
        assert flank_locks[0][1] == expected_safe


class TestFlankRecompute:
    def test_remove_switch_drops_stale_flank_entries(self):
        """Removing a switch must recompute neighbours so no stale
        (removed_id, state) entry survives (design_new.md:686)."""
        net = _demo5()
        # SW_XOVER_N flanks SW_XOVER_S; remove SW_XOVER_S and confirm the
        # dangling reference is gone from SW_XOVER_N.
        assert "SW_XOVER_S" in {nb for nb, _ in net.switches["SW_XOVER_N"].flank_protection}
        net.remove_switch("SW_XOVER_S")
        remaining = {nb for nb, _ in net.switches["SW_XOVER_N"].flank_protection}
        assert "SW_XOVER_S" not in remaining, (
            "stale flank entry to a removed switch must be dropped on recompute"
        )

    def test_minimal_adjacent_pair_built_by_hand(self):
        """A hand-built two-switch adjacency computes a flank relationship,
        independent of any demo scene."""
        net = RailwayNetwork()
        # Two adjacent switch tiles sharing the E/W edge, each EW + a diagonal
        # so each has a MAIN (EW) state and a DIVERGE state.
        net.add_track(TrackSegment(x=0, y=0, track_bits=TRACK_EW | TRACK_NW))
        net.add_track(TrackSegment(x=1, y=0, track_bits=TRACK_EW | TRACK_SE))
        net.add_switch(Switch(switch_id="SW_L", x=0, y=0))
        net.add_switch(Switch(switch_id="SW_R", x=1, y=0))

        l_neighbours = {nb for nb, _ in net.switches["SW_L"].flank_protection}
        r_neighbours = {nb for nb, _ in net.switches["SW_R"].flank_protection}
        assert "SW_R" in l_neighbours
        assert "SW_L" in r_neighbours
