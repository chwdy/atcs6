"""Develop mode: the real-mode stack on the in-memory fake device.

design_new.md "Power -> Hardware configuration": the persisted `develop` flag
makes `build_session_interfaces` wire the same `Ch347*` interfaces onto a
`FakeCh347Device` (no USB hardware, watchdog unarmed). These tests cover the
config round-trip of the flag, the develop factory path (no monkeypatching —
develop must work with no `ch347api` installed), the occupancy pipeline reading
injected fake state, the `/api/debug/hw` endpoints, and the commissioning tools
(self-test / occupancy-test / commission) running on the fake.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_hardware_develop.py -v
"""

import json
import threading
from datetime import datetime

import pytest

import hardware_config
import operation_environment
from enums import OperationMode
from hardware.fake import FakeCh347Device


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    """Point the store's file at a temp path, reset the singleton, and stub
    atexit so factory tests do not leak process-exit handlers."""
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    monkeypatch.setattr(hardware_config.atexit, "register", lambda *a, **k: None)
    hardware_config.reset_to_defaults()
    yield
    from hardware import commission
    commission.get_session().close()
    hardware_config.reset_to_defaults()


def _develop_payload(**overrides):
    """A full develop-mode configuration: every chip kind, all binding maps."""
    payload = {
        "enabled": True,
        "develop": True,
        "controller": {"kind": "ch347", "device_port": None},
        "chips": [
            {"chip_id": "pwm1", "kind": "pca9685", "i2c_address": "0x40"},
            {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
            {"chip_id": "in1", "kind": "hc165", "spi_select": "cs1"},
            {"chip_id": "cur1", "kind": "ina3221", "i2c_address": "0x41"},
        ],
        "block_track": {
            "B1": {"chip_id": "pwm1", "forward": 0, "reverse": 1},
        },
        "switch_output": {
            "SW_A": {"chip_id": "sw1", "port_main": 5, "port_diverge": 6},
        },
        "block_occupancy": {"B1": {"chip_id": "in1", "port": 2}},
        "block_occupancy_current": {
            "B1": {"chip_id": "cur1", "port": 0, "threshold_ma": 15.0},
        },
    }
    payload.update(overrides)
    return payload


# --- config round-trip ------------------------------------------------------
def test_develop_defaults_false():
    assert operation_environment.get_selected() is OperationMode.SIMULATION
    assert hardware_config.is_develop() is False


def test_virtual_hardware_round_trips_in_its_separate_store():
    hardware_config.apply_updates(_develop_payload())
    assert hardware_config.as_dict()["develop"] is True
    with open(hardware_config.CONFIG_PATH) as fh:
        assert "develop" not in json.load(fh)
    with open(operation_environment.ENVIRONMENT_PATH) as fh:
        assert json.load(fh)["selected"] == "virtual_hardware"
    hardware_config.reset_to_defaults()
    assert operation_environment.get_selected() is OperationMode.SIMULATION
    hardware_config.load_from_disk()
    assert hardware_config.is_develop() is False
    operation_environment.load_from_disk()
    assert hardware_config.is_develop() is True


def test_legacy_develop_flag_selects_virtual_hardware_independently():
    hardware_config.apply_updates({"develop": True}, persist=False)
    assert hardware_config.is_develop() is True
    assert hardware_config.derive_operation_mode() == OperationMode.VIRTUAL_HARDWARE


# --- factory ---------------------------------------------------------------
def test_factory_develop_wires_interfaces_on_fake_without_driver():
    # No monkeypatching of open_ch347_device: develop mode must never touch the
    # real driver, so it works with no ch347api installed.
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    from hardware.interfaces import (
        OccupancyDriver,
        SwitchDriver,
        TrackDriver,
    )
    assert isinstance(blk, TrackDriver)
    assert isinstance(sw, SwitchDriver)
    assert isinstance(inp, OccupancyDriver)
    assert hardware_config.derive_operation_mode() == OperationMode.VIRTUAL_HARDWARE
    # The session device is the opened fake, reachable for the debug endpoints.
    device = hardware_config.get_session_device()
    assert isinstance(device, FakeCh347Device)
    assert device.opened
    # No physical track to protect: the loss-of-control watchdog stays unarmed.
    assert blk._watchdog is None


def test_factory_develop_off_still_needs_real_driver(monkeypatch):
    import hardware.ch347_device as chdev

    def _boom(port=None):
        raise RuntimeError("no device attached")

    monkeypatch.setattr(chdev, "open_ch347_device", _boom)
    hardware_config.apply_updates(_develop_payload(develop=False), persist=False)
    with pytest.raises(hardware_config.HardwareBackendUnavailable):
        hardware_config.build_session_interfaces()
    assert hardware_config.get_session_device() is None


# --- occupancy pipeline on injected fake state ------------------------------
def test_injected_occupancy_bit_reaches_the_pipeline():
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    device = hardware_config.get_session_device()
    assert inp.read_block_currents()["B1"] is False
    device.set_input_bit("cs1", 2, True)   # B1's '165 binding: in1 port 2
    assert inp.read_block_currents()["B1"] is True
    device.set_input_bit("cs1", 2, False)
    assert inp.read_block_currents()["B1"] is False


def test_injected_current_decides_while_driven():
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    device = hardware_config.get_session_device()
    # Drive B1 above the idle deadband: the current channel now decides.
    blk.set_voltage("B1", 8.0)
    device.set_ina3221_current(0x41, 1, 0.025)   # 25 mA > 15 mA threshold
    assert inp.read_block_currents()["B1"] is True
    device.set_ina3221_current(0x41, 1, 0.0)
    assert inp.read_block_currents()["B1"] is False
    # Idle again: the '165 bit decides even with current present.
    blk.set_voltage("B1", 0.0)
    device.set_ina3221_current(0x41, 1, 0.025)
    assert inp.read_block_currents()["B1"] is False


# --- REST surface ----------------------------------------------------------
@pytest.fixture
def client():
    from app_factory import create_app
    from app_state import AppState
    from network import RailwayNetwork
    from path_dispatcher import PathDispatcher
    from persistence import PersistenceManager
    from simulation import SimulationController

    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=PersistenceManager(storage_dir="layouts"),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client, state


def _boot_develop_session(state):
    """Mirror bootstrap: build develop interfaces and wire them into the
    running SimulationController, so the debug endpoints see the live fake."""
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    _wire_session(state, inp)
    return inp, blk, sw


def _wire_session(state, inp):
    """Point the whole read path at `inp`, the way bootstrap does by passing it
    to the SimulationController constructor.

    The detector holds its own reference, so setting `sim_controller`'s alone
    would leave Step 0 still reading the simulated driver — and the verdict the
    read-back reports comes from the detector.
    """
    state.sim_controller.input_interface = inp
    state.sim_controller.block_occupancy_detector.input_interface = inp
    state.network.input_interface = inp
    # The bound blocks have to exist in the layout: Step 0 walks
    # `network.blocks`, so a binding for a block the network never heard of is
    # never read and never confirmed.
    from block import Block

    for block_id in hardware_config.get_hardware_config().block_occupancy:
        state.network.blocks.setdefault(block_id, Block(block_id))
    for block_id in hardware_config.get_hardware_config().block_occupancy_current:
        state.network.blocks.setdefault(block_id, Block(block_id))


def _run_step_zero(state):
    """Run the tick's Step 0 once: read the detectors and debounce.

    What turns a poked register into the verdict the bench's "App sees" column
    reports. The runtime does this at 10 Hz — from `update()` while running and
    from `poll_hardware_occupancy()` while stopped — but nothing ticks under the
    test client, so a test that wants a verdict has to ask for the read.
    """
    state.sim_controller.block_occupancy_detector.step()


def test_debug_endpoints_409_when_not_develop(client):
    test_client, _ = client
    assert test_client.get("/api/debug/hw").status_code == 409
    resp = test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "B1", "occupied": True})
    assert resp.status_code == 409
    resp = test_client.post("/api/debug/hw/current",
                            json={"block_id": "B1", "current_ma": 25.0})
    assert resp.status_code == 409


