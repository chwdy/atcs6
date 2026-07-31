"""Shutdown reporting and the loss-of-control fail-safe (shutdown.py).

The behaviour under test is the one that used to be missing: `atexit` does not
run when a process is killed by a signal, so `enter_safe_state()` never fired on
the shutdown path this server actually takes. These tests pin the three pieces
that close that gap -- the safe-state call happening on every observable exit,
the record that says whether it succeeded, and the run marker that is the only
evidence a SIGKILL leaves behind.
"""

import json
import os

import pytest

import shutdown


@pytest.fixture(autouse=True)
def isolated_runtime_dir(tmp_path, monkeypatch):
    """Never touch the developer's real .runtime/ from a test run."""
    monkeypatch.setattr(shutdown, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(shutdown, "RUN_MARKER_PATH", tmp_path / "server_run.json")
    monkeypatch.setattr(shutdown, "LAST_SHUTDOWN_PATH", tmp_path / "last_shutdown.json")
    # A test must not spawn the heartbeat thread; it outlives the test and
    # would rewrite the marker mid-assertion.
    monkeypatch.setattr(shutdown, "_start_heartbeat", lambda port: None)
    monkeypatch.setattr(shutdown, "_caught_signal", None)
    monkeypatch.setattr(shutdown, "_reported", False)
    yield


class FakeSim:
    """Stands in for SimulationController's enter_safe_state contract."""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def enter_safe_state(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("USB write failed")


class FakeState:
    def __init__(self, running=True, trains=None):
        self.simulation_running = running
        self.network = type("N", (), {"trains": trains or {}})()


# --- run marker -----------------------------------------------------------


def test_first_boot_reports_no_previous_run():
    assert shutdown.claim_run_marker(port=5001) is None
    assert shutdown.RUN_MARKER_PATH.exists()


def test_clean_exit_releases_marker_so_next_boot_is_quiet():
    shutdown.claim_run_marker(port=5001)
    shutdown._release_run_marker()
    assert not shutdown.RUN_MARKER_PATH.exists()
    assert shutdown.claim_run_marker(port=5001) is None


def test_leftover_marker_from_a_dead_pid_reports_abnormal_termination(monkeypatch):
    """The SIGKILL case: the process never ran anything, so the marker it left
    behind is the whole story."""
    shutdown.claim_run_marker(port=5001)
    marker = json.loads(shutdown.RUN_MARKER_PATH.read_text())
    marker["pid"] = 999999  # a pid that is not running
    marker["boot_id"] = "deadbeef"
    shutdown.RUN_MARKER_PATH.write_text(json.dumps(marker))
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid: False)

    previous = shutdown.claim_run_marker(port=5001)

    assert previous is not None
    assert previous["clean"] is False
    assert previous["safe_state_applied"] is False
    assert previous["reason"] == "abnormal_termination"
    # And it is persisted for /api/server/shutdown-report.
    assert shutdown.read_last_shutdown()["clean"] is False


def test_marker_owned_by_a_live_pid_is_a_second_server_not_a_crash(monkeypatch):
    """Two servers on one checkout must not make each other report a crash."""
    shutdown.claim_run_marker(port=5001)
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid: True)

    assert shutdown.claim_run_marker(port=5002) is None


def test_pid_alive_is_true_for_this_process_and_false_for_a_dead_one():
    assert shutdown._pid_alive(os.getpid()) is True
    assert shutdown._pid_alive(999999) is False
    assert shutdown._pid_alive(None) is False


# --- the fail-safe itself -------------------------------------------------


def test_finalize_drives_outputs_to_safe_state_and_records_success():
    sim = FakeSim()
    shutdown.claim_run_marker(port=5001)

    shutdown._finalize(sim, FakeState(running=True, trains={"T1": object()}))

    assert sim.calls == 1
    record = shutdown.read_last_shutdown()
    assert record["safe_state_applied"] is True
    assert record["clean"] is True
    assert record["simulation"] == {"running": True, "train_count": 1}
    # The marker is gone, so the next boot knows this exit was deliberate.
    assert not shutdown.RUN_MARKER_PATH.exists()


def test_finalize_records_a_failed_safe_state_instead_of_swallowing_it():
    """A hardware write that fails during shutdown is the single most important
    thing to report -- the track may still be energized."""
    sim = FakeSim(fail=True)

    shutdown._finalize(sim, FakeState())

    record = shutdown.read_last_shutdown()
    assert record["safe_state_applied"] is False
    assert "USB write failed" in record["safe_state_error"]


def test_finalize_runs_once_even_if_atexit_fires_twice():
    sim = FakeSim()
    shutdown._finalize(sim, FakeState())
    shutdown._finalize(sim, FakeState())
    assert sim.calls == 1


def test_caught_signal_is_named_in_the_record():
    import signal as signal_mod

    shutdown._caught_signal = signal_mod.SIGTERM
    try:
        shutdown._finalize(FakeSim(), FakeState())
    finally:
        shutdown._caught_signal = None

    record = shutdown.read_last_shutdown()
    assert record["signal_name"] == "SIGTERM"
    assert record["reason"] == "signal"


def test_supervisor_stamps_the_signal_onto_the_childs_record():
    """The reloader child cannot name its own signal -- werkzeug replaces our
    handler in that process -- so the supervisor fills it in afterwards."""
    import signal as signal_mod

    shutdown._finalize(FakeSim(), FakeState())
    assert shutdown.read_last_shutdown()["signal_name"] is None

    shutdown._annotate_record(signal_mod.SIGTERM, exit_code=0)

    record = shutdown.read_last_shutdown()
    assert record["signal_name"] == "SIGTERM"
    assert record["initiator"] == "supervisor"
    assert record["child_exit_code"] == 0


# --- record durability ----------------------------------------------------


def test_a_truncated_record_does_not_break_the_next_boot():
    """The record is written by a process that is dying; the reader must treat
    a half-written file as 'no record', not as a crash."""
    shutdown.LAST_SHUTDOWN_PATH.write_text('{"reason": "sig')
    assert shutdown.read_last_shutdown() is None


def test_writes_are_atomic_leaving_no_partial_file_behind():
    shutdown._write_json(shutdown.LAST_SHUTDOWN_PATH, {"a": 1})
    assert json.loads(shutdown.LAST_SHUTDOWN_PATH.read_text()) == {"a": 1}
    leftovers = list(shutdown.RUNTIME_DIR.glob("*.tmp"))
    assert leftovers == []
