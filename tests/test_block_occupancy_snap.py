"""Tests for the snap-to-seam reconciliation in
TrainPhysics.reconcile_to_detected_occupancy.

Physical detection outranks the calculated position: when the current detector
confirms a block entered/vacated that the calculated span does not yet agree
with, the span is slid forward to the seam. This is inert under the v1 simulated
sensor (current is derived from the span), so these tests drive the detector
slice directly to exercise the geometry as a real/early sensor would.
"""

from block import Block
from enums import Direction
from network import RailwayNetwork
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar


def build_line(num_tiles: int = 6) -> RailwayNetwork:
    """A straight E-W line, one block per tile (BLK_0 .. BLK_{n-1})."""
    network = RailwayNetwork()
    for x in range(num_tiles):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = f"BLK_{x}"
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


def test_snap_no_op_with_empty_slices():
    network = build_line(6)
    sim, train, controller, physics = place(network, "T", head_x=0, length_cm=40.0)
    before = controller.physics_sync.current_span

    moved = physics.reconcile_to_detected_occupancy(train, controller)

    assert moved is False
    assert controller.physics_sync.current_span is before
