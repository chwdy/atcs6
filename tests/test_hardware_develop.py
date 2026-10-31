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
    """Point the store's file at a temp path and reset the singleton."""
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
    from hardware import commission
    commission.get_session().close()
    hardware_config.reset_to_defaults()


def _empty_the_virtual_layout(device=None):
    """Lift the startup train off every bound block.

    A Virtual Hardware session comes up with one block occupied (see
    `virtual_rig.seed_startup_presence`) because a rig at rest is a layout with a
    loco parked on it. Tests that exercise the pipeline itself want neither that
    train nor the register file's raw default — they want only the state they
    place — so they start from an empty layout.
    """
    from hardware import virtual_rig

    cfg = hardware_config.get_hardware_config()
    device = device if device is not None else hardware_config.get_session_device()
    for block_id in virtual_rig.bound_block_ids(cfg):
        virtual_rig.set_block_presence(cfg, device, block_id, False)


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
    assert operation_environment.get_selected() is not OperationMode.VIRTUAL_HARDWARE


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
    assert operation_environment.get_selected() is not OperationMode.VIRTUAL_HARDWARE
    operation_environment.load_from_disk()
    assert operation_environment.get_selected() is OperationMode.VIRTUAL_HARDWARE


def test_legacy_develop_flag_selects_virtual_hardware_independently():
    hardware_config.apply_updates({"develop": True}, persist=False)
    assert operation_environment.get_selected() is OperationMode.VIRTUAL_HARDWARE
    assert operation_environment.get_selected() == OperationMode.VIRTUAL_HARDWARE


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
    assert operation_environment.get_selected() == OperationMode.VIRTUAL_HARDWARE
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


# --- the state a fresh virtual rig comes up in ------------------------------
def test_virtual_session_starts_with_exactly_one_block_occupied():
    """One train parked on the layout, not a full one and not an empty one.

    The register file's own default is neither: an untouched `FakeCh347Device`
    returns zero for every '165 byte, which on an `active_low` chain reads as
    CURRENT DETECTED — so the session used to come up with every bound block
    occupied, a state no real rig can be in.
    """
    for active_low in (False, True):
        chips = [
            dict(c, active_low=active_low) if c["chip_id"] == "in1" else c
            for c in _develop_payload()["chips"]
        ]
        hardware_config.apply_updates(
            _develop_payload(
                chips=chips,
                block_occupancy={
                    "B1": {"chip_id": "in1", "port": 2},
                    "B2": {"chip_id": "in1", "port": 3},
                    "B3": {"chip_id": "in1", "port": 4},
                },
            ),
            persist=False,
        )
        inp, _blk, _sw = hardware_config.build_session_interfaces()
        occupied = {b for b, v in inp.read_block_currents().items() if v}
        assert occupied == {"B1"}, f"active_low={active_low}"


def test_injected_occupancy_bit_reaches_the_pipeline():
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    device = hardware_config.get_session_device()
    _empty_the_virtual_layout(device)
    assert inp.read_block_currents()["B1"] is False
    device.set_input_bit("cs1", 2, True)   # B1's '165 binding: in1 port 2
    assert inp.read_block_currents()["B1"] is True
    device.set_input_bit("cs1", 2, False)
    assert inp.read_block_currents()["B1"] is False


def test_injected_current_decides_while_driven():
    hardware_config.apply_updates(_develop_payload(), persist=False)
    inp, blk, sw = hardware_config.build_session_interfaces()
    device = hardware_config.get_session_device()
    _empty_the_virtual_layout(device)
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
    _empty_the_virtual_layout()
    _wire_session(state, inp)
    return inp, blk, sw


def _activate_virtual_session(state):
    """Activate a hardware-backed Virtual Hardware session the way
    `/api/operation/activate` does, and return the live fake device.

    Unlike `_boot_develop_session`, which only points the read path at the fake,
    this hands the runtime's own power controllers the hardware drivers — so a
    safe state actually reaches the device.
    """
    hardware_config.apply_updates(_develop_payload(), persist=False)
    state.sim_controller.activate_interfaces(
        OperationMode.VIRTUAL_HARDWARE,
        *hardware_config.build_session_interfaces(
            hardware_config.get_hardware_config(),
            environment=OperationMode.VIRTUAL_HARDWARE,
            track_polarity_of=hardware_config.track_polarity_reversed,
        ),
    )
    return hardware_config.get_live_device()


