"""Verify TrainController.travel_direction is a derived property.

Per design_new.md, travel_direction is computed from
(self.active_cab, self.current_span) on every read. There is no setter.
Calling controller.commands.set_cab(...) must flip travel_direction immediately on
the next read — no stale field, no cache.

Note: this test exercises the read-only property contract. It does not
exercise the full set_cab() side-effects (which currently invoke
train.set_cab() that touches removed enums in production train.py — that
is a documented Bucket C regression).
"""

import pytest
from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from enums import Direction, LeadingEnd
from train import Train, TrainCar, TrainPosition
from train_controller import create_controller_for_train


def _build_two_tile_network() -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(3):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    return network


def _make_train(network: RailwayNetwork) -> Train:
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_CAB", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, 0, 0, direction=Direction.EAST)
    return train


def test_travel_direction_has_no_setter():
    """travel_direction is a property; assignment raises AttributeError."""
    network = _build_two_tile_network()
    train = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)

    with pytest.raises(AttributeError):
        controller.travel_direction = "EAST"


def test_travel_direction_none_when_no_cab():
    """No cab selected -> travel_direction is None on read."""
    network = _build_two_tile_network()
    train = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)

    # No cab set yet.
    controller.commands.cab = None
    assert controller.travel_direction is None


def test_travel_direction_flips_when_facing_flips():
    """Reversing the train's facing direction flips travel_direction on next read.

    We bypass the controller.commands.set_cab() machinery (Bucket C regression in
    production train.py) and directly flip TrainPosition.leading_end + entry
    edges, which is what the cab swap is supposed to do at the geometry level.
    The point of this test is to verify the property is *derived*, not stored.
    """
    network = _build_two_tile_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_FLIP", consist=[car], max_speed=60.0)
    network.add_train(train)

    # Start facing EAST (train at tile (0,0) -> (1,0)).
    train.position = TrainPosition(
        end_a_tile=(1, 0),
        end_a_distance_cm=10.0,
        end_a_entry_edge="W",
        end_b_tile=(0, 0),
        end_b_distance_cm=10.0,
        end_b_entry_edge="W",
        leading_end=LeadingEnd.A,
        occupied_tiles=[(0, 0), (1, 0)],
        occupied_blocks=[],
    )

    controller = create_controller_for_train(train.train_id, network)
    controller.commands.cab = "A"

    # First read: EAST.
    direction_a = controller.travel_direction
    assert direction_a in ("EAST", "E")

    # Reverse facing by flipping leading_end and entry edges.
    train.position.leading_end = LeadingEnd.B
    train.position.end_a_entry_edge = "E"
    train.position.end_b_entry_edge = "E"
    controller.commands.cab = "B"

    # Next read recomputes; no setter, no cache to invalidate.
    direction_b = controller.travel_direction
    assert direction_b in ("WEST", "W")
    # And critically, it is *different* from the prior read on the same
    # controller instance — proving the property is not cached.
    assert direction_a != direction_b


def test_travel_direction_recomputes_per_read():
    """Calling travel_direction twice with the same inputs returns the same value
    (consistency), but flipping the inputs between reads changes the output."""
    network = _build_two_tile_network()
    train = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)
    controller.commands.cab = "A"

    a = controller.travel_direction
    b = controller.travel_direction
    assert a == b

    # Now invalidate by clearing active_cab.
    controller.commands.cab = None
    c = controller.travel_direction
    assert c is None
    assert a is not None
