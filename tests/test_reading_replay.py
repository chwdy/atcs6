"""The replay library (`tools/reading_replay.py`) and the cutting tool
(`tools/cut_reading_log.py`).

These check that a replay feeds the runtime exactly what the rig read:

- folding the change-only lines gives every block's reading at every pass, and a
  file that cannot be folded (no first line listing every block, lines missing)
  is refused rather than replayed wrong;
- a cut taken from the middle of a run holds the same readings as the run;
- a replayed run writes a detector reading log with the same readings, pass by
  pass, as the recording it replays;
- a pass the replayed runtime does not read is reported, not skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bootstrap import create_demo_network
from enums import OperationMode
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from tools import cut_reading_log
from tools.reading_replay import (
    ReadingPlayer,
    ReplayDiverged,
    ReplayOccupancyInput,
    fold_lines,
    load_passes,
    read_lines,
)

RECORDING = Path(__file__).parent / "replay" / "2026-11-07-two-locos-at-start.jsonl"


def line(n, readings=None, full=None, kind="tick"):
    out = {
        "pass": n,
        "pass_kind": kind,
        "wall_time": 1000.0 + n,
        "simulation_time": n * 0.1,
        "delta_seconds": 0.1,
        "operating_delta_seconds": 0.1,
    }
    if full is not None:
        out.update(full=full, environment="real_hardware", on_threshold=2, off_threshold=10)
    if readings is not None:
        out["readings"] = readings
    return out


def on(**blocks):
    return {b: {"present": v} for b, v in blocks.items()}


def test_folding_gives_every_block_at_every_pass():
    passes = fold_lines([
        line(1, on(A=True, B=False), full="start"),
        line(2),
        line(3, on(B=True)),
        line(4, on(A=False)),
    ])
    assert [p.present() for p in passes] == [
        {"A": True, "B": False},
        {"A": True, "B": False},
        {"A": True, "B": True},
        {"A": False, "B": True},
    ]


def test_a_line_listing_every_block_replaces_the_state():
    passes = fold_lines([
        line(1, on(A=True, B=True), full="start"),
        line(2, on(C=True), full="layout"),
    ])
    assert passes[1].present() == {"C": True}


def test_a_file_whose_first_line_lists_no_blocks_is_refused():
    with pytest.raises(ValueError, match="first line must list every block"):
        fold_lines([line(5, on(A=True))])


def test_missing_lines_without_a_full_line_are_refused():
    with pytest.raises(ValueError, match="lines missing"):
        fold_lines([line(1, on(A=True), full="start"), line(3)])
    # The log writes a full line after a failed write, which is foldable.
    passes = fold_lines([
        line(1, on(A=True), full="start"),
        line(3, on(A=False), full="write_failure"),
    ])
    assert passes[1].present() == {"A": False}


def test_a_cut_from_the_middle_holds_the_runs_readings(tmp_path):
    whole = load_passes(RECORDING)
    out = tmp_path / "cut.jsonl"
    cut_reading_log.main([str(RECORDING), "--from-pass", "50", "--to-pass", "120", "-o", str(out)])

    cut_lines = read_lines(out)
    assert cut_lines[0]["full"] == "cut"
    assert cut_lines[0]["environment"] == "real_hardware"
    cut = load_passes(out)
    assert [p.pass_no for p in cut] == list(range(50, 121))
    by_no = {p.pass_no: p for p in whole}
    assert all(p.readings == by_no[p.pass_no].readings for p in cut)


def build_replay(passes, environment_active=True):
    replay_input = ReplayOccupancyInput(passes)
    network = create_demo_network(scene="demo7_1")
    sim = SimulationController(
        network,
        PathDispatcher(network),
        operation_mode=OperationMode.REAL_HARDWARE,
        input_interface=replay_input,
        environment_active=environment_active,
    )
    return sim, ReadingPlayer(sim, replay_input, passes)


def test_a_replayed_run_reads_what_the_rig_read(tmp_path):
    passes = load_passes(RECORDING)
    sim, player = build_replay(passes)
    sim.detector_reading_log.enable(tmp_path)

    player.play_to()

    sim.detector_reading_log.close()
    [written] = list(tmp_path.glob("readings-*.jsonl"))
    replayed = load_passes(written)
    assert len(replayed) == len(passes)
    for original, again in zip(passes, replayed):
        assert again.pass_kind == original.pass_kind
        # Each pass runs at its recorded wall time (design_v2.md "Detector
        # reading log", A recorded run can be replayed in a test).
        assert again.wall_time == original.wall_time
        assert again.readings == original.readings, f"pass {original.pass_no}"


def test_a_pass_the_replay_does_not_read_is_reported():
    passes = load_passes(RECORDING, to_pass=5)
    _sim, player = build_replay(passes, environment_active=False)
    with pytest.raises(ReplayDiverged, match="pass 1 was a 'tick' pass"):
        player.play_to(1)


def test_the_player_refuses_a_controller_that_reads_something_else():
    passes = load_passes(RECORDING, to_pass=5)
    network = create_demo_network(scene="demo7_1")
    sim = SimulationController(network, PathDispatcher(network))
    with pytest.raises(ValueError, match="does not read this ReplayOccupancyInput"):
        ReadingPlayer(sim, ReplayOccupancyInput(passes), passes)


def test_the_summary_lists_the_passes_where_a_block_changed(tmp_path, capsys):
    log = tmp_path / "readings.jsonl"
    log.write_text("".join(
        json.dumps(x) + "\n"
        for x in [
            line(1, on(A=True, B=False), full="start"),
            line(2, {"A": {"present": True, "current_ma": 30.1}}),
            line(3, on(B=True)),
        ]
    ))
    cut_reading_log.main([str(log), "--summary"])
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 2, "a change of current alone is not a change of present"
    assert printed[0].startswith("pass      1  full=start")
    assert printed[1].startswith("pass      3") and printed[1].endswith("B:on")
