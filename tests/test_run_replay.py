"""Record a run, then replay it from its detector reading log alone.

design_new.md "Detector reading log": the server's first line records what it
booted with (`start_state`), and every state-changing request gets a line with
the pass it ran after. `tools/reading_replay.py`'s `RunReplay` rebuilds a server
from that record, sends each command at the same point in the run, and plays
the recorded readings.

The recording here is made on Virtual Hardware, with the rig's own layout and
hardware setup (`tests/replay/demo7_1-rig-layout.json`), the way the server
runs it: boot, activate, two locomotives put on the detectors, both adopted,
one powered on and off, and one request the server refuses. The replay must:

- answer every command the way the recorded server did;
- end with the same trains on the same blocks;
- read, pass by pass, exactly what the recorded run read;
- record each command after the same pass.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import bootstrap
import hardware_config
import operation_environment
from app_factory import create_app
from app_state import AppState
from detector_reading_log import SimulationLock
from enums import OperationMode
from network import RailwayNetwork
from persistence import PersistenceManager
from runtime import PASS_STOPPED, PASS_TICK, runtime_pass
from tools import cut_reading_log, reading_replay
from tools.reading_replay import RunReplay, load_passes, load_run, read_lines

RIG_LAYOUT = Path(__file__).parent / "replay" / "demo7_1-rig-layout.json"
WEST_BLOCK = "BLK_4F4CF0"
EAST_BLOCK = "BLK_4F6CD7"
# Virtual Hardware starts with one train on its first bound block
# (`hardware.virtual_rig.seed_startup_presence`); the recording lifts it first.
SEEDED_BLOCK = "BLK_4F7A2F"
TICK = 0.1


class Server:
    """A server booted the way `server.py` boots it, with the runtime loop's
    passes run by hand."""

    def __init__(self, tmp_path, monkeypatch):
        rig = json.loads(RIG_LAYOUT.read_text())
        hardware_config.reset_to_defaults()
        hardware_config.apply_snapshot(rig["hardware"])
        operation_environment.select(OperationMode.VIRTUAL_HARDWARE)
        network = RailwayNetwork()
        network.from_dict(rig["snapshot"])
        monkeypatch.setattr(bootstrap, "create_empty_network", lambda: network)
        self.log_dir = tmp_path / "readings"
        network, dispatcher, sim, _pm = bootstrap.bootstrap_runtime(
            demo=False, setup_train=False, detector_reading_log_dir=self.log_dir
        )
        lock = SimulationLock()
        self.state = AppState(
            network=network,
            path_dispatcher=dispatcher,
            sim_controller=sim,
            persistence_manager=PersistenceManager(storage_dir=str(tmp_path / "layouts")),
            simulation_lock=lock,
            simulation_running=False,
            simulation_speed=1.0,
            last_update_time=datetime.now(),
        )
        lock.count_passes_with(lambda: self.state.sim_controller.detector_reading_log.pass_count)
        app = create_app(self.state)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def passes(self, n):
        for _ in range(n):
            which = runtime_pass(self.state)
            with self.state.simulation_lock:
                if which == PASS_TICK:
                    self.state.sim_controller.update(TICK, operating_delta_seconds=TICK)
                elif which == PASS_STOPPED:
                    self.state.sim_controller.poll_hardware_occupancy()

    def files(self):
        self.state.sim_controller.detector_reading_log.close()
        return sorted(self.log_dir.glob("readings-*.jsonl"))


def held(network):
    return {
        train_id: (
            train.is_unidentified,
            sorted(b for b, block in network.blocks.items() if block.occupied_by == train_id),
        )
        for train_id, train in network.trains.items()
    }


def unidentified(network):
    return sorted(t for t, train in network.trains.items() if train.is_unidentified)


def adopt_body(new_train_id, head_tile, direction):
    return {
        "new_train_id": new_train_id,
        "length_cm": 10,
        "max_speed": 100,
        "direction": direction,
        "head_tile": {"x": head_tile[0], "y": head_tile[1]},
    }


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    client = server.client

    server.passes(3)  # Halted, not activated: no detector is read yet
    assert client.post("/api/operation/activate").status_code == 200
    for block_id, present in ((SEEDED_BLOCK, False), (WEST_BLOCK, True), (EAST_BLOCK, True)):
        response = client.put(
            "/api/virtual-hardware/input/presence", json={"block_id": block_id, "present": present}
        )
        assert response.status_code == 200, response.get_json()
    server.passes(5)
    [first] = unidentified(server.state.network)

    assert client.post(
        f"/api/trains/{first}/adopt", json=adopt_body("TRAIN_A", (7, 6), "SOUTH")
    ).status_code == 200
    server.passes(3)
    [second] = unidentified(server.state.network)
    assert client.post(
        f"/api/trains/{second}/adopt", json=adopt_body("TRAIN_B", (9, 13), "WEST")
    ).status_code == 200
    # Refused: the train is no longer unidentified. Its answer is recorded too.
    assert client.post(
        f"/api/trains/TRAIN_A/adopt", json=adopt_body("TRAIN_C", (7, 6), "SOUTH")
    ).status_code == 400
    server.passes(2)
    assert client.post("/api/trains/TRAIN_A/power-on").status_code == 200
    server.passes(4)
    assert client.post("/api/trains/TRAIN_A/power-off").status_code == 200
    server.passes(4)

    return server, held(server.state.network), server.files()


def test_the_log_records_the_start_and_every_command(recorded):
    _server, _end, files = recorded
    lines = [line for f in files for line in read_lines(f)]
    first_pass = next(line for line in lines if "command" not in line)
    assert first_pass["full"] == "start"
    start = first_pass["start_state"]
    assert start["selected_environment"] == "virtual_hardware"
    assert start["trains_on_track"] == []
    assert set(start["layout"]["blocks"]) >= {WEST_BLOCK, EAST_BLOCK}
    assert start["hardware"]["block_occupancy"]

    commands = [line for line in lines if "command" in line]
    assert [(c["command"]["method"], c["command"]["path"], c["command"]["status"]) for c in commands] == [
        ("POST", "/api/operation/activate", 200),
        ("PUT", "/api/virtual-hardware/input/presence", 200),
        ("PUT", "/api/virtual-hardware/input/presence", 200),
        ("PUT", "/api/virtual-hardware/input/presence", 200),
        ("POST", "/api/trains/UNIDENT_1/adopt", 200),
        ("POST", "/api/trains/UNIDENT_2/adopt", 200),
        ("POST", "/api/trains/TRAIN_A/adopt", 400),
        ("POST", "/api/trains/TRAIN_A/power-on", 200),
        ("POST", "/api/trains/TRAIN_A/power-off", 200),
    ]
    assert [c["command_no"] for c in commands] == list(range(1, 10))
    # Activation came before the first detector read, so it ran after pass 0;
    # the first adoption ran after the fifth pass that read the detectors.
    assert commands[0]["after_pass"] == 0
    assert commands[4]["after_pass"] == 5


def test_a_replay_answers_every_command_and_ends_the_same(recorded, tmp_path):
    _server, end, files = recorded
    run = load_run(*files)
    with RunReplay(run, tmp_path / "replay") as replay:
        replay.play_to()
        assert replay.divergences == []
        assert [c.path for c in replay.skipped] == ["/api/virtual-hardware/input/presence"] * 3
        assert held(replay.network) == end
    assert end == {"TRAIN_A": (False, [EAST_BLOCK]), "TRAIN_B": (False, [WEST_BLOCK])}


def test_a_replay_reads_what_the_run_read_and_records_the_same_commands(recorded, tmp_path):
    _server, _end, files = recorded
    replay_dir = tmp_path / "replay-readings"
    with RunReplay(load_run(*files), tmp_path / "replay", reading_log_dir=replay_dir) as replay:
        replay.play_to()
    [replayed_file] = sorted(replay_dir.glob("readings-*.jsonl"))

    original = load_passes(*files)
    replayed = load_passes(replayed_file)
    assert [(p.pass_no, p.pass_kind, p.readings) for p in replayed] == [
        (p.pass_no, p.pass_kind, p.readings) for p in original
    ]

    def sent(lines, skip_device=True):
        return [
            (line["after_pass"], line["command"]["path"], line["command"]["status"])
            for line in lines
            if "command" in line
            and not (skip_device and line["command"]["path"].startswith("/api/virtual-hardware/"))
        ]

    original_lines = [line for f in files for line in read_lines(f)]
    assert sent(read_lines(replayed_file)) == sent(original_lines)


def test_a_replay_can_stop_part_way_for_the_test_to_look(recorded, tmp_path):
    _server, _end, files = recorded
    with RunReplay(load_run(*files), tmp_path / "replay") as replay:
        replay.play_to(5)
        [only] = unidentified(replay.network)
        assert held(replay.network) == {only: (True, [WEST_BLOCK, EAST_BLOCK])}
        replay.play_to()
        assert replay.divergences == []


def test_a_log_with_no_start_state_is_refused(tmp_path):
    cut = Path(__file__).parent / "replay" / "2026-11-07-two-locos-at-start.jsonl"
    with pytest.raises(ValueError, match="carrying `start_state`"):
        load_run(cut)


def test_a_run_cut_short_replays_from_the_command_line(recorded, tmp_path, capsys):
    _server, _end, files = recorded
    cut = tmp_path / "cut.jsonl"
    cut_reading_log.main([*map(str, files), "--to-pass", "9", "-o", str(cut)])
    capsys.readouterr()

    assert reading_replay.main([str(cut)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "replayed 9 passes and 4 commands (3 device commands not sent)"
    assert "DIVERGED" not in "\n".join(printed)
    assert printed[1:] == [
        "train        TRAIN_A          " + EAST_BLOCK,
        "train        TRAIN_B          " + WEST_BLOCK,
    ]


def test_a_command_answered_differently_is_reported(recorded, tmp_path):
    _server, _end, files = recorded
    run = load_run(*files)
    # Pretend the rig refused the first adoption: the replay accepts it.
    run.commands[4] = reading_replay.RecordedCommand(
        **{**run.commands[4].__dict__, "status": 400}
    )
    with RunReplay(run, tmp_path / "replay") as replay:
        replay.play_to()
    [text] = replay.divergences
    assert "command 5 (POST /api/trains/UNIDENT_1/adopt, after pass 5)" in text
    assert "the rig answered 400, the replay answered 200" in text
