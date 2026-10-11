"""Holding-point chunking (design_new.md "Holding-point chunking").

A reservation stops at the first holding-point block ahead even when the track
past it is clear. Step `66aa` holds it there until the lock end reaches the
terminal, then extends to the next holding point. A train standing in the
terminal reaches it through the lock end's head-block fallback, so the hold
always releases once the onward track is reservable.
"""

from enums import Direction
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import TRACK_EW, Tile
from train import Train, TrainCar
from tests.journey.scenes.demo_test_scene import DemoTestScene, P2_BLOCK
from tests.segment_test_helpers import make_segment_block_with_tracks

LINE = ["BLK_1", "BLK_2", "BLK_3", "BLK_4", "BLK_5"]


def _line(holding_points=()):
    """Five two-tile blocks in a straight east-west line, a train on BLK_1."""
    network = RailwayNetwork()
    for x in range(2 * len(LINE)):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    for i, block_id in enumerate(LINE):
        make_segment_block_with_tracks(
            network, block_id, positions=[(2 * i, 0), (2 * i + 1, 0)],
            name=block_id, length_cm=100.0,
        )
        network.blocks[block_id].is_holding_point = block_id in holding_points
    train = Train(
        train_id="T1",
        consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)],
        max_speed=60.0,
    )
    network.add_train(train)
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, direction=Direction.EAST
    )
    controller._apply_head_frame("end_a")
    return network, dispatcher, sim


def test_a_fresh_reservation_stops_at_the_first_holding_point():
    network, dispatcher, _sim = _line(holding_points={"BLK_3"})

    result = dispatcher.create_path(
        train_id="T1", requested_destination_block_id="BLK_5"
    )

    assert result.success
    assert result.reached_target is False
    path = dispatcher.get_active_path("T1")
    assert path.get_reservation_end_block_id() == "BLK_3"
    assert network.blocks["BLK_4"].reserved_by_path is None


def test_an_extend_grows_to_the_next_holding_point_past_the_terminal():
    network, dispatcher, _sim = _line(holding_points={"BLK_3", "BLK_4"})
    path_id = dispatcher.create_path(
        train_id="T1", requested_destination_block_id="BLK_5"
    ).path_id

    result = dispatcher.extend_path(
        path_id=path_id, requested_destination_block_id="BLK_5"
    )

    assert result.success
    path = dispatcher.get_active_path("T1")
    assert path.get_reservation_end_block_id() == "BLK_4"
    # The next extend reaches the target: no holding point lies past BLK_4.
    dispatcher.extend_path(path_id=path_id, requested_destination_block_id="BLK_5")
    assert path.get_reservation_end_block_id() == "BLK_5"


def test_a_holding_point_at_the_target_adds_no_bound():
    _network, dispatcher, _sim = _line(holding_points={"BLK_5"})

    result = dispatcher.create_path(
        train_id="T1", requested_destination_block_id="BLK_5"
    )

    assert result.reached_target is True
    assert dispatcher.get_active_path("T1").get_reservation_end_block_id() == "BLK_5"


def test_step_66aa_holds_until_the_lock_end_reaches_the_terminal():
    network, dispatcher, sim = _line(holding_points={"BLK_3"})
    path_id = dispatcher.create_path(
        train_id="T1", requested_destination_block_id="BLK_5"
    ).path_id
    path = dispatcher.get_active_path("T1")
    executor = sim.route_executor

    # Only the floor block ahead of the head is locked: the lock end is behind
    # the holding-point terminal, so step 66aa holds the extend.
    network.block_manager.lock_blocks_for_path(["BLK_2"], path_id)
    assert path.get_lock_end_block_id() == "BLK_2"
    assert executor._held_at_holding_point("T1") is True

    # Once the lock reaches the terminal, the hold releases.
    network.block_manager.lock_blocks_for_path(["BLK_3"], path_id)
    assert path.get_lock_end_block_id() == "BLK_3"
    assert executor._held_at_holding_point("T1") is False


def test_a_route_through_a_holding_point_still_reaches_its_target():
    """End to end: the demo_test route p1 -> p2 runs through a holding point on
    its SHORT branch. The reservation stops there first, then the train carries
    on and arrives at p2 — the hold never deadlocks."""
    fixture = DemoTestScene().build()
    sim, network, train_id = fixture.sim_controller, fixture.network, fixture.train_id
    holding_point = "BLK_2FEE7D"
    network.blocks[holding_point].is_holding_point = True
    dispatcher = sim.path_dispatcher
    controller = sim.get_or_create_controller(train_id)

    saw_chunk_end = False
    arrived = False
    for _ in range(3000):
        sim.update(0.1)
        path = dispatcher.get_active_path(train_id)
        if path is not None and path.get_reservation_end_block_id() == holding_point:
            saw_chunk_end = True
        head = controller._get_current_block_id()
        if head == P2_BLOCK and controller.is_stopped() and path is None:
            arrived = True
            break

    assert saw_chunk_end, "the reservation never stopped at the holding point"
    assert arrived, "the train never arrived at p2"
    assert controller.eb.active is False
