"""One owner of the outputs, checked at the session's device handle
(design_v2.md "Hardware interface", Who may write the outputs).

Run with: ``/opt/homebrew/anaconda3/envs/general/bin/python -m pytest
tests/test_output_owner.py -q``
"""

import threading

import pytest

import hardware_config
import operation_environment
from enums import OperationMode
from hardware import device_handle as owners
from hardware.device_handle import (
    OutputNotOwned,
    OutputsOwned,
    SessionDeviceHandle,
)
from hardware.fake import FakeCh347Device
from hardware.interfaces import build_chip, build_real_interfaces
from hardware_config import ChipConfig, PortBinding


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    hardware_config.reset_to_defaults()
    yield
    from hardware import commission, occupancy_bench

    commission.get_session().close()
    occupancy_bench.get_session().close()
    hardware_config.reset_to_defaults()


PWM = ChipConfig("pwm1", "pca9685", i2c_address=0x40)
SW = ChipConfig("sw1", "hc595", spi_select="cs0", chain_length=1)
IN = ChipConfig("in1", "hc165", spi_select="cs1")
CUR = ChipConfig("cur1", "ina3221", i2c_address=0x41)


def _handle(refusals=None):
    device = FakeCh347Device(present_addresses={0x40, 0x41})
    device.open()

    def report(writer, output, owner):
        if refusals is not None:
            refusals.append((writer, output, owner))

    return SessionDeviceHandle(device, OperationMode.VIRTUAL_HARDWARE, on_refused=report), device


# --- the owner record -------------------------------------------------------


def test_the_outputs_start_with_no_owner_and_one_owner_at_a_time():
    handle, _device = _handle()
    assert handle.owner is None

    handle.take(owners.OCCUPANCY_BENCH)
    assert handle.owner == owners.OCCUPANCY_BENCH
    handle.take(owners.OCCUPANCY_BENCH)            # taking again changes nothing

    with pytest.raises(OutputsOwned) as refused:
        handle.take(owners.COMMISSIONING_SESSION)
    assert refused.value.owner == owners.OCCUPANCY_BENCH
    assert handle.owner == owners.OCCUPANCY_BENCH

    handle.give_back(owners.COMMISSIONING_SESSION)  # not the owner: nothing
    assert handle.owner == owners.OCCUPANCY_BENCH
    handle.give_back(owners.OCCUPANCY_BENCH)
    assert handle.owner is None


def test_closing_the_handle_leaves_nobody_owning_the_outputs():
    handle, device = _handle()
    handle.take(owners.RUNTIME)

    handle.close()

    assert handle.owner is None
    assert device.closed is True


# --- the refusal and its warning --------------------------------------------


def test_a_write_from_anyone_but_the_owner_reaches_no_chip():
    refusals = []
    handle, device = _handle(refusals)
    handle.take(owners.COMMISSIONING_SESSION)
    pwm = build_chip(handle.writer(owners.RUNTIME), PWM)

    with pytest.raises(OutputNotOwned) as refused:
        pwm.set_duty_cycle(3, 0.5)

    assert refused.value.writer == owners.RUNTIME
    assert device.duty_cycle(0x40, 3) == 0.0
    assert refusals == [(owners.RUNTIME, "I2C chip 0x40 register 0x12",
                         owners.COMMISSIONING_SESSION)]


def test_the_owners_write_reaches_the_chip():
    refusals = []
    handle, device = _handle(refusals)
    handle.take(owners.COMMISSIONING_SESSION)
    pwm = build_chip(handle.writer(owners.COMMISSIONING_SESSION), PWM)

    pwm.set_duty_cycle(3, 0.5)

    assert device.duty_cycle(0x40, 3) == pytest.approx(0.5, abs=0.01)
    assert refusals == []


def test_a_refused_shift_out_keeps_the_chain_as_it_was():
    refusals = []
    handle, device = _handle(refusals)
    chain = build_chip(handle.writer(owners.PORT_TEST_PULSE), SW)

    with pytest.raises(OutputNotOwned):
        chain.set_output(2, True)

    assert chain.is_high(2) is False
    assert device.latched_bit("cs0", 2) is False
    assert len(refusals) == 1
    assert refusals[0][1] == "shift-out chain on cs0"