def _wire_session(state, inp):
    """Point the whole read path at `inp`, the way bootstrap does by passing it
    to the SimulationController constructor.

    The detector holds its own reference, so setting `sim_controller`'s alone
    would leave Observe still reading the simulated driver — and the verdict the
    read-back reports comes from the detector.
    """
    state.sim_controller.input_interface = inp
    state.sim_controller.block_occupancy_detector.input_interface = inp
    state.network.input_interface = inp
    # The bound blocks have to exist in the layout: Observe walks
    # `network.blocks`, so a binding for a block the network never heard of is
    # never read and never confirmed.
    from block import Block

    for block_id in hardware_config.get_hardware_config().block_occupancy:
        state.network.blocks.setdefault(block_id, Block(block_id))
    for block_id in hardware_config.get_hardware_config().block_occupancy_current:
        state.network.blocks.setdefault(block_id, Block(block_id))


def _run_step_zero(state, times: int = 1):
    """Run the tick's Observe stage: read the detectors and debounce.

    What turns a poked register into the verdict the bench's "App sees" column
    reports. The runtime does this at 10 Hz — from `update()` while running and
    from `poll_hardware_occupancy()` while stopped — but nothing ticks under the
    test client, so a test that wants a verdict has to ask for the read.
    """
    for _ in range(times):
        state.sim_controller.block_occupancy_detector.step()


def _confirm_step_zero(state):
    """Read enough times for a poked presence to be confirmed occupied.

    Confirming takes `OCCUPANCY_ON_THRESHOLD` consecutive reads, so a test that
    wants a block held asks for that many rather than assuming one is enough —
    the mirror of the `OCCUPANCY_OFF_THRESHOLD` loops on the clear side.
    """
    from constants import OCCUPANCY_ON_THRESHOLD

    _run_step_zero(state, OCCUPANCY_ON_THRESHOLD)


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
    # appears only once Observe has read and debounced.
    assert data["blocks"]["B1"]["occupied"] is False
    _confirm_step_zero(state)
    data = test_client.get("/api/debug/hw").get_json()
    assert data["blocks"]["B1"]["occupied"] is True


def test_debug_current_poke_and_readback(client):
    test_client, state = client
    inp, blk, sw = _boot_develop_session(state)
    blk.set_voltage("B1", 8.0)   # driven -> current decides
    resp = test_client.post("/api/debug/hw/current",
                            json={"block_id": "B1", "current_ma": 25.0})
    assert resp.status_code == 200
    _confirm_step_zero(state)
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
    _confirm_step_zero(state)
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
    assert operation_environment.get_selected() is not OperationMode.VIRTUAL_HARDWARE   # not a running develop session yet
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
    assert operation_environment.get_selected() is OperationMode.VIRTUAL_HARDWARE
    resp = test_client.post(
        "/api/hardware/scan", json={"config": _develop_payload(develop=False)}
    )
    data = resp.get_json()
    assert data["controller"]["reachable"] is False
    assert data["controller"]["i2c_scan"] == []


def test_scan_follows_payload_develop_on_over_persisted_off(client):
    test_client, _ = client
    hardware_config.apply_updates(_develop_payload(develop=False), persist=False)
    assert operation_environment.get_selected() is not OperationMode.VIRTUAL_HARDWARE
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
    assert test_client.post("/api/operation/setup").status_code == 200
    resp = test_client.post("/api/hardware/commission/open", json={"config": payload})
    assert resp.get_json()["ok"] is True
    resp = test_client.post("/api/hardware/commission/inputs", json={"chip_id": "in1"})
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["ports"]) == 8


def test_setup_tools_are_refused_outside_setup(client):
    """The tools that drive the outputs run only in Setup (design_new.md
    "Hardware setup", Setup); doc gap B adds a port-test output pulse."""
    test_client, _state = client
    payload = _develop_payload()
    hardware_config.apply_updates(payload, persist=False)
    assert test_client.post(
        "/api/hardware/commission/open", json={"config": payload}
    ).status_code == 409
    assert test_client.post("/api/hardware/bench/open").status_code == 409
    assert test_client.post(
        "/api/hardware/port-test",
        json={"config": payload, "chip_id": "sw1", "port": 0, "action": "pulse_output"},
    ).status_code == 409


def test_entering_setup_keeps_the_session_device_and_detectors(client):
    """Entering Setup for the environment the session was built for keeps its
    device and `OccupancyInput` open, so the detectors keep being read and
    every Setup tool reaches the rig through that one handle; no environment
    is active (design_new.md "Hardware setup", Setup)."""
    from hardware.interfaces import OccupancyDriver

    test_client, state = client
    device = _activate_virtual_session(state)

    response = test_client.post("/api/operation/setup")

    assert response.status_code == 200
    assert response.get_json()["session_state"] == {"value": "setup", "halt_reason": None}
    assert device.closed is False
    assert hardware_config.get_live_device() is device
    assert isinstance(state.sim_controller.input_interface, OccupancyDriver)
    assert state.sim_controller.environment_active is False
    assert state.setup_active is True
    # Already in Setup: refused.
    assert test_client.post("/api/operation/setup").status_code == 409


