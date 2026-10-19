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
import signal
import socket

import pytest

import shutdown


def _never_called(*args, **kwargs):
    raise AssertionError(f"should not have been called: {args} {kwargs}")


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


# --- the device stays open until the safe state is written ----------------


@pytest.fixture
def isolated_hardware(tmp_path, monkeypatch):
    """A Virtual Hardware setup on temp files, reset before and after."""
    import hardware_config
    import operation_environment

    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    hardware_config.reset_to_defaults()
    yield hardware_config
    hardware_config.reset_to_defaults()


def test_finalize_closes_the_device_after_the_safe_state_not_before(
    isolated_hardware, monkeypatch
):
    from hardware.fake import FakeCh347Device

    device = FakeCh347Device()
    device.open()
    monkeypatch.setattr(isolated_hardware, "_live_device", device)
    open_during_safe_state = []

    class Sim:
        def enter_safe_state(self):
            open_during_safe_state.append(not device.closed)

    shutdown._finalize(Sim(), FakeState())

    assert open_during_safe_state == [True]
    assert device.closed is True
    assert isolated_hardware.get_live_device() is None


@pytest.mark.parametrize("activated_after_boot", [False, True])
def test_sigint_safes_the_outputs_of_a_session_activated_after_boot(
    isolated_hardware, monkeypatch, tmp_path, activated_after_boot
):
    """The 2026-10-16 rig run: Ctrl-C reported `safe_state_applied: false`,
    every chip refusing with "the CH347 is not open".

    Each device open used to register its own `atexit` close. One opened at
    boot registered before `_finalize`, so it closed last; one opened by an
    activation after boot registered after `_finalize`, and atexit runs the
    newest hook first -- so the device was closed before the safe state was
    written through it.

    This boots a Virtual Hardware session the way `server.main` does, activates
    it again through the real route, then runs every exit hook registered along
    the way in atexit's order. The fake refuses the bus once closed, as the
    real `Ch347Device` does.
    """
    import atexit
    import threading
    from datetime import datetime

    import operation_environment
    from app_factory import create_app
    from app_state import AppState
    from block import Block
    from enums import OperationMode
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from persistence import PersistenceManager
    from simulation import SimulationController

    exit_hooks = []
    monkeypatch.setattr(
        atexit, "register", lambda fn, *a, **k: exit_hooks.append((fn, a, k))
    )
    monkeypatch.setattr(shutdown, "_install_signal_handlers", lambda: None)

    hardware_config = isolated_hardware
    hardware_config.apply_updates(
        {
            "controller": {"kind": "ch347", "device_port": None},
            "chips": [
                {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
                {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
                {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
                {"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"},
            ],
            "block_track": {"B1": {"chip_id": "pwm1", "forward": 0, "reverse": 1}},
            "switch_output": {},
            "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
            "block_occupancy_current": {
                "B1": {"chip_id": "cur1", "port": 0, "threshold_ma": 15.0},
            },
        },
        persist=False,
    )
    operation_environment.select(OperationMode.VIRTUAL_HARDWARE, persist=False)

    # Boot: the session's device opens before shutdown reporting is installed.
    network = RailwayNetwork()
    network.blocks["B1"] = Block(block_id="B1")
    dispatcher = PathDispatcher(network)
    interfaces = hardware_config.build_session_interfaces(
        environment=OperationMode.VIRTUAL_HARDWARE,
        track_polarity_of=hardware_config.track_polarity_reversed,
    )
    sim = SimulationController(
        network,
        dispatcher,
        operation_mode=OperationMode.VIRTUAL_HARDWARE,
        input_interface=interfaces[0],
        block_output_interface=interfaces[1],
        switch_output_interface=interfaces[2],
        environment_active=True,
        activation_issues=[],
    )
    state = AppState(
        network=network,
        path_dispatcher=dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    shutdown.install_shutdown_reporting(sim, state=state, port=None)

    if activated_after_boot:
        app = create_app(state)
        app.config["TESTING"] = True
        response = app.test_client().post("/api/operation/activate")
        assert response.status_code == 200, response.get_json()
    device = hardware_config.get_live_device()
    assert device is not None and not device.closed

    # Ctrl-C: the signal handler exits, and atexit runs newest-first.
    monkeypatch.setattr(shutdown, "_caught_signal", signal.SIGINT)
    for fn, args, kwargs in reversed(exit_hooks):
        fn(*args, **kwargs)

    record = shutdown.read_last_shutdown()
    assert record["safe_state_error"] is None
    assert record["safe_state_applied"] is True
    assert record["signal_name"] == "SIGINT"
    assert device.closed is True
    assert hardware_config.get_live_device() is None


# --- port takeover --------------------------------------------------------

# One lsof -F block per process: `p` opens it, `R`/`c`/`f` describe it. The
# supervisor (2200) and the child that inherited its listening fd (2201) both
# appear -- that pairing is the whole reason _signal_targets exists.
LSOF_SUPERVISOR_AND_CHILD = "p2200\nR1\ncpython3.12\nf3\np2201\nR2200\ncpython3.12\nf3\nf6\n"


def _fake_capture(lsof_output):
    def capture(cmd, timeout=5.0):
        return f"argv of {cmd[2]}" if cmd[0] == "ps" else lsof_output

    return capture


def test_port_listeners_parses_lsof_and_annotates_with_argv(monkeypatch):
    monkeypatch.setattr(shutdown, "_capture", _fake_capture(LSOF_SUPERVISOR_AND_CHILD))

    listeners = shutdown._port_listeners(5001)

    assert [i["pid"] for i in listeners] == [2200, 2201]
    assert [i["ppid"] for i in listeners] == [1, 2200]
    assert listeners[0]["argv"] == "argv of 2200"


def test_only_the_ancestor_is_signalled_not_the_fd_inheriting_child(monkeypatch):
    """SIGTERM to the child would race the supervisor that is trying to restart
    it; SIGTERM to the supervisor drains the child and releases the port."""
    monkeypatch.setattr(shutdown, "_capture", _fake_capture(LSOF_SUPERVISOR_AND_CHILD))

    targets = shutdown._signal_targets(shutdown._port_listeners(5001))

    assert [i["pid"] for i in targets] == [2200]


def test_takeover_signals_nothing_without_a_terminal_to_ask(monkeypatch):
    """A backgrounded or supervised boot has no one to answer, so the other
    process must survive untouched -- the boot just fails as it always did."""
    monkeypatch.setattr(shutdown, "_capture", _fake_capture(LSOF_SUPERVISOR_AND_CHILD))
    monkeypatch.setattr(shutdown, "_can_prompt", lambda: False)
    monkeypatch.setattr(shutdown, "_stop_listeners", _never_called)

    assert shutdown.offer_port_takeover("127.0.0.1", 5001, log=lambda _m: None) is False


def test_takeover_signals_nothing_when_the_answer_is_no(monkeypatch):
    monkeypatch.setattr(shutdown, "_capture", _fake_capture(LSOF_SUPERVISOR_AND_CHILD))
    monkeypatch.setattr(shutdown, "_can_prompt", lambda: True)
    monkeypatch.setattr(shutdown, "_ask_yes_no", lambda question: False)
    monkeypatch.setattr(shutdown, "_stop_listeners", _never_called)

    assert shutdown.offer_port_takeover("127.0.0.1", 5001, log=lambda _m: None) is False


def test_takeover_declines_when_the_holder_cannot_be_identified(monkeypatch):
    """No lsof, or a holder owned by another user: there is nothing to offer."""
    monkeypatch.setattr(shutdown, "_capture", _fake_capture(""))
    monkeypatch.setattr(shutdown, "_can_prompt", lambda: True)
    monkeypatch.setattr(shutdown, "_ask_yes_no", _never_called)

    assert shutdown.offer_port_takeover("127.0.0.1", 5001, log=lambda _m: None) is False


def test_takeover_sigterms_the_target_and_reports_the_port_free(monkeypatch):
    signalled = []
    monkeypatch.setattr(shutdown, "_capture", _fake_capture(LSOF_SUPERVISOR_AND_CHILD))
    monkeypatch.setattr(shutdown, "_can_prompt", lambda: True)
    monkeypatch.setattr(shutdown, "_ask_yes_no", lambda question: True)
    monkeypatch.setattr(shutdown, "_wait_for_port", lambda host, port, timeout: True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    assert shutdown.offer_port_takeover("127.0.0.1", 5001, log=lambda _m: None) is True
    assert signalled == [(2200, signal.SIGTERM)]


def test_force_kill_takes_every_listener_not_just_the_ancestor(monkeypatch):
    """Nothing is being safed at this point, and killing only the parent would
    strand the child on the port with no one left to ask about it."""
    signalled = []
    monkeypatch.setattr(shutdown, "_capture", _fake_capture(LSOF_SUPERVISOR_AND_CHILD))
    monkeypatch.setattr(shutdown, "_can_prompt", lambda: True)
    monkeypatch.setattr(shutdown, "_ask_yes_no", lambda question: True)
    # SIGTERM leaves the port held; the retry after SIGKILL finds it free.
    waits = iter([False, True])
    monkeypatch.setattr(shutdown, "_wait_for_port", lambda h, p, t: next(waits))
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    assert shutdown.offer_port_takeover("127.0.0.1", 5001, log=lambda _m: None) is True
    assert signalled == [
        (2200, signal.SIGTERM),
        (2200, signal.SIGKILL),
        (2201, signal.SIGKILL),
    ]


def test_ensure_port_available_asks_nobody_when_the_port_is_free(monkeypatch):
    monkeypatch.setattr(shutdown, "_port_is_free", lambda host, port: True)
    monkeypatch.setattr(shutdown, "offer_port_takeover", _never_called)

    assert shutdown.ensure_port_available("127.0.0.1", 5001) is True


def test_a_free_port_is_free_and_a_bound_one_is_not():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert shutdown._port_is_free("127.0.0.1", port) is False
    assert shutdown._port_is_free("127.0.0.1", port) is True


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
