"""The detector reading log: one line per pass of what every block detector read.

design_new.md "Event log", Detector reading log. The event log records what the
runtime decided; this log records the readings it decided from, so a rig run
that went wrong can be read back pass by pass. These cover:

- the per-pass line (counter, pass kind, wall time, both clocks' deltas, readings);
- change-only writing, and the lines that list every block (first line, layout
  change, environment change, new file, after a failed write);
- what a hardware reading carries (source, zone, mA, raw bit, hold);
- that a failed write is reported once on the event stream and never stops the
  tick;
- the file limits (rotation and how many files are kept).

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_detector_reading_log.py -v
"""

from __future__ import annotations

import json

import pytest

import detector_reading_log as drl
from block import Block
from block_occupancy import OccupancyInput, SimulatedOccupancyDriver
from constants import OCCUPANCY_OFF_THRESHOLD, OCCUPANCY_ON_THRESHOLD
from detector_reading_log import (
    FULL_ENVIRONMENT,
    FULL_LAYOUT,
    FULL_NEW_FILE,
    FULL_START,
    FULL_WRITE_FAILURE,
    WRITE_FAILED_CONDITION_KEY,
    DetectorReadingLog,
    block_reading,
    read_detector_reading_log,
)
from entity_ids import segment_id_for_block
from enums import OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import TRACK_EW, Tile


class FakeDetector(OccupancyInput):
    """A hand-driven per-block current map, standing in for real detectors."""

    def __init__(self, block_ids):
        self.current = {block_id: False for block_id in block_ids}

    def read_block_currents(self):
        return dict(self.current)


def build_network(block_ids):
    """A straight E-W line, one block per tile."""
    network = RailwayNetwork()
    for x, block_id in enumerate(block_ids):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(block_id)
        network.add_track(track)
        block = Block(block_id=block_id, length_cm=100.0)
        block.ordered_tiles = [(x, 0, "W")]
        network.blocks[block_id] = block
    return network


def build_sim(tmp_path, block_ids=("BLK_0", "BLK_1", "BLK_2"), input_interface=None,
              mode=OperationMode.REAL_HARDWARE):
    network = build_network(block_ids)
    detector = input_interface if input_interface is not None else FakeDetector(block_ids)
    sim = SimulationController(
        network,
        PathDispatcher(network),
        operation_mode=mode,
        input_interface=detector,
    )
    sim._fake_clock = 0.0
    sim.unidentified_clock = lambda: sim._fake_clock
    sim.detector_reading_log.enable(tmp_path / "readings")
    return network, sim, detector


def lines_of(sim):
    return read_detector_reading_log(sim.detector_reading_log.path)


# ------------------------------------------------------------ off by default


def test_the_log_is_off_until_enabled():
    network = build_network(["BLK_0"])
    sim = SimulationController(network, PathDispatcher(network))
    sim.update(0.1)
    assert not sim.detector_reading_log.enabled
    assert sim.detector_reading_log.path is None


# --------------------------------------------------------- the per-pass line


def test_a_running_pass_writes_its_timing_and_every_reading_first(tmp_path):
    _network, sim, detector = build_sim(tmp_path)
    detector.current["BLK_1"] = True

    sim.update(0.1, operating_delta_seconds=0.25)

    [line] = lines_of(sim)
    assert line["pass"] == 1
    assert line["pass_kind"] == "tick"
    assert line["simulation_time"] == pytest.approx(0.1)
    assert line["delta_seconds"] == pytest.approx(0.1)
    # The uncapped delta the operating clock advanced by, never its reading.
    assert line["operating_delta_seconds"] == pytest.approx(0.25)
    assert "operating_time" not in line
    # The wall time the pass started, the same stamp the pass measured from.
    assert line["wall_time"] == sim._pass_started_wall_time
    # The first line lists every block the read returned, with its context.
    assert line["full"] == FULL_START
    assert line["environment"] == "real_hardware"
    assert line["on_threshold"] == OCCUPANCY_ON_THRESHOLD
    assert line["off_threshold"] == OCCUPANCY_OFF_THRESHOLD
    assert line["readings"] == {
        "BLK_0": {"present": False},
        "BLK_1": {"present": True},
        "BLK_2": {"present": False},
    }


def test_the_reading_is_recorded_before_the_debounce_confirms_it(tmp_path):
    # One present read is below the on-threshold, so the block is not confirmed
    # yet — but the log already shows what the detector said.
    assert OCCUPANCY_ON_THRESHOLD > 1
    _network, sim, detector = build_sim(tmp_path)
    sim.update(0.1)
    detector.current["BLK_2"] = True
    sim.update(0.1)

    assert "BLK_2" not in sim.block_occupancy_detector.confirmed_occupied
    assert lines_of(sim)[-1]["readings"] == {"BLK_2": {"present": True}}


