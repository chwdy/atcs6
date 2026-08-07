"""Tests for the snap-to-seam reconciliation in
TrainPhysics.reconcile_to_detected_occupancy.

Physical detection outranks the calculated position: when the current detector
confirms a block entered/vacated that the calculated span does not yet agree
with, the span is slid forward to the seam. This is inert under the v1 simulated
sensor (current is derived from the span), so these tests drive the detector
slice directly to exercise the geometry as a real/early sensor would.
"""

from typing import Dict, Set

from block import Block
from block_occupancy import OccupancyInput
from enums import Direction
from network import RailwayNetwork
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar
from entity_ids import segment_id_for_block


class FakeDetectorInput(OccupancyInput):
    """A detector that answers for `detected` and reports current on `live`."""

    def __init__(self, detected: Set[str], live: Set[str]) -> None:
        self.detected = set(detected)
        self.live = set(live)

    def detected_block_ids(self) -> Set[str]:
        return set(self.detected)

    def read_block_currents(self) -> Dict[str, bool]:
        return {bid: bid in self.live for bid in self.detected}


def build_line(num_tiles: int = 6) -> RailwayNetwork:
    """A straight E-W line, one block per tile (BLK_0 .. BLK_{n-1})."""
    network = RailwayNetwork()
    for x in range(num_tiles):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        network.blocks[f"BLK_{x}"] = Block(block_id=f"BLK_{x}", length_cm=100.0)
    return network


def place(network, train_id, head_x, length_cm):
    sim = SimulationController(network)
    train = Train(
        train_id=train_id,
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=length_cm)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    span = network.place_train(train, head_x, 0, Direction.EAST)
    controller.physics_sync.current_span = span
    physics = sim._get_or_create_physics(train)
    return sim, train, controller, physics


def test_snap_entry_advances_head_to_seam():
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    span = controller.physics_sync.current_span
    assert "BLK_1" not in (span.occupied_blocks or [])

    # Detector confirmed BLK_1 entered before the calculated head reached it.
    controller.physics_sync.pending_detected_entered_blocks = {"BLK_1"}
    moved = physics.reconcile_to_detected_occupancy(train, controller)

    assert moved is True
    span = controller.physics_sync.current_span
    assert "BLK_1" in span.occupied_blocks
    # Head (end_a) snapped to BLK_1's first tile at its entry seam (distance 0).
    assert span.end_a.tile == (1, 0)
    assert span.end_a.distance_cm == 0.0


def test_snap_inert_when_span_already_covers_block():
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    before = controller.physics_sync.current_span

    # The detector confirms a block the span already covers -> nothing to snap.
    controller.physics_sync.pending_detected_entered_blocks = {"BLK_0"}
    moved = physics.reconcile_to_detected_occupancy(train, controller)

    assert moved is False
    assert controller.physics_sync.current_span is before


def test_snap_exit_slides_tail_out_of_block():
    network = build_line(6)
    # 250cm train with head at tile 2's exit edge spans BLK_2, BLK_1, BLK_0.
    sim, train, controller, physics = place(network, "T", head_x=2, length_cm=250.0)
    span = controller.physics_sync.current_span
    assert "BLK_0" in span.occupied_blocks

    # Detector confirmed the tail cleared BLK_0 before the calculated tail did.
    controller.physics_sync.pending_detected_vacated_blocks = {"BLK_0"}
    moved = physics.reconcile_to_detected_occupancy(train, controller)

    assert moved is True
    assert "BLK_0" not in controller.physics_sync.current_span.occupied_blocks


def test_snap_stops_at_a_detected_block_that_reads_clear():
    """The correction never crosses a boundary the cap would not let the train
    cross under power.

    BLK_2 is confirmed and handed to the train, but BLK_1 between them reads
    clear. Reaching BLK_2 would mean having crossed track whose detector says
    nothing is on it, so the span stays where the detector can vouch for it.
    """
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={f"BLK_{i}" for i in range(6)}, live={"BLK_0", "BLK_2"}
    )
    sim.block_occupancy_detector.seed_occupied({"BLK_0", "BLK_2"})
    before = controller.physics_sync.current_span

    controller.physics_sync.pending_detected_entered_blocks = {"BLK_2"}
    moved = physics.reconcile_to_detected_occupancy(train, controller)

    assert moved is False
    assert controller.physics_sync.current_span is before
    assert controller.physics_sync.current_span.occupied_blocks == ["BLK_0"]


def test_snap_crosses_a_detected_block_that_reads_occupied():
    """The contrast: with BLK_1 confirmed too, the run of confirmed blocks is
    unbroken and the span follows the detector all the way into BLK_2."""
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={f"BLK_{i}" for i in range(6)},
        live={"BLK_0", "BLK_1", "BLK_2"},
    )
    sim.block_occupancy_detector.seed_occupied({"BLK_0", "BLK_1", "BLK_2"})

    controller.physics_sync.pending_detected_entered_blocks = {"BLK_1", "BLK_2"}
    moved = physics.reconcile_to_detected_occupancy(train, controller)

    assert moved is True
    assert "BLK_2" in controller.physics_sync.current_span.occupied_blocks


def test_snap_no_op_with_empty_slices():
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    before = controller.physics_sync.current_span

    moved = physics.reconcile_to_detected_occupancy(train, controller)

    assert moved is False
    assert controller.physics_sync.current_span is before


# ---------------------------------------------------------------------------
# A large correction is reported (design_new.md "Power")
# ---------------------------------------------------------------------------


def overrun_events(sim):
    """The overrun warnings on the stream, newest last."""
    return [
        n
        for n in sim.notification_center.peek_recent(200)
        if getattr(n, "operation", None) == "reconcile_to_detected_occupancy"
    ]


def test_an_ordinary_one_block_correction_is_not_reported():
    """The cap parks the head on the next block's entry seam, so every normal
    entry gains exactly that one block once it confirms. Reporting it would
    bury the stream in sensor latency."""
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={f"BLK_{i}" for i in range(6)}, live={"BLK_0", "BLK_1"}
    )
    sim.block_occupancy_detector.seed_occupied({"BLK_0", "BLK_1"})
    controller.physics_sync.pending_detected_entered_blocks = {"BLK_1"}

    assert physics.reconcile_to_detected_occupancy(train, controller) is True
    assert overrun_events(sim) == []


def test_a_two_block_correction_is_reported_as_an_overrun():
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={f"BLK_{i}" for i in range(6)},
        live={"BLK_0", "BLK_1", "BLK_2"},
    )
    sim.block_occupancy_detector.seed_occupied({"BLK_0", "BLK_1", "BLK_2"})
    controller.physics_sync.pending_detected_entered_blocks = {"BLK_1", "BLK_2"}

    assert physics.reconcile_to_detected_occupancy(train, controller) is True

    events = overrun_events(sim)
    assert len(events) == 1
    event = events[0]
    assert event.kind == "warning"
    assert event.error.origin == "span_overrun_correction"
    assert event.payload["blocks_gained"] == ["BLK_1", "BLK_2"]
    assert event.payload["correction_cm"] > 0
    assert event.payload["train_id"] == "T"


def test_a_correction_that_moves_nothing_reports_nothing():
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)

    assert physics.reconcile_to_detected_occupancy(train, controller) is False
    assert overrun_events(sim) == []
