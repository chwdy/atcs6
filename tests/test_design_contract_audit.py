"""Design-contract audit tests.

Each test in this file pins ONE specific divergence between the current
simulation core and `design_new.md`. They are all expected to FAIL today;
each failure carries a precise diagnostic that points at the offending
contract line.

If you're here because a test failed: read the docstring of the failing
test for the contract reference and the suspected fix site. Don't silence
or adjust the assertion; either fix the production code or update
`design_new.md` first (and then update the test to match).

Findings as of the audit:

  F1  Block.occupied_by stale: BLOCK_MAIN_WEST never tail-vacates.
      (Covered by tests/test_demo_train_journey_strict.py — included here
      only as an index entry, no new test.)

  F2  Block.occupied_by released mid-traversal: BLOCK_MAIN_EAST_1 flips
      OCCUPIED → FREE while train is on (5,0). Same root cause as F1.
      (Covered by tests/test_demo_train_journey_strict.py.)

  F4  Path-completion gate fires on arrival: once the train is fully
      stopped at the usable end of its authority, the stop-arrival check
      calls `try_complete_active_path()` and the path becomes `COMPLETED`.
      A dwell after the stop is not a controller state (the train stays
      READY), so nothing closes the completion gate.

  F6  Route run never gets removed for normal completion: because F4
      prevents `try_complete_active_path()` → `handle_path_completion()`
      → terminal-walk detection → `finish_route_run()`, the route stays
      assigned to the train forever after it stops.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime

pytestmark = pytest.mark.scene("demo_main")


# --------------------------------------------------------------------------- #
# Shared scene driver
# --------------------------------------------------------------------------- #


def _run_demo_until_stopped(max_ticks: int = 2000, dt: float = 0.1):
    """Build the demo scene, run it until the train has been stopped for
    at least 80 ticks, return `(network, sim_controller, controller, ticks)`.

    Bootstrap defaults the demo train to ``DEMO_ROUTE_V2`` (the extended
    multi-leg journey); these audit tests need the original single-stop
    terminal behavior, so re-point the train to ``DEMO_ROUTE_V1``.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        network, _path, sim_controller, _persistence = bootstrap_runtime(
            demo=True, setup_train=True
        )
    train_id = "DEMO_TRAIN"
    registry = sim_controller.route_executor.route_registry
    v2 = registry.get("DEMO_ROUTE_V2")
    if v2 is not None:
        v2.is_active = False
        v2.unassign_train(train_id)
    v1 = registry.get("DEMO_ROUTE_V1")
    if v1 is not None:
        v1.is_active = True
        v1.assign_train(train_id)
    controller = sim_controller.get_or_create_controller(train_id)
    stopped_streak = 0
    last_tick = 0
    for tick in range(1, max_ticks + 1):
        sim_controller.update(dt)
        last_tick = tick
        if tick > 200 and controller.physics_sync.current_speed_kmh <= 0.05:
            stopped_streak += 1
            if stopped_streak >= 80:
                break
        else:
            stopped_streak = 0
    return network, sim_controller, controller, last_tick


# --------------------------------------------------------------------------- #
# F4: path-completion fires on arrival at a terminal
# --------------------------------------------------------------------------- #


def test_F4_path_completes_on_arrival_at_terminal():
    """design_new.md "Active-path completion procedure" steps `8df5`/`bc55`
    /`790a`/`09b0`: once the train is fully stopped at the usable end of
    the live authority, the stop-arrival check must call
    `PathDispatcher.try_complete_active_path(train_id)`, the path's status
    must transition to `COMPLETED`, and `received_path_plan` must return
    `None` thereafter. A dwell after the stop is not a controller state (the
    train stays READY), so nothing closes the completion gate.
    """
    _, sim_controller, controller, _ = _run_demo_until_stopped()
    dispatcher = sim_controller.path_dispatcher
    active = dispatcher.get_active_path("DEMO_TRAIN")
    assert active is None, (
        f"Expected `active_path_id_by_train('DEMO_TRAIN')` to be None "
        f"after the train stopped at terminal, but got path "
        f"{active.path_id!r} with status={active.status.value!r}. "
        f"design_new.md step `09b0` requires `Path.status = COMPLETED` "
        f"after the post-physics stop check passes."
    )
    assert controller.path_projection.received_path_plan is None, (
        f"Expected `controller.path_projection.received_path_plan` to be None after path "
        f"completion (design_new.md step `09b0`), got "
        f"{controller.path_projection.received_path_plan!r}."
    )


# --------------------------------------------------------------------------- #
# F6: Route run never gets removed for normal completion path
# --------------------------------------------------------------------------- #


def test_F6_route_finishes_after_terminal_stop():
    """design_new.md step `879a` and the "Active-path completion procedure"
    steps `09b0`/`29c5`/`d843`: after the train stops at its terminal,
    Phase 3 completion → `RouteExecutor.handle_path_completion(...)` →
    Phase 1 `finish_route_run(train_id)` must run, which sets
    `RouteRun.current_route_status = FINISHED` and makes
    `assigned_route_id_for(train_id)` return None.

    Currently the route remains assigned forever because F4 stops the
    chain at the completion handshake.
    """
    _, sim_controller, controller, _ = _run_demo_until_stopped()
    re_ex = sim_controller.route_executor
    assigned = re_ex.assigned_route_id_for("DEMO_TRAIN")
    assert assigned is None, (
        f"After the train stopped at the terminal and the route's "
        f"terminal-walk should have run, `assigned_route_id_for"
        f"('DEMO_TRAIN')` is still {assigned!r}. Per design_new.md "
        f"step `879a`, Phase 1 must call `finish_route_run(train_id)` "
        f"so the run transitions out of RUNNING and assigned-route reads "
        f"return None. Root cause: the upstream completion handshake "
        f"(F4) never fires."
    )
