"""The session state and the pass the runtime loop runs for it.

design_new.md "Session state": the session reads Setup, Live, Paused, or Halted
with a reason, derived from six stored flags in a fixed precedence order.
"Running and stopped", Which pass runs: Live runs the whole tick; Setup and a
cut-out halt in Virtual/Real Hardware run the stopped pass; a failure halt only
consumes a watchdog trip; everything else runs no pass.
"""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from app_state import AppState
from enums import HaltReason, OperationMode, SessionStateValue
from runtime import PASS_STOPPED, PASS_TICK, PASS_WATCHDOG, runtime_pass
from session_state import derive_session_state


def _sim(**flags):
    base = dict(
        failed_runtime=False,
        hardware_failure=False,
        environment_active=True,
        layout_cut_out_active=False,
        operation_mode=OperationMode.SIMULATION,
    )
    base.update(flags)
    return SimpleNamespace(**base)


def _derive(sim, *, setup_active=False, simulation_running=True):
    return derive_session_state(
        sim, setup_active=setup_active, simulation_running=simulation_running
    )


def test_live_when_no_flag_holds():
    state = _derive(_sim())
    assert state.value is SessionStateValue.LIVE
    assert state.halt_reason is None
    assert state.is_live


def test_paused_when_play_is_off():
    assert _derive(_sim(), simulation_running=False).value is SessionStateValue.PAUSED


def test_a_runtime_failure_outranks_everything():
    state = _derive(
        _sim(failed_runtime=True, environment_active=False, layout_cut_out_active=True),
        setup_active=True,
    )
    assert (state.value, state.halt_reason) == (
        SessionStateValue.HALTED,
        HaltReason.RUNTIME_FAILURE,
    )


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


def test_a_hardware_failure_does_not_halt_an_active_simulation():
    state = _derive(_sim(hardware_failure=True))
    assert state.value is SessionStateValue.LIVE


def test_the_cut_out_flag_halts_an_active_session():
    state = _derive(_sim(layout_cut_out_active=True), simulation_running=False)
    assert state.halt_reason is HaltReason.LAYOUT_EMERGENCY_CUT_OUT


def test_to_dict():
    state = _derive(_sim(environment_active=False))
    assert state.to_dict() == {"value": "halted", "halt_reason": "not_activated"}


def _app_state(sim, *, running=True, setup=False):
    state = AppState(
        network=None,
        path_dispatcher=None,
        sim_controller=sim,
        persistence_manager=None,
        simulation_lock=threading.Lock(),
        simulation_running=running,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    state.setup_active = setup
    return state


@pytest.mark.parametrize(
    "flags,running,setup,expected",
    [
        ({}, True, False, PASS_TICK),
        ({}, False, False, None),  # Simulation paused
        ({"layout_cut_out_active": True}, True, False, None),  # cut-out in Simulation
        ({"failed_runtime": True}, True, False, PASS_WATCHDOG),
        (
            {"hardware_failure": True, "environment_active": False,
             "operation_mode": OperationMode.REAL_HARDWARE},
            True, False, PASS_WATCHDOG,
        ),
        (
            {"environment_active": False, "operation_mode": OperationMode.REAL_HARDWARE},
            True, False, None,
        ),  # not activated
        (
            {"layout_cut_out_active": True, "operation_mode": OperationMode.REAL_HARDWARE},
            True, False, PASS_STOPPED,
        ),
        (
            {"environment_active": False, "operation_mode": OperationMode.VIRTUAL_HARDWARE},
            False, True, PASS_STOPPED,
        ),  # Setup
        (
            {"environment_active": False, "hardware_failure": True,
             "operation_mode": OperationMode.VIRTUAL_HARDWARE},
            False, True, None,
        ),  # Setup with a hardware failure latched
    ],
)
def test_runtime_pass(flags, running, setup, expected):
    assert runtime_pass(_app_state(_sim(**flags), running=running, setup=setup)) == expected


def test_a_halt_writes_no_play_pause_bit():
    """The halts write only their own latch; `simulation_running` stays the
    Play/Pause bit, so clearing a runtime failure returns to the prior state."""
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from runtime_payloads import FatalConflictKind, FatalPhysicalEntryConflict
    from simulation import SimulationController

    network = RailwayNetwork()
    sim = SimulationController(network, PathDispatcher(network))
    state = _app_state(sim, running=True)

    sim.enter_failed_runtime(
        FatalPhysicalEntryConflict(
            resource_type="block",
            resource_id="B",
            incoming_train_id="T1",
            prior_owner_train_id="T2",
            conflict_kind=FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN,
        )
    )
    assert state.simulation_running is True
    assert state.session_state().halt_reason is HaltReason.RUNTIME_FAILURE

    sim.clear_failed_runtime()
    assert state.session_state().is_live


def _recording_sim(**flags):
    sim = _sim(**flags)
    sim.calls = []
    sim.consume_watchdog_trip = lambda: sim.calls.append("consume_watchdog_trip")
    sim.poll_hardware_occupancy = lambda: sim.calls.append("poll_hardware_occupancy")
    sim.mark_no_pass = lambda: sim.calls.append("mark_no_pass")
    sim.update = lambda *a, **k: sim.calls.append("update")
    return sim


@pytest.mark.parametrize(
    "flags,running,setup",
    [
        # Stopped pass: the cut-out halt in Real Hardware.
        ({"layout_cut_out_active": True, "operation_mode": OperationMode.REAL_HARDWARE}, True, False),
        # Failure halt.
        ({"failed_runtime": True}, True, False),
        # No pass: Simulation paused.
        ({}, False, False),
    ],
)
def test_every_iteration_without_a_running_pass_consumes_a_watchdog_trip(flags, running, setup):
    """design_new.md "Trip-record consumption escalates a failed drop to
    hardware failure": the runtime loop consumes the trip record on every
    iteration, not only on a running pass."""
    from runtime import run_loop_iteration

    sim = _recording_sim(**flags)
    run_loop_iteration(_app_state(sim, running=running, setup=setup), 0.1)
    assert sim.calls[0] == "consume_watchdog_trip"


def test_a_running_pass_consumes_the_trip_inside_update():
    from runtime import run_loop_iteration

    sim = _recording_sim()
    run_loop_iteration(_app_state(sim, running=True, setup=False), 0.1)
    assert sim.calls == ["update"]