def test_debug_occupancy_poke_and_readback(client):
    test_client, state = client
    inp, blk, sw = _boot_develop_session(state)
    resp = test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "B1", "occupied": True})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    # The runtime pipeline reads the injected bit...
    assert inp.read_block_currents()["B1"] is True
    # ...the register read-back reports it immediately...
    data = test_client.get("/api/debug/hw").get_json()
    assert data["develop_active"] is True
    assert data["blocks"]["B1"]["occupancy_bit"] is True
    # ...but `occupied` is the pipeline's verdict, not the register, so it
    # appears only once Step 0 has read and debounced.
    assert data["blocks"]["B1"]["occupied"] is False
    _run_step_zero(state)
    data = test_client.get("/api/debug/hw").get_json()
    assert data["blocks"]["B1"]["occupied"] is True


def test_debug_current_poke_and_readback(client):
    test_client, state = client
    inp, blk, sw = _boot_develop_session(state)
    blk.set_voltage("B1", 8.0)   # driven -> current decides
    resp = test_client.post("/api/debug/hw/current",
                            json={"block_id": "B1", "current_ma": 25.0})
    assert resp.status_code == 200
    _run_step_zero(state)
    data = test_client.get("/api/debug/hw").get_json()
    assert data["blocks"]["B1"]["current_ma"] == pytest.approx(25.0, abs=1.0)
    assert data["blocks"]["B1"]["volts"] == pytest.approx(8.0, abs=0.1)
    assert data["blocks"]["B1"]["occupied"] is True


