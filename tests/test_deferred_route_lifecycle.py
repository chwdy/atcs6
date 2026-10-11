"""Deferred route-lifecycle mechanisms (design_new.md "Route And RouteExecutor").

Two independent mechanisms that both defer a route-list mutation to the Action
stage rather than performing it inline in the HTTP/service handler:

1. **request_route_assignment / `_pending_route_assignments`** (lines
   1530-1545, 1860-1864). A retarget / swap / unassign of a train with a live
   RUNNING run does NOT mutate either route's `assigned_train_ids` inline: it
   arms the abort + inclusive cancel and holds the request until Action's
   `remove_train_route_run(...)` archives the old run and commits the
   replacement. A train with no live run commits immediately.

2. **Deferred route-deletion / `pending_route_deletions` / `_historical_routes`**
   (lines 2098-2103). `DELETE /api/train-routes/<id>` never removes a route
   used by a live or pending run inline: it records the route as pending
   deletion (`request_route_deletion`), fans out per-train `route_deactivate`,
   and lets
   `finalize_pending_route_deletions()` archive + unregister it once nothing
   holds it. The archived definition stays resolvable through `get_route(...)`.

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_deferred_route_lifecycle.py -q``
"""

import threading
from datetime import datetime

import pytest

from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from train import Train, TrainCar
from train_route import RouteRunStatus, RouteStartMode

from services import train_route_service, train_service
from services.exceptions import NotFoundError, ValidationError


@pytest.fixture
def state_with_train():
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="TRAIN_A", consist=[car], max_speed=80.0)
    network.add_train(train)
    return state, train


def _make_route(state, route_id, stop_block):
    return train_route_service.create_route(
        state,
        route_id=route_id,
        name=route_id,
        tasks=[{"task_kind": "stop", "task_location": stop_block}],
    )


def _setup_running_run(state, train, *, route_id="R", stop_block="BLK_1"):
    """Register a one-stop route, assign the train, allocate a RUNNING run, and
    ensure the controller exists for the Action consumers."""
    route = _make_route(state, route_id, stop_block)
    route.assign_train(train.train_id)
    rex = state.sim_controller.route_executor
    rex._ensure_run_for(train, route)
    state.sim_controller.get_or_create_controller(train.train_id)
    return route, rex


# --------------------------------------------------------------------------
# Mechanism 1: request_route_assignment / _pending_route_assignments
# --------------------------------------------------------------------------


def test_retarget_with_live_run_is_deferred_then_committed(state_with_train):
    """A retarget of a train with a live run holds the request; Action's
    teardown commits it (design_new.md lines 1530-1531, 2105)."""
    state, train = state_with_train
    route_r, rex = _setup_running_run(state, train, route_id="R", stop_block="BLK_1")
    route_r2 = _make_route(state, "R2", "BLK_2")
    controller = state.sim_controller.get_controller("TRAIN_A")
    assert rex.execution_state_for("TRAIN_A") is not None

    rex.request_route_assignment("TRAIN_A", "R2")

    # Deferred: neither stored list mutated; the request is held; abort armed.
    assert "TRAIN_A" in route_r.assigned_train_ids          # old still assigned
    assert "TRAIN_A" not in route_r2.assigned_train_ids     # new NOT yet assigned
    assert rex._pending_route_assignments["TRAIN_A"]["requested_route_id"] == "R2"
    assert controller.route_hooks.phase_latches.route_abort is True
    # Held request state is never reported as an assignment.
    assert rex.assigned_route_id_for("TRAIN_A") == "R"

    # Action abort consumer archives the old run and commits the replacement.
    state.sim_controller._consume_pending_route_abort(controller, train)

    assert rex.execution_state_for("TRAIN_A") is None       # old run torn down
    assert "TRAIN_A" not in route_r.assigned_train_ids      # old unassigned
    assert "TRAIN_A" in route_r2.assigned_train_ids         # new assigned
    assert "TRAIN_A" not in rex._pending_route_assignments  # queue consumed
    assert rex.assigned_route_id_for("TRAIN_A") == "R2"


