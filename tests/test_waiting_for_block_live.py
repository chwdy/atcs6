"""Live recompute of ``waiting_for_block_id`` (design_new.md "API payload ownership").

Per the canonical "Data Flow To UI" contract, the dispatcher-progress field
``waiting_for_block_id`` is "recomputed live on each read, never stored — re-runs
the same shared pathfinder/eligibility walk ``extend_path(...)`` uses toward the
stored requested target and reports the block owning the first forward resource
that fails acquisition because of live occupancy/reservation/lock conflict; it is
``None`` when the authority reaches its target or no viable suffix exists."

Before this change the field was a stored ``Path`` attribute that ``create_path``
truncation never wrote, so a best-effort path truncated at creation reported
``None`` even while visibly stuck behind an occupied block. These tests pin the
live behavior: the projection reports the true blocker, and clears the moment the
obstruction frees — without any ``extend_path`` call refreshing a stored field.
"""

from network import RailwayNetwork
from track import Tile, TRACK_EW
from train import Train, TrainCar
from enums import Direction
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from tests.train_test_helpers import set_train_span
from tests.segment_test_helpers import make_segment_block_with_tracks


def _linear_network():
    """A - B - C - D - E linear EW corridor, two tiles per block."""
    net = RailwayNetwork()
    for x in range(0, 10):
        net.add_track(Tile(x, 0, track_bits=TRACK_EW))
    for bid, (a, b) in [
        ("BLOCK_A", (0, 1)),
        ("BLOCK_B", (2, 3)),
        ("BLOCK_C", (4, 5)),
        ("BLOCK_D", (6, 7)),
        ("BLOCK_E", (8, 9)),
    ]:
        make_segment_block_with_tracks(net, bid, positions=[(a, 0), (b, 0)], name=bid, length_cm=100.0
        )
    return net


def _place(net, train_id, tile_x, block_id):
    car = TrainCar(car_id=f"{train_id}_CAR", car_type="LOCOMOTIVE", length_cm=40.0)
    train = Train(train_id=train_id, consist=[car], max_speed=60.0)
    net.add_train(train)
    net.place_train(train, tile_x, 0, Direction.EAST)
    net.block_manager.occupy_block(block_id, train_id)
    return train


def test_create_truncated_path_reports_live_blocker():
    """A best-effort path truncated at create_path time reports the occupied
    block ahead live, and clears once that block frees.

    The stored-field implementation reported ``None`` here (create_path never
    wrote the stored ``waiting_for_block_id``); the live recompute reports the
    real blocker.
    """
    net = _linear_network()
    t1 = _place(net, "T1", 0, "BLOCK_A")  # routing train, head in BLOCK_A
    _place(net, "T2", 4, "BLOCK_C")  # blocker occupying BLOCK_C

    disp = PathDispatcher(net)
    sim = SimulationController(net, disp)
    sc = sim._get_or_create_controller(t1)
    set_train_span(
        sc, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLOCK_A"]
    )

    # Route A -> E; BLOCK_C is occupied, so best-effort create truncates short.
    res = disp.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_E"
    )
    assert res.success
    path = disp.get_path(res.path_id)
    # Truncated short of the requested target (authority did not reach E).
    assert path.get_authority_end_block_id() != "BLOCK_E"
    assert path.requested_destination_block_id == "BLOCK_E"

    progress = disp.get_active_path_progress("T1")
    assert progress is not None
    # Live recompute names the occupied block that stops forward growth.
    assert progress["waiting_for_block_id"] == "BLOCK_C"

    # Free the blocker: the suffix toward E becomes acquirable, so the live
    # projection clears to None on the very next read (no extend_path needed).
    net.block_manager.release_block("BLOCK_C")
    progress_after = disp.get_active_path_progress("T1")
    assert progress_after is not None
    assert progress_after["waiting_for_block_id"] is None


def test_path_reaching_target_reports_none():
    """When the authority already reaches its requested target the live
    projection is None (nothing is blocking it)."""
    net = _linear_network()
    t1 = _place(net, "T1", 0, "BLOCK_A")

    disp = PathDispatcher(net)
    sim = SimulationController(net, disp)
    sc = sim._get_or_create_controller(t1)
    set_train_span(
        sc, (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLOCK_A"]
    )

    # Nothing ahead is occupied; A -> C succeeds end-to-end.
    res = disp.create_path(
        train_id="T1", requested_destination_block_id="BLOCK_C"
    )
    assert res.success
    path = disp.get_path(res.path_id)
    assert path.get_authority_end_block_id() == "BLOCK_C"

    progress = disp.get_active_path_progress("T1")
    assert progress is not None
    assert progress["waiting_for_block_id"] is None