def test_debug_unbound_block_404(client):
    test_client, state = client
    _boot_develop_session(state)
    resp = test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "UNIDENTIFIED", "occupied": True})
    assert resp.status_code == 404
    resp = test_client.post("/api/debug/hw/current",
                            json={"block_id": "UNIDENTIFIED", "current_ma": 25.0})
    assert resp.status_code == 404


def test_debug_missing_fields_400(client):
    test_client, state = client
    _boot_develop_session(state)
    assert test_client.post("/api/debug/hw/occupancy",
                            json={"block_id": "B1"}).status_code == 400
    assert test_client.post("/api/debug/hw/current",
                            json={"current_ma": 5.0}).status_code == 400


# --- one-block presence (the bench's "place a train here") ------------------
def test_presence_writes_both_detectors_so_the_verdict_holds_either_way(client):
    """The reason presence exists: setting one source leaves the other saying
    the opposite, and which one the runtime believes depends on the drive. A
    placed object must read as present idle AND driven."""
    test_client, state = client
    inp, blk, _sw = _boot_develop_session(state)

    resp = test_client.put("/api/virtual-hardware/input/presence",
                           json={"block_id": "B1", "present": True})
    assert resp.status_code == 200 and resp.get_json()["present"] is True

    # Idle: the '165 bit answers.
    assert inp.read_block_currents()["B1"] is True
    data = test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]
    assert data["occupancy_bit"] is True
    assert data["current_ma"] > data["baseline_ma"] + data["threshold_ma"]

    # Driven: the current channel answers instead — same verdict.
    blk.set_voltage("B1", 8.0)
    assert inp.read_block_currents()["B1"] is True

    test_client.put("/api/virtual-hardware/input/presence",
                    json={"block_id": "B1", "present": False})
    assert inp.read_block_currents()["B1"] is False
    blk.set_voltage("B1", 0.0)
    assert inp.read_block_currents()["B1"] is False


def test_presence_rejects_an_unwired_block_and_a_missing_field(client):
    test_client, state = client
    _boot_develop_session(state)
    assert test_client.put("/api/virtual-hardware/input/presence",
                           json={"block_id": "UNIDENTIFIED", "present": True}).status_code == 404
    assert test_client.put("/api/virtual-hardware/input/presence",
                           json={"block_id": "B1"}).status_code == 400


def test_presence_409s_outside_virtual_hardware(client):
    test_client, _ = client
    resp = test_client.put("/api/virtual-hardware/input/presence",
                           json={"block_id": "B1", "present": True})
    assert resp.status_code == 409


# --- the read-back the Virtual Bench renders --------------------------------
def test_readback_reports_binding_flags_and_the_commanded_duty_pair(client):
    """The bench builds its rows straight off this payload, so it needs to tell
    'unwired' from 'wired and reading zero' — that is what the has_* flags are
    for — and it shows the duty pair the board was actually handed."""
    test_client, state = client
    _inp, blk, _sw = _boot_develop_session(state)
    blk.set_voltage("B1", 6.0)

    entry = test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]
    assert (entry["has_track"], entry["has_digital"], entry["has_current"]) == (True, True, True)
    assert entry["duty_forward"] == pytest.approx(0.5, abs=0.02)
    assert entry["duty_reverse"] == pytest.approx(0.0, abs=0.01)
    assert entry["volts"] == pytest.approx(6.0, abs=0.1)
    assert entry["track_reversed"] is False