def test_entering_setup_is_refused_in_simulation(client, monkeypatch):
    test_client, _state = client
    monkeypatch.setattr(operation_environment, "_selected", OperationMode.SIMULATION)
    assert test_client.post("/api/operation/setup").status_code == 409


def test_the_stopped_pass_reads_the_detectors_in_setup_and_writes_nothing(client):
    test_client, state = client
    _activate_virtual_session(state)
    assert test_client.post("/api/operation/setup").status_code == 200
    sim = state.sim_controller
    writes = []
    sim.power_controller.set_block_voltage = lambda *a, **k: writes.append(a)
    reads = []
    real_read = sim.input_interface.read_block_currents

    def counting_read(*a, **k):
        reads.append(True)
        return real_read(*a, **k)

    sim.input_interface.read_block_currents = counting_read
    sim.block_occupancy_detector.input_interface = sim.input_interface

    sim.poll_hardware_occupancy()

    assert reads, "the detectors are read in Setup"
    assert writes == [], "the drain writes no output while the Setup tools own them"


def test_entering_setup_reports_a_rig_it_could_not_safe(client):
    """A bridge that has left the bus must not turn Enter Setup into a 500.

    Setup's tools are how the operator reaches the rig, so entry completes: the
    response names every chip that did not answer, and the hardware-failure
    latch says the outputs were never proven safe. The session reads Setup,
    which outranks the hardware failure until an activation succeeds."""
    test_client, state = client
    device = _activate_virtual_session(state)
    device.set_fault("disconnect")           # the bridge leaves the bus

    resp = test_client.post("/api/operation/setup")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["safe_state"]["ok"] is False
    failures = data["safe_state"]["failures"]
    assert {line.split(":")[0] for line in failures} == {"sw1", "pwm1"}
    assert all("disconnected" in line for line in failures)
    assert state.sim_controller.hardware_failure is True
    assert state.sim_controller.environment_active is False
    assert state.setup_active is True
    assert data["session_state"]["value"] == "setup"


# --- occupancy bench: a Setup tool -------------------------------------------
def _ready_virtual_layout(state):
    """A layout and setup that pass readiness together: one block bound on every
    map, and no bindings for entities the layout does not have."""
    from block import Block

    state.network.blocks["B1"] = Block(block_id="B1")
    hardware_config.apply_updates(_develop_payload(switch_output={}), persist=False)


def test_the_bench_borrows_the_setup_device_and_closing_it_never_activates(client):
    """The bench reaches the rig through the session's device in Setup, and
    closing it completes the safe state and leaves the session in Setup
    (design_new.md "Hardware setup", Occupancy bench)."""
    test_client, state = client
    _ready_virtual_layout(state)
    assert test_client.post("/api/operation/activate").status_code == 200
    device = hardware_config.get_live_device()
    assert device is not None
    assert test_client.post("/api/operation/setup").status_code == 200

    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True
    data = test_client.post("/api/hardware/bench/close").get_json()

    assert data == {"ok": True}
    assert state.setup_active is True
    assert state.sim_controller.environment_active is False
    assert hardware_config.get_live_device() is device
    assert device.closed is False


def test_the_block_check_runs_on_the_virtual_rig_and_needs_an_open_bench(client):
    """`POST /api/hardware/bench/check-block` (design_new.md "Hardware setup",
    Occupancy bench, Block check): refused while the bench is closed and for a
    block with no rail binding, and in Virtual Hardware a well-wired block checks
    out because the fake device models the swamped detector."""
    test_client, state = client
    _ready_virtual_layout(state)
    assert test_client.post("/api/operation/activate").status_code == 200
    assert test_client.post("/api/operation/setup").status_code == 200

    closed = test_client.post("/api/hardware/bench/check-block", json={"block_id": "B1"})
    assert closed.status_code == 409

    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True
    try:
        unknown = test_client.post("/api/hardware/bench/check-block", json={"block_id": "B9"})
        assert unknown.status_code == 400

        data = test_client.post("/api/hardware/bench/check-block", json={"block_id": "B1"}).get_json()
        assert data["ok"] is True
        assert data["block_id"] == "B1"
        assert data["digital"] in ("followed_drive", "present_at_rest")
        assert data["others"] == []
    finally:
        test_client.post("/api/hardware/bench/close")


def test_a_bench_on_its_own_fake_device_starts_with_every_block_clear(client):
    """Setup entered without ever activating Virtual Hardware: no session holds
    a fake device, so the bench opens its own. An untouched fake reads every
    '165 bit as zero, which an active-low chain reports as a train on every
    block, so the bench writes every block clear first and an empty block's bit
    follows the drive (design_new.md "Hardware setup", Virtual Hardware)."""
    from block import Block

    test_client, state = client
    state.network.blocks["B1"] = Block(block_id="B1")
    payload = _develop_payload(switch_output={})
    payload["chips"][2]["active_low"] = True        # in1, the hc165
    hardware_config.apply_updates(payload, persist=False)
    assert test_client.post("/api/operation/setup").status_code == 200
    assert hardware_config.get_live_device() is None

    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True
    try:
        data = test_client.post("/api/hardware/bench/check-block", json={"block_id": "B1"}).get_json()
        assert data["digital"] == "followed_drive"
        assert data["verdict"] == "wired"
    finally:
        test_client.post("/api/hardware/bench/close")


