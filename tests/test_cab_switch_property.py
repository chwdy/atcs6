"""Verify TrainController.travel_direction is a derived property.

Per design_new.md, travel_direction is computed from
(self.commands.cab, self.physics_sync.current_span) on every read. There is no
setter. Selecting a different cab must flip travel_direction immediately on the
next read — no stale field, no cache. v1: head/tail derive from the cab
(cab "A" -> end_a is head, "B" -> end_b), so swapping the cab swaps which
physical end is the head and flips the network-frame direction.
"""

import pytest
from network import RailwayNetwork
from track import TrackSegment, TRACK_EW
from enums import Direction
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from train_controller import create_controller_for_train


def _build_two_tile_network() -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(3):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    return network


def _make_train(network: RailwayNetwork):
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_CAB", consist=[car], max_speed=60.0)
    network.add_train(train)
    span = network.place_train(train, 0, 0, direction=Direction.EAST)
    return train, span


def test_travel_direction_has_no_setter():
    """travel_direction is a property; assignment raises AttributeError."""
    network = _build_two_tile_network()
    train, _ = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)

    with pytest.raises(AttributeError):
        controller.travel_direction = "EAST"


def test_travel_direction_none_when_no_cab():
    """No cab selected -> travel_direction is None on read."""
    network = _build_two_tile_network()
    train, span = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)
    controller.physics_sync.current_span = span

    controller.commands.cab = None
    assert controller.travel_direction is None


def test_travel_direction_flips_when_cab_flips():
    """Switching the cab flips travel_direction on the next read.

    v1: head/tail derive from the cab. Cab "A" makes end_a the head; cab "B"
    makes end_b the head. For a two-tile span end_a=(1,0)/end_b=(0,0), cab "A"
    points EAST (tail (0,0) -> head (1,0)) and cab "B" points WEST. The point of
    this test is that travel_direction is *derived* (recomputed per read), not
    stored or cached.
    """
    network = _build_two_tile_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_FLIP", consist=[car], max_speed=60.0)
    network.add_train(train)

    controller = create_controller_for_train(train.train_id, network)
    # Two-tile span: end_a at (1,0), end_b at (0,0).
    controller.physics_sync.current_span = TrainSpan(
        end_a=SpanEndLocator(tile=(1, 0), distance_cm=10.0, entry_edge="W"),
        end_b=SpanEndLocator(tile=(0, 0), distance_cm=10.0, entry_edge="W"),
        occupied_tiles=[(0, 0), (1, 0)],
        occupied_blocks=[],
    )

    controller.commands.cab = "A"
    direction_a = controller.travel_direction
    assert direction_a in ("EAST", "E")

    # Switch the cab: head becomes end_b, so the network-frame direction flips.
    controller.commands.cab = "B"
    direction_b = controller.travel_direction
    assert direction_b in ("WEST", "W")
    # Different from the prior read on the same controller instance — proving
    # the property is recomputed, not cached.
    assert direction_a != direction_b


def test_travel_direction_recomputes_per_read():
    """Reading travel_direction twice with the same inputs is consistent, but
    clearing the cab between reads changes the output (derived, not cached)."""
    network = _build_two_tile_network()
    train, span = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)
    controller.physics_sync.current_span = span
    controller.commands.cab = "A"

    a = controller.travel_direction
    b = controller.travel_direction
    assert a == b

    # Now invalidate by clearing the active cab.
    controller.commands.cab = None
    c = controller.travel_direction
    assert c is None
    assert a is not None
