"""Regression: a Quick Route whose path crosses the demo_5 depot throat must
acquire and run.

The throat packs SW_XOVER_N(7,8) and SW_LEAD(7,9) one tile apart, in series
on the normal S3_N->S2_N return leg (and on every route onto the depot lead).
They appear in each other's `flank_protection` list, so before the route-point
exemption (design_new.md "Flank Protection Computation" + the switch-first
acquisition step) one switch's flank claim pinned the other to the opposite
state and `Switch.acquire_lock` rejected the route's own primary -- no path
through the throat could form. A clean train Quick-Routed S3_N -> S2_N then
sat in READY/NO_AUTHORITY forever.

This drives the canonical Quick Route flow (one-off Route + RouteRun, single
`stop` task on the target block, `all_or_nothing` acquisition) through the
service layer the editor calls, and asserts the train forms a path and
reaches S2_N without an emergency brake.
"""

import contextlib
import io
import threading
from datetime import datetime

import pytest

from bootstrap import bootstrap_runtime
from app_state import AppState
from block_manager import ReservedBlockTraversal
from services import train_route_service, train_service

# S2-N platform tiles (PLT_S2_N): arrival is "stopped with head on one of them".
S2_N_TILES = [(3, 4), (3, 5), (4, 5)]
# Block B_1, one tile past SW_8_10 and right before SW_LEAD (7,9).
B_1_TILE = (8, 9)
# PLT_DEPOT_3 platform tiles (base (3,12) + the two extension tiles).
DEPOT_3_TILES = [(3, 12), (3, 13), (2, 13)]