def test_a_stopped_pass_writes_a_line_with_no_clock_advance(tmp_path):
    _network, sim, detector = build_sim(tmp_path)
    sim.update(0.1)
    detector.current["BLK_0"] = True

    sim.poll_hardware_occupancy()

    stopped = lines_of(sim)[-1]
    assert stopped["pass"] == 2
    assert stopped["pass_kind"] == "stopped"
    assert stopped["delta_seconds"] == 0.0
    assert stopped["operating_delta_seconds"] == 0.0
    assert stopped["simulation_time"] == pytest.approx(0.1)
    assert stopped["readings"] == {"BLK_0": {"present": True}}


# ------------------------------------------------------ change-only writing


def test_later_lines_carry_only_the_blocks_whose_reading_changed(tmp_path):
    _network, sim, detector = build_sim(tmp_path)
    sim.update(0.1)                         # full line
    sim.update(0.1)                         # nothing changed
    detector.current["BLK_1"] = True
    sim.update(0.1)                         # BLK_1 came on
    sim.update(0.1)                         # nothing changed
    detector.current["BLK_1"] = False
    detector.current["BLK_2"] = True
    sim.update(0.1)                         # BLK_1 off, BLK_2 on

    lines = lines_of(sim)
    assert [line["pass"] for line in lines] == [1, 2, 3, 4, 5]
    assert "full" in lines[0]
    assert all("full" not in line for line in lines[1:])
    assert "readings" not in lines[1]
    assert lines[2]["readings"] == {"BLK_1": {"present": True}}
    assert "readings" not in lines[3]
    assert lines[4]["readings"] == {
        "BLK_1": {"present": False},
        "BLK_2": {"present": True},
    }


def test_replaying_the_lines_rebuilds_every_pass_reading(tmp_path):
    # The change-only file loses nothing: folding the lines in order gives back
    # exactly what each pass read.
    _network, sim, detector = build_sim(tmp_path)
    script = [
        {"BLK_0": True}, {"BLK_0": True}, {"BLK_0": True, "BLK_1": True},
        {"BLK_1": True}, {}, {"BLK_2": True},
    ]
    expected = []
    for on in script:
        detector.current = {b: b in on for b in ("BLK_0", "BLK_1", "BLK_2")}
        sim.update(0.1)
        expected.append(dict(detector.current))

    state = {}
    rebuilt = []
    for line in lines_of(sim):
        if "full" in line:
            state = {}
        for block_id, reading in line.get("readings", {}).items():
            state[block_id] = reading["present"]
        rebuilt.append(dict(state))
    assert rebuilt == expected


def test_a_requested_layout_change_lists_every_block_again(tmp_path):
    _network, sim, _detector = build_sim(tmp_path)
    sim.update(0.1)
    sim.update(0.1)

    sim.detector_reading_log.request_full(FULL_LAYOUT)
    sim.update(0.1)

    line = lines_of(sim)[-1]
    assert line["full"] == FULL_LAYOUT
    assert set(line["readings"]) == {"BLK_0", "BLK_1", "BLK_2"}


def test_a_changed_set_of_read_blocks_lists_every_block_again(tmp_path):
    network, sim, detector = build_sim(tmp_path)
    sim.update(0.1)

    # A block added to the layout starts appearing in the read.
    block = Block(block_id="BLK_3", length_cm=100.0)
    block.ordered_tiles = [(3, 0, "W")]
    network.blocks["BLK_3"] = block
    detector.current["BLK_3"] = False
    sim.update(0.1)

    line = lines_of(sim)[-1]
    assert line["full"] == FULL_LAYOUT
    assert set(line["readings"]) == {"BLK_0", "BLK_1", "BLK_2", "BLK_3"}


def test_a_new_environment_lists_every_block_again(tmp_path):
    network, sim, _detector = build_sim(tmp_path)
    sim.update(0.1)

    # Activation rebinds the boundary and builds a fresh detector.
    replacement = FakeDetector(network.blocks)
    replacement.current["BLK_0"] = True
    sim.activate_interfaces(OperationMode.VIRTUAL_HARDWARE, replacement, None, None)
    sim.update(0.1)

    line = lines_of(sim)[-1]
    assert line["full"] == FULL_ENVIRONMENT
    assert line["environment"] == "virtual_hardware"
    assert line["readings"]["BLK_0"] == {"present": True}


def test_the_simulated_driver_is_recorded_too(tmp_path):
    network = build_network(["BLK_0", "BLK_1"])
    sim = SimulationController(network, PathDispatcher(network))
    assert isinstance(sim.input_interface, SimulatedOccupancyDriver)
    sim.detector_reading_log.enable(tmp_path)
    sim.update(0.1)

    [line] = lines_of(sim)
    assert line["environment"] == "simulation"
    assert line["readings"] == {
        "BLK_0": {"present": False},
        "BLK_1": {"present": False},
    }