def test_an_expired_bench_leaves_the_session_in_setup(client, monkeypatch):
    test_client, state = client
    _ready_virtual_layout(state)
    assert test_client.post("/api/operation/activate").status_code == 200
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True

    from hardware import occupancy_bench

    monkeypatch.setattr(occupancy_bench, "IDLE_TIMEOUT_S", -1.0)
    data = test_client.get("/api/hardware/bench/state").get_json()

    assert data["open"] is False
    assert state.setup_active is True
    assert state.sim_controller.environment_active is False


def test_activate_closes_an_open_bench_and_leaves_setup(client):
    test_client, state = client
    _ready_virtual_layout(state)
    assert test_client.post("/api/operation/activate").status_code == 200
    assert test_client.post("/api/operation/setup").status_code == 200
    assert test_client.post("/api/hardware/bench/open").get_json()["ok"] is True

    assert test_client.post("/api/operation/activate").status_code == 200

    from hardware import occupancy_bench

    assert occupancy_bench.get_session().is_open() is False
    assert state.setup_active is False
    assert state.session_state().is_live


def test_virtual_hardware_fault_api_controls_the_live_fake(client):
    """`PUT /api/virtual-hardware/fault` takes a VirtualHardwareFaultRequest
    (design_new.md "Hardware REST payload schemas"): an operator-facing
    VirtualHardwareFaultKind against a target reproduces on the live fake."""
    test_client, state = client
    _boot_develop_session(state)

    response = test_client.put(
        "/api/virtual-hardware/fault",
        json={
            "action": "inject",
            "target_kind": "transaction",
            "target_id": "controller0",
            "operation": "write",
            "fault": "transaction_refusal",
        },
    )

    assert response.status_code == 200
    injected = response.get_json()["injected_faults"]
    assert any(f["fault"] == "transaction_refusal" for f in injected)
    # The injected operator fault reproduces on the transport: the same
    # OSError the real occupancy/output pipeline turns into a hardware failure.
    device = hardware_config.get_session_device()
    with pytest.raises(OSError):
        device.i2c_write(0x40, b"\x00")
    state_payload = test_client.get("/api/virtual-hardware").get_json()
    assert state_payload["virtual_hardware_active"] is True
    assert any(
        f["fault"] == "transaction_refusal"
        for f in state_payload["injected_faults"]
    )

    cleared = test_client.post("/api/virtual-hardware/faults/clear").get_json()
    assert cleared["injected_faults"] == []
    # After clear the transport recovers.
    assert device.i2c_write(0x40, b"\x00") in (True, False)


def test_virtual_hardware_fault_api_rejects_malformed_requests(client):
    """Validation of the VirtualHardwareFaultRequest: fault is required for
    inject and optional for clear (a valid fault narrows removal to that one
    record; an unknown kind is still rejected), and an incompatible
    (target_kind, fault) inject pair is rejected rather than translated
    (design_new.md 3353)."""
    test_client, state = client
    _boot_develop_session(state)

    def put(body):
        return test_client.put("/api/virtual-hardware/fault", json=body)

    # inject with no fault -> 400
    assert put({
        "action": "inject", "target_kind": "chip", "target_id": "c0",
    }).status_code == 400
    # incompatible (chip cannot carry output_disagrees_with_command) -> 400
    assert put({
        "action": "inject", "target_kind": "chip", "target_id": "c0",
        "fault": "output_disagrees_with_command",
    }).status_code == 400
    # unknown fault -> 400
    assert put({
        "action": "inject", "target_kind": "transaction", "target_id": "c0",
        "fault": "made_up",
    }).status_code == 400
    # clear with an UNKNOWN fault -> 400 (a valid fault is now allowed on a clear
    # to remove only that one record; an unknown kind is still rejected).
    assert put({
        "action": "clear", "target_kind": "chip", "target_id": "c0",
        "fault": "made_up",
    }).status_code == 400
    # missing target_id -> 400
    assert put({
        "action": "inject", "target_kind": "chip", "fault": "device_disconnect",
    }).status_code == 400

    # A well-formed inject then its clear both succeed.
    assert put({
        "action": "inject", "target_kind": "chip", "target_id": "c0",
        "fault": "device_disconnect",
    }).status_code == 200
    assert put({
        "action": "clear", "target_kind": "chip", "target_id": "c0",
    }).status_code == 200


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