def test_reads_have_no_owner():
    """A detector read, a register-pointer write before a read, and an input
    chip's configuration are not output writes (design_v2.md "Hardware
    interface", Reads have no owner)."""
    refusals = []
    handle, device = _handle(refusals)
    handle.take(owners.OCCUPANCY_BENCH)
    reader = handle.writer(owners.BLOCK_OCCUPANCY_TEST)
    device.set_input_bit("cs1", 4, True)

    assert build_chip(reader, IN).read_bit(4) is True
    assert build_chip(reader, PWM).read_duty_cycle(0) == 0.0
    current = build_chip(reader, CUR)
    current.configure()
    current.read_current(1)
    assert reader.i2c_scan() == [0x40, 0x41]
    assert refusals == []


def _runtime_interfaces(handle):
    return build_real_interfaces(
        handle.writer(owners.RUNTIME),
        [PWM, SW],
        {"B1": ("pwm1", 0, 1)},
        {},
        {"S1": ("sw1", 0, 1, None)},
    )


def test_the_safe_state_is_accepted_while_a_tool_owns_the_outputs():
    """design_v2.md "Hardware interface", The safe state is accepted from
    anyone — including from another thread, as the Real Hardware watchdog
    calls it."""
    refusals = []
    handle, device = _handle(refusals)
    handle.take(owners.RUNTIME)
    _inp, track, switch = _runtime_interfaces(handle)
    track.set_voltage("B1", 6.0)
    handle.give_back(owners.RUNTIME)
    handle.take(owners.OCCUPANCY_BENCH)
    bench = build_chip(handle.writer(owners.OCCUPANCY_BENCH), PWM)
    bench.set_duty_cycle(5, 0.4)

    errors = []

    def watchdog_drop():
        try:
            track.enter_safe_state()
            switch.enter_safe_state()
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=watchdog_drop)
    worker.start()
    worker.join(5)

    assert errors == []
    assert refusals == []
    assert device.duty_cycle(0x40, 0) == 0.0
    assert device.duty_cycle(0x40, 5) == 0.0
    assert handle.owner == owners.OCCUPANCY_BENCH


def test_a_refused_throw_pulses_no_coil_and_lowers_nothing():
    refusals = []
    handle, device = _handle(refusals)
    handle.take(owners.RUNTIME)
    _inp, _track, switch = _runtime_interfaces(handle)
    handle.give_back(owners.RUNTIME)
    handle.take(owners.COMMISSIONING_SESSION)

    with pytest.raises(OutputNotOwned):
        switch.set_throw("S1", 1)

    assert device.latched_bit("cs0", 1) is False
    assert switch.get_last_throw("S1") is None
    assert len(refusals) == 1          # the first write only; nothing to lower


def _lose_the_outputs_after(handle, latches):
    """Make the owner change once `latches` shift-outs have landed, so a later
    write of the same pulse is refused."""
    device = handle.device
    real = device.latch_out
    count = {"n": 0}

    def latch_out(data, select):
        real(data, select)
        count["n"] += 1
        if count["n"] == latches:
            handle._owner = owners.COMMISSIONING_SESSION

    device.latch_out = latch_out


def test_a_throw_refused_after_its_first_write_still_lowers_every_bit():
    """Once a pulse's on-write landed, its off-writes are a move to the safe
    state and are never refused (design_v2.md "Hardware interface", The safe
    state is accepted from anyone)."""
    refusals = []
    handle, device = _handle(refusals)
    handle.take(owners.RUNTIME)
    _inp, _track, switch = build_real_interfaces(
        handle.writer(owners.RUNTIME), [SW], {}, {}, {"S1": ("sw1", 0, 1, 2)},
    )
    _lose_the_outputs_after(handle, 1)          # standby high lands, then refused

    with pytest.raises(OutputNotOwned):
        switch.set_throw("S1", 1)

    assert device.latched_bit("cs0", 2) is False   # standby
    assert device.latched_bit("cs0", 1) is False   # coil
    assert len(refusals) == 1


