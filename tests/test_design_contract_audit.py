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

  F3  Phase-ownership violation: `TrainController.tick()` (Phase 2)
      writes `state = RUNNING_AT_STATION` inside `_is_at_station()`.
      design_new.md:196,282 says only Phase 1 (`enter_station_dwell()`)
      may make that transition.

  F4  Path-completion gate never fires: because F3 closes the state==
      RUNNING_MOVING gate inside `is_stopped_at_authority_end()` BEFORE
      Phase 3's post-physics stop check runs, `try_complete_active_path()`
      never executes. The active path therefore stays `ACTIVE` for the
      entire run.

  F5  One-tick `RUNNING_AT_STATION` + ACTIVE path window: at the tick
      Phase 2 transitions to RUNNING_AT_STATION, Phase 3 hasn't run yet,
      so the snapshot violates the design invariant "RUNNING_AT_STATION
      ⇒ received_path_plan is None" (design_new.md ~line 1632 / the
      `at_station_implies_no_path` invariant in `tests/journey/`).

  F6  Route run never gets removed for normal completion: because F4
      prevents `try_complete_active_path()` → `handle_path_completion()`
      → terminal-walk detection → `finish_route_run()`, the route stays
      assigned to the train forever after it stops.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime, get_current_scene_id
from enums import TrainState

if get_current_scene_id() != "demo_main":
    pytest.skip(
        f"Test built for scene 'demo_main'; active scene is "
        f"{get_current_scene_id()!r}.",
        allow_module_level=True,
    )


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
# F3: Phase 2 writes RUNNING_AT_STATION (contract: only Phase 1 enter_station_dwell)
# --------------------------------------------------------------------------- #


def test_F3_phase2_does_not_write_running_at_station_state():
    """`TrainController.tick()` is Phase 2. Per design_new.md ~line 196 and
    ~line 282, only `RouteExecutor` (Phase 1) may transition the controller
    into `RUNNING_AT_STATION`, via `TrainController.enter_station_dwell()`.

    The current implementation writes `self.state = RUNNING_AT_STATION`
    directly inside Phase 2's `TrainController.tick()` whenever
    `_is_at_station()` returns True (train_controller.py:828-829). This
    test enables a one-tick guard around `Phase 2 → Phase 3 → Phase 1`
    and verifies the controller's state field is not written by Phase 2.

    We can't intercept the field write without monkey-patching, so we
    check the consequence: after a single tick where the train is stopped
    AT a route station, the dispatcher's path completion handshake should
    have fired and the path should be COMPLETED. With the current
    implementation it stays ACTIVE forever because the state field has
    been clobbered by Phase 2 before Phase 3 reads it.
    """
    network, sim_controller, controller, _ = _run_demo_until_stopped()
    dispatcher = sim_controller.path_dispatcher
    active = dispatcher.get_active_path("DEMO_TRAIN")
    # Phase-2 transition to RUNNING_AT_STATION is the *cause*; the *effect*
    # we can assert directly is "after the train fully stopped at the
    # terminal, the dispatcher path must not still be ACTIVE."
    if active is None:
        return  # OK — would mean the path completed normally
    raise AssertionError(
        f"After the train stopped at the East Terminal, the dispatcher "
        f"path {active.path_id!r} is still ACTIVE "
        f"({active.status.value!r}). Phase 3's post-physics stop check "
        f"depends on `controller.ops.state == RUNNING_MOVING` "
        f"(train_controller.py:1756); Phase 2 already transitioned the "
        f"state to {controller.ops.state.value!r} before Phase 3 runs, so "
        f"`try_complete_active_path()` never fires. Fix site: remove the "
        f"`self.state = RUNNING_AT_STATION` write at "
        f"train_controller.py:826-835 and let Phase 1 "
        f"`enter_station_dwell()` own the transition."
    )


# --------------------------------------------------------------------------- #
# F4: path-completion never fires (consequence test, distinct contract refs)
# --------------------------------------------------------------------------- #


def test_F4_path_completes_on_arrival_at_terminal():
    """design_new.md "Active-path completion procedure" steps `8df5`/`bc55`
    /`790a`/`09b0`: once the train is fully stopped at the usable end of
    the live authority, Phase 3 must call
    `PathDispatcher.try_complete_active_path(train_id)`, the path's status
    must transition to `COMPLETED`, and `received_path_plan` must return
    `None` thereafter.

    Currently the active path stays `ACTIVE` forever because the
    Phase-2-owned RUNNING_AT_STATION write closes the state gate before
    Phase 3 reaches the completion check (see test_F3 above).
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
# F5: one-tick (RUNNING_AT_STATION, active path) window
# --------------------------------------------------------------------------- #


def test_F5_at_station_implies_no_active_path():
    """design_new.md ~line 1632 (and the existing
    `at_station_implies_no_path` invariant in tests/journey/invariants.py)
    requires: `state == RUNNING_AT_STATION` implies
    `received_path_plan is None`.

    The current implementation violates this for at least one tick — the
    tick where Phase 2's `_is_at_station()` flips the state but Phase 3
    has not yet completed the path. We drive the demo to that tick and
    catch the violation.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        network, _path, sim_controller, _persistence = bootstrap_runtime(
            demo=True, setup_train=True
        )
    train_id = "DEMO_TRAIN"
    controller = sim_controller.get_or_create_controller(train_id)
    dispatcher = sim_controller.path_dispatcher
    dt = 0.1
    violation = None
    for tick in range(1, 2001):
        sim_controller.update(dt)
        if controller.ops.state == TrainState.RUNNING_AT_STATION:
            active = dispatcher.get_active_path(train_id)
            if active is not None:
                violation = (tick, active.path_id, active.status.value)
                break
        if tick > 200 and controller.physics_sync.current_speed_kmh <= 0.05 and controller.ops.state == TrainState.READY:
            # Past the stop without ever observing the bug
            break
    if violation is not None:
        tick, pid, status = violation
        raise AssertionError(
            f"tick={tick}: controller.ops.state == RUNNING_AT_STATION while "
            f"active path {pid!r} is still {status!r}. Per design_new.md, "
            f"this combination must not be observable from outside a "
            f"single Phase-3 commit window."
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
