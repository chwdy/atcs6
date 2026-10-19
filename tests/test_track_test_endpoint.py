"""The block editor's track Test sweep shows up on the layout while it runs.

`POST /api/hardware/track-test` drives a block's two bound rails to full voltage
one way, off, then full voltage the other way. It used to do that without telling
the model anything, so an operator who had the block-voltage debug overlay on
watched a loco run up and back while the layout drew the block at 0.0 V through
the whole test — the same disagreement `POST /api/hardware/switch-test` was fixed
for when a test throw moved the points and left `Switch.position` behind.

So the sweep mirrors each leg onto the applied-output store
(`BlockPowerController.get_recorded_voltage`) as it starts, in the
nominal sign the runtime commands for those rails, and drops it back to zero when
the rails go dead. The sweep resolves the rail binding's `reversed` flag the way
the driver does, so the first leg is nominally positive on every block and the
mirror never contradicts the rails it just drove.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_track_test_endpoint.py -v
"""

import threading
from datetime import datetime

import pytest

import hardware_config
from app_factory import create_app
from app_state import AppState
from hardware import self_test
from hardware.chips.pca9685 import PCA9685
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from tests.segment_test_helpers import make_segment_block


@pytest.fixture(autouse=True)
def _isolated_hw_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )
    hardware_config.reset_to_defaults()
    yield
    hardware_config.reset_to_defaults()


@pytest.fixture(autouse=True)
def _no_dwell(monkeypatch):
    """Each leg is held a second on the bench; the mirror is what is under test,
    not the wait, and the sweep reports before each sleep."""
    monkeypatch.setattr(self_test, "TRACK_TEST_HOLD_S", 0.0)


# Develop mode: the real-mode stack against the in-memory fake device, so the
# endpoint runs its whole path with no rig attached.
CONFIG = {
    "enabled": True,
    "develop": True,
    "controller": {"kind": "ch347", "device_port": None},
    "chips": [{"chip_id": "pwm1", "kind": "pca9685", "i2c_address": 0x40}],
    "block_track": {"BLK_A": {"chip_id": "pwm1", "forward": 4, "reverse": 9}},
}


@pytest.fixture
def client():
    """A one-block layout behind the Flask test client."""
    network = RailwayNetwork()
    block = make_segment_block(network, "BLK_A")

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
    # The track direction sweep runs only in Setup (design_new.md "Hardware
    # setup", Track direction sweep).
    state.setup_active = True
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client, block, sim_controller.power_controller


def _sweep(test_client, block_id="BLK_A", config=None):
    return test_client.post(
        "/api/hardware/track-test",
        json={"config": config or CONFIG, "block_id": block_id},
    )


class _Legs(list):
    """The block's voltage as each of the sweep's three legs is held.

    The sweep sleeps once per leg with the rails in that leg's state, which is
    exactly the window `/api/network` polls into, so sampling on the sleep
    samples what the overlay would draw. Chip bring-up sleeps too, before any
    rail is driven, so only the last three samples are legs.
    """

    @property
    def legs(self):
        return self[-3:]


def _legs(monkeypatch, power_controller, block_id="BLK_A"):
    seen = _Legs()
    monkeypatch.setattr(
        self_test.time,
        "sleep",
        lambda _s: seen.append(power_controller.get_recorded_voltage(block_id)),
    )
    return seen


def _driven_channels(monkeypatch):
    """The PWM channels the sweep energizes, in the order it energizes them.

    Which physical rail goes hot first is the whole subject of the polarity
    flag, and the mirror alone cannot show it — a mirrored +12 V says the same
    thing whichever channel carries it."""
    driven = []
    original = PCA9685.set_duty_cycle

    def spy(self, channel, duty):
        if duty > 0:
            driven.append(channel)
        original(self, channel, duty)

    monkeypatch.setattr(PCA9685, "set_duty_cycle", spy)
    return driven


def test_sweep_energizes_the_block_on_the_layout(client, monkeypatch):
    test_client, block, pc = client
    seen = _legs(monkeypatch, pc)

    resp = _sweep(test_client)

    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert seen.legs == [12.0, 0.0, -12.0]


def test_sweep_leaves_the_block_de_energized(client):
    test_client, block, pc = client
    assert _sweep(test_client).get_json()["ok"] is True
    assert pc.get_recorded_voltage("BLK_A") == 0.0


def test_sweep_drives_a_reversed_block_the_way_the_driver_would(client, monkeypatch):
    """A block flipped to Reversed gets its two rails traded, exactly as the
    driver trades them resolving the same binding, so the operator who just
    flipped the block sweeps again and watches the loco run the right way — the
    sweep is the only check that control has. The mirror follows: the first leg
    drives whichever rail a nominal positive drives, so it reads +12 V here too.
    The flag rides in on the rail binding of the config payload the sweep drives
    (design_new.md "Power", Rail polarity lives on the rail binding)."""
    test_client, block, pc = client
    reversed_config = {
        **CONFIG,
        "block_track": {
            "BLK_A": {"chip_id": "pwm1", "forward": 4, "reverse": 9, "reversed": True}
        },
    }
    driven = _driven_channels(monkeypatch)
    seen = _legs(monkeypatch, pc)

    assert _sweep(test_client, config=reversed_config).get_json()["ok"] is True

    assert driven == [9, 4]                  # reverse rail first, not forward
    assert seen.legs == [12.0, 0.0, -12.0]


def test_sweep_drives_the_forward_rail_first_on_a_normal_block(client, monkeypatch):
    """The Normal counterpart, and the discovery case: a fresh binding is Normal,
    so a block wired the wrong way round names itself by running the loco
    backwards on the first leg."""
    test_client, _block, _pc = client
    driven = _driven_channels(monkeypatch)

    assert _sweep(test_client).get_json()["ok"] is True

    assert driven == [4, 9]


def test_unbound_block_is_reported_and_touches_no_voltage(client):
    test_client, block, pc = client
    pc.set_recorded_voltage("BLK_A", 5.0)

    resp = _sweep(test_client, block_id="BLK_A_MISSING")

    assert resp.get_json()["ok"] is False
    assert pc.get_recorded_voltage("BLK_A") == 5.0