def test_a_commissioning_pulse_lowers_the_coil_when_lowering_standby_fails():
    from hardware.commission import CommissionSession
    from hardware.setup_tools import SetupToolWriteFailed
    import hardware.commission as commission_mod

    handle, device = _handle()
    cfg = hardware_config.HardwareConfig(chips=[SW])
    session = CommissionSession()
    session.open(cfg, handle)
    real = device.latch_out
    calls = {"n": 0}

    def latch_out(data, select):
        calls["n"] += 1
        if calls["n"] == 3:                     # standby low
            raise OSError("bus error")
        real(data, select)

    device.latch_out = latch_out
    sleep = commission_mod.sleep_holding
    commission_mod.sleep_holding = lambda chip, secs: None
    try:
        with pytest.raises(SetupToolWriteFailed):
            session.pulse_output("sw1", 1, standby_port=2, pulse_s=0.0)
    finally:
        commission_mod.sleep_holding = sleep

    assert device.latched_bit("cs0", 1) is False   # the coil still went low
    assert session.is_open() is False               # its own write failed: it ended
    assert handle.owner is None


def test_reopening_the_commissioning_session_zeroes_the_chips_it_drops():
    """Open with one setup, drive a channel, reopen with a setup that lacks
    that chip: the dropped chip is zeroed first, and the session keeps the
    outputs across the rebuild."""
    from hardware.commission import CommissionSession

    handle, device = _handle()
    session = CommissionSession()
    session.open(hardware_config.HardwareConfig(chips=[PWM]), handle)
    session.set_output("pwm1", 4, 0.5)
    assert device.duty_cycle(0x40, 4) > 0

    session.open(hardware_config.HardwareConfig(chips=[SW]), handle)

    assert device.duty_cycle(0x40, 4) == 0.0
    assert handle.owner == owners.COMMISSIONING_SESSION
    session.close()


def test_a_failed_throw_at_activation_fails_it_when_the_latch_was_already_set():
    """Activating out of Halted for `hardware_failure`: a throw at step 6 that
    cannot be issued fails the activation (design_v2.md "Operating environment
    and activation", A refused or failed activation leaves no environment
    active): the safe state, no environment active, the runtime gives up the
    outputs, and the latch stays set."""
    from network import RailwayNetwork
    from simulation import SimulationController
    from switch import Switch

    handle, device = _handle()
    hardware_config._handle = handle
    network = RailwayNetwork()
    network.switches["S1"] = Switch(switch_id="S1", x=0, y=0)
    sim = SimulationController(network)
    sim.hardware_failure = True                  # Halted for hardware_failure
    handle.take(owners.RUNTIME)
    sim.activate_interfaces(OperationMode.VIRTUAL_HARDWARE, *_runtime_interfaces(handle))
    device.set_fault("shift_write_error")

    assert sim.finish_activation() is False

    assert sim.environment_active is False
    assert sim.hardware_failure is True
    assert handle.owner is None
    failure = sim.notification_center.latest_by_operation("enter_hardware_failure")
    assert failure is not None and failure.payload["component"] == "switch_output"


# --- through the runtime -----------------------------------------------------


def _runtime_on(handle):
    from network import RailwayNetwork
    from simulation import SimulationController
    from block import Block

    network = RailwayNetwork()
    network.blocks["B1"] = Block(block_id="B1")
    sim = SimulationController(network)
    handle.on_refused = sim.report_output_not_owned
    handle.take(owners.RUNTIME)
    sim.activate_interfaces(OperationMode.VIRTUAL_HARDWARE, *_runtime_interfaces(handle))
    return sim


def test_a_refused_voltage_records_nothing_and_is_not_a_hardware_failure():
    """design_v2.md "Hardware interface", A refused write changes nothing: no
    voltage recorded, no hardware I/O failure, and one `output_not_owned`
    warning naming the writer and the output."""
    handle, device = _handle()
    sim = _runtime_on(handle)
    handle.give_back(owners.RUNTIME)
    handle.take(owners.OCCUPANCY_BENCH)

    assert sim.power_controller.set_block_voltage("B1", 6.0) is False

    assert device.duty_cycle(0x40, 0) == 0.0
    assert sim.power_controller.get_recorded_voltage("B1") == 0.0
    assert sim.hardware_failure is False
    warnings = [
        e for e in sim.notification_center.peek_recent(200)
        if e.error is not None and e.error.origin == "output_not_owned"
    ]
    assert len(warnings) == 1
    assert warnings[0].kind == "warning"
    assert warnings[0].payload["writer"] == owners.RUNTIME
    assert warnings[0].payload["owner"] == owners.OCCUPANCY_BENCH
    assert warnings[0].payload["output"].startswith("I2C chip 0x40")


