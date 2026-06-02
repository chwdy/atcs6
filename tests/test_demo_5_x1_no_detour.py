"""Regression test: BEST_EFFORT must wait at a contested crossover, not
detour around the whole map.

Scenario
--------
demo_5 scissors crossover SW_X1 connects the N and S tracks between stations
S1 and S2. Its DIVERGE state (1) lets each track run straight through; its
MAIN state (0) is the swap/cross. A train going from S2-N to S1-S needs the
CROSS (MAIN), and the shortest walk the pathfinder returns is exactly that
one-crossing route:

    PLT_S2_N -> BLK_X1_TO_S2_N -> (SW_X1 MAIN) -> BLK_S1_S_TO_X1 -> PLT_S1_S

When another train (A) is running straight and holds SW_X1 in DIVERGE, the
crossing is blocked. The dispatcher's longer alternatives to PLT_S1_S go all
the way around the loop (via SW_X2 / the S1<->S4 tunnels).

Pre-fix behaviour
-----------------
create_path()/extend_path() iterated over every candidate (shortest first)
and FELL THROUGH to the next, longer candidate whenever the chosen one could
not be acquired or truncated. With the train parked right before X1 (head in
BLK_X1_TO_S2_N) the crossing route had no forward block to truncate to, so the
loop committed the train to an ~8-block detour around the map via SW_X2 -
producing the head-to-head deadlock the operator reported.

Post-fix expectation (design_new.md:931 / :940)
-----------------------------------------------
"selects exactly one candidate ... There is no fall-through to a second
candidate within the same call; if that candidate cannot produce any storable
live resource_sequence ... the call fails." So:

  * Parked right before X1: create_path FAILS (train waits in place); it never
    acquires a route that leaves X1's neighbourhood.
  * Approaching from S2-N: create_path truncates to BLK_X1_TO_S2_N (advance to
    the block before X1 and wait) - the working near case must still work.
  * extend_path from that truncated authority returns BLOCKED_NO_GROWTH (keep
    retrying the same path_id), never a detour.
"""

import contextlib
import io

import pytest

from bootstrap import get_current_scene_id, bootstrap_runtime
from runtime_payloads import AcquisitionMode


if get_current_scene_id() != "demo_5":
    pytest.skip(
        f"Test built for scene 'demo_5'; active scene is "
        f"{get_current_scene_id()!r}.",
        allow_module_level=True,
    )


# Any of these appearing in an acquired authority means the dispatcher routed
# the train AROUND the map instead of waiting at X1.
DETOUR_MARKERS = frozenset({
    "SW_X2",
    "BLK_S2_N_TO_S3_N_A", "BLK_S2_N_TO_S3_N_B_1", "BLK_S2_N_TO_S3_N_B_2",
    "BLK_S3_N_TO_X2", "BLK_X2_TO_S4_S", "BLK_X2_TO_S4_N",
    "PLT_S3_N_BLOCK", "PLT_S3_S_BLOCK",
    "PLT_S4_N_BLOCK", "PLT_S4_S_BLOCK",
    "PLT_S1_N_BLOCK",
})


def _seq_ids(res):
    seq = getattr(res, "resource_sequence", None)
    return [r.resource_id for r in seq] if seq else []


def _is_detour(res):
    return bool(DETOUR_MARKERS.intersection(_seq_ids(res)))


def _build_demo5(setup_train):
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _ = bootstrap_runtime(
            demo=True, setup_train=setup_train, scene="demo_5"
        )
    return network, dispatcher, sim


def _lock_x1_diverge_via_train_a(dispatcher, network):
    """Train A (T1, at PLT_S1_S) runs straight S1S->S2S, locking SW_X1 in its
    straight (DIVERGE) state - the 'another train going straight' from the
    report. The straight route arrives at S2-S in the 'forward' block frame;
    we verify the lock actually took (not just that some path was created)."""
    x1 = network.get_track(2, 3)
    for dirc in ("forward", "reverse"):
        res = dispatcher.create_path(
            train_id="T1",
            requested_destination_block_id="PLT_S2_S_BLOCK",
            requested_destination_direction=dirc,
            acquisition_mode=AcquisitionMode.BEST_EFFORT,
        )
        sw_x1 = network.switches.get(x1.switch_id) if x1.switch_id else None
        locked_diverge = sw_x1 is not None and any(
            state == 1 for (_train, state) in sw_x1.locks.values()
        )
        if res.success and locked_diverge:
            return res
        # A path was created but it did not lock X1 in DIVERGE (e.g. it routed
        # the long way around): release it and try the other direction.
        if res.success and res.path_id:
            dispatcher.cancel_path(res.path_id)
    raise AssertionError("setup failed: could not lock SW_X1 in DIVERGE via train A")


