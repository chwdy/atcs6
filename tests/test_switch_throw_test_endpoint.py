"""The switch editor's Test throw is a throw, not a coil poke.

`POST /api/hardware/switch-test` exists because the bench test used to drive the
coils straight through the commissioning session: the points alternated main /
diverge under the operator's eyes while `Switch.position` never moved, so the
moment the test stopped the app's idea of the layout was wrong and nothing
reconciled it. The endpoint commands the alignment it is testing through
`SwitchManager`, so the model follows the direction being tested — whichever
mechanism ends up driving the coil.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_switch_throw_test_endpoint.py -v
"""

import threading
from datetime import datetime

import pytest

import hardware_config
from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from switch import Switch
from switch_manager import SwitchManager
from track import Tile, TRACK_EW, TRACK_NS, TRACK_NW


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    monkeypatch.setattr(hardware_config.atexit, "register", lambda *a, **k: None)
    hardware_config.reset_to_defaults()
    yield
    from hardware import commission

    commission.get_session().close()
    hardware_config.reset_to_defaults()


CONFIG = {
    "enabled": True,
    "develop": True,
    "controller": {"kind": "ch347", "device_port": None},
    "chips": [
        {"chip_id": "sw1", "kind": "hc595", "spi_select": "cs0", "chain_length": 3},
    ],
    "switch_output": {
        "SW_1_0": {"chip_id": "sw1", "port_main": 5, "port_diverge": 6},
    },
}


@pytest.fixture
def client():
    """A one-switch layout behind the Flask test client."""
    network = RailwayNetwork()
    # A three-way tile at (1, 0): EW through, NS diverging.
    network.add_track(Tile(x=0, y=0, track_bits=TRACK_EW))
    network.add_track(Tile(x=1, y=0, track_bits=TRACK_EW | TRACK_NS | TRACK_NW))
    network.add_track(Tile(x=2, y=0, track_bits=TRACK_EW))
    switch = Switch(switch_id="SW_1_0", x=1, y=0)
    network.switches["SW_1_0"] = switch
    network.switch_manager = SwitchManager(switches=network.switches, network=network)

    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    network.switch_manager.simulation_controller = sim_controller
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
        yield test_client, state, switch


def _throw(test_client, position, switch_id="SW_1_0"):
    return test_client.post(
        "/api/hardware/switch-test",
        json={"config": CONFIG, "switch_id": switch_id, "position": position},
    )


# ------------------------------------------------------- the model follows


def test_the_test_throw_moves_the_switchs_own_position(client):
    test_client, _state, switch = client
    assert int(switch.position) == 0

    resp = _throw(test_client, 1)

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert int(switch.position) == 1


def test_alternating_test_throws_leave_the_model_on_the_last_side(client):
    """What the bench test actually does — main, diverge, main — and where the
    model has to be when the operator presses Stop."""
    test_client, _state, switch = client

    for position in (0, 1, 0, 1, 0):
        assert _throw(test_client, position).get_json()["ok"] is True

    assert int(switch.position) == 0


# ------------------------------------ the running session owns its own coils


def test_a_live_switch_output_drives_the_throw_itself(client):
    """With a session holding the controller and a switch driver on it, the
    throw goes through the runtime: no second handle, and the driver pulses the
    coils exactly as it does for an operating throw."""
    test_client, state, switch = client

    # Boot a develop-mode session, as bootstrap does: the real-mode stack
    # (SwitchDriver and all) on the in-memory fake device.
    hardware_config.apply_updates(CONFIG, persist=False)
    _inp, _blk, sw_iface = hardware_config.build_session_interfaces()
    from hardware.interfaces import SwitchDriver

    assert isinstance(sw_iface, SwitchDriver)
    state.sim_controller.switch_output_interface = sw_iface
    state.network.switch_output_interface = sw_iface

    body = _throw(test_client, 1).get_json()

    assert body["ok"] is True
    assert body["driver"] == "runtime"
    # The driver pulsed the diverge coil, and its record says so.
    assert sw_iface.get_last_throw("SW_1_0") == 1
    assert int(switch.position) == 1