def test_a_refused_throw_records_no_position_and_is_not_a_hardware_failure():
    handle, _device = _handle()
    sim = _runtime_on(handle)
    handle.give_back(owners.RUNTIME)
    handle.take(owners.COMMISSIONING_SESSION)

    assert sim.switch_power_controller.set_throw("S1", 1) is False
    assert sim.hardware_failure is False


def test_leaving_live_through_a_hardware_failure_takes_the_outputs_from_the_runtime(
    monkeypatch,
):
    handle, _device = _handle()
    monkeypatch.setattr(hardware_config, "_handle", handle)
    sim = _runtime_on(handle)
    assert handle.owner == owners.RUNTIME
    from runtime_payloads import HardwareFailure

    sim.enter_hardware_failure(
        HardwareFailure(component="block_occupancy", operation="read", message="x")
    )

    assert handle.owner is None


def test_a_hardware_failure_leaves_a_tool_that_owns_the_outputs_owning_them(monkeypatch):
    """design_v2.md "Hardware interface", A failure the tool did not cause
    leaves it open: the safe state is driven and the tool stays the owner."""
    handle, device = _handle()
    monkeypatch.setattr(hardware_config, "_handle", handle)
    sim = _runtime_on(handle)
    sim.deactivate_environment([])
    handle.take(owners.OCCUPANCY_BENCH)
    bench = build_chip(handle.writer(owners.OCCUPANCY_BENCH), PWM)
    bench.set_duty_cycle(0, 0.5)
    from runtime_payloads import HardwareFailure

    sim.enter_hardware_failure(
        HardwareFailure(component="block_occupancy", operation="read", message="x")
    )

    assert handle.owner == owners.OCCUPANCY_BENCH
    assert device.duty_cycle(0x40, 0) == 0.0       # the safe state got through


# --- the REST surface ---------------------------------------------------------


def _payload():
    return {
        "controller": {"kind": "ch347", "device_port": None},
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 1},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
            {"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"},
        ],
        "block_track": {"B1": {"chip_id": "pwm1", "forward": 0, "reverse": 1}},
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
        "block_occupancy_current": {"B1": {"chip_id": "cur1", "port": 0}},
    }