def test_parked_before_locked_crossover_does_not_detour():
    """Head parked in BLK_X1_TO_S2_N (right before X1). With X1 locked against
    the crossing, create_path must FAIL (wait), never acquire a map-long
    detour."""
    network, dispatcher, sim = _build_demo5(setup_train=False)

    # A at S1-S (will lock X1), B parked right before X1.
    sim.add_train(train_id="T1", starting_block_id="PLT_S1_S_BLOCK",
                  initial_direction="NORTH", max_speed_kmh=40.0, length_cm=50.0,
                  train_type="LOCOMOTIVE", initial_cab="A", start_tile=(2, 1))
    placed = False
    for facing in ("EAST", "SOUTH"):
        try:
            sim.add_train(train_id="TB", starting_block_id="BLK_X1_TO_S2_N",
                          initial_direction=facing, max_speed_kmh=40.0,
                          length_cm=20.0, train_type="LOCOMOTIVE",
                          initial_cab="A", start_tile=(2, 4))
            placed = True
            break
        except ValueError:
            continue
    assert placed, "could not place TB in BLK_X1_TO_S2_N"

    _lock_x1_diverge_via_train_a(dispatcher, network)

    # The crossing arrives at PLT_S1_S in the 'reverse' block frame; that is the
    # natural (shortest) target the route executor would resolve.
    res = dispatcher.create_path(
        train_id="TB",
        requested_destination_block_id="PLT_S1_S_BLOCK",
        requested_destination_direction="reverse",
        acquisition_mode=AcquisitionMode.BEST_EFFORT,
    )

    assert not _is_detour(res), (
        f"dispatcher routed TB around the map instead of waiting at X1: "
        f"{_seq_ids(res)}"
    )
    # Per design_new.md:940 the single chosen (crossing) candidate cannot store
    # a prefix (no forward block before X1), so the call fails - TB waits.
    assert not res.success, (
        f"expected create_path to fail (TB waits before X1), got "
        f"{res.result_kind} seq={_seq_ids(res)}"
    )


def test_approaching_crossover_truncates_to_block_before_x1():
    """The working near case: a train one block back (T4 at S2-N) must still
    truncate to BLK_X1_TO_S2_N and wait there - the fix must not break it."""
    network, dispatcher, sim = _build_demo5(setup_train=True)
    _lock_x1_diverge_via_train_a(dispatcher, network)

    res = dispatcher.create_path(
        train_id="T4",
        requested_destination_block_id="PLT_S1_S_BLOCK",
        requested_destination_direction="reverse",
        acquisition_mode=AcquisitionMode.BEST_EFFORT,
    )

    assert res.success, f"expected truncated success, got {res.failure_kind}"
    assert not _is_detour(res), f"unexpected detour: {_seq_ids(res)}"
    assert res.authority_end_block_id == "BLK_X1_TO_S2_N", (
        f"expected authority to stop at the block before X1, got "
        f"{res.authority_end_block_id} seq={_seq_ids(res)}"
    )


def test_extend_at_locked_crossover_blocks_no_growth():
    """After the truncated create, extend_path from the authority end must
    return BLOCKED_NO_GROWTH (keep waiting on the same path), not detour."""
    network, dispatcher, sim = _build_demo5(setup_train=True)
    _lock_x1_diverge_via_train_a(dispatcher, network)

    created = dispatcher.create_path(
        train_id="T4",
        requested_destination_block_id="PLT_S1_S_BLOCK",
        requested_destination_direction="reverse",
        acquisition_mode=AcquisitionMode.BEST_EFFORT,
    )
    assert created.success and created.authority_end_block_id == "BLK_X1_TO_S2_N"
    seq_before = _seq_ids(created)

    extended = dispatcher.extend_path(
        path_id=created.path_id,
        requested_destination_block_id="PLT_S1_S_BLOCK",
        requested_destination_direction="reverse",
        acquisition_mode=AcquisitionMode.BEST_EFFORT,
    )

    assert not _is_detour(extended), (
        f"extend routed T4 around the map: {_seq_ids(extended)}"
    )
    from runtime_payloads import PathAcquisitionResultKind
    assert extended.result_kind == PathAcquisitionResultKind.BLOCKED_NO_GROWTH, (
        f"expected BLOCKED_NO_GROWTH (wait), got "
        f"success={extended.success} kind={extended.result_kind} "
        f"fail={extended.failure_kind}"
    )
    assert _seq_ids(extended) == seq_before, "authority must be unchanged while waiting"