def test_readback_volts_follow_the_rail_polarity_flag(client):
    """A block wired backwards drives the channel the binding calls `reverse`
    for a positive command (TrackDriver swaps them). The read-back has to undo
    that, or the panel shows the layout driving the opposite way from the model."""
    test_client, state = client
    payload = _develop_payload()
    payload["block_track"]["B1"]["reversed"] = True
    hardware_config.apply_updates(payload, persist=False)
    # Same wiring bootstrap uses: the driver reads the flag through this
    # callable, so omitting it would leave polarity unapplied at the source.
    _inp, blk, _sw = hardware_config.build_session_interfaces(
        track_polarity_of=hardware_config.track_polarity_reversed
    )
    _wire_session(state, _inp)
    blk.set_voltage("B1", 6.0)

    entry = test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]
    assert entry["track_reversed"] is True
    assert entry["duty_forward"] == pytest.approx(0.0, abs=0.01)
    assert entry["duty_reverse"] == pytest.approx(0.5, abs=0.02)
    assert entry["volts"] == pytest.approx(6.0, abs=0.1)


def test_readback_resolves_the_detector_bit_through_the_chain_polarity(client):
    """An active-low chain pulls its line LOW when current flows, so the raw
    register bit is the OPPOSITE of "a train is here". The read-back has to
    carry the resolved reading too, or a panel keyed to the raw bit offers to
    place a train on every block that already has one."""
    test_client, state = client
    payload = _develop_payload()
    payload["chips"] = [
        dict(c, active_low=True) if c["chip_id"] == "in1" else c
        for c in payload["chips"]
    ]
    hardware_config.apply_updates(payload, persist=False)
    inp, _blk, _sw = hardware_config.build_session_interfaces()
    _wire_session(state, inp)

    # An untouched fake reads all-zero, which on this chain means PRESENT.
    entry = test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]
    assert entry["active_low"] is True
    assert entry["occupancy_bit"] is False
    assert entry["detector_present"] is True
    assert inp.read_block_currents()["B1"] is True

    # Lifting the object through the presence endpoint flips the raw bit to 1,
    # and both the resolved reading and the pipeline agree it is now clear.
    test_client.put("/api/virtual-hardware/input/presence",
                    json={"block_id": "B1", "present": False})
    entry = test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]
    assert entry["occupancy_bit"] is True
    assert entry["detector_present"] is False
    assert inp.read_block_currents()["B1"] is False


def test_readback_lists_a_track_only_block(client):
    """A block with rails but no detector still has an output worth watching."""
    test_client, state = client
    payload = _develop_payload()
    payload["block_track"]["B_OUT_ONLY"] = {"chip_id": "pwm1", "forward": 2, "reverse": 3}
    hardware_config.apply_updates(payload, persist=False)
    _inp, _blk, _sw = hardware_config.build_session_interfaces()
    _wire_session(state, _inp)

    entry = test_client.get("/api/virtual-hardware").get_json()["blocks"]["B_OUT_ONLY"]
    assert entry["has_track"] is True
    assert (entry["has_digital"], entry["has_current"]) == (False, False)
    assert entry["occupancy_bit"] is None and entry["current_ma"] is None
    # No detector, so there is no verdict to report — null, not "clear".
    assert entry["occupied"] is None


def test_readback_never_re_reads_the_detectors(client):
    """The GET must not call `read_block_currents()`.

    That call MUTATES the driver: it advances the lighting-zone clear window and
    rewrites `_last_reported`. The bench polls several times a second, so a
    read-back that went through it would add its reads to a window sized for the
    10 Hz tick — the panel would change the release timing of the block it is
    watching. The verdict comes off the detector's confirmed set instead, and the
    per-detector values are direct chip-register reads, which have no such
    effect.
    """
    test_client, state = client
    inp, _blk, _sw = _boot_develop_session(state)
    reads = []
    original = inp.read_block_currents

    def _counted():
        reads.append(1)
        return original()

    inp.read_block_currents = _counted

    entry = test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]
    assert reads == []
    # And it still reports the register values, which come from the chips.
    assert entry["occupancy_bit"] is False and entry["current_ma"] is not None