def test_unassign_with_live_run_is_deferred(state_with_train):
    """Unassigning a train with a live run holds `{requested_route_id: None}`
    and leaves the train on no route once Action tears the run down."""
    state, train = state_with_train
    route_r, rex = _setup_running_run(state, train)
    controller = state.sim_controller.get_controller("TRAIN_A")

    rex.request_route_assignment("TRAIN_A", None)

    assert "TRAIN_A" in route_r.assigned_train_ids          # not unassigned yet
    assert rex._pending_route_assignments["TRAIN_A"]["requested_route_id"] is None
    assert controller.route_hooks.phase_latches.route_abort is True

    state.sim_controller._consume_pending_route_abort(controller, train)

    assert rex.execution_state_for("TRAIN_A") is None
    assert "TRAIN_A" not in route_r.assigned_train_ids      # now unassigned
    assert rex.assigned_route_id_for("TRAIN_A") is None     # on no route
    assert "TRAIN_A" not in rex._pending_route_assignments


def test_assign_without_run_commits_immediately(state_with_train):
    """With no live run the request commits inline -- nothing is deferred and no
    abort is armed (design_new.md line 1530, "no RUNNING run" case)."""
    state, train = state_with_train
    route_r = _make_route(state, "R", "BLK_1")
    rex = state.sim_controller.route_executor
    controller = state.sim_controller.get_or_create_controller("TRAIN_A")

    rex.request_route_assignment("TRAIN_A", "R")

    assert "TRAIN_A" in route_r.assigned_train_ids          # immediate assign
    assert "TRAIN_A" not in rex._pending_route_assignments  # nothing deferred
    assert controller.route_hooks.phase_latches.route_abort is False
    assert rex.assigned_route_id_for("TRAIN_A") == "R"


def test_same_route_running_request_is_noop(state_with_train):
    """A request naming the same route a live run already follows is an
    idempotent no-op; a supplied start override is dropped (line 1530)."""
    state, train = state_with_train
    route_r, rex = _setup_running_run(state, train)
    controller = state.sim_controller.get_controller("TRAIN_A")

    rex.request_route_assignment(
        "TRAIN_A",
        "R",
        start_override={"start_mode": RouteStartMode.NEAREST, "start_task_index": None},
    )

    assert controller.route_hooks.phase_latches.route_abort is False  # no teardown
    assert "TRAIN_A" not in rex._pending_route_assignments
    assert "TRAIN_A" not in rex._pending_start_overrides    # start override dropped
    assert rex.execution_state_for("TRAIN_A") is not None    # run untouched


def test_start_override_moves_to_pending_start_on_commit(state_with_train):
    """A per-assignment start override rides beside the held route and only
    moves to `_pending_start_overrides` when the assignment commits (line 2079).
    """
    state, train = state_with_train
    route_r, rex = _setup_running_run(state, train, route_id="R", stop_block="BLK_1")
    route_r2 = _make_route(state, "R2", "BLK_2")
    controller = state.sim_controller.get_controller("TRAIN_A")

    rex.request_route_assignment(
        "TRAIN_A",
        "R2",
        start_override={"start_mode": RouteStartMode.NEAREST, "start_task_index": None},
    )
    # Held beside the route, not yet in the start-override stash.
    assert "TRAIN_A" not in rex._pending_start_overrides
    held = rex._pending_route_assignments["TRAIN_A"]["start_override"]
    assert held["start_mode"] == RouteStartMode.NEAREST

    state.sim_controller._consume_pending_route_abort(controller, train)

    # Moved to the start-override stash for the next fresh run.
    assert rex._pending_start_overrides["TRAIN_A"]["start_mode"] == RouteStartMode.NEAREST
    assert "TRAIN_A" in route_r2.assigned_train_ids


def test_remove_with_no_run_removes_stored_assignment(state_with_train):
    """`remove_train_route_run` with no live run still removes any stored
    assignment (design_new.md line 1860, "a train assigned before its first Plan
    allocation")."""
    state, train = state_with_train
    route_r = _make_route(state, "R", "BLK_1")
    rex = state.sim_controller.route_executor
    route_r.assign_train("TRAIN_A")                          # assigned, no run yet
    assert rex.execution_state_for("TRAIN_A") is None

    rex.remove_train_route_run("TRAIN_A")

    assert "TRAIN_A" not in route_r.assigned_train_ids


def test_assign_route_service_defers_around_live_run(state_with_train):
    """End-to-end through the service: `assign_route(force=True)` on a train with
    a live run defers via `request_route_assignment` instead of mutating lists
    inline."""
    state, train = state_with_train
    route_r, rex = _setup_running_run(state, train, route_id="R", stop_block="BLK_1")
    _make_route(state, "R2", "BLK_2")
    controller = state.sim_controller.get_controller("TRAIN_A")

    train_service.assign_route(state, "TRAIN_A", route_id="R2", force=True)

    # Deferred by the service.
    assert rex._pending_route_assignments["TRAIN_A"]["requested_route_id"] == "R2"
    assert "TRAIN_A" in route_r.assigned_train_ids           # old list untouched
    assert controller.route_hooks.phase_latches.route_abort is True


