"""The near end reaches the farthest block the calibration train stands in.

design_new.md "Speed calibration", The near end holds only the blocks the train
stands in: the near end is its outer block and the blocks the points as set lead
to out from it, up to and including the farthest one the train stands in, so a
train that rolled past a short run-off block and stopped wholly in the next one
still stands in the near end. "The pass first moves the train into an end of
the stretch": a later leg that finds the train outside both ends first drives it
into the nearer end, as at opening.

The track is a straight line A | B | C | R1 | R2, with B the measured block and
A and C its outer blocks.
"""

from __future__ import annotations

import pytest

from network import RailwayNetwork
from speed_calibration import (
    PHASE_LEG,
    PHASE_MOVING,
    CalibrationObservation,
    CalibrationRun,
    SpeedCalibration,
)
from tests.segment_test_helpers import make_segment_block_with_tracks
from track import TRACK_EW, Tile

TRAIN = "T"
TOWARD_A = 0  # a leg toward A, so C is the near end


@pytest.fixture
def network():
    net = RailwayNetwork()
    for x, block_id in enumerate(["A", "B", "C", "R1", "R2"]):
        net.add_track(Tile(x, 0, track_bits=TRACK_EW))
        make_segment_block_with_tracks(net, block_id, [(x, 0)], length_cm=20.0)
    return net


def _near_end(network):
    (_far, near), _positions = network.block_manager.runoff_blocks(
        "B", ("A", "C"), 10.0, TRAIN, TOWARD_A
    )
    return near


def test_a_train_past_a_block_it_rolled_through_still_stands_in_the_near_end(network):
    network.blocks["R2"].occupied_by = TRAIN
    assert _near_end(network) == ("R1", "R2")


def test_the_near_end_stops_at_the_farthest_block_the_train_stands_in(network):
    network.blocks["R1"].occupied_by = TRAIN
    assert _near_end(network) == ("R1",)


def test_a_train_only_in_the_outer_block_has_no_near_run_off_blocks(network):
    network.blocks["C"].occupied_by = TRAIN
    assert _near_end(network) == ()


def test_a_later_leg_finding_the_train_outside_both_ends_moves_it_first():
    calibration = SpeedCalibration()
    run = CalibrationRun(
        train_id=TRAIN,
        profile_id="P",
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        heading_to=TOWARD_A,
        phase=PHASE_LEG,
    )
    run.leg_armed = False
    run.leg_authority_seen = True  # left over from the leg that just ended
    obs = CalibrationObservation(
        now=10.0,
        is_stopped=True,
        head_block="ELSEWHERE",
        outer_block_index=None,
    )

    command = calibration.advance(run, obs)

    assert run.phase == PHASE_MOVING
    assert command is not None and command.move_into_place
    assert command.arm_leg_to in (None, "")


def test_a_later_leg_finding_the_train_still_on_b_moves_it_first():
    """design_new.md "Speed calibration", The pass first moves the train into an
    end of the stretch: a later leg that finds the train on B — here its span
    reaches into end A, but B still holds it — drives it into the nearer end
    before it runs the leg."""
    calibration = SpeedCalibration()
    run = CalibrationRun(
        train_id=TRAIN,
        profile_id="P",
        measured_block_id="B",
        outer_block_ids=("A", "C"),
        heading_to=TOWARD_A,
        phase=PHASE_LEG,
    )
    run.leg_armed = False
    obs = CalibrationObservation(
        now=10.0,
        is_stopped=True,
        head_block="B",
        outer_block_index=0,
        measured_block_held=True,
    )

    command = calibration.advance(run, obs)

    assert run.phase == PHASE_MOVING
    assert command is not None and command.move_into_place