def _single_train_demo5(start_block, facing):
    """demo_5 network with exactly one powered train (no contention) so the
    throat path is exercised deterministically."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, persistence = bootstrap_runtime(
            demo=True, setup_train=False, scene="demo_5"
        )
        sim.add_train(
            train_id="T3", starting_block_id=start_block,
            initial_direction=facing, max_speed_kmh=40.0, length_cm=50.0,
            train_type="LOCOMOTIVE", initial_cab="A", start_tile=None,
        )
        sim.get_or_create_controller("T3").commands.enqueue_command("power_on")
    state = AppState(
        network=network, path_dispatcher=dispatcher, sim_controller=sim,
        persistence_manager=persistence,
        simulation_lock=threading.Lock(), simulation_running=True,
        simulation_speed=1.0, last_update_time=datetime.now(),
    )
    return state


def _quick_route(state, train_id, block_id, route_id, direction="forward"):
    """Replicate the editor's Quick Route three-call flow via the services.

    `direction` is the block-relative arrival frame; it must name the frame the
    intended walk actually reaches `block_id` in, because candidate search now
    enforces `requested_destination_direction` (design_new.md "`create_path()`
    flow"). The return leg from the north platforms into PLT_S2_N through the
    throat arrives "reverse".
    """
    train_route_service.create_route(
        state, route_id=route_id, name=f"Quick: {train_id} -> {block_id}",
        mode="one_off",
        tasks=[{
            "task_kind": "stop",
            "task_location": {"block_id": block_id},
            "task_direction": direction,
        }],
        acquisition_mode="all_or_nothing",
    )
    train_route_service.activate_route(state, route_id)
    train_service.assign_route(state, train_id, route_id=route_id, force=True)


@pytest.mark.parametrize("dt,label", [(0.1, "1x"), (0.2, "2x")])
def test_quick_route_return_leg_through_throat_forms_and_arrives(dt, label):
    """T3 at S3_N, Quick-Routed to S2_N (return leg crossing the throat),
    forms a path and arrives without EB."""
    state = _single_train_demo5("PLT_S3_N_BLOCK", "WEST")
    sim = state.sim_controller
    dispatcher = state.path_dispatcher

    for _ in range(15):
        sim.update(dt)

    _quick_route(state, "T3", "PLT_S2_N_BLOCK", "ROUTE_QR_RETURN", direction="reverse")

    path_formed_tick = None
    arrived = False
    max_ticks = max(80, int(120.0 / dt))
    for t in range(1, max_ticks):
        sim.update(dt)
        ctrl = sim.get_or_create_controller("T3")

        assert not ctrl.eb.active, (
            f"T3 emergency-braked at tick {t} (head "
            f"{ctrl._get_head_tile()}) on the S3_N->S2_N return leg. The "
            f"throat path should acquire cleanly with the route-point "
            f"flank-protection exemption."
        )

        if (path_formed_tick is None
                and dispatcher.active_path_id_by_train("T3") is not None):
            path_formed_tick = t

        head = ctrl._get_head_tile()
        if (head in S2_N_TILES
                and ctrl.physics_sync.current_speed_kmh < 0.5 and t > 25):
            arrived = True
            break

    assert path_formed_tick is not None, (
        "Quick Route to S2_N never formed a path. The S3_N->S2_N path crosses "
        "the throat (SW_XOVER_N -> SW_LEAD); before the route-point exemption "
        "the mutual flank locks made acquire_lock reject the route's own "
        "primary, so create_path failed every tick (BLOCKED_BEFORE_TARGET / "
        "acquire_lock_rejected at SW_LEAD)."
    )
    assert arrived, (
        f"T3 formed a path at tick {path_formed_tick} but did not reach S2_N "
        f"within {max_ticks} ticks. Final head="
        f"{sim.get_or_create_controller('T3')._get_head_tile()}."
    )


def test_quick_route_s2n_then_depot_3_arrives_without_eb():
    """The user's full scenario: Quick-Route T3 to S2_N, then (once arrived)
    Quick-Route it to the depot platform 3.

    The depot is reachable only through the throat (Bug 1) and the approach
    runs up the winding depot lead and doubles back through the ladder
    hairpin into PLT_DEPOT_3 (Bug 2). Before the cab-direction redesign,
    `_required_cab_for_path()`'s gross start->end vector fallback fired at
    each bend of the lead/ladder (per-tile direction 90deg from the rotating
    facing), flipping the required cab mid-route -> WRONG_DIRECTION_CAB ->
    ATC speed_limit 0 -> overspeed EB at a depot-lead/ladder tile. With the
    fallback restricted to the path-start case, a 90deg bend mid-path is no
    longer read as a reversal, so T3 climbs the lead, rounds the ladder, and
    stops in the platform with no cab flip and no EB.
    """
    state = _single_train_demo5("PLT_S4_N_BLOCK", "WEST")
    sim = state.sim_controller

    # --- leg 1: drive to S2_N (the throat-crossing return leg) ---
    for _ in range(15):
        sim.update(0.1)
    _quick_route(state, "T3", "PLT_S2_N_BLOCK", "ROUTE_QR_LEG1", direction="reverse")
    reached_s2n = False
    for _ in range(700):
        sim.update(0.1)
        ctrl = sim.get_or_create_controller("T3")
        assert not ctrl.eb.active, "EB on leg 1 (S4_N -> S2_N)"
        if (ctrl._get_head_tile() in S2_N_TILES
                and ctrl.physics_sync.current_speed_kmh < 0.5):
            reached_s2n = True
            break
    assert reached_s2n, "T3 never reached S2_N on leg 1"
    for _ in range(30):  # let the platform stop settle
        sim.update(0.1)

    # --- leg 2: S2_N -> depot platform 3 (throat + lead + ladder hairpin) ---
    _quick_route(state, "T3", "PLT_DEPOT_3_BLOCK", "ROUTE_QR_LEG2", direction="reverse")
    reached_depot = False
    for t in range(1500):
        sim.update(0.1)
        ctrl = sim.get_or_create_controller("T3")
        assert not ctrl.eb.active, (
            f"T3 emergency-braked on the way to depot 3 (head "
            f"{ctrl._get_head_tile()}, tick {t}). The depot lead/ladder bends "
            f"rotate the train's facing; the cab-direction check must treat a "
            f"mid-path 90deg bend as a turn, not a reversal."
        )
        if (ctrl._get_head_tile() in DEPOT_3_TILES
                and ctrl.physics_sync.current_speed_kmh < 0.5):
            reached_depot = True
            break
    assert reached_depot, (
        "T3 did not reach depot platform 3. Final head="
        f"{sim.get_or_create_controller('T3')._get_head_tile()}."
    )


def test_best_effort_return_leg_truncated_at_b1_still_crosses_sw_lead():
    """Regression: a BEST_EFFORT return leg that truncates BETWEEN its two
    crossover route points must still traverse the second one.

    The return leg S3-N -> S2-N runs route points SW_8_10 then SW_LEAD, which
    flank-protect the lead-crossover apex SW_7_10 to OPPOSITE states. We reserve
    corridor block A ahead so the path can only acquire as far as B_1 — over
    SW_8_10 (which flank-locks SW_7_10), stopping before SW_LEAD. T3 drives to
    B_1 and physically enters SW_8_10, converting that flank lock into train
    protection on the apex. When block A frees, the later extend through SW_LEAD
    used to be rejected on the still-pinned apex (the same-path conflict
    exemption only fired when both route points were in one acquisition call),
    leaving the train stuck at B_1 forever. With the cross-segment exemption it
    crosses SW_LEAD and reaches S2-N.
    """
    state = _single_train_demo5("PLT_S3_N_BLOCK", "WEST")
    sim = state.sim_controller
    bm = sim.block_manager
    for _ in range(15):
        sim.update(0.1)

    # Reserve block A so the return-leg path must truncate at B_1.
    res = bm.reserve_blocks_for_path(
        block_ids=["BLK_S2_N_TO_S3_N_A"], path_id="DUMMY_BLOCKER", train_id="BLK",
        directions={"BLK_S2_N_TO_S3_N_A":
                    ReservedBlockTraversal(exit_edge="E", block_direction="forward")},
    )
    assert res.success, f"could not reserve block A: {res.reason}"

    # B_1 sits straight off SW_8_10, so the junction-fouling exclusion would
    # normally hold a truncated authority BACK before SW_8_10 (at B_2). Marking
    # B_1 one-way exempts it (a one-way block can host no opposing move), so the
    # path truncates ONTO B_1 over SW_8_10 — the split this test exercises. The
    # return leg enters B_1 from the SW_8_10 edge, its forward frame.
    state.network.blocks["BLK_S2_N_TO_S3_N_B_1"].allowed_travel_directions = {"forward"}

    # Same three-call Quick Route flow as `_quick_route`, but BEST_EFFORT so the
    # path truncates at B_1 (over SW_8_10) instead of failing the whole acquire.
    train_route_service.create_route(
        state, route_id="ROUTE_QR_TRUNC", name="Quick: T3 -> S2_N (best effort)",
        mode="one_off",
        tasks=[{"task_kind": "stop",
                "task_location": {"block_id": "PLT_S2_N_BLOCK"},
                "task_direction": "reverse"}],
        acquisition_mode="best_effort",
    )
    train_route_service.activate_route(state, "ROUTE_QR_TRUNC")
    train_service.assign_route(state, "T3", route_id="ROUTE_QR_TRUNC", force=True)

    released = False
    arrived = False
    for t in range(1, 1400):
        sim.update(0.1)
        ctrl = sim.get_or_create_controller("T3")
        assert not ctrl.eb.active, (
            f"T3 emergency-braked at tick {t} (head {ctrl._get_head_tile()})"
        )
        head = ctrl._get_head_tile()
        spd = ctrl.physics_sync.current_speed_kmh
        if not released and t > 80 and head == B_1_TILE and spd < 0.2:
            # Confirm the split state: apex pinned by T3's own SW_8_10 protection.
            apex = state.network.switches["SW_7_10"]
            assert apex.train_protection_claims or apex.locks, (
                "expected the apex to be pinned by SW_8_10 before block A frees"
            )
            bm.release_path_block_reservations(
                ["BLK_S2_N_TO_S3_N_A"], "DUMMY_BLOCKER"
            )
            released = True
        if released and head in S2_N_TILES and spd < 0.5 and t > 25:
            arrived = True
            break

    assert released, (
        "T3 never settled at B_1 behind the reserved block A — the truncation "
        "split was not exercised."
    )
    assert arrived, (
        "T3 stuck after block A freed — it could not extend through SW_LEAD. "
        f"Final head={sim.get_or_create_controller('T3')._get_head_tile()}. The "
        f"cross-segment same-path conflict exemption regressed: the apex SW_7_10 "
        f"stayed pinned by T3's own SW_8_10 protection and SW_LEAD was rejected."
    )


# B_2 tiles (BLK_S2_N_TO_S3_N_B_2): the last switch-clear block on the return
# leg before SW_8_10. (9,10) is the tile straight off SW_8_10 — "outside 8,10".
B_2_TILES = [(9, 10), (9, 11)]


@pytest.mark.parametrize("dt,label", [(0.1, "1x"), (0.2, "2x")])
def test_best_effort_return_leg_holds_at_b2_not_b1_when_throat_busy(dt, label):
    """Regression: with B_1 two-way (the demo default), a BEST_EFFORT return
    leg that cannot clear the throat truncates to B_2 — BEFORE SW_8_10 — and
    never parks a truncated authority on B_1 (8,9) or block A.

    Both B_1 (8,9) and A sit straight off a switch (SW_8_10 and SW_XOVER_N) and
    are two-way, so the junction-fouling exclusion (design_new.md best_effort
    `create_path` item 11) forbids ending a truncated authority on either: it
    holds back to B_2, the last switch-clear block, keeping the throat free for
    cross-traffic. Block A is reserved so the full S3_N->S2_N path is blocked;
    if the exclusion regressed, truncation would instead land on the now-only-
    occupancy-free B_1 (8,9), letting the train foul the SW_8_10 junction.

    The companion `test_best_effort_return_leg_truncated_at_b1_still_crosses_sw_lead`
    pins the opposite side of the same rule: B_1 marked one-way IS an allowed
    terminal, because a one-way block can host no opposing move.
    """
    state = _single_train_demo5("PLT_S3_N_BLOCK", "WEST")
    sim = state.sim_controller
    dispatcher = state.path_dispatcher
    bm = sim.block_manager
    for _ in range(15):
        sim.update(dt)

    # Reserve corridor block A so the full return path to S2_N is unreachable;
    # the only occupancy-free shorter terminal is B_1, which the exclusion must
    # still refuse (it is two-way and straight off SW_8_10).
    res = bm.reserve_blocks_for_path(
        block_ids=["BLK_S2_N_TO_S3_N_A"], path_id="DUMMY_BLOCKER", train_id="BLK",
        directions={"BLK_S2_N_TO_S3_N_A":
                    ReservedBlockTraversal(exit_edge="E", block_direction="forward")},
    )
    assert res.success, f"could not reserve block A: {res.reason}"

    # BEST_EFFORT Quick Route S3_N -> S2_N; B_1 stays two-way (the demo default).
    train_route_service.create_route(
        state, route_id="ROUTE_QR_B2HOLD",
        name="Quick: T3 -> S2_N (best effort, throat busy)",
        mode="one_off",
        tasks=[{"task_kind": "stop",
                "task_location": {"block_id": "PLT_S2_N_BLOCK"},
                "task_direction": "reverse"}],
        acquisition_mode="best_effort",
    )
    train_route_service.activate_route(state, "ROUTE_QR_B2HOLD")
    train_service.assign_route(state, "T3", route_id="ROUTE_QR_B2HOLD", force=True)

    path_formed = False
    settled_at_b2 = False
    max_ticks = max(120, int(90.0 / dt))
    for t in range(1, max_ticks):
        sim.update(dt)
        ctrl = sim.get_or_create_controller("T3")
        assert not ctrl.eb.active, (
            f"T3 emergency-braked at tick {t} (head {ctrl._get_head_tile()})"
        )

        ap = dispatcher.get_active_path("T3")
        if ap is not None:
            path_formed = True
            auth_end = ap.get_authority_end_block_id()
            # The core regression guard: a truncated authority on this leg may
            # only terminate at B_2. Never B_1 (8,9) — that fouls SW_8_10.
            assert auth_end == "BLK_S2_N_TO_S3_N_B_2", (
                f"truncated authority ended at {auth_end}, expected "
                f"BLK_S2_N_TO_S3_N_B_2. The junction-fouling exclusion must hold "
                f"a two-way terminal back before SW_8_10 (at B_2), not on B_1."
            )

        head = ctrl._get_head_tile()
        # The truncated authority must never let the head park on B_1 (8,9).
        assert head != B_1_TILE, (
            f"T3 parked head on B_1 {B_1_TILE} at tick {t} — the junction-"
            f"fouling exclusion should hold it at B_2, outside SW_8_10."
        )

        spd = ctrl.physics_sync.current_speed_kmh
        if path_formed and t > 60 and head in B_2_TILES and spd < 0.5:
            settled_at_b2 = True
            break

    assert path_formed, (
        "best-effort path never formed for the S3_N->S2_N return leg with block "
        "A reserved; create_path should truncate to B_2, not fail."
    )
    assert settled_at_b2, (
        "T3 never settled on B_2 outside SW_8_10. Final head="
        f"{sim.get_or_create_controller('T3')._get_head_tile()}."
    )