# ------------------------------------------------------- hardware readings


def _real_occupancy_input():
    """A real `OccupancyDriver` over the fake CH347: B1 bound to a '165 bit and
    an INA3221 channel with a 15 mA threshold."""
    from hardware.fake import FakeCh347Device
    from hardware.interfaces import build_real_interfaces
    from hardware_config import ChipConfig

    dev = FakeCh347Device(present_addresses={0x40, 0x41})
    specs = [
        ChipConfig("pwm1", "pca9685", i2c_address=0x40),
        ChipConfig("in1", "hc165", spi_select="cs1"),
        ChipConfig("cur1", "ina3221", i2c_address=0x41),
    ]
    inp, blk, _sw = build_real_interfaces(
        dev, specs,
        block_track={"B1": ("pwm1", 0, 1)},
        block_occupancy={"B1": ("in1", 3)},
        switch_output={},
        block_occupancy_current={"B1": ("cur1", 0, 0.0, 0.015)},
        idle_deadband_v=0.25,
        current_sense_min_drive_v=4.0,
    )
    return dev, inp, blk


def test_a_hardware_reading_carries_its_source_and_measured_value(tmp_path):
    dev, inp, blk = _real_occupancy_input()
    _network, sim, _ = build_sim(tmp_path, block_ids=("B1",), input_interface=inp)

    # Driven: the current channel decides and its mA is recorded.
    blk.set_voltage("B1", 9.0)
    dev.set_ina3221_current(0x41, 1, 0.01283)       # 12.83 mA, under the 15 mA trip
    sim.update(0.1)
    driven = lines_of(sim)[-1]["readings"]["B1"]
    assert driven == {
        "present": False,
        "source": "current",
        "zone": "driven",
        "current_ma": 12.8,
        "threshold_ma": 15.0,
    }

    # Idle: the '165 decides and its raw bit is recorded.
    blk.set_voltage("B1", 0.0)
    dev.set_input_bit("cs1", 3, True, width=1)
    sim.update(0.1)
    idle = lines_of(sim)[-1]["readings"]["B1"]
    assert idle == {
        "present": True,
        "source": "digital",
        "zone": "idle",
        "raw_bit": True,
    }


def test_block_reading_keeps_only_the_reading_fields():
    detail = {
        "zone": "lighting", "driven": False, "commanded_volts": 1.5,
        "source": "current", "current_ma": 4.349, "threshold_ma": 10.0,
        "configured_threshold_ma": 10.0, "baseline_ma": 0.0, "held_zone": True,
        "clear_count": 3, "clear_hold_reads": 30, "held_from_previous": True,
    }
    assert block_reading(True, detail) == {
        "present": True,
        "source": "current",
        "zone": "lighting",
        "current_ma": 4.3,
        "threshold_ma": 10.0,
        "held_from_previous": True,
    }


# ------------------------------------------- a failed write never stops the tick


class _BrokenFile:
    def write(self, _text):
        raise OSError(28, "No space left on device")

    def close(self):
        pass


def _failing_open(*_args, **_kwargs):
    raise OSError(28, "No space left on device")


def _write_failure_events(sim):
    return [
        n for n in sim.notification_center.peek_recent(200)
        if n.source == "DetectorReadingLog" and n.operation == "record_pass"
    ]


def test_a_failed_write_is_reported_once_and_the_tick_carries_on(tmp_path, monkeypatch):
    _network, sim, detector = build_sim(tmp_path)
    sim.update(0.1)
    log = sim.detector_reading_log

    # The disk fills: the open file and every reopen fail from here on.
    log._file = _BrokenFile()
    monkeypatch.setattr(drl, "open", _failing_open, raising=False)
    detector.current["BLK_1"] = True
    for _ in range(OCCUPANCY_ON_THRESHOLD + 2):
        sim.update(0.1)

    # The ticks ran: the clock advanced and the detector confirmed the block.
    assert sim.simulation_time == pytest.approx(0.1 * (OCCUPANCY_ON_THRESHOLD + 3))
    assert "BLK_1" in sim.block_occupancy_detector.confirmed_occupied
    assert not sim.runtime_halted

    # One warning for the whole failing stretch.
    [event] = _write_failure_events(sim)
    assert event.kind == "warning"
    assert event.error is not None
    assert "No space left on device" in event.error.text
    assert WRITE_FAILED_CONDITION_KEY in sim.notification_center._active_conditions

    # The disk recovers: the next line restates every block, the gap in `pass`
    # shows the lost lines, and the condition is resolved.
    monkeypatch.undo()
    sim.update(0.1)
    lines = lines_of(sim)
    assert [line["pass"] for line in lines] == [1, OCCUPANCY_ON_THRESHOLD + 4]
    assert lines[-1]["full"] == FULL_WRITE_FAILURE
    assert lines[-1]["readings"]["BLK_1"] == {"present": True}
    assert WRITE_FAILED_CONDITION_KEY not in sim.notification_center._active_conditions

    # A later failure is a new stretch, so it is reported again.
    log._file = _BrokenFile()
    monkeypatch.setattr(drl, "open", _failing_open, raising=False)
    sim.update(0.1)
    assert len(_write_failure_events(sim)) == 2


