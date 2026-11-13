"""The session state and the pass the runtime loop runs for it.

design_v2.md "Session state": the session reads Setup, Live or Halted with a
reason, derived from stored flags in a fixed precedence order. "Runtime loop",
Which pass runs: Live runs the whole runtime step; Setup runs the stopped pass;
Halted runs no pass; every iteration consumes a watchdog trip.

"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app_state import AppState
from enums import HaltReason, OperationMode, SessionStateValue
from runtime import PASS_STOPPED, PASS_TICK, PASS_WATCHDOG, runtime_pass
from session_state import derive_session_state


def _sim(**flags):
    base = dict(
        hardware_failure=False,
        environment_active=True,
        operation_mode=OperationMode.VIRTUAL_HARDWARE,
        occupancy_is_real=True,
    )
    base.update(flags)
    return SimpleNamespace(**base)


def _derive(sim, *, setup_active=False):
    return derive_session_state(sim, setup_active=setup_active)


def test_live_when_no_flag_holds():
    state = _derive(_sim())
    assert state.value is SessionStateValue.LIVE
    assert state.halt_reason is None
    assert state.is_live


def test_setup_outranks_a_hardware_failure():
    state = _derive(
        _sim(hardware_failure=True, environment_active=False), setup_active=True
    )
    assert state.value is SessionStateValue.SETUP


def test_a_hardware_failure_names_the_cause_of_an_inactive_environment():
    state = _derive(_sim(hardware_failure=True, environment_active=False))
    assert state.halt_reason is HaltReason.HARDWARE_FAILURE


def test_an_inactive_environment_without_a_failure_is_not_activated():
    state = _derive(_sim(environment_active=False))
    assert state.halt_reason is HaltReason.NOT_ACTIVATED


def test_to_dict():
    state = _derive(_sim(environment_active=False))
    assert state.to_dict() == {"value": "halted", "halt_reason": "not_activated"}


def _app_state(sim, *, setup=False):
    state = AppState(
        network=None,
        sim_controller=sim,
        persistence_manager=None,
        simulation_lock=threading.Lock(),
    )
    state.setup_active = setup
    return state


@pytest.mark.parametrize(
    "flags,setup,expected",
    [
        ({}, False, PASS_TICK),  # Live
        ({"hardware_failure": True, "environment_active": False}, False, PASS_WATCHDOG),
        ({"environment_active": False}, False, PASS_WATCHDOG),  # not activated
        ({"environment_active": False}, True, PASS_STOPPED),  # Setup
        # Setup with a hardware failure latched.
        ({"environment_active": False, "hardware_failure": True}, True, PASS_WATCHDOG),
        # Setup with no detector interface open.
        ({"environment_active": False, "occupancy_is_real": False}, True, PASS_WATCHDOG),
    ],
)
def test_runtime_pass(flags, setup, expected):
    assert runtime_pass(_app_state(_sim(**flags), setup=setup)) == expected


def _recording_sim(**flags):
    sim = _sim(**flags)
    sim.calls = []
    sim.consume_watchdog_trip = lambda: sim.calls.append("consume_watchdog_trip")
    sim.note_session_live = lambda live: sim.calls.append(("note_session_live", live))
    sim.run_train_step = lambda live: sim.calls.append(("run_train_step", live))
    return sim


@pytest.mark.parametrize(
    "flags,setup,step",
    [
        ({}, False, [("run_train_step", True)]),  # Live: the whole step
        ({"environment_active": False}, True, [("run_train_step", False)]),  # Setup
        ({"environment_active": False}, False, []),  # not activated: no pass
        ({"hardware_failure": True, "environment_active": False}, False, []),
    ],
)
def test_every_iteration_consumes_a_watchdog_trip_first(flags, setup, step):
    """design_v2.md "Runtime loop", Every time round consumes a watchdog trip,
    even when no pass runs."""
    from runtime import run_loop_iteration

    sim = _recording_sim(**flags)
    run_loop_iteration(_app_state(sim, setup=setup))
    live = not flags and not setup
    assert sim.calls == ["consume_watchdog_trip", ("note_session_live", live), *step]
