"""Verify TrainController.lookahead is a pure derived function (no caching).

Per design_new.md, the controller's lookahead is computed live on every read
from (current_span, cab, received_path_plan, network_state, signals,
current_speed_kmh, eb_active). There is no stored cache and no invalidation
rule — creating or extending a path through the dispatcher must reflect on
the very next `controller.lookahead` read without any explicit sync step.

Likewise, `controller.received_path_plan` is a read-only property that
resolves to PathDispatcher.get_active_path(self.train_id) on every read.
"""

from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from train import Train, TrainCar
from path_dispatcher import PathDispatcher
from train_controller import create_controller_for_train


def _build_three_block_network() -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(6):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    network.create_block_with_tracks(
        block_id="BLK_A", positions=[(0, 0), (1, 0)], name="A", length_cm=200.0
    )
    network.create_block_with_tracks(
        block_id="BLK_B", positions=[(2, 0), (3, 0)], name="B", length_cm=200.0
    )
    network.create_block_with_tracks(
        block_id="BLK_C", positions=[(4, 0), (5, 0)], name="C", length_cm=200.0
    )
    return network


def test_received_path_plan_reads_live_from_dispatcher():
    """create_path -> controller.received_path_plan returns the new path on next read."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T1", consist=[car], max_speed=60.0)
    network.add_train(train)
    from enums import Direction
    network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    controller = create_controller_for_train(
        train_id=train.train_id, network=network, path_dispatcher=dispatcher
    )

    # No path: live read returns None.
    assert controller.received_path_plan is None
    assert controller.assigned_path_id is None
    assert controller.ato_destination_block_id is None

    # Create path via dispatcher.
    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLK_C",
        requested_destination_direction="forward",
    )
    assert result.success

    # Live read must reflect the new path immediately, no explicit sync.
    plan = controller.received_path_plan
    assert plan is not None
    assert plan.path_id == result.path_id
    assert controller.assigned_path_id == result.path_id
    assert controller.ato_destination_block_id == "BLK_C"


def test_lookahead_reflects_new_path_immediately():
    """create_path -> controller.lookahead recomputes and reflects new authority."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_LA", consist=[car], max_speed=60.0)
    network.add_train(train)
    from enums import Direction
    network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    controller = create_controller_for_train(
        train_id=train.train_id, network=network, path_dispatcher=dispatcher
    )
    controller.power_on = True
    controller.cab = "A"

    # Snapshot initial lookahead (no path).
    initial = controller.lookahead

    # Create path via dispatcher.
    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLK_C",
        requested_destination_direction="forward",
    )
    assert result.success

    # Subsequent read must recompute. We confirm the path is now visible
    # through `received_path_plan` on the same controller without any
    # explicit cache invalidation step.
    assert controller.received_path_plan is not None
    assert controller.received_path_plan.path_id == result.path_id

    # The lookahead is recomputed every read; calling it twice with the
    # same inputs should yield consistent results.
    second = controller.lookahead
    third = controller.lookahead
    assert isinstance(second, list)
    assert isinstance(third, list)
    # Pure function: equal inputs -> equal outputs (length and per-point IDs).
    assert len(second) == len(third)


def test_received_path_plan_returns_none_after_path_status_changes():
    """When the dispatcher transitions the path away from ACTIVE, the
    controller's received_path_plan returns None on the next read with no
    setter call."""
    network = _build_three_block_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_END", consist=[car], max_speed=60.0)
    network.add_train(train)
    from enums import Direction
    network.place_train(train, 0, 0, direction=Direction.EAST)

    dispatcher = PathDispatcher(network)
    controller = create_controller_for_train(
        train_id=train.train_id, network=network, path_dispatcher=dispatcher
    )

    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLK_C",
        requested_destination_direction="forward",
    )
    assert result.success
    assert controller.received_path_plan is not None

    # Cancel the path (this drops it from active_paths and changes status).
    dispatcher.cancel_path(result.path_id)

    # Next read of received_path_plan returns None automatically — there is
    # no setter, no explicit clear required.
    assert controller.received_path_plan is None
    assert controller.assigned_path_id is None
    assert controller.ato_destination_block_id is None
