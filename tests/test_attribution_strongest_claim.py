"""The strongest claim among touching trains, and deletion never handing a
conflict block to the trains left (design_new.md "Block occupancy detection",
The strongest claim among touching trains; Deleting a train never hands its
current to another train).

These replay the rig run of 2026-11-07. TRAIN_4640 ran under ATO past its stop
in Up and on into Left. TRAIN_8677 stood on Main with no reservation, and
Main's far end leads into Left through a switch, so the stopped-train
either-side rule made it touch Left too. Left became a conflict and went to
neither train; TRAIN_4640, its loco now on Left, read clear everywhere and was
ghosted; the operator deleted it, and Left went to TRAIN_8677 — two locomotives
merged into one train.
"""

from block_occupancy import AttributionKind, AttributionResult
from constants import OCCUPANCY_OFF_THRESHOLD, OCCUPANCY_ON_THRESHOLD
from tests.test_attribution_conflict import build_two_trains, read
from tests.test_block_occupancy_detector import (
    FakeController,
    FakePathProjection,
    make_detector,
)


# ------------------------------------------------------------ detector level
#
#   UP — LEFT — (switch) — MAIN
#
# The moving / ghosted train is beside LEFT on UP; the standing train is on MAIN.
NEIGHBOURS = {"UP": {"LEFT"}, "LEFT": {"UP", "MAIN"}, "MAIN": {"LEFT"}}


def rig(directional, head_side=None, retained=None, frame=None):
    net, det = make_detector(
        ["UP", "LEFT", "MAIN"],
        [frame or {"UP": True, "LEFT": True, "MAIN": True}],
        on=1,
        neighbours=NEIGHBOURS,
        directional=directional,
        head_side=head_side or (lambda train_id, gained: "LEFT"),
    )
    det.retained_span = lambda train_id: (retained or {}).get(train_id, set())
    det.controllers.update({"T_UP": FakeController(), "T_MAIN": FakeController()})
    return net, det


def results_for(det, seeded):
    det.seed_occupied(seeded)
    entered, _ = det.step()
    return det.attribute(entered, set())


def test_a_train_driven_into_a_block_outranks_a_standing_train_beside_it():
    """The overrunning train is cut out alone; the standing train keeps its
    own blocks and never gains the one it was never driven into."""
    net, det = rig(directional=lambda train_id: train_id == "T_UP")
    net.blocks["UP"].occupied_by = "T_UP"
    net.blocks["MAIN"].occupied_by = "T_MAIN"
    det.controllers["T_UP"].path_projection = FakePathProjection(received_path_plan=object())

    results = results_for(det, {"UP", "MAIN"})

    assert results == [AttributionResult(AttributionKind.OVERRUN_ENTRY, "LEFT", "T_UP")]


def test_a_ghosted_train_gets_its_locomotive_back_over_a_standing_train():
    """A ghosted train has lost its locomotive next to the block; a standing
    train whose own blocks read current has not."""
    net, det = rig(
        directional=lambda train_id: False,
        retained={"T_UP": {"UP"}},
        frame={"UP": False, "LEFT": True, "MAIN": True},
    )
    net.blocks["MAIN"].occupied_by = "T_MAIN"

    results = results_for(det, {"MAIN"})

    assert results == [AttributionResult(AttributionKind.ENTRY, "LEFT", "T_UP")]


def test_two_standing_trains_are_still_a_conflict():
    net, det = rig(directional=lambda train_id: False)
    net.blocks["UP"].occupied_by = "T_UP"
    net.blocks["MAIN"].occupied_by = "T_MAIN"

    results = results_for(det, {"UP", "MAIN"})

    assert results == [
        AttributionResult(AttributionKind.CONFLICT, "LEFT", candidates=("T_MAIN", "T_UP"))
    ]


def test_a_driven_train_and_a_ghosted_train_are_a_conflict():
    """Both can account for new current, so neither is passed over."""
    net, det = rig(
        directional=lambda train_id: train_id == "T_UP",
        retained={"T_MAIN": {"MAIN"}},
        frame={"UP": True, "LEFT": True, "MAIN": False},
    )
    net.blocks["UP"].occupied_by = "T_UP"

    results = results_for(det, {"UP"})

    assert results == [
        AttributionResult(AttributionKind.CONFLICT, "LEFT", candidates=("T_MAIN", "T_UP"))
    ]


def test_an_orphaned_conflict_block_goes_to_an_unidentified_train_unmatched():
    """Even with one train still touching it, a conflict block a deleted train
    shared is not matched: it goes straight to an unidentified train."""
    notices = []
    net, det = rig(directional=lambda train_id: False)
    det.notify = lambda operation, message: notices.append(message)
    net.blocks["MAIN"].occupied_by = "T_MAIN"
    det.seed_occupied({"MAIN", "LEFT"})
    det.step()

    results = det.attribute(set(), set(), reoffered=(), orphaned={"LEFT"})

    assert results == [AttributionResult(AttributionKind.UNIDENTIFIED, "LEFT")]
    assert any("deleted" in message for message in notices)


# ------------------------------------------------------------ runtime level


def test_deleting_a_train_hands_its_conflict_block_to_an_unidentified_train():
    """T_W on BLK_1 and T_E on BLK_3 both stand next to BLK_2. Both lose
    detection and are ghosted, so they keep sharing the claim and BLK_2 stays a
    conflict. Deleting T_E must not give BLK_2 to T_W."""
    network, sim, detector_input = build_two_trains()
    for train_id in ("T_W", "T_E"):
        sim.controllers[train_id].powered = False
    detector_input.live.add("BLK_2")
    read(sim, OCCUPANCY_ON_THRESHOLD)
    assert network.blocks["BLK_2"].occupied_by is None

    detector_input.live.discard("BLK_1")
    detector_input.live.discard("BLK_3")
    read(sim, OCCUPANCY_OFF_THRESHOLD + 1)
    assert sim.controllers["T_W"].presence_unknown
    assert sim.controllers["T_E"].presence_unknown
    assert network.blocks["BLK_2"].occupied_by is None

    sim.remove_train("T_E")
    read(sim)

    # The read hands BLK_2 to a fresh unidentified train; Observe's occupancy
    # commit later in the tick writes `Block.occupied_by` from these sets.
    unidentified = [t for t, train in network.trains.items() if train.is_unidentified]
    assert len(unidentified) == 1
    entered = sim.controllers[unidentified[0]].physics_sync.pending_detected_entered_blocks
    assert "BLK_2" in entered
    assert "BLK_2" not in sim.controllers["T_W"].physics_sync.pending_detected_entered_blocks