# --------------------------------------------------------------------------
# Mechanism 2: deferred route-deletion / pending_route_deletions / _historical_routes
# --------------------------------------------------------------------------


def test_delete_route_with_no_trains_finalizes_immediately(state_with_train):
    """Deleting a route nothing holds archives + unregisters it in the same call
    (design_new.md line 2103)."""
    state, _train = state_with_train
    route = _make_route(state, "R", "BLK_1")
    rex = state.sim_controller.route_executor

    assert train_route_service.delete_route(state, "R") is True

    assert "R" not in rex.route_registry                    # removed from active
    assert rex.pending_route_deletions == set()             # nothing left pending
    assert rex.get_historical_route("R") is route           # archived definition
    assert rex.get_route("R") is route                      # resolves via fallback


def test_delete_route_with_live_run_defers_until_teardown(state_with_train):
    """Deleting a route with a live run retains it, fans out `route_deactivate`,
    and archives only after the Action teardown (design_new.md lines
    2098-2102)."""
    state, train = state_with_train
    route, rex = _setup_running_run(state, train, route_id="R", stop_block="BLK_1")
    controller = state.sim_controller.get_controller("TRAIN_A")
    assert rex.execution_state_for("TRAIN_A") is not None

    assert train_route_service.delete_route(state, "R") is True

    # Deferred: retained + pending; train still assigned; latch armed.
    assert "R" in rex.route_registry
    assert "R" in rex.pending_route_deletions
    assert "TRAIN_A" in route.assigned_train_ids
    assert controller.route_hooks.phase_latches.route_deactivate is True
    assert rex.get_route("R") is route                      # still resolves live

    # Action deactivate consumer tears the run down; finalize then archives.
    state.sim_controller._consume_pending_route_deactivate(controller, train)

    assert rex.execution_state_for("TRAIN_A") is None        # run torn down
    assert "TRAIN_A" not in route.assigned_train_ids         # unassigned
    assert "R" not in rex.route_registry                     # removed from active
    assert "R" not in rex.pending_route_deletions            # finalized
    assert rex.get_historical_route("R") is route            # archived
    assert rex.get_route("R") is route                       # resolves via fallback
    # A route pending deletion is never re-applied by _pending_reassign.
    assert "TRAIN_A" not in rex._pending_reassign


def test_delete_route_with_assigned_but_unrun_train_finalizes_on_teardown(
    state_with_train,
):
    """A train assigned but not yet running still defers deletion: the
    `route_deactivate` teardown unassigns it, then finalize archives."""
    state, train = state_with_train
    route = _make_route(state, "R", "BLK_1")
    rex = state.sim_controller.route_executor
    route.assign_train("TRAIN_A")                            # assigned, no run
    controller = state.sim_controller.get_or_create_controller("TRAIN_A")

    train_route_service.delete_route(state, "R")

    # Retained while the train is still assigned (the pending latch holds it).
    assert "R" in rex.route_registry
    assert "R" in rex.pending_route_deletions
    assert controller.route_hooks.phase_latches.route_deactivate is True

    state.sim_controller._consume_pending_route_deactivate(controller, train)

    assert "TRAIN_A" not in route.assigned_train_ids
    assert "R" not in rex.route_registry                     # finalized
    assert rex.get_historical_route("R") is route


def test_delete_route_with_assigned_never_ticked_train_finalizes_now(
    state_with_train,
):
    """A train assigned but never ticked has no controller (paused / just-loaded
    / assigned through the API while paused), so there is no `route_deactivate`
    latch to arm and no Action consumer that would ever unassign it. `delete_route`
    must still remove the route: it unassigns the train inline through
    `remove_train_route_run(...)` so `finalize_pending_route_deletions()` archives
    the route in the same call. Regression: previously the no-controller train was
    skipped, the train stayed in `assigned_train_ids`, and the route lingered
    forever while DELETE returned True.
    """
    state, _train = state_with_train
    route = _make_route(state, "R", "BLK_1")
    rex = state.sim_controller.route_executor
    route.assign_train("TRAIN_A")                            # assigned, no run
    # Do NOT create a controller: the sim has never ticked this train.
    assert state.sim_controller.get_controller("TRAIN_A") is None
    assert rex.execution_state_for("TRAIN_A") is None

    assert train_route_service.delete_route(state, "R") is True

    # Finalized in the same call -- nothing held it once the train was unassigned.
    assert "TRAIN_A" not in route.assigned_train_ids         # unassigned inline
    assert "R" not in rex.route_registry                     # removed from active
    assert "R" not in rex.pending_route_deletions            # nothing left pending
    assert rex.get_historical_route("R") is route            # archived definition
    assert rex.get_route("R") is route                       # resolves via fallback
    # Re-delete is an idempotent success against the archived route.
    assert train_route_service.delete_route(state, "R") is True


