"""An operator switch throw is refused while the session is Halted for
`runtime_failure`, and accepted while Halted for `layout_emergency_cut_out`
(design_new.md `PUT /api/switches/<switch_id>/state`; "Session state").

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_switch_throw_runtime_halt.py -q``
"""

from __future__ import annotations

import contextlib
import io
import threading
from datetime import datetime

import pytest

from app_state import AppState
from bootstrap import bootstrap_runtime
from services import switch_service

pytestmark = pytest.mark.scene("demo_main")


@pytest.fixture
def state():
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, persistence = bootstrap_runtime(
            demo=True, setup_train=True
        )
    return AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=persistence,
        simulation_lock=threading.Lock(),
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


def _record(monkeypatch, state):
    calls, results = [], []
    monkeypatch.setattr(
        state.network.switch_manager,
        "request_position_change",
        lambda *args, **kwargs: calls.append(args) or (True, "ok"),
    )
    monkeypatch.setattr(
        switch_service,
        "_emit_command_result",
        lambda _state, _sid, _cid, success, message: results.append((success, message)),
    )
    return calls, results


def test_a_throw_is_refused_while_halted_for_runtime_failure(state, monkeypatch):
    switch_id = next(iter(state.network.switches))
    calls, results = _record(monkeypatch, state)
    state.sim_controller.failed_runtime = True

    switch_service.request_position_change(state, switch_id, new_state=1)

    assert calls == []
    assert results == [(False, "runtime halted")]


def test_a_throw_is_accepted_while_halted_for_the_layout_cut_out(state, monkeypatch):
    switch_id = next(iter(state.network.switches))
    calls, results = _record(monkeypatch, state)
    state.sim_controller.layout_cut_out_active = True

    switch_service.request_position_change(state, switch_id, new_state=1)

    assert len(calls) == 1
    assert results == [(True, "ok")]
