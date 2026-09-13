"""Verify TrainController.travel_direction is a derived property.

Per design_new.md:2283, travel_direction is computed from
(received_path_plan, current_span) on every read. There is no setter and no
operator cab. It is None when there is no live authority (received_path_plan is
None). When authority exists, the head is the authority-framed physical span end
(design_new.md "Authority-side head derivation"), so re-framing the span to the
other head end swaps which physical end is the head and flips the network-frame
direction on the next read — no stale field, no cache.
"""

from types import SimpleNamespace

import pytest
from network import RailwayNetwork
from track import Tile, TRACK_EW
from enums import Direction
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from train_controller import create_controller_for_train


def _build_two_tile_network() -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(3):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    return network


def _make_train(network: RailwayNetwork):
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_CAB", consist=[car], max_speed=60.0)
    network.add_train(train)
    span = network.place_train(train, 0, 0, direction=Direction.EAST)
    return train, span


def _give_authority(controller) -> None:
    """Make ``received_path_plan`` read non-None by stubbing the dispatcher.

    travel_direction only checks that a plan exists and then reads the span
    frame, so an empty ``resource_sequence`` is enough to lift the no-authority
    gate for this property test.
    """
    fake_path = SimpleNamespace(path_id="P", resource_sequence=[])
    controller.path_dispatcher = SimpleNamespace(
        get_active_path=lambda _train_id: fake_path
    )


def test_travel_direction_has_no_setter():
    """travel_direction is a property; assignment raises AttributeError."""
    network = _build_two_tile_network()
    train, _ = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)

    with pytest.raises(AttributeError):
        controller.travel_direction = "EAST"


def test_travel_direction_none_when_no_reservation():
    """No live authority (received_path_plan is None) -> travel_direction None."""
    network = _build_two_tile_network()
    train, span = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)
    controller.physics_sync.current_span = span

    # No dispatcher / no active path -> no authority.
    assert controller.travel_direction is None


def test_travel_direction_flips_when_head_frame_flips():
    """Re-framing the head to the other span end flips travel_direction.

    The head is the authority-framed physical end. For a two-tile span
    end_a=(1,0)/end_b=(0,0), framing end_a as head points EAST (tail (0,0) ->
    head (1,0)) and framing end_b as head points WEST. The point of this test is
    that travel_direction is *derived* (recomputed per read), not stored.
    """
    network = _build_two_tile_network()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_FLIP", consist=[car], max_speed=60.0)
    network.add_train(train)

    controller = create_controller_for_train(train.train_id, network)
    _give_authority(controller)
    # Two-tile span: end_a at (1,0), end_b at (0,0).
    controller.physics_sync.current_span = TrainSpan(
        end_a=SpanEndLocator(tile=(1, 0), distance_cm=10.0, entry_edge="W"),
        end_b=SpanEndLocator(tile=(0, 0), distance_cm=10.0, entry_edge="W"),
        occupied_tiles=[(0, 0), (1, 0)],
        occupied_blocks=[],
    )

    assert controller._apply_head_frame("end_a") is None
    direction_a = controller.travel_direction
    assert direction_a in ("EAST", "E")

    # Re-frame the head to end_b: the network-frame direction flips.
    assert controller._apply_head_frame("end_b") is None
    direction_b = controller.travel_direction
    assert direction_b in ("WEST", "W")
    # Different from the prior read on the same controller instance — proving
    # the property is recomputed, not cached.
    assert direction_a != direction_b


def test_travel_direction_recomputes_per_read():
    """Reading travel_direction twice with the same inputs is consistent, but
    dropping authority between reads changes the output (derived, not cached)."""
    network = _build_two_tile_network()
    train, span = _make_train(network)
    controller = create_controller_for_train(train.train_id, network)
    controller.physics_sync.current_span = span
    _give_authority(controller)

    a = controller.travel_direction
    b = controller.travel_direction
    assert a == b

    # Now invalidate by dropping the live authority.
    controller.path_dispatcher = SimpleNamespace(
        get_active_path=lambda _train_id: None
    )
    c = controller.travel_direction
    assert c is None
    assert a is not None