@pytest.fixture
def client():
    from app_factory import create_app
    from app_state import AppState
    from block import Block
    from network import RailwayNetwork
    from persistence import PersistenceManager
    from simulation import SimulationController

    network = RailwayNetwork()
    network.blocks["B1"] = Block(block_id="B1")
    sim_controller = SimulationController(network)
    state = AppState(
        network=network,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    hardware_config.apply_updates(_payload(), persist=False)
    with app.test_client() as test_client:
        yield test_client, state


def _pulse(test_client):
    return test_client.post("/api/hardware/port-test", json={
        "config": _payload(), "chip_id": "sw1", "port": 3,
        "action": "pulse_output", "pulse_s": 0.0,
    })


def test_a_port_test_pulse_is_refused_while_the_bench_is_open(client):
    test_client, _state = client
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True

    refused = _pulse(test_client)

    assert refused.status_code == 409
    body = refused.get_json()
    assert body["reason"] == "outputs_owned"
    assert "close it first" in body["error"]
    assert hardware_config.get_device_handle().owner == owners.OCCUPANCY_BENCH


def test_a_one_request_tool_holds_the_outputs_only_for_its_request(client):
    test_client, _state = client
    assert test_client.post("/api/operation/setup").status_code == 200

    assert _pulse(test_client).get_json()["ok"] is True

    assert hardware_config.get_device_handle().owner is None
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True


def test_open_and_close_tools_refuse_each_other(client):
    test_client, _state = client
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post(
        "/api/hardware/commission/open", json={"config": _payload()}
    ).get_json()["ok"] is True

    bench = test_client.post("/api/hardware/bench/open")
    track = test_client.post(
        "/api/hardware/track-test", json={"config": _payload(), "block_id": "B1"}
    )

    assert bench.status_code == 409 and bench.get_json()["reason"] == "outputs_owned"
    assert track.status_code == 409 and track.get_json()["reason"] == "outputs_owned"

    assert test_client.post("/api/hardware/commission/close").get_json()["ok"] is True
    assert hardware_config.get_device_handle().owner is None
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True
    commission = test_client.post("/api/hardware/commission/open", json={"config": _payload()})
    assert commission.status_code == 409
    assert commission.get_json()["reason"] == "outputs_owned"


def test_activation_gives_the_outputs_to_the_runtime_and_setup_takes_them_back(client):
    test_client, _state = client
    assert test_client.post("/api/operation/setup").status_code == 200
    setup_device = hardware_config.get_live_device()
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True

    assert test_client.post("/api/operation/activate").status_code == 200

    handle = hardware_config.get_device_handle()
    # Activation built its interfaces on the Setup handle (design_v2.md "Setup
    # tools", Activation uses the Setup handle), after closing the bench.
    assert handle.device is setup_device
    assert handle.owner == owners.RUNTIME

    assert test_client.post("/api/operation/setup").status_code == 200
    assert hardware_config.get_device_handle() is handle
    assert handle.owner is None


# --- a failure of a tool's own write ends the tool ---------------------------


def test_a_failed_commissioning_write_ends_the_session_as_a_hardware_failure(client):
    """design_v2.md "Hardware interface", A failure the tool did not cause
    leaves it open: a failure of the tool's own write ends the tool, and it is
    a hardware I/O failure."""
    from hardware import commission

    test_client, state = client
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post(
        "/api/hardware/commission/open", json={"config": _payload()}
    ).get_json()["ok"] is True
    hardware_config.get_live_device().set_fault("shift_write_error")

    body = test_client.post(
        "/api/hardware/commission/output", json={"chip_id": "sw1", "port": 3, "duty": 1.0}
    ).get_json()

    assert body["ok"] is False and body["tool_closed"] is True
    assert commission.get_session().is_open() is False
    assert hardware_config.get_device_handle().owner is None
    assert state.sim_controller.hardware_failure is True
    assert state.session_state().value.value == "setup"


def test_a_failed_bench_drive_ends_the_bench(client):
    from hardware import occupancy_bench

    test_client, state = client
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True
    hardware_config.get_live_device().set_fault("i2c_write_error")

    body = test_client.post(
        "/api/hardware/bench/drive", json={"running": True, "percent": 40}
    ).get_json()

    assert body["ok"] is False and body["tool_closed"] is True
    assert occupancy_bench.get_session().is_open() is False
    assert state.sim_controller.hardware_failure is True


def test_a_failed_read_leaves_the_commissioning_session_open(client):
    from hardware import commission

    test_client, state = client
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post(
        "/api/hardware/commission/open", json={"config": _payload()}
    ).get_json()["ok"] is True
    hardware_config.get_live_device().set_fault("shift_read_error")

    body = test_client.post("/api/hardware/commission/inputs", json={"chip_id": "in1"}).get_json()

    assert body["ok"] is False
    assert commission.get_session().is_open() is True
    assert hardware_config.get_device_handle().owner == owners.COMMISSIONING_SESSION


def test_a_restart_closes_an_open_setup_tool_before_the_exec(client, monkeypatch):
    """`os.execv` runs no atexit hook, so the restart closes the tool itself."""
    from hardware import occupancy_bench

    test_client, state = client
    scheduled = []

    class _NoTimer:
        def __init__(self, delay, fn):
            scheduled.append(fn)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _NoTimer)
    monkeypatch.setattr(
        state.persistence_manager, "save_merged_layout", lambda *a, **k: None
    )
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True
    hardware_config.get_live_device()
    test_client.post("/api/hardware/bench/drive", json={"running": True, "percent": 40})

    assert test_client.post("/api/server/restart").status_code == 200

    assert scheduled, "the exec was scheduled"
    assert occupancy_bench.get_session().is_open() is False
    assert hardware_config.get_device_handle().owner is None
    assert hardware_config.get_live_device().duty_cycle(0x40, 0) == 0.0