def test_readback_verdict_follows_the_debounce_not_the_register(client):
    """`occupied` is what the app acts on, so it clears on the pipeline's
    schedule — after OCCUPANCY_OFF_THRESHOLD clear reads — not the instant the
    register drops. A panel keyed to the raw read would say "clear" while the
    layout still showed the block held."""
    from constants import OCCUPANCY_OFF_THRESHOLD

    test_client, state = client
    _inp, _blk, _sw = _boot_develop_session(state)

    test_client.put("/api/virtual-hardware/input/presence",
                    json={"block_id": "B1", "present": True})
    _run_step_zero(state)
    assert test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]["occupied"] is True

    test_client.put("/api/virtual-hardware/input/presence",
                    json={"block_id": "B1", "present": False})
    for _ in range(OCCUPANCY_OFF_THRESHOLD - 1):
        _run_step_zero(state)
        assert (
            test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]["occupied"]
            is True
        )
    _run_step_zero(state)
    assert test_client.get("/api/virtual-hardware").get_json()["blocks"]["B1"]["occupied"] is False


def test_config_get_reports_session_info(client):
    test_client, state = client
    data = test_client.get("/api/hardware/config").get_json()
    assert data["session"]["operation_environment"] == "simulation"
    assert data["session"]["environment_active"] is True
    assert data["session"]["virtual_hardware_active"] is False
    _boot_develop_session(state)
    data = test_client.get("/api/hardware/config").get_json()
    assert data["session"]["develop_active"] is True


# --- commissioning tools on the fake ---------------------------------------
def test_scan_runs_on_fake_when_develop_set(client):
    test_client, _ = client
    payload = _develop_payload()
    hardware_config.apply_updates(payload, persist=False)
    resp = test_client.post("/api/hardware/scan", json={"config": payload})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["controller"]["reachable"] is True
    # The fake is seeded from the payload's chip list, so every configured I2C
    # address answers the scan.
    assert data["controller"]["i2c_scan"] == ["0x40", "0x41"]


def test_scan_runs_on_fake_before_enable(client):
    # The pre-enable commissioning flow: develop is persisted but enabled is
    # still false (you cannot enable until every chip is proven). The gate is
    # the develop flag alone, so Detect / Rescan still run on the fake.
    test_client, _ = client
    payload = _develop_payload(enabled=False)
    hardware_config.apply_updates(payload, persist=False)
    assert hardware_config.is_develop() is False   # not a running develop session yet
    resp = test_client.post("/api/hardware/scan", json={"config": payload})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["controller"]["reachable"] is True


def test_scan_still_fails_without_develop(client):
    test_client, _ = client
    payload = _develop_payload(develop=False)
    hardware_config.apply_updates(payload, persist=False)
    resp = test_client.post("/api/hardware/scan", json={"config": payload})
    # No fake gate, no physical device: the controller line reports unreachable.
    assert resp.get_json()["controller"]["reachable"] is False


# The scan follows the flag on the config UNDER TEST, not the persisted one, so
# the setup panel's Develop toggle changes which device answers with no Save in
# between. Both directions matter: reading the persisted flag instead made a
# scan sent with develop off still open the fake and report its seeded addresses
# as a genuine "Connected".
def test_scan_follows_payload_develop_off_over_persisted_on(client):
    test_client, _ = client
    hardware_config.apply_updates(_develop_payload(), persist=False)
    assert hardware_config.is_develop() is True
    resp = test_client.post(
        "/api/hardware/scan", json={"config": _develop_payload(develop=False)}
    )
    data = resp.get_json()
    assert data["controller"]["reachable"] is False
    assert data["controller"]["i2c_scan"] == []


def test_scan_follows_payload_develop_on_over_persisted_off(client):
    test_client, _ = client
    hardware_config.apply_updates(_develop_payload(develop=False), persist=False)
    assert hardware_config.is_develop() is False
    resp = test_client.post(
        "/api/hardware/scan", json={"config": _develop_payload(develop=True)}
    )
    data = resp.get_json()
    assert data["controller"]["reachable"] is True
    assert data["controller"]["i2c_scan"] == ["0x40", "0x41"]


def test_occupancy_test_reads_live_session_fake(client):
    test_client, state = client
    _boot_develop_session(state)
    device = hardware_config.get_session_device()
    device.set_input_bit("cs1", 2, True)
    resp = test_client.post(
        "/api/hardware/occupancy-test",
        json={"config": _develop_payload(), "block_id": "B1"},
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert data["digital"]["value"] is True


def test_commission_session_opens_on_fake(client):
    test_client, _ = client
    payload = _develop_payload()
    hardware_config.apply_updates(payload, persist=False)
    assert test_client.post("/api/operation/commissioning/enter").status_code == 200
    resp = test_client.post("/api/hardware/commission/open", json={"config": payload})
    assert resp.get_json()["ok"] is True
    resp = test_client.post("/api/hardware/commission/inputs", json={"chip_id": "in1"})
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["ports"]) == 8


