"""Phase-1 gap-analysis fixes (design_new.md vs code, 2026-09-20).

Covers:
- H3: the fatal block-conflict payload carries `incoming_path_id` (always) and
  `prior_owner_path_id` (from the prior owner's active path for an occupied
  conflict, from `block.reservation.path_id` for a reserved conflict).
- M7: `SwitchManager.on_switch_tail_vacated(...)` returns `bool`.
- M3: the `/api/trains` composite carries `ato_status`.
"""

from types import SimpleNamespace

from block_manager import ReservedBlockTraversal
from runtime_payloads import FatalConflictKind
from switch import Switch
from switch_manager import SwitchManager
from tests.test_block_occupancy_cap import build_line, place


# --------------------------------------------------------------------------- #
# H3 — fatal block-conflict payload path ids
# --------------------------------------------------------------------------- #

def _bm_with_capture():
    network = build_line()  # BLK_0..BLK_5
    sim, *_ = place(network, head_x=0, length_cm=40.0)
    bm = sim.block_manager
    captured = []
    bm.fatal_conflict_reporter = captured.append
    bm.active_path_getter = lambda tid: {
        "A": SimpleNamespace(path_id="pathA"),
        "B": SimpleNamespace(path_id="pathB"),
    }.get(tid)
    return network, bm, captured


def test_occupied_conflict_carries_incoming_and_prior_owner_path_ids():
    _network, bm, captured = _bm_with_capture()

    assert bm.occupy_block("BLK_2", "A") is True  # clean
    assert bm.occupy_block("BLK_2", "B") is False  # occupied by A -> conflict

    assert len(captured) == 1
    payload = captured[0]
    assert payload.conflict_kind == FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN
    assert payload.incoming_path_id == "pathB"        # always, from active path
    assert payload.prior_owner_path_id == "pathA"     # prior owner's active path


def test_reserved_conflict_reads_prior_owner_path_id_from_reservation():
    _network, bm, captured = _bm_with_capture()

    # Reserve BLK_3 for path P1 (train A). The reservation, not the active-path
    # getter, is the source of prior_owner_path_id for a reserved conflict.
    result = bm.reserve_blocks_for_path(
        ["BLK_3"], "P1", "A",
        directions={"BLK_3": ReservedBlockTraversal(exit_edge="E", block_direction="forward")},
    )
    assert result.success is True, result.reason

    assert bm.occupy_block("BLK_3", "B") is False  # reserved for A -> conflict

    assert len(captured) == 1
    payload = captured[0]
    assert payload.conflict_kind == FatalConflictKind.RESERVED_FOR_OTHER_TRAIN
    assert payload.incoming_path_id == "pathB"
    assert payload.prior_owner_path_id == "P1"  # from block.reservation.path_id


def test_disabled_entry_conflict_has_no_prior_owner_path_id():
    _network, bm, captured = _bm_with_capture()
    bm._block_registry["BLK_4"].disabled = True

    assert bm.occupy_block("BLK_4", "B") is False

    payload = captured[0]
    assert payload.conflict_kind == FatalConflictKind.DISABLED_RESOURCE_ENTRY
    assert payload.incoming_path_id == "pathB"
    assert payload.prior_owner_path_id is None


# --------------------------------------------------------------------------- #
# M7 — tail-vacate returns bool
# --------------------------------------------------------------------------- #

def test_on_switch_tail_vacated_returns_bool():
    route = frozenset({"N", "E"})
    switch = Switch(switch_id="SW_1", x=0, y=0)
    switch.route_owners = {route: "T1"}
    manager = SwitchManager(switches={"SW_1": switch}, network=None)

    # A clean tail-vacate that removed the owned entry -> True.
    assert manager.on_switch_tail_vacated("SW_1", "T1", route=route) is True
    # The train now owns nothing on this switch -> False (no-op).
    assert manager.on_switch_tail_vacated("SW_1", "T1", route=route) is False
    # A train that never owned an entry -> False.
    assert manager.on_switch_tail_vacated("SW_1", "T2", route=route) is False

    # An unknown switch -> False.
    assert manager.on_switch_tail_vacated("SW_MISSING", "T1", route=route) is False


# --------------------------------------------------------------------------- #
# M3 — /api/trains composite carries ato_status
# --------------------------------------------------------------------------- #

def test_api_trains_composite_carries_ato_status(tmp_path):
    import threading
    from datetime import datetime

    from app_factory import create_app
    from app_state import AppState
    from persistence import PersistenceManager

    network = build_line()
    sim, _train, _controller, _physics = place(network, head_x=0, length_cm=40.0)
    state = AppState(
        network=network,
        path_dispatcher=sim.path_dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path)),
        simulation_lock=threading.Lock(),
    )
    client = create_app(state).test_client()

    resp = client.get("/api/trains")
    assert resp.status_code == 200
    # `/api/trains` and `/api/network` share one builder, so the read model is
    # served here too, in `operator_status_by_train_id` (design_new.md "Data
    # Flow To UI").
    status = resp.get_json()["operator_status_by_train_id"]["T"]
    assert set(status) == {"ato_status", "pending_actions", "hold_reason"}