def test_redelete_after_archival_is_idempotent(state_with_train):
    """Re-deleting a route already archived by a completed deferred deletion is
    a no-op success, not a 404 (design_new.md line 2103)."""
    state, _train = state_with_train
    _make_route(state, "R", "BLK_1")
    train_route_service.delete_route(state, "R")             # archives immediately

    assert train_route_service.delete_route(state, "R") is True


def test_redelete_while_teardown_pending_is_idempotent(state_with_train):
    """Re-deleting while teardown is still pending is idempotent -- the route
    stays retained and pending (design_new.md line 2103)."""
    state, train = state_with_train
    route, rex = _setup_running_run(state, train, route_id="R", stop_block="BLK_1")
    controller = state.sim_controller.get_controller("TRAIN_A")

    train_route_service.delete_route(state, "R")
    assert train_route_service.delete_route(state, "R") is True   # idempotent

    assert "R" in rex.route_registry                         # still retained
    assert "R" in rex.pending_route_deletions
    assert controller.route_hooks.phase_latches.route_deactivate is True


def test_delete_unknown_route_raises(state_with_train):
    """A route that never existed still raises NotFoundError."""
    state, _train = state_with_train
    with pytest.raises(NotFoundError):
        train_route_service.delete_route(state, "NEVER_EXISTED")


# ---------------------------------------------------------------------------
# A finished run is deleted, and an unassigned one-off route goes with it
# (design_new.md "Terminal route completion"). A route pending deletion gets no
# fresh run and no new assignment (step `9c4f`, "Route deletion waits for live
# teardown").
# ---------------------------------------------------------------------------


def test_finish_deletes_the_run_and_the_unassigned_one_off_route(state_with_train):
    state, train = state_with_train
    route, rex = _setup_running_run(state, train, route_id="R", stop_block="BLK_1")
    run = rex.execution_state_for("TRAIN_A")
    assert run is not None

    rex.finish_route_run("TRAIN_A")

    assert rex.execution_state_for("TRAIN_A") is None
    assert run not in rex.history_for_train("TRAIN_A")
    assert rex.assigned_route_id_for("TRAIN_A") is None
    assert "R" not in rex.route_registry
    assert "R" not in rex.pending_route_deletions
    assert rex.get_historical_route("R") is route


def test_finish_keeps_a_one_off_route_another_train_still_uses(state_with_train):
    state, train = state_with_train
    route, rex = _setup_running_run(state, train, route_id="R", stop_block="BLK_1")
    route.assign_train("TRAIN_B")

    rex.finish_route_run("TRAIN_A")

    assert rex.execution_state_for("TRAIN_A") is None
    assert "R" in rex.route_registry
    assert "R" not in rex.pending_route_deletions
    assert route.assigned_train_ids == ["TRAIN_B"]


def test_plan_allocates_no_run_on_a_route_pending_deletion(state_with_train):
    state, train = state_with_train
    route = _make_route(state, "R", "BLK_1")
    route.assign_train("TRAIN_A")
    rex = state.sim_controller.route_executor
    state.sim_controller.get_or_create_controller("TRAIN_A").powered = True

    rex.request_route_deletion("R")
    rex.tick_train_plan("TRAIN_A", 0.1)
    assert rex.execution_state_for("TRAIN_A") is None

    # Control: the same route with no deletion pending does get a run.
    rex.pending_route_deletions.discard("R")
    rex.tick_train_plan("TRAIN_A", 0.1)
    assert rex.execution_state_for("TRAIN_A") is not None


def test_a_route_pending_deletion_accepts_no_new_assignment(state_with_train):
    state, _train = state_with_train
    _make_route(state, "R", "BLK_1")
    state.sim_controller.route_executor.request_route_deletion("R")

    with pytest.raises(ValidationError, match="being deleted"):
        train_service.assign_route(state, "TRAIN_A", route_id="R")
    with pytest.raises(ValidationError, match="being deleted"):
        train_route_service.update_route(state, "R", assigned_train_ids=["TRAIN_A"])
    assert state.sim_controller.route_executor.assigned_route_id_for("TRAIN_A") is None