def test_without_a_live_output_the_commissioning_session_pulses_the_coils(client):
    test_client, _state, switch = client

    body = _throw(test_client, 1).get_json()

    assert body["ok"] is True
    assert body["driver"] == "commission"
    assert int(switch.position) == 1


# ----------------------------------------- the state the bench actually runs in


def test_the_bench_throw_works_while_no_environment_is_active(client):
    """The state every bench throw happens in.

    Commissioning takes the device by deactivating the environment, so
    `environment_active` is False for the whole time the switch editor's Test is
    usable. Recording where the points went is not a throw command — the coil is
    already pulsed — so it must not be refused for running with no environment
    to command."""
    test_client, state, switch = client
    state.sim_controller.environment_active = False

    body = _throw(test_client, 1).get_json()

    assert body["ok"] is True, body["error"]
    assert body["driver"] == "commission"
    assert int(switch.position) == 1


def test_the_alternating_bench_test_runs_past_its_first_throw(client):
    """What the panel's Test button does: throw a side every second until the
    operator stops it. A single non-ok answer stops that loop, so a refusal on
    throw #1 leaves a "test" that fires one coil and quits — which is a test that
    can never show the operator both sides of their wiring."""
    test_client, state, switch = client
    state.sim_controller.environment_active = False

    for position in (0, 1, 0, 1):
        body = _throw(test_client, position).get_json()
        assert body["ok"] is True, body["error"]

    assert int(switch.position) == 1


def test_the_bench_throw_pulses_the_port_the_binding_names(client, monkeypatch):
    """Each side fires the port bound to it, so re-picking a port is how an
    operator maps a coil to a direction."""
    test_client, state, _switch = client
    state.sim_controller.environment_active = False
    from hardware import commission

    pulses = []
    session = commission.get_session()
    monkeypatch.setattr(
        session,
        "pulse_output",
        lambda chip_id, port, **kw: pulses.append((chip_id, port, kw.get("off_port"))),
    )
    monkeypatch.setattr(session, "open", lambda *a, **kw: None)

    _throw(test_client, 0)
    _throw(test_client, 1)

    assert pulses == [("sw1", 5, 6), ("sw1", 6, 5)]


def test_a_bench_throw_does_not_drive_the_coil_a_second_time(client):
    """The bench pulsed the coil itself, on chips commissioning has taken. The
    model write that follows must not reach an output as well."""
    test_client, state, _switch = client
    state.sim_controller.environment_active = False
    thrown = []
    state.sim_controller.switch_power_controller.set_throw = (
        lambda switch_id, target: thrown.append((switch_id, target)) or True
    )

    assert _throw(test_client, 1).get_json()["ok"] is True

    assert thrown == []


# ------------------------------------------------------------------ refusals


def test_a_failed_pulse_leaves_the_model_alone(client):
    """The pulse comes first for a reason: a coil that never fired must not
    leave the app claiming an alignment nothing moved to."""
    test_client, _state, switch = client
    from hardware import commission

    class RefusingSession(commission.CommissionSession):
        def pulse_output(self, *args, **kwargs):
            raise RuntimeError("bridge went away")

    original = commission._session
    commission._session = RefusingSession()
    try:
        body = _throw(test_client, 1).get_json()
    finally:
        commission._session = original

    assert body["ok"] is False
    assert "bridge went away" in body["error"]
    assert int(switch.position) == 0


def test_an_unbound_switch_is_refused_rather_than_silently_moved(client):
    test_client, _state, switch = client
    resp = test_client.post(
        "/api/hardware/switch-test",
        json={
            "config": {**CONFIG, "switch_output": {}},
            "switch_id": "SW_1_0",
            "position": 1,
        },
    )

    assert resp.status_code == 400
    assert "no coil binding" in resp.get_json()["error"]
    assert int(switch.position) == 0


def test_an_unknown_switch_is_404(client):
    test_client, _state, _switch = client
    resp = _throw(test_client, 1, switch_id="SW_NOPE")
    assert resp.status_code == 404


def test_a_bad_position_is_400(client):
    test_client, _state, _switch = client
    resp = test_client.post(
        "/api/hardware/switch-test",
        json={"config": CONFIG, "switch_id": "SW_1_0", "position": 7},
    )
    assert resp.status_code == 400