def test_a_driver_detail_that_raises_does_not_stop_the_tick(tmp_path):
    class _BadDetail(FakeDetector):
        def last_read_detail(self):
            raise RuntimeError("detail unavailable")

    _network, sim, _ = build_sim(
        tmp_path, input_interface=_BadDetail(("BLK_0", "BLK_1", "BLK_2"))
    )
    sim.update(0.1)
    sim.update(0.1)

    assert sim.simulation_time == pytest.approx(0.2)
    assert len(_write_failure_events(sim)) == 1


def test_a_directory_that_cannot_be_made_leaves_the_log_off(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("a file where the directory should be")
    network = build_network(["BLK_0"])
    sim = SimulationController(network, PathDispatcher(network))

    sim.detector_reading_log.enable(blocker / "readings")
    sim.update(0.1)

    assert not sim.detector_reading_log.enabled
    assert sim.simulation_time == pytest.approx(0.1)
    assert len(_write_failure_events(sim)) == 1


# ------------------------------------------------------------- file limits


class _Detector:
    """The two things the log reads from a detector."""

    on_threshold = OCCUPANCY_ON_THRESHOLD
    off_threshold = OCCUPANCY_OFF_THRESHOLD

    def __init__(self, raw):
        self.last_raw = raw
        self.input_interface = object()


def _record(log, detector, n):
    for i in range(n):
        log.record_pass(
            pass_kind="tick", wall_time=1000.0 + i, simulation_time=0.1 * i,
            delta_seconds=0.1, operating_delta_seconds=0.1,
            detector=detector, environment="simulation",
        )


def test_a_full_file_is_closed_and_the_next_one_opens_with_every_block(tmp_path):
    log = DetectorReadingLog(max_file_bytes=400, max_total_bytes=1_000_000)
    log.enable(tmp_path)
    first = log.path
    detector = _Detector({"B1": True, "B2": False})
    _record(log, detector, 6)

    assert log.path != first
    files = sorted(tmp_path.glob("readings-*.jsonl"))
    assert len(files) >= 2
    opening = [read_detector_reading_log(p)[0] for p in files]
    assert opening[0]["full"] == FULL_START
    assert all(line["full"] == FULL_NEW_FILE for line in opening[1:])
    assert all(set(line["readings"]) == {"B1", "B2"} for line in opening)


def test_the_oldest_files_are_deleted_to_stay_inside_the_byte_budget(tmp_path):
    # Every pass fills its file, so every pass opens a new one and prunes.
    budget = 500
    log = DetectorReadingLog(max_file_bytes=1, max_total_bytes=budget)
    log.enable(tmp_path)
    detector = _Detector({"B1": True})
    opened = [log.path]
    for _ in range(8):
        _record(log, detector, 1)
        if log.path != opened[-1]:
            opened.append(log.path)

    kept = sorted(str(p) for p in tmp_path.glob("readings-*.jsonl"))
    assert log.path in kept
    # What survives is the newest run of files, and before the current file
    # was opened they fit the budget.
    assert kept == sorted(opened[-len(kept):])
    assert 1 < len(kept) < len(opened)
    older = [p for p in kept if p != log.path]
    assert sum((tmp_path / p).stat().st_size for p in older) <= budget


def test_every_line_is_one_json_object(tmp_path):
    _network, sim, detector = build_sim(tmp_path)
    for i in range(5):
        detector.current["BLK_0"] = i % 2 == 0
        sim.update(0.1)
    with open(sim.detector_reading_log.path, encoding="utf-8") as f:
        raw_lines = f.read().splitlines()
    assert len(raw_lines) == 5
    for text in raw_lines:
        assert isinstance(json.loads(text), dict)


def test_bootstrap_enables_the_log_only_when_given_a_directory(tmp_path):
    from bootstrap import bootstrap_runtime

    _n, _d, off, _p = bootstrap_runtime(demo=False)
    assert not off.detector_reading_log.enabled

    _n, _d, on, _p = bootstrap_runtime(
        demo=False, detector_reading_log_dir=tmp_path / "readings"
    )
    assert on.detector_reading_log.enabled
    assert on.detector_reading_log.path.startswith(str(tmp_path / "readings"))
