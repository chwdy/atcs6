"""The v2 runtime step connected to the loop, the requests the page sends,
and the snapshot fields (design_v2.md "Runtime loop", "Running a train",
"Live fields").

The detectors are a scripted stand-in: the test says which blocks read
occupied, and the loop runs real passes on the rig layout.
"""

import logging
import threading
from datetime import datetime

import pytest

import demo7_1
from app_factory import create_app
from app_state import AppState
from block_occupancy import OccupancyInput
from block_power import SimulatedTrackDriver
from enums import OperationMode, SwitchState
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from runtime import run_loop_iteration
from simulation import SimulationController
from switch_power import SimulatedSwitchDriver


class ScriptedDetectors(OccupancyInput):
    def __init__(self, network):
        self.network = network
        self.occupied = set()

    def detected_block_ids(self):
        return set(self.network.blocks)

    def read_block_currents(self):
        return {block_id: block_id in self.occupied for block_id in self.network.blocks}


class Session:
    def __init__(self, tmp_path):
        logging.disable(logging.CRITICAL)
        try:
            self.network = demo7_1.create_comprehensive_demo()
        finally:
            logging.disable(logging.NOTSET)
        self.ids = {b.name: block_id for block_id, b in self.network.blocks.items()}
        dispatcher = PathDispatcher(self.network)
        self.sim = SimulationController(self.network, dispatcher)
        self.detectors = ScriptedDetectors(self.network)
        self.track = SimulatedTrackDriver()
        self.sim.activate_interfaces(
            OperationMode.VIRTUAL_HARDWARE, self.detectors, self.track, SimulatedSwitchDriver()
        )
        self.sim.finish_activation()
        for switch in self.network.switches.values():
            switch.position = SwitchState.MAIN
        self.state = AppState(
            network=self.network,
            path_dispatcher=dispatcher,
            sim_controller=self.sim,
            persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
            simulation_lock=threading.Lock(),
            simulation_running=True,
            simulation_speed=1.0,
            last_update_time=datetime.now(),
        )
        app = create_app(self.state)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def passes(self, *occupied, count=3):
        self.detectors.occupied = {self.ids[name] for name in occupied}
        for _ in range(count):
            run_loop_iteration(self.state, 0.1)

    def volts(self, name):
        return self.track.get_actual_voltage(self.ids[name])

    def snapshot(self):
        return self.client.get("/api/network").get_json()


@pytest.fixture
def session(tmp_path):
    return Session(tmp_path)


def test_a_live_session_runs_the_train_step(session):
    assert session.state.session_state().is_live
    session.passes("Left")
    assert list(session.sim.train_runtime.trains) == ["T1"]

    reply = session.client.put("/api/trains/T1/knob", json={"volts": 6})
    assert reply.status_code == 200 and reply.get_json() == {"success": True, "reason": None}
    session.passes("Left")
    assert session.volts("Left") == 6.0 and session.volts("Main") == 6.0
    assert session.volts("Up") == 0.0


def test_the_snapshot_carries_the_live_fields(session):
    session.passes("Left")
    session.client.put("/api/trains/T1/knob", json={"volts": 6})
    session.passes("Left")
    snap = session.snapshot()
    blocks = snap.get("blocks") or snap.get("live_blocks")
    left, main = blocks[session.ids["Left"]], blocks[session.ids["Main"]]
    assert left["reads_occupied"] is True and left["train_id"] == "T1"
    assert left["voltage"] == 6.0
    assert main["reads_occupied"] is False and main["claimed_by_train_id"] == "T1"
    switches = snap.get("switches") or snap.get("live_switches")
    assert switches["SW_7_5"]["held"] is True
    assert switches["SW_11_9"]["held"] is False
    assert snap["running_trains"]["T1"]["state"] == "moving"


def test_requests_are_refused_with_their_reason(session):
    session.passes("Left")
    put = session.client.put
    post = session.client.post
    assert put("/api/trains/T9/knob", json={"volts": 6}).get_json()["reason"] == "unknown_train"
    assert put("/api/trains/T1/knob", json={"volts": 13}).get_json()["reason"] == "out_of_range"
    put("/api/trains/T1/knob", json={"volts": 6})
    reply = post("/api/trains/T1/reverse")
    assert reply.status_code == 400 and reply.get_json()["reason"] == "not_stopped"
    assert session.client.delete("/api/trains/T1").get_json()["reason"] == "not_stopped"


def test_stop_reverse_and_remove(session):
    session.passes("Left")
    session.client.put("/api/trains/T1/knob", json={"volts": 6})
    assert session.client.post("/api/trains/T1/stop").get_json() == {"success": True, "reason": None}
    session.passes("Left")
    assert session.volts("Left") == 0.0
    assert session.client.post("/api/trains/T1/reverse").get_json()["success"] is True
    assert session.sim.train_runtime.trains["T1"].direction == "reverse"
    assert session.client.delete("/api/trains/T1").get_json()["success"] is True
    assert session.sim.train_runtime.trains == {}


def test_stop_all_zeroes_the_track_at_once(session):
    session.passes("Left")
    session.client.put("/api/trains/T1/knob", json={"volts": 6})
    session.passes("Left")
    assert session.volts("Left") == 6.0
    assert session.client.post("/api/trains/stop-all").get_json()["success"] is True
    assert session.volts("Left") == 0.0 and session.volts("Main") == 0.0


def test_a_held_switch_is_not_thrown(session):
    session.passes("Left")
    session.client.put("/api/trains/T1/knob", json={"volts": 6})
    session.passes("Left")
    reply = session.client.put("/api/switches/SW_7_5/state", json={"state": 1})
    assert reply.status_code == 409 and reply.get_json()["reason"] == "switch_held"
    assert int(session.network.switches["SW_7_5"].position) == 0
    reply = session.client.put("/api/switches/SW_11_9/state", json={"state": 1})
    assert reply.status_code == 202


def test_leaving_live_stops_every_train_and_keeps_them(session):
    session.passes("Left")
    session.client.put("/api/trains/T1/knob", json={"volts": 6})
    session.passes("Left")
    session.state.setup_active = True
    assert not session.state.session_state().is_live
    session.passes("Left")
    train = session.sim.train_runtime.trains["T1"]
    assert train.knob_volts == 0.0 and train.claim_block_id is None
    reply = session.client.put("/api/trains/T1/knob", json={"volts": 6})
    assert reply.get_json()["reason"] == "not_live"


def test_setup_shows_trains_before_going_live(session):
    session.state.setup_active = True
    session.passes("Left", "Right")
    assert len(session.sim.train_runtime.trains) == 2
    assert session.volts("Left") == 0.0


def test_an_activation_starts_with_no_trains(session):
    session.passes("Left")
    assert session.sim.train_runtime.trains
    session.sim.finish_activation()
    assert session.sim.train_runtime.trains == {}
