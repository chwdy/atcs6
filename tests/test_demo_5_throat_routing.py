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
from speed_profile import SpeedProfileRegistry
from services import train_route_service, train_service

# S2-N platform tiles (PLT_S2_N): arrival is "stopped with head on one of them".
S2_N_TILES = [(3, 4), (3, 5), (4, 5)]
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
        speed_profile_registry=SpeedProfileRegistry(),
        simulation_lock=threading.Lock(), simulation_running=True,
        simulation_speed=1.0, last_update_time=datetime.now(),
    )
    return state


def _quick_route(state, train_id, block_id, route_id):
    """Replicate the editor's Quick Route three-call flow via the services."""
    train_route_service.create_route(
        state, route_id=route_id, name=f"Quick: {train_id} -> {block_id}",
        mode="one_off",
        tasks=[{
            "task_kind": "stop",
            "task_location": {"block_id": block_id},
            "task_direction": "forward",
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

    _quick_route(state, "T3", "PLT_S2_N_BLOCK", "ROUTE_QR_RETURN")

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
    _quick_route(state, "T3", "PLT_S2_N_BLOCK", "ROUTE_QR_LEG1")
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
    _quick_route(state, "T3", "PLT_DEPOT_3_BLOCK", "ROUTE_QR_LEG2")
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