def test_commissioning_enter_releases_the_boundary_with_its_device(client, monkeypatch):
    """`enter` must leave nothing bound to the handle it closes.

    A hardware-backed session builds every chip on one open device, and entering
    commissioning closes that device. While the session's drivers stayed bound to
    it, the next safe state wrote through a dead handle and `OutputSafety` raised
    "the CH347 is not open" — which 500'd a second `enter` and `activate` alike,
    leaving no way back to operation short of restarting the server.
    """
    test_client, state = client

    class StrictFake(FakeCh347Device):
        """`Ch347Device` refuses bus ops once closed; the plain fake does not,
        so a stale binding would go unnoticed here."""

        def i2c_write(self, addr, data):
            if self.closed:
                raise RuntimeError("i2c_write: the CH347 is not open")
            return super().i2c_write(addr, data)

        def latch_out(self, data, select):
            if self.closed:
                raise RuntimeError("latch_out: the CH347 is not open")
            return super().latch_out(data, select)

    monkeypatch.setattr("hardware.fake.FakeCh347Device", StrictFake)
    hardware_config.apply_updates(_develop_payload(), persist=False)
    state.sim_controller.activate_interfaces(
        OperationMode.VIRTUAL_HARDWARE,
        *hardware_config.build_session_interfaces(
            hardware_config.get_hardware_config(),
            environment=OperationMode.VIRTUAL_HARDWARE,
            track_polarity_of=hardware_config.track_polarity_reversed,
        ),
    )
    device = hardware_config.get_live_device()
    assert isinstance(device, StrictFake)

    assert test_client.post("/api/operation/commissioning/enter").status_code == 200
    assert device.closed is True
    assert hardware_config.get_live_device() is None
    # The boundary follows the device out — the outputs were safed on the way.
    from block_occupancy import SimulatedOccupancyDriver
    from block_power import SimulatedTrackDriver

    assert isinstance(
        state.sim_controller.power_controller.output_interface, SimulatedTrackDriver
    )
    assert isinstance(state.sim_controller.input_interface, SimulatedOccupancyDriver)
    assert state.sim_controller.environment_active is False
    # Releasing the boundary must not erase why the environment is inactive.
    assert state.sim_controller.activation_issues == [
        {"code": "commissioning", "message": "commissioning owns the device"}
    ]

    assert test_client.post("/api/operation/commissioning/exit").status_code == 200
    # The re-entry that used to 500 on the closed handle.
    assert test_client.post("/api/operation/commissioning/enter").status_code == 200


def test_virtual_hardware_fault_api_controls_the_live_fake(client):
    test_client, state = client
    _boot_develop_session(state)

    response = test_client.put(
        "/api/virtual-hardware/fault",
        json={"kind": "i2c_write_error", "enabled": True},
    )

    assert response.status_code == 200
    assert response.get_json()["faults"]["i2c_write_error"] is True
    device = hardware_config.get_session_device()
    with pytest.raises(OSError, match="i2c_write_error"):
        device.i2c_write(0x40, b"\x00")
    state_payload = test_client.get("/api/virtual-hardware").get_json()
    assert state_payload["virtual_hardware_active"] is True
    assert state_payload["faults"]["i2c_write_error"] is True

    cleared = test_client.post("/api/virtual-hardware/faults/clear").get_json()
    assert not any(cleared["faults"].values())


def test_virtual_rig_api_places_and_removes_a_train_through_detectors(client):
    test_client, state = client
    occupancy, _track, _switch = _boot_develop_session(state)

    placed = test_client.post(
        "/api/virtual-hardware/scenario",
        json={"action": "start", "blocks": ["B1"]},
    ).get_json()
    assert placed["scenario"]["block_id"] == "B1"
    assert occupancy.read_block_currents()["B1"] is True

    removed = test_client.post(
        "/api/virtual-hardware/scenario", json={"action": "remove"}
    ).get_json()
    assert removed["scenario"]["active"] is False
    assert occupancy.read_block_currents()["B1"] is False
